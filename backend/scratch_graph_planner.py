"""
scratch_graph_planner.py — Scratch Workflow Builder (without capabilities)

Implements the staged pipeline for building a LangGraph workflow from scratch
when the user chose NOT to reuse any capabilities. All nodes are GENERATE nodes.

Pipeline (v2):
  Stage 1   — Skeleton Planner + Validation        [1 LLM call + programmatic]
  Stage 1.2 — Node Description Enricher            [1+ LLM calls; topology frozen; production-grade specs]
  Stage 1.5 — Advisory Global Selection            [0–1 LLM call; primary/secondary split]
  Stage 1.7 — Persistence Architect                [0–1 LLM call; enrich workflowRegistry fields]
  Stage 2a  — Node Schema Builder                  [1 or ceil(N/6) SEQUENTIAL LLM calls]
              + ExecutionPathIndex built from skeleton before any LLM call
              + PRIMARY (full) + SECONDARY (name+type) global register; register cached across batches
              + on validation retry: targeted secondary-detail lookup (cheap follow-up)
  Stage 2b  — Node Schema Validation               [per-batch, programmatic, path-aware]
  Stage 2c  — Execution State Schema Derivation    [pure code]
  Stage 3a  — Edge Mapper                          [1 LLM call PER ROUTER, parallel]
  Stage 3b  — Edge Mapping Validation              [per router, programmatic]
  Stage 4   — Graph Assembler                      [pure code]
  Stage 4b  — Final Graph Validation               [programmatic; full equal global catalog]
  Stage 4c  — Persistence Registry Extract        [filter + 0–1 LLM call, merge]

Key design decisions:
  - Stage 2a batches run STRICTLY SEQUENTIAL — no parallelism, no race conditions
  - ExecutionPathIndex (pure code) prevents cross-branch bindings at compile time
  - Stage 3a sends one isolated call per router with ancestor-filtered outputParams;
    routers run in parallel (no shared state between them)
  - Repair is always scoped: a bad batch re-runs that batch only; a bad router
    re-runs that router only — no cross-stage contamination

Entry points:
  plan_skeleton_from_prompt(...)  — Stage 1 only, prompt-mode (NL builder)
  plan_phase_graph_scratch(...)   — full pipeline; pass skeleton_plan= to skip Stage 1
Returns a dict compatible with GraphDefinition (existing model in agent_new.py).
"""

import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from json_repair import repair_json
from pydantic import BaseModel, Field, model_validator

from services.global_bindings import (
    global_field_basename,
    iter_template_binding_refs,
    prior_phase_global_name,
    unique_bare_aliases,
)

try:
    import json
    with open('config.json', 'r') as _f:
        _cfg = json.load(_f)
        _global_vars_config_str = json.dumps(_cfg.get("available_global_variables", []))
except Exception:
    _global_vars_config_str = "[]"

_GLOBAL_VARS_INSTRUCTION = f"""
AVAILABLE SYSTEM VARIABLES:
{_global_vars_config_str}

CRITICAL BINDING RULE FOR SYSTEM VARIABLES:
To pass a system variable to any node, you MUST declare it in 'inputBindings' using:
"boundTo": "system.VAR_NAME" (e.g., "system.user_id").
Do not leave it blank.
"""
logger = logging.getLogger(__name__)
_ARCADEDB_UNAVAILABLE_UNTIL: Optional[float] = None
_ARCADEDB_COOLDOWN_SECONDS: float = 60.0

# ===========================================================================
# SECTION 0b — TARGETED REPAIR MODELS (Stage 4b binding fixes)
# ===========================================================================

class ScratchBindingFix(BaseModel):
    nodeId: str
    paramName: str
    correctedValue: str  # must be {{node.param}} or {{global.key}} or {{pick:...}}


class ScratchRepairResult(BaseModel):
    bindingFixes: List[ScratchBindingFix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _json_compact(obj: Any, *, max_chars: int = 12000) -> str:
    """
    Best-effort JSON log string for large objects.
    Truncates to max_chars to avoid runaway logs.
    """
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + f"... <truncated {len(s) - max_chars} chars>"
    return s


def _log_binding_audit(event: str, **fields: Any) -> None:
    """
    Grep-friendly structured audit log for binding / cross-phase tracking.

    Example: grep '[binding-audit]' backend.log
    """
    parts: List[str] = []
    for key in sorted(fields.keys()):
        val = fields[key]
        if isinstance(val, (list, dict)):
            parts.append(f"{key}={_json_compact(val, max_chars=4000)}")
        else:
            parts.append(f"{key}={val!r}")
    logger.info(f"[binding-audit] {event} | " + " ".join(parts))


async def _safe_checkpoint(
    checkpoint_fn: Optional[Any],
    state: "ScratchPlanningState",
    sub_stage: str,
    **kwargs: Any,
) -> None:
    """
    Invoke optional resume checkpoint without failing the planner pipeline.

    Checkpoints are best-effort (credit-resume). MongoDB outages must not turn a
    successful graph plan into a 503.
    """
    if checkpoint_fn is None:
        return
    try:
        await checkpoint_fn(state, sub_stage, **kwargs)
    except Exception as exc:
        logger.warning(
            "[scratch_planner] Checkpoint %s failed (non-fatal, planning continues): %s",
            sub_stage,
            exc,
        )


from services.gemini_service import (
    async_generate,
    GeminiGenerateConfig,
    get_or_create_cache,
    model_supports_thinking_level,
)
from services.workflow_budget_service import BillingError
from repositories.arcadedb_repository import EnterpriseGraphRepository
from services.collection_schema import (
    extract_collection_names_from_text,
    resolve_schemas_for_planner,
)
from services.provider_operation import (
    extract_provider_operations_from_text,
    operations_to_dicts,
)
from services.persistence_link import attach_persistence_links_to_graph


# ===========================================================================
# SECTION 1 — DATA MODELS
# ===========================================================================

# ── Stage 1 ─────────────────────────────────────────────────────────────────

class SkeletonNode(BaseModel):
    nodeId: str
    type: Literal["tool", "ui_screen", "router", "end"]
    name: str
    description: str
    stepId: Optional[str] = None


class SkeletonEdge(BaseModel):
    fromNode: str
    to: str
    kind: Literal["sequential", "conditional", "fallback"]


class SkeletonPlan(BaseModel):
    entryNodeId: str
    endNodeId: str
    nodes: List[SkeletonNode]
    edges: List[SkeletonEdge]


class EnrichedNodeDescription(BaseModel):
    """Stage 1.2: description rewrite for one existing skeleton node."""
    nodeId: str
    description: str


class NodeDescriptionEnrichmentResult(BaseModel):
    """Stage 1.2 LLM output — descriptions only; topology is applied by merge."""
    nodes: List[EnrichedNodeDescription] = Field(default_factory=list)


# ── Stage 2a/2b ─────────────────────────────────────────────────────────────

class GlobalVar(BaseModel):
    name: str
    datatype: str
    source: str
    description: str = ""
    itemType: Optional[str] = None  # "scalar" | "record" | "enum" when datatype == "list"
    scalarItemDatatype: Optional[str] = None
    itemSchema: Optional[List[Any]] = None
    enumValues: Optional[List[str]] = None


def _namespace_global_var(g: GlobalVar) -> GlobalVar:
    """Issue 8: prior-phase GlobalVar.name is sourceNode.field (system globals stay bare)."""
    new_name = prior_phase_global_name(g.source, g.name)
    if new_name == g.name:
        return g
    return g.model_copy(update={"name": new_name})


def _namespace_global_vars(globals_list: List[GlobalVar]) -> List[GlobalVar]:
    out: List[GlobalVar] = []
    seen: set = set()
    for g in globals_list or []:
        ng = _namespace_global_var(g)
        if not ng.name or ng.name in seen:
            continue
        seen.add(ng.name)
        out.append(ng)
    return out


def _global_ref_key_map(globals_list: List[GlobalVar]) -> Dict[str, GlobalVar]:
    """``global.<canonicalName>`` plus unique bare ``global.<field>`` aliases."""
    by_key: Dict[str, GlobalVar] = {}
    names = [g.name for g in (globals_list or []) if g.name]
    aliases = unique_bare_aliases(names)
    for g in globals_list or []:
        if not g.name:
            continue
        by_key[f"global.{g.name}"] = g
        base = global_field_basename(g.name)
        if aliases.get(base) == g.name:
            by_key.setdefault(f"global.{base}", g)
    return by_key


def _canonical_selected_global_names(selected: List[str], known_names: set) -> List[str]:
    """Map LLM-selected names (bare, namespaced, or global.* prefixed) onto canonical names."""
    aliases = unique_bare_aliases(known_names)
    out: List[str] = []
    seen: set = set()
    for n in selected or []:
        raw = (n or "").strip()
        if raw.startswith("global."):
            raw = raw[len("global.") :]
        canon = raw if raw in known_names else aliases.get(raw)
        if canon and canon not in seen:
            out.append(canon)
            seen.add(canon)
    return out


class NodeOutputEntry(BaseModel):
    nodeId: str
    batch: int
    outputs: List[Dict[str, Any]]  # [{"name": ..., "datatype": ..., "itemSchema": ...}]


class AvailableVarsRegister(BaseModel):
    # Primary globals (advisory selection from Stage 1.5) — shown in full to binders.
    globals: List[GlobalVar] = Field(default_factory=list)
    # Secondary globals (full list minus primary) — compact fallback only.
    secondaryGlobals: List[GlobalVar] = Field(default_factory=list)
    nodeOutputs: List[NodeOutputEntry] = Field(default_factory=list)


class GlobalSelectionResult(BaseModel):
    """Stage 1.5: advisory subset of prior-phase globals likely used by this phase."""
    selectedGlobalNames: List[str] = Field(default_factory=list)
    rationale: str = ""


# ── Stage 1.7 — Persistence Architect ───────────────────────────────────────

class PersistenceField(BaseModel):
    """Production-grade collection field proposed by Stage 1.7 (not node params)."""
    name: str
    datatype: Literal["string", "boolean", "number"] = "string"
    category: str = ""  # free-text observability only (lifecycle, audit, …)
    reason: str = ""


class EntitySchema(BaseModel):
    collection: str
    fields: List[PersistenceField] = Field(default_factory=list)


class PersistencePlan(BaseModel):
    entities: List[EntitySchema] = Field(default_factory=list)


class SecondaryDetailLookupResult(BaseModel):
    """Targeted follow-up: which compact secondary globals need full descriptions injected."""
    neededGlobalNames: List[str] = Field(default_factory=list)
    rationale: str = ""


class NodeBatch(BaseModel):
    batchIndex: int
    nodeIds: List[str]
    # Batches are always run sequentially — no dependency graph needed.


class InputBinding(BaseModel):
    boundTo: str  # "nodeId.paramName"


class NodeParam(BaseModel):
    name: str
    datatype: str  # "string" | "boolean" | "number" | "list"
    required: bool
    description: str
    # Only inputParams should carry bindings. Kept optional so older/errant LLM
    # responses that include outputParam.inputBindings still parse and can be
    # rejected/normalised by validation.
    inputBindings: List[InputBinding] = Field(default_factory=list)
    # List item kind. Required in practice when datatype == "list" (Check 6).
    # "scalar" → array of string/boolean/number; "record" → array of objects;
    # "enum" → array of values from enumValues.
    itemType: Optional[Literal["scalar", "record", "enum"]] = None
    scalarItemDatatype: Optional[Literal["string", "boolean", "number"]] = None
    # Record items only: one entry per field of a single record.
    itemSchema: Optional[List["NodeParam"]] = None
    enumValues: Optional[List[str]] = None

    @model_validator(mode="after")
    def _check_list_item_schema(self) -> "NodeParam":
        if self.datatype != "list":
            if self.itemSchema or self.enumValues or self.scalarItemDatatype or self.itemType:
                raise ValueError(
                    f"NodeParam '{self.name}': itemType / itemSchema / scalarItemDatatype / "
                    f"enumValues must only be set when datatype='list'."
                )
            return self

        item_type = self.itemType
        if item_type is None:
            if self.itemSchema:
                item_type = "record"
            elif self.enumValues:
                item_type = "enum"
            elif self.scalarItemDatatype:
                item_type = "scalar"
            if item_type is not None:
                self.itemType = item_type

        if item_type == "scalar":
            if self.itemSchema:
                raise ValueError(
                    f"NodeParam '{self.name}': itemType='scalar' must not set itemSchema. "
                    f"Use scalarItemDatatype only (e.g. string for ['Python','Java'])."
                )
        elif item_type == "enum":
            if self.itemSchema:
                raise ValueError(
                    f"NodeParam '{self.name}': itemType='enum' must not set itemSchema."
                )
        elif item_type is not None and item_type != "record":
            raise ValueError(
                f"NodeParam '{self.name}': itemType must be scalar, record, or enum, "
                f"got {item_type!r}."
            )
        return self


NodeParam.model_rebuild()  # resolve forward ref List["NodeParam"] in itemSchema


_SCALAR_ITEM_DATATYPES = frozenset({"string", "boolean", "number"})


def _obj_get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read an attribute or dict key from NodeParam / dump dict / GenerationIOParam."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _effective_item_type(obj: Any) -> Optional[str]:
    """Resolve list item kind, inferring from legacy itemSchema-only dumps."""
    if obj is None:
        return None
    t = _obj_get(obj, "itemType")
    if t:
        return str(t)
    if _obj_get(obj, "itemSchema"):
        return "record"
    if _obj_get(obj, "enumValues"):
        return "enum"
    if _obj_get(obj, "scalarItemDatatype"):
        return "scalar"
    return None


def _dump_list_item_annotation(p: Any) -> Dict[str, Any]:
    """Serialize list-item metadata for gen-spec / register dumps."""
    if _obj_get(p, "datatype", "dataType") != "list":
        return {}
    item_schema = _obj_get(p, "itemSchema")
    if item_schema:
        dumped_schema = [
            f.model_dump() if hasattr(f, "model_dump") else f
            for f in item_schema
        ]
    else:
        dumped_schema = None
    return {
        "itemType": _obj_get(p, "itemType"),
        "scalarItemDatatype": _obj_get(p, "scalarItemDatatype"),
        "enumValues": _obj_get(p, "enumValues"),
        "itemSchema": dumped_schema,
    }


def _dump_node_param_io(p: "NodeParam", *, data_type_key: str = "dataType") -> Dict[str, Any]:
    """Serialize a NodeParam for generation.inputs/outputs / declaredOutputs."""
    row: Dict[str, Any] = {
        "name": p.name,
        data_type_key: p.datatype,
        "required": p.required,
        "description": p.description,
    }
    row.update(_dump_list_item_annotation(p))
    return row


def _ref_meta_from_obj(obj: Any, datatype: Optional[str] = None) -> "RefMeta":
    dt = datatype or _obj_get(obj, "datatype", "dataType", default="string")
    return RefMeta(
        datatype=str(dt),
        itemSchema=_obj_get(obj, "itemSchema"),
        itemType=_obj_get(obj, "itemType"),
        scalarItemDatatype=_obj_get(obj, "scalarItemDatatype"),
        enumValues=_obj_get(obj, "enumValues"),
    )


class NodeSchema(BaseModel):
    nodeId: str
    inputParams: List[NodeParam] = Field(default_factory=list)
    outputParams: List[NodeParam] = Field(default_factory=list)


class NodeSchemaList(BaseModel):
    nodes: List[NodeSchema]


# ── Shared binding-ref types ──────────────────────────────────────────────────

@dataclass
class BindingRef:
    """Parsed components of a boundTo string (non-pick, non-global)."""
    node_id: str
    param_name: str
    index: Optional[int] = None        # set for "nodeId.param[0].field" style
    item_field: Optional[str] = None   # set together with index


@dataclass
class RefMeta:
    """Metadata about an available binding reference (node output or global)."""
    datatype: str
    itemSchema: Optional[List[Any]] = None
    itemType: Optional[str] = None
    scalarItemDatatype: Optional[str] = None
    enumValues: Optional[List[str]] = None


# ── Stage 2c ─────────────────────────────────────────────────────────────────

class StateField(BaseModel):
    fieldName: str        # "nodeId__paramName"  (double underscore)
    datatype: str
    producedBy: str       # nodeId
    sourceParam: str      # param name on the producing node
    itemSchema: Optional[List[NodeParam]] = None
    itemType: Optional[str] = None
    scalarItemDatatype: Optional[str] = None
    enumValues: Optional[List[str]] = None


class StateSchema(BaseModel):
    fields: List[StateField] = Field(default_factory=list)


# ── Stage 3a ─────────────────────────────────────────────────────────────────

class MappedEdge(BaseModel):
    fromNode: str
    to: str
    kind: Literal["sequential", "conditional", "fallback"]
    condition: Optional[str] = None


class RouterEdgeCondition(BaseModel):
    """Result of a single per-router Stage 3a LLM call."""
    to: str
    kind: Literal["conditional", "fallback"]
    condition: str


class RouterEdgeResult(BaseModel):
    """Per-router response from Stage 3a."""
    routerNodeId: str
    edges: List[RouterEdgeCondition]


class EdgeMap(BaseModel):
    edges: List[MappedEdge]


# ── Intermediate pipeline state ──────────────────────────────────────────────

class LogicalStepLite(BaseModel):
    stepId: str
    name: str
    description: str
    # When set, the skeleton planner MUST produce exactly ONE node for this step
    # and that node MUST have this type. None = LLM decides type and may split.
    preferredKind: Optional[Literal["ui_screen", "tool"]] = None


class ScratchPlanningState(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    # Inputs
    phaseId: str
    phaseName: str
    phaseDescription: str
    steps: List[LogicalStepLite]
    priorPhaseOutputs: List[GlobalVar] = Field(default_factory=list)
    # Accumulated cross-phase DB persistence (collections, fields, filter keys).
    workflowRegistry: Dict[str, Any] = Field(default_factory=dict)
    # Short block from POST /stage0/respond → scratch/preview handoff
    stage0Context: Optional[str] = None
    # Probed / Process_Configs / registry schemas for generationDescription quality (Task 6)
    knownCollectionSchemasText: Optional[str] = None
    knownCollectionSchemas: Dict[str, Any] = Field(default_factory=dict)
    projectId: Optional[str] = None
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None

    # Stage 1
    skeletonPlan: Optional[SkeletonPlan] = None

    # Stage 1.2 — production-grade node descriptions (resume / skip)
    nodeDescriptionsEnriched: bool = False

    # Stage 2a — batch plan (pure code, strictly ordered)
    nodeBatchPlan: List[NodeBatch] = Field(default_factory=list)

    # Stage 2a — Execution Path Index (pure code, built once from skeleton)
    # nodeId → set of eligible ancestor nodeIds on every valid execution path
    # Used by Stage 2b to reject cross-branch bindings at compile time.
    executionPathIndex: Dict[str, Any] = Field(default_factory=dict)

    # Stage 1.5 — advisory primary global names chosen from priorPhaseOutputs
    selectedPrimaryGlobalNames: List[str] = Field(default_factory=list)

    # Stage 1.7 — Persistence Architect ran for this phase (resume / skip)
    persistenceArchitectCompleted: bool = False
    # Collections Stage 1.7 already designed (cache — skip re-invoke unless new touch)
    persistenceArchitectedCollections: List[str] = Field(default_factory=list)
    # Last Stage 1.7 proposals (category+reason) for checkpoint / human review
    persistenceArchitectProposals: List[Dict[str, Any]] = Field(default_factory=list)

    # Stage 2a — available variables register (grows sequentially as each batch validates)
    availableVarsRegister: AvailableVarsRegister = Field(
        default_factory=AvailableVarsRegister
    )

    # Stage 2a — assembled from all validated batches
    nodeSchemas: List[NodeSchema] = Field(default_factory=list)

    # Stage 2c
    stateSchema: Optional[StateSchema] = None

    # Stage 3a
    edgeMap: Optional[EdgeMap] = None

    # Stage 4
    graphDefinition: Optional[Dict[str, Any]] = None

    # Stage 4b
    validationWarnings: List[str] = Field(default_factory=list)

    # Stage 6
    langgraphCode: str = ""


# ===========================================================================
# SECTION 2 — LLM UTILITIES
# ===========================================================================

_MAX_RETRIES = 3
_SKELETON_PLANNER_MODEL = "gemini-3.1-pro-preview"
_DESC_ENRICHER_MODEL = "gemini-3.1-pro-preview"
_DESC_ENRICHER_BATCH_SIZE = 4
_NODE_SCHEMA_BUILDER_MODEL = "gemini-2.5-pro"
_EDGE_MAPPER_MODEL = "gemini-3.5-flash-lite"
_GLOBAL_SELECTOR_MODEL = "gemini-3.5-flash-lite"
_SECONDARY_LOOKUP_MODEL = "gemini-3.5-flash-lite"
_PERSISTENCE_ARCHITECT_MODEL = "gemini-3.5-flash-lite"
_SCRATCH_LLM_OPERATION = "scratch-graph-planning"
_SCRATCH_CACHE_TTL_SECONDS = 3600


async def _get_scratch_cached_content(
    api_key: Optional[str],
    model: str,
    *,
    system_text: str,
    cache_key_prefix: str,
    display_name: str,
) -> Optional[str]:
    """
    Shared explicit context cache for static scratch-planner system prompts.

    Cost optimization only — returns None on failure so callers keep working
    via the existing uncached prompt path.
    """
    try:
        cache_key = (
            cache_key_prefix
            + "_"
            + hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
        )
        return await get_or_create_cache(
            model=model,
            system_text=system_text,
            ttl_seconds=_SCRATCH_CACHE_TTL_SECONDS,
            cache_key=cache_key,
            api_key=api_key or None,
            display_name=display_name,
        )
    except Exception as exc:
        logger.warning(
            "Scratch cache unavailable (%s), continuing uncached: %s",
            display_name,
            exc,
        )
        return None


def _usage_metadata_to_dict(usage: Any) -> Dict[str, int]:
    """Convert Gemini usage_metadata into a plain dict for token tracking."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "prompt_token_count": int(usage.get("prompt_token_count", 0) or 0),
            "candidates_token_count": int(usage.get("candidates_token_count", 0) or 0),
            "thoughts_token_count": int(usage.get("thoughts_token_count", 0) or 0),
            "total_token_count": int(usage.get("total_token_count", 0) or 0),
            "cached_content_token_count": int(
                usage.get("cached_content_token_count", 0) or 0
            ),
        }
    return {
        "prompt_token_count": int(getattr(usage, "prompt_token_count", 0) or 0),
        "candidates_token_count": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thoughts_token_count": int(getattr(usage, "thoughts_token_count", 0) or 0),
        "total_token_count": int(getattr(usage, "total_token_count", 0) or 0),
        "cached_content_token_count": int(
            getattr(usage, "cached_content_token_count", 0) or 0
        ),
    }


def _record_scratch_llm_call(
    *,
    project_id: Optional[str],
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    sub_operation: str,
    model: str,
    prompt: str,
    response_text: str = "",
    usage_metadata: Optional[Dict[str, int]] = None,
    status: str,
    attempt: int,
    started_at: datetime,
    finished_at: datetime,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort token tracking for every scratch planner LLM call."""
    usage_metadata = usage_metadata or {}
    prompt_tokens = int(usage_metadata.get("prompt_token_count", 0) or (len(prompt) // 4))
    completion_tokens = int(usage_metadata.get("candidates_token_count", 0) or (len(response_text) // 4))
    thoughts_tokens = int(usage_metadata.get("thoughts_token_count", 0) or 0)
    total_tokens = int(
        usage_metadata.get("total_token_count", 0)
        or (prompt_tokens + completion_tokens + thoughts_tokens)
    )

    tracking_metadata: Dict[str, Any] = {
        "planner": "scratch",
        "prompt_chars": len(prompt or ""),
        "response_chars": len(response_text or ""),
        **(metadata or {}),
    }
    if usage_metadata:
        tracking_metadata["usage_metadata"] = usage_metadata

    try:
        from services.llm_token_tracker import get_tracker, record_llm_call

        get_tracker()
        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation=_SCRATCH_LLM_OPERATION,
            sub_operation=sub_operation,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            thoughts_tokens=thoughts_tokens,
            status=status,
            attempt=attempt,
            is_retry=attempt > 1,
            metadata=tracking_metadata,
            started_at=started_at,
            finished_at=finished_at,
        )
    except Exception as exc:
        logger.debug(f"[scratch/llm-tracking] Failed to record LLM call: {exc}")


async def _llm_call(
    api_key: Optional[str],
    prompt: str,
    *,
    model: str,
    sub_operation: str,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    phase_id: Optional[str] = None,
    attempt: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    temperature: float = 0.3,
    as_json: bool = False,
    response_schema: Any = None,
    system_text: Optional[str] = None,
    cached_content: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Single Gemini call via gemini_service. Returns (raw_text, parsed_or_None).
    For JSON calls, parsed_or_None is the parsed Pydantic object when response_schema is set.
    Thinking is enabled automatically for models that support it (3.x pro/preview).

    When *cached_content* is set, *system_text* is stored server-side and must not be
    prepended to the per-request prompt (avoids double billing). *prompt* is the dynamic
    user portion only. When cache is unavailable, *system_text* is prepended to *prompt*.
    """
    if cached_content and system_text:
        api_prompt = prompt
        logical_prompt = f"{system_text}\n\n{prompt}"
    elif system_text:
        api_prompt = f"{system_text}\n\n{prompt}"
        logical_prompt = api_prompt
    else:
        api_prompt = prompt
        logical_prompt = prompt

    cfg = GeminiGenerateConfig(
        temperature=temperature,
        # Enable thinking for models that support it (gate is inside the service)
        thinking_level="high" if model_supports_thinking_level(model) else None,
        response_mime_type="application/json" if as_json and response_schema is not None else None,
        response_schema=response_schema if as_json and response_schema is not None else None,
        fallback_profile="planner",
        cached_content=cached_content,
    )

    started_at = datetime.utcnow()
    wall_start = time.perf_counter()
    prompt_chars = len(logical_prompt or "")
    logger.info(
        f"[scratch/llm] → {sub_operation} model={model} attempt={attempt} "
        f"phase={phase_id or '-'} prompt_chars={prompt_chars} "
        f"cached={'yes' if cached_content else 'no'}"
    )
    response = None
    try:
        response = await async_generate(
            model,
            api_prompt,
            config=cfg,
            api_key=api_key,
        )

        raw_text = ""
        try:
            raw_text = response.text or ""
        except Exception:
            pass

        finished_at = datetime.utcnow()
        usage_metadata = _usage_metadata_to_dict(getattr(response, "usage_metadata", None))
        call_metadata: Dict[str, Any] = {
            "phase_id": phase_id,
            "response_schema": getattr(response_schema, "__name__", str(response_schema)) if response_schema else None,
            "cached_content_active": bool(cached_content),
            **(metadata or {}),
        }
        cached_tokens = int(usage_metadata.get("cached_content_token_count", 0) or 0)
        if cached_tokens:
            call_metadata["cached_content_token_count"] = cached_tokens
        _record_scratch_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            sub_operation=sub_operation,
            model=model,
            prompt=logical_prompt,
            response_text=raw_text,
            usage_metadata=usage_metadata,
            status="success",
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            metadata=call_metadata,
        )

        parsed = None
        if as_json and response_schema is not None:
            parsed = response.parsed

        duration_ms = int((time.perf_counter() - wall_start) * 1000)
        logger.info(
            f"[scratch/llm] ✓ {sub_operation} model={model} attempt={attempt} "
            f"duration_ms={duration_ms} response_chars={len(raw_text)} "
            f"tokens={usage_metadata.get('total_token_count', 0)} "
            f"(prompt={usage_metadata.get('prompt_token_count', 0)}, "
            f"completion={usage_metadata.get('candidates_token_count', 0)}, "
            f"cached={cached_tokens})"
        )
        return raw_text, parsed
    except BillingError:
        raise
    except Exception as exc:
        duration_ms = int((time.perf_counter() - wall_start) * 1000)
        logger.warning(
            f"[scratch/llm] ✗ {sub_operation} model={model} attempt={attempt} "
            f"duration_ms={duration_ms} error={exc}"
        )
        finished_at = datetime.utcnow()
        usage_metadata = _usage_metadata_to_dict(
            getattr(response, "usage_metadata", None) if response is not None else None
        )
        _record_scratch_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            sub_operation=sub_operation,
            model=model,
            prompt=logical_prompt,
            response_text="",
            usage_metadata=usage_metadata,
            status="failed",
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            metadata={
                "phase_id": phase_id,
                "error": str(exc),
                "cached_content_active": bool(cached_content),
                "response_schema": getattr(response_schema, "__name__", str(response_schema)) if response_schema else None,
                **(metadata or {}),
            },
        )
        raise


async def _scratch_llm_call(
    api_key: Optional[str],
    *,
    model: str,
    sub_operation: str,
    system_text: str,
    user_prompt: str,
    full_prompt: str,
    cached_content: Optional[str],
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    phase_id: Optional[str] = None,
    attempt: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    temperature: float = 0.3,
    as_json: bool = False,
    response_schema: Any = None,
) -> Tuple[str, Any]:
    """
    Scratch planner LLM call with optional context cache.

    When *cached_content* is set, sends *user_prompt* with cached *system_text*.
    Otherwise sends *full_prompt* (original section order) with no cache split.
    """
    if cached_content:
        return await _llm_call(
            api_key,
            user_prompt,
            model=model,
            sub_operation=sub_operation,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            phase_id=phase_id,
            attempt=attempt,
            metadata=metadata,
            temperature=temperature,
            as_json=as_json,
            response_schema=response_schema,
            system_text=system_text,
            cached_content=cached_content,
        )
    return await _llm_call(
        api_key,
        full_prompt,
        model=model,
        sub_operation=sub_operation,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        phase_id=phase_id,
        attempt=attempt,
        metadata=metadata,
        temperature=temperature,
        as_json=as_json,
        response_schema=response_schema,
    )


def _strip_json_fences(text: str) -> str:
    """Remove ```json ... ``` fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_json_robust(text: str) -> Any:
    """Parse JSON, repairing if needed."""
    text = _strip_json_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(repair_json(text))


# ===========================================================================
# SECTION 3 — STAGE 1: SKELETON PLANNER + VALIDATION
# ===========================================================================

_SKELETON_SYSTEM = """You are a graph topology designer. You are planning a LangGraph workflow graph. Your ONLY job is to convert the logical steps below into a set of nodes and directed edges.

═══════════════════════════════════════════════════════
STRUCTURAL RULES
═══════════════════════════════════════════════════════
1. One node per distinct action or decision point.
2. A router node (type="router") is required wherever the flow branches. Name it "<prior_node>_router".
3. An end node (type="end") with nodeId="end" is always required.
4. Do NOT assign capabilities, input parameters, output parameters, or bindings — ONLY names and descriptions.
5. nodeId values must be snake_case.
6. Edges must cover every path from entry to end (including error/retry branches implied by step descriptions).
7. If prior phase outputs are listed, do NOT create a node whose sole purpose is to collect data already listed there.

GENERATE node kinds:
  - "tool"      → pure server-side logic; no user interaction whatsoever
  - "ui_screen" → any node where a user sees or interacts with a screen (forms, uploads, confirmations, e-signatures)
    A ui_screen is FULL-STACK: its description must cover both the frontend UI AND the backend handler.
    Do NOT create a separate tool node for a screen's backend — it is part of the same screen node.

Node types (exactly one of): "tool" | "ui_screen" | "router" | "end"

═══════════════════════════════════════════════════════
DATA / PARAMETER RULES — CRITICAL
═══════════════════════════════════════════════════════
A. Bare "object" / "array" datatypes and JSON-encoded blob strings are FORBIDDEN.
   Scalars remain flat: every individual scalar piece of data is its own named field (string/boolean/number).
   Collections (variable-length or large fixed-count) use datatype "list". Choose the item kind:
   - list of primitives (["Python","Java"]) → itemType scalar + scalarItemDatatype string/boolean/number.
     NEVER wrap primitives as one-field records like [{skill: string}].
   - list of objects ([{name, score}]) → itemType record + itemSchema (one entry per field of a record).
   - list of closed-set values → itemType enum + enumValues.
   Do NOT enumerate hundreds of indexed flat params for dynamic collections.
   WRONG:  aadhaar_document: object
   CORRECT: aadhaar_number: string, aadhaar_name: string, aadhaar_dob: string, aadhaar_address: string

A1. Use the cardinality threshold to decide between flat vs list:
    - Fixed count ≤ 5 at design time → FLAT indexed scalar params (name_1_field, name_2_field, …)
    - Fixed count > 5 OR dynamic/unknown count → datatype "list" with the correct itemType
    NEVER describe an output as a "stringified JSON array/object" — this is nesting in disguise.
    For flat (count ≤ 5): the node description MUST spell out one flat field PER attribute PER repetition.
    WRONG:  "...output 'mcq_list_json' (a stringified JSON array of question objects)."
    WRONG:  "...output 'required_skills' as [{skill: string}] when the schema is an array of strings."
    CORRECT (scalar array): "...output 'required_skills' (list of strings, itemType=scalar)."
    CORRECT (dynamic records): "...output 'mcq_questions' (list, itemType=record) with itemSchema:
              question_text, option_a, option_b, option_c, option_d, correct_option — one record
              per generated question. Describe value space when a field is later compared
              (e.g. correct_option is the letter a/b/c/d, not the option display text)."
    CORRECT (fixed N≤5): "...output N separate fields: mcq_1_question_text, mcq_1_option_a, ...
              mcq_N_correct_option — one flat string field for every attribute of every question."

  When a collection is expected to exceed ~50–100 records or needs pagination in the UI, do not
  pass the full list between nodes. Instead: the producing node writes records to the named
  collection and outputs batch_id (string) and count (number); downstream nodes query by batch_id.

B. Every entity, field, attribute, or piece of data mentioned in a step description MUST appear
   as an explicit parameter in that node. Do not group things under a generic object.

C. For document uploads (images, PDFs, scanned files):
   Use a field like "aadhaar_image_base64: string" or "aadhaar_file_url: string".
   The image/file itself is a single string (base64 or URL). Extracted text fields are separate params.

D. BACKEND CRUD contract (when persistence is involved):
   Name the exact collection/table, the operation (insert/find/update/upsert/etc.),
   the filter keys, and which prior node output supplies each value.
   Keep collection names CONSISTENT across all steps that share data.

═══════════════════════════════════════════════════════
DESCRIPTION QUALITY — CRITICAL
═══════════════════════════════════════════════════════
The description field is passed VERBATIM to the code generator.
It must be a complete, standalone specification for THIS node only.
Low-quality descriptions that use vague labels WILL produce broken code.

For ui_screen nodes — the description MUST include:
  • What the screen looks like (form fields, buttons, labels, file-upload controls)
  • Every exact field the user fills in, with its label as shown on screen
  • What happens when the user submits/clicks (backend call, what data is saved/returned)
  • Which prior data is pre-filled on screen (e.g. "pre-fill user_name from collect_documents")
  • If saving to / reading from DB: exact collection name AND field:type schema —
    e.g. Collection 'mna_leads' schema: {company_name:string, is_deleted:string}.
    Copy from KNOWN COLLECTION SCHEMAS when present. Collection name alone is INVALID.
  • Prefer the exact Mongo field name for values this screen saves/returns.
    If a returned/output name differs from the Mongo field, include:
    MongoDB field mapping for collection '<coll>': <output_name> → <mongo_field>.
    Each mongo_field once. Never map one name onto two similar fields
    (status vs current_status vs pipeline_status).

  BAD:  "Established context: Collect user name, email, Aadhaar and PAN from the customer."
  GOOD: "Create a UI screen with four fields: full name (text input, label='Full Name'),
         email (email input, label='Email'), Aadhaar upload (file input, accept image/*,
         label='Aadhaar Card'), PAN upload (file input, accept image/*, label='PAN Card').
         Submit button labelled 'Continue'. On submit, store the uploaded files as base64
         strings (aadhaar_image_base64, pan_image_base64) and return all four fields."

For tool nodes — the description MUST include:
  • Every input this tool receives (by name, where it comes from)
  • The exact computation, API call, OCR logic, DB query, or transformation performed
  • What specific values are produced as outputs (names and meaning)
  • If verification/comparison: describe the exact logic (e.g. compare extracted_name to user_name)
  • If using a library: name it (e.g. Tesseract for OCR etc.)
  • If reading/writing MongoDB: exact collection name AND full field:type schema
    (e.g. Collection 'mna_leads' schema: {company_name:string, company_profile:string,
    current_tab:string, is_deleted:string, lead_id:string}). Naming the collection alone is NOT enough.
    Copy schemas from KNOWN COLLECTION SCHEMAS / Stage 0 / WORKFLOW PERSISTENCE REGISTRY when present.
    Prefer exact Mongo field names. If a tool input/output name differs, the description
    MUST include: MongoDB field mapping for collection '<coll>': <param> → <mongo_field>.
    One mongo_field per mapping. Do not guess among similar keys.

  BAD:  "Standard processing applied for document verification and Aadhaar extraction."
  GOOD: "Tool receives aadhaar_image_base64 (base64 image string) and user_name (string).
         Run OCR using Tesseract; if confidence < 0.85, fall back to Gemini API.
         Extract: extracted_name, extracted_dob, extracted_address, extracted_aadhaar_number.
         Compare extracted_name to user_name (case-insensitive, strip whitespace).
         Output: verification_status ('success' or 'failure'), extracted_name, extracted_dob,
         extracted_aadhaar_number, mismatch_reason (string, empty if matched)."

  BAD (DB):  "Inserts discovered leads into the 'mna_leads' collection."
  GOOD (DB): "Inserts into Collection 'mna_leads' schema: {company_name:string,
         company_profile:string, funding:string, leadership:string, products:string,
         news:string, technology:string, competitors:string, current_tab:string,
         is_deleted:string}. Set current_tab='Live Leads', is_deleted='false'."
  GOOD (DB rename): "Output param ticket_status ('OPEN'). Write Mongo field status.
         MongoDB field mapping for collection 'ticket_info': ticket_status → status."

For router nodes — the description MUST include:
  • Which exact output param is evaluated
  • The exact string/boolean values that trigger each branch
  • Which branch leads where and why

  BAD:  "Route based on verification result."
  GOOD: "Reads verification_status from verify_identity node.
         If verification_status == 'success' → proceed to e_signature.
         If verification_status == 'failure' → return to collect_documents for re-upload."

Output valid JSON matching the SkeletonPlan schema exactly — no markdown, no explanation."""

_SKELETON_JSON_SCHEMA = """Required JSON schema:
{
  "entryNodeId": "snake_case_id",
  "endNodeId": "end",
  "nodes": [
    {
      "nodeId": "snake_case_id",
      "type": "tool | ui_screen | router | end",
      "name": "Human Readable Name",
      "description": "Very detailed description of what this node does.",
      "stepId": "L1"
    }
  ],
  "edges": [
    {
      "fromNode": "node_a",
      "to": "node_b",
      "kind": "sequential | conditional"
    }
  ]
}"""

_SKELETON_CACHED_SYSTEM = f"{_SKELETON_SYSTEM}\n\n{_SKELETON_JSON_SCHEMA}"


def _stage0_constraints_only(ctx: str) -> str:
    """
    Remove the global workflow step list from Stage 0 handoff.

    Per-phase LOGICAL STEPS already define what to implement; repeating all steps
    from Stage 0 as "authoritative" causes the skeleton planner to plan later
    phases inside the current phase.
    """
    if not ctx or not ctx.strip():
        return ""
    text = ctx.strip()
    marker = "Workflow steps:"
    idx = text.find(marker)
    if idx != -1:
        text = text[:idx].rstrip()
    return text.strip()


def _build_skeleton_user_prompt(state: ScratchPlanningState) -> str:
    """Dynamic per-request portion of the Stage 1 skeleton planner prompt."""
    locked_steps = [s for s in state.steps if s.preferredKind is not None]

    steps_lines: List[str] = []
    for s in state.steps:
        if s.preferredKind:
            steps_lines.append(
                f"  {s.stepId} | {s.name}  [LOCKED: must be exactly ONE {s.preferredKind} node]\n"
                f"     {s.description}"
            )
        else:
            steps_lines.append(
                f"  {s.stepId} | {s.name}  [FREE: LLM decides type and may split]\n"
                f"     {s.description}"
            )
    steps_text = "\n".join(steps_lines)

    prior_section = ""
    if state.priorPhaseOutputs:
        prior_lines: List[str] = []
        for g in state.priorPhaseOutputs:
            desc = (g.description or "").strip()
            desc_part = f" — {desc}" if desc else ""
            prior_lines.append(
                f"  - {g.name} : {_format_ref_type_of(g)}  (from {g.source}){desc_part}"
            )
        prior_section = (
            f"\nDATA AVAILABLE FROM PRIOR PHASES (do NOT create nodes to collect these):\n"
            f"  Bind as global.<name> copying the register name EXACTLY\n"
            f"  (usually global.<sourceNodeId>.<field> when the output came from a prior-phase node).\n"
            f"{chr(10).join(prior_lines)}\n"
        )

    registry_section = _format_registry_for_prompt(state.workflowRegistry)

    schemas_section = ""
    if state.knownCollectionSchemasText and state.knownCollectionSchemasText.strip():
        schemas_section = f"\n{state.knownCollectionSchemasText.strip()}\n"

    stage0_section = ""
    if state.stage0Context and state.stage0Context.strip():
        constraints_only = _stage0_constraints_only(state.stage0Context.strip())
        if constraints_only:
            stage0_section = (
                f"\n{constraints_only}\n"
                "(Stage 0 context above: DB schemas, LLM, APIs, and secrets only. "
                "Do NOT add nodes from it — implement ONLY the LOGICAL STEPS below.)\n"
            )

    locked_section = ""
    if locked_steps:
        locked_lines = "\n".join(
            f"  • stepId='{s.stepId}' ({s.name})\n"
            f"    → Produce EXACTLY ONE node with: type='{s.preferredKind}', stepId='{s.stepId}'\n"
            f"    → Do NOT split, do NOT create helper/router nodes for this step alone.\n"
            f"    → The JSON for this node MUST include: \"stepId\": \"{s.stepId}\""
            for s in locked_steps
        )
        locked_section = f"""
═══════════════════════════════════════════════════════
LOCKED STEP CONSTRAINTS — NON-NEGOTIABLE
═══════════════════════════════════════════════════════
The following steps have a mandatory node type fixed by the user.
For EACH locked step you must produce exactly ONE node — no more, no less.
The node MUST have the exact type and stepId shown below.
Splitting a locked step into multiple nodes is a hard validation error.
Adding routers or helper nodes that belong only to a locked step is forbidden.

{locked_lines}

FREE steps (no preferredKind): you may split into multiple nodes, add routers,
add error-handler nodes, or choose any type. Free-step nodes may share the
same stepId as their source step (or null) — this does not affect locked steps.
"""

    return f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}
{registry_section}{schemas_section}{prior_section}{stage0_section}{locked_section}
SCOPE RULE — THIS PHASE ONLY:
Implement ONLY the LOGICAL STEPS listed below for phase "{state.phaseName}".
Do not implement steps belonging to other phases, even if mentioned elsewhere in this prompt.

LOGICAL STEPS:
{steps_text}""" + _GLOBAL_VARS_INSTRUCTION


def _build_skeleton_prompt(state: ScratchPlanningState) -> str:
    """Full Stage 1 prompt in original section order (uncached fallback).

    Order: SYSTEM rules → dynamic steps/context → JSON schema at the end.
    This preserves the exact original prompt layout.
    """
    return (
        f"{_SKELETON_SYSTEM}\n\n"
        f"{_build_skeleton_user_prompt(state)}\n\n"
        f"{_SKELETON_JSON_SCHEMA}"
    )


def _validate_skeleton(
    plan: SkeletonPlan,
    locked_steps: Optional[List[LogicalStepLite]] = None,
) -> List[str]:
    """
    Programmatic validation of the skeleton plan.
    Returns a list of error strings. Empty list = valid.

    locked_steps: steps that have a preferredKind set. For each:
      - Exactly ONE node must carry that step's stepId.
      - That node's type must equal preferredKind.
    """
    errors: List[str] = []
    node_ids = {n.nodeId for n in plan.nodes}

    # entryNodeId exists
    if plan.entryNodeId not in node_ids:
        errors.append(f"entryNodeId '{plan.entryNodeId}' not found in nodes.")

    # endNodeId exists and type="end"
    end_node = next((n for n in plan.nodes if n.nodeId == plan.endNodeId), None)
    if end_node is None:
        errors.append(f"endNodeId '{plan.endNodeId}' not found in nodes.")
    elif end_node.type != "end":
        errors.append(f"endNodeId node '{plan.endNodeId}' must have type='end', got '{end_node.type}'.")

    # nodeId uniqueness
    seen: set = set()
    for n in plan.nodes:
        if n.nodeId in seen:
            errors.append(f"Duplicate nodeId: '{n.nodeId}'.")
        seen.add(n.nodeId)

    # All edge fromNode/to reference declared nodes
    for e in plan.edges:
        if e.fromNode not in node_ids:
            errors.append(f"Edge fromNode '{e.fromNode}' not in nodes.")
        if e.to not in node_ids:
            errors.append(f"Edge to '{e.to}' not in nodes.")

    # Router nodes have ≥ 2 outgoing edges
    out_edges: Dict[str, int] = defaultdict(int)
    for e in plan.edges:
        out_edges[e.fromNode] += 1
    for n in plan.nodes:
        if n.type == "router" and out_edges.get(n.nodeId, 0) < 2:
            errors.append(
                f"Router node '{n.nodeId}' has {out_edges.get(n.nodeId, 0)} outgoing edge(s); "
                f"routers require ≥ 2."
            )

    # Non-router, non-end nodes with 0 outgoing edges (warn only — not a hard error)
    for n in plan.nodes:
        if n.type not in ("router", "end") and out_edges.get(n.nodeId, 0) == 0:
            errors.append(
                f"Node '{n.nodeId}' (type={n.type}) has no outgoing edges. "
                f"Every non-end node must have at least one outgoing edge."
            )

    # Reachability: BFS from entry
    if plan.entryNodeId in node_ids:
        reachable: set = set()
        queue = deque([plan.entryNodeId])
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in plan.edges:
            adj[e.fromNode].append(e.to)
        while queue:
            cur = queue.popleft()
            if cur in reachable:
                continue
            reachable.add(cur)
            for nxt in adj.get(cur, []):
                if nxt not in reachable:
                    queue.append(nxt)
        unreachable = node_ids - reachable
        for nid in sorted(unreachable):
            errors.append(f"Node '{nid}' is unreachable from entry '{plan.entryNodeId}'.")

    # ── preferredKind constraints ──────────────────────────────────────────────
    # For each locked step: exactly ONE node carries that stepId, and its type
    # must equal preferredKind. Any deviation is a hard validation error.
    if locked_steps:
        # Build a map: stepId → list of nodes that carry it
        step_node_map: Dict[str, List[SkeletonNode]] = defaultdict(list)
        for n in plan.nodes:
            if n.stepId:
                step_node_map[n.stepId].append(n)

        for ls in locked_steps:
            sid = ls.stepId
            kind = ls.preferredKind
            nodes_for_step = step_node_map.get(sid, [])

            if len(nodes_for_step) == 0:
                errors.append(
                    f"Step '{sid}' has preferredKind='{kind}' but no node carries stepId='{sid}'. "
                    f"The skeleton must include exactly one {kind} node for this step."
                )
            elif len(nodes_for_step) > 1:
                node_ids_list = [n.nodeId for n in nodes_for_step]
                errors.append(
                    f"Step '{sid}' has preferredKind='{kind}' (LOCKED — no splitting allowed) "
                    f"but {len(nodes_for_step)} nodes carry stepId='{sid}': {node_ids_list}. "
                    f"Merge them into a single {kind} node."
                )
            else:
                node = nodes_for_step[0]
                if node.type != kind:
                    errors.append(
                        f"Step '{sid}' requires type='{kind}' but node '{node.nodeId}' "
                        f"has type='{node.type}'. Change the node type to '{kind}'."
                    )

    return errors


async def _stage1_skeleton_planner(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
) -> SkeletonPlan:
    """
    Stage 1: Call LLM to produce a SkeletonPlan (topology only, no params).
    Validates the result and retries up to _MAX_RETRIES times.
    """
    prompt = _build_skeleton_user_prompt(state)
    full_prompt = _build_skeleton_prompt(state)
    cached_content = await _get_scratch_cached_content(
        api_key,
        _SKELETON_PLANNER_MODEL,
        system_text=_SKELETON_CACHED_SYSTEM,
        cache_key_prefix="scratch_skeleton",
        display_name="scratch-skeleton-system",
    )
    last_error: Optional[str] = None
    # Pre-compute locked steps once — reused in every validation attempt
    locked_steps = [s for s in state.steps if s.preferredKind is not None]
    if locked_steps:
        logger.info(
            f"[scratch/stage1] {len(locked_steps)} locked step(s): "
            + ", ".join(f"{s.stepId}={s.preferredKind}" for s in locked_steps)
        )

    for attempt in range(1, _MAX_RETRIES + 1):
        temperature = round(0.2 + (attempt - 1) * 0.1, 2)
        logger.info(f"[scratch/stage1] Skeleton planner attempt {attempt}/{_MAX_RETRIES} "
                    f"(phase='{state.phaseId}', temp={temperature})")
        try:
            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_SKELETON_PLANNER_MODEL,
                sub_operation="stage1-skeleton-planner",
                system_text=_SKELETON_CACHED_SYSTEM,
                user_prompt=prompt,
                full_prompt=full_prompt,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={"stage": "1", "phase_name": state.phaseName},
                temperature=temperature,
                as_json=True,
                response_schema=SkeletonPlan,
            )
            if parsed is None:
                # Fall back to manual parse from raw_text
                data = _parse_json_robust(raw)
                skeleton = SkeletonPlan(**data)
            else:
                skeleton = parsed

            errors = _validate_skeleton(skeleton, locked_steps=locked_steps)
            if errors:
                last_error = "; ".join(errors)
                logger.warning(
                    f"[scratch/stage1] Skeleton validation failed (attempt {attempt}): {last_error}"
                )
                # Inject error feedback into the next prompt
                prompt = (
                    f"PREVIOUS ATTEMPT FAILED VALIDATION:\n{last_error}\n\n"
                    f"Please fix all listed errors and regenerate the full SkeletonPlan.\n\n"
                    f"{_build_skeleton_user_prompt(state)}"
                )
                full_prompt = (
                    f"PREVIOUS ATTEMPT FAILED VALIDATION:\n{last_error}\n\n"
                    f"Please fix all listed errors and regenerate the full SkeletonPlan.\n\n"
                    f"{_build_skeleton_prompt(state)}"
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                continue

            type_counts: Dict[str, int] = defaultdict(int)
            for n in skeleton.nodes:
                type_counts[n.type] += 1
            logger.info(
                f"[scratch/stage1] Skeleton validated: {len(skeleton.nodes)} node(s), "
                f"{len(skeleton.edges)} edge(s), types={dict(type_counts)}, "
                f"entry={skeleton.entryNodeId}, end={skeleton.endNodeId}."
            )
            logger.debug(
                "[scratch/stage1] Skeleton structure:\n"
                + _json_compact(
                    {
                        "entryNodeId": skeleton.entryNodeId,
                        "endNodeId": skeleton.endNodeId,
                        "nodes": [
                            {
                                "nodeId": n.nodeId,
                                "type": n.type,
                                "stepId": n.stepId,
                                "name": n.name,
                            }
                            for n in skeleton.nodes
                        ],
                        "edges": [
                            {"from": e.fromNode, "to": e.to, "kind": e.kind}
                            for e in skeleton.edges
                        ],
                    }
                )
            )
            return skeleton

        except BillingError:
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"[scratch/stage1] Attempt {attempt} error: {exc}")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.5 * attempt)

    raise RuntimeError(
        f"[scratch/stage1] Skeleton planner failed after {_MAX_RETRIES} attempts "
        f"for phase '{state.phaseId}'. Last error: {last_error}"
    )


# ===========================================================================
# SECTION 3a — STAGE 1.2: NODE DESCRIPTION ENRICHER
# ===========================================================================

_DESC_ENRICHER_SYSTEM = """You are a production workflow specification writer for enterprise applications.

Your ONLY job is to rewrite node descriptions so a downstream code generator can emit
production-grade, ready-to-run implementations — not tutorial, toy, or happy-path stubs.

You do NOT change graph topology. You do NOT add, remove, rename, or retarget nodes or edges.
Return one enriched description per requested nodeId.

═══════════════════════════════════════════════════════
WHY THIS PASS EXISTS
═══════════════════════════════════════════════════════
The skeleton planner invents structure. Under that load it compresses the user requirement
into thin blurbs. Those blurbs are passed VERBATIM to screen/tool generators.
Thin descriptions produce generic code (e.g. email = regex; verification = "if strings match").
Your descriptions must restore every user-stated detail AND specify the enterprise-grade solution.

═══════════════════════════════════════════════════════
HARD CONSTRAINTS
═══════════════════════════════════════════════════════
1. Enrich ONLY the nodeIds listed under ENRICH THESE NODES. Ignore all others.
2. Keep the same nodeId strings exactly.
3. Do not invent nodes, edges, routers, or types.
4. Do not drop facts from the ORIGINAL USER REQUIREMENT that belong on that node.
   Redistribute user-stated fields, labels, APIs, collections, branch values, SLAs,
   and business rules onto the node that owns them. Preserve the user's wording
   for names, labels, and exact status strings.
5. "Different scenarios" belong INSIDE existing nodes (validation failure UX, retries,
   timeouts, unknown router values). Do not imply new topology.
6. Never shrink a useful spec into a slogan. If the skeleton already names fields or
   collections, KEEP those names and expand around them.

═══════════════════════════════════════════════════════
DOMAIN-AWARE PRODUCTION DESIGN
═══════════════════════════════════════════════════════
Read the full workflow requirement first. Infer the industry / product context
(campaign management, KYC/onboarding, ticketing, M&A, billing, underwriting, etc.)
and design EACH node as it would ship in that enterprise product — not a demo.

Toy vs production (this is the bar):
  BAD (toy email): "Validate email with a regex; output valid/invalid."
  GOOD (campaign / outreach): "Enterprise email deliverability check, not syntax-only.
       1) Normalize (trim; lowercase domain; IDN → punycode).
       2) RFC 5321/5322 syntax (reject spaces, multiple @, invalid quoted locals).
       3) DNS: resolve domain; require MX (fallback A/AAAA only if MX absent AND
          policy allows); treat NXDOMAIN / SERVFAIL as undeliverable with reason codes.
       4) Reject disposable / temporary domains from a maintained blocklist.
       5) Flag role accounts (info@, admin@, noreply@) per campaign policy.
       6) Optional SMTP RCPT TO probe only if configured; timeout 8s; never treat
          timeout as success.
       Output: email_normalized, deliverability_status
         ('deliverable'|'risky'|'undeliverable'|'unknown'),
         reason_code (syntax_invalid|no_mx|disposable|role_account|dns_failure|smtp_rejected|ok),
         reason_detail. Do not log full addresses in violation of PII policy."

  BAD (toy OCR/KYC): "Run OCR and compare names."
  GOOD: "OCR with confidence threshold; fallback vision model; unicode-normalize +
       casefold comparison; ignore honorifics; reject low-quality frames; structured
       mismatch_reason; retention policy for raw ID images."

