from __future__ import annotations

import asyncio
import logging
import os
import sys
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Allow running this file directly:
#   python backend/services/plan_credit_estimator.py
_THIS_DIR = os.path.dirname(__file__)
_BACKEND_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))  # .../backend
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))  # repo root
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


@dataclass
class CostEstimateItem:
    kind: str
    count: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    rationale: Optional[str] = None


@dataclass
class PlanCostEstimate:
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    predicted_cost: float
    step_count: int
    phase_count: int
    complexity: str  # "simple" | "medium" | "complex"
    planned_node_count: int
    planned_edge_count: int
    ui_screen_count: int
    tool_count: int
    stage0_cost_usd: float
    preview_cost_usd: float
    execute_cost_usd: float
    dashboard_report_count: int = 0
    items: List[CostEstimateItem] = field(default_factory=list)
    assumptions: Dict[str, Any] = field(default_factory=dict)

# # Stage 1 (graph definition) token model defaults.
# DEFAULT_GRAPH_TEMPLATE_TOKENS = 3100
# DEFAULT_SCHEMA_OVERHEAD_TOKENS = 150
# DEFAULT_PER_STEP_REASONING_TOKEN = 500
# DEFAULT_TOKENS_PER_NODE = 350
# DEFAULT_TOKENS_PER_EDGE = 40
# DEFAULT_GAP_HINT_TOKENS_PER_NODE = 100
# ---- Shared heuristics ----

DEFAULT_NODE_REUSE_FRACTION = 0.40
DEFAULT_UI_FRACTION_OF_ALL_NODES = 0.30
DEFAULT_TOOL_FRACTION_OF_ALL_NODES = 0.30

# Per FREE/hybrid step when the planner may split one logical step into nodes.
# Expander / steps-mode substeps are already capability-sized (~1 graph node each).
AVG_NODES_PER_STEP_SIMPLE = 2.0
AVG_NODES_PER_STEP_MEDIUM = 2.5
AVG_NODES_PER_STEP_COMPLEX = 3.0

DEFAULT_EDGES_PER_NODE = 1.2
NODES_PER_SCHEMA_BATCH = 6

# Prompt-mode /plan/estimate: cost only through /nl/plan
# (enterprise expand + Stage 0 + scratch). Topology bands assume expander
# produces 8–18 logical steps — NOT derived from "." / ";" / newline counts.
PROMPT_UNTIL_NL_PLAN_TOPOLOGY: Dict[str, Dict[str, int]] = {
    "simple": {"nodes": 12, "edges": 15, "routers": 2},
    "medium": {"nodes": 20, "edges": 26, "routers": 3},
    "complex": {"nodes": 28, "edges": 36, "routers": 5},
}

# Enterprise expander (before Stage 0) — gemini-3.1-pro-preview + thinking.
# Defaults calibrated from live /nl/expand and /nl/steps/revise runs (~25–35s).
# llm_token_count averages override these when enough successful samples exist.
ENTERPRISE_EXPAND_MODEL = "gemini-3.1-pro-preview"
ENTERPRISE_EXPAND_TEMPLATE_TOKENS = 3500
ENTERPRISE_EXPAND_OUTPUT_TOKENS = 4200
ENTERPRISE_REVISE_TEMPLATE_TOKENS = 3800
ENTERPRISE_REVISE_OUTPUT_TOKENS = 4500
ENTERPRISE_REVISE_TOKENS_PER_STEP = 90
# Composed expanded-steps prompt that Stage 0 / Stage 1 actually see
ENTERPRISE_STEPS_PROMPT_TOKENS = 1800
ASSUMED_ENTERPRISE_STEPS = {"simple": 8, "medium": 12, "complex": 16}
NL_EXPANDER_OBSERVED_BUFFER = 1.25
NL_EXPANDER_OBSERVED_MIN_SAMPLES = 3


# Headroom applied to the final predicted_cost returned by /plan/estimate.
# Live 2026-09-18 LinkedIn: cap $0.90 exhausted on screen 1 (spent $0.93) before
# the gap tool ran. 1.40 was not enough for Angular 3.1-pro + IR/backend.
SAFETY_BUFFER_MULTIPLIER = 1.55

# Execute-only buffers (applied before the global SAFETY_BUFFER_MULTIPLIER).
EXECUTE_REPAIR_BUFFER_FRACTION = 0.25
EXECUTE_REVALIDATION_BUFFER_FRACTION = 0.10
UI_PIPELINE_COST_MULTIPLIER = 1.50
PREVIEW_COST_BUFFER_FRACTION = 0.10
# Floor after pipeline multiplier. Learned intercept (~$0.08–0.17) is from an
# older flash-only UI path; a GENERATE:ui_screen now runs IR + DS enrich +
# backend + Angular gemini-3.1-pro-preview (and often a recitation retry).
UI_MIN_COST_USD_PER_SCREEN = 0.48

# Stage 0 — gemini-3-flash-preview (per turn: system + growing user JSON)
STAGE0_SYSTEM_TOKENS = 1400
STAGE0_USER_BASE_TOKENS = 600
STAGE0_USER_PER_TURN_GROWTH = 350
STAGE0_OUTPUT_TOKENS_PER_TURN = 450
STAGE0_MAX_TURNS = 10

# Scratch requirement analysis — flash, prompt mode only
REQ_ANALYSIS_INPUT_TOKENS = 2200
REQ_ANALYSIS_OUTPUT_TOKENS = 900

# Scratch skeleton — gemini-3.1-pro-preview + thinking
SKELETON_TEMPLATE_TOKENS = 4200
SKELETON_OUTPUT_PER_NODE = 90
SKELETON_OUTPUT_PER_EDGE = 35
SKELETON_RETRY_FACTOR = 1.5

# Scratch Stage 1.2 description enricher — gemini-3.1-pro-preview + thinking
DESC_ENRICH_TEMPLATE_TOKENS = 3800
DESC_ENRICH_INPUT_PER_NODE = 280
DESC_ENRICH_OUTPUT_PER_NODE = 520
DESC_ENRICH_RETRY_FACTOR = 1.25
DESC_ENRICH_NODES_PER_BATCH = 4

# IR dataSource spec enricher (ai_processor Step 5.5) — gemini-3.1-pro-preview + thinking
DS_ENRICH_TEMPLATE_TOKENS = 4200
DS_ENRICH_INPUT_PER_DS = 400
DS_ENRICH_OUTPUT_PER_DS = 700
DS_ENRICH_RETRY_FACTOR = 1.25
DS_ENRICH_DS_PER_BATCH = 3
DS_ENRICH_AVG_DS_PER_SCREEN = 3

# Scratch node schemas — gemini-2.5-pro, sequential batches
SCHEMA_BATCH_INPUT_BASE = 2800
SCHEMA_BATCH_INPUT_PER_NODE = 420
SCHEMA_BATCH_OUTPUT_PER_NODE = 380
SCHEMA_BATCH_RETRY_FACTOR = 1.35

# Scratch router edges — flash
ROUTER_EDGE_INPUT_TOKENS = 1800
ROUTER_EDGE_OUTPUT_TOKENS = 280
ROUTER_EDGE_RETRY_FACTOR = 1.25

# Scratch persistence extract — flash, optional per phase
PERSISTENCE_INPUT_TOKENS = 1600
PERSISTENCE_OUTPUT_TOKENS = 350

# Execute tool gap — research + codegen.
# Old 210-token output / flash-lite rates priced a Unipile-class tool at ~$0.003.
# Live gap tools use gemini-3.5-flash (large prompt + pin), up to 3 attempts,
# then Issue 7 syntax fixer / escalation on gemini-2.5-pro; gap resolution can
# run two rounds.
DEFAULT_TOOL_TEMPLATE_TOKENS = 12000
DEFAULT_TOOL_RESEARCH_TOKENS = 3000
DEFAULT_TOOL_OUTPUT_TOKENS = 4000
DEFAULT_TOOL_RETRY_RATE = 2.4
TOOL_SYNTAX_FIX_PROBABILITY = 0.55

# Execute UI screen — learned formula
DEFAULT_UI_PROMPT_TOKENS = 225
UI_ENHANCED_TOKEN_A = 2.0134
UI_ENHANCED_TOKEN_B = 1323.62
UI_COST_USD_A = 0.000010
UI_COST_USD_B = 0.0792
UI_RETRY_COUNT_A = -0.000131
UI_RETRY_COUNT_B = 0.9591

# USD per 1k tokens by model tier (aligned with llm_token_tracker MODEL_PRICING)
# gemini-3.5-flash-lite: $0.30 / $2.50 per 1M
FLASH_LITE_RATE_PER_1K_INPUT_USD = 0.00030
FLASH_LITE_RATE_PER_1K_OUTPUT_USD = 0.0025
# gemini-3-flash-preview: $0.50 / $3.00 per 1M
FLASH_PREVIEW_RATE_PER_1K_INPUT_USD = 0.00050
FLASH_PREVIEW_RATE_PER_1K_OUTPUT_USD = 0.0030
# Legacy alias — most "flash" micro-calls use Flash-Lite
FLASH_RATE_PER_1K_INPUT_USD = FLASH_LITE_RATE_PER_1K_INPUT_USD
FLASH_RATE_PER_1K_OUTPUT_USD = FLASH_LITE_RATE_PER_1K_OUTPUT_USD
PRO_RATE_PER_1K_INPUT_USD = 0.00125
PRO_RATE_PER_1K_OUTPUT_USD = 0.01
PRO_PREVIEW_RATE_PER_1K_INPUT_USD = 0.002
PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD = 0.012
# Gap-tool codegen is gemini-3.5-flash ($1.50 / $9.00 per 1M), not flash-lite.
TOOL_RATE_PER_1K_INPUT_USD = 0.0015
TOOL_RATE_PER_1K_OUTPUT_USD = 0.0090
# Issue 7 dedicated syntax fixer / recitation-style escalation.
TOOL_FIX_RATE_PER_1K_INPUT_USD = PRO_RATE_PER_1K_INPUT_USD
TOOL_FIX_RATE_PER_1K_OUTPUT_USD = PRO_RATE_PER_1K_OUTPUT_USD

