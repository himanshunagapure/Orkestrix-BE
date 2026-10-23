"""
Persist NL plan preview into Process_Configs the same way
/agentic/plan/scratch/preview does for steps mode.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _step_as_dict(step: Any) -> Dict[str, Any]:
    if hasattr(step, "model_dump"):
        raw = step.model_dump(mode="json")
    elif isinstance(step, dict):
        raw = dict(step)
    else:
        raw = {
            "stepId": getattr(step, "stepId", None),
            "name": getattr(step, "name", None),
            "description": getattr(step, "description", None),
            "inputs": list(getattr(step, "inputs", None) or []),
            "outputs": list(getattr(step, "outputs", None) or []),
        }
    return {
        "stepId": raw.get("stepId") or "",
        "name": raw.get("name") or "",
        "description": raw.get("description") or "",
        "inputs": list(raw.get("inputs") or []),
        "outputs": list(raw.get("outputs") or []),
    }


def _phase_graph_as_dict(phase: Any) -> Dict[str, Any]:
    if hasattr(phase, "model_dump"):
        dumped = phase.model_dump(mode="json", exclude={"langgraph_code"})
        dumped.pop("langgraph_code", None)
        dumped.pop("langgraph_code_manual", None)
        dumped.pop("langgraph_code_llm", None)
        return dumped
    if isinstance(phase, dict):
        out = {k: v for k, v in phase.items() if not str(k).startswith("langgraph_code")}
        return out
    return {"graph": phase}


def _nested_steps_for_preview(
    *,
    steps: List[Any],
    workflow_name: str,
    prompt: str,
    phase_graphs: List[Any],
) -> List[Dict[str, Any]]:
    """Match scratch/preview nested phase → subSteps shape."""
    phase_type = "FREEZE"
    phase_id = "phase_1"
    phase_name = workflow_name or "Phase 1"
    if phase_graphs:
        first = phase_graphs[0]
        graph = {}
        if isinstance(first, dict):
            phase_type = first.get("phaseType") or phase_type
            graph = first.get("graph") or {}
        elif hasattr(first, "phaseType"):
            phase_type = getattr(first, "phaseType", None) or phase_type
            graph = getattr(first, "graph", None) or {}
        if isinstance(graph, dict):
            phase_id = graph.get("phaseId") or graph.get("graphId") or phase_id
            phase_name = graph.get("phaseName") or graph.get("name") or phase_name

    return [
        {
            "stepId": phase_id,
            "name": phase_name,
            "description": prompt or phase_name,
            "phaseType": phase_type,
            "subSteps": [_step_as_dict(s) for s in (steps or [])],
        }
    ]


def _normalize_analysis_source(value: Any) -> Optional[str]:
    """Execute / Stage 0 only accept llm|fallback (or null)."""
    if value in ("llm", "fallback"):
        return value
    if value in (None, ""):
        return None
    # NL historically returned "prompt" — map to llm for execute compatibility.
    return "llm"


def _tenant_object_ids(
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> Dict[str, Any]:
    from bson import ObjectId

    out: Dict[str, Any] = {}
    for key, raw in (
        ("orgId", org_id),
        ("subscriberId", subscriber_id),
        ("userId", user_id),
    ):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            out[key] = ObjectId(raw) if isinstance(raw, str) else raw
        except Exception:
            out[key] = raw
    return out


def _find_existing_process_config(
    db: Any,
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Locate the register-created Process_Configs doc.

    Prefer the full tenant query; if that misses (e.g. register omitted userId
    but /nl/plan sent it), fall back to engineType + workflow_id (+ subscriber).
    Returns (doc, query_to_use_for_update).
    """
    from agent_new import _agentic_workflow_tenant_query

    primary = {
        **_agentic_workflow_tenant_query(
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        ),
        "agentic_process.workflow_id": project_id,
    }
    doc = db["Process_Configs"].find_one(primary)
    if doc is not None:
        return doc, primary

    # Fallback: avoid creating a duplicate when tenant fields differ slightly.
    fallback: Dict[str, Any] = {
        "engineType": "agentic_graph",
        "agentic_process.workflow_id": project_id,
    }
    if subscriber_id is not None:
        from bson import ObjectId

        try:
            fallback["subscriberId"] = (
                ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            )
        except Exception:
            fallback["subscriberId"] = subscriber_id

    doc = db["Process_Configs"].find_one(fallback)
    if doc is not None:
        logger.info(
            "[nl/plan] Process_Configs matched via fallback query projectId=%s",
            project_id,
        )
        # Update by _id so we never create a second registration doc.
        return doc, {"_id": doc["_id"]}

    return None, primary