Specify, when relevant to THIS node:
  • Exact algorithm / decision procedure (ordered steps, thresholds, match rules)
  • Standard libraries or platform services by name (dnspython MX, email-validator,
    Tesseract + Gemini vision fallback, etc.) — not random unpaid SaaS the user
    never mentioned. Prefer libraries and credentials already in Stage 0 / the prompt.
  • Failure modes and reason codes (operators need why, not only a boolean)
  • Timeouts, retries with backoff, idempotency keys for writes / side effects
  • Input sanitization, PII / secret handling, audit fields
  • Empty, invalid, duplicate, timeout, and unknown-value paths
  • Observability: what to log vs what never to log

═══════════════════════════════════════════════════════
DESCRIPTION FORMAT — this is the codegen spec
═══════════════════════════════════════════════════════
Each description is a complete standalone specification for THIS node only.

For ui_screen nodes include:
  • Layout and every control (labels, types, accept filters, required, placeholders)
  • Client-side AND server-side validation (never client-only for money/PII/identity)
  • Pre-fill from prior nodes (name the source node + field)
  • Submit / secondary actions and the backend handler behavior
  • Error, empty, loading, and success states the user actually sees
  • If Mongo: exact collection name AND field:type schema; mapping stanza if names differ
  • Accessibility: labels tied to inputs, validation announced, keyboard submit

For tool nodes include:
  • Every input (name + where it comes from) and every output (name + value space)
  • Ordered processing steps with thresholds and branch logic inside the tool
  • DB: collection, operation, filter keys, field:type schema, mapping stanza
  • Side effects: retries, idempotency, what happens on partial failure
  • Status/flag outputs MUST list exact strings later routers will compare

For router nodes include:
  • Exact param evaluated (producing node + name)
  • Exact string/boolean values for each existing outgoing branch
  • Unexpected/null values must still use an EXISTING edge (usually fallback)

═══════════════════════════════════════════════════════
FIDELITY RULES — USER-STATED FACTS MUST SURVIVE VERBATIM
═══════════════════════════════════════════════════════
These rules are the highest-priority constraints and override production-design inferences.

1. VERBATIM PRESERVATION — the following user-stated facts MUST appear in the enriched
   description exactly as the user wrote them (do NOT paraphrase, generalize, or drop):
   • External API: every base URL, endpoint path, HTTP method, header/auth type, and
     any sample request body, response shape, or field mapping the user described.
   • Database: every collection name and field name the user stated, with their types
     when given. Do NOT rename or generalize (e.g. "leads_table" ≠ "records").
   • Algorithms: exact scoring tables, threshold values, match rules, SLA durations.
   • Business rules: exact branch conditions, status strings, enums, error codes.
   • UI: exact button labels, column headers, status badge text the user provided.

2. MISSING SCHEMA — if the user mentioned a collection but no field schema was supplied:
   → Include in the description:
     "Runtime: inspect the [collection_name] collection to discover actual field names
      before querying. Filter by [user-stated key field]. Never assume snake_case vs
      camelCase — resolve from live schema."

3. REDISTRIBUTION — spread facts to the node that OWNS them.
   If a user-stated fact (URL, field name, business rule) belongs to a sibling node,
   add a cross-reference: "See <sibling_nodeId> for [fact]." Do NOT silently drop it.

DATA RULES:
  • No bare object/array JSON blobs. Scalars are named fields. Lists use itemType
    scalar (primitives), record (itemSchema), or enum — never wrap string arrays as records.
  • Every field mentioned must be an explicit named parameter.
  • Files: base64 or URL string + separate extracted fields.
  • Collection names consistent across nodes.

Output valid JSON matching the schema — no markdown, no explanation."""

_DESC_ENRICHER_JSON_SCHEMA = """Required JSON schema:
{
  "nodes": [
    {
      "nodeId": "snake_case_id",
      "description": "Production-grade standalone specification for this node."
    }
  ]
}"""

_DESC_ENRICHER_CACHED_SYSTEM = f"{_DESC_ENRICHER_SYSTEM}\n\n{_DESC_ENRICHER_JSON_SCHEMA}"


def _enrichable_skeleton_nodes(skeleton: SkeletonPlan) -> List[SkeletonNode]:
    return [n for n in skeleton.nodes if n.type != "end"]


def _format_skeleton_for_description_enricher(skeleton: SkeletonPlan) -> str:
    node_lines: List[str] = []
    for n in skeleton.nodes:
        node_lines.append(
            f"  - nodeId={n.nodeId} type={n.type} name={n.name} stepId={n.stepId or '-'}\n"
            f"    current_description: {n.description or '(empty)'}"
        )
    edges = "\n".join(
        f"  - {e.fromNode} -> {e.to} [{e.kind}]"
        for e in skeleton.edges
    )
    return (
        f"ENTRY: {skeleton.entryNodeId}\n"
        f"END  : {skeleton.endNodeId}\n"
        f"NODES:\n{chr(10).join(node_lines) if node_lines else '  (none)'}\n"
        f"EDGES:\n{edges or '  (none)'}"
    )


def _requirement_text_for_enricher(state: ScratchPlanningState) -> str:
    step_lines: List[str] = []
    for s in state.steps:
        kind = s.preferredKind or "FREE"
        step_lines.append(
            f"  {s.stepId} | {s.name} [{kind}]\n"
            f"     {s.description}"
        )
    steps_text = "\n".join(step_lines) if step_lines else "  (none)"
    return (
        f"PHASE: {state.phaseName}\n"
        f"GOAL : {state.phaseDescription}\n\n"
        f"ORIGINAL USER REQUIREMENT / LOGICAL STEPS (authoritative — restore details):\n"
        f"{steps_text}"
    )


def _build_desc_enricher_user_prompt(
    state: ScratchPlanningState,
    batch_nodes: List[SkeletonNode],
) -> str:
    skeleton = state.skeletonPlan
    assert skeleton is not None
    batch_ids = [n.nodeId for n in batch_nodes]
    batch_block = "\n".join(
        f"  - {n.nodeId} ({n.type}) {n.name}" for n in batch_nodes
    )

    prior_section = ""
    if state.priorPhaseOutputs:
        prior_lines = [
            f"  - {g.name} : {_format_ref_type_of(g)} (from {g.source})"
            + (f" — {g.description}" if (g.description or "").strip() else "")
            for g in state.priorPhaseOutputs
        ]
        prior_section = (
            "\nDATA AVAILABLE FROM PRIOR PHASES:\n"
            + "\n".join(prior_lines)
            + "\n"
        )

    registry_section = _format_registry_for_prompt(state.workflowRegistry)
    schemas_section = ""
    if state.knownCollectionSchemasText and state.knownCollectionSchemasText.strip():
        schemas_section = f"\n{state.knownCollectionSchemasText.strip()}\n"

    stage0_section = ""
    if state.stage0Context and state.stage0Context.strip():
        constraints_only = _stage0_constraints_only(state.stage0Context.strip())
        if constraints_only:
            stage0_section = f"\n{constraints_only}\n"

    return f"""{_requirement_text_for_enricher(state)}
{registry_section}{schemas_section}{prior_section}{stage0_section}
SKELETON GRAPH (topology is FROZEN — do not change it):
{_format_skeleton_for_description_enricher(skeleton)}

ENRICH THESE NODES (return exactly these nodeIds, nothing else):
{batch_block}

Rewrite each listed node's description into a production-grade codegen spec for this
enterprise workflow. Apply ALL FIDELITY RULES from the system prompt:
• Preserve every user-stated fact verbatim (API URLs, collection names, field names,
  status strings, threshold values, business rules, UI labels).
• If the user mentioned a DB collection but no schema appears above → add the runtime
  inspect note specified in FIDELITY RULE 2.
Do not invent requirements, add nodes, or change graph topology.
Target nodeIds: {", ".join(batch_ids)}"""


def _build_desc_enricher_prompt(
    state: ScratchPlanningState,
    batch_nodes: List[SkeletonNode],
) -> str:
    return (
        f"{_DESC_ENRICHER_CACHED_SYSTEM}\n\n"
        f"{_build_desc_enricher_user_prompt(state, batch_nodes)}"
    )


def _resolve_enricher_node_id(
    raw_id: str,
    skeleton_ids: Dict[str, str],
    skeleton_ids_lower: Dict[str, List[str]],
) -> Optional[str]:
    """Map an enricher nodeId onto a skeleton id (exact, then unique casefold)."""
    nid = (raw_id or "").strip()
    if not nid:
        return None
    if nid in skeleton_ids:
        return skeleton_ids[nid]
    matches = skeleton_ids_lower.get(nid.lower()) or []
    if len(matches) == 1:
        return matches[0]
    return None


def _description_collapsed(original: str, incoming: str) -> bool:
    """True when incoming looks like slogan-collapse, not a denser rewrite."""
    orig = (original or "").strip()
    inc = (incoming or "").strip()
    if not orig or not inc:
        return False
    # Keep original only when the rewrite is under half the prior spec (or tiny).
    return len(inc) < max(80, int(0.5 * len(orig)))


def _parse_enrichment_result(raw: str, parsed: Any) -> NodeDescriptionEnrichmentResult:
    if isinstance(parsed, NodeDescriptionEnrichmentResult):
        return parsed
    if isinstance(parsed, dict):
        return NodeDescriptionEnrichmentResult(**parsed)
    data = _parse_json_robust(raw)
    if isinstance(data, dict):
        return NodeDescriptionEnrichmentResult(**data)
    raise ValueError("enricher response was not a JSON object")


def _enrichment_result_errors(
    result: NodeDescriptionEnrichmentResult,
    batch_nodes: List[SkeletonNode],
    skeleton: SkeletonPlan,
) -> List[str]:
    """Coverage errors that should trigger a batch retry."""
    skeleton_ids = {n.nodeId: n.nodeId for n in skeleton.nodes}
    skeleton_ids_lower: Dict[str, List[str]] = defaultdict(list)
    for n in skeleton.nodes:
        skeleton_ids_lower[n.nodeId.lower()].append(n.nodeId)

    by_id: Dict[str, str] = {}
    for item in result.nodes or []:
        resolved = _resolve_enricher_node_id(
            item.nodeId or "", skeleton_ids, skeleton_ids_lower
        )
        desc = (item.description or "").strip()
        if resolved and desc:
            by_id[resolved] = desc

    errors: List[str] = []
    current_desc = {n.nodeId: (n.description or "") for n in skeleton.nodes}
    for n in batch_nodes:
        incoming = by_id.get(n.nodeId)
        if not incoming:
            errors.append(f"missing non-empty description for '{n.nodeId}'")
            continue
        orig = (current_desc.get(n.nodeId) or n.description or "").strip()
        if _description_collapsed(orig, incoming):
            errors.append(
                f"description for '{n.nodeId}' collapsed "
                f"({len(orig)}→{len(incoming)} chars); expand to a production-grade spec"
            )
    return errors


def _apply_enriched_descriptions(
    skeleton: SkeletonPlan,
    enriched_nodes: List[EnrichedNodeDescription],
    *,
    allowed_ids: Optional[set] = None,
) -> Tuple[SkeletonPlan, int, List[str]]:
    """
    Merge enricher output onto the skeleton. Topology is unchanged.

    Ignores unknown nodeIds, end nodes, empty text, and slogan-collapsed rewrites.
    """
    allowed = allowed_ids if allowed_ids is not None else {n.nodeId for n in skeleton.nodes}
    skeleton_ids = {n.nodeId: n.nodeId for n in skeleton.nodes}
    skeleton_ids_lower: Dict[str, List[str]] = defaultdict(list)
    for n in skeleton.nodes:
        skeleton_ids_lower[n.nodeId.lower()].append(n.nodeId)

    by_id: Dict[str, str] = {}
    extras: List[str] = []
    for item in enriched_nodes or []:
        nid = (item.nodeId or "").strip()
        desc = (item.description or "").strip()
        if not nid or not desc:
            continue
        resolved = _resolve_enricher_node_id(nid, skeleton_ids, skeleton_ids_lower)
        if resolved is None:
            extras.append(nid)
            continue
        if resolved not in allowed:
            # Other-batch ids are ignored silently; only unknown ids are extras.
            continue
        by_id[resolved] = desc

    warnings: List[str] = []
    if extras:
        warnings.append(
            f"enricher returned extra nodeIds (ignored): {sorted(set(extras))}"
        )

    applied = 0
    new_nodes: List[SkeletonNode] = []
    for n in skeleton.nodes:
        incoming = by_id.get(n.nodeId)
        if n.type == "end" or not incoming:
            new_nodes.append(n)
            continue
        original = (n.description or "").strip()
        if _description_collapsed(original, incoming):
            warnings.append(
                f"enricher collapsed '{n.nodeId}' "
                f"({len(original)}→{len(incoming)} chars); keeping original"
            )
            new_nodes.append(n)
            continue
        new_nodes.append(n.model_copy(update={"description": incoming}))
        applied += 1

    missing = [
        n.nodeId
        for n in skeleton.nodes
        if n.nodeId in allowed and n.type != "end" and n.nodeId not in by_id
    ]
    if missing:
        warnings.append(f"enricher omitted nodeIds: {missing}")

    return skeleton.model_copy(update={"nodes": new_nodes}), applied, warnings


async def _stage1_2_enrich_batch(
    state: ScratchPlanningState,
    batch_nodes: List[SkeletonNode],
    api_key: Optional[str],
    cached_content: Optional[str],
    batch_index: int,
    batch_count: int,
) -> NodeDescriptionEnrichmentResult:
    skeleton = state.skeletonPlan
    assert skeleton is not None
    user_prompt = _build_desc_enricher_user_prompt(state, batch_nodes)
    full_prompt = _build_desc_enricher_prompt(state, batch_nodes)
    last_error: Optional[str] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        temperature = round(0.2 + (attempt - 1) * 0.1, 2)
        logger.info(
            f"[scratch/stage1.2] Enricher batch {batch_index}/{batch_count} "
            f"attempt {attempt}/{_MAX_RETRIES} nodes={len(batch_nodes)} temp={temperature}"
        )
        try:
            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_DESC_ENRICHER_MODEL,
                sub_operation="stage1.2-description-enricher",
                system_text=_DESC_ENRICHER_CACHED_SYSTEM,
                user_prompt=user_prompt,
                full_prompt=full_prompt,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={
                    "stage": "1.2",
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "node_ids": [n.nodeId for n in batch_nodes],
                },
                temperature=temperature,
                as_json=True,
                response_schema=NodeDescriptionEnrichmentResult,
            )
            result = _parse_enrichment_result(raw, parsed)
            coverage_errors = _enrichment_result_errors(result, batch_nodes, skeleton)
            if coverage_errors:
                last_error = "; ".join(coverage_errors)
                logger.warning(
                    f"[scratch/stage1.2] Batch {batch_index} coverage failed "
                    f"(attempt {attempt}): {last_error}"
                )
                retry_prefix = (
                    f"PREVIOUS ATTEMPT FAILED VALIDATION:\n{last_error}\n\n"
                    "Return a production-grade description for EVERY requested nodeId. "
                    "Do not omit nodes. Do not collapse the spec into a slogan.\n\n"
                )
                user_prompt = retry_prefix + _build_desc_enricher_user_prompt(state, batch_nodes)
                full_prompt = retry_prefix + _build_desc_enricher_prompt(state, batch_nodes)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                continue
            return result
        except BillingError:
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"[scratch/stage1.2] Batch {batch_index} attempt {attempt} error: {exc}")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(
        f"[scratch/stage1.2] Enricher batch {batch_index} failed after "
        f"{_MAX_RETRIES} attempts. Last error: {last_error}"
    )


async def _stage1_2_enrich_node_descriptions(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
) -> Tuple[Optional[SkeletonPlan], List[str]]:
    """
    Stage 1.2 — Rewrite skeleton node descriptions into production-grade codegen specs.

    Topology is frozen. Failures are non-fatal (caller keeps Stage 1 descriptions).
    """
    skeleton = state.skeletonPlan
    if skeleton is None:
        return None, ["stage1.2 skipped: no skeleton"]

    targets = _enrichable_skeleton_nodes(skeleton)
    if not targets:
        logger.info("[scratch/stage1.2] No enrichable nodes — skip.")
        return skeleton, []

    cached_content = await _get_scratch_cached_content(
        api_key,
        _DESC_ENRICHER_MODEL,
        system_text=_DESC_ENRICHER_CACHED_SYSTEM,
        cache_key_prefix="scratch_desc_enricher",
        display_name="scratch-desc-enricher-system",
    )

    warnings: List[str] = []
    applied_total = 0
    batches = [
        targets[i : i + _DESC_ENRICHER_BATCH_SIZE]
        for i in range(0, len(targets), _DESC_ENRICHER_BATCH_SIZE)
    ]
    logger.info(
        f"[scratch/stage1.2] Enriching {len(targets)} node description(s) "
        f"in {len(batches)} batch(es) of ≤{_DESC_ENRICHER_BATCH_SIZE}"
    )

    for batch_index, batch_nodes in enumerate(batches, start=1):
        allowed_ids = {n.nodeId for n in batch_nodes}
        try:
            result = await _stage1_2_enrich_batch(
                state,
                batch_nodes,
                api_key,
                cached_content,
                batch_index,
                len(batches),
            )
            skeleton, applied, merge_warnings = _apply_enriched_descriptions(
                skeleton,
                result.nodes,
                allowed_ids=allowed_ids,
            )
            state.skeletonPlan = skeleton
            applied_total += applied
            warnings.extend(merge_warnings)
            logger.info(
                f"[scratch/stage1.2] Batch {batch_index}/{len(batches)} applied "
                f"{applied}/{len(batch_nodes)} description(s)"
            )
        except BillingError:
            raise
        except Exception as exc:
            msg = f"batch {batch_index} failed ({exc}); keeping Stage 1 descriptions for {sorted(allowed_ids)}"
            logger.warning("[scratch/stage1.2] %s", msg)
            warnings.append(msg)

    logger.info(
        f"[scratch/stage1.2] Done: applied {applied_total}/{len(targets)} "
        f"enriched description(s)"
    )
    return skeleton, warnings


# ===========================================================================
# SECTION 3b — STAGE 1.5: ADVISORY GLOBAL VARIABLE SELECTION
# ===========================================================================

_GLOBAL_SELECTOR_SYSTEM = """You are selecting which prior-phase outputs (global variables)
are most likely needed by the CURRENT phase of a multi-phase workflow.