_PERSISTENCE_KEYWORDS = (
    "mongo",
    "database",
    "db ",
    "collection",
    "persist",
    "save to",
    "store in",
    "upsert",
    "insert",
)

_ROUTER_KEYWORDS = (
    "if ",
    "else",
    "decision",
    "branch",
    "route",
    "when ",
    "unless",
    "approve",
    "reject",
    "verify",
)

_COMPLEXITY_KEYWORDS: Dict[str, Sequence[str]] = {
    "complex": (
        "workflow",
        "multi-tenant",
        "role",
        "permission",
        "rbac",
        "websocket",
        "real-time",
        "analytics",
        "reporting",
        "pipeline",
        "orchestrat",
        "sync",
        "async",
        "queue",
        "scheduler",
        "integration",
        "oauth",
        "sso",
        "payment",
        "stripe",
    ),
    "medium": (
        "crud",
        "dashboard",
        "table",
        "pagination",
        "filter",
        "search",
        "upload",
        "download",
        "api",
        "db",
        "database",
        "auth",
        "login",
        "signup",
        "approval",
        "review",
        "export",
        "import",
    ),
}


def _flatten_step_texts(steps: Any) -> List[str]:
    if not steps:
        return []

    if isinstance(steps, (str, bytes)):
        return [str(steps)]

    out: List[str] = []

    def _step_to_text(s: Any) -> str:
        if s is None:
            return ""
        if isinstance(s, str):
            return s
        if isinstance(s, dict):
            name = (s.get("name") or "").strip()
            desc = (s.get("description") or "").strip()
            return (desc or name).strip()
        name = getattr(s, "name", None)
        desc = getattr(s, "description", None)
        name = (name or "").strip() if isinstance(name, str) else ""
        desc = (desc or "").strip() if isinstance(desc, str) else ""
        return (desc or name).strip()

    def _get_substeps(s: Any) -> Optional[Iterable[Any]]:
        if isinstance(s, dict):
            return s.get("subSteps")
        return getattr(s, "subSteps", None)

    if isinstance(steps, Sequence):
        for phase in steps:
            subs = _get_substeps(phase)
            if subs:
                for sub in subs:
                    txt = _step_to_text(sub)
                    if txt:
                        out.append(txt)
            else:
                txt = _step_to_text(phase)
                if txt:
                    out.append(txt)
        return out

    txt = _step_to_text(steps)
    return [txt] if txt else []


def _phase_step_counts(steps: Any) -> List[int]:
    """Logical step count per phase (for multi-phase scratch preview)."""
    if not steps or not isinstance(steps, Sequence):
        return [1]

    counts: List[int] = []
    for phase in steps:
        subs = phase.get("subSteps") if isinstance(phase, dict) else getattr(phase, "subSteps", None)
        if subs:
            n = sum(1 for sub in subs if _flatten_step_texts([sub]))
            counts.append(max(1, n))
        else:
            counts.append(1)
    return counts or [1]


def _substep_preferred_kind(sub: Any) -> Optional[str]:
    """Return 'ui_screen', 'tool', or None (hybrid / unset — planner may split freely)."""
    if isinstance(sub, dict):
        raw = sub.get("preferredKind")
    else:
        raw = getattr(sub, "preferredKind", None)
    if not raw:
        return None
    kind = str(raw).strip().lower()
    if kind in {"ui", "screen", "ui-screen", "ui_screen"}:
        return "ui_screen"
    if kind in {"service", "api", "tool"}:
        return "tool"
    if kind in {"hybrid", "free", "auto"}:
        return None
    return None


@dataclass
class _PhaseNodePlan:
    locked_ui_steps: int
    locked_tool_steps: int
    hybrid_step_count: int
    planned_nodes: int
    planned_edges: int
    router_count: int
    ui_screen_count: int
    tool_count: int


def _plan_phase_from_substeps(
    *,
    substeps: Optional[Iterable[Any]],
    phase_text: str,
    avg_nodes: float,
    edges_per_node: float,
    node_reuse_fraction: float,
    ui_fraction_of_all_nodes: float,
    tool_fraction_of_all_nodes: float,
) -> _PhaseNodePlan:
    """
    Steps-mode topology aligned with scratch planner preferredKind rules:
      - ui_screen → exactly one ui_screen node
      - tool      → exactly one tool node
      - hybrid / unset → heuristic avg_nodes per step (LLM may split)
    """
    locked_ui = 0
    locked_tool = 0
    hybrid_count = 0

    if substeps:
        for sub in substeps:
            if not _flatten_step_texts([sub]):
                continue
            kind = _substep_preferred_kind(sub)
            if kind == "ui_screen":
                locked_ui += 1
            elif kind == "tool":
                locked_tool += 1
            else:
                hybrid_count += 1
    else:
        hybrid_count = 1

    locked_nodes = locked_ui + locked_tool
    hybrid_nodes = int(math.ceil(hybrid_count * avg_nodes)) if hybrid_count > 0 else 0
    router_count = _estimate_router_count(locked_ui + locked_tool + hybrid_count, phase_text)
    planned_nodes = max(3, locked_nodes + hybrid_nodes + router_count + 1)  # +1 end node
    planned_edges = max(0, int(math.ceil(planned_nodes * edges_per_node)))

    generate_fraction = max(0.0, min(1.0, 1.0 - node_reuse_fraction))
    hybrid_generate = int(math.ceil(hybrid_nodes * generate_fraction)) if hybrid_nodes > 0 else 0

    ui_fraction_of_all_nodes = max(0.0, min(1.0, ui_fraction_of_all_nodes))
    tool_fraction_of_all_nodes = max(0.0, min(1.0, tool_fraction_of_all_nodes))
    denom = ui_fraction_of_all_nodes + tool_fraction_of_all_nodes
    ui_share = ui_fraction_of_all_nodes / denom if denom > 0 else 0.5

    hybrid_ui = int(math.ceil(hybrid_generate * ui_share)) if hybrid_generate > 0 else 0
    hybrid_tool = max(0, hybrid_generate - hybrid_ui)

    return _PhaseNodePlan(
        locked_ui_steps=locked_ui,
        locked_tool_steps=locked_tool,
        hybrid_step_count=hybrid_count,
        planned_nodes=planned_nodes,
        planned_edges=planned_edges,
        router_count=router_count,
        ui_screen_count=locked_ui + hybrid_ui,
        tool_count=locked_tool + hybrid_tool,
    )


def _classify_complexity(text: str, step_count: int) -> str:
    t = (text or "").lower()

    complex_hits = sum(1 for kw in _COMPLEXITY_KEYWORDS["complex"] if kw in t)
    medium_hits = sum(1 for kw in _COMPLEXITY_KEYWORDS["medium"] if kw in t)

    if step_count >= 10:
        complex_hits += 2
    elif step_count >= 6:
        medium_hits += 1

    if complex_hits >= 2 or (complex_hits >= 1 and medium_hits >= 2):
        return "complex"
    # Do not treat 4–7 capability steps (typical expander output) as "medium"
    # solely from step count; keywords + longer lists still bump complexity.
    if medium_hits >= 2 or step_count >= 8:
        return "medium"
    return "simple"


def _classify_prompt_complexity(text: str) -> str:
    """
    Prompt-mode complexity without inventing steps from punctuation.
    Uses requirement keywords + prompt size only.
    """
    t = (text or "").lower()
    tokens = _estimate_tokens_from_text(t)
    complex_hits = sum(1 for kw in _COMPLEXITY_KEYWORDS["complex"] if kw in t)
    medium_hits = sum(1 for kw in _COMPLEXITY_KEYWORDS["medium"] if kw in t)
    # Domain signals common in NL workflows
    for kw in (
        "router", "route", "approv", "ocr", "email", "sms", "kyc", "verify",
        "branch", "loop", "retry", "mongo", "database",
        "enterprise", "production", "loan", "onboard", "claim",
    ):
        if kw in t:
            medium_hits += 1
    if tokens >= 700:
        complex_hits += 2
    elif tokens >= 350:
        medium_hits += 1
    if complex_hits >= 2 or (complex_hits >= 1 and medium_hits >= 2):
        return "complex"
    if medium_hits >= 2 or tokens >= 200:
        return "medium"
    return "simple"


def _avg_nodes_per_step(complexity: str, step_count: int = 1) -> float:
    """
    Heuristic nodes invented per FREE (unlocked) step.

    Capability-dense step lists (typical /nl/expand output) stay near 1:1.
    Only very short lists (1–2 coarse steps) may split into a few nodes.
    """
    if complexity == "simple":
        base = AVG_NODES_PER_STEP_SIMPLE
    elif complexity == "complex":
        base = AVG_NODES_PER_STEP_COMPLEX
    else:
        base = AVG_NODES_PER_STEP_MEDIUM
    n = max(1, int(step_count or 1))
    if n <= 2:
        return max(base, 1.5 if complexity != "simple" else 1.25)
    if n >= 12:
        return min(base, 1.3)
    if n >= 8:
        return min(base, 2.0)
    return base


def _estimate_stage0_turns(complexity: str, step_count: int, text_tokens: int) -> int:
    base = {"simple": 2, "medium": 4, "complex": 6}.get(complexity, 3)
    if step_count >= 8:
        base += 1
    if text_tokens > 1200:
        base += 1
    return min(STAGE0_MAX_TURNS, max(2, base))


def _estimate_router_count(steps_in_phase: int, phase_text: str) -> int:
    t = (phase_text or "").lower()
    keyword_hits = sum(1 for kw in _ROUTER_KEYWORDS if kw in t)
    if steps_in_phase <= 2 and keyword_hits == 0:
        return 0
    base = max(0, int(math.ceil(steps_in_phase / 4.0)) - 1)
    if keyword_hits >= 2:
        base += 1
    return min(steps_in_phase, max(0, base))


def _needs_persistence_extract(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _PERSISTENCE_KEYWORDS)