def persist_nl_preview_to_process_configs(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    selected_name: Optional[str],
    selected_type: Optional[str],
    selected_category: Optional[str],
    prompt: Optional[str],
    plan_response: Any,
    token: Optional[str] = None,
    all_collections: Optional[List[Any]] = None,
    collection_metadata: Optional[List[Any]] = None,
    kg_selected_collections: Optional[List[Any]] = None,
    suggested_reports: Optional[List[Any]] = None,
) -> bool:
    """
    Upsert Process_Configs.agentic_process preview fields identical to
    /agentic/plan/scratch/preview.

    Preserves top-level billing fields from /workflow/register (predictedCost,
    reservedUsd, etc.) because this only $sets selected* + agentic_process.
    Does not overwrite selectedType/selectedCategory/token with None.

    Never creates a new Process_Configs row. Admission must have run
    /workflow/register first; upserting here would drop predictedCost and
    duplicate the workflow under a different tenant query.
    """
    if not project_id:
        logger.warning("[nl/plan] skip Process_Configs persist: missing projectId")
        return False

    try:
        from agent_new import db
    except Exception as exc:
        logger.warning("[nl/plan] Process_Configs persist import failed: %s", exc)
        return False

    workflow_name = (
        getattr(plan_response, "workflowName", None)
        or selected_name
        or "NL Workflow"
    )
    mode = getattr(plan_response, "mode", None) or "prompt"
    analysis_source = _normalize_analysis_source(
        getattr(plan_response, "analysis_source", None)
    )
    warnings = list(getattr(plan_response, "warnings", None) or [])
    phase_graphs = list(getattr(plan_response, "phaseGraphs", None) or [])
    steps = list(getattr(plan_response, "steps", None) or [])

    try:
        existing, query = _find_existing_process_config(
            db,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning("[nl/plan] Process_Configs lookup failed: %s", exc)
        return False

    if existing is None:
        logger.warning(
            "[nl/plan] skip Process_Configs persist: no register doc for projectId=%s "
            "(call /workflow/register first)",
            project_id,
        )
        return False

    existing_agentic = existing.get("agentic_process") or {}
    kind = existing_agentic.get("kind") or "workflow"
    reports = existing_agentic.get("reports")
    if reports is None:
        reports = []
    preserved_suggested = (
        suggested_reports
        if suggested_reports is not None
        else (existing_agentic.get("suggestedReports") or [])
    )

    # Prefer request values; fall back to register so revise doesn't wipe metadata.
    resolved_name = selected_name or existing.get("selectedName") or workflow_name
    resolved_type = selected_type if selected_type is not None else existing.get("selectedType")
    resolved_category = (
        selected_category if selected_category is not None else existing.get("selectedCategory")
    )
    resolved_token = token if token is not None else existing.get("token")

    tenant_set = _tenant_object_ids(org_id, subscriber_id, user_id)

    persisted_steps = _nested_steps_for_preview(
        steps=steps,
        workflow_name=workflow_name,
        prompt=prompt or "",
        phase_graphs=phase_graphs,
    )
    phase_graphs_dump = [_phase_graph_as_dict(p) for p in phase_graphs]

    now = datetime.utcnow()
    agentic_process = {
        "workflow_id": project_id,
        "workflowName": workflow_name,
        "projectId": project_id,
        "kind": kind,
        "mode": mode,
        "analysis_source": analysis_source,
        "prompt": prompt,
        "steps": persisted_steps,
        "phaseGraphs": phase_graphs_dump,
        "warnings": warnings,
        # Same as scratch/preview (execute cost is returned on the NL response / estimate APIs).
        "estimatedCredits": 0,
        "planner": "scratch",
        "status": "preview",
        "allCollections": all_collections or existing_agentic.get("allCollections") or [],
        "collectionMetadata": (
            collection_metadata or existing_agentic.get("collectionMetadata") or []
        ),
        "kgSelectedCollections": (
            kg_selected_collections
            or existing_agentic.get("kgSelectedCollections")
            or []
        ),
        "suggestedReports": preserved_suggested,
        "reports": reports,
    }

    set_fields: Dict[str, Any] = {
        **tenant_set,
        "engineType": "agentic_graph",
        "selectedName": resolved_name,
        "updated_at": now,
        "agentic_process": agentic_process,
    }
    # Only set optional metadata when we have a value (avoid wiping register fields).
    if resolved_type is not None:
        set_fields["selectedType"] = resolved_type
    if resolved_category is not None:
        set_fields["selectedCategory"] = resolved_category
    if resolved_token is not None:
        set_fields["token"] = resolved_token

    try:
        result = db["Process_Configs"].update_one(query, {"$set": set_fields})
        if result.matched_count == 0:
            logger.warning(
                "[nl/plan] Process_Configs update matched 0 docs for projectId=%s",
                project_id,
            )
            return False
        logger.info(
            "[nl/plan] Persisted preview to Process_Configs projectId=%s "
            "phaseGraphs=%d nodes≈%s",
            project_id,
            len(phase_graphs_dump),
            sum(
                len(((p.get("graph") or {}).get("nodes") or []))
                for p in phase_graphs_dump
                if isinstance(p, dict)
            ),
        )
        return True
    except Exception as exc:
        logger.warning(
            "[nl/plan] Failed to persist preview in Process_Configs projectId=%s: %s",
            project_id,
            exc,
        )
        return False