You will receive:
  1. The CURRENT phase goal and its skeleton graph (node names + descriptions only —
     no input/output params yet).
  2. The FULL list of prior-phase outputs available as global.* bindings.
     Names are namespaced as "<sourceNodeId>.<field>" when two nodes can emit the
     same field (e.g. screen_upload.status vs tool_validate.status). Copy those
     names EXACTLY into selectedGlobalNames.

Your job: choose a PRIMARY subset of those globals that this phase will likely consume.
Be inclusive when unsure — missing a needed global from the primary set is worse than
including an unused one. This selection is advisory only; Stage 2 will still see the
rest as a secondary fallback.

Prefer globals whose names/descriptions match data the phase nodes will consume
(e.g. user-edited invoices for anomaly detection, not raw OCR extraction).
Prefer the more recent / downstream artifact when two globals represent the same
logical dataset at different stages of processing. When both exist, select the
namespaced name of the artifact you want — do not collapse them to a bare field.

Return ONLY JSON matching the schema.""".strip()


def _format_prior_globals_for_selection(globals_list: List[GlobalVar]) -> str:
    if not globals_list:
        return "  (none)"
    lines: List[str] = []
    for g in globals_list:
        desc = (g.description or "").strip()
        desc_part = f" — {desc}" if desc else ""
        lines.append(
            f"  - name={g.name} type={_format_ref_type_of(g)} source={g.source}{desc_part}"
        )
    return "\n".join(lines)


def _format_skeleton_for_global_selection(skeleton: SkeletonPlan) -> str:
    node_lines = []
    for n in skeleton.nodes:
        if n.type in ("router", "end"):
            continue
        node_lines.append(
            f"  - nodeId={n.nodeId} type={n.type} name={n.name}\n"
            f"    description: {n.description}"
        )
    edges = "\n".join(
        f"  - {e.fromNode} -> {e.to} [{e.kind}]"
        for e in skeleton.edges
    )
    return (
        f"ENTRY: {skeleton.entryNodeId}\n"
        f"END  : {skeleton.endNodeId}\n"
        f"NODES:\n{chr(10).join(node_lines) if node_lines else '  (none)'}\n"
        f"EDGES:\n{edges or '  (none)'}"
    )


def _partition_globals(
    all_globals: List[GlobalVar],
    selected_names: List[str],
) -> Tuple[List[GlobalVar], List[GlobalVar]]:
    """
    Split all_globals into (primary, secondary).
    Unknown selected names are ignored. If selection is empty, primary = all.
    """
    if not all_globals:
        return [], []
    if not selected_names:
        return list(all_globals), []

    by_name = {g.name: g for g in all_globals if g.name}
    aliases = unique_bare_aliases(by_name.keys())
    primary: List[GlobalVar] = []
    seen: set = set()
    for name in selected_names:
        raw = (name or "").strip()
        if raw.startswith("global."):
            raw = raw[len("global.") :]
        g = by_name.get(raw)
        if g is None and raw in aliases:
            g = by_name.get(aliases[raw])
        if g is None and global_field_basename(raw) == raw:
            # Unique bare field (or LLM dropped the node prefix).
            canon = aliases.get(raw)
            if canon:
                g = by_name.get(canon)
        if g and g.name not in seen:
            primary.append(g)
            seen.add(g.name)
    secondary = [g for g in all_globals if g.name not in seen]
    if not primary:
        # Safety: never leave primary empty when globals exist
        return list(all_globals), []
    return primary, secondary


def _seed_register_globals(state: ScratchPlanningState) -> None:
    """
    Populate availableVarsRegister.globals (primary) and secondaryGlobals from
    priorPhaseOutputs + selectedPrimaryGlobalNames. Idempotent / overwrite-safe.
    """
    register = state.availableVarsRegister
    primary, secondary = _partition_globals(
        list(state.priorPhaseOutputs or []),
        list(state.selectedPrimaryGlobalNames or []),
    )
    register.globals = primary
    register.secondaryGlobals = secondary
    _log_binding_audit(
        "register_seeded",
        phaseId=state.phaseId,
        primaryCount=len(primary),
        secondaryCount=len(secondary),
        primaryNames=[g.name for g in primary],
        secondaryNames=[g.name for g in secondary],
    )


async def _stage1_5_select_primary_globals(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
) -> List[str]:
    """
    Stage 1.5 — Advisory selection of which prior-phase globals are primary
    for Stage 2a's binder. Coarse guess based on skeleton names/descriptions.
    On failure, falls back to selecting ALL globals as primary.
    """
    all_globals = list(state.priorPhaseOutputs or [])
    if not all_globals:
        return []
    if state.skeletonPlan is None:
        return [g.name for g in all_globals]

    known_names = {g.name for g in all_globals}
    user_prompt = f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

SKELETON GRAPH FOR THIS PHASE:
{_format_skeleton_for_global_selection(state.skeletonPlan)}

ALL PRIOR-PHASE GLOBALS (choose a primary subset):
{_format_prior_globals_for_selection(all_globals)}

Return selectedGlobalNames as a list of exact 'name' values from the list above
(copy "<sourceNodeId>.<field>" when that is the listed name; do not drop the nodeId).
Prefer inclusion when uncertain. Prefer edited/final artifacts over raw/earlier ones
when both exist for the same logical data.
"""
    full_prompt = f"{_GLOBAL_SELECTOR_SYSTEM}\n\n{user_prompt}"
    cached_content = await _get_scratch_cached_content(
        api_key,
        _GLOBAL_SELECTOR_MODEL,
        system_text=_GLOBAL_SELECTOR_SYSTEM,
        cache_key_prefix="scratch_global_selector",
        display_name="scratch-global-selector-system",
    )

    last_error: Optional[str] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_GLOBAL_SELECTOR_MODEL,
                sub_operation="stage1.5-global-selector",
                system_text=_GLOBAL_SELECTOR_SYSTEM,
                user_prompt=user_prompt,
                full_prompt=full_prompt,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={"stage": "1.5", "global_count": len(all_globals)},
                temperature=0.1,
                as_json=True,
                response_schema=GlobalSelectionResult,
            )
            if parsed is None:
                data = _parse_json_robust(raw)
                result = GlobalSelectionResult(**data)
            else:
                result = parsed

            selected = _canonical_selected_global_names(
                list(result.selectedGlobalNames or []), known_names
            )
            if not selected:
                # Empty selection → treat as "all" (safe default)
                selected = [g.name for g in all_globals]
            logger.info(
                f"[scratch/stage1.5] Selected {len(selected)}/{len(all_globals)} "
                f"primary global(s): {selected}. rationale={result.rationale!r}"
            )
            _log_binding_audit(
                "stage1_5_primary_selected",
                phaseId=state.phaseId,
                selectedPrimary=selected,
                allGlobals=[g.name for g in all_globals],
                rationale=result.rationale,
            )
            return selected
        except BillingError:
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"[scratch/stage1.5] Attempt {attempt} error: {exc}")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.0 * attempt)

    logger.warning(
        f"[scratch/stage1.5] Falling back to ALL globals as primary "
        f"(last_error={last_error})"
    )
    return [g.name for g in all_globals]


_SECONDARY_LOOKUP_SYSTEM = """You decide which compact SECONDARY prior-phase globals need
full descriptions injected into a Stage 2a binder retry.

Context: the binder already saw PRIMARY globals (with descriptions) and SECONDARY globals
as name+type only. A required input still has no eligible binding (or referenced an unknown
ref) — often because a SECONDARY global is the correct source but its name alone was ambiguous.

Return only secondary global names that are plausible candidates for those unresolved
required inputs. Prefer inclusion when unsure. Do NOT return PRIMARY names. Return ONLY JSON.""".strip()


def _errors_warrant_secondary_detail_lookup(errors: List[str]) -> List[str]:
    """
    Return only Stage 2b errors where secondary-global descriptions could help.

    Secondary detail is useful when the binder cannot resolve a required input to any
    eligible candidate (missing binding, or unknown register ref). It does NOT help for:
      - missing/extra nodeIds
      - cross-branch / invalid ancestor path errors
      - outputParams carrying inputBindings
      - pick path/coverage structural issues (unless also an unknown-ref case)
    """
    relevant: List[str] = []
    for e in errors or []:
        el = e.lower()
        if "required inputparam" in el and "has no binding" in el:
            relevant.append(e)
        elif "is not in the register or earlier in this batch" in el:
            # Unknown ref — may need a secondary global the binder missed by name.
            relevant.append(e)
        # Explicitly ignore: Missing/Unexpected nodeIds, cross-branch ancestor,
        # outputParam inputBindings, pick mutual-exclusivity / coverage, etc.
    return relevant


async def _lookup_secondary_global_details(
    state: ScratchPlanningState,
    register: AvailableVarsRegister,
    *,
    batch: NodeBatch,
    skeleton: SkeletonPlan,
    validation_errors: List[str],
    api_key: Optional[str] = None,
) -> List[GlobalVar]:
    """
    Targeted follow-up when Stage 2a may need secondary globals.

    Only runs for unresolved-required / unknown-ref binding errors (see
    `_errors_warrant_secondary_detail_lookup`). Returns the subset of secondary
    GlobalVars (with full description/source) to inject into the next binder
    attempt. On failure / empty selection / irrelevant errors, returns [].
    """
    relevant_errors = _errors_warrant_secondary_detail_lookup(validation_errors)
    if not relevant_errors:
        logger.info(
            "[scratch/stage2a] Secondary detail lookup skipped — "
            "validation errors are not binding-candidate unresolved types."
        )
        return []

    secondary = list(register.secondaryGlobals or [])
    if not secondary:
        return []

    by_name = {g.name: g for g in secondary}
    aliases = unique_bare_aliases(by_name.keys())
    node_map = {n.nodeId: n for n in skeleton.nodes}
    batch_nodes = [node_map[nid] for nid in batch.nodeIds if nid in node_map]
    nodes_text = "\n".join(
        f"  - {n.nodeId} ({n.type}): {n.name} — {n.description}"
        for n in batch_nodes
    ) or "  (none)"
    secondary_text = "\n".join(
        f"  - name={g.name} type={g.datatype}" for g in secondary
    )
    error_text = "\n".join(f"  - {e}" for e in relevant_errors[:12]) or "  (none)"

    user_prompt = f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

BATCH NODES (need bindings):
{nodes_text}

UNRESOLVED BINDING ERRORS (only these warrant secondary detail):
{error_text}

SECONDARY GLOBALS (name + type only — choose which need full descriptions):
{secondary_text}

Return neededGlobalNames as names from the SECONDARY list above.
"""
    full_prompt = f"{_SECONDARY_LOOKUP_SYSTEM}\n\n{user_prompt}"
    cached_content = await _get_scratch_cached_content(
        api_key,
        _SECONDARY_LOOKUP_MODEL,
        system_text=_SECONDARY_LOOKUP_SYSTEM,
        cache_key_prefix="scratch_secondary_detail_lookup",
        display_name="scratch-secondary-detail-lookup",
    )

    try:
        raw, parsed = await _scratch_llm_call(
            api_key,
            model=_SECONDARY_LOOKUP_MODEL,
            sub_operation="stage2a-secondary-detail-lookup",
            system_text=_SECONDARY_LOOKUP_SYSTEM,
            user_prompt=user_prompt,
            full_prompt=full_prompt,
            cached_content=cached_content,
            project_id=state.projectId,
            org_id=state.orgId,
            subscriber_id=state.subscriberId,
            user_id=state.userId,
            phase_id=state.phaseId,
            attempt=1,
            metadata={
                "stage": "2a-secondary-lookup",
                "batch_index": batch.batchIndex,
                "secondary_count": len(secondary),
                "relevant_error_count": len(relevant_errors),
            },
            temperature=0.1,
            as_json=True,
            response_schema=SecondaryDetailLookupResult,
        )
        if parsed is None:
            data = _parse_json_robust(raw)
            result = SecondaryDetailLookupResult(**data)
        else:
            result = parsed

        selected: List[GlobalVar] = []
        seen_sel: set = set()
        for n in (result.neededGlobalNames or []):
            raw = (n or "").strip()
            if raw.startswith("global."):
                raw = raw[len("global.") :]
            g = by_name.get(raw)
            if g is None and raw in aliases:
                g = by_name.get(aliases[raw])
            if g and g.name not in seen_sel:
                selected.append(g)
                seen_sel.add(g.name)
        # Cap enrichment volume — rarely need more than a handful.
        if len(selected) > 8:
            selected = selected[:8]
        logger.info(
            f"[scratch/stage2a] Secondary detail lookup: "
            f"{len(selected)}/{len(secondary)} enriched — "
            f"{[g.name for g in selected]}. rationale={result.rationale!r}"
        )
        return selected
    except BillingError:
        raise
    except Exception as exc:
        logger.warning(
            f"[scratch/stage2a] Secondary detail lookup failed "
            f"(continuing without enrichment): {exc}"
        )
        return []


# ===========================================================================
# SECTION 4 — STAGE 2a: NODE SCHEMA BUILDER (batched LLM calls)
# ===========================================================================

def _effective_edges_between_eligible(
    node_ids: List[str],
    all_edges: List[SkeletonEdge],
) -> List[Tuple[str, str]]:
    """
    Build effective (from, to) edges between eligible nodes, traversing through
    router/end nodes transparently.

    Example: collect_docs → verify_doc → router → approve
    Eligible: {collect_docs, verify_doc, approve}
    Effective edges: (collect_docs, verify_doc), (verify_doc, approve)
    """
    eligible_set = set(node_ids)

    # Build adjacency for the full graph (including routers/end)
    full_adj: Dict[str, List[str]] = defaultdict(list)
    for e in all_edges:
        full_adj[e.fromNode].append(e.to)

    effective: List[Tuple[str, str]] = []

    def _reach_eligible(start: str, visited: Optional[set] = None) -> List[str]:
        """BFS from start; collect first eligible nodes reachable (stop at eligible nodes)."""
        if visited is None:
            visited = set()
        found: List[str] = []
        queue: deque = deque(full_adj.get(start, []))
        seen: set = set()
        while queue:
            cur = queue.popleft()
            if cur in seen or cur in visited:
                continue
            seen.add(cur)
            if cur in eligible_set:
                found.append(cur)
                # Do NOT traverse further from eligible nodes (they're their own ancestors)
            else:
                queue.extend(full_adj.get(cur, []))
        return found

    for nid in node_ids:
        for reachable in _reach_eligible(nid):
            effective.append((nid, reachable))

    return effective


def _topological_sort_nodes(node_ids: List[str], edges: List[SkeletonEdge]) -> List[str]:
    """
    Kahn's algorithm topological sort over the given node_ids.
    Traverses through router/end pass-through nodes to find effective ordering.
    Returns nodes in dependency order (ancestors first).
    """
    id_set = set(node_ids)
    in_degree: Dict[str, int] = {nid: 0 for nid in node_ids}
    adj: Dict[str, List[str]] = {nid: [] for nid in node_ids}

    for from_nid, to_nid in _effective_edges_between_eligible(node_ids, edges):
        if from_nid in id_set and to_nid in id_set and from_nid != to_nid:
            if to_nid not in adj[from_nid]:  # dedup
                adj[from_nid].append(to_nid)
                in_degree[to_nid] += 1

    queue = deque(nid for nid in node_ids if in_degree[nid] == 0)
    result: List[str] = []
    while queue:
        cur = queue.popleft()
        result.append(cur)
        for nxt in adj.get(cur, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    # Any remaining (cycle / unreachable) — append in original order
    remaining = [nid for nid in node_ids if nid not in set(result)]
    result.extend(remaining)
    return result


def _build_batch_plan(skeleton: SkeletonPlan) -> List[NodeBatch]:
    """
    Stage 2a Step 1: Build the strictly ordered batch plan (pure code, no LLM).

    Rules:
    - N ≤ 6 → 1 batch
    - N > 6 → ceil(N/6) batches, topologically ordered, STRICTLY SEQUENTIAL
    - Batch size = 6 max
    - Fan-in nodes placed after all their predecessors

    Batches always run in index order (0, 1, 2, ...). No parallelism.
    """
    BATCH_SIZE = 6

    eligible = [
        n.nodeId for n in skeleton.nodes
        if n.type not in ("router", "end")
    ]

    if not eligible:
        return []

    sorted_nodes = _topological_sort_nodes(eligible, skeleton.edges)

    chunks = [
        sorted_nodes[i: i + BATCH_SIZE]
        for i in range(0, len(sorted_nodes), BATCH_SIZE)
    ]

    batches = [
        NodeBatch(batchIndex=idx, nodeIds=chunk)
        for idx, chunk in enumerate(chunks)
    ]
    logger.debug(
        f"[scratch/stage2a] Batch plan: {len(batches)} batch(es), "
        f"sizes={[len(b.nodeIds) for b in batches]}"
    )
    return batches


def _path_index_ancestors(execution_path_index: Dict[str, Any], node_id: str) -> set:
    """Post-intersection ancestor set for *node_id* (old flat index or new dict shape)."""
    if not execution_path_index:
        return set()
    if "ancestors" in execution_path_index:
        return set(execution_path_index["ancestors"].get(node_id, set()) or set())
    return set(execution_path_index.get(node_id, set()) or set())


def _path_index_path_sets(execution_path_index: Dict[str, Any], node_id: str) -> List[set]:
    """Per-path ancestor sets at fan-in nodes (empty when index uses legacy flat shape)."""
    if not execution_path_index or "ancestors" not in execution_path_index:
        return []
    raw = execution_path_index.get("path_sets", {}).get(node_id, [])
    return [set(ps) for ps in raw]


def _parse_pick_refs(pick_bound_to: str) -> List[str]:
    """Parse ``pick:a.x,b.y`` → ``['a.x', 'b.y']``."""
    if not pick_bound_to.startswith("pick:"):
        return []
    return [r.strip() for r in pick_bound_to[5:].split(",") if r.strip()]


# Regex for indexed binding: "nodeId.paramName[<int>].fieldName"
_INDEXED_BINDING_RE = re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]\.([A-Za-z_][A-Za-z0-9_]*)$'
)


def _parse_bound_to(bound_to: str) -> Optional[BindingRef]:
    """
    Parse a plain nodeId.paramName or nodeId.paramName[idx].field binding string
    into a BindingRef.

    Returns None for global.*, pick:, or malformed strings.
    The caller is responsible for handling those cases before calling this.
    """
    if not bound_to or bound_to.startswith("global.") or bound_to.startswith("pick:"):
        return None
    m = _INDEXED_BINDING_RE.match(bound_to)
    if m:
        return BindingRef(
            node_id=m.group(1),
            param_name=m.group(2),
            index=int(m.group(3)),
            item_field=m.group(4),
        )
    # Plain "nodeId.paramName"
    if "." in bound_to:
        node_id, param_name = bound_to.split(".", 1)
        return BindingRef(node_id=node_id, param_name=param_name)
    return None


def _check_itemschema_compat(
    node_id: str,
    param_name: str,
    receiver_schema: List[Any],
    source_schema: List[Any],
    source_label: str,
) -> List[str]:
    """
    Check 7 helper: receiver's itemSchema must be a subset of source's itemSchema
    (every field declared by receiver must exist in source with matching datatype).

    Entries may be NodeParam objects or serialized dicts — both are handled via
    _item_field_name/_item_field_datatype helpers.
    """
    errors: List[str] = []
    source_by_name = {_item_field_name(f): f for f in source_schema}
    for rf in receiver_schema:
        rf_name = _item_field_name(rf)
        rf_dtype = _item_field_datatype(rf)
        sf = source_by_name.get(rf_name)
        if sf is None:
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': itemSchema field '{rf_name}' "
                f"not found in {source_label}'s itemSchema."
            )
        elif _item_field_datatype(sf) != rf_dtype:
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': itemSchema field '{rf_name}' "
                f"has datatype '{rf_dtype}' but {source_label} declares it as "
                f"'{_item_field_datatype(sf)}'."
            )
        elif rf_dtype == "list":
            # Nested list fields must match item kind, not just datatype=list.
            errors.extend(_check_list_binding_compat(
                node_id,
                f"{param_name}.{rf_name}",
                rf,
                _ref_meta_from_obj(sf),
                f"{source_label}.{rf_name}",
            ))
    return errors


def _check_list_binding_compat(
    node_id: str,
    param_name: str,
    receiving: Any,
    src_meta: "RefMeta",
    source_label: str,
) -> List[str]:
    """
    Check 7: list→list item-kind compatibility.

    scalar ↔ scalar (same scalarItemDatatype)
    enum   ↔ enum   (receiver values ⊆ source values, if both declare them)
    record ↔ record (receiver itemSchema ⊆ source itemSchema)
    Mixed kinds are incompatible — a string[] must not bind to [{skill: string}].
    """
    errors: List[str] = []
    recv_type = _effective_item_type(receiving)
    src_type = _effective_item_type(src_meta)
    if recv_type and src_type and recv_type != src_type:
        errors.append(
            f"Node '{node_id}' inputParam '{param_name}': list itemType '{recv_type}' "
            f"is incompatible with {source_label} itemType '{src_type}'. "
            f"Scalar arrays must bind to scalar arrays; record lists to record lists."
        )
        return errors
    if (recv_type or src_type) == "scalar":
        recv_dt = _obj_get(receiving, "scalarItemDatatype") or "string"
        src_dt = src_meta.scalarItemDatatype or "string"
        if recv_dt != src_dt:
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': scalar list item datatype "
                f"'{recv_dt}' does not match {source_label} '{src_dt}'."
            )
        return errors
    if (recv_type or src_type) == "enum":
        recv_vals = set(_obj_get(receiving, "enumValues") or [])
        src_vals = set(src_meta.enumValues or [])
        if recv_vals and src_vals and not recv_vals.issubset(src_vals):
            extra = sorted(recv_vals - src_vals)
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': enumValues {extra} "
                f"are not in {source_label}'s enumValues."
            )
        return errors
    recv_schema = _obj_get(receiving, "itemSchema") or []
    src_schema = src_meta.itemSchema or []
    if recv_schema:
        errors.extend(_check_itemschema_compat(
            node_id, param_name, recv_schema, src_schema, source_label
        ))
    return errors


def _validate_pick_binding(
    node_id: str,
    param_name: str,
    pick_bound_to: str,
    combined_refs: Dict[str, "RefMeta"],
    execution_path_index: Dict[str, Any],
    receiving_ip: Optional["NodeParam"] = None,
) -> List[str]:
    """Validate a pick: multi-source binding (Stage 2b)."""
    errors: List[str] = []
    pick_refs = _parse_pick_refs(pick_bound_to)
    if not pick_refs:
        errors.append(
            f"Node '{node_id}' inputParam '{param_name}': empty pick: binding."
        )
        return errors

    for pref in pick_refs:
        if pref.startswith("global."):
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': pick cannot include "
                f"global ref '{pref}' — globals are always available regardless of branch."
            )
            return errors

    for pref in pick_refs:
        bref = _parse_bound_to(pref)
        parent_ref = f"{bref.node_id}.{bref.param_name}" if bref else pref
        if parent_ref not in combined_refs:
            errors.append(
                f"Node '{node_id}' inputParam '{param_name}': pick ref '{pref}' "
                f"is not in the register or earlier in this batch."
            )
        elif receiving_ip is not None and receiving_ip.datatype == "list":
            # Check 7 for pick: source must also be list with compatible item kind
            src_meta = combined_refs[parent_ref]
            if src_meta.datatype != "list":
                errors.append(
                    f"Node '{node_id}' inputParam '{param_name}': pick ref '{pref}' "
                    f"has datatype '{src_meta.datatype}' but receiving param is 'list' — "
                    f"list→list binding required."
                )
            else:
                errors.extend(_check_list_binding_compat(
                    node_id, param_name, receiving_ip, src_meta, f"pick ref '{pref}'"
                ))

    pick_source_nodes = []
    for pref in pick_refs:
        if "." in pref:
            bref = _parse_bound_to(pref)
            pick_source_nodes.append(bref.node_id if bref else pref.split(".", 1)[0])
    path_sets = _path_index_path_sets(execution_path_index, node_id)
    if len(pick_refs) > 1 and not path_sets:
        errors.append(
            f"Node '{node_id}' inputParam '{param_name}': pick binding has "
            f"{len(pick_refs)} sources but no path-set data for coverage validation."
        )
        return errors
    if path_sets:
        for i, path_set in enumerate(path_sets):
            sources_in_path = [n for n in pick_source_nodes if n in path_set]
            if not sources_in_path:
                errors.append(
                    f"Node '{node_id}' inputParam '{param_name}': pick binding does not "
                    f"cover path {i + 1} (ancestors on this path: {sorted(path_set)})."
                )
            elif len(sources_in_path) > 1:
                errors.append(
                    f"Node '{node_id}' inputParam '{param_name}': pick sources "
                    f"{sources_in_path} can both execute on the same path — pick is not valid."
                )

    if not errors:
        _log_binding_audit(
            "pick_binding_valid",
            nodeId=node_id,
            param=param_name,
            pickRefs=pick_refs,
            sourceNodes=pick_source_nodes,
        )
    else:
        _log_binding_audit(
            "pick_binding_invalid",
            nodeId=node_id,
            param=param_name,
            pickRefs=pick_refs,
            errors=errors,
        )

    return errors


def _binding_refs_to_state_reads(bound_to: str, state_schema: StateSchema) -> List[str]:
    """Resolve boundTo (single ref or pick: list) to state field keys for stateReads."""
    if bound_to.startswith("pick:"):
        keys: List[str] = []
        for pref in _parse_pick_refs(bound_to):
            if pref.startswith("global."):
                continue
            sk = _binding_to_state_key(pref, state_schema)
            if sk:
                keys.append(sk)
        return keys
    if bound_to.startswith("global."):
        return []
    sk = _binding_to_state_key(bound_to, state_schema)
    return [sk] if sk else []


def _extract_binding_inner(template: str) -> str:
    """Extract inner 'node.param' / 'pick:...' / 'global.key' from '{{...}}'."""
    s = (template or "").strip()
    if s.startswith("{{") and s.endswith("}}"):
        return s[2:-2].strip()
    return s


def _summarize_schema_bindings(schema: NodeSchema) -> List[Dict[str, Any]]:
    """Compact per-node binding summary for audit logs."""
    rows: List[Dict[str, Any]] = []
    for ip in schema.inputParams:
        if ip.inputBindings:
            bound_to = ip.inputBindings[0].boundTo
            kind = "pick" if bound_to.startswith("pick:") else (
                "global" if bound_to.startswith("global.") else "node"
            )
        elif ip.required:
            bound_to = ""
            kind = "MISSING_REQUIRED"
        else:
            bound_to = ""
            kind = "optional_empty"
        rows.append({
            "param": ip.name,
            "required": ip.required,
            "kind": kind,
            "boundTo": bound_to or None,
        })
    return rows


def _summarize_graph_input_bindings(graph_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compact binding summary from assembled graph dict."""
    rows: List[Dict[str, Any]] = []
    for n in graph_dict.get("nodes") or []:
        nid = n.get("nodeId")
        cfg = n.get("config") or {}
        gen = cfg.get("generation") or {}
        required_names = {
            ip.get("name")
            for ip in (gen.get("inputs") or [])
            if ip.get("required") and ip.get("name")
        }
        bindings = cfg.get("inputBindings") or {}
        for pname, val in bindings.items():
            val_s = str(val or "")
            inner = _extract_binding_inner(val_s) if val_s else ""
            if inner.startswith("pick:"):
                kind = "pick"
            elif inner.startswith("global."):
                kind = "global"
            elif inner:
                kind = "node"
            elif pname in required_names:
                kind = "EMPTY_REQUIRED"
            else:
                kind = "optional_empty"
            rows.append({
                "nodeId": nid,
                "param": pname,
                "required": pname in required_names,
                "kind": kind,
                "binding": val_s or None,
            })
        for pname in required_names:
            if pname not in bindings:
                rows.append({
                    "nodeId": nid,
                    "param": pname,
                    "required": True,
                    "kind": "MISSING_REQUIRED",
                    "binding": None,
                })
    return rows


def _graph_node_ids(graph_dict: Dict[str, Any]) -> set:
    return {n.get("nodeId") for n in (graph_dict.get("nodes") or []) if n.get("nodeId")}


def _build_available_output_refs(state: "ScratchPlanningState") -> Dict[str, RefMeta]:
    """nodeId.paramName -> RefMeta from NodeSchemas + ALL global vars (primary+secondary)."""
    refs: Dict[str, RefMeta] = {}
    for s in (state.nodeSchemas or []):
        for op in (s.outputParams or []):
            if op.name:
                refs[f"{s.nodeId}.{op.name}"] = _ref_meta_from_obj(op)
    register = getattr(state, "availableVarsRegister", None)
    # Issue 8: namespaced global.<nodeId>.<field> plus unique bare aliases.
    merged: List[GlobalVar] = list(state.priorPhaseOutputs or [])
    if register is not None:
        seen = {g.name for g in merged}
        for g in _register_all_globals(register):
            if g.name and g.name not in seen:
                merged.append(g)
                seen.add(g.name)
    for ref_key, g in _global_ref_key_map(merged).items():
        refs.setdefault(ref_key, _ref_meta_from_obj(g))
    return refs


def _format_available_refs_for_repair(state: "ScratchPlanningState") -> str:
    """
    Format available binding refs for Stage 4b repair.

    Design: repair must see the FULL unfiltered global catalog with equal detail
    (name + type + description + source) — Stage 1.5 bias does not apply here.
    This is the safety net when Stage 2a preferred the wrong primary.
    """
    lines: List[str] = []
    for s in (state.nodeSchemas or []):
        for op in (s.outputParams or []):
            if op.name:
                lines.append(
                    f"  - {s.nodeId}.{op.name} : "
                    f"{_format_ref_type_of(op)}"
                )

    # Prefer priorPhaseOutputs (authoritative full set); fall back to register merge.
    all_globals: List[GlobalVar] = list(state.priorPhaseOutputs or [])
    if not all_globals:
        register = getattr(state, "availableVarsRegister", None)
        if register is not None:
            all_globals = _register_all_globals(register)

    if all_globals:
        lines.append(
            "  ALL PRIOR-PHASE GLOBALS (full catalog — choose the semantically correct one;"
            " do NOT prefer a 'primary' subset):"
        )
        for g in all_globals:
            desc = (g.description or "").strip()
            desc_part = f" — {desc}" if desc else ""
            lines.append(
                f"  - global.{g.name} : {_format_ref_type_of(g)} (source={g.source}){desc_part}"
            )
    return "\n".join(lines) if lines else "  (none)"


def _format_graph_topology_compact(graph_dict: Dict[str, Any], *, max_edges: int = 120) -> str:
    nodes = graph_dict.get("nodes") or []
    edges = graph_dict.get("edges") or []
    node_types: Dict[str, str] = {}
    for n in nodes:
        nid = n.get("nodeId")
        if not nid:
            continue
        node_types[nid] = n.get("type") or "unknown"

    lines: List[str] = []
    lines.append("NODES (nodeId:type):")
    for nid in sorted(node_types.keys()):
        lines.append(f"  - {nid}:{node_types[nid]}")

    lines.append("")
    lines.append("EDGES (from -> to [kind] condition?):")
    for i, e in enumerate(edges[:max_edges]):
        frm = e.get("fromNode")
        to = e.get("to")
        kind = e.get("kind")
        cond = e.get("condition")
        if cond:
            lines.append(f"  - {frm} -> {to} [{kind}] if {cond}")
        else:
            lines.append(f"  - {frm} -> {to} [{kind}]")
    if len(edges) > max_edges:
        lines.append(f"  ... ({len(edges) - max_edges} more edges truncated)")
    return "\n".join(lines)


def _format_pick_path_sets(state: "ScratchPlanningState", node_id: str) -> str:
    anc = _path_index_ancestors(state.executionPathIndex, node_id)
    psets = _path_index_path_sets(state.executionPathIndex, node_id)
    lines: List[str] = []
    lines.append(f"  guaranteed_ancestors(intersection): {sorted(anc)}")
    if not psets:
        lines.append("  path_sets: (none)")
    else:
        for i, ps in enumerate(psets, start=1):
            lines.append(f"  path_set[{i}]: {sorted(ps)}")
    return "\n".join(lines)