def _estimate_tokens_from_text(text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    return max(1, int(math.ceil(len(t) / 4.0)))


def _calc_llm_cost_usd(
    *,
    input_tokens: float,
    output_tokens: float,
    rate_per_1k_input_usd: float,
    rate_per_1k_output_usd: float,
) -> float:
    return (
        (max(0.0, input_tokens) / 1000.0) * rate_per_1k_input_usd
        + (max(0.0, output_tokens) / 1000.0) * rate_per_1k_output_usd
    )


@dataclass
class NlExpanderCallEstimate:
    operation: str
    endpoint: str
    model: str
    predicted_cost: float
    estimated_cost_usd: float
    input_tokens: int
    output_tokens: int
    source: str
    sample_count: int = 0
    observed_avg_cost_usd: Optional[float] = None
    assumptions: Dict[str, Any] = field(default_factory=dict)


async def _observed_nl_expander_stats(sub_operation: str) -> Optional[Dict[str, Any]]:
    """Average successful llm_token_count rows for expand/revise."""
    try:
        from services.database import Database

        pipeline = [
            {
                "$match": {
                    "operation": "nl-enterprise-expand",
                    "sub_operation": sub_operation,
                    "status": "success",
                    "pricing.total_cost_usd": {"$gt": 0},
                }
            },
            {"$sort": {"createdAt": -1}},
            {"$limit": 50},
            {
                "$group": {
                    "_id": None,
                    "n": {"$sum": 1},
                    "avg_in": {"$avg": "$prompt_tokens"},
                    "avg_out": {"$avg": "$completion_tokens"},
                    "avg_cost": {"$avg": "$pricing.total_cost_usd"},
                    "max_cost": {"$max": "$pricing.total_cost_usd"},
                }
            },
        ]
        rows = await asyncio.wait_for(
            Database.aggregate_to_list("llm_token_count", pipeline),
            timeout=2.5,
        )
        if not rows:
            return None
        row = rows[0]
        n = int(row.get("n") or 0)
        if n <= 0:
            return None
        return {
            "sample_count": n,
            "avg_input_tokens": int(round(float(row.get("avg_in") or 0))),
            "avg_output_tokens": int(round(float(row.get("avg_out") or 0))),
            "avg_cost_usd": float(row.get("avg_cost") or 0),
            "max_cost_usd": float(row.get("max_cost") or 0),
        }
    except Exception as exc:
        logger.warning(
            "llm_token_count lookup for %s failed: %s: %s",
            sub_operation,
            type(exc).__name__,
            exc or "(no message)",
        )
        return None


def _nl_expander_heuristic_tokens(
    *,
    kind: Literal["expand", "revise"],
    prompt: str,
    steps: Any = None,
    feedback: str = "",
) -> Tuple[int, int]:
    prompt_tokens = _estimate_tokens_from_text(prompt)
    if kind == "expand":
        inp = ENTERPRISE_EXPAND_TEMPLATE_TOKENS + prompt_tokens
        out = ENTERPRISE_EXPAND_OUTPUT_TOKENS
        return max(1, inp), max(1, out)
    step_tokens = 0
    for s in steps or []:
        if isinstance(s, dict):
            text = f"{s.get('name') or ''} {s.get('description') or ''}"
        else:
            text = f"{getattr(s, 'name', '') or ''} {getattr(s, 'description', '') or ''}"
        step_tokens += _estimate_tokens_from_text(text) or ENTERPRISE_REVISE_TOKENS_PER_STEP
    inp = (
        ENTERPRISE_REVISE_TEMPLATE_TOKENS
        + prompt_tokens
        + step_tokens
        + _estimate_tokens_from_text(feedback)
    )
    out = ENTERPRISE_REVISE_OUTPUT_TOKENS
    return max(1, inp), max(1, out)


def _blend_nl_expander_estimate(
    *,
    kind: Literal["expand", "revise"],
    heuristic_in: int,
    heuristic_out: int,
    observed: Optional[Dict[str, Any]],
    safety_buffer_multiplier: float,
) -> Tuple[int, int, float, float, str, int, Optional[float], Dict[str, Any]]:
    heuristic_cost = _calc_llm_cost_usd(
        input_tokens=heuristic_in,
        output_tokens=heuristic_out,
        rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    sample_count = int((observed or {}).get("sample_count") or 0)
    observed_avg = float((observed or {}).get("avg_cost_usd") or 0) if observed else None
    assumptions: Dict[str, Any] = {
        "kind": kind,
        "model": ENTERPRISE_EXPAND_MODEL,
        "heuristic_input_tokens": heuristic_in,
        "heuristic_output_tokens": heuristic_out,
        "heuristic_cost_usd": round(heuristic_cost, 6),
        "safety_buffer_multiplier": float(safety_buffer_multiplier),
        "rates_per_1k_usd": {
            "input": PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
            "output": PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
        },
    }
    if observed and sample_count > 0:
        assumptions["observed"] = observed
        # Scale observed cost if this request's heuristic input differs a lot.
        obs_in = max(1, int(observed.get("avg_input_tokens") or heuristic_in))
        scale = max(0.6, min(2.0, heuristic_in / float(obs_in)))
        observed_scaled = float(observed["avg_cost_usd"]) * scale
        assumptions["observed_scale"] = round(scale, 3)
        assumptions["observed_scaled_cost_usd"] = round(observed_scaled, 6)
        if sample_count >= NL_EXPANDER_OBSERVED_MIN_SAMPLES:
            estimated = max(heuristic_cost, observed_scaled)
            predicted = estimated * NL_EXPANDER_OBSERVED_BUFFER
            source = "observed"
            inp = max(heuristic_in, int(observed.get("avg_input_tokens") or 0))
            out = max(heuristic_out, int(observed.get("avg_output_tokens") or 0))
        else:
            estimated = max(heuristic_cost, observed_scaled)
            predicted = estimated * safety_buffer_multiplier
            source = "blended"
            inp, out = heuristic_in, heuristic_out
        assumptions["observed_buffer"] = NL_EXPANDER_OBSERVED_BUFFER
        return inp, out, estimated, predicted, source, sample_count, observed_avg, assumptions

    predicted = heuristic_cost * safety_buffer_multiplier
    return (
        heuristic_in,
        heuristic_out,
        heuristic_cost,
        predicted,
        "default",
        0,
        None,
        assumptions,
    )


async def estimate_nl_expand_cost(
    *,
    prompt: str,
    safety_buffer_multiplier: float = SAFETY_BUFFER_MULTIPLIER,
) -> NlExpanderCallEstimate:
    """LLM cost the user may incur for one POST /nl/expand call."""
    inp, out = _nl_expander_heuristic_tokens(kind="expand", prompt=prompt or "")
    observed = await _observed_nl_expander_stats("prompt-to-enterprise-steps")
    inp, out, estimated, predicted, source, n, obs_avg, assumptions = _blend_nl_expander_estimate(
        kind="expand",
        heuristic_in=inp,
        heuristic_out=out,
        observed=observed,
        safety_buffer_multiplier=safety_buffer_multiplier,
    )
    assumptions["kind"] = "expand"
    return NlExpanderCallEstimate(
        operation="expand",
        endpoint="/aiqod-agent/agent/agentic/nl/expand",
        model=ENTERPRISE_EXPAND_MODEL,
        predicted_cost=float(predicted),
        estimated_cost_usd=float(estimated),
        input_tokens=int(inp),
        output_tokens=int(out),
        source=source,
        sample_count=n,
        observed_avg_cost_usd=obs_avg,
        assumptions=assumptions,
    )


async def estimate_nl_revise_cost(
    *,
    prompt: str,
    steps: Any,
    feedback: str,
    safety_buffer_multiplier: float = SAFETY_BUFFER_MULTIPLIER,
) -> NlExpanderCallEstimate:
    """LLM cost the user may incur for one POST /nl/steps/revise call."""
    inp, out = _nl_expander_heuristic_tokens(
        kind="revise",
        prompt=prompt or "",
        steps=steps,
        feedback=feedback or "",
    )
    observed = await _observed_nl_expander_stats("revise-enterprise-steps")
    inp, out, estimated, predicted, source, n, obs_avg, assumptions = _blend_nl_expander_estimate(
        kind="revise",
        heuristic_in=inp,
        heuristic_out=out,
        observed=observed,
        safety_buffer_multiplier=safety_buffer_multiplier,
    )
    assumptions["kind"] = "revise"
    assumptions["step_count"] = len(list(steps or []))
    return NlExpanderCallEstimate(
        operation="revise",
        endpoint="/aiqod-agent/agent/agentic/nl/steps/revise",
        model=ENTERPRISE_EXPAND_MODEL,
        predicted_cost=float(predicted),
        estimated_cost_usd=float(estimated),
        input_tokens=int(inp),
        output_tokens=int(out),
        source=source,
        sample_count=n,
        observed_avg_cost_usd=obs_avg,
        assumptions=assumptions,
    )


def _estimate_ui_screen_cost_usd_from_prompt_tokens(prompt_tokens: int) -> Dict[str, float]:
    pt = max(0, int(prompt_tokens))
    enhanced = (UI_ENHANCED_TOKEN_A * pt) + UI_ENHANCED_TOKEN_B
    cost_usd = (UI_COST_USD_A * enhanced) + UI_COST_USD_B
    retry_count = (UI_RETRY_COUNT_A * enhanced) + UI_RETRY_COUNT_B
    effective_calls = 1.0 + max(0.0, retry_count)
    effective_cost = cost_usd * effective_calls
    return {
        "prompt_tokens": float(pt),
        "enhanced_tokens": float(enhanced),
        "base_cost_usd": float(cost_usd),
        "retry_count": float(max(0.0, retry_count)),
        "effective_calls": float(effective_calls),
        "effective_cost_usd": float(effective_cost),
    }


def _estimate_ui_execute_pipeline(
    ui_count: int,
    per_ui_prompt_tokens: int,
) -> Tuple[Dict[str, float], float, float, float]:
    """Formula cost, pipeline overhead, live floor top-up, and total for GENERATE:ui_screen."""
    parts = _estimate_ui_screen_cost_usd_from_prompt_tokens(per_ui_prompt_tokens)
    if ui_count <= 0:
        return parts, 0.0, 0.0, 0.0
    base = float(parts["effective_cost_usd"]) * ui_count
    pipelined = base * max(1.0, float(UI_PIPELINE_COST_MULTIPLIER))
    floored = max(pipelined, float(UI_MIN_COST_USD_PER_SCREEN) * ui_count)
    pipeline_overhead = max(0.0, pipelined - base)
    floor_topup = max(0.0, floored - pipelined)
    return parts, base, pipeline_overhead, floor_topup


def _estimate_gap_tool_codegen(tool_count: int) -> Tuple[int, int, float, List[CostEstimateItem]]:
    """Research + flash codegen + expected syntax-fixer/pro escalation per GENERATE:tool."""
    n = max(0, int(tool_count))
    if n <= 0:
        return 0, 0, 0.0, []

    tool_in = int(
        (DEFAULT_TOOL_TEMPLATE_TOKENS + DEFAULT_TOOL_RESEARCH_TOKENS)
        * DEFAULT_TOOL_RETRY_RATE
        * n
    )
    tool_out = int(DEFAULT_TOOL_OUTPUT_TOKENS * DEFAULT_TOOL_RETRY_RATE * n)
    gen_cost = _calc_llm_cost_usd(
        input_tokens=tool_in,
        output_tokens=tool_out,
        rate_per_1k_input_usd=TOOL_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=TOOL_RATE_PER_1K_OUTPUT_USD,
    )
    items = [
        CostEstimateItem(
            kind="execute_tool_generation",
            count=n,
            input_tokens=tool_in,
            output_tokens=tool_out,
            cost_usd=gen_cost,
            rationale=(
                "gemini-3.5-flash research + codegen per GENERATE:tool "
                f"(~{DEFAULT_TOOL_TEMPLATE_TOKENS + DEFAULT_TOOL_RESEARCH_TOKENS} in / "
                f"{DEFAULT_TOOL_OUTPUT_TOKENS} out × retry {DEFAULT_TOOL_RETRY_RATE})"
            ),
        )
    ]
    fix_in = int(DEFAULT_TOOL_TEMPLATE_TOKENS * TOOL_SYNTAX_FIX_PROBABILITY * n)
    fix_out = int(DEFAULT_TOOL_OUTPUT_TOKENS * TOOL_SYNTAX_FIX_PROBABILITY * n)
    fix_cost = _calc_llm_cost_usd(
        input_tokens=fix_in,
        output_tokens=fix_out,
        rate_per_1k_input_usd=TOOL_FIX_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=TOOL_FIX_RATE_PER_1K_OUTPUT_USD,
    )
    if fix_cost > 0:
        items.append(
            CostEstimateItem(
                kind="execute_tool_syntax_fixer",
                count=n,
                input_tokens=fix_in,
                output_tokens=fix_out,
                cost_usd=fix_cost,
                rationale=(
                    f"gemini-2.5-pro syntax fixer / model escalation "
                    f"(p≈{TOOL_SYNTAX_FIX_PROBABILITY})"
                ),
            )
        )
    return tool_in + fix_in, tool_out + fix_out, gen_cost + fix_cost, items


def _ui_execute_line_items(
    ui_count: int,
    per_ui_prompt_tokens: int,
    *,
    remaining: bool = False,
) -> Tuple[Dict[str, float], float, int, int, List[CostEstimateItem]]:
    parts, base, pipeline_overhead, floor_topup = _estimate_ui_execute_pipeline(
        ui_count, per_ui_prompt_tokens
    )
    items: List[CostEstimateItem] = []
    if ui_count <= 0:
        return parts, 0.0, 0, 0, items
    scope = "GENERATE:ui_screen" if remaining else "IR + backend + Angular per screen"
    items.append(
        CostEstimateItem(
            kind="execute_ui_screen",
            count=ui_count,
            input_tokens=int(parts["prompt_tokens"]) * ui_count,
            output_tokens=int(parts["enhanced_tokens"]) * ui_count,
            cost_usd=base,
            rationale=(
                f"{scope}; per_screen_prompt_tokens≈{per_ui_prompt_tokens}, "
                f"effective_calls≈{parts['effective_calls']:.2f}"
            ),
        )
    )
    if pipeline_overhead > 0:
        items.append(
            CostEstimateItem(
                kind="execute_ui_pipeline_buffer",
                count=ui_count,
                input_tokens=0,
                output_tokens=0,
                cost_usd=pipeline_overhead,
                rationale=(
                    f"×{UI_PIPELINE_COST_MULTIPLIER:.2f} headroom — Angular 3.1-pro "
                    "recitation/fix loops and gap-resolution round 2"
                ),
            )
        )
    if floor_topup > 0:
        items.append(
            CostEstimateItem(
                kind="execute_ui_live_floor",
                count=ui_count,
                input_tokens=0,
                output_tokens=0,
                cost_usd=floor_topup,
                rationale=(
                    f"min ${UI_MIN_COST_USD_PER_SCREEN:.2f}/screen after pipeline "
                    "(live LinkedIn screen 1 spent $0.93 under a $0.90 cap)"
                ),
            )
        )
    total = base + pipeline_overhead + floor_topup
    in_tok = int(parts["prompt_tokens"]) * ui_count
    out_tok = int(parts["enhanced_tokens"]) * ui_count
    return parts, total, in_tok, out_tok, items


def _estimate_ui_datasource_enricher_item(
    *,
    ui_count: int,
    per_ui_prompt_tokens: int,
) -> Optional[CostEstimateItem]:
    """Cost of IR Step 5.5 — production-grade dataSource description/databaseLogic."""
    if ui_count <= 0:
        return None
    ds_per_screen = max(1, int(DS_ENRICH_AVG_DS_PER_SCREEN))
    batches_per_screen = max(1, int(math.ceil(ds_per_screen / DS_ENRICH_DS_PER_BATCH)))
    batches = batches_per_screen * ui_count
    ds_count = ds_per_screen * ui_count
    en_in = int(
        batches
        * (
            DS_ENRICH_TEMPLATE_TOKENS
            + max(0, int(per_ui_prompt_tokens))
            + ds_per_screen * DS_ENRICH_INPUT_PER_DS
        )
        * DS_ENRICH_RETRY_FACTOR
    )
    en_out = int(ds_count * DS_ENRICH_OUTPUT_PER_DS * DS_ENRICH_RETRY_FACTOR)
    en_cost = _calc_llm_cost_usd(
        input_tokens=en_in,
        output_tokens=en_out,
        rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    return CostEstimateItem(
        kind="execute_ui_datasource_enricher",
        count=ui_count,
        input_tokens=en_in,
        output_tokens=en_out,
        cost_usd=en_cost,
        rationale=(
            f"IR Step 5.5 production-grade dataSource specs ×{ui_count} screen(s), "
            f"≈{ds_per_screen} DS/screen, batch≤{DS_ENRICH_DS_PER_BATCH}, "
            f"retry≈{DS_ENRICH_RETRY_FACTOR}"
        ),
    )


def _split_generate_nodes(
    planned_nodes: int,
    *,
    hinted_ui: int,
    hinted_tool: int,
    node_reuse_fraction: float,
    ui_fraction_of_all_nodes: float,
    tool_fraction_of_all_nodes: float,
) -> Tuple[int, int]:
    generate_nodes = max(0, int(math.ceil(planned_nodes * max(0.0, min(1.0, 1.0 - node_reuse_fraction)))))
    hinted_total = hinted_ui + hinted_tool
    if hinted_total > 0:
        if generate_nodes >= hinted_total:
            remaining = generate_nodes - hinted_ui - hinted_tool
            extra_ui = int(math.ceil(remaining * 0.5))
            ui_count = hinted_ui + extra_ui
            tool_count = hinted_tool + (remaining - extra_ui)
        else:
            ui_share = hinted_ui / hinted_total
            ui_count = int(round(generate_nodes * ui_share))
            tool_count = generate_nodes - ui_count
        return ui_count, tool_count

    ui_fraction_of_all_nodes = max(0.0, min(1.0, ui_fraction_of_all_nodes))
    tool_fraction_of_all_nodes = max(0.0, min(1.0, tool_fraction_of_all_nodes))
    denom = ui_fraction_of_all_nodes + tool_fraction_of_all_nodes
    ui_share = ui_fraction_of_all_nodes / denom if denom > 0 else 0.5
    ui_count = int(math.ceil(generate_nodes * ui_share))
    tool_count = max(0, generate_nodes - ui_count)
    return ui_count, tool_count


def estimate_reports_cost(
    reports: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, int, float, List[Dict[str, Any]]]:
    """
    Estimate input tokens, output tokens, and USD cost for generating dashboard reports.
    Returns: (total_input_tokens, total_output_tokens, total_cost_usd, detailed_report_estimates)
    """
    if not reports:
        return 0, 0, 0.0, []

    total_in = 0
    total_out = 0
    detailed_estimates = []
    RETRY_MULTIPLIER = 1.3

    for idx, report in enumerate(reports):
        name = report.get("reportName") or f"Report {idx+1}"
        rtype = str(report.get("reportType") or "table").strip().lower()
        if rtype == "summary":
            multiplier = 1.0
        elif rtype == "table":
            multiplier = 1.5
        elif rtype == "chart":
            multiplier = 2.0
        elif rtype == "heatmap":
            multiplier = 2.5
        elif rtype == "pivot":
            multiplier = 2.5
        else:
            multiplier = 2.0
        base_input = 3000
        base_output = 1000
        input_tokens = base_input * multiplier
        output_tokens = base_output * multiplier
        fields = report.get("requiredFields") or []
        filters = report.get("optionalFilters") or []
        
        input_tokens += len(fields) * 150
        output_tokens += len(fields) * 75

        input_tokens += len(filters) * 200
        output_tokens += len(filters) * 100

        # Aggregation complexity (requires mongodb/sql aggregation pipelines, group-by logic)
        agg = report.get("aggregation")
        if agg and str(agg).strip():
            input_tokens += 800
            output_tokens += 400

        # Apply retry factor for code generation
        final_in = int(math.ceil(input_tokens * RETRY_MULTIPLIER))
        final_out = int(math.ceil(output_tokens * RETRY_MULTIPLIER))

        # Calculate cost using Pro tier pricing
        cost_usd = (final_in / 1000.0) * PRO_RATE_PER_1K_INPUT_USD + (final_out / 1000.0) * PRO_RATE_PER_1K_OUTPUT_USD

        total_in += final_in
        total_out += final_out

        detailed_estimates.append({
            "reportName": name,
            "reportType": report.get("reportType"),
            "inputTokens": final_in,
            "outputTokens": final_out,
            "costUsd": cost_usd,
            "multiplierUsed": multiplier,
            "hasAggregation": bool(agg and str(agg).strip()),
        })

    total_cost_usd = (total_in / 1000.0) * PRO_RATE_PER_1K_INPUT_USD + (total_out / 1000.0) * PRO_RATE_PER_1K_OUTPUT_USD
    return total_in, total_out, total_cost_usd, detailed_estimates


def estimate_workflow_cost(
    *,
    prompt: Optional[str] = None,
    steps: Any = None,
    suggested_reports: Optional[List[Dict[str, Any]]] = None,
    node_reuse_fraction: float = DEFAULT_NODE_REUSE_FRACTION,
    ui_fraction_of_all_nodes: float = DEFAULT_UI_FRACTION_OF_ALL_NODES,
    tool_fraction_of_all_nodes: float = DEFAULT_TOOL_FRACTION_OF_ALL_NODES,
    edges_per_node: float = DEFAULT_EDGES_PER_NODE,
    safety_buffer_multiplier: float = SAFETY_BUFFER_MULTIPLIER,
    ui_prompt_tokens_default: int = DEFAULT_UI_PROMPT_TOKENS,
) -> PlanCostEstimate:
    """
    Pre-flight workflow cost estimator for the full builder pipeline:
    Stage 0 → scratch preview → execute (gap resolution).

    No LLM calls. Intended for POST /agentic/plan/estimate before register/Stage 0.
    """
    if (prompt and prompt.strip()) and steps:
        raise ValueError("Provide exactly one of `prompt` or `steps`.")
    if not (prompt and prompt.strip()) and not steps:
        raise ValueError("Either `prompt` (non-empty) or `steps` must be provided.")

    is_prompt_mode = bool(prompt and prompt.strip())
    step_texts = _flatten_step_texts(steps) if steps else []
    step_count = len(step_texts) if steps else max(1, len(re.findall(r"\n+|[.;]", prompt or "")) // 1)

    if is_prompt_mode:
        base_text = prompt.strip()
        phase_count = 1
        phase_step_counts = [step_count]
    else:
        base_text = " ".join(t for t in step_texts if t).strip()
        phase_step_counts = _phase_step_counts(steps)
        phase_count = max(1, len(phase_step_counts))

    complexity = _classify_complexity(base_text, step_count=step_count)
    avg_nodes = _avg_nodes_per_step(complexity, step_count)
    base_text_tokens = _estimate_tokens_from_text(base_text)

    phase_nodes: List[int] = []
    phase_edges: List[int] = []
    phase_routers: List[int] = []
    phase_texts: List[str] = []
    locked_ui_steps = 0
    locked_tool_steps = 0
    hybrid_step_count = 0
    ui_count = 0
    tool_count = 0

    if steps and isinstance(steps, Sequence):
        for phase in steps:
            subs = _get_phase_substeps(phase)
            phase_texts.append(
                _flatten_step_texts([phase])[0]
                if not subs
                else " ".join(_flatten_step_texts(subs))
            )
            plan = _plan_phase_from_substeps(
                substeps=subs,
                phase_text=phase_texts[-1],
                avg_nodes=avg_nodes,
                edges_per_node=edges_per_node,
                node_reuse_fraction=node_reuse_fraction,
                ui_fraction_of_all_nodes=ui_fraction_of_all_nodes,
                tool_fraction_of_all_nodes=tool_fraction_of_all_nodes,
            )
            phase_nodes.append(plan.planned_nodes)
            phase_edges.append(plan.planned_edges)
            phase_routers.append(plan.router_count)
            locked_ui_steps += plan.locked_ui_steps
            locked_tool_steps += plan.locked_tool_steps
            hybrid_step_count += plan.hybrid_step_count
            ui_count += plan.ui_screen_count
            tool_count += plan.tool_count
        planned_nodes = sum(phase_nodes)
        planned_edges = sum(phase_edges)
    else:
        nodes = max(3, int(math.ceil(step_count * avg_nodes)))
        edges = max(0, int(math.ceil(nodes * edges_per_node)))
        phase_nodes = [nodes]
        phase_edges = [edges]
        phase_routers = [_estimate_router_count(step_count, base_text)]
        planned_nodes = nodes
        planned_edges = edges
        ui_count, tool_count = _split_generate_nodes(
            planned_nodes,
            hinted_ui=0,
            hinted_tool=0,
            node_reuse_fraction=node_reuse_fraction,
            ui_fraction_of_all_nodes=ui_fraction_of_all_nodes,
            tool_fraction_of_all_nodes=tool_fraction_of_all_nodes,
        )

    items: List[CostEstimateItem] = []
    total_input = 0
    total_output = 0
    stage0_cost = 0.0
    preview_cost = 0.0
    execute_cost = 0.0

    # ---- Stage 0 clarification ----
    stage0_turns = _estimate_stage0_turns(complexity, step_count, base_text_tokens)
    stage0_input = 0
    stage0_output = 0
    for turn in range(1, stage0_turns + 1):
        stage0_input += STAGE0_SYSTEM_TOKENS + STAGE0_USER_BASE_TOKENS + base_text_tokens + (turn - 1) * STAGE0_USER_PER_TURN_GROWTH
        stage0_output += STAGE0_OUTPUT_TOKENS_PER_TURN
    stage0_cost = _calc_llm_cost_usd(
        input_tokens=stage0_input,
        output_tokens=stage0_output,
        rate_per_1k_input_usd=FLASH_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=FLASH_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="stage0_clarification",
            count=stage0_turns,
            input_tokens=stage0_input,
            output_tokens=stage0_output,
            cost_usd=stage0_cost,
            rationale=f"heuristic {stage0_turns} gemini-3-flash-preview turn(s) before scratch preview",
        )
    )

    # Prompt-mode NL no longer runs analyze_requirements_with_llm; expander
    # already produced steps. Do not charge a phantom requirement-analysis call.

    # ---- Scratch preview per phase ----
    skeleton_in_total = 0
    skeleton_out_total = 0
    skeleton_cost_total = 0.0
    enrich_in_total = 0
    enrich_out_total = 0
    enrich_cost_total = 0.0
    schema_in_total = 0
    schema_out_total = 0
    schema_cost_total = 0.0
    router_in_total = 0
    router_out_total = 0
    router_cost_total = 0.0
    persistence_calls = 0
    persistence_in_total = 0
    persistence_out_total = 0
    persistence_cost_total = 0.0

    for idx, (nodes_p, edges_p, routers_p) in enumerate(zip(phase_nodes, phase_edges, phase_routers)):
        phase_text = phase_texts[idx] if idx < len(phase_texts) else base_text
        phase_text_tokens = _estimate_tokens_from_text(phase_text)

        sk_in = int((SKELETON_TEMPLATE_TOKENS + phase_text_tokens + base_text_tokens) * SKELETON_RETRY_FACTOR)
        sk_out = int((nodes_p * SKELETON_OUTPUT_PER_NODE + edges_p * SKELETON_OUTPUT_PER_EDGE) * SKELETON_RETRY_FACTOR)
        sk_cost = _calc_llm_cost_usd(
            input_tokens=sk_in,
            output_tokens=sk_out,
            rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
            rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
        )
        skeleton_in_total += sk_in
        skeleton_out_total += sk_out
        skeleton_cost_total += sk_cost

        enrichable = max(1, nodes_p - 1)  # skip end node
        enrich_batches = max(1, int(math.ceil(enrichable / DESC_ENRICH_NODES_PER_BATCH)))
        en_in = int(
            enrich_batches
            * (DESC_ENRICH_TEMPLATE_TOKENS + phase_text_tokens + nodes_p * DESC_ENRICH_INPUT_PER_NODE)
            * DESC_ENRICH_RETRY_FACTOR
        )
        en_out = int(
            enrichable * DESC_ENRICH_OUTPUT_PER_NODE * DESC_ENRICH_RETRY_FACTOR
        )
        en_cost = _calc_llm_cost_usd(
            input_tokens=en_in,
            output_tokens=en_out,
            rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
            rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
        )
        enrich_in_total += en_in
        enrich_out_total += en_out
        enrich_cost_total += en_cost

        eligible = max(1, nodes_p - routers_p - 1)
        batches = max(1, int(math.ceil(eligible / NODES_PER_SCHEMA_BATCH)))
        batch_in = int((SCHEMA_BATCH_INPUT_BASE + eligible * SCHEMA_BATCH_INPUT_PER_NODE) * SCHEMA_BATCH_RETRY_FACTOR)
        batch_out = int(eligible * SCHEMA_BATCH_OUTPUT_PER_NODE * SCHEMA_BATCH_RETRY_FACTOR)
        sch_in = batch_in * batches
        sch_out = batch_out * batches
        sch_cost = _calc_llm_cost_usd(
            input_tokens=sch_in,
            output_tokens=sch_out,
            rate_per_1k_input_usd=PRO_RATE_PER_1K_INPUT_USD,
            rate_per_1k_output_usd=PRO_RATE_PER_1K_OUTPUT_USD,
        )
        schema_in_total += sch_in
        schema_out_total += sch_out
        schema_cost_total += sch_cost

        if routers_p > 0:
            r_in = int(routers_p * ROUTER_EDGE_INPUT_TOKENS * ROUTER_EDGE_RETRY_FACTOR)
            r_out = int(routers_p * ROUTER_EDGE_OUTPUT_TOKENS * ROUTER_EDGE_RETRY_FACTOR)
            r_cost = _calc_llm_cost_usd(
                input_tokens=r_in,
                output_tokens=r_out,
                rate_per_1k_input_usd=FLASH_RATE_PER_1K_INPUT_USD,
                rate_per_1k_output_usd=FLASH_RATE_PER_1K_OUTPUT_USD,
            )
            router_in_total += r_in
            router_out_total += r_out
            router_cost_total += r_cost

        if _needs_persistence_extract(phase_text) or _needs_persistence_extract(base_text):
            persistence_calls += 1
            persistence_in_total += PERSISTENCE_INPUT_TOKENS
            persistence_out_total += PERSISTENCE_OUTPUT_TOKENS
            persistence_cost_total += _calc_llm_cost_usd(
                input_tokens=PERSISTENCE_INPUT_TOKENS,
                output_tokens=PERSISTENCE_OUTPUT_TOKENS,
                rate_per_1k_input_usd=FLASH_RATE_PER_1K_INPUT_USD,
                rate_per_1k_output_usd=FLASH_RATE_PER_1K_OUTPUT_USD,
            )

    items.append(
        CostEstimateItem(
            kind="scratch_skeleton",
            count=phase_count,
            input_tokens=skeleton_in_total,
            output_tokens=skeleton_out_total,
            cost_usd=skeleton_cost_total,
            rationale=f"per-phase skeleton planner ×{phase_count}, retry≈{SKELETON_RETRY_FACTOR}",
        )
    )
    items.append(
        CostEstimateItem(
            kind="scratch_description_enricher",
            count=phase_count,
            input_tokens=enrich_in_total,
            output_tokens=enrich_out_total,
            cost_usd=enrich_cost_total,
            rationale=(
                f"Stage 1.2 production-grade node descriptions ×{phase_count}, "
                f"batch≤{DESC_ENRICH_NODES_PER_BATCH}, retry≈{DESC_ENRICH_RETRY_FACTOR}"
            ),
        )
    )
    items.append(
        CostEstimateItem(
            kind="scratch_node_schemas",
            count=sum(max(1, int(math.ceil(max(1, n - r - 1) / NODES_PER_SCHEMA_BATCH))) for n, r in zip(phase_nodes, phase_routers)),
            input_tokens=schema_in_total,
            output_tokens=schema_out_total,
            cost_usd=schema_cost_total,
            rationale=f"ceil(nodes/6) schema batches per phase, retry≈{SCHEMA_BATCH_RETRY_FACTOR}",
        )
    )
    if router_in_total > 0:
        items.append(
            CostEstimateItem(
                kind="scratch_router_edges",
                count=sum(phase_routers),
                input_tokens=router_in_total,
                output_tokens=router_out_total,
                cost_usd=router_cost_total,
                rationale=f"edge mapper per router, retry≈{ROUTER_EDGE_RETRY_FACTOR}",
            )
        )
    if persistence_calls > 0:
        items.append(
            CostEstimateItem(
                kind="scratch_persistence",
                count=persistence_calls,
                input_tokens=persistence_in_total,
                output_tokens=persistence_out_total,
                cost_usd=persistence_cost_total,
                rationale="optional stage4c persistence extract when DB keywords detected",
            )
        )

    preview_cost += skeleton_cost_total + enrich_cost_total + schema_cost_total + router_cost_total + persistence_cost_total
    preview_variance_cost = preview_cost * PREVIEW_COST_BUFFER_FRACTION
    preview_cost += preview_variance_cost
    items.append(
        CostEstimateItem(
            kind="preview_variance_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=preview_variance_cost,
            rationale=(
                f"{int(PREVIEW_COST_BUFFER_FRACTION * 100)}% buffer for scratch planner "
                "retry loops and phase variance"
            ),
        )
    )
    total_input += skeleton_in_total + enrich_in_total + schema_in_total + router_in_total + persistence_in_total
    total_output += skeleton_out_total + enrich_out_total + schema_out_total + router_out_total + persistence_out_total

    # ---- Execute: tool gaps ----
    tools_in, tools_out, tools_cost, tool_items = _estimate_gap_tool_codegen(tool_count)
    items.extend(tool_items)

    # ---- Execute: UI screen gaps ----
    per_ui_prompt_tokens = max(
        int(ui_prompt_tokens_default),
        int(math.ceil(base_text_tokens / max(1, ui_count))),
    )
    ui_cost_parts, ui_cost_total, ui_in_tok, ui_out_tok, ui_items = _ui_execute_line_items(
        ui_count, per_ui_prompt_tokens
    )
    items.extend(ui_items)

    ds_enrich_item = _estimate_ui_datasource_enricher_item(
        ui_count=ui_count,
        per_ui_prompt_tokens=per_ui_prompt_tokens,
    )
    ds_enrich_cost = 0.0
    ds_enrich_in = 0
    ds_enrich_out = 0
    if ds_enrich_item:
        items.append(ds_enrich_item)
        ds_enrich_cost = float(ds_enrich_item.cost_usd)
        ds_enrich_in = int(ds_enrich_item.input_tokens)
        ds_enrich_out = int(ds_enrich_item.output_tokens)

    # ---- Execute: Dashboard reports ----
    rep_in, rep_out, rep_cost, rep_details = estimate_reports_cost(suggested_reports)
    if suggested_reports:
        items.append(
            CostEstimateItem(
                kind="execute_dashboard_reports",
                count=len(suggested_reports),
                input_tokens=rep_in,
                output_tokens=rep_out,
                cost_usd=rep_cost,
                rationale=(
                    f"pro tier report code generation for dashboard screen; "
                    f"types include: {', '.join(str(r.get('reportType') or 'table') for r in suggested_reports)}"
                ),
            )
        )
    execute_cost = tools_cost + ui_cost_total + ds_enrich_cost + rep_cost
    revalidation_cost = execute_cost * EXECUTE_REVALIDATION_BUFFER_FRACTION
    items.append(
        CostEstimateItem(
            kind="execute_revalidation_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=revalidation_cost,
            rationale=(
                f"{int(EXECUTE_REVALIDATION_BUFFER_FRACTION * 100)}% buffer for "
                "post-gap re-validation and binding repairs"
            ),
        )
    )
    execute_cost += revalidation_cost
    repair_cost = execute_cost * EXECUTE_REPAIR_BUFFER_FRACTION
    items.append(
        CostEstimateItem(
            kind="execute_repair_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=repair_cost,
            rationale=f"{int(EXECUTE_REPAIR_BUFFER_FRACTION * 100)}% buffer for post-gap/postgen fixes",
        )
    )
    execute_cost += repair_cost

    total_input += (
        stage0_input
        + tools_in
        + ui_in_tok
        + ds_enrich_in
        + rep_in
    )
    total_output += (
        stage0_output
        + tools_out
        + ui_out_tok
        + ds_enrich_out
        + rep_out
    )

    total_cost_usd = stage0_cost + preview_cost + execute_cost
    predicted_cost = float(total_cost_usd * max(1.0, float(safety_buffer_multiplier)))

    return PlanCostEstimate(
        total_input_tokens=int(total_input),
        total_output_tokens=int(total_output),
        total_cost_usd=float(total_cost_usd),
        predicted_cost=predicted_cost,
        step_count=step_count,
        phase_count=phase_count,
        complexity=complexity,
        planned_node_count=planned_nodes,
        planned_edge_count=planned_edges,
        ui_screen_count=ui_count,
        tool_count=tool_count,
        stage0_cost_usd=float(stage0_cost),
        preview_cost_usd=float(preview_cost),
        execute_cost_usd=float(execute_cost),
        dashboard_report_count=len(suggested_reports) if suggested_reports else 0,
        items=items,
        assumptions={
            "estimator_version": "workflow_pipeline_v3",
            "base_prompt_tokens_est": base_text_tokens,
            "phase_count": phase_count,
            "phase_nodes": phase_nodes,
            "phase_edges": phase_edges,
            "phase_routers": phase_routers,
            "stage0_turns_assumed": stage0_turns,
            "avg_nodes_per_step": avg_nodes,
            "locked_ui_steps": locked_ui_steps,
            "locked_tool_steps": locked_tool_steps,
            "hybrid_step_count": hybrid_step_count,
            "safety_buffer_multiplier": float(safety_buffer_multiplier),
            "preview_cost_buffer_fraction": float(PREVIEW_COST_BUFFER_FRACTION),
            "ui_pipeline_cost_multiplier": float(UI_PIPELINE_COST_MULTIPLIER),
            "execute_revalidation_buffer_fraction": float(EXECUTE_REVALIDATION_BUFFER_FRACTION),
            "execute_repair_buffer_fraction": float(EXECUTE_REPAIR_BUFFER_FRACTION),
            "tool_retry_rate": float(DEFAULT_TOOL_RETRY_RATE),
            "ui_min_cost_usd_per_screen": float(UI_MIN_COST_USD_PER_SCREEN),
            "ui_prompt_tokens_per_screen": int(per_ui_prompt_tokens),
            "suggested_reports_detail": rep_details,
            "estimate_scope": "full_pipeline",
            "execute_included": True,
        },
    )


def estimate_prompt_mode_until_nl_plan(
    *,
    prompt: str,
    safety_buffer_multiplier: float = SAFETY_BUFFER_MULTIPLIER,
) -> PlanCostEstimate:
    """
    Prompt-mode pre-admission estimate: cost **through** `/nl/plan` only.

    Covers enterprise step expansion + Stage 0 clarification + scratch
    planning (what `/stage0/start` + `/nl/plan` run). Does **not** include
    `/plan/execute` gap codegen — that comes later from actual GENERATE
    node counts after the graph exists.

    Complexity is from keywords + prompt size. Does **not** invent step counts
    from '.' / ';' / newlines.
    """
    base_text = (prompt or "").strip()
    if not base_text:
        raise ValueError("`prompt` must be non-empty for prompt-mode estimate.")

    complexity = _classify_prompt_complexity(base_text)
    base_text_tokens = _estimate_tokens_from_text(base_text)
    expanded_prompt_tokens = base_text_tokens + ENTERPRISE_STEPS_PROMPT_TOKENS
    topo = PROMPT_UNTIL_NL_PLAN_TOPOLOGY.get(complexity) or PROMPT_UNTIL_NL_PLAN_TOPOLOGY["medium"]
    planned_nodes = int(topo["nodes"])
    planned_edges = int(topo["edges"])
    assumed_steps = int(ASSUMED_ENTERPRISE_STEPS.get(complexity, 12))
    # Boost routers when the prompt itself talks about branching
    routers_p = max(
        int(topo["routers"]),
        _estimate_router_count(assumed_steps, base_text),
    )
    phase_count = 1
    stage0_turns = _estimate_stage0_turns(
        complexity,
        step_count=assumed_steps,
        text_tokens=expanded_prompt_tokens,
    )

    items: List[CostEstimateItem] = []
    total_input = 0
    total_output = 0

    # ---- Enterprise expander (before Stage 0) ----
    exp_in = int(ENTERPRISE_EXPAND_TEMPLATE_TOKENS + base_text_tokens)
    exp_out = int(ENTERPRISE_EXPAND_OUTPUT_TOKENS)
    exp_cost = _calc_llm_cost_usd(
        input_tokens=exp_in,
        output_tokens=exp_out,
        rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="enterprise_step_expand",
            count=1,
            input_tokens=exp_in,
            output_tokens=exp_out,
            cost_usd=exp_cost,
            rationale=(
                "Prompt-mode expander before Stage 0 "
                f"(assumed ~{assumed_steps} enterprise steps)"
            ),
        )
    )

    # ---- Stage 0 (clarifies the expanded steps, not the raw one-liner) ----
    stage0_input = 0
    stage0_output = 0
    for turn in range(1, stage0_turns + 1):
        stage0_input += (
            STAGE0_SYSTEM_TOKENS
            + STAGE0_USER_BASE_TOKENS
            + expanded_prompt_tokens
            + (turn - 1) * STAGE0_USER_PER_TURN_GROWTH
        )
        stage0_output += STAGE0_OUTPUT_TOKENS_PER_TURN
    stage0_cost = _calc_llm_cost_usd(
        input_tokens=stage0_input,
        output_tokens=stage0_output,
        rate_per_1k_input_usd=FLASH_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=FLASH_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="stage0_clarification",
            count=stage0_turns,
            input_tokens=stage0_input,
            output_tokens=stage0_output,
            cost_usd=stage0_cost,
            rationale=(
                f"heuristic {stage0_turns} clarification turn(s) before /nl/plan "
                f"(complexity={complexity})"
            ),
        )
    )

    # ---- Scratch /nl/plan (enterprise steps → Stage 1 skeleton) ----
    preview_cost = 0.0
    sk_in = int(
        (SKELETON_TEMPLATE_TOKENS + expanded_prompt_tokens * 2) * SKELETON_RETRY_FACTOR
    )
    sk_out = int(
        (planned_nodes * SKELETON_OUTPUT_PER_NODE + planned_edges * SKELETON_OUTPUT_PER_EDGE)
        * SKELETON_RETRY_FACTOR
    )
    sk_cost = _calc_llm_cost_usd(
        input_tokens=sk_in,
        output_tokens=sk_out,
        rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="scratch_skeleton",
            count=1,
            input_tokens=sk_in,
            output_tokens=sk_out,
            cost_usd=sk_cost,
            rationale=f"Stage 1 skeleton inside /nl/plan (assumed ~{planned_nodes} nodes)",
        )
    )
    preview_cost += sk_cost

    enrichable = max(1, planned_nodes - 1)
    enrich_batches = max(1, int(math.ceil(enrichable / DESC_ENRICH_NODES_PER_BATCH)))
    en_in = int(
        enrich_batches
        * (
            DESC_ENRICH_TEMPLATE_TOKENS
            + expanded_prompt_tokens
            + planned_nodes * DESC_ENRICH_INPUT_PER_NODE
        )
        * DESC_ENRICH_RETRY_FACTOR
    )
    en_out = int(enrichable * DESC_ENRICH_OUTPUT_PER_NODE * DESC_ENRICH_RETRY_FACTOR)
    en_cost = _calc_llm_cost_usd(
        input_tokens=en_in,
        output_tokens=en_out,
        rate_per_1k_input_usd=PRO_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="scratch_description_enricher",
            count=enrich_batches,
            input_tokens=en_in,
            output_tokens=en_out,
            cost_usd=en_cost,
            rationale=(
                f"Stage 1.2 description enricher inside /nl/plan "
                f"(≈{enrich_batches} batch(es), ≤{DESC_ENRICH_NODES_PER_BATCH} nodes)"
            ),
        )
    )
    preview_cost += en_cost

    eligible = max(1, planned_nodes - routers_p - 1)
    batches = max(1, int(math.ceil(eligible / NODES_PER_SCHEMA_BATCH)))
    batch_in = int(
        (SCHEMA_BATCH_INPUT_BASE + eligible * SCHEMA_BATCH_INPUT_PER_NODE) * SCHEMA_BATCH_RETRY_FACTOR
    )
    batch_out = int(eligible * SCHEMA_BATCH_OUTPUT_PER_NODE * SCHEMA_BATCH_RETRY_FACTOR)
    sch_in = batch_in * batches
    sch_out = batch_out * batches
    sch_cost = _calc_llm_cost_usd(
        input_tokens=sch_in,
        output_tokens=sch_out,
        rate_per_1k_input_usd=PRO_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=PRO_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="scratch_node_schemas",
            count=batches,
            input_tokens=sch_in,
            output_tokens=sch_out,
            cost_usd=sch_cost,
            rationale=f"Stage 2a schema batches inside /nl/plan (≈{batches} batch(es))",
        )
    )
    preview_cost += sch_cost

    if routers_p > 0:
        r_in = int(routers_p * ROUTER_EDGE_INPUT_TOKENS * ROUTER_EDGE_RETRY_FACTOR)
        r_out = int(routers_p * ROUTER_EDGE_OUTPUT_TOKENS * ROUTER_EDGE_RETRY_FACTOR)
        r_cost = _calc_llm_cost_usd(
            input_tokens=r_in,
            output_tokens=r_out,
            rate_per_1k_input_usd=FLASH_RATE_PER_1K_INPUT_USD,
            rate_per_1k_output_usd=FLASH_RATE_PER_1K_OUTPUT_USD,
        )
        items.append(
            CostEstimateItem(
                kind="scratch_router_edges",
                count=routers_p,
                input_tokens=r_in,
                output_tokens=r_out,
                cost_usd=r_cost,
                rationale="Stage 3a router edge mapping inside /nl/plan",
            )
        )
        preview_cost += r_cost
    else:
        r_in = r_out = 0

    persistence_in = persistence_out = 0
    persistence_cost = 0.0
    if _needs_persistence_extract(base_text) or complexity != "simple":
        persistence_in = PERSISTENCE_INPUT_TOKENS
        persistence_out = PERSISTENCE_OUTPUT_TOKENS
        persistence_cost = _calc_llm_cost_usd(
            input_tokens=persistence_in,
            output_tokens=persistence_out,
            rate_per_1k_input_usd=FLASH_RATE_PER_1K_INPUT_USD,
            rate_per_1k_output_usd=FLASH_RATE_PER_1K_OUTPUT_USD,
        )
        items.append(
            CostEstimateItem(
                kind="scratch_persistence",
                count=1,
                input_tokens=persistence_in,
                output_tokens=persistence_out,
                cost_usd=persistence_cost,
                rationale="Stage 4c persistence extract when DB keywords detected",
            )
        )
        preview_cost += persistence_cost

    # Plan summary LLM in /nl/plan (small)
    summary_in = 800 + expanded_prompt_tokens
    summary_out = 400
    summary_cost = _calc_llm_cost_usd(
        input_tokens=summary_in,
        output_tokens=summary_out,
        rate_per_1k_input_usd=FLASH_PREVIEW_RATE_PER_1K_INPUT_USD,
        rate_per_1k_output_usd=FLASH_PREVIEW_RATE_PER_1K_OUTPUT_USD,
    )
    items.append(
        CostEstimateItem(
            kind="nl_plan_summary",
            count=1,
            input_tokens=summary_in,
            output_tokens=summary_out,
            cost_usd=summary_cost,
            rationale="Human-readable plan summary after graph assembly",
        )
    )
    preview_cost += summary_cost

    preview_variance = preview_cost * PREVIEW_COST_BUFFER_FRACTION
    items.append(
        CostEstimateItem(
            kind="preview_variance_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=preview_variance,
            rationale=(
                f"{int(PREVIEW_COST_BUFFER_FRACTION * 100)}% buffer for scratch "
                "retry loops inside /nl/plan"
            ),
        )
    )
    preview_cost += preview_variance

    total_input = (
        exp_in
        + stage0_input
        + sk_in
        + en_in
        + sch_in
        + r_in
        + persistence_in
        + summary_in
    )
    total_output = (
        exp_out
        + stage0_output
        + sk_out
        + en_out
        + sch_out
        + r_out
        + persistence_out
        + summary_out
    )
    total_cost_usd = exp_cost + stage0_cost + preview_cost
    predicted_cost = float(total_cost_usd * max(1.0, float(safety_buffer_multiplier)))

    return PlanCostEstimate(
        total_input_tokens=int(total_input),
        total_output_tokens=int(total_output),
        total_cost_usd=float(total_cost_usd),
        predicted_cost=predicted_cost,
        step_count=0,
        phase_count=phase_count,
        complexity=complexity,
        planned_node_count=planned_nodes,
        planned_edge_count=planned_edges,
        ui_screen_count=0,
        tool_count=0,
        stage0_cost_usd=float(stage0_cost + exp_cost),
        preview_cost_usd=float(preview_cost),
        execute_cost_usd=0.0,
        dashboard_report_count=0,
        items=items,
        assumptions={
            "estimator_version": "prompt_until_nl_plan_v2",
            "estimate_scope": "through_nl_plan",
            "note": (
                "Prompt-mode admission estimate: enterprise expand + Stage 0 + "
                "/nl/plan (scratch) only. Execute/gap cost is NOT included — "
                "reserve/update again after /nl/plan using topology-based estimatedCost."
            ),
            "assumed_enterprise_steps": assumed_steps,
            "complexity": complexity,
            "complexity_basis": "keywords_and_prompt_size_not_punctuation_steps",
            "assumed_preview_topology": topo,
            "assumed_routers": routers_p,
            "stage0_turns_assumed": stage0_turns,
            "base_prompt_tokens_est": base_text_tokens,
            "safety_buffer_multiplier": float(safety_buffer_multiplier),
            "preview_cost_buffer_fraction": float(PREVIEW_COST_BUFFER_FRACTION),
            "execute_included": False,
        },
    )