async def _repair_graph_bindings_with_llm_scratch(
    graph_dict: Dict[str, Any],
    state: "ScratchPlanningState",
    binding_errors: List[str],
    *,
    api_key: Optional[str] = None,
    attempt: int = 1,
) -> Dict[str, Any]:
    """
    Targeted Stage 4b repair: asks LLM only for binding fixes for required inputs.
    Does NOT regenerate skeleton/schemas/edges.
    """
    node_ids = _graph_node_ids(graph_dict)
    available_refs = _build_available_output_refs(state)

    # Compute allowed fix targets: only required inputs with missing/empty bindings
    allowed_targets: List[Tuple[str, str]] = []
    for n in (graph_dict.get("nodes") or []):
        nid = n.get("nodeId")
        cfg = n.get("config") or {}
        gen = cfg.get("generation") or {}
        inps = gen.get("inputs") or []
        bindings = cfg.get("inputBindings") or {}
        if not nid:
            continue
        for ip in inps:
            if not ip.get("required"):
                continue
            name = ip.get("name")
            if not name:
                continue
            val = bindings.get(name)
            if val is None or val == "":
                allowed_targets.append((nid, name))

    if not allowed_targets:
        _log_binding_audit(
            "stage4b_repair_skipped",
            phaseId=state.phaseId,
            reason="no_empty_required_targets",
        )
        return graph_dict

    _log_binding_audit(
        "stage4b_repair_start",
        phaseId=state.phaseId,
        attempt=attempt,
        targets=[{"nodeId": nid, "param": p} for nid, p in allowed_targets],
        errorCount=len(binding_errors),
        errors=binding_errors[:15],
        globalCatalog=[g.name for g in (state.priorPhaseOutputs or [])],
    )
    allowed_targets_text = "\n".join(
        f"  - nodeId='{nid}' paramName='{p}'" for nid, p in allowed_targets
    )
    available_refs_text = _format_available_refs_for_repair(state)

    topology_text = _format_graph_topology_compact(graph_dict)
    per_target_paths = "\n".join(
        f"- TARGET {nid}.{p}\n{_format_pick_path_sets(state, nid)}"
        for nid, p in allowed_targets
    )

    prompt = f"""You are fixing required inputBindings in a scratch-generated LangGraph phase graph.

PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

GRAPH TOPOLOGY (compact):
{topology_text}

FAN-IN PATH CONTEXT (for pick decisions):
{per_target_paths}

CURRENT BINDING ERRORS:
{chr(10).join('  - ' + e.replace('global.', 'system.') for e in binding_errors) if binding_errors else '  (none)'}

ALLOWED FIX TARGETS (you MUST ONLY output fixes for these exact pairs):
{allowed_targets_text}

AVAILABLE REFERENCES (ONLY these are valid):
{available_refs_text or '  (none)'}

STRICT RULES:
- Return ONLY JSON matching the response schema.
- Each correctedValue MUST be one of:
  1) '{{{{nodeId.param}}}}' where nodeId.param is listed under AVAILABLE REFERENCES
  2) '{{{{global.key}}}}' where global.key is listed under AVAILABLE REFERENCES
  3) '{{{{pick:nodeA.param,nodeB.param,...}}}}' where every nodeX.param is listed under AVAILABLE REFERENCES.
- pick: must NOT include any global.* entry.
- For pick:, choose sources that are mutually exclusive branches and cover all paths to the target.
- For global.* refs: copy the EXACT register key (usually global.<sourceNodeId>.<field>
  when two prior nodes emit the same field). Use the FULL catalog with equal weight —
  choose the semantically correct prior-phase output (e.g. edited/final over raw).
  There is no primary/secondary preference at repair time.
- Do NOT output any fixes for targets not listed above.
"""

    logger.info(
        f"[scratch/stage4b] Repair LLM attempt {attempt}: {len(allowed_targets)} binding target(s)."
    )

    resp = await async_generate(
        "gemini-3.5-flash-lite",
        prompt,
        config=GeminiGenerateConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=ScratchRepairResult,
        ),
        api_key=api_key,
    )
    repair = resp.parsed or ScratchRepairResult()
    if repair.bindingFixes:
        for _fix in repair.bindingFixes:
            if _fix.correctedValue and "system." in _fix.correctedValue:
                _fix.correctedValue = _fix.correctedValue.replace("system.", "global.")
    
    _log_binding_audit(
        "stage4b_repair_llm_response",
        phaseId=state.phaseId,
        attempt=attempt,
        fixCount=len(repair.bindingFixes or []),
        fixes=[
            {"nodeId": f.nodeId, "param": f.paramName, "value": f.correctedValue}
            for f in (repair.bindingFixes or [])
        ],
    )

    # Apply fixes with validation
    target_set = set(allowed_targets)
    for fix in (repair.bindingFixes or []):
        if (fix.nodeId, fix.paramName) not in target_set:
            logger.warning(
                f"[scratch/stage4b] bindingFix skipped (not allowed target): {fix.nodeId}.{fix.paramName}"
            )
            continue
        if fix.nodeId not in node_ids:
            logger.warning(
                f"[scratch/stage4b] bindingFix skipped (unknown nodeId): {fix.nodeId}"
            )
            continue

        inner = _extract_binding_inner(fix.correctedValue)
        if not inner:
            logger.warning(
                f"[scratch/stage4b] bindingFix skipped (empty correctedValue): {fix.nodeId}.{fix.paramName}"
            )
            continue

        if inner.startswith("pick:"):
            pick_errors = _validate_pick_binding(
                fix.nodeId,
                fix.paramName,
                inner,
                available_refs,
                state.executionPathIndex,
            )
            if pick_errors:
                logger.warning(
                    f"[scratch/stage4b] bindingFix skipped (invalid pick): {pick_errors}"
                )
                continue
        else:
            # single ref must exist and must be safe
            if inner.startswith("global."):
                if inner not in available_refs:
                    logger.warning(
                        f"[scratch/stage4b] bindingFix skipped (unknown global ref): {inner}"
                    )
                    continue
            else:
                # Index-aware: resolve indexed binding to parent ref for lookup
                bref = _parse_bound_to(inner)
                if bref is None:
                    logger.warning(
                        f"[scratch/stage4b] bindingFix skipped (malformed ref): {inner}"
                    )
                    continue
                parent_ref = f"{bref.node_id}.{bref.param_name}"
                if parent_ref not in available_refs:
                    logger.warning(
                        f"[scratch/stage4b] bindingFix skipped (unknown ref): {inner} "
                        f"(parent ref '{parent_ref}' not in available_refs)"
                    )
                    continue
                # For indexed bindings, also validate index/field against itemSchema
                if bref.index is not None and bref.item_field:
                    src_meta = available_refs[parent_ref]
                    if src_meta.datatype != "list" or not src_meta.itemSchema:
                        logger.warning(
                            f"[scratch/stage4b] bindingFix skipped (indexed ref on non-list): {inner}"
                        )
                        continue
                    field_names = {f.name for f in src_meta.itemSchema}
                    if bref.item_field not in field_names:
                        logger.warning(
                            f"[scratch/stage4b] bindingFix skipped (field '{bref.item_field}' "
                            f"not in itemSchema of '{parent_ref}'): {inner}"
                        )
                        continue
                src_node = bref.node_id
                safe_anc = _path_index_ancestors(state.executionPathIndex, fix.nodeId)
                if src_node not in safe_anc:
                    logger.warning(
                        f"[scratch/stage4b] bindingFix skipped (not guaranteed ancestor): {inner} for {fix.nodeId}.{fix.paramName}"
                    )
                    continue

        # Update graph node config + stateReads
        for n in (graph_dict.get("nodes") or []):
            if n.get("nodeId") != fix.nodeId:
                continue
            cfg = n.get("config") or {}
            cfg.setdefault("inputBindings", {})
            cfg["inputBindings"][fix.paramName] = "{{" + inner + "}}"
            # Update stateReads for non-global bindings
            sr = cfg.get("stateReads") or []
            for k in _binding_refs_to_state_reads(inner, state.stateSchema):
                if k and k not in sr:
                    sr.append(k)
            cfg["stateReads"] = sr
            n["config"] = cfg
            _log_binding_audit(
                "stage4b_repair_applied",
                phaseId=state.phaseId,
                attempt=attempt,
                nodeId=fix.nodeId,
                param=fix.paramName,
                correctedValue=fix.correctedValue,
                bindingKind=(
                    "pick" if inner.startswith("pick:")
                    else "global" if inner.startswith("global.")
                    else "node"
                ),
            )
            break

    return graph_dict


def _build_execution_path_index(skeleton: SkeletonPlan) -> Dict[str, Any]:
    """
    Stage 2a Step 2: Build the Execution Path Index (pure code, no LLM).

    For each eligible node (non-router, non-end), computes the set of eligible
    ancestor node IDs that lie on ALL unbroken execution paths from entry to
    that node.

    Branch handling:
      - At a router, each outgoing branch is walked independently.
      - Nodes on the approve branch do NOT include nodes from the reject branch.
      - Fan-in nodes (multiple incoming paths) get the INTERSECTION of all
        incoming ancestor sets — only ancestors guaranteed to have executed.

    This index is used by Stage 2b to reject cross-branch inputBindings at
    compile time: a binding `nodeA.param → nodeB` is valid only if
    `nodeA ∈ execution_path_index[nodeB]`.

    Back-edges (retry loops) are detected and skipped to prevent cycles.
    """
    node_map = {n.nodeId: n for n in skeleton.nodes}

    # Build full adjacency
    full_adj: Dict[str, List[str]] = defaultdict(list)
    for e in skeleton.edges:
        full_adj[e.fromNode].append(e.to)

    # Detect back-edges via DFS colouring
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n.nodeId: WHITE for n in skeleton.nodes}
    back_edges: set = set()

    def _dfs_colour(node: str) -> None:
        color[node] = GRAY
        for nxt in full_adj.get(node, []):
            if color.get(nxt) == GRAY:
                back_edges.add((node, nxt))
            elif color.get(nxt) == WHITE:
                _dfs_colour(nxt)
        color[node] = BLACK

    import sys
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 5000))
    try:
        for nid in list(node_map.keys()):
            if color[nid] == WHITE:
                _dfs_colour(nid)
    finally:
        sys.setrecursionlimit(old_limit)

    # Forward DAG (back-edges removed)
    fwd_adj: Dict[str, List[str]] = defaultdict(list)
    for e in skeleton.edges:
        if (e.fromNode, e.to) not in back_edges:
            fwd_adj[e.fromNode].append(e.to)

    # Compute in-degrees for Kahn's topological traversal on the DAG
    in_deg: Dict[str, int] = {n.nodeId: 0 for n in skeleton.nodes}
    for from_n, to_list in fwd_adj.items():
        for to_n in to_list:
            in_deg[to_n] = in_deg.get(to_n, 0) + 1

    # ancestors[nodeId] = set of eligible ancestor nodeIds | None (not yet initialised)
    # None means "no path has reached this node yet".
    ancestors: Dict[str, Optional[set]] = {n.nodeId: None for n in skeleton.nodes}
    ancestors[skeleton.entryNodeId] = set()
    path_sets: Dict[str, List[set]] = defaultdict(list)

    queue: deque = deque([nid for nid, deg in in_deg.items() if deg == 0])

    while queue:
        cur = queue.popleft()
        cur_node = node_map.get(cur)
        is_eligible = cur_node is not None and cur_node.type not in ("router", "end")

        cur_anc = ancestors[cur] if ancestors[cur] is not None else set()

        for nxt in fwd_adj.get(cur, []):
            # What ancestors does cur contribute to nxt?
            contribution: set = set(cur_anc)
            if is_eligible:
                contribution.add(cur)

            path_sets[nxt].append(contribution)

            if ancestors[nxt] is None:
                # First path to reach nxt
                ancestors[nxt] = contribution
            else:
                # Fan-in: intersection — only keep ancestors present on ALL paths
                ancestors[nxt] = ancestors[nxt].intersection(contribution)

            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)

    # Return index for eligible nodes only
    ancestors_result: Dict[str, Any] = {}
    path_sets_result: Dict[str, List[set]] = {}
    for nid, anc_set in ancestors.items():
        node = node_map.get(nid)
        if node and node.type not in ("router", "end"):
            ancestors_result[nid] = anc_set if anc_set is not None else set()
            if path_sets.get(nid):
                path_sets_result[nid] = path_sets[nid]

    if back_edges:
        logger.info(
            f"[scratch/stage2a] ExecutionPathIndex: skipped {len(back_edges)} "
            f"back-edge(s) for DAG traversal."
        )
    if ancestors_result:
        anc_sizes = [len(v) for v in ancestors_result.values()]
        logger.debug(
            f"[scratch/stage2a] ExecutionPathIndex ancestor counts: "
            f"min={min(anc_sizes)} max={max(anc_sizes)} "
            f"avg={sum(anc_sizes) / len(anc_sizes):.1f}"
        )
    return {"ancestors": ancestors_result, "path_sets": path_sets_result}


def _build_global_register_text(register: AvailableVarsRegister) -> str:
    """
    Phase-stable primary/secondary global register (no nodeOutputs).
    Safe to embed in the Stage 2a context cache — identical across batches.
    PRIMARY: name + type + description + source.
    SECONDARY: name + type only (descriptions via targeted follow-up if needed).
    """
    lines: List[str] = []

    if register.globals:
        lines.append(
            "PRIMARY GLOBAL INPUTS (prefer these — prior-phase outputs selected for this phase):"
        )
        lines.append(
            '  Reference with the \'global.\' prefix: {"boundTo": "global.<name>"} '
            "copying <name> EXACTLY from the list below (usually <sourceNodeId>.<field>)."
        )
        for g in register.globals:
            desc = (g.description or "").strip()
            desc_part = f" — {desc}" if desc else ""
            lines.append(
                f"  - global.{g.name} : {_format_ref_type_of(g)}  "
                f"(source node: {g.source}){desc_part}"
            )

    if register.secondaryGlobals:
        lines.append(
            "SECONDARY GLOBAL INPUTS (compact fallback — use ONLY if no PRIMARY global fits):"
        )
        lines.append(
            "  Format: name + type only. Prefer PRIMARY above whenever possible. "
            "If you need a secondary entry's description, it will be supplied via a "
            "targeted detail lookup on retry — do not invent descriptions."
        )
        for g in register.secondaryGlobals:
            lines.append(f"  - global.{g.name} : {_format_ref_type_of(g)}")

    if register.globals or register.secondaryGlobals:
        lines.append(
            "BINDING PREFERENCE: bind to PRIMARY globals first. Consult SECONDARY only "
            "when a required input has no suitable PRIMARY candidate."
        )

    return "\n".join(lines) if lines else "(none)"


def _build_node_outputs_register_text(register: AvailableVarsRegister) -> str:
    """Growing per-batch nodeOutputs block — must stay in the dynamic prompt, not cache."""
    if not register.nodeOutputs:
        return ""
    lines: List[str] = ["NODE OUTPUTS (already validated in prior batches):"]
    for entry in register.nodeOutputs:
        for o in entry.outputs:
            type_str = _format_ref_type_of(o)
            lines.append(
                f"  - {entry.nodeId}.{o['name']} : {type_str}  "
                f"(produced by {entry.nodeId}, batch {entry.batch})"
            )
    return "\n".join(lines)


_COLLECTION_MATCH_CACHE: Dict[str, re.Pattern] = {}


def _collection_match_pattern(coll_name: str) -> re.Pattern:
    """Word-boundary regex for a collection name, cached per name.

    Word boundaries matter: substring matching would false-positive match
    e.g. "voice" inside "invoices", or a collection named "order" against
    text like "in order to...". \\b avoids both.
    """
    pattern = _COLLECTION_MATCH_CACHE.get(coll_name)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(coll_name.lower()) + r"\b")
        _COLLECTION_MATCH_CACHE[coll_name] = pattern
    return pattern


def _match_enterprise_graph_collections(nodes_in_batch, all_collections: List[str]) -> List[str]:
    """
    Word-boundary match node name/description text against known Enterprise
    Graph collection names. Nodes don't carry an explicit collection
    reference, so this is a low-risk substitute per Technical Design 5.4
    rather than a node-model change (see: collection is matched, not
    declared).
    """
    matched: List[str] = []
    for coll_name in all_collections:
        pattern = _collection_match_pattern(coll_name)
        for n in nodes_in_batch:
            haystack = f"{n.name} {n.description}".lower()
            if pattern.search(haystack):
                matched.append(coll_name)
                break
    return matched


async def _build_enterprise_graph_schema_section(
    nodes_in_batch, subscriber_id: Optional[str], org_id: Optional[str]
) -> str:
    if not subscriber_id or not org_id:
        logger.warning(
            "Enterprise Graph schema lookup skipped: missing subscriber_id/org_id on state"
        )
        return ""

    global _ARCADEDB_UNAVAILABLE_UNTIL
    now = time.monotonic()
    if _ARCADEDB_UNAVAILABLE_UNTIL is not None and now < _ARCADEDB_UNAVAILABLE_UNTIL:
        logger.info(
            "Enterprise Graph schema lookup skipped: ArcadeDB marked unavailable "
            "(cooldown active) — continuing without it"
        )
        return ""

    try:
        repo = EnterpriseGraphRepository()
        all_collections = await asyncio.wait_for(
            repo.list_all_collections(subscriber_id, org_id), timeout=3.0
        )
        if not all_collections:
            return ""

        matched = _match_enterprise_graph_collections(nodes_in_batch, all_collections)
        
        if not matched:
            return ""

        schemas = await asyncio.wait_for(
            repo.get_schemas_for_collections(subscriber_id, org_id, matched), timeout=3.0
        )
        if not schemas:
            return ""

        blocks: List[str] = []
        for coll_name, fields in schemas.items():
            field_lines = "\n".join(
                f"  - {f['name']} : {f['type']}" for f in fields
            )
            blocks.append(f"Collection: {coll_name}\n{field_lines}")

        if not blocks:
            return ""

        return (
            "\n\n═══════════════════════════════════════════════════════\n"
            "ENTERPRISE GRAPH SCHEMA (use these exact field names)\n"
            "═══════════════════════════════════════════════════════\n"
            + "\n\n".join(blocks)
            + "\n"
        )
    except asyncio.TimeoutError:
        _ARCADEDB_UNAVAILABLE_UNTIL = time.monotonic() + _ARCADEDB_COOLDOWN_SECONDS
        logger.warning(
            "Enterprise Graph schema lookup timed out (>3s) — marking ArcadeDB "
            "unavailable for %ss, continuing without it",
            _ARCADEDB_COOLDOWN_SECONDS,
        )
        return ""
    except Exception:
        _ARCADEDB_UNAVAILABLE_UNTIL = time.monotonic() + _ARCADEDB_COOLDOWN_SECONDS
        logger.exception(
            "Enterprise Graph schema lookup failed — marking ArcadeDB unavailable "
            "for %ss, continuing without it",
            _ARCADEDB_COOLDOWN_SECONDS,
        )
        return ""

def _build_register_text(register: AvailableVarsRegister) -> str:
    """Full AvailableVarsRegister text for uncached / full prompts."""
    parts = [
        _build_global_register_text(register),
        _build_node_outputs_register_text(register),
    ]
    text = "\n".join(p for p in parts if p)
    return text if text else "(none)"


def _register_all_globals(register: AvailableVarsRegister) -> List[GlobalVar]:
    """Primary + secondary globals for validation / repair (full set)."""
    by_name: Dict[str, GlobalVar] = {}
    for g in list(register.globals or []) + list(register.secondaryGlobals or []):
        if not g.name:
            continue
        by_name.setdefault(g.name, g)
    return list(by_name.values())


def _format_secondary_detail_block(globals_list: List[GlobalVar]) -> str:
    """Full detail block for secondary globals selected by a targeted lookup."""
    if not globals_list:
        return ""
    lines = [
        "SECONDARY GLOBAL DETAIL LOOKUP (full descriptions for candidates that may be needed):",
        "  Use these when no PRIMARY global fits. Still bind as global.<name> "
        "copying the listed name EXACTLY (usually <sourceNodeId>.<field>).",
    ]
    for g in globals_list:
        desc = (g.description or "").strip()
        desc_part = f" — {desc}" if desc else ""
        lines.append(
            f"  - global.{g.name} : {_format_ref_type_of(g)}  "
            f"(source node: {g.source}){desc_part}"
        )
    return "\n".join(lines)


def _item_field_name(f: Any) -> str:
    """Get the name from an itemSchema entry (NodeParam or serialized dict)."""
    return f.name if isinstance(f, NodeParam) else (f.get("name") or "?")


def _item_field_datatype(f: Any) -> str:
    """Get the datatype from an itemSchema entry (NodeParam or serialized dict)."""
    if isinstance(f, NodeParam):
        return f.datatype
    if isinstance(f, dict):
        return str(f.get("datatype") or f.get("dataType") or f.get("type") or "?")
    return str(getattr(f, "datatype", None) or getattr(f, "dataType", None) or "?")


def _format_ref_type(
    datatype: str,
    item_schema: Optional[Any] = None,
    *,
    item_type: Optional[str] = None,
    scalar_item_datatype: Optional[str] = None,
    enum_values: Optional[List[str]] = None,
) -> str:
    """
    Render a ref's type as a compact, LLM-readable string.

    Plain scalars        → "string" / "boolean" / "number"
    Scalar list          → "list<string>"
    Enum list            → "list<enum[a,b]>"
    Record list          → "list<field1:type1, field2:type2, ...>"
    Untyped list         → "list"
    """
    if datatype != "list":
        return datatype
    effective = item_type or ("record" if item_schema else None)
    if effective == "scalar":
        return f"list<{scalar_item_datatype or 'string'}>"
    if effective == "enum":
        vals = ",".join(str(v) for v in (enum_values or []) if v is not None)
        return f"list<enum[{vals}]>" if vals else "list<enum>"
    if item_schema:
        fields_str = ", ".join(
            f"{_item_field_name(f)}:{_item_field_datatype(f)}" for f in item_schema
        )
        return f"list<{fields_str}>"
    return "list"


def _format_ref_type_of(obj: Any, datatype: Optional[str] = None) -> str:
    """Format a NodeParam, GlobalVar, RefMeta, or serialized dump dict."""
    dt = datatype or _obj_get(obj, "datatype", "dataType", default="")
    return _format_ref_type(
        str(dt),
        _obj_get(obj, "itemSchema"),
        item_type=_obj_get(obj, "itemType"),
        scalar_item_datatype=_obj_get(obj, "scalarItemDatatype"),
        enum_values=_obj_get(obj, "enumValues"),
    )


def _build_static_node_schema_cache_text(
    register: AvailableVarsRegister,
    state: ScratchPlanningState,
) -> str:
    """
    Static / phase-constant portion of Stage 2a prompts (identical across batches).
    Includes system rules + primary/secondary globals + workflow registry.
    Excludes growing nodeOutputs (those stay in the dynamic prompt).
    Cached so we pay full price once per phase, then cached rate for later batches.
    """
    global_register_text = _build_global_register_text(register)
    registry_text = _format_registry_for_prompt(state.workflowRegistry)
    return (
        f"{_NODE_SCHEMA_SYSTEM}\n\n"
        f"PHASE: {state.phaseName}\n"
        f"GOAL : {state.phaseDescription}\n\n"
        f"AVAILABLE VARIABLES REGISTER — GLOBALS (stable for this phase):\n"
        f"{global_register_text}\n"
        f"{registry_text}"
    )


_NODE_SCHEMA_INTRO = "You are designing the input/output parameters for workflow nodes."

_NODE_SCHEMA_RULES = """
═══════════════════════════════════════════════════════
DATA TYPE RULES — CRITICAL
═══════════════════════════════════════════════════════
A. Bare "object" / "array" datatypes and JSON-encoded blob strings are FORBIDDEN.
   Allowed datatypes: string, boolean, number, list ONLY.
   Scalars (string / boolean / number): every individual piece of data is its own param.
   Collections: use datatype "list" and set exactly one item kind:
     • itemType "scalar" + scalarItemDatatype ("string"|"boolean"|"number")
       for arrays of primitives. Example: required_skills: ["Python","Java"].
       NEVER wrap a primitive array as a one-field record ([{skill: string}] is WRONG).
     • itemType "record" + populated itemSchema — one entry per field of a single
       record. ItemSchema entries must each be scalar or a depth-1 list and must
       NOT carry inputBindings.
     • itemType "enum" + enumValues for a closed set of strings.

   WRONG:  {"name": "aadhaar_data", "datatype": "object"}
   CORRECT: separate scalar params:
     {"name": "aadhaar_number",  "datatype": "string"}
     {"name": "aadhaar_name",    "datatype": "string"}
     {"name": "aadhaar_dob",     "datatype": "string"}
     {"name": "aadhaar_address", "datatype": "string"}

   WRONG:  {"name": "user_documents", "datatype": "array"}
   CORRECT: {"name": "aadhaar_image_base64", "datatype": "string"},
            {"name": "pan_image_base64", "datatype": "string"}

   WRONG:  {"name": "required_skills", "datatype": "list",
            "itemSchema": [{"name": "skill", "datatype": "string", ...}]}
   CORRECT (scalar array):
            {"name": "required_skills", "datatype": "list", "itemType": "scalar",
             "scalarItemDatatype": "string",
             "description": "Required skill names as plain strings."}

A1. CARDINALITY THRESHOLD:
    - Fixed count ≤ 5 at design time → FLAT indexed scalar params ("<item>_<index>_<field>").
    - Fixed count > 5 OR dynamic/unknown count → datatype "list" with the correct itemType.
    A "string" param that holds a JSON-encoded array/object is the SAME violation as bare "array"
    — datatype "string" does not make it flat.
    WRONG:   {"name": "mcq_list_json", "datatype": "string",
              "description": "stringified JSON array of question objects..."}
    CORRECT (dynamic records — use list + itemType record + itemSchema):
             {"name": "mcq_questions", "datatype": "list", "itemType": "record",
              "required": true,
              "description": "Generated MCQ records; count is dynamic.",
              "itemSchema": [
                {"name": "question_text", "datatype": "string", "required": true, "description": "The question."},
                {"name": "option_a",      "datatype": "string", "required": true, "description": "Option A."},
                {"name": "option_b",      "datatype": "string", "required": true, "description": "Option B."},
                {"name": "option_c",      "datatype": "string", "required": true, "description": "Option C."},
                {"name": "option_d",      "datatype": "string", "required": true, "description": "Option D."},
                {"name": "correct_option","datatype": "string", "required": true, "description": "One of a/b/c/d."}
              ]}
    CORRECT (fixed 3 items — use flat):
             {"name": "line_item_1_name", "datatype": "string"}, ... (3 × fields)

B. For file uploads (images, PDFs, scanned docs): use datatype "string" and name it
   "<doc>_image_base64" or "<doc>_file_url". Do NOT use object/array for files.

C. For flags / status values: use "string" with the exact possible values listed in
   the description field. Use "boolean" only for true/false switches.

═══════════════════════════════════════════════════════
COMPLETENESS RULES — CRITICAL
═══════════════════════════════════════════════════════
D. Every entity, field, attribute, or piece of data mentioned in the node description
   MUST appear as an explicit param. Do not omit any mentioned field.
   Read the node description carefully and include ALL named fields.

E. For tool nodes that compare, verify, or match values: the param being compared FROM
   must be an inputParam with a binding. Do not skip comparison inputs.
   Example: if description says "match extracted_name with user_name", then
   "user_name" must be an inputParam bound to the node that collected it — not omitted.

F. outputParams must list every value the node produces. If a tool does OCR and produces
   extracted text fields, list each field separately.

G. outputParams must state the exact possible values in the description field when the
   param is a status/flag. Example: description "verification result: 'success' or 'failure'"
   This is used by the router in Stage 3 to write accurate conditions.

H. PERSISTENCE PARAM NAMES — when the node description names a WORKFLOW PERSISTENCE
   REGISTRY collection:
   Prefer the exact Mongo field name as the param name .
   If you must use a different param name, the NODE description (already given) must
   already contain: MongoDB field mapping for collection '<coll>': <param> → <mongo_field>.
   Do not invent a rename that is not mapped there. Do not map one param to two
   similar registry fields (status vs current_status vs pipeline_status).
   The param's own description must cite the Mongo field, e.g.
   "Ticket lifecycle; MongoDB ticket_info.status".

I. LIST PARAM DESCRIPTION — if an inputParam or outputParam is datatype "list", the description
   MUST state, in prose, what happens per item (the iteration/transform logic), e.g.:
   "For each item, compare user_answer to correct_option and emit is_correct."
   Do NOT re-flatten the list into indexed fields to describe this logic.

═══════════════════════════════════════════════════════
BINDING RULES
═══════════════════════════════════════════════════════
1. Use the exact nodeId and name from the YOUR TASK node list — do NOT add or remove nodes.
2. inputBindings reference: (a) outputParams of nodes EARLIER in this same batch list, OR
   (b) entries in the Available Variables Register (node outputs AND global inputs).
   Intra-phase node binding format:   {"boundTo": "nodeId.paramName"}
   Cross-phase GLOBAL input format:   {"boundTo": "global.<name>"}
   ← Prefer PRIMARY globals; use SECONDARY only if PRIMARY has no suitable candidate.
     ANY global (primary or secondary) MUST be bound using the EXACT register key.
     Prior-phase outputs are namespaced: if the register lists
     "global.collect_basic_details.full_name", bind
     {"boundTo": "global.collect_basic_details.full_name"}.
     Do NOT drop the source nodeId when the register includes it.
     Do NOT bind a prior-phase output as a same-phase node ref
     (e.g. NOT "collect_basic_details.full_name" without the global. prefix) —
     that node is not in this phase.
     Bare "global.<field>" is valid ONLY when the register lists that exact key
     (the field name is unique across prior-phase nodes).
     When PRIMARY and SECONDARY both have plausible candidates for the same semantic need,
     prefer the PRIMARY one (it was selected as more likely for this phase).
2a. LIST BINDINGS: a list-typed inputParam may bind to a list-typed output of the SAME
    item kind. Scalar lists bind to scalar lists with matching scalarItemDatatype.
    Record lists bind to a record list whose itemSchema is a superset of the receiver's
    declared fields. A scalar inputParam may bind to a single literal-indexed item field
    of a *record* list output:
    {"boundTo": "generate_mcqs.mcq_questions[0].question_text"}
    Indexed field syntax is NOT valid for scalar/enum lists. A scalar param CANNOT bind
    to a whole list.
3. Do NOT reference nodes from later batches (no forward references).
4. Cross-branch binding is FORBIDDEN: nodes on separate branches of the same router
   must not bind to each other's outputs. Only bind to outputs guaranteed to have
   executed on every possible path to this node.
4a. Fan-in `pick` binding. When a required input has more than one legal producer on
   mutually exclusive branches of the same router upstream, bind with a single pick
   entry (do NOT emit multiple inputBindings list entries):
   {"boundTo": "pick:upload_files_screen.uploaded_files_json,fetch_invoices_tool.uploaded_files_json"}
   Use only when branches are mutually exclusive. If unsure and the param is optional,
   leave inputBindings: [] per Rule 6. If the param is required, you must bind it
   (single source or pick:) — never leave a required param unbound.
   pick sources must all be nodeId.paramName refs from this phase — never include
   global.* inside a pick list.
5. outputParams must NOT have inputBindings — outputs are produced, not consumed.
6. Required inputParams (tool or ui_screen) MUST have a binding to an upstream
   node output or global.* — never leave inputBindings: [] on a required param.
   Only optional inputParams may have inputBindings: [] (runtime default / absent).
7. All names must be snake_case.

Output JSON matching this schema exactly — no markdown, no explanation:
{
  "nodes": [
    {
      "nodeId": "example_node",
      "inputParams": [
        {
          "name": "param_name",
          "datatype": "string | boolean | number",
          "required": true,
          "description": "One-line description. For status params: include all possible values.",
          "inputBindings": [{"boundTo": "source_node.param_name"}]
        },
        {
          "name": "list_param_example",
          "datatype": "list",
          "itemType": "record",
          "required": true,
          "description": "For each item, <describe what this node does per record>.",
          "inputBindings": [{"boundTo": "source_node.list_param"}],
          "itemSchema": [
            {"name": "field_one", "datatype": "string",  "required": true, "description": "Meaning of field_one."},
            {"name": "field_two", "datatype": "number",  "required": true, "description": "Meaning of field_two."},
            {"name": "flag_field","datatype": "boolean", "required": false,"description": "Optional flag."}
          ]
        },
        {
          "name": "required_skills",
          "datatype": "list",
          "itemType": "scalar",
          "scalarItemDatatype": "string",
          "required": true,
          "description": "Plain skill-name strings, not objects.",
          "inputBindings": [{"boundTo": "source_node.required_skills"}]
        }
      ],
      "outputParams": [
        {
          "name": "output_name",
          "datatype": "string | boolean | number",
          "required": true,
          "description": "One-line description. For status params: list exact possible values e.g. 'success' or 'failure'."
        },
        {
          "name": "output_list_example",
          "datatype": "list",
          "itemType": "record",
          "required": true,
          "description": "Produced list; for each item <describe what each record contains>.",
          "itemSchema": [
            {"name": "result_field", "datatype": "string", "required": true, "description": "..."}
          ]
        }
      ]
    }
  ]
}""".strip()

_NODE_SCHEMA_SYSTEM = f"{_NODE_SCHEMA_INTRO}\n\n{_NODE_SCHEMA_RULES}"


async def _build_node_schema_user_prompt(
    batch: NodeBatch,
    skeleton: SkeletonPlan,
    register: AvailableVarsRegister,
    state: ScratchPlanningState,
    execution_path_index: Optional[Dict[str, Any]] = None,
) -> str:
    """Dynamic per-batch portion of the Stage 2a node schema builder prompt."""
    node_map = {n.nodeId: n for n in skeleton.nodes}
    nodes_in_batch = [node_map[nid] for nid in batch.nodeIds if nid in node_map]
    nodes_text = "\n".join(
        f"  nodeId: {n.nodeId}\n"
        f"  name:   {n.name}\n"
        f"  type:   {n.type}\n"
        f"  description: {n.description}\n"
        for n in nodes_in_batch
    )
    register_text = _build_register_text(register)
    registry_text = _format_registry_for_prompt(state.workflowRegistry)

    allowed_binding_section = ""
    if execution_path_index is not None and nodes_in_batch:
        outputs_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for entry in register.nodeOutputs:
            for o in entry.outputs:
                outputs_by_node[entry.nodeId].append(o)

        primary_g_lines: List[str] = []
        for g in register.globals:
            desc = (g.description or "").strip()
            desc_part = f" — {desc}" if desc else ""
            primary_g_lines.append(
                f"  - global.{g.name} : {_format_ref_type_of(g)} (source={g.source}){desc_part}"
            )
        secondary_g_lines: List[str] = []
        for g in register.secondaryGlobals:
            secondary_g_lines.append(f"  - global.{g.name} : {_format_ref_type_of(g)}")

        per_node_blocks: List[str] = []
        for n in nodes_in_batch:
            anc = _path_index_ancestors(execution_path_index, n.nodeId)
            allowed_nodes = [nid for nid in sorted(anc) if nid in outputs_by_node]
            ref_lines: List[str] = []
            for nid in allowed_nodes:
                for o in outputs_by_node.get(nid, []):
                    type_str = _format_ref_type_of(o)
                    ref_lines.append(f"  - {nid}.{o['name']} : {type_str}")
            refs_text = "\n".join(ref_lines) if ref_lines else "  (none)"
            per_node_blocks.append(
                f"NODE: {n.nodeId}\n"
                f"Guaranteed ancestor nodes (safe to bind from): {allowed_nodes or []}\n"
                f"Allowed binding refs for this node (SAFE ONLY):\n{refs_text}"
            )

        allowed_binding_section = (
            "\n\n═══════════════════════════════════════════════════════\n"
            "PER-NODE ALLOWED BINDING SOURCES (ExecutionPathIndex)\n"
            "═══════════════════════════════════════════════════════\n"
            "For single-source bindings you MUST ONLY reference refs listed under\n"
            "\"Allowed binding refs for this node\" below (intersection ancestors).\n"
            "For fan-in inputs where producers sit on mutually exclusive branches,\n"
            "use Rule 4a pick: syntax instead — pick sources may appear here even\n"
            "when they are NOT listed individually (branch-exclusive producers).\n"
            "If a single-source ref is not listed, it is NOT guaranteed on all paths.\n"
            "Do NOT use a single-source binding to it. Use pick: or omit per Rule 6.\n"
            "\n"
            "PRIMARY GLOBAL refs (prefer these):\n"
            f"{(chr(10).join(primary_g_lines) if primary_g_lines else '  (none)')}\n"
            "\n"
            "SECONDARY GLOBAL refs (fallback only — use if PRIMARY has no fit):\n"
            f"{(chr(10).join(secondary_g_lines) if secondary_g_lines else '  (none)')}\n"
            "\n"
            + "\n\n".join(per_node_blocks)
            + "\n"
        )

    # Dynamic portion: prefer omitting the static register when using cache.
    # Callers that need the full uncached prompt still receive register_text via
    # _build_node_schema_prompt.
    enterprise_graph_section = await _build_enterprise_graph_schema_section(
        nodes_in_batch, state.subscriberId, state.orgId
    )

    return f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

AVAILABLE VARIABLES REGISTER (you may bind inputParams to these):
{register_text}
{registry_text}
{allowed_binding_section}
{enterprise_graph_section}
YOUR TASK — Define inputParams and outputParams for each of the following nodes:
(Nodes are listed in dependency order. Later nodes in this list may bind to outputs
of earlier nodes in THIS list using "nodeId.paramName" dot notation.)

{nodes_text}""" + _GLOBAL_VARS_INSTRUCTION


async def _build_node_schema_dynamic_user_prompt(
    batch: NodeBatch,
    skeleton: SkeletonPlan,
    register: AvailableVarsRegister,
    state: ScratchPlanningState,
    execution_path_index: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Dynamic-only portion for Stage 2a when the static register block is cached.

    Omits phase globals (PRIMARY/SECONDARY) — those live in the cached system
    context and must not be re-sent per batch. Includes only batch-local content:
    growing intra-phase nodeOutputs + path-index per-node refs + task nodes.
    """
    node_map = {n.nodeId: n for n in skeleton.nodes}
    nodes_in_batch = [node_map[nid] for nid in batch.nodeIds if nid in node_map]
    nodes_text = "\n".join(
        f"  nodeId: {n.nodeId}\n"
        f"  name:   {n.name}\n"
        f"  type:   {n.type}\n"
        f"  description: {n.description}\n"
        for n in nodes_in_batch
    )

    # Growing nodeOutputs are batch-dependent — keep in dynamic prompt.
    node_out_lines: List[str] = []
    if register.nodeOutputs:
        node_out_lines.append("NODE OUTPUTS (already validated in prior batches):")
        for entry in register.nodeOutputs:
            for o in entry.outputs:
                type_str = _format_ref_type_of(o)
                node_out_lines.append(
                    f"  - {entry.nodeId}.{o['name']} : {type_str}  "
                    f"(produced by {entry.nodeId}, batch {entry.batch})"
                )
    node_out_text = "\n".join(node_out_lines) if node_out_lines else "(none yet)"

    allowed_binding_section = ""
    if execution_path_index is not None and nodes_in_batch:
        outputs_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for entry in register.nodeOutputs:
            for o in entry.outputs:
                outputs_by_node[entry.nodeId].append(o)

        per_node_blocks: List[str] = []
        for n in nodes_in_batch:
            anc = _path_index_ancestors(execution_path_index, n.nodeId)
            allowed_nodes = [nid for nid in sorted(anc) if nid in outputs_by_node]
            ref_lines: List[str] = []
            for nid in allowed_nodes:
                for o in outputs_by_node.get(nid, []):
                    type_str = _format_ref_type_of(o)
                    ref_lines.append(f"  - {nid}.{o['name']} : {type_str}")
            refs_text = "\n".join(ref_lines) if ref_lines else "  (none)"
            per_node_blocks.append(
                f"NODE: {n.nodeId}\n"
                f"Guaranteed ancestor nodes (safe to bind from): {allowed_nodes or []}\n"
                f"Allowed binding refs for this node (SAFE ONLY):\n{refs_text}"
            )

        allowed_binding_section = (
            "\n═══════════════════════════════════════════════════════\n"
            "PER-NODE ALLOWED BINDING SOURCES (ExecutionPathIndex)\n"
            "═══════════════════════════════════════════════════════\n"
            "Cross-phase global.* bindings: use the cached AVAILABLE VARIABLES REGISTER — "
            "GLOBALS block (PRIMARY prefer; SECONDARY fallback only). Do NOT expect "
            "globals repeated here — bind as global.<name> from that register, "
            "copying the listed name EXACTLY (usually <sourceNodeId>.<field>).\n"
            "Below lists intra-phase node refs only (these grow each batch).\n"
            "\n"
            + "\n\n".join(per_node_blocks)
            + "\n"
        )

    enterprise_graph_section = await _build_enterprise_graph_schema_section(
        nodes_in_batch, state.subscriberId, state.orgId
    )

    return f"""NODE OUTPUTS available from earlier batches in this phase:
{node_out_text}
{allowed_binding_section}
{enterprise_graph_section}
YOUR TASK — Define inputParams and outputParams for each of the following nodes:
(Nodes are listed in dependency order. Later nodes in this list may bind to outputs
of earlier nodes in THIS list using "nodeId.paramName" dot notation.)

{nodes_text}""" + _GLOBAL_VARS_INSTRUCTION


async def _build_node_schema_prompt(
    batch: NodeBatch,
    skeleton: SkeletonPlan,
    register: AvailableVarsRegister,
    state: ScratchPlanningState,
    execution_path_index: Optional[Dict[str, Any]] = None,
) -> str:
    """Full Stage 2a prompt in original section order (uncached fallback)."""
    user_part = await _build_node_schema_user_prompt(
        batch, skeleton, register, state, execution_path_index
    )
    return f"{_NODE_SCHEMA_INTRO}\n\n{user_part}\n\n{_NODE_SCHEMA_RULES}"


# Heuristic guard for the "stringified JSON blob" loophole: an LLM can satisfy the literal
# "no object/array datatype" rule while still violating FLAT STRUCTURE by naming a "string"
# param whose *value* is a JSON-encoded array/object packing multiple fields or records.
_JSON_BLOB_NAME_RE = re.compile(r"(_json|_list|_array|_blob|_payload)$", re.IGNORECASE)
_JSON_BLOB_DESC_RE = re.compile(
    r"\b("
    r"stringified\s+json|json[\s-]?encoded|json\s+array|json\s+object|"
    r"array\s+of\s+objects|list\s+of\s+objects|each\s+containing|each\s+with|"
    r"array\s+of\s+\d+|list\s+of\s+\d+"
    r")\b",
    re.IGNORECASE,
)


def _detect_json_blob_params(schema: NodeSchema) -> List[str]:
    """
    Flags outputParams that look like a JSON-encoded blob smuggled into a "string"
    datatype (e.g. a "mcq_list_json" holding a stringified array of question objects).
    A name-pattern hit AND a description-pattern hit together are required, to keep false
    positives low while still catching the exact escape hatch this rule exists to close.

    NOTE: datatype="list" params are explicitly exempt — they are the sanctioned
    replacement for the blob pattern and must never trigger this check.
    """
    errors: List[str] = []
    for op in schema.outputParams:
        if op.datatype != "string":
            # "list" params are exempt; non-string, non-"list" won't match blob names anyway
            continue
        if _JSON_BLOB_NAME_RE.search(op.name) and _JSON_BLOB_DESC_RE.search(op.description or ""):
            errors.append(
                f"Node '{schema.nodeId}' outputParam '{op.name}' (datatype=string, "
                f"description={op.description!r}) is a JSON-encoded blob smuggled past the "
                f"FLAT STRUCTURE rule. datatype=\"string\" does not exempt a param that packs "
                f"multiple fields/records inside it. Replace this param with "
                f"datatype=\"list\". Use itemType=\"scalar\" + scalarItemDatatype for "
                f"arrays of strings/numbers/booleans, or itemType=\"record\" + itemSchema "
                f"for arrays of objects. Do not wrap string arrays as [{{field: string}}]. "
                f"Do not reintroduce a JSON-encoded string or a flat indexed explosion."
            )
    return errors


_VALID_DATATYPES: frozenset = frozenset({"string", "boolean", "number", "list"})


def _collect_list_kind_errors(
    node_id: str,
    param_label: str,
    p: "NodeParam",
    *,
    field_path: Optional[str] = None,
) -> List[str]:
    """Check 6: a list param (or nested list field) must declare a valid item kind."""
    if p.datatype != "list":
        return []
    errors: List[str] = []
    path = field_path or p.name
    prefix = f"Node '{node_id}' {param_label} '{path}'"
    item_type = _effective_item_type(p)
    if item_type == "scalar":
        if p.scalarItemDatatype not in _SCALAR_ITEM_DATATYPES:
            errors.append(
                f"{prefix}: itemType='scalar' requires scalarItemDatatype "
                f"(string|boolean|number)."
            )
        if p.itemSchema:
            errors.append(
                f"{prefix}: itemType='scalar' must not set itemSchema — use "
                f"scalarItemDatatype only (do not wrap strings as records)."
            )
        return errors
    if item_type == "enum":
        if not p.enumValues:
            errors.append(f"{prefix}: itemType='enum' requires non-empty enumValues.")
        return errors
    if item_type != "record" or not p.itemSchema:
        errors.append(
            f"{prefix}: datatype='list' requires itemType 'scalar' (with "
            f"scalarItemDatatype), 'record' (with itemSchema), or 'enum' "
            f"(with enumValues). Do NOT wrap a list of strings as "
            f"[{{field: string}}] — use itemType='scalar'."
        )
        return errors
    for item_field in p.itemSchema:
        if item_field.datatype not in ("string", "boolean", "number", "list"):
            errors.append(
                f"{prefix}: itemSchema field '{item_field.name}' has invalid datatype "
                f"'{item_field.datatype}'. Item fields must be scalar or list (depth ≤ 1)."
            )
        if item_field.datatype == "list":
            nested_kind = _effective_item_type(item_field)
            errors.extend(_collect_list_kind_errors(
                node_id, param_label, item_field,
                field_path=f"{path}.{item_field.name}",
            ))
            if nested_kind == "record" and item_field.itemSchema:
                for nested in item_field.itemSchema:
                    if nested.datatype == "list":
                        errors.append(
                            f"{prefix}: itemSchema nesting depth > 1 is not permitted "
                            f"(field '{item_field.name}' → '{nested.name}' is also a list)."
                        )
        if item_field.inputBindings:
            errors.append(
                f"{prefix}: itemSchema field '{item_field.name}' must not carry "
                f"inputBindings — itemSchema describes record shape, not bound values."
            )
    return errors


def _validate_node_schema_batch(
    batch_schemas: List[NodeSchema],
    batch: NodeBatch,
    register: AvailableVarsRegister,
    execution_path_index: Dict[str, Any],
) -> List[str]:
    """
    Stage 2b: Validate a single batch of NodeSchemas.
    Returns error strings. Empty list = valid.

    Checks (in order):
      1. All expected nodeIds present, no extras
      2. inputBindings reference only the register or earlier nodes in this batch
         (index-aware: "nodeId.param[0].field" resolves against "nodeId.param")
      3. Referenced binding source is a valid ancestor per the Execution Path Index
      4. outputParams do not have non-empty inputBindings
      5. outputParams do not smuggle nested structure into a JSON-encoded string
      6a. Datatype allowlist: only string/boolean/number/list permitted
      6. list params must declare itemType scalar|record|enum (record needs itemSchema;
         scalar needs scalarItemDatatype; enum needs enumValues). Do not wrap string
         arrays as one-field records.
      7. list→list binding: source must also be list with compatible item kind
         (scalar↔scalar, enum↔enum, record field-subset)
      7b. scalar→whole-list binding is forbidden; indexed field syntax is record-only
      8. literal-index binding: index non-negative int, field exists in source's
         itemSchema, field's scalar type matches receiving param's datatype
    """
    errors: List[str] = []
    expected_ids = set(batch.nodeIds)
    returned_ids = {s.nodeId for s in batch_schemas}

    missing = expected_ids - returned_ids
    if missing:
        errors.append(f"Missing nodeIds from batch {batch.batchIndex}: {sorted(missing)}")

    extra = returned_ids - expected_ids
    if extra:
        errors.append(f"Unexpected nodeIds in batch {batch.batchIndex}: {sorted(extra)}")

    # Build lookup: what's available (register + earlier nodes in batch)
    # Dict[str, RefMeta] keyed on "nodeId.paramName" or "global.paramName"
    available_refs: Dict[str, RefMeta] = {}
    for entry in register.nodeOutputs:
        for o in entry.outputs:
            available_refs[f"{entry.nodeId}.{o['name']}"] = _ref_meta_from_obj(o)
    for ref_key, g in _global_ref_key_map(_register_all_globals(register)).items():
        available_refs[ref_key] = _ref_meta_from_obj(g)

    # Track outputs of earlier nodes within this batch
    earlier_outputs: Dict[str, RefMeta] = {}
    earlier_batch_nodes: set = set()

    ordered_schemas = sorted(
        batch_schemas,
        key=lambda s: batch.nodeIds.index(s.nodeId) if s.nodeId in batch.nodeIds else 999,
    )

    for schema in ordered_schemas:
        combined_refs: Dict[str, RefMeta] = {**available_refs, **earlier_outputs}
        node_valid_ancestors = _path_index_ancestors(execution_path_index, schema.nodeId)

        # ── Check 6a: datatype allowlist for all params ───────────────────────
        for ip in schema.inputParams:
            if ip.datatype not in _VALID_DATATYPES:
                errors.append(
                    f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                    f"datatype '{ip.datatype}' is not allowed. "
                    f"Use one of: string, boolean, number, list."
                )
        for op in schema.outputParams:
            if op.datatype not in _VALID_DATATYPES:
                errors.append(
                    f"Node '{schema.nodeId}' outputParam '{op.name}': "
                    f"datatype '{op.datatype}' is not allowed. "
                    f"Use one of: string, boolean, number, list."
                )

        # ── Check 6: list params must declare a valid item kind ───────────────
        for param_label, params in [("inputParam", schema.inputParams), ("outputParam", schema.outputParams)]:
            for p in params:
                errors.extend(_collect_list_kind_errors(schema.nodeId, param_label, p))

        # ── Binding checks per inputParam ─────────────────────────────────────
        for ip in schema.inputParams:
            if ip.required and not ip.inputBindings:
                errors.append(
                    f"Node '{schema.nodeId}' required inputParam '{ip.name}' has no "
                    f"binding — required inputs must bind to upstream output or global.*"
                )
                continue

            for binding in ip.inputBindings:
                ref = binding.boundTo

                if ref.startswith("pick:"):
                    errors.extend(
                        _validate_pick_binding(
                            schema.nodeId,
                            ip.name,
                            ref,
                            combined_refs,
                            execution_path_index,
                            receiving_ip=ip,
                        )
                    )
                    continue

                if ref.startswith("global."):
                    if ref not in combined_refs:
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' is not in the globals register."
                        )
                        continue
                    src_meta = combined_refs[ref]
                    if ip.datatype == "list":
                        if src_meta.datatype != "list":
                            errors.append(
                                f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                                f"list-typed param binds to '{ref}' which has "
                                f"datatype '{src_meta.datatype}' — must also be list."
                            )
                        else:
                            errors.extend(_check_list_binding_compat(
                                schema.nodeId, ip.name, ip, src_meta, ref
                            ))
                    elif (
                        ip.datatype in ("string", "boolean", "number")
                        and src_meta.datatype == "list"
                    ):
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}' "
                            f"(datatype='{ip.datatype}'): cannot bind to whole list "
                            f"'{ref}'. Use indexed syntax on a record list, or bind "
                            f"the list param itself."
                        )
                    continue

                # Parse the binding (may be plain or indexed)
                bref = _parse_bound_to(ref)
                if bref is None:
                    errors.append(
                        f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                        f"malformed binding '{ref}'."
                    )
                    continue

                parent_ref = f"{bref.node_id}.{bref.param_name}"

                # Check 1 (index-aware): parent ref must be in combined_refs
                if parent_ref not in combined_refs:
                    errors.append(
                        f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                        f"binding '{ref}' (parent ref '{parent_ref}') is not in the "
                        f"register or earlier in this batch."
                    )
                    continue

                # Check 2 (index-aware): ancestor check uses parent node_id
                if (
                    bref.node_id not in node_valid_ancestors
                    and bref.node_id not in earlier_batch_nodes
                ):
                    errors.append(
                        f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                        f"binding '{ref}' references '{bref.node_id}' which is NOT "
                        f"a valid ancestor on an unbroken execution path to "
                        f"'{schema.nodeId}'. This is a cross-branch or invalid binding."
                    )
                    continue

                src_meta = combined_refs[parent_ref]

                if bref.index is not None:
                    # ── Check 8: literal-index scalar extraction (record lists only)
                    if src_meta.datatype != "list":
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' uses indexed syntax but source param "
                            f"'{parent_ref}' is not a list (datatype='{src_meta.datatype}')."
                        )
                        continue
                    src_kind = _effective_item_type(src_meta)
                    if src_kind in ("scalar", "enum"):
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' uses indexed field syntax but source "
                            f"'{parent_ref}' is a {src_kind} list — indexed '.field' "
                            f"access is only valid for record lists."
                        )
                        continue
                    if not src_meta.itemSchema:
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' uses indexed syntax but source '{parent_ref}' "
                            f"has no itemSchema — cannot validate field '{bref.item_field}'."
                        )
                        continue
                    schema_fields_by_name = {
                        _item_field_name(f): f for f in src_meta.itemSchema
                    }
                    src_field = schema_fields_by_name.get(bref.item_field)
                    if src_field is None:
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' — field '{bref.item_field}' not found in "
                            f"'{parent_ref}' itemSchema (fields: "
                            f"{sorted(schema_fields_by_name.keys())})."
                        )
                    elif _item_field_datatype(src_field) != ip.datatype:
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                            f"binding '{ref}' — field '{bref.item_field}' has datatype "
                            f"'{_item_field_datatype(src_field)}' but receiving param is '{ip.datatype}'."
                        )
                    elif ip.datatype == "list":
                        # Indexed extraction of a nested list must still satisfy Check 7.
                        errors.extend(_check_list_binding_compat(
                            schema.nodeId,
                            ip.name,
                            ip,
                            _ref_meta_from_obj(src_field),
                            f"{parent_ref}[].{bref.item_field}",
                        ))
                else:
                    # Plain (non-indexed) binding
                    # ── Check 7: list→list compatibility ─────────────────────
                    if ip.datatype == "list":
                        if src_meta.datatype != "list":
                            errors.append(
                                f"Node '{schema.nodeId}' inputParam '{ip.name}': "
                                f"list-typed param binds to '{parent_ref}' which has "
                                f"datatype '{src_meta.datatype}' — must also be list."
                            )
                        else:
                            errors.extend(_check_list_binding_compat(
                                schema.nodeId, ip.name, ip, src_meta, parent_ref
                            ))
                    # ── Check 7b: scalar must not bind to whole list ──────────
                    elif ip.datatype in ("string", "boolean", "number") and src_meta.datatype == "list":
                        errors.append(
                            f"Node '{schema.nodeId}' inputParam '{ip.name}' (datatype='{ip.datatype}'): "
                            f"cannot bind to whole list '{parent_ref}'. "
                            f"Use indexed syntax: '{parent_ref}[0].<fieldName>' "
                            f"(record lists only)."
                        )

        # ── Check 4: outputParams must not carry inputBindings ────────────────
        for op in schema.outputParams:
            if op.inputBindings:
                errors.append(
                    f"Node '{schema.nodeId}' outputParam '{op.name}' "
                    f"must not have inputBindings (outputParams are produced, not consumed)."
                )
            earlier_outputs[f"{schema.nodeId}.{op.name}"] = _ref_meta_from_obj(op)

        # ── Check 5: reject JSON-blob strings ─────────────────────────────────
        errors.extend(_detect_json_blob_params(schema))

        earlier_batch_nodes.add(schema.nodeId)

    if errors:
        _log_binding_audit(
            "stage2b_validation_failed",
            batchIndex=batch.batchIndex,
            nodeIds=list(batch.nodeIds),
            errorCount=len(errors),
            errors=errors[:20],
        )
    else:
        binding_rows: List[Dict[str, Any]] = []
        for schema in batch_schemas:
            binding_rows.extend([
                {"nodeId": schema.nodeId, **row}
                for row in _summarize_schema_bindings(schema)
            ])
        _log_binding_audit(
            "stage2b_validation_passed",
            batchIndex=batch.batchIndex,
            nodeIds=list(batch.nodeIds),
            bindings=binding_rows,
        )

    return errors


async def _run_single_batch(
    batch: NodeBatch,
    skeleton: SkeletonPlan,
    register: AvailableVarsRegister,
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
    execution_path_index: Dict[str, Any] = None,
) -> List[NodeSchema]:
    """
    Run Stage 2a + Stage 2b for one batch.
    Retries on validation failure (batch-scoped, never re-runs other batches).
    Repair is isolated: only this batch re-runs; the register and other batches
    are untouched.

    On retry, if secondary globals exist, a cheap targeted detail lookup may
    inject full descriptions for secondary candidates into the next attempt.
    """
    # Cache system rules + phase/global register (stable within a phase — NO nodeOutputs).
    # Dynamic prompt carries per-batch nodes + growing nodeOutputs.
    static_cache_text = _build_static_node_schema_cache_text(register, state)
    dynamic_user_prompt = await _build_node_schema_dynamic_user_prompt(
        batch, skeleton, register, state, execution_path_index=execution_path_index
    )
    full_prompt = await _build_node_schema_prompt(
        batch, skeleton, register, state, execution_path_index=execution_path_index
    )
    cached_content = await _get_scratch_cached_content(
        api_key,
        _NODE_SCHEMA_BUILDER_MODEL,
        system_text=static_cache_text,
        cache_key_prefix="scratch_node_schema_phase_register",
        display_name="scratch-node-schema-phase-register",
    )
    _log_binding_audit(
        "stage2a_batch_start",
        phaseId=state.phaseId,
        batchIndex=batch.batchIndex,
        nodeIds=list(batch.nodeIds),
        cacheActive=bool(cached_content),
        primaryGlobals=[g.name for g in register.globals],
        secondaryGlobals=[g.name for g in register.secondaryGlobals],
    )
    # When cache is active, send dynamic-only user prompt; otherwise full prompt.
    user_prompt = dynamic_user_prompt if cached_content else full_prompt
    last_errors: List[str] = []
    # Secondary descriptions enriched via targeted lookup on retry (batched once).
    enriched_secondary: List[GlobalVar] = []

    for attempt in range(1, _MAX_RETRIES + 1):
        temperature = round(0.2 + (attempt - 1) * 0.1, 2)
        logger.info(
            f"[scratch/stage2a] Batch {batch.batchIndex} attempt {attempt}/{_MAX_RETRIES} "
            f"nodes={batch.nodeIds} temp={temperature}"
        )
        try:
            # Append secondary detail block (after lookup on a prior failed attempt).
            secondary_detail = _format_secondary_detail_block(enriched_secondary)
            effective_user = user_prompt
            effective_full = full_prompt
            if secondary_detail:
                effective_user = f"{user_prompt}\n\n{secondary_detail}\n"
                effective_full = f"{full_prompt}\n\n{secondary_detail}\n"

            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_NODE_SCHEMA_BUILDER_MODEL,
                sub_operation="stage2a-node-schema-builder",
                system_text=static_cache_text if cached_content else _NODE_SCHEMA_SYSTEM,
                user_prompt=effective_user,
                full_prompt=effective_full,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={
                    "stage": "2a",
                    "batch_index": batch.batchIndex,
                    "node_ids": list(batch.nodeIds),
                    "secondary_enriched": [g.name for g in enriched_secondary],
                },
                temperature=temperature,
                as_json=True,
                # NOTE: NodeSchemaList contains NodeParam which has a self-referential
                # itemSchema field (List["NodeParam"]). Passing it as response_schema
                # causes the Gemini SDK schema generator to recurse infinitely.
                # Parse the raw JSON manually instead.
            )
            data = _parse_json_robust(raw) if parsed is None else (
                parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed
            )
            schema_list = NodeSchemaList(**data)

            for _n in schema_list.nodes:
                for _ip in _n.inputParams:
                    for _b in _ip.inputBindings:
                        if _b.boundTo and "system." in _b.boundTo:
                            _b.boundTo = _b.boundTo.replace("system.", "global.")

            errors = _validate_node_schema_batch(
                schema_list.nodes, batch, register, execution_path_index
            )
            if errors:
                last_errors = errors
                error_text = "\n".join(f"  - {e.replace('global.', 'system.')}" for e in errors)
                logger.warning(
                    f"[scratch/stage2b] Batch {batch.batchIndex} validation failed:\n{error_text}"
                )
                # One-shot secondary detail enrichment before the next binder retry.
                # Only for unresolved required bindings / unknown register refs —
                # not for missing nodes, cross-branch paths, or outputParam errors.
                if (
                    attempt < _MAX_RETRIES
                    and register.secondaryGlobals
                    and not enriched_secondary
                    and _errors_warrant_secondary_detail_lookup(errors)
                ):
                    enriched_secondary = await _lookup_secondary_global_details(
                        state,
                        register,
                        batch=batch,
                        skeleton=skeleton,
                        validation_errors=errors,
                        api_key=api_key,
                    )
                retry_header = (
                    f"PREVIOUS ATTEMPT FOR BATCH {batch.batchIndex} FAILED VALIDATION:\n"
                    f"{error_text}\n\n"
                    f"Fix all listed errors and regenerate ONLY the nodes in this batch.\n"
                )
                if enriched_secondary:
                    retry_header += (
                        "If a required input has no PRIMARY fit, consider SECONDARY "
                        "globals — full descriptions for candidates are provided below.\n"
                    )
                retry_header += "\n"
                if cached_content:
                    user_prompt = (
                        retry_header
                        + await _build_node_schema_dynamic_user_prompt(
                            batch, skeleton, register, state,
                            execution_path_index=execution_path_index,
                        )
                    )
                else:
                    user_prompt = (
                        retry_header
                        + await _build_node_schema_user_prompt(
                            batch, skeleton, register, state,
                            execution_path_index=execution_path_index,
                        )
                    )
                full_prompt = (
                    retry_header
                    + await _build_node_schema_prompt(
                        batch, skeleton, register, state,
                        execution_path_index=execution_path_index,
                    )
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                continue

            logger.info(
                f"[scratch/stage2a] Batch {batch.batchIndex} validated: "
                f"{len(schema_list.nodes)} node(s)."
            )
            return schema_list.nodes

        except BillingError:
            raise
        except Exception as exc:
            last_errors = [str(exc)]
            logger.warning(f"[scratch/stage2a] Batch {batch.batchIndex} attempt {attempt} error: {exc}")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.5 * attempt)

    raise RuntimeError(
        f"[scratch/stage2a] Batch {batch.batchIndex} failed after {_MAX_RETRIES} attempts. "
        f"Nodes: {batch.nodeIds}. Last errors: {last_errors}"
    )


async def _stage2a_node_schema_builder(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
    *,
    checkpoint_fn: Optional[Any] = None,
    start_after_batch_index: int = -1,
) -> List[NodeSchema]:
    """
    Stage 2a: Build node schemas for all non-router, non-end nodes.

    Execution pattern (v2 — strictly sequential):
      BatchPlan + ExecutionPathIndex built from skeleton (pure code)
      → Batch 0 LLM call → Stage 2b validation → register updated
      → Batch 1 LLM call → Stage 2b validation → register updated
      → ...
      → All batches done → full NodeSchemaList returned

    Each batch is fully independent of others at call time — only the
    cumulative register (from prior validated batches) is shared.
    If a batch fails validation it re-runs in isolation; the register
    and all completed batches are untouched.
    """
    skeleton = state.skeletonPlan
    assert skeleton is not None

    # Step 1: Build batch plan (pure code)
    batches = _build_batch_plan(skeleton)
    state.nodeBatchPlan = batches

    if not batches:
        logger.info("[scratch/stage2a] No eligible nodes — empty schema list.")
        return []

    # Step 2: Build Execution Path Index (pure code, once, before any LLM call)
    execution_path_index = _build_execution_path_index(skeleton)
    state.executionPathIndex = execution_path_index
    logger.info(
        f"[scratch/stage2a] ExecutionPathIndex built for "
        f"{len(execution_path_index.get('ancestors', {}))} eligible node(s)."
    )

    logger.info(
        f"[scratch/stage2a] {len(batches)} batch(es) for "
        f"{sum(len(b.nodeIds) for b in batches)} eligible node(s). "
        f"Running STRICTLY SEQUENTIAL."
    )

    all_schemas: Dict[str, NodeSchema] = {}
    register = state.availableVarsRegister  # mutated in-place as batches complete
    eligible_ids = {n.nodeId for n in skeleton.nodes if n.type not in ("router", "end")}

    # Seed from resumed checkpoint state
    for schema in state.nodeSchemas:
        all_schemas[schema.nodeId] = schema

    # Seed register with prior-phase globals (primary + secondary advisory split).
    # Do not wipe nodeOutputs already present from resumed batches.
    _seed_register_globals(state)

    # Step 4: Sequential batch execution — Batch 0 → 1 → 2 → ...
    for batch in sorted(batches, key=lambda b: b.batchIndex):
        if batch.batchIndex <= start_after_batch_index:
            continue
        logger.info(
            f"[scratch/stage2a] Starting batch {batch.batchIndex}/{len(batches) - 1} "
            f"nodes={batch.nodeIds}"
        )
        schemas = await _run_single_batch(
            batch, skeleton, register, state, api_key, execution_path_index
        )

        for schema in schemas:
            all_schemas[schema.nodeId] = schema
            # Extend register — available to all subsequent batches
            register.nodeOutputs.append(NodeOutputEntry(
                nodeId=schema.nodeId,
                batch=batch.batchIndex,
                outputs=[
                    {
                        "name": op.name,
                        "datatype": op.datatype,
                        **_dump_list_item_annotation(op),
                    }
                    for op in schema.outputParams
                ],
            ))
        state.nodeSchemas = [
            all_schemas[nid]
            for nid in _topological_sort_nodes(list(eligible_ids), skeleton.edges)
            if nid in all_schemas
        ]
        logger.info(
            f"[scratch/stage2a] Batch {batch.batchIndex} complete — "
            f"register now has {len(register.nodeOutputs)} node output entry(ies)."
        )
        if checkpoint_fn is not None:
            await _safe_checkpoint(checkpoint_fn, state, "stage2a", batch_index=batch.batchIndex)

    # Final check: every eligible node has a schema
    missing = eligible_ids - set(all_schemas.keys())
    if missing:
        raise RuntimeError(
            f"[scratch/stage2a] Final check failed: schemas missing for nodes: {sorted(missing)}"
        )

    # Return in topological order
    all_sorted = _topological_sort_nodes(list(eligible_ids), skeleton.edges)
    ordered = [all_schemas[nid] for nid in all_sorted if nid in all_schemas]
    binding_count = sum(
        len(ip.inputBindings)
        for s in ordered
        for ip in s.inputParams
    )
    logger.info(
        f"[scratch/stage2a] All batches complete: {len(ordered)} schema(s), "
        f"{binding_count} input binding(s), "
        f"{sum(len(s.outputParams) for s in ordered)} output param(s)."
    )
    return ordered


# ===========================================================================
# SECTION 5 — STAGE 2c: EXECUTION STATE SCHEMA DERIVATION (pure code)
# ===========================================================================

def _stage2c_state_schema(node_schemas: List[NodeSchema]) -> StateSchema:
    """
    Stage 2c: Derive the flat execution state schema from validated NodeSchemas.

    For every outputParam across all non-router, non-end nodes:
      stateFieldName = "{nodeId}__{paramName}"  (double underscore)

    Also resolves inputBindings to their stateFieldName.
    """
    fields: List[StateField] = []
    for schema in node_schemas:
        for op in schema.outputParams:
            fields.append(StateField(
                fieldName=f"{schema.nodeId}__{op.name}",
                datatype=op.datatype,
                producedBy=schema.nodeId,
                sourceParam=op.name,
                itemSchema=op.itemSchema,
                itemType=op.itemType,
                scalarItemDatatype=op.scalarItemDatatype,
                enumValues=op.enumValues,
            ))

    return StateSchema(fields=fields)


def _binding_to_state_key(bound_to: str, state_schema: StateSchema) -> Optional[str]:
    """
    Resolve "nodeId.paramName" (or indexed "nodeId.paramName[0].field") →
    "nodeId__paramName" using the StateSchema.

    For indexed bindings the parent list state key is returned — Stage 6 codegen
    preserves the full binding string separately for indexed access.
    Returns None if not found.
    """
    bref = _parse_bound_to(bound_to)
    if bref is None:
        return None
    expected = f"{bref.node_id}__{bref.param_name}"
    for f in state_schema.fields:
        if f.fieldName == expected:
            return expected
    return None


# ===========================================================================
# SECTION 6 — STAGE 3a: EDGE MAPPER (per-router isolated LLM calls)
# ===========================================================================

_REF_PATTERN_3A = re.compile(r'\b([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\b')

_ROUTER_EDGE_INTRO = "You are assigning routing conditions for exactly ONE router node."

_ROUTER_EDGE_RULES = """
═══════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════
1. Write a condition string for EVERY outgoing edge listed in OUTGOING EDGES.
2. Conditions MUST reference only outputParams listed in ANCESTOR OUTPUT PARAMS.
   Use "nodeId.paramName" dot notation exactly as listed — do NOT invent new param names.
3. CRITICAL — Do NOT invent enum values. The string values you compare against
   (e.g. "success", "failure", "true", "false") MUST come from the description of the node
   that produces that param. The node description will state what values the param can hold.
   If the producing node's description says the output is "success or failure", use exactly
   those strings. Never use values like "MATCHED", "VERIFIED", "OK", "YES" unless the
   description explicitly uses those exact strings.
4. All conditions together must be exhaustive — every possible case is covered.
   The last branch should use != (not equals) rather than a specific value to act as the
   default/fallback.
5. Use standard Python comparison operators: ==, !=, <, >, <=, >=, and, or, not.
6. Do NOT add or remove edges from OUTGOING EDGES.

EXAMPLE (for a router that reads a status param):
  If ancestor output is: verify_identity outputs: [verification_status:string]
  And the node description says verification_status is "success" or "failure":
  → success branch:  verify_identity.verification_status == "success"
  → failure branch:  verify_identity.verification_status != "success"
""".strip()

_ROUTER_EDGE_SYSTEM = f"{_ROUTER_EDGE_INTRO}\n\n{_ROUTER_EDGE_RULES}"


def _build_router_edge_context(
    router_id: str,
    state: ScratchPlanningState,
    ancestor_ids: set,
) -> str:
    """Router phase context, edges, and ancestor params (no rules or JSON template)."""
    skeleton = state.skeletonPlan
    node_map = {n.nodeId: n for n in skeleton.nodes}
    schema_map = {s.nodeId: s for s in state.nodeSchemas}

    router_node = node_map.get(router_id)
    router_desc = router_node.description if router_node else ""

    ancestor_params_lines: List[str] = []
    for anc_id in sorted(ancestor_ids):
        schema = schema_map.get(anc_id)
        if schema and schema.outputParams:
            params_text = ", ".join(
                f"{op.name}:{_format_ref_type_of(op)}"
                for op in schema.outputParams
            )
            ancestor_params_lines.append(f"  {anc_id} outputs: [{params_text}]")

    ancestor_params_section = (
        "\n".join(ancestor_params_lines)
        if ancestor_params_lines
        else "  (no ancestor outputs available)"
    )

    outgoing = [e for e in skeleton.edges if e.fromNode == router_id]
    outgoing_text = "\n".join(
        f"  → {e.to}  [{e.kind}]" for e in outgoing
    )

    return f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

ROUTER NODE: {router_id}
DESCRIPTION: {router_desc}

OUTGOING EDGES (you must write a condition for each):
{outgoing_text}