def estimate_execute_cost_from_topology(
    *,
    ui_screen_count: int,
    tool_count: int,
    node_count: int = 0,
    edge_count: int = 0,
    router_count: int = 0,
    base_text: str = "",
    suggested_reports: Optional[List[Dict[str, Any]]] = None,
    safety_buffer_multiplier: float = SAFETY_BUFFER_MULTIPLIER,
    ui_prompt_tokens_default: int = DEFAULT_UI_PROMPT_TOKENS,
) -> PlanCostEstimate:
    """
    Remaining-cost estimate for /plan/execute after the graph already exists.

    Use this after `/nl/plan` (or scratch preview): Stage 0 and scratch planning
    are already spent. Counts should be actual GENERATE ui_screen / tool nodes.
    """
    ui_count = max(0, int(ui_screen_count))
    tool_n = max(0, int(tool_count))
    base_text_tokens = _estimate_tokens_from_text(base_text or "")
    complexity = _classify_complexity(base_text or "", step_count=max(1, ui_count + tool_n))

    items: List[CostEstimateItem] = []
    total_input = 0
    total_output = 0

    tools_in, tools_out, tools_cost, tool_items = _estimate_gap_tool_codegen(tool_n)
    items.extend(tool_items)

    per_ui_prompt_tokens = max(
        int(ui_prompt_tokens_default),
        int(math.ceil(base_text_tokens / max(1, ui_count))) if ui_count else int(ui_prompt_tokens_default),
    )
    ui_cost_parts, ui_cost_total, ui_in_tok, ui_out_tok, ui_items = _ui_execute_line_items(
        ui_count, per_ui_prompt_tokens, remaining=True
    )
    items.extend(ui_items)

    ds_enrich_item = _estimate_ui_datasource_enricher_item(
        ui_count=ui_count,
        per_ui_prompt_tokens=per_ui_prompt_tokens,
    )
    ds_enrich_cost = 0.0
    ds_enrich_in = 0
    ds_enrich_out = 0
    if ds_enrich_item:
        items.append(ds_enrich_item)
        ds_enrich_cost = float(ds_enrich_item.cost_usd)
        ds_enrich_in = int(ds_enrich_item.input_tokens)
        ds_enrich_out = int(ds_enrich_item.output_tokens)

    rep_in, rep_out, rep_cost, rep_details = estimate_reports_cost(suggested_reports)
    if suggested_reports:
        items.append(
            CostEstimateItem(
                kind="execute_dashboard_reports",
                count=len(suggested_reports),
                input_tokens=rep_in,
                output_tokens=rep_out,
                cost_usd=rep_cost,
                rationale="dashboard report generation (remaining)",
            )
        )

    execute_cost = tools_cost + ui_cost_total + ds_enrich_cost + rep_cost
    revalidation_cost = execute_cost * EXECUTE_REVALIDATION_BUFFER_FRACTION
    items.append(
        CostEstimateItem(
            kind="execute_revalidation_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=revalidation_cost,
            rationale=(
                f"{int(EXECUTE_REVALIDATION_BUFFER_FRACTION * 100)}% buffer for "
                "post-gap re-validation"
            ),
        )
    )
    execute_cost += revalidation_cost
    repair_cost = execute_cost * EXECUTE_REPAIR_BUFFER_FRACTION
    items.append(
        CostEstimateItem(
            kind="execute_repair_buffer",
            count=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=repair_cost,
            rationale=f"{int(EXECUTE_REPAIR_BUFFER_FRACTION * 100)}% buffer for post-gap fixes",
        )
    )
    execute_cost += repair_cost

    total_input = tools_in + ui_in_tok + ds_enrich_in + rep_in
    total_output = tools_out + ui_out_tok + ds_enrich_out + rep_out
    predicted_cost = float(execute_cost * max(1.0, float(safety_buffer_multiplier)))
    planned_nodes = int(node_count) if node_count > 0 else (ui_count + tool_n + max(0, router_count) + 1)
    planned_edges = int(edge_count) if edge_count > 0 else max(0, int(math.ceil(planned_nodes * DEFAULT_EDGES_PER_NODE)))

    return PlanCostEstimate(
        total_input_tokens=int(total_input),
        total_output_tokens=int(total_output),
        total_cost_usd=float(execute_cost),
        predicted_cost=predicted_cost,
        step_count=max(1, ui_count + tool_n),
        phase_count=1,
        complexity=complexity,
        planned_node_count=planned_nodes,
        planned_edge_count=planned_edges,
        ui_screen_count=ui_count,
        tool_count=tool_n,
        stage0_cost_usd=0.0,
        preview_cost_usd=0.0,
        execute_cost_usd=float(execute_cost),
        dashboard_report_count=len(suggested_reports) if suggested_reports else 0,
        items=items,
        assumptions={
            "estimator_version": "nl_topology_execute_v2",
            "estimate_scope": "execute_remaining",
            "note": (
                "Stage 0 + scratch preview already completed; this is remaining "
                "/plan/execute (gap resolution) cost from actual GENERATE node counts."
            ),
            "ui_screen_count": ui_count,
            "tool_count": tool_n,
            "router_count": int(router_count),
            "safety_buffer_multiplier": float(safety_buffer_multiplier),
            "ui_pipeline_cost_multiplier": float(UI_PIPELINE_COST_MULTIPLIER),
            "ui_min_cost_usd_per_screen": float(UI_MIN_COST_USD_PER_SCREEN),
            "ui_prompt_tokens_per_screen": int(per_ui_prompt_tokens),
            "suggested_reports_detail": rep_details,
        },
    )