ANCESTOR OUTPUT PARAMS (you may ONLY reference these in conditions):
{ancestor_params_section}"""


def _build_router_edge_json_template(router_id: str) -> str:
    return f"""Output JSON exactly — no markdown, no explanation:
{{
  "routerNodeId": "{router_id}",
  "edges": [
    {{
      "to": "target_node_id",
      "kind": "conditional",
      "condition": "source_node.param_name == \\"exact_value_from_node_description\\""
    }}
  ]
}}"""


def _build_router_edge_user_prompt(
    router_id: str,
    state: ScratchPlanningState,
    ancestor_ids: set,
) -> str:
    """Dynamic per-router portion for cached Stage 3a calls."""
    return (
        f"{_build_router_edge_context(router_id, state, ancestor_ids)}\n\n"
        f"{_build_router_edge_json_template(router_id)}"
    )


def _build_router_edge_prompt(
    router_id: str,
    state: ScratchPlanningState,
    ancestor_ids: set,
) -> str:
    """
    Full Stage 3a prompt in original section order (uncached fallback).
    """
    return (
        f"{_ROUTER_EDGE_INTRO}\n\n"
        f"{_build_router_edge_context(router_id, state, ancestor_ids)}\n\n"
        f"{_ROUTER_EDGE_RULES}\n\n"
        f"{_build_router_edge_json_template(router_id)}"
    )


def _validate_router_edge_result(
    result: RouterEdgeResult,
    router_id: str,
    skeleton: SkeletonPlan,
    ancestor_ids: set,
    node_schemas: List[NodeSchema],
) -> List[str]:
    """
    Stage 3b: Validate the result of a single per-router Stage 3a call.
    Returns error strings. Empty = valid.
    """
    errors: List[str] = []

    # Build valid refs: only ancestor outputParams
    valid_refs: set = set()
    schema_map = {s.nodeId: s for s in node_schemas}
    for anc_id in ancestor_ids:
        schema = schema_map.get(anc_id)
        if schema:
            for op in schema.outputParams:
                valid_refs.add(f"{anc_id}.{op.name}")

    # Expected outgoing conditional edges from this router
    expected_targets = {
        e.to for e in skeleton.edges
        if e.fromNode == router_id and e.kind == "conditional"
    }
    returned_targets = {e.to for e in result.edges}

    missing_targets = expected_targets - returned_targets
    if missing_targets:
        errors.append(
            f"Router '{router_id}': missing conditions for edges to: {sorted(missing_targets)}"
        )

    extra_targets = returned_targets - expected_targets
    if extra_targets:
        errors.append(
            f"Router '{router_id}': unexpected edge targets (not in skeleton): {sorted(extra_targets)}"
        )

    for edge in result.edges:
        if not edge.condition or not edge.condition.strip():
            errors.append(
                f"Router '{router_id}' edge to '{edge.to}': condition is empty or null."
            )
            continue

        # Validate that all nodeId.paramName refs are in the ancestor-filtered context
        for m in _REF_PATTERN_3A.finditer(edge.condition):
            ref = f"{m.group(1)}.{m.group(2)}"
            if ref not in valid_refs:
                errors.append(
                    f"Router '{router_id}' edge to '{edge.to}': condition references "
                    f"'{ref}' which is not in this router's ancestor-filtered outputParams."
                )

    return errors


async def _stage3a_per_router(
    router_id: str,
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
) -> RouterEdgeResult:
    """
    Stage 3a + 3b for a single router node.

    Each router call is fully independent — no shared state with other routers.
    If this router fails validation, only this router re-runs. All other routers
    that already completed are untouched.
    """
    skeleton = state.skeletonPlan

    # Get this router's ancestor set from the ExecutionPathIndex
    # executionPathIndex is keyed on eligible (non-router) nodes; we need the
    # ancestors of the router itself — walk backwards from the router's direct
    # predecessors to collect the effective ancestor set.
    # We use the direct-predecessor eligible ancestors: the union of all
    # eligible ancestors reachable from any direct parent of this router.
    node_map = {n.nodeId: n for n in skeleton.nodes}

    ancestor_ids: set = set()
    # Find immediate predecessors of this router
    for e in skeleton.edges:
        if e.to == router_id:
            pred_id = e.fromNode
            pred_node = node_map.get(pred_id)
            if pred_node and pred_node.type not in ("router", "end"):
                # Predecessor is eligible — include it and its ancestors
                ancestor_ids.add(pred_id)
                ancestor_ids.update(_path_index_ancestors(state.executionPathIndex, pred_id))
            else:
                # Predecessor is itself a router/end — use that router's context
                # (which we approximate by taking ancestors of nodes flowing into it)
                for inner_e in skeleton.edges:
                    if inner_e.to == pred_id:
                        inner_pred = node_map.get(inner_e.fromNode)
                        if inner_pred and inner_pred.type not in ("router", "end"):
                            ancestor_ids.add(inner_e.fromNode)
                            ancestor_ids.update(
                                _path_index_ancestors(
                                    state.executionPathIndex, inner_e.fromNode
                                )
                            )

    user_prompt = _build_router_edge_user_prompt(router_id, state, ancestor_ids)
    full_prompt = _build_router_edge_prompt(router_id, state, ancestor_ids)
    cached_content = await _get_scratch_cached_content(
        api_key,
        _EDGE_MAPPER_MODEL,
        system_text=_ROUTER_EDGE_SYSTEM,
        cache_key_prefix="scratch_router_edge",
        display_name="scratch-router-edge-system",
    )
    last_errors: List[str] = []

    for attempt in range(1, _MAX_RETRIES + 1):
        temperature = round(0.2 + (attempt - 1) * 0.1, 2)
        logger.info(
            f"[scratch/stage3a] Router '{router_id}' attempt {attempt}/{_MAX_RETRIES} "
            f"ancestors={sorted(ancestor_ids)} temp={temperature}"
        )
        try:
            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_EDGE_MAPPER_MODEL,
                sub_operation="stage3a-edge-mapper",
                system_text=_ROUTER_EDGE_SYSTEM,
                user_prompt=user_prompt,
                full_prompt=full_prompt,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={
                    "stage": "3a",
                    "router_id": router_id,
                    "ancestor_ids": sorted(ancestor_ids),
                },
                temperature=temperature,
                as_json=True,
                response_schema=RouterEdgeResult,
            )
            if parsed is None:
                data = _parse_json_robust(raw)
                result = RouterEdgeResult(**data)
            else:
                result = parsed

            errors = _validate_router_edge_result(
                result, router_id, skeleton, ancestor_ids, state.nodeSchemas
            )
            if errors:
                last_errors = errors
                error_text = "\n".join(f"  - {e}" for e in errors)
                logger.warning(
                    f"[scratch/stage3b] Router '{router_id}' validation failed:\n{error_text}"
                )
                user_prompt = (
                    f"PREVIOUS ATTEMPT FOR ROUTER '{router_id}' FAILED VALIDATION:\n"
                    f"{error_text}\n\n"
                    f"Fix all listed errors and regenerate conditions for this router only.\n\n"
                    f"{_build_router_edge_user_prompt(router_id, state, ancestor_ids)}"
                )
                full_prompt = (
                    f"PREVIOUS ATTEMPT FOR ROUTER '{router_id}' FAILED VALIDATION:\n"
                    f"{error_text}\n\n"
                    f"Fix all listed errors and regenerate conditions for this router only.\n\n"
                    f"{_build_router_edge_prompt(router_id, state, ancestor_ids)}"
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                continue

            logger.info(
                f"[scratch/stage3a] Router '{router_id}' validated: "
                f"{len(result.edges)} condition(s)."
            )
            return result

        except BillingError:
            raise
        except Exception as exc:
            last_errors = [str(exc)]
            logger.warning(
                f"[scratch/stage3a] Router '{router_id}' attempt {attempt} error: {exc}"
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.5 * attempt)

    raise RuntimeError(
        f"[scratch/stage3a] Router '{router_id}' failed after {_MAX_RETRIES} attempts. "
        f"Last errors: {last_errors}"
    )


async def _stage3a_edge_mapper(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
    *,
    checkpoint_fn: Optional[Any] = None,
    resume_completed_router_results: Optional[List[RouterEdgeResult]] = None,
) -> EdgeMap:
    """
    Stage 3a orchestrator: run one isolated LLM call per router, then assemble EdgeMap.

    When ``checkpoint_fn`` is set, routers run sequentially with a checkpoint after
    each router (enables mid-stage3a resume). Otherwise routers run in parallel.
    """
    skeleton = state.skeletonPlan
    assert skeleton is not None

    router_ids = [n.nodeId for n in skeleton.nodes if n.type == "router"]

    if not router_ids:
        logger.info("[scratch/stage3a] No router nodes — building sequential EdgeMap.")
        edges = [
            MappedEdge(fromNode=e.fromNode, to=e.to, kind=e.kind, condition=None)
            for e in skeleton.edges
        ]
        return EdgeMap(edges=edges)

    router_results: List[RouterEdgeResult] = list(resume_completed_router_results or [])
    completed_ids = {r.routerNodeId for r in router_results}
    pending_ids = [rid for rid in router_ids if rid not in completed_ids]

    if checkpoint_fn is not None:
        logger.info(
            f"[scratch/stage3a] Running {len(pending_ids)} pending router(s) SEQUENTIALLY "
            f"(checkpoint enabled, {len(completed_ids)} already done): {pending_ids}"
        )
        sequential_start = time.perf_counter()
        for rid in pending_ids:
            result = await _stage3a_per_router(rid, state, api_key)
            router_results.append(result)
            await _safe_checkpoint(
                checkpoint_fn,
                state,
                "stage3a",
                router_id=rid,
                completed_router_results=[
                    r.model_dump(mode="json") for r in router_results
                ],
            )
        logger.info(
            f"[scratch/stage3a] Sequential router mapping finished in "
            f"{int((time.perf_counter() - sequential_start) * 1000)}ms "
            f"({len(router_ids)} router(s))."
        )
    else:
        logger.info(
            f"[scratch/stage3a] Running {len(router_ids)} per-router isolated calls "
            f"in parallel: {router_ids}"
        )
        parallel_start = time.perf_counter()
        router_results = list(
            await asyncio.gather(
                *[_stage3a_per_router(rid, state, api_key) for rid in router_ids]
            )
        )
        logger.info(
            f"[scratch/stage3a] Parallel router mapping finished in "
            f"{int((time.perf_counter() - parallel_start) * 1000)}ms "
            f"({len(router_ids)} router(s))."
        )

    router_conditions: Dict[str, Dict[str, str]] = {}
    for result in router_results:
        router_conditions[result.routerNodeId] = {
            e.to: e.condition for e in result.edges
        }

    assembled: List[MappedEdge] = []
    for sk_edge in skeleton.edges:
        if sk_edge.kind == "sequential" or sk_edge.fromNode not in router_conditions:
            assembled.append(MappedEdge(
                fromNode=sk_edge.fromNode,
                to=sk_edge.to,
                kind=sk_edge.kind,
                condition=None,
            ))
        else:
            cond = router_conditions[sk_edge.fromNode].get(sk_edge.to)
            assembled.append(MappedEdge(
                fromNode=sk_edge.fromNode,
                to=sk_edge.to,
                kind=sk_edge.kind,
                condition=cond,
            ))

    edge_map = EdgeMap(edges=assembled)
    logger.info(
        f"[scratch/stage3a] EdgeMap assembled: {len(edge_map.edges)} edge(s) "
        f"({len(router_ids)} router(s) contributed conditional conditions)."
    )
    return edge_map


# ===========================================================================
# SECTION 7 — STAGE 4: GRAPH ASSEMBLER (pure code)
# ===========================================================================

def _condition_to_state_notation(condition: str, state_schema: StateSchema) -> str:
    """
    Translate "nodeId.paramName op value" → "state['nodeId__paramName'] op value"
    for router function emission in Stage 6.
    Also normalises JSON-style boolean literals (true/false/null) to Python equivalents.
    """
    _REF_PATTERN = re.compile(r'\b([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\b')

    def replacer(m: re.Match) -> str:
        node_id = m.group(1)
        param = m.group(2)
        state_key = f"{node_id}__{param}"
        # Check if this is a valid state field
        for f in state_schema.fields:
            if f.fieldName == state_key:
                return f'state["{state_key}"]'
        # Not a state field — return as-is (literal or non-state ref)
        return m.group(0)

    translated = _REF_PATTERN.sub(replacer, condition)

    # Normalise JSON booleans/null → Python literals
    translated = re.sub(r'\btrue\b', 'True', translated)
    translated = re.sub(r'\bfalse\b', 'False', translated)
    translated = re.sub(r'\bnull\b', 'None', translated)

    return translated


def _registry_field_type_for_description(fname: str, fmeta: Any) -> str:
    """Map workflowRegistry field metadata to description schema types."""
    if isinstance(fmeta, dict):
        t = str(
            fmeta.get("type")
            or fmeta.get("datatype")
            or fmeta.get("dataType")
            or "string"
        )
        category = str(fmeta.get("category") or "").lower()
    else:
        t = str(fmeta or "string")
        category = ""
    if fname.endswith("_at") and (t == "number" or category in ("audit", "milestone")):
        return "Date"
    return t


def _parse_description_schema_field_list(body: str) -> List[Tuple[str, str]]:
    """Parse ``field:type, ...`` from inside a schema ``{...}`` block."""
    fields: List[Tuple[str, str]] = []
    seen_lower: set[str] = set()
    for m in re.finditer(r"([a-zA-Z_][\w]*)\s*:\s*([^,}{]+)", body or ""):
        fname = (m.group(1) or "").strip()
        ftype = (m.group(2) or "").strip()
        if not fname or fname.lower() in seen_lower:
            continue
        seen_lower.add(fname.lower())
        fields.append((fname, ftype or "string"))
    return fields


def _format_description_schema_field_list(fields: List[Tuple[str, str]]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in fields[:60])


def _merge_existing_schema_with_registry(
    existing: List[Tuple[str, str]],
    registry: Dict[str, str],
) -> Tuple[List[Tuple[str, str]], bool]:
    """
    Preserve skeleton/description fields exactly; append registry-only extras.

    Returns (merged_fields, registry_is_superset).
    registry_is_superset is True when every existing field name appears in registry
    with the exact same spelling — safe to update the schema block.
    """
    if not registry:
        return existing, False
    if not existing:
        return sorted(registry.items(), key=lambda item: item[0].lower()), True

    reg_keys = set(registry.keys())
    registry_is_superset = all(fname in reg_keys for fname, _ in existing)
    existing_lower = {fname.lower() for fname, _ in existing}
    merged = list(existing)
    for fname in sorted(registry.keys(), key=str.lower):
        if fname.lower() not in existing_lower:
            merged.append((fname, registry[fname]))
    return merged, registry_is_superset


def _build_authoritative_collection_field_types(
    workflow_registry: Optional[Dict[str, Any]],
    known_schemas: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, str]]:
    """
    Merge knownCollectionSchemas + workflowRegistry into coll -> {field: type}.
    Used as the registry side of description schema enrichment (Stage 4).
    """
    result: Dict[str, Dict[str, str]] = {}
    for coll_name, info in (known_schemas or {}).items():
        if not isinstance(coll_name, str) or coll_name == "__meta__":
            continue
        if not isinstance(info, dict):
            continue
        probe_fields = info.get("fields") or {}
        if not isinstance(probe_fields, dict) or not probe_fields:
            continue
        result[coll_name] = {str(k): str(v or "string") for k, v in probe_fields.items()}

    reg = _normalize_workflow_registry(workflow_registry)
    for coll_name, info in (reg.get("collections") or {}).items():
        if not isinstance(coll_name, str) or not isinstance(info, dict):
            continue
        reg_fields = info.get("fields") or {}
        if not isinstance(reg_fields, dict) or not reg_fields:
            continue
        coll_fields = result.setdefault(coll_name, {})
        for fname, fmeta in reg_fields.items():
            if fname:
                coll_fields[str(fname)] = _registry_field_type_for_description(
                    str(fname), fmeta
                )
    return result


_COLLECTION_SCHEMA_BLOCK_RE = re.compile(
    r"((?:MongoDB\s+)?Collection\s+'([^']+)'\s+schema\s*:\s*)\{[^}]*\}",
    re.IGNORECASE,
)

_MONGO_COLLECTION_MENTION_RE = re.compile(
    r"(?:mongodb\s+)?collection\s+'([^']+)'",
    re.IGNORECASE,
)


def _description_explicitly_mentions_collection(description: str, coll_name: str) -> bool:
    """
    True only when the description names this collection as a Mongo collection,
    not merely as a workflow param / list (e.g. mcq_questions payload).
    """
    if not description or not coll_name:
        return False
    needle = coll_name.lower()
    for m in _MONGO_COLLECTION_MENTION_RE.finditer(description):
        if (m.group(1) or "").strip().lower() == needle:
            return True
    if re.search(
        r"(?:mongodb\s+)?collection\s+" + re.escape(coll_name) + r"\b",
        description,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _enrich_generation_description_with_persistence_schemas(
    description: str,
    workflow_registry: Optional[Dict[str, Any]],
    known_schemas: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Append Stage 1.7 registry fields into generationDescription schema stanzas.

    Skeleton/description schema fields are authoritative — never renamed or removed.
    Registry extras (e.g. created_at) are appended only when the registry contains
    every existing description field with the exact same name (superset guard).
    """
    if not description:
        return description or ""

    merged = _build_authoritative_collection_field_types(
        workflow_registry, known_schemas
    )
    if not merged:
        return description

    coll_lookup = {k.lower(): (k, v) for k, v in merged.items()}
    desc = description

    def _replace_schema_block(m: re.Match[str]) -> str:
        prefix = m.group(1)
        coll_raw = m.group(2)
        entry = coll_lookup.get(coll_raw.lower())
        if not entry:
            return m.group(0)
        _, registry_fields = entry
        block = m.group(0)
        brace_start = block.rfind("{")
        brace_end = block.rfind("}")
        existing_body = block[brace_start + 1:brace_end] if brace_start >= 0 else ""
        existing_fields = _parse_description_schema_field_list(existing_body)
        merged_fields, registry_is_superset = _merge_existing_schema_with_registry(
            existing_fields, registry_fields
        )
        if not registry_is_superset:
            missing = [n for n, _ in existing_fields if n not in registry_fields]
            logger.warning(
                "[scratch/stage4] Schema enrichment skipped for collection '%s': "
                "registry missing description field(s) %s — keeping skeleton names.",
                coll_raw,
                missing,
            )
            return m.group(0)
        inner = _format_description_schema_field_list(merged_fields)
        return f"{prefix}{{{inner}}}"

    desc = _COLLECTION_SCHEMA_BLOCK_RE.sub(_replace_schema_block, desc)

    append_parts: List[str] = []
    for coll_name, fields in merged.items():
        if not fields:
            continue
        if not _description_explicitly_mentions_collection(desc, coll_name):
            continue
        schema_pat = re.escape(coll_name) + r"[^\n]{0,80}schema\s*:"
        if re.search(schema_pat, desc, flags=re.IGNORECASE):
            continue
        parts = [f"{k}:{v}" for k, v in sorted(fields.items())[:60]]
        append_parts.append(
            f"Collection '{coll_name}' schema: {{{', '.join(parts)}}}."
        )
    if append_parts:
        desc = desc.rstrip() + " " + " ".join(append_parts)
    return desc


def _stage4_assemble_graph(state: ScratchPlanningState) -> Dict[str, Any]:
    """
    Stage 4: Merge SkeletonPlan + NodeSchemas + StateSchema + EdgeMap
    into a GraphDefinition-compatible dict.

    All non-router, non-end nodes get:
      - capabilityRef.capabilityId = "GENERATE:{PascalCaseName}"
      - nodeType = "generate" (via capabilityRef)
      - config.stateReads: state fields read by this node (from inputBindings)
      - config.stateWrites: state fields written by this node (from outputParams)
      - config.generation: GenerationSpec with inputs/outputs

    Router nodes get:
      - config.router with routingRules derived from EdgeMap conditions

    Returns a dict that can be parsed as GraphDefinition by the existing system.
    """
    skeleton = state.skeletonPlan
    state_schema = state.stateSchema
    edge_map = state.edgeMap
    node_schemas = {s.nodeId: s for s in state.nodeSchemas}

    # ── Build state field lookup ──────────────────────────────────────────────
    field_lookup: Dict[str, StateField] = {
        f.fieldName: f for f in state_schema.fields
    }

    # ── Build outgoing edge lookup for routers ────────────────────────────────
    # edge_map edges keyed by fromNode
    router_edges: Dict[str, List[MappedEdge]] = defaultdict(list)
    for e in edge_map.edges:
        router_edges[e.fromNode].append(e)

    # ── Translate edge conditions to state notation ───────────────────────────
    def _translate_condition(cond: Optional[str]) -> Optional[str]:
        if not cond:
            return None
        return _condition_to_state_notation(cond, state_schema)

    graph_id = f"graph_{state.phaseId}_{uuid.uuid4().hex[:8]}"
    graph_nodes: List[Dict[str, Any]] = []
    graph_edges: List[Dict[str, Any]] = []
    all_stage4_binding_errors: List[str] = []

    skeleton_node_map = {n.nodeId: n for n in skeleton.nodes}

    for skel_node in skeleton.nodes:
        nid = skel_node.nodeId

        # ── END node ─────────────────────────────────────────────────────────
        if skel_node.type == "end":
            graph_nodes.append({
                "nodeId": nid,
                "type": "end",
                "name": skel_node.name,
                "stepId": skel_node.stepId,
                "capabilityRef": None,
                "config": {
                    "inputBindings": {},
                    "declaredOutputs": [],
                    "stateReads": [],
                    "stateWrites": [],
                    "router": None,
                    "generation": None,
                    "generationDescription": None,
                },
            })
            continue

        # ── ROUTER node ───────────────────────────────────────────────────────
        if skel_node.type == "router":
            outgoing = router_edges.get(nid, [])
            routing_rules: List[Dict[str, Any]] = []
            default_target: Optional[str] = None
            fallback_target: Optional[str] = None

            for e in outgoing:
                if e.kind == "conditional" and e.condition:
                    translated = _translate_condition(e.condition)
                    routing_rules.append({
                        "condition": translated or e.condition,
                        "targetNodeId": e.to,
                    })
                    if default_target is None:
                        default_target = e.to
                    fallback_target = e.to

            graph_nodes.append({
                "nodeId": nid,
                "type": "router",
                "name": skel_node.name,
                "stepId": skel_node.stepId,
                "capabilityRef": None,
                "config": {
                    "inputBindings": {},
                    "declaredOutputs": [],
                    "stateReads": [],
                    "stateWrites": [],
                    "router": {
                        "routingRules": routing_rules,
                        "defaultTarget": default_target,
                        "fallbackTarget": fallback_target,
                    },
                    "generation": None,
                    "generationDescription": None,
                },
            })
            continue

        # ── REGULAR node (tool | ui_screen) — all become GENERATE ────────────
        schema = node_schemas.get(nid)
        if schema is None:
            logger.warning(f"[scratch/stage4] No schema found for node '{nid}' — skipping.")
            continue

        # stateReads: resolve inputBindings → state field names
        state_reads: List[str] = []
        input_bindings: Dict[str, str] = {}
        stage4_binding_errors: List[str] = []
        for ip in schema.inputParams:
            if len(ip.inputBindings) > 1:
                raise ValueError(
                    f"Node '{nid}' inputParam '{ip.name}': multiple inputBindings entries — "
                    f"use pick: syntax in a single boundTo, not multiple list entries."
                )
            if ip.inputBindings:
                binding = ip.inputBindings[0]
                bound_to = binding.boundTo
                if bound_to.startswith("global."):
                    input_bindings[ip.name] = f"{{{{{bound_to}}}}}"
                elif bound_to.startswith("pick:"):
                    state_reads.extend(_binding_refs_to_state_reads(bound_to, state_schema))
                    input_bindings[ip.name] = f"{{{{{bound_to}}}}}"
                else:
                    state_reads.extend(_binding_refs_to_state_reads(bound_to, state_schema))
                    sk = _binding_to_state_key(bound_to, state_schema)
                    if sk:
                        input_bindings[ip.name] = f"{{{{{bound_to}}}}}"
                    elif ip.required:
                        stage4_binding_errors.append(
                            f"Node '{nid}' required input '{ip.name}': binding '{bound_to}' could not be resolved."
                        )
                        input_bindings[ip.name] = ""
                    # optional unresolved: omit binding key (treated as absent)
            else:
                if ip.required:
                    stage4_binding_errors.append(
                        f"Node '{nid}' required input '{ip.name}' has no binding."
                    )
                    input_bindings[ip.name] = ""
                else:
                    input_bindings[ip.name] = ""  # optional only

        all_stage4_binding_errors.extend(stage4_binding_errors)

        # stateWrites: all outputParams that appear in StateSchema
        state_writes: List[str] = []
        for op in schema.outputParams:
            expected_key = f"{nid}__{op.name}"
            if expected_key in field_lookup:
                state_writes.append(expected_key)

        # generation spec inputs/outputs (use NodeSchema params)
        gen_inputs = [_dump_node_param_io(ip, data_type_key="dataType") for ip in schema.inputParams]
        gen_outputs = [_dump_node_param_io(op, data_type_key="dataType") for op in schema.outputParams]

        # Pascal case for capability ID
        pascal_name = "".join(w.capitalize() for w in nid.split("_"))
        gen_description = _enrich_generation_description_with_persistence_schemas(
            skel_node.description,
            state.workflowRegistry,
            state.knownCollectionSchemas,
        )

        graph_nodes.append({
            "nodeId": nid,
            "type": skel_node.type,   # "tool" or "ui_screen"
            "name": skel_node.name,
            "stepId": skel_node.stepId,
            "capabilityRef": {
                "capabilityId": f"GENERATE:{pascal_name}",
                "kind": skel_node.type,
                "version": None,
                "source": None,
                "class_path": None,
                "wrapper_class": None,
                "wrapper_kwarg": None,
            },
            "config": {
                "inputBindings": input_bindings,
                "declaredOutputs": gen_outputs,
                "stateReads": list(dict.fromkeys(state_reads)),  # deduplicated, ordered
                "stateWrites": state_writes,
                "router": None,
                "generation": {
                    "kind": skel_node.type,
                    "stepId": skel_node.stepId or "",
                    "description": gen_description,
                    "inputs": gen_inputs,
                    "outputs": gen_outputs,
                },
                "generationDescription": gen_description,
                # intentDescription is set here once and must never be overwritten by
                # binding-refresh, IR-sync, or plan/update paths (see GraphNodeConfig).
                "intentDescription": gen_description,
            },
        })
        if skel_node.type == "tool":
            pinned_ops = extract_provider_operations_from_text(
                gen_description or "",
                skel_node.description or "",
                state.stage0Context or "",
            )
            if pinned_ops:
                graph_nodes[-1]["config"]["providerOperations"] = operations_to_dicts(
                    pinned_ops
                )
                logger.info(
                    "[scratch/stage4] node %s pinned %s providerOperation(s)",
                    nid,
                    len(pinned_ops),
                )

    # ── Assemble edges ────────────────────────────────────────────────────────
    # IMPORTANT: Edge conditions are stored in "nodeId.param op value" format
    # (as produced by Stage 3a).  Do NOT translate them to state["nodeId__param"]
    # notation here — that format is only valid for the scratch planner's own
    # Stage 6 code generator.  The main executor uses _generate_langgraph_code_for_phases
    # whose _evaluate_condition helper resolves "nodeId.param" refs from
    # state['node_outputs']['nodeId']['param'].  If conditions were translated to
    # state["key"] format, eval(cond, {}, {}) would raise NameError (state is not
    # in the empty eval namespace) and every router would silently always return False.
    for me in edge_map.edges:
        graph_edges.append({
            "fromNode": me.fromNode,
            "to": me.to,
            "kind": me.kind,
            "condition": me.condition,  # keep original "nodeId.param op value" format
        })

    # ── Collect requiredGlobalInputs ──────────────────────────────────────────
    required_globals: List[str] = []
    for node_dict in graph_nodes:
        for _param, binding_val in (node_dict.get("config") or {}).get("inputBindings", {}).items():
            if not isinstance(binding_val, str):
                continue
            if "{{" in binding_val:
                inner = binding_val.strip()
                inner_body = (
                    inner[2:-2].strip()
                    if inner.startswith("{{") and inner.endswith("}}")
                    else inner
                )
                if inner_body.startswith("pick:"):
                    for entry in _parse_pick_refs(inner_body):
                        if entry.startswith("global."):
                            logger.warning(
                                f"[scratch/stage4] Node '{node_dict.get('nodeId')}' param "
                                f"'{_param}': pick binding contains global ref '{entry}' — "
                                f"globals must not appear inside pick lists."
                            )
                    continue
                for src, key in iter_template_binding_refs(binding_val):
                    if src != "global" or not key:
                        continue
                    # Namespaced prior-phase keys (nodeId.field) are not caller start-inputs.
                    if "." in key:
                        continue
                    if key not in required_globals:
                        required_globals.append(key)

    type_counts: Dict[str, int] = defaultdict(int)
    for n in graph_nodes:
        type_counts[n.get("type", "unknown")] += 1
    conditional_edges = sum(1 for e in graph_edges if e.get("kind") == "conditional")
    logger.info(
        f"[scratch/stage4] Graph assembled graphId={graph_id}: "
        f"{len(graph_nodes)} node(s) {dict(type_counts)}, "
        f"{len(graph_edges)} edge(s) ({conditional_edges} conditional), "
        f"{len(state_schema.fields)} state field(s), "
        f"{len(required_globals)} required global input(s)."
    )
    if all_stage4_binding_errors:
        _log_binding_audit(
            "stage4_assembler_binding_errors",
            phaseId=state.phaseId,
            graphId=graph_id,
            errors=all_stage4_binding_errors,
        )
    _log_binding_audit(
        "stage4_assembled_bindings",
        phaseId=state.phaseId,
        graphId=graph_id,
        requiredGlobals=required_globals,
        bindings=_summarize_graph_input_bindings({
            "nodes": graph_nodes,
        }),
    )
    logger.debug(
        f"[scratch/stage4] Graph structure graphId={graph_id}:\n"
        + _json_compact(
            {
                "graphId": graph_id,
                "entryNodeId": skeleton.entryNodeId,
                "endNodeId": skeleton.endNodeId,
                "nodes": [
                    {
                        "nodeId": n.get("nodeId"),
                        "type": n.get("type"),
                        "stepId": n.get("stepId"),
                        "stateReads": (n.get("config") or {}).get("stateReads", []),
                        "stateWrites": (n.get("config") or {}).get("stateWrites", []),
                    }
                    for n in graph_nodes
                ],
                "edges": [
                    {
                        "from": e.get("fromNode"),
                        "to": e.get("to"),
                        "kind": e.get("kind"),
                        "condition": e.get("condition"),
                    }
                    for e in graph_edges
                ],
            },
            max_chars=20000,
        )
    )

    return {
        "graphId": graph_id,
        "phaseId": state.phaseId,
        "phaseName": state.phaseName,
        "name": state.phaseName,
        "description": state.phaseDescription,
        "goal": state.phaseDescription,
        "nodes": graph_nodes,
        "edges": graph_edges,
        "entryNodeId": skeleton.entryNodeId,
        "endNodeId": skeleton.endNodeId,
        "requiredGlobalInputs": required_globals,
        # stateSchema embedded for Stage 6
        "_stateSchema": {
            "fields": [f.model_dump() for f in state_schema.fields]
        },
        "_stage4BindingErrors": all_stage4_binding_errors,
    }


# ===========================================================================
# SECTION 8 — STAGE 4b: FINAL GRAPH VALIDATION (programmatic)
# ===========================================================================

def _stage4b_validate_graph(
    graph_dict: Dict[str, Any],
    state: ScratchPlanningState,
) -> Tuple[List[str], List[str]]:
    """
    Stage 4b: Final programmatic validation of the assembled graph.
    Returns (errors, warnings). Errors are fatal — caller must abort on non-empty errors.
    """
    errors: List[str] = []
    warnings: List[str] = []
    logger.info(
        f"[scratch/stage4b] Validating assembled graph "
        f"phase={graph_dict.get('phaseId')} nodes={len(graph_dict.get('nodes', []))} "
        f"edges={len(graph_dict.get('edges', []))}"
    )
    state_schema = state.stateSchema
    field_names = {f.fieldName for f in state_schema.fields}

    node_ids = {n["nodeId"] for n in graph_dict["nodes"]}
    entry = graph_dict["entryNodeId"]
    end = graph_dict["endNodeId"]

    if entry not in node_ids:
        warnings.append(f"entryNodeId '{entry}' not found in assembled graph nodes.")
    if end not in node_ids:
        warnings.append(f"endNodeId '{end}' not found in assembled graph nodes.")

    # Track state field writers — no field should be written by > 1 node
    field_writers: Dict[str, str] = {}

    for n in graph_dict["nodes"]:
        nid = n["nodeId"]
        cfg = n.get("config") or {}

        # stateReads reference declared fields
        for sr in cfg.get("stateReads") or []:
            if sr not in field_names:
                warnings.append(
                    f"Node '{nid}' stateReads references '{sr}' "
                    f"which is not in StateSchema."
                )

        # stateWrites reference declared fields, no duplicate writers
        for sw in cfg.get("stateWrites") or []:
            if sw not in field_names:
                warnings.append(
                    f"Node '{nid}' stateWrites references '{sw}' "
                    f"which is not in StateSchema."
                )
            if sw in field_writers:
                warnings.append(
                    f"State field '{sw}' is written by both '{field_writers[sw]}' "
                    f"and '{nid}' — namespace conflict."
                )
            else:
                field_writers[sw] = nid

        # Required inputs must have a non-empty binding
        gen = cfg.get("generation") or {}
        for ip in gen.get("inputs") or []:
            ip_name = ip.get("name")
            if not ip_name or not ip.get("required"):
                continue
            binding_val = (cfg.get("inputBindings") or {}).get(ip_name)
            if binding_val is None or binding_val == "":
                err = (
                    f"Node '{nid}' required input '{ip_name}' has no resolved binding "
                    f"— required inputs must bind to upstream node or global.*"
                )
                errors.append(err)
                _log_binding_audit(
                    "stage4b_empty_required",
                    phaseId=graph_dict.get("phaseId"),
                    nodeId=nid,
                    param=ip_name,
                )

    # Every non-end node has at least one outgoing edge
    out_counts: Dict[str, int] = defaultdict(int)
    for e in graph_dict["edges"]:
        out_counts[e["fromNode"]] += 1

    for n in graph_dict["nodes"]:
        nid = n["nodeId"]
        if n["type"] != "end" and out_counts.get(nid, 0) == 0:
            warnings.append(
                f"Node '{nid}' (type={n['type']}) has no outgoing edges."
            )

    # Reachability from entry
    adj: Dict[str, List[str]] = defaultdict(list)
    for e in graph_dict["edges"]:
        adj[e["fromNode"]].append(e["to"])
    reachable: set = set()
    queue = deque([entry])
    while queue:
        cur = queue.popleft()
        if cur in reachable:
            continue
        reachable.add(cur)
        for nxt in adj.get(cur, []):
            if nxt not in reachable:
                queue.append(nxt)
    unreachable = node_ids - reachable
    for nid in sorted(unreachable):
        warnings.append(f"Node '{nid}' is unreachable from entry '{entry}'.")

    if errors:
        logger.error(
            f"[scratch/stage4b] Validation produced {len(errors)} error(s)."
        )
    if warnings:
        logger.warning(
            f"[scratch/stage4b] Validation produced {len(warnings)} warning(s)."
        )
    if not errors and not warnings:
        logger.info("[scratch/stage4b] Validation passed with no errors or warnings.")
        _log_binding_audit(
            "stage4b_validation_passed",
            phaseId=graph_dict.get("phaseId"),
            bindings=_summarize_graph_input_bindings(graph_dict),
        )

    return errors, warnings


# ===========================================================================
# SECTION 9 — STAGE 6: LANGGRAPH CODE EMISSION (deterministic)
# ===========================================================================