def _get_phase_substeps(phase: Any) -> Optional[Iterable[Any]]:
    if isinstance(phase, dict):
        return phase.get("subSteps")
    return getattr(phase, "subSteps", None)


def estimator_phases_from_logical_steps(steps: Any, *, phase_name: str = "Workflow") -> List[Dict[str, Any]]:
    """
    Wrap a flat LogicalStep list (from /nl/expand) as one estimator phase.

    ``estimate_workflow_cost`` reads preferredKind from phase.subSteps.
    Passing expander steps as a top-level array would treat each as its own
    phase and over-count skeleton/schema LLM calls.
    """
    dumped: List[Dict[str, Any]] = []
    for s in steps or []:
        if hasattr(s, "model_dump"):
            dumped.append(s.model_dump(mode="json"))
        elif isinstance(s, dict):
            dumped.append(dict(s))
        else:
            dumped.append(
                {
                    "stepId": getattr(s, "stepId", "") or "",
                    "name": getattr(s, "name", "") or "",
                    "description": getattr(s, "description", "") or "",
                    "preferredKind": getattr(s, "preferredKind", None),
                }
            )
    texts = _flatten_step_texts(dumped)
    return [
        {
            "stepId": "phase_1",
            "name": phase_name,
            "description": " ".join(texts)[:800] or phase_name,
            "subSteps": dumped,
        }
    ]