_PYTHON_TYPE_MAP: Dict[str, str] = {
    "string": "str",
    "str": "str",
    "boolean": "bool",
    "bool": "bool",
    "number": "float",
    "float": "float",
    "integer": "int",
    "int": "int",
    "object": "Dict[str, Any]",
    "dict": "Dict[str, Any]",
    "array": "List[Any]",
    "list": "List[Any]",
    "any": "Any",
    "Any": "Any",
}


def _to_py_type(
    datatype: str,
    item_schema: Optional[List[NodeParam]] = None,
    *,
    item_type: Optional[str] = None,
    scalar_item_datatype: Optional[str] = None,
) -> str:
    """
    Map a param datatype to its Python type hint string.

    Scalar lists → List[str]/List[bool]/List[float]
    Enum lists   → List[str]
    Record lists → List[Dict[str, Any]]
    """
    if datatype == "list":
        effective = item_type or ("record" if item_schema else None)
        if effective == "scalar":
            inner = _PYTHON_TYPE_MAP.get((scalar_item_datatype or "string").lower(), "str")
            return f"List[{inner}]"
        if effective == "enum":
            return "List[str]"
        return "List[Dict[str, Any]]"
    return _PYTHON_TYPE_MAP.get(datatype.lower(), "Any")


def _state_key_to_var(state_key: str) -> str:
    """'nodeId__paramName' → 'param_name' (use the param portion as the local var name)."""
    parts = state_key.split("__", 1)
    return parts[1] if len(parts) == 2 else state_key


def _stage6_emit_langgraph_code(state: ScratchPlanningState) -> str:
    """
    Stage 6: Emit deterministic LangGraph Python code from the assembled graph.

    Produces:
    1. WorkflowState TypedDict — all fields from StateSchema, namespaced.
    2. Node functions — reads from state via stateReads, writes via stateWrites.
    3. Router functions — conditions translated to state["nodeId__param"] notation.
    4. Graph wiring — StateGraph, add_node, add_edge, add_conditional_edges.
    """
    graph_dict = state.graphDefinition
    state_schema = state.stateSchema
    gap_resolutions = getattr(state, "gapResolutions", None) or {}
    phase_name = state.phaseName
    phase_id = state.phaseId

    lines: List[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        "# ============================================================",
        f"# LangGraph Workflow — {phase_name}",
        f"# Phase: {phase_id}",
        f"# Generated by scratch_graph_planner (no-capabilities mode)",
        "# ============================================================",
        "",
        "from __future__ import annotations",
        "from typing import Any, Dict, List, Optional, TypedDict",
        "from langgraph.graph import StateGraph, END",
        "",
    ]

    # ── WorkflowState TypedDict ───────────────────────────────────────────────
    lines += [
        "class WorkflowState(TypedDict, total=False):",
    ]
    if state_schema and state_schema.fields:
        for f in state_schema.fields:
            py_type = _to_py_type(
                f.datatype,
                f.itemSchema,
                item_type=f.itemType,
                scalar_item_datatype=f.scalarItemDatatype,
            )
            lines.append(f"    {f.fieldName}: {py_type}")
    else:
        lines.append("    pass  # no state fields derived")
    lines.append("")
    lines.append("")

    # ── Node functions ────────────────────────────────────────────────────────
    node_map = {n["nodeId"]: n for n in graph_dict["nodes"]}

    for node_dict in graph_dict["nodes"]:
        nid = node_dict["nodeId"]
        node_type = node_dict["type"]
        cfg = node_dict.get("config") or {}

        if node_type == "end":
            continue

        # ── ROUTER function ───────────────────────────────────────────────────
        if node_type == "router":
            router_cfg = cfg.get("router") or {}
            routing_rules = router_cfg.get("routingRules") or []
            default_target = router_cfg.get("defaultTarget")
            fallback_target = router_cfg.get("fallbackTarget")

            lines += [
                f"def {nid}(state: WorkflowState) -> str:",
                f'    """Router: {node_dict["name"]}"""',
            ]
            for rule in routing_rules:
                condition = rule.get("condition", "")
                target = rule.get("targetNodeId", "")
                lines.append(f"    if {condition}:")
                lines.append(f'        return "{target}"')
            if default_target:
                lines.append(f'    return "{default_target}"')
            elif fallback_target:
                lines.append(f'    return "{fallback_target}"')
            else:
                lines.append('    return "end"')
            lines.append("")
            lines.append("")
            continue

        # ── GENERATE node function ────────────────────────────────────────────
        state_reads = cfg.get("stateReads") or []
        state_writes = cfg.get("stateWrites") or []
        input_bindings = cfg.get("inputBindings") or {}
        gen = cfg.get("generation") or {}
        gap = gap_resolutions.get(nid)

        lines += [
            f"def {nid}(state: WorkflowState) -> dict:",
            f'    """',
            f"    {node_dict['name']}",
            f"    {gen.get('description', '')}",
        ]
        if gap and gap.generatedLogic:
            logic_lines = gap.generatedLogic.splitlines()
            for ll in logic_lines:
                lines.append(f"    {ll}")
        if gap and gap.implementationHints:
            lines.append("    Hints:")
            for hint in gap.implementationHints:
                lines.append(f"      - {hint}")
        lines.append('    """')

        # State reads (coalesce when pick produces multiple keys for same param)
        # Detect which input params use indexed binding — they emit their own access expr.
        indexed_param_assigns: List[Tuple[str, str]] = []  # [(param_name, python_expr), ...]
        for param_name, bval in input_bindings.items():
            inner = _extract_binding_inner(bval)
            if inner and not inner.startswith("global.") and not inner.startswith("pick:"):
                bref = _parse_bound_to(inner)
                if bref and bref.index is not None and bref.item_field:
                    # Parent list is already in reads_by_var under bref.param_name;
                    # emit indexed element access after parent list read.
                    parent_var = bref.param_name
                    expr = f'({parent_var} or [])[{bref.index}]["{bref.item_field}"]'
                    indexed_param_assigns.append((param_name, expr))

        if state_reads or indexed_param_assigns:
            lines.append("    # Read from workflow state")
            reads_by_var: Dict[str, List[str]] = defaultdict(list)
            for sr in state_reads:
                var_name = _state_key_to_var(sr)
                reads_by_var[var_name].append(sr)
            for var_name, keys in reads_by_var.items():
                if len(keys) == 1:
                    lines.append(f'    {var_name} = state.get("{keys[0]}")')
                else:
                    or_parts = "\n        or ".join(
                        f'state.get("{k}")' for k in keys
                    )
                    lines.append(f"    {var_name} = (\n        {or_parts}\n    )")
            # Emit indexed scalar accesses after parent list reads
            for param_name, expr in indexed_param_assigns:
                lines.append(f"    {param_name} = {expr}")
            lines.append("")

        # Implementation stub
        lines.append("    # TODO: implement node logic")
        lines.append("    result = {}")
        lines.append("")

        # State writes
        if state_writes:
            lines.append("    # Write to workflow state")
            lines.append("    return {")
            for sw in state_writes:
                var_name = _state_key_to_var(sw)
                lines.append(f'        "{sw}": result.get("{var_name}"),')
            lines.append("    }")
        else:
            lines.append("    return {}")

        lines.append("")
        lines.append("")

    # ── Graph wiring ──────────────────────────────────────────────────────────
    entry = graph_dict["entryNodeId"]
    end_node = graph_dict["endNodeId"]

    lines += [
        "# ── Graph Construction ───────────────────────────────────────────",
        "workflow = StateGraph(WorkflowState)",
        "",
    ]

    # add_node for all non-end nodes
    for node_dict in graph_dict["nodes"]:
        nid = node_dict["nodeId"]
        if node_dict["type"] != "end":
            lines.append(f'workflow.add_node("{nid}", {nid})')
    lines.append("")

    # add edges
    # Group conditional edges by fromNode for add_conditional_edges
    conditional_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in graph_dict["edges"]:
        if e["kind"] == "conditional":
            conditional_groups[e["fromNode"]].append(e)

    sequential_froms = {
        e["fromNode"]
        for e in graph_dict["edges"]
        if e["kind"] == "sequential"
    }

    for e in graph_dict["edges"]:
        if e["kind"] == "sequential":
            to_node = "END" if e["to"] == end_node else f'"{e["to"]}"'
            lines.append(f'workflow.add_edge("{e["fromNode"]}", {to_node})')

    lines.append("")

    for from_node, cond_edges in conditional_groups.items():
        targets_map = ", ".join(
            f'"{e["to"]}": "{e["to"]}"'
            for e in cond_edges
        )
        lines.append(
            f'workflow.add_conditional_edges('
        )
        lines.append(f'    "{from_node}",')
        lines.append(f"    {from_node},")
        lines.append(f"    {{{targets_map}}}")
        lines.append(")")

    lines += [
        "",
        f'workflow.set_entry_point("{entry}")',
        "",
        "app = workflow.compile()",
        "",
    ]

    code = "\n".join(lines)
    logger.info(
        f"[scratch/stage6] LangGraph code emitted: {len(code)} chars, "
        f"{len(lines)} line(s), phase={phase_id}"
    )
    return code


# ===========================================================================
# SECTION 10 — WORKFLOW PERSISTENCE REGISTRY (cross-phase handoff)
# ===========================================================================

_PERSISTENCE_EXTRACT_MODEL = "gemini-3.5-flash-lite"

_WRITE_KW_RE = re.compile(
    r"\b(insert|inserts|save|saves|store|stores|upsert|update|updates|persist|write\s+to|create\s+record)\b",
    re.IGNORECASE,
)
_READ_KW_RE = re.compile(
    r"\b(fetch|fetches|load|loads|query|queries|find|finds|get|gets|retrieve|retrieves|read\s+from)\b",
    re.IGNORECASE,
)
_PERSIST_KW_RE = re.compile(
    r"\b(mongodb|mongo\s*db|database|collection)\b",
    re.IGNORECASE,
)


class PersistenceFieldExtract(BaseModel):
    name: str
    type: str = "string"
    evidence: str = ""

class PersistenceRelationshipExtract(BaseModel):
    from_field: str
    to_collection: str
    to_field: str

class PersistenceOperationExtract(BaseModel):
    kind: Literal["write", "read"]
    collection: str
    fields: List[PersistenceFieldExtract] = Field(default_factory=list)
    filter_keys: List[str] = Field(default_factory=list)
    relationships: List[PersistenceRelationshipExtract] = Field(default_factory=list)
    evidence: str = ""


class NodePersistenceExtract(BaseModel):
    nodeId: str
    operations: List[PersistenceOperationExtract] = Field(default_factory=list)


class PhasePersistenceExtractResponse(BaseModel):
    extractions: List[NodePersistenceExtract] = Field(default_factory=list)


def _empty_workflow_registry() -> Dict[str, Any]:
    return {"version": 1, "collections": {}}


def _normalize_workflow_registry(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return _empty_workflow_registry()
    out = _empty_workflow_registry()
    out["version"] = int(raw.get("version") or 1)
    cols = raw.get("collections")
    if isinstance(cols, dict):
        out["collections"] = copy.deepcopy(cols)
    return out


def _build_step_description_lookup(steps: List[LogicalStepLite]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for s in steps or []:
        sid = (s.stepId or "").strip()
        if sid:
            lookup[sid] = (s.description or "").strip()
    return lookup


def _node_description_text(node: Dict[str, Any]) -> str:
    cfg = node.get("config") or {}
    return (
        (cfg.get("generationDescription") or "").strip()
        or (node.get("name") or "").strip()
    )


def _node_declared_output_names(node: Dict[str, Any]) -> List[str]:
    cfg = node.get("config") or {}
    names: List[str] = []
    for o in cfg.get("declaredOutputs") or []:
        if isinstance(o, dict) and o.get("name"):
            names.append(str(o["name"]))
    return names


def _is_db_crud_candidate(node: Dict[str, Any]) -> bool:
    ntype = (node.get("type") or "").strip()
    if ntype not in ("tool", "ui_screen"):
        return False
    text = _node_description_text(node)
    if not text:
        return False
    if _PERSIST_KW_RE.search(text):
        return True
    if _WRITE_KW_RE.search(text) or _READ_KW_RE.search(text):
        return True
    if re.search(r"\b\w+_info\b", text, re.IGNORECASE):
        return True
    return False


def _filter_db_candidate_nodes(
    graph_dict: Dict[str, Any],
    step_lookup: Dict[str, str],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for node in graph_dict.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if not _is_db_crud_candidate(node):
            continue
        step_id = (node.get("stepId") or "").strip()
        candidates.append({
            "nodeId": node.get("nodeId") or "",
            "name": node.get("name") or "",
            "type": node.get("type") or "",
            "description": _node_description_text(node),
            "declaredOutputs": _node_declared_output_names(node),
            "stepDescription": step_lookup.get(step_id, ""),
        })
    return [c for c in candidates if c.get("nodeId")]


def _format_registry_for_prompt(registry: Optional[Dict[str, Any]]) -> str:
    reg = _normalize_workflow_registry(registry)
    cols = reg.get("collections") or {}
    if not cols:
        return ""

    lines = [
        "",
        "═══════════════════════════════════════════════════════",
        "WORKFLOW PERSISTENCE REGISTRY (authoritative collection schemas)",
        "═══════════════════════════════════════════════════════",
        "When reading/writing shared collections, use EXACT MongoDB field names below.",
        "Listed fields are authoritative for name/type on that collection.",
        "Still include EVERY field named in the node description as a param (completeness).",
        "",
        "MONGO ↔ WORKFLOW NAME MAPPING — REQUIRED IN THE NODE DESCRIPTION",
        "Prefer the exact Mongo field name as the workflow param (no mapping needed).",
        "If a workflow param/output uses a DIFFERENT name than the Mongo field, the node",
        "description MUST contain this exact stanza (codegen uses it as the write/read key):",
        "  MongoDB field mapping for collection '<coll>': <param> → <mongo_field>.",
        "Rules:",
        "  • Left side = workflow param/output name. Right side = ONE registry field below.",
        "  • Each mongo_field may appear at most once. Never map one param to two fields.",
        "  • Do NOT guess among similar fields (status vs current_status vs pipeline_status).",
        "    Pick the field the node actually writes/reads; name it on the right side.",
        "  • Identical names need no mapping line.",
        "  BAD:  Save ticket_status to the collection (which mongo key?).",
        "  BAD:  ticket_status → status, ticket_status → current_status  (ambiguous).",
        "  GOOD: Param ticket_status. MongoDB field mapping for collection 'ticket_info':",
        "        ticket_status → status.",
        "  GOOD: Param is named status (same as Mongo) — no mapping line.",
        "",
    ]
    for coll_name in sorted(cols.keys()):
        info = cols.get(coll_name) or {}
        if not isinstance(info, dict):
            continue
        fields = info.get("fields") or {}
        field_names = sorted(fields.keys()) if isinstance(fields, dict) else []
        filter_keys = info.get("filter_keys") or []
        if isinstance(filter_keys, set):
            filter_keys = sorted(filter_keys)
        writers = info.get("writers") or []
        readers = info.get("readers") or []
        lines.append(f"  Collection: {coll_name}")
        if field_names:
            typed = []
            for fname in field_names:
                fmeta = fields.get(fname)
                if isinstance(fmeta, dict):
                    ftype = fmeta.get("type") or fmeta.get("dataType") or "string"
                else:
                    ftype = str(fmeta or "string")
                typed.append(f"{fname}:{ftype}")
            lines.append(f"    fields: {', '.join(typed)}")
        if filter_keys:
            lines.append(f"    filter_keys: {', '.join(filter_keys)}")
        if writers:
            lines.append(f"    written_by: {', '.join(writers)}")
        if readers:
            lines.append(f"    read_by: {', '.join(readers)}")
        lines.append("")
    return "\n".join(lines)


# ===========================================================================
# SECTION 10b — STAGE 1.7 PERSISTENCE ARCHITECT (entity schema design)
# ===========================================================================

_PERSISTENCE_ARCHITECT_INTRO = (
    "You are the Persistence Architect for a multi-phase workflow builder. "
    "You design production-grade MongoDB collection schemas for entities this phase persists. "
    "You do NOT design node inputParams or outputParams — only collection field schemas."
)

_PERSISTENCE_ARCHITECT_RULES = """
═══════════════════════════════════════════════════════
PERSISTENCE ARCHITECT — RULES
═══════════════════════════════════════════════════════
Your job: for each persistent entity/collection touched by this phase, propose
fields a production-grade version of that collection needs — grounded in THIS
phase's workflow semantics (skeleton nodes + phase goal).

Questions to answer per entity (propose a field ONLY when the concern applies):
1. Lifecycle — Does this entity have meaningful state/progression?
2. Ownership — Does responsibility belong to a specific person/team/role?
3. Auditability — Would knowing who/when changed this entity matter?
4. Historical traceability — Do important values need tracking as they change?
5. Deletion semantics — Soft-delete / compliance implications vs hard delete?
6. Concurrency — Can multiple actors/processes modify this entity?
7. Milestones — Meaningful events whose timestamps will later be useful?
8. Relationships — Interaction with other persistent entities?
9. Query/reporting — Filter, search, or aggregate needs?
10. Derived vs stored — Prefer compute over persist when appropriate.

JUSTIFICATION STANDARD:
- Inference from workflow semantics is expected. Example: create → review → approve
  never literally says "track who approved," but an approval step is enough evidence
  for approved_by / approved_at.
- Do NOT propose fields with no tie to this phase's entities/steps.
- Do NOT dump a fixed universal field list onto every collection.
- A static lookup table may need almost none of the concerns above.

BUSINESS / DOMAIN FIELDS — DO NOT RE-PROPOSE:
- Skeleton node descriptions already define business/domain fields in their schema blocks.
- Those names are seeded into EXISTING FIELDS automatically — do NOT list them again.
- Your job is ONLY to propose NEW production-grade fields not already listed below.

EXISTING FIELDS ARE AUTHORITATIVE:
- Do NOT redefine, rename, or reinterpret fields listed under EXISTING FIELDS.
- Only propose NEW fields when this phase's semantics require them.
- If every concern is already covered, return { "entities": [] }.
- Prefer snake_case field names. datatype must be string | boolean | number only.
- Timestamp / audit fields (*_at): use datatype="number" (stored as epoch ms).

OUTPUT: Return ONLY JSON matching PersistencePlan
  { "entities": [ { "collection": "...", "fields": [
      { "name": "...", "datatype": "string|boolean|number",
        "category": "lifecycle|audit|ownership|business|...", "reason": "..." }
    ] } ] }
If nothing to add, return { "entities": [] }.
""".strip()

_PERSISTENCE_ARCHITECT_SYSTEM = (
    f"{_PERSISTENCE_ARCHITECT_INTRO}\n\n{_PERSISTENCE_ARCHITECT_RULES}"
)


def _skeleton_text_has_persistence_signal(text: str) -> bool:
    """
    Returns True only when skeleton text contains an explicit schema block —
    the strongest signal that Stage 1 planned real MongoDB persistence.
    Loose keyword matching (insert/save/collection) is intentionally not enough
    because those words also appear in non-DB contexts (e.g. "mcq_list" is a
    workflow param named 'collection' in a node description but is not a Mongo DB).
    """
    if not text or not text.strip():
        return False
    # Require an actual Collection schema block written by Stage 1 skeleton planner.
    if _COLLECTION_SCHEMA_BLOCK_RE.search(text):
        return True
    return False


def _collections_touched_by_skeleton(
    skeleton: Optional[SkeletonPlan],
    phase_description: str = "",
) -> Tuple[List[str], bool]:
    """
    Return (collection_names, has_persistence_signal) from skeleton node descriptions.
    Only collections explicitly named in a schema block count — loose word matches
    are excluded to avoid false positives (e.g. workflow param named 'collection').
    """
    if skeleton is None:
        return [], False
    names: List[str] = []
    seen: set[str] = set()
    signal = False
    for n in skeleton.nodes or []:
        if n.type not in ("tool", "ui_screen"):
            continue
        text = (n.description or "").strip()
        if not text:
            continue
        for m in _COLLECTION_SCHEMA_BLOCK_RE.finditer(text):
            coll = (m.group(2) or "").strip()
            if coll and coll not in seen:
                seen.add(coll)
                names.append(coll)
                signal = True
    return names, signal


def _format_existing_fields_for_architect(
    registry: Optional[Dict[str, Any]],
    known_schemas: Optional[Dict[str, Any]] = None,
) -> str:
    """EXISTING FIELDS block: name + category + reason (authoritative — no redesign)."""
    reg = _normalize_workflow_registry(registry)
    cols: Dict[str, Any] = dict(reg.get("collections") or {})

    # Seed probed / Process_Configs schemas so Stage 1.7 does not redefine them.
    for coll_name, info in (known_schemas or {}).items():
        if not isinstance(coll_name, str) or coll_name == "__meta__":
            continue
        if not isinstance(info, dict):
            continue
        probe_fields = info.get("fields") or {}
        if not isinstance(probe_fields, dict) or not probe_fields:
            continue
        entry = cols.setdefault(coll_name, {"fields": {}})
        fields_map = entry.setdefault("fields", {})
        if not isinstance(fields_map, dict):
            fields_map = {}
            entry["fields"] = fields_map
        for fname, ftype in probe_fields.items():
            if not fname or fname in fields_map:
                continue
            fields_map[str(fname)] = {
                "type": str(ftype or "string"),
                "category": "existing",
                "reason": "from known/probed collection schema",
            }

    if not cols:
        return "(none — no prior registry fields)"

    lines: List[str] = []
    for coll_name in sorted(cols.keys()):
        info = cols.get(coll_name) or {}
        if not isinstance(info, dict):
            continue
        fields = info.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            lines.append(f"  Collection `{coll_name}`: (no fields yet)")
            continue
        lines.append(f"  Collection `{coll_name}`:")
        for fname in sorted(fields.keys()):
            fmeta = fields.get(fname)
            if isinstance(fmeta, dict):
                ftype = fmeta.get("type") or fmeta.get("dataType") or "string"
                category = (fmeta.get("category") or "").strip() or "-"
                reason = (fmeta.get("reason") or "").strip() or "-"
            else:
                ftype = str(fmeta or "string")
                category, reason = "-", "-"
            lines.append(
                f"    - {fname} ({ftype}) category={category} reason={reason}"
            )
    return "\n".join(lines) if lines else "(none — no prior registry fields)"


def _merge_architect_proposals_into_registry(
    registry: Dict[str, Any],
    plan: PersistencePlan,
    *,
    phase_id: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Merge Stage 1.7 proposals into workflowRegistry.
    Existing field names are authoritative — never overwrite (case-insensitive).
    Returns (merged_registry, proposal_audit_rows).
    """
    reg = _normalize_workflow_registry(registry)
    cols: Dict[str, Any] = reg.setdefault("collections", {})
    proposals: List[Dict[str, Any]] = []

    for entity in plan.entities or []:
        coll = (entity.collection or "").strip()
        if not coll:
            continue
        entry = cols.setdefault(coll, {
            "fields": {},
            "filter_keys": [],
            "writers": [],
            "readers": [],
        })
        fields_map: Dict[str, Any] = entry.setdefault("fields", {})
        existing_lower = {str(k).lower(): k for k in fields_map.keys()}

        for fld in entity.fields or []:
            fname = (fld.name or "").strip()
            if not fname:
                continue
            datatype = (fld.datatype or "string").strip() or "string"
            if datatype not in ("string", "boolean", "number"):
                datatype = "string"
            category = (fld.category or "").strip()
            reason = (fld.reason or "").strip()
            skipped = fname.lower() in existing_lower
            row = {
                "phaseId": phase_id,
                "collection": coll,
                "field": fname,
                "datatype": datatype,
                "category": category,
                "reason": reason,
                "skipped": skipped,
            }
            proposals.append(row)
            _log_binding_audit("persistence_architect_proposal", **row)
            if skipped:
                continue
            fields_map[fname] = {
                "type": datatype,
                "category": category,
                "reason": reason,
                "writers": [],
                "readers": [],
                "source": "persistence_architect",
            }
            existing_lower[fname.lower()] = fname
    return reg, proposals


def _build_persistence_architect_user_prompt(
    state: ScratchPlanningState,
    target_collections: List[str],
) -> str:
    skeleton = state.skeletonPlan
    skeleton_block = (
        _format_skeleton_for_global_selection(skeleton)
        if skeleton is not None
        else "(no skeleton)"
    )
    targets = (
        "\n".join(f"  - {c}" for c in target_collections)
        if target_collections
        else "  (none named — infer entity/collection names from skeleton if persistence applies)"
    )
    return f"""PHASE: {state.phaseName}
GOAL : {state.phaseDescription}

SKELETON GRAPH FOR THIS PHASE:
{skeleton_block}

CANDIDATE COLLECTIONS TO ENRICH (from skeleton / phase text):
{targets}

EXISTING FIELDS (authoritative — do not redefine):
{_format_existing_fields_for_architect(
    state.workflowRegistry,
    state.knownCollectionSchemas,
)}

Propose ONLY NEW fields per entity that are not already in EXISTING FIELDS above.
Do not rename or duplicate skeleton schema field names.
Return PersistencePlan JSON only.
"""


def _seed_registry_from_skeleton_schemas(
    registry: Dict[str, Any],
    skeleton: Optional[SkeletonPlan],
) -> Dict[str, Any]:
    """
    Seed workflowRegistry collection fields from Stage 1 skeleton description
    schema blocks so Stage 1.7 and Stage 4 treat skeleton names as authoritative.
    """
    reg = _normalize_workflow_registry(registry)
    cols: Dict[str, Any] = reg.setdefault("collections", {})
    if skeleton is None:
        return reg
    for node in skeleton.nodes or []:
        if node.type not in ("tool", "ui_screen"):
            continue
        text = (node.description or "").strip()
        if not text:
            continue
        for m in _COLLECTION_SCHEMA_BLOCK_RE.finditer(text):
            coll = (m.group(2) or "").strip()
            if not coll:
                continue
            block = m.group(0)
            brace_start = block.rfind("{")
            brace_end = block.rfind("}")
            if brace_start < 0 or brace_end <= brace_start:
                continue
            fields = _parse_description_schema_field_list(block[brace_start + 1:brace_end])
            if not fields:
                continue
            entry = cols.setdefault(coll, {
                "fields": {},
                "filter_keys": [],
                "writers": [],
                "readers": [],
            })
            fields_map: Dict[str, Any] = entry.setdefault("fields", {})
            existing_lower = {str(k).lower() for k in fields_map.keys()}
            for fname, ftype in fields:
                if fname.lower() in existing_lower:
                    continue
                fields_map[fname] = {
                    "type": ftype or "string",
                    "category": "business",
                    "reason": "from skeleton node description schema",
                    "source": "skeleton",
                }
                existing_lower.add(fname.lower())
    return reg


async def _stage1_7_persistence_architect(
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Stage 1.7 — Design production-grade collection schemas into workflowRegistry.

    Runs after Stage 1 (needs skeleton) and before Stage 2a (so the enriched
    registry is visible in node-schema prompts). Never invents node params.
    On failure, leaves the registry unchanged (Stage 2a still works).
    """
    skeleton = state.skeletonPlan
    if skeleton is None:
        return _normalize_workflow_registry(state.workflowRegistry)

    # Skeleton schema blocks are authoritative — seed before architect / Stage 4.
    state.workflowRegistry = _seed_registry_from_skeleton_schemas(
        state.workflowRegistry, skeleton
    )

    touched, has_signal = _collections_touched_by_skeleton(
        skeleton, state.phaseDescription or ""
    )
    if not has_signal and not touched:
        logger.info(
            "[scratch/stage1.7] No persistence signal in skeleton — skipping architect."
        )
        return _normalize_workflow_registry(state.workflowRegistry)

    already = set(state.persistenceArchitectedCollections or [])
    targets = [c for c in touched if c]
    # Per-collection cache: only send collections not yet designed in this phase.
    # Cross-phase re-touches still run (fresh state each phase; merge skips
    # existing field names so prior-phase fields stay authoritative).
    new_targets = [c for c in targets if c not in already]
    if targets and not new_targets and state.persistenceArchitectCompleted:
        logger.info(
            "[scratch/stage1.7] All touched collections already architected — skip."
        )
        return _normalize_workflow_registry(state.workflowRegistry)
    call_targets = new_targets if new_targets else targets

    # Also treat known/probed collections mentioned in skeleton as targets.
    for k in (state.knownCollectionSchemas or {}).keys():
        if k == "__meta__" or not isinstance(k, str):
            continue
        k_l = k.lower()
        if any(k_l in (t or "").lower() for t in (call_targets or [])):
            continue
        # Include probed collection if skeleton text mentions it.
        skel_blob = " ".join(
            f"{n.name or ''} {n.description or ''}"
            for n in (skeleton.nodes or [])
            if n.type in ("tool", "ui_screen")
        ).lower()
        if k.lower() in skel_blob and k not in call_targets:
            call_targets.append(k)

    user_prompt = _build_persistence_architect_user_prompt(state, call_targets)
    full_prompt = f"{_PERSISTENCE_ARCHITECT_SYSTEM}\n\n{user_prompt}"
    cached_content = await _get_scratch_cached_content(
        api_key,
        _PERSISTENCE_ARCHITECT_MODEL,
        system_text=_PERSISTENCE_ARCHITECT_SYSTEM,
        cache_key_prefix="scratch_persistence_architect",
        display_name="scratch-persistence-architect-system",
    )

    last_error: Optional[str] = None
    plan: Optional[PersistencePlan] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw, parsed = await _scratch_llm_call(
                api_key,
                model=_PERSISTENCE_ARCHITECT_MODEL,
                sub_operation="stage1.7-persistence-architect",
                system_text=_PERSISTENCE_ARCHITECT_SYSTEM,
                user_prompt=user_prompt,
                full_prompt=full_prompt,
                cached_content=cached_content,
                project_id=state.projectId,
                org_id=state.orgId,
                subscriber_id=state.subscriberId,
                user_id=state.userId,
                phase_id=state.phaseId,
                attempt=attempt,
                metadata={
                    "stage": "1.7",
                    "target_collections": call_targets,
                },
                temperature=0.2,
                as_json=True,
                response_schema=PersistencePlan,
            )
            if parsed is not None and isinstance(parsed, PersistencePlan):
                plan = parsed
            else:
                data = _parse_json_robust(raw)
                plan = PersistencePlan(**data)
            break
        except BillingError:
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"[scratch/stage1.7] Attempt {attempt} error: {exc}")
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.0 * attempt)

    if plan is None:
        logger.warning(
            f"[scratch/stage1.7] Architect failed — registry unchanged "
            f"(last_error={last_error})"
        )
        state.validationWarnings.append(
            f"[stage1.7] Persistence architect failed: {last_error}"
        )
        return _normalize_workflow_registry(state.workflowRegistry)

    # Seed probed fields only for collections this plan touches (or call targets),
    # so EXISTING names dedupe without importing unrelated Stage 0 collections.
    relevant = {
        e.collection.strip()
        for e in (plan.entities or [])
        if (e.collection or "").strip()
    } | set(call_targets)
    seed_reg = _normalize_workflow_registry(state.workflowRegistry)
    for coll_name, info in (state.knownCollectionSchemas or {}).items():
        if not isinstance(coll_name, str) or coll_name == "__meta__":
            continue
        if coll_name not in relevant:
            continue
        if not isinstance(info, dict):
            continue
        probe_fields = info.get("fields") or {}
        if not isinstance(probe_fields, dict) or not probe_fields:
            continue
        entry = seed_reg.setdefault("collections", {}).setdefault(
            coll_name,
            {"fields": {}, "filter_keys": [], "writers": [], "readers": []},
        )
        fields_map = entry.setdefault("fields", {})
        lower_map = {str(k).lower(): k for k in fields_map.keys()}
        for fname, ftype in probe_fields.items():
            if not fname or str(fname).lower() in lower_map:
                continue
            fields_map[str(fname)] = {
                "type": str(ftype or "string"),
                "category": "existing",
                "reason": "from known/probed collection schema",
                "writers": [],
                "readers": [],
                "source": "known_schema",
            }
            lower_map[str(fname).lower()] = str(fname)

    merged, proposals = _merge_architect_proposals_into_registry(
        seed_reg, plan, phase_id=state.phaseId or ""
    )
    state.persistenceArchitectProposals = proposals
    designed = list(dict.fromkeys(
        list(state.persistenceArchitectedCollections or [])
        + [
            e.collection.strip()
            for e in (plan.entities or [])
            if (e.collection or "").strip()
        ]
        + call_targets
    ))
    state.persistenceArchitectedCollections = designed
    n_fields = sum(len(e.fields or []) for e in (plan.entities or []))
    n_applied = sum(1 for p in proposals if not p.get("skipped"))
    logger.info(
        f"[scratch/stage1.7] Architect proposed {n_fields} field(s) across "
        f"{len(plan.entities or [])} entit(y/ies); applied={n_applied} "
        f"skipped={len(proposals) - n_applied}; "
        f"registry collections={len(merged.get('collections') or {})}"
    )
    return merged


_PERSISTENCE_EXTRACT_INTRO = (
    "You extract MongoDB persistence contracts from workflow node specifications."
)

_PERSISTENCE_EXTRACT_RULES = """
RULES — CRITICAL:
1. Only report collection names and fields that are EXPLICITLY supported by the node's
   description, stepDescription, or declaredOutputs. Include a short evidence quote.
2. If persistence is unclear or not mentioned, return an empty operations list for that node.
3. Do NOT invent collection names, field names, or operations.
4. For write operations: fields should match data saved to the collection (often declaredOutputs).
5. For read operations: report collection + filter_keys only unless MongoDB field names are
   explicitly stated in the description. Do NOT use output param prefixes like db_ as MongoDB keys.
6. collection names must be bare identifiers (e.g. ticket_info), not prefixed with db_.
7. For relationships: The to_collection MUST be a different collection than the current one. A field can never reference itself.
""".strip()

_PERSISTENCE_EXTRACT_OUTPUT = (
    "Return JSON matching the schema: extractions[] with nodeId and operations[].\n"
    "Each operation: kind (write|read), collection, fields[] (name, type, evidence), "
    "filter_keys[], relationships[] (from_field, to_collection, to_field), evidence."
)

_PERSISTENCE_EXTRACT_SYSTEM = (
    f"{_PERSISTENCE_EXTRACT_INTRO}\n\n{_PERSISTENCE_EXTRACT_RULES}\n\n"
    f"{_PERSISTENCE_EXTRACT_OUTPUT}"
)


def _build_persistence_extract_user_prompt(candidates: List[Dict[str, Any]]) -> str:
    nodes_json = json.dumps(candidates, indent=2, ensure_ascii=False)
    return f"""NODES TO ANALYZE:
{nodes_json}"""


def _build_persistence_extract_prompt(candidates: List[Dict[str, Any]]) -> str:
    """Full Stage 4c prompt in original section order (uncached fallback)."""
    user_part = _build_persistence_extract_user_prompt(candidates)
    return (
        f"{_PERSISTENCE_EXTRACT_INTRO}\n\n{_PERSISTENCE_EXTRACT_RULES}\n\n"
        f"{user_part}\n\n{_PERSISTENCE_EXTRACT_OUTPUT}"
    )


def _merge_persistence_into_registry(
    registry: Dict[str, Any],
    extractions: List[NodePersistenceExtract],
) -> Dict[str, Any]:
    reg = _normalize_workflow_registry(registry)
    cols: Dict[str, Any] = reg.setdefault("collections", {})

    for node_ext in extractions or []:
        node_id = (node_ext.nodeId or "").strip()
        if not node_id:
            continue
        for op in node_ext.operations or []:
            coll = (op.collection or "").strip()
            if not coll or not (op.evidence or "").strip():
                continue
            entry = cols.setdefault(coll, {
                "fields": {},
                "filter_keys": [],
                "writers": [],
                "readers": [],
            })
            fields_map: Dict[str, Any] = entry.setdefault("fields", {})
            for fk in op.filter_keys or []:
                fk_s = str(fk).strip()
                if fk_s and fk_s not in entry["filter_keys"]:
                    entry["filter_keys"].append(fk_s)
            kind = (op.kind or "").strip().lower()
            if kind == "write" and node_id not in entry["writers"]:
                entry["writers"].append(node_id)
            if kind == "read" and node_id not in entry["readers"]:
                entry["readers"].append(node_id)
            for fld in op.fields or []:
                fname = (fld.name or "").strip()
                if not fname or not (fld.evidence or "").strip():
                    continue
                existing = fields_map.get(fname)
                if not existing:
                    fields_map[fname] = {
                        "type": (fld.type or "string").strip() or "string",
                        "writers": [node_id] if kind == "write" else [],
                        "readers": [node_id] if kind == "read" else [],
                    }
                else:
                    if kind == "write" and node_id not in existing.setdefault("writers", []):
                        existing["writers"].append(node_id)
                    if kind == "read" and node_id not in existing.setdefault("readers", []):
                        existing["readers"].append(node_id)
    return reg


async def _extract_persistence_via_llm(
    candidates: List[Dict[str, Any]],
    *,
    api_key: Optional[str] = None,
    state: ScratchPlanningState,
) -> List[NodePersistenceExtract]:
    user_prompt = _build_persistence_extract_user_prompt(candidates)
    full_prompt = _build_persistence_extract_prompt(candidates)
    cached_content = await _get_scratch_cached_content(
        api_key,
        _PERSISTENCE_EXTRACT_MODEL,
        system_text=_PERSISTENCE_EXTRACT_SYSTEM,
        cache_key_prefix="scratch_persistence",
        display_name="scratch-persistence-system",
    )
    raw, parsed = await _scratch_llm_call(
        api_key,
        model=_PERSISTENCE_EXTRACT_MODEL,
        sub_operation="stage4c-persistence-extract",
        system_text=_PERSISTENCE_EXTRACT_SYSTEM,
        user_prompt=user_prompt,
        full_prompt=full_prompt,
        cached_content=cached_content,
        project_id=state.projectId,
        org_id=state.orgId,
        subscriber_id=state.subscriberId,
        user_id=state.userId,
        phase_id=state.phaseId,
        attempt=1,
        temperature=0.1,
        as_json=True,
        response_schema=PhasePersistenceExtractResponse,
    )
    if parsed is not None and isinstance(parsed, PhasePersistenceExtractResponse):
        return list(parsed.extractions or [])
    data = _parse_json_robust(raw)
    if isinstance(data, dict):
        try:
            return list(PhasePersistenceExtractResponse(**data).extractions or [])
        except Exception as exc:
            logger.warning(f"[scratch/stage4c] Failed to parse persistence extract: {exc}")
    return []


async def _update_registry_from_phase(
    graph_dict: Dict[str, Any],
    registry: Dict[str, Any],
    state: ScratchPlanningState,
    api_key: Optional[str] = None,
    collection_metadata: Optional[List[Dict[str, Any]]] = None, # <--- ADDED
) -> Dict[str, Any]:
    """Stage 4c: filter DB nodes, optional LLM extract, merge into workflow registry."""
    step_lookup = _build_step_description_lookup(state.steps)
    candidates = _filter_db_candidate_nodes(graph_dict, step_lookup)
    if not candidates:
        logger.info(
            f"[scratch/stage4c] Phase '{state.phaseId}': no DB CRUD candidates — registry unchanged."
        )
        return _normalize_workflow_registry(registry)

    logger.info(
        f"[scratch/stage4c] Phase '{state.phaseId}': {len(candidates)} DB candidate node(s): "
        f"{[c.get('nodeId') for c in candidates]}"
    )
    try:
        extractions = await _extract_persistence_via_llm(candidates, api_key=api_key, state=state)
    except BillingError:
        raise
    except Exception as exc:
        logger.warning(
            f"[scratch/stage4c] Persistence LLM extract failed for phase '{state.phaseId}': {exc}"
        )
        state.validationWarnings.append(
            f"[stage4c] Persistence registry extract failed: {exc}"
        )
        return _normalize_workflow_registry(registry)

    merged = _merge_persistence_into_registry(registry, extractions)

    # Issue 4: compile a single writer→reader join key and pin it on both nodes
    # BEFORE gap-tool / IR generation. Independent LLM extracts can disagree
    # (jd_id vs candidates_batch_id); the compiled link is authoritative.
    try:
        nodes = graph_dict.get("nodes") or []
        links, lineage_errors = attach_persistence_links_to_graph(nodes, merged)
        dumped_links = [lk.model_dump() for lk in links]
        # Public key survives Stage 5 stripping of "_"-prefixed internals.
        graph_dict["persistenceLinks"] = dumped_links
        graph_dict["_persistenceLinks"] = dumped_links
        if lineage_errors:
            graph_dict["_persistenceLineageErrors"] = lineage_errors
            for err in lineage_errors:
                state.validationWarnings.append(f"[stage4c] persistence lineage: {err}")
            logger.warning(
                "[scratch/stage4c] persistence lineage errors: %s", lineage_errors
            )
        else:
            logger.info(
                "[scratch/stage4c] compiled %s persistenceLink(s)", len(links)
            )
    except Exception as link_exc:
        logger.warning("[scratch/stage4c] persistenceLink compile failed: %s", link_exc)

    if state.subscriberId and state.orgId:
        # Safely extract ignored sources (prevents crashes if the field was removed from the model)
        raw_ignored = getattr(state, "ignoredKnowledgeSources", [])
        ignored_names = [name.replace("eg_", "") for name in (raw_ignored or [])]

        try:
            repo = EnterpriseGraphRepository()
            
            # Helper to check the UI merge decision
            def get_merge_decision(coll_name: str) -> bool:
                if not collection_metadata:
                    return True # Default to merge if no metadata payload exists
                for meta in collection_metadata:
                    if meta.get("collection_name") == coll_name:
                        return meta.get("is_merged", True)
                return True

            # Helper to extract the target store decision
            def get_target_store(coll_name: str) -> str:
                if not collection_metadata:
                    return "mongodb"
                for meta in collection_metadata:
                    if meta.get("collection_name") == coll_name:
                        return meta.get("target_store", "mongodb")
                return "mongodb"

            for node_ext in extractions:
                for op in node_ext.operations:
                    if not op.collection:
                        continue
                    if op.collection in ignored_names:
                        logger.info(f"[scratch/stage4c] Skipping Enterprise Graph save for '{op.collection}' (ignored by user).")
                        continue

                    # Evaluate if the user chose to merge or create a new one
                    is_merged = get_merge_decision(op.collection)
                    
                    target_collection_name = op.collection
                    if not is_merged:
                        target_collection_name = f"{op.collection}_new_{uuid.uuid4().hex[:6]}"
                        logger.info(f"[scratch/stage4c] User declined merge. Creating brand new collection '{target_collection_name}'.")

                    target_store_val = get_target_store(op.collection)
                    
                    fields_to_upsert = [
                        {"name": f.name, "type": f.type, "aliases": []}
                        for f in op.fields if f.name
                    ]
                    if fields_to_upsert:
                        await repo.upsert_schema(
                            subscriber_id=state.subscriberId,
                            org_id=state.orgId,
                            collection_name=target_collection_name,
                            fields=fields_to_upsert,
                            target_store=target_store_val
                        )
                    for rel in op.relationships:
                        if rel.from_field and rel.to_collection and rel.to_field:
                            await repo.upsert_relationship(
                                subscriber_id=state.subscriberId,
                                org_id=state.orgId,
                                from_coll=target_collection_name, # <--- UPDATED
                                from_field=rel.from_field,
                                to_coll=rel.to_collection,
                                to_field=rel.to_field
                            )
        except Exception as db_exc:
            logger.warning(f"[scratch/stage4c] ArcadeDB write-back failed: {db_exc}")

    n_cols = len(merged.get("collections") or {})
    logger.info(
        f"[scratch/stage4c] Phase '{state.phaseId}': registry updated — "
        f"{n_cols} collection(s), extracted {len(extractions)} node(s)."
    )
    return merged


# ===========================================================================
# SECTION 11 — MAIN ORCHESTRATOR
# ===========================================================================

def compose_prompt_mode_requirement(
    prompt: str,
    stage0_context: Optional[str] = None,
) -> str:
    """
    Build the FREE-step text Stage 1 implements in prompt-mode.

    Stage 0 context is treated as constraints-only in the skeleton prompt
    ("do not add nodes from it"). Clarifications that change the flow must
    therefore live inside the FREE step, or Stage 1 will ignore them.
    """
    requirement = (prompt or "").strip() or "Create a workflow"
    ctx = (stage0_context or "").strip()
    if ctx:
        requirement = (
            f"{requirement}\n\n"
            f"AUTHORITATIVE CLARIFICATIONS (implement these; do not ignore):\n"
            f"{ctx}"
        )
    return requirement


def build_prompt_mode_steps(
    prompt: str,
    stage0_context: Optional[str] = None,
) -> List[LogicalStepLite]:
    """One FREE LogicalStepLite wrapping the full NL requirement."""
    return [
        LogicalStepLite(
            stepId="L1",
            name="Full workflow",
            description=compose_prompt_mode_requirement(prompt, stage0_context),
            preferredKind=None,
        )
    ]


def _normalize_skeleton_plan(skeleton_plan: Any) -> SkeletonPlan:
    """Accept SkeletonPlan, dict, or a model_dump()-able object."""
    if isinstance(skeleton_plan, SkeletonPlan):
        return skeleton_plan
    if isinstance(skeleton_plan, dict):
        return SkeletonPlan.model_validate(skeleton_plan)
    if hasattr(skeleton_plan, "model_dump"):
        return SkeletonPlan.model_validate(skeleton_plan.model_dump())
    raise TypeError(
        f"skeleton_plan must be SkeletonPlan or dict, got {type(skeleton_plan).__name__}"
    )


async def _inject_known_collection_schemas(state: ScratchPlanningState) -> None:
    """Resolve known collection schemas onto planning state (best-effort)."""
    try:
        step_texts = [f"{s.name}\n{s.description}" for s in (state.steps or [])]
        schemas_dict, schemas_text = await resolve_schemas_for_planner(
            texts=[
                state.phaseDescription,
                state.stage0Context,
                *step_texts,
            ],
            project_id=state.projectId,
            subscriber_id=state.subscriberId,
            workflow_registry=state.workflowRegistry,
        )
        if schemas_dict:
            state.knownCollectionSchemas = {
                k: v for k, v in schemas_dict.items() if k != "__meta__"
            }
        if schemas_text:
            state.knownCollectionSchemasText = schemas_text
            logger.info(
                "[scratch] Injected known collection schemas into Stage 1 prompt "
                "(%d chars) for phase=%s",
                len(schemas_text),
                state.phaseId,
            )
    except Exception as exc:
        logger.warning(
            "[scratch] Failed to resolve collection schemas for planner: %s", exc
        )


async def plan_skeleton_from_prompt(
    *,
    prompt: str,
    phase_id: str = "phase_1",
    phase_name: str = "Workflow",
    phase_description: Optional[str] = None,
    stage0_context: Optional[str] = None,
    api_key: Optional[str] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workflow_registry: Optional[Dict[str, Any]] = None,
) -> SkeletonPlan:
    """
    Prompt-mode Stage 1: same skeleton planner, fed the full NL requirement
    as one FREE step so it may split, add routers, and write detailed descriptions.

    Used by NL builder. Callers then pass the result to
    ``plan_phase_graph_scratch(..., skeleton_plan=...)`` to run Stage 1.2+.
    """
    requirement_steps = build_prompt_mode_steps(prompt, stage0_context)
    state = ScratchPlanningState(
        phaseId=phase_id,
        phaseName=phase_name,
        phaseDescription=(phase_description or prompt or "").strip() or phase_name,
        steps=requirement_steps,
        workflowRegistry=_normalize_workflow_registry(workflow_registry),
        stage0Context=(stage0_context or "").strip() or None,
        projectId=project_id,
        orgId=org_id,
        subscriberId=subscriber_id,
        userId=user_id,
    )
    await _inject_known_collection_schemas(state)
    logger.info(
        "[scratch/prompt-mode] Stage 1 skeleton from NL prompt "
        "phase=%s name=%r prompt_chars=%d stage0=%s",
        phase_id,
        phase_name,
        len((prompt or "").strip()),
        "yes" if (stage0_context or "").strip() else "no",
    )
    return await _stage1_skeleton_planner(state, api_key)


def _globals_from_prior_phase_outputs(
    prior_phase_outputs: Optional[Dict[str, Any]],
) -> List[GlobalVar]:
    """Convert nodeId → outputs into namespaced GlobalVars (Issue 8).

    Dedup key is ``sourceNode.field``, so two nodes emitting ``status`` both survive.
    """
    global_vars: List[GlobalVar] = []
    seen_global_names: set = set()
    if not prior_phase_outputs:
        return global_vars
    for node_id, param_names in prior_phase_outputs.items():
        if not isinstance(param_names, list):
            continue
        for item in param_names:
            if isinstance(item, dict):
                pname = item.get("name") or ""
                gname = prior_phase_global_name(str(node_id or ""), str(pname))
                if not gname or gname in seen_global_names:
                    continue
                seen_global_names.add(gname)
                global_vars.append(GlobalVar(
                    name=gname,
                    datatype=item.get("datatype") or item.get("dataType") or "Any",
                    source=str(node_id or ""),
                    description=item.get("description") or "",
                    itemType=item.get("itemType"),
                    scalarItemDatatype=item.get("scalarItemDatatype"),
                    itemSchema=item.get("itemSchema"),
                    enumValues=item.get("enumValues"),
                ))
            else:
                pname = str(item) if item else ""
                gname = prior_phase_global_name(str(node_id or ""), pname)
                if not gname or gname in seen_global_names:
                    continue
                seen_global_names.add(gname)
                global_vars.append(GlobalVar(
                    name=gname,
                    datatype="Any",
                    source=str(node_id or ""),
                ))
    return global_vars


async def plan_phase_graph_scratch(
    phase_id: str,
    phase_name: str,
    phase_description: str,
    steps: Any,   # list[LogicalStep] from agent_new.py or list[dict]
    *,
    prior_phase_outputs: Optional[Dict[str, Any]] = None,
    workflow_registry: Optional[Dict[str, Any]] = None,
    stage0_context: Optional[str] = None,
    api_key: Optional[str] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    run_gap_resolution: bool = True,
    run_code_emission: bool = True,
    gap_resolver: Optional[Any] = None,
    checkpoint_fn: Optional[Any] = None,
    resume_state_snapshot: Optional[Dict[str, Any]] = None,
    resume_sub_stage: Optional[str] = None,
    resume_batch_index: Optional[int] = None,
    resume_router_id: Optional[str] = None,
    resume_completed_router_results: Optional[List[Dict[str, Any]]] = None,
    collection_metadata: Optional[List[Dict[str, Any]]] = None,
    skeleton_plan: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Main entry point for the scratch (no-capabilities) graph planner.

    Runs the full pipeline and returns a GraphDefinition-compatible dict.

    Parameters
    ----------
    phase_id            : Phase identifier (e.g. "phase_1")
    phase_name          : Human-readable phase name
    phase_description   : Phase goal / description
    steps               : list of LogicalStep objects or dicts with stepId/name/description
    prior_phase_outputs : Optional dict of prior phase outputs
                          (nodeId → list[paramName] OR list[{name,datatype,description}])
                          Rich entries are preferred so Stage 1.5 / Stage 2a can select and bind.
    workflow_registry   : Accumulated cross-phase persistence registry from prior phases
    stage0_context      : Short authoritative text from Stage 0 requirement clarification
    api_key             : Gemini API key (if None, uses genai default credentials)
    run_gap_resolution  : If False, skip Stage 5 (useful for /plan/preview)
    run_code_emission   : If False, skip Stage 6
    gap_resolver        : Async callable for Stage 5 gap resolution. Signature:
                            async (graph_dict: Dict) -> Dict
                          The callable receives the assembled graph dict (without internal
                          "_" prefixed keys) and must return the updated graph dict.
                          Typically wraps _resolve_gap_generations from agent_new.py.
                          Required when run_gap_resolution=True.
    skeleton_plan       : Optional precomputed SkeletonPlan (object or dict).
                          When set, Stage 1 is skipped; Stage 1.2 still enriches
                          descriptions unless resume already marked them done.

    Returns
    -------
    GraphDefinition-compatible dict. Also contains "_stateSchema", "_workflowRegistry",
    and "_langgraphCode" internal keys (stripped by the caller before GraphDefinition()).
    """
    # ── Normalise steps ────────────────────────────────────────────────────────
    lite_steps: List[LogicalStepLite] = []
    for s in (steps or []):
        if isinstance(s, dict):
            raw_kind = s.get("preferredKind")
            kind = raw_kind if raw_kind in ("ui_screen", "tool") else None
            lite_steps.append(LogicalStepLite(
                stepId=s.get("stepId", ""),
                name=s.get("name", ""),
                description=s.get("description", ""),
                preferredKind=kind,
            ))
        else:
            # Assume it has .stepId, .name, .description and optionally .preferredKind
            raw_kind = getattr(s, "preferredKind", None)
            kind = raw_kind if raw_kind in ("ui_screen", "tool") else None
            lite_steps.append(LogicalStepLite(
                stepId=getattr(s, "stepId", ""),
                name=getattr(s, "name", ""),
                description=getattr(s, "description", ""),
                preferredKind=kind,
            ))

    # ── Normalise prior phase outputs (namespaced; keep colliding fields) ──────
    # Accepts legacy list[str] or rich list[{name,datatype,description}].
    # Issue 8: dedup key is sourceNode.field, not the bare field name.
    global_vars: List[GlobalVar] = _globals_from_prior_phase_outputs(prior_phase_outputs)
    seen_global_names: set = {g.name for g in global_vars}
    try:
        import json
        with open('config.json', 'r') as _f:
            _cfg = json.load(_f)
            for _g in _cfg.get("available_global_variables", []):
                _name = _g.get("name")
                if _name and _name not in seen_global_names:
                    seen_global_names.add(_name)
                    global_vars.append(GlobalVar(
                        name=_name,
                        datatype=_g.get("type", "string"),
                        source="system_config",
                        description=_g.get("description", "")
                    ))
    except Exception as e:
        logger.warning(f"Failed to load config globals for validation: {e}")

    # ── Initialise pipeline state ──────────────────────────────────────────────
    resume_from = resume_sub_stage or "stage1"
    # Older checkpoints may resume at stage2a+ without a stage1_2 / stage1_5 / stage1_7 entry —
    # map unknown stage names onto nearest known stage so resume doesn't crash.
    if resume_state_snapshot:
        state = ScratchPlanningState.model_validate(resume_state_snapshot)
        state.phaseId = phase_id
        state.phaseName = phase_name
        state.phaseDescription = phase_description
        state.projectId = project_id or state.projectId
        state.orgId = org_id or state.orgId
        state.subscriberId = subscriber_id or state.subscriberId
        state.userId = user_id or state.userId
        if stage0_context:
            state.stage0Context = (stage0_context or "").strip() or None
        if workflow_registry:
            state.workflowRegistry = _normalize_workflow_registry(workflow_registry)
        # Prefer snapshot's priorPhaseOutputs (may already be enriched); else use newly
        # normalised globals when snapshot has none. Always namespace bare prior-phase names.
        if state.priorPhaseOutputs:
            state.priorPhaseOutputs = _namespace_global_vars(list(state.priorPhaseOutputs))
        elif global_vars:
            state.priorPhaseOutputs = global_vars
    else:
        state = ScratchPlanningState(
            phaseId=phase_id,
            phaseName=phase_name,
            phaseDescription=phase_description,
            steps=lite_steps,
            priorPhaseOutputs=global_vars,
            workflowRegistry=_normalize_workflow_registry(workflow_registry),
            stage0Context=(stage0_context or "").strip() or None,
            projectId=project_id,
            orgId=org_id,
            subscriberId=subscriber_id,
            userId=user_id,
        )

    # Explicit caller skeleton wins when resume snapshot has none.
    # Fresh NL runs always apply the supplied skeleton and skip Stage 1.
    if skeleton_plan is not None:
        incoming = _normalize_skeleton_plan(skeleton_plan)
        if state.skeletonPlan is None or resume_state_snapshot is None:
            state.skeletonPlan = incoming
            logger.info(
                "[scratch_planner] Using caller-supplied skeleton_plan "
                "nodes=%d edges=%d — Stage 1 will be skipped",
                len(incoming.nodes),
                len(incoming.edges),
            )

    # Task 6: resolve known collection schemas before Stage 1 so descriptions
    # include field:type (not just collection names).
    await _inject_known_collection_schemas(state)

    if state.priorPhaseOutputs:
        _log_binding_audit(
            "cross_phase_handoff_in",
            phaseId=phase_id,
            priorGlobalCount=len(state.priorPhaseOutputs),
            globals=[
                {
                    "name": g.name,
                    "datatype": g.datatype,
                    "itemType": g.itemType,
                    "source": g.source,
                    "description": (g.description or "")[:120],
                }
                for g in state.priorPhaseOutputs
            ],
        )

    _scratch_sub_stages = (
        "stage1", "stage1_2", "stage1_5", "stage1_7", "stage2a", "stage2c", "stage3a", "stage4", "stage4b", "stage4c"
    )
    if resume_from not in _scratch_sub_stages:
        # Backward-compat: treat unknown resume markers as stage2a (post-skeleton).
        logger.warning(
            f"[scratch_planner] Unknown resume_sub_stage={resume_from!r}; "
            f"falling back to stage2a"
        )
        resume_from = "stage2a"

    def _run_from(sub_stage: str) -> bool:
        return _scratch_sub_stages.index(sub_stage) >= _scratch_sub_stages.index(resume_from)

    _reg_cols = len((state.workflowRegistry.get("collections") or {}))
    logger.info(
        f"[scratch_planner] Starting pipeline for phase='{phase_id}' "
        f"name='{phase_name}' steps={len(lite_steps)} prior_globals={len(state.priorPhaseOutputs)} "
        f"registry_collections={_reg_cols} "
        f"stage0_ctx={'yes' if state.stage0Context else 'no'} "
        f"project={project_id or '-'} org={org_id or '-'} "
        f"subscriber={subscriber_id or '-'} "
        f"gap_resolution={run_gap_resolution} code_emission={run_code_emission} "
        f"resume_from={resume_from}"
    )

    # ── Stage 1 — Skeleton Planner ─────────────────────────────────────────────
    pipeline_start = time.perf_counter()
    if _run_from("stage1") and state.skeletonPlan is None:
        logger.info("[scratch_planner] Stage 1: Skeleton Planner")
        state.skeletonPlan = await _stage1_skeleton_planner(state, api_key)
        if checkpoint_fn is not None:
            await _safe_checkpoint(checkpoint_fn, state, "stage1")
        logger.info(
            f"[scratch_planner] Stage 1 done in "
            f"{int((time.perf_counter() - pipeline_start) * 1000)}ms"
        )
    elif state.skeletonPlan is not None:
        logger.info("[scratch_planner] Stage 1: skipped (resumed)")

    # ── Stage 1.2 — Production-grade node description enricher ─────────────────
    # Topology is frozen. Rewrites descriptions so Stage 2a / generators receive
    # enterprise codegen specs instead of thin Stage 1 blurbs.
    if _run_from("stage1_2") and state.skeletonPlan is not None:
        if resume_from == "stage1_2" or not state.nodeDescriptionsEnriched:
            stage12_start = time.perf_counter()
            logger.info("[scratch_planner] Stage 1.2: Node Description Enricher")
            try:
                enriched, enrich_warnings = await _stage1_2_enrich_node_descriptions(
                    state, api_key
                )
                if enriched is not None:
                    state.skeletonPlan = enriched
                if enrich_warnings:
                    state.validationWarnings.extend(
                        f"[stage1.2] {w}" for w in enrich_warnings
                    )
            except BillingError:
                raise
            except Exception as exc:
                logger.warning(
                    "[scratch_planner] Stage 1.2 failed (non-fatal): %s", exc
                )
                state.validationWarnings.append(
                    f"[stage1.2] Description enricher failed: {exc}"
                )
            state.nodeDescriptionsEnriched = True
            if checkpoint_fn is not None:
                await _safe_checkpoint(checkpoint_fn, state, "stage1_2")
            logger.info(
                f"[scratch_planner] Stage 1.2 done in "
                f"{int((time.perf_counter() - stage12_start) * 1000)}ms"
            )
        else:
            logger.info(
                "[scratch_planner] Stage 1.2: skipped (already enriched)"
            )
    elif state.skeletonPlan is None:
        logger.info("[scratch_planner] Stage 1.2: skipped (no skeleton)")

    # ── Stage 1.5 — Advisory primary global selection ──────────────────────────
    # Coarse LLM pick based on skeleton node names/descriptions. Advisory only —
    # Stage 2a still receives secondary (compact) + Stage 4b sees the full set.
    if _run_from("stage1_5"):
        if state.priorPhaseOutputs:
            # Re-run when explicitly resuming at 1.5, or when selection not yet set.
            if resume_from == "stage1_5" or not state.selectedPrimaryGlobalNames:
                stage15_start = time.perf_counter()
                logger.info("[scratch_planner] Stage 1.5: Advisory Global Selection")
                state.selectedPrimaryGlobalNames = await _stage1_5_select_primary_globals(
                    state, api_key
                )
                if checkpoint_fn is not None:
                    await _safe_checkpoint(checkpoint_fn, state, "stage1_5")
                logger.info(
                    f"[scratch_planner] Stage 1.5 done in "
                    f"{int((time.perf_counter() - stage15_start) * 1000)}ms "
                    f"primary={len(state.selectedPrimaryGlobalNames)}/"
                    f"{len(state.priorPhaseOutputs)}"
                )
            else:
                logger.info(
                    f"[scratch_planner] Stage 1.5: skipped (already selected "
                    f"{len(state.selectedPrimaryGlobalNames)} primary global(s))"
                )
        else:
            state.selectedPrimaryGlobalNames = []
            logger.info("[scratch_planner] Stage 1.5: skipped (no prior-phase globals)")

    # ── Stage 1.7 — Persistence Architect ──────────────────────────────────────
    # Designs production-grade collection fields into workflowRegistry BEFORE
    # Stage 2a builds its prompt cache. Existing registry fields stay authoritative.
    if _run_from("stage1_7"):
        if resume_from == "stage1_7" or not state.persistenceArchitectCompleted:
            stage17_start = time.perf_counter()
            logger.info("[scratch_planner] Stage 1.7: Persistence Architect")
            try:
                state.workflowRegistry = await _stage1_7_persistence_architect(
                    state, api_key
                )
                state.persistenceArchitectCompleted = True
            except BillingError:
                raise
            except Exception as exc:
                logger.warning(
                    "[scratch_planner] Stage 1.7 failed (non-fatal): %s", exc
                )
                state.validationWarnings.append(
                    f"[stage1.7] Persistence architect failed: {exc}"
                )
                state.persistenceArchitectCompleted = True
            if checkpoint_fn is not None:
                await _safe_checkpoint(checkpoint_fn, state, "stage1_7")
            _reg_n = len((state.workflowRegistry.get("collections") or {}))
            logger.info(
                f"[scratch_planner] Stage 1.7 done in "
                f"{int((time.perf_counter() - stage17_start) * 1000)}ms "
                f"registry_collections={_reg_n}"
            )
        else:
            logger.info(
                "[scratch_planner] Stage 1.7: skipped (already completed)"
            )

    # ── Stage 2a — Node Schema Builder ────────────────────────────────────────
    stage2_start = time.perf_counter()
    if _run_from("stage2a") and (
        resume_from == "stage2a" or not state.nodeSchemas
    ):
        logger.info("[scratch_planner] Stage 2a: Node Schema Builder")
        start_batch = resume_batch_index if resume_from == "stage2a" else -1
        state.nodeSchemas = await _stage2a_node_schema_builder(
            state,
            api_key,
            checkpoint_fn=checkpoint_fn,
            start_after_batch_index=start_batch if start_batch is not None else -1,
        )
        logger.info(
            f"[scratch_planner] Stage 2a done in "
            f"{int((time.perf_counter() - stage2_start) * 1000)}ms"
        )
    elif state.nodeSchemas:
        logger.info("[scratch_planner] Stage 2a: skipped (resumed)")

    # ── Stage 2c — Execution State Schema Derivation ──────────────────────────
    if _run_from("stage2c") and state.stateSchema is None:
        logger.info("[scratch_planner] Stage 2c: State Schema Derivation")
        state.stateSchema = _stage2c_state_schema(state.nodeSchemas)
        logger.info(
            f"[scratch_planner] StateSchema: {len(state.stateSchema.fields)} field(s)"
        )

    # ── Stage 3a — Edge Mapper ────────────────────────────────────────────────
    stage3_start = time.perf_counter()
    if _run_from("stage3a") and state.edgeMap is None:
        logger.info("[scratch_planner] Stage 3a: Edge Mapper")
        resumed_router_results: Optional[List[RouterEdgeResult]] = None
        if resume_from == "stage3a" and resume_completed_router_results:
            resumed_router_results = [
                RouterEdgeResult.model_validate(r)
                for r in resume_completed_router_results
            ]
        state.edgeMap = await _stage3a_edge_mapper(
            state,
            api_key,
            checkpoint_fn=checkpoint_fn,
            resume_completed_router_results=resumed_router_results,
        )
        logger.info(
            f"[scratch_planner] Stage 3a done in "
            f"{int((time.perf_counter() - stage3_start) * 1000)}ms"
        )
    elif state.edgeMap is not None:
        logger.info("[scratch_planner] Stage 3a: skipped (resumed)")

    # ── Stage 4 — Graph Assembler ─────────────────────────────────────────────
    if _run_from("stage4") and state.graphDefinition is None:
        logger.info("[scratch_planner] Stage 4: Graph Assembler")
        state.graphDefinition = _stage4_assemble_graph(state)

    # ── Stage 4b — Final Graph Validation ─────────────────────────────────────
    if _run_from("stage4b") and state.graphDefinition is not None:
        logger.info("[scratch_planner] Stage 4b: Final Graph Validation")
        stage4b_errors, stage4b_warnings = _stage4b_validate_graph(state.graphDefinition, state)
        state.validationWarnings.extend(stage4b_warnings)
        if stage4b_warnings:
            logger.warning(
                f"[scratch_planner] Stage 4b warnings ({len(stage4b_warnings)}):\n"
                + "\n".join(f"  - {w}" for w in stage4b_warnings)
            )

        # Attempt targeted binding repair before aborting.
        stage4_assembler_binding_errors = list(state.graphDefinition.get("_stage4BindingErrors") or [])
        binding_errors_for_repair = stage4_assembler_binding_errors + stage4b_errors
        if binding_errors_for_repair:
            max_attempts = 2
            _log_binding_audit(
                "stage4b_repair_loop_start",
                phaseId=state.phaseId,
                assemblerErrors=len(stage4_assembler_binding_errors),
                validatorErrors=len(stage4b_errors),
                totalErrors=len(binding_errors_for_repair),
            )
            for attempt in range(1, max_attempts + 1):
                state.graphDefinition = await _repair_graph_bindings_with_llm_scratch(
                    state.graphDefinition,
                    state,
                    binding_errors_for_repair,
                    api_key=api_key,
                    attempt=attempt,
                )
                # Re-validate after repair
                stage4b_errors, stage4b_warnings = _stage4b_validate_graph(state.graphDefinition, state)
                state.validationWarnings.extend(stage4b_warnings)
                if not stage4b_errors:
                    # Clear assembler binding errors once fixed to avoid re-trigger
                    state.graphDefinition["_stage4BindingErrors"] = []
                    _log_binding_audit(
                        "stage4b_repair_loop_success",
                        phaseId=state.phaseId,
                        attempt=attempt,
                        bindings=_summarize_graph_input_bindings(state.graphDefinition),
                    )
                    break
                binding_errors_for_repair = stage4b_errors
                _log_binding_audit(
                    "stage4b_repair_loop_retry",
                    phaseId=state.phaseId,
                    attempt=attempt,
                    remainingErrors=stage4b_errors[:15],
                )

        if stage4b_errors:
            error_text = "\n".join(f"  - {e}" for e in stage4b_errors)
            logger.error(f"[scratch_planner] Stage 4b errors ({len(stage4b_errors)}):\n{error_text}")
            raise ValueError(
                f"Stage 4b graph validation failed with {len(stage4b_errors)} error(s):\n" + error_text
            )

    # ── Stage 4c — Persistence registry extract + merge ─────────────────────
    if _run_from("stage4c") and state.graphDefinition is not None:
        logger.info("[scratch_planner] Stage 4c: Persistence Registry")
        state.workflowRegistry = await _update_registry_from_phase(
            state.graphDefinition,
            state.workflowRegistry,
            state,
            api_key,
            collection_metadata,
        )
        state.graphDefinition["_workflowRegistry"] = state.workflowRegistry
        if checkpoint_fn is not None:
            await _safe_checkpoint(checkpoint_fn, state, "stage4c")

    # ── Stage 5 — Gap Resolution ───────────────────────────────────────────────
    # Uses the caller-supplied gap_resolver (wraps _resolve_gap_generations from
    # agent_new.py) to generate Angular apps (ui_screen) or dynamic tools (tool).
    # No custom LLM call here — full node info is already in config.generation.
    if run_gap_resolution:
        if gap_resolver is None:
            logger.warning(
                "[scratch_planner] Stage 5: run_gap_resolution=True but no gap_resolver "
                "provided — skipping. Pass gap_resolver=<_resolve_gap_generations wrapper> "
                "to enable actual screen/tool generation."
            )
        else:
            logger.info(
                "[scratch_planner] Stage 5: Gap Resolution via gap_resolver "
                "(generate_angular_app_async / _generate_gap_tool_and_upsert_capability)"
            )
            # Strip internal _-prefixed keys before handing off to the resolver
            public_graph = {
                k: v for k, v in state.graphDefinition.items()
                if not k.startswith("_")
            }
            internal_keys = {
                k: v for k, v in state.graphDefinition.items()
                if k.startswith("_")
            }
            updated_graph = await gap_resolver(public_graph)
            # Restore internal keys (stateSchema, langgraphCode stubs, etc.)
            state.graphDefinition = {**updated_graph, **internal_keys}
            logger.info("[scratch_planner] Stage 5: Gap Resolution complete.")
    else:
        logger.info("[scratch_planner] Stage 5: Gap Resolution SKIPPED (preview mode)")

    # ── Stage 6 — LangGraph Code Emission ─────────────────────────────────────
    if run_code_emission:
        logger.info("[scratch_planner] Stage 6: LangGraph Code Emission")
        state.langgraphCode = _stage6_emit_langgraph_code(state)
        state.graphDefinition["_langgraphCode"] = state.langgraphCode
    else:
        logger.info("[scratch_planner] Stage 6: Code Emission SKIPPED (preview mode)")

    total_ms = int((time.perf_counter() - pipeline_start) * 1000)
    logger.info(
        f"[scratch_planner] Pipeline complete for phase='{phase_id}' in {total_ms}ms. "
        f"graphId={state.graphDefinition.get('graphId')} "
        f"nodes={len(state.graphDefinition['nodes'])} "
        f"edges={len(state.graphDefinition['edges'])} "
        f"warnings={len(state.validationWarnings)}"
    )

    # Embed warnings and registry for caller
    state.graphDefinition["_planningWarnings"] = state.validationWarnings
    state.graphDefinition["_workflowRegistry"] = state.workflowRegistry

    return state.graphDefinition