def estimate_plan_cost_rough(
    *,
    prompt: Optional[str] = None,
    steps: Any = None,
    **kwargs: Any,
) -> PlanCostEstimate:
    """Backward-compatible alias for estimate_workflow_cost."""
    return estimate_workflow_cost(prompt=prompt, steps=steps, **kwargs)


def _test_plan_credit_estimator() -> None:
    samples: List[Tuple[str, Dict[str, Any]]] = [
        (
            "simple prompt",
            {"prompt": "Create a simple login screen with email and password."},
        ),
        (
            "medium prompt",
            {
                "prompt": "Build a CRUD dashboard with authentication, a data table, filtering, pagination, and CSV export.",
            },
        ),
        (
            "complex prompt",
            {
                "prompt": (
                    "Create a multi-tenant admin workflow with role-based permissions, analytics, "
                    "real-time updates via websocket, and integration with an external API."
                ),
            },
        ),
        (
            "steps payload (phase/subSteps)",
            {
                "steps": [
                    {
                        "stepId": "phase_1",
                        "name": "Phase 1",
                        "description": "User onboarding",
                        "subSteps": [
                            {"stepId": "L1", "name": "Login", "description": "Login with email/password"},
                            {"stepId": "L2", "name": "Profile", "description": "Collect user profile details"},
                            {"stepId": "L3", "name": "Verification", "description": "Email OTP verification"},
                        ],
                    }
                ],
            },
        ),
        (
            "locked preferredKind (ui + tool only)",
            {
                "steps": [
                    {
                        "stepId": "phase_1",
                        "name": "Phase 1",
                        "description": "Onboarding",
                        "subSteps": [
                            {"stepId": "L1", "name": "Form", "description": "User form", "preferredKind": "ui_screen"},
                            {"stepId": "L2", "name": "API", "description": "Call API", "preferredKind": "tool"},
                            {"stepId": "L3", "name": "Review", "description": "Review and branch", "preferredKind": "hybrid"},
                        ],
                    }
                ],
            },
        ),
        (
            "dashboard with suggested reports",
            {
                "steps": [
                    {
                        "stepId": "phase_1",
                        "name": "Phase 1",
                        "description": "Onboarding and Analytics",
                        "subSteps": [
                            {"stepId": "L1", "name": "Form", "description": "User form", "preferredKind": "ui_screen"},
                        ],
                    }
                ],
                "suggested_reports": [
                    {
                        "reportName": "All Persons Data",
                        "description": "A comprehensive list of all recorded persons, including their names, ages, and countries.",
                        "reportType": "Table",
                        "requiredFields": ["person_name", "age", "country"],
                        "optionalFilters": ["country", "age"],
                        "aggregation": None
                    },
                    {
                        "reportName": "Persons Count by Country",
                        "description": "A chart illustrating the distribution of recorded persons across different countries.",
                        "reportType": "Chart",
                        "requiredFields": ["country"],
                        "optionalFilters": ["age"],
                        "aggregation": "COUNT(person_name) GROUP BY country"
                    }
                ]
            }
        ),
    ]

    for title, kwargs in samples:
        est = estimate_workflow_cost(**kwargs)
        print()
        print("=" * 80)
        print(title)
        print(f"total_cost_usd={est.total_cost_usd:.6f} predicted_cost={est.predicted_cost:.6f}")
        print(f"stage0={est.stage0_cost_usd:.4f} preview={est.preview_cost_usd:.4f} execute={est.execute_cost_usd:.4f}")
        print(f"step_count={est.step_count} phase_count={est.phase_count} complexity={est.complexity}")
        print(
            f"planned_nodes={est.planned_node_count} ui={est.ui_screen_count} tool={est.tool_count}"
        )
        for item in est.items:
            print(f"- {item.kind}: count={item.count}, cost_usd={item.cost_usd:.6f}")


if __name__ == "__main__":
    _test_plan_credit_estimator()
