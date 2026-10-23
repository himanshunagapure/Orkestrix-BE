"""
Compute workflow-builder progress for frontend status UI.

Reads Process_Configs (status + billing) and optional workflow_build_checkpoints
(where the pipeline stopped, partial graphs, scratch sub-stages).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services.build_checkpoint import CHECKPOINT_COLLECTION, RESUME_ENDPOINT
from services.workflow_budget_service import (
    PROCESS_CONFIGS_COLLECTION,
    _process_configs_query,
    workflow_billing_from_doc,
)

_SKIP_NODE_TYPES = frozenset({"router", "fallback_router", "end"})

_SCRATCH_SUB_STAGES = (
    "stage1",
    "stage2a",
    "stage2c",
    "stage3a",
    "stage4",
    "stage4b",
    "stage4c",
)

_SCRATCH_SUB_STAGE_LABELS = {
    "stage1": "Planning workflow skeleton",
    "stage2a": "Building node schemas",
    "stage2c": "Validating node schemas",
    "stage3a": "Mapping edges and routers",
    "stage4": "Assembling graph",
    "stage4b": "Validating graph bindings",
    "stage4c": "Extracting persistence registry",
}

_PIPELINE_STAGE_LABELS = {
    "stage0": "Requirements clarification (Stage 0)",
    "preview": "Graph planning (preview)",
    "execute": "Gap resolution and code generation (execute)",
}


def _is_generatable_node(node: Dict[str, Any]) -> bool:
    node_type = (node.get("type") or "").strip()
    if node_type in _SKIP_NODE_TYPES:
        return False
    cap_id = ((node.get("capabilityRef") or {}).get("capabilityId") or "").strip()
    config = node.get("config") or {}
    if node_type in ("ui_screen", "tool"):
        return True
    if cap_id.startswith("GENERATE:"):
        return True
    if config.get("generation"):
        return True
    return False


def _node_needs_gap_resolution(node: Dict[str, Any]) -> bool:
    cap_id = ((node.get("capabilityRef") or {}).get("capabilityId") or "").strip()
    if cap_id.startswith("GENERATE:"):
        return True
    config = node.get("config") or {}
    if config.get("generation"):
        return True
    return False


def _gap_progress_from_phase_graphs(
    phase_graphs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    resolved_nodes: List[Dict[str, Any]] = []
    remaining_nodes: List[Dict[str, Any]] = []
    total = 0
    resolved = 0

    for phase in phase_graphs or []:
        graph = phase.get("graph") or {}
        phase_id = graph.get("phaseId") or graph.get("graphId") or "unknown_phase"
        phase_name = graph.get("phaseName") or graph.get("name") or phase_id
        for node in graph.get("nodes") or []:
            if not _is_generatable_node(node):
                continue
            total += 1
            cap_id = ((node.get("capabilityRef") or {}).get("capabilityId") or "").strip()
            item = {
                "phaseId": phase_id,
                "phaseName": phase_name,
                "nodeId": node.get("nodeId"),
                "name": node.get("name"),
                "type": node.get("type"),
                "capabilityId": cap_id or None,
            }
            if _node_needs_gap_resolution(node):
                remaining_nodes.append(item)
            else:
                resolved += 1
                resolved_nodes.append(item)

    remaining = len(remaining_nodes)
    percent = (resolved / total * 100.0) if total else 0.0
    return {
        "total": total,
        "resolved": resolved,
        "remaining": remaining,
        "percentComplete": round(percent, 1),
        "resolvedNodes": resolved_nodes,
        "remainingNodes": remaining_nodes,
    }


def _scratch_sub_stage_fraction(sub_stage: Optional[str]) -> float:
    if not sub_stage:
        return 0.0
    try:
        idx = _SCRATCH_SUB_STAGES.index(sub_stage)
    except ValueError:
        return 0.0
    return (idx + 1) / len(_SCRATCH_SUB_STAGES)


def _infer_pipeline_stage(
    *,
    workflow_status: str,
    pipeline_stage: Optional[str],
    phase_graphs: List[Dict[str, Any]],
    scratch_state: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Fill pipeline_stage when the checkpoint is missing or stale.

    Mid-execute progress depends on ``workflow_build_checkpoints``. If that write
    fails (or has not happened yet), Process_Configs still shows ``building`` while
    ``pipeline_stage`` is null — which previously forced overallPercentComplete=0.
    """
    if pipeline_stage:
        return pipeline_stage
    if workflow_status == "stage0_in_progress":
        return "stage0"
    if workflow_status == "building":
        # Preview leaves phaseGraphs on Process_Configs; execute flips status to
        # building while those graphs still contain GENERATE:* nodes.
        if phase_graphs:
            return "execute"
        if scratch_state:
            return "preview"
        return "execute"
    return None


def _major_stage_index(
    *,
    workflow_status: str,
    pipeline_stage: Optional[str],
) -> int:
    """0=registered … 4=complete (used for coarse overall percent)."""
    if workflow_status == "draft":
        return 4
    if pipeline_stage == "execute":
        return 3
    if pipeline_stage == "preview":
        return 2
    if pipeline_stage == "stage0" or workflow_status == "stage0_in_progress":
        return 1
    if workflow_status == "preview":
        return 2
    if workflow_status == "paused_credit":
        if pipeline_stage == "execute":
            return 3
        if pipeline_stage == "preview":
            return 2
        if pipeline_stage == "stage0":
            return 1
    if workflow_status in ("queued", "building"):
        if pipeline_stage == "preview":
            return 2
        if pipeline_stage == "execute":
            return 3
        # building with unknown stage: treat as execute-in-progress, not "0%"
        if workflow_status == "building":
            return 3
        return 0
    return 0


def _within_stage_percent(
    *,
    workflow_status: str,
    pipeline_stage: Optional[str],
    phase_index: int,
    phase_total: int,
    scratch_state: Optional[Dict[str, Any]],
    gap_progress: Dict[str, Any],
    stage0_session: Optional[Dict[str, Any]],
) -> float:
    if workflow_status == "draft":
        return 100.0

    if pipeline_stage == "stage0" or workflow_status == "stage0_in_progress":
        session = stage0_session or {}
        state = session.get("state") or {}
        if state.get("stage0b_complete"):
            return 100.0
        turn_count = int(session.get("turn_count") or 0)
        return min(100.0, max(10.0, turn_count * 10.0))

    if pipeline_stage == "execute" or (
        workflow_status == "building" and (pipeline_stage is None or pipeline_stage == "execute")
    ):
        return float(gap_progress.get("percentComplete") or 0.0)

    if pipeline_stage == "preview":
        phases = max(1, phase_total or 1)
        phase_frac = min(phase_index, phases - 1) / phases
        sub_frac = _scratch_sub_stage_fraction(
            (scratch_state or {}).get("sub_stage")
        ) / phases
        return min(99.0, (phase_frac + sub_frac) * 100.0)

    if workflow_status == "preview":
        return 100.0

    return 0.0


def _stopped_at_label(
    *,
    workflow_status: str,
    pipeline_stage: Optional[str],
    phase_index: int,
    phase_total: int,
    scratch_state: Optional[Dict[str, Any]],
    last_error: Optional[Dict[str, Any]],
    stage0_action: Optional[str],
    stage0_guidance_flag_key: Optional[str] = None,
    current_node_id: Optional[str] = None,
) -> str:
    if workflow_status == "draft":
        return "Workflow build complete"

    if workflow_status == "preview" and not pipeline_stage:
        return "Graph planning complete — ready to execute"

    if pipeline_stage == "stage0":
        action = stage0_action or "respond"
        if action == "guidance":
            flag = stage0_guidance_flag_key
            return f"Paused during Stage 0 setup guidance ({flag or 'guidance'})"
        if action == "start":
            return "Paused during Stage 0 start"
        return "Paused during Stage 0 clarification"

    if pipeline_stage == "preview":
        phase_id = (scratch_state or {}).get("phase_id") or f"phase {phase_index + 1}"
        sub = (scratch_state or {}).get("sub_stage")
        sub_label = _SCRATCH_SUB_STAGE_LABELS.get(sub or "", sub or "planning")
        if workflow_status == "preview":
            return "Graph planning complete — ready to execute"
        if workflow_status == "paused_credit":
            return f"Paused during preview — {phase_id}: {sub_label}"
        return f"In progress — preview — {phase_id}: {sub_label}"

    if pipeline_stage == "execute":
        node_id = current_node_id or (last_error or {}).get("node_id")
        phase_id = (last_error or {}).get("phase_id")
        in_flight = workflow_status in ("building", "queued")
        if in_flight and node_id:
            return f"In progress — generating '{node_id}'"
        if in_flight and phase_id:
            return f"In progress — phase '{phase_id}'"
        if in_flight:
            return "In progress — Gap resolution and code generation (execute)"
        if node_id:
            return f"Paused during execute — generating '{node_id}'"
        if phase_id:
            return f"Paused during execute — phase '{phase_id}'"
        return "Paused during execute (gap resolution / codegen)"

    if workflow_status == "paused_credit":
        return "Paused — credits exhausted"

    if workflow_status == "building":
        stage_label = _PIPELINE_STAGE_LABELS.get(pipeline_stage or "", "Building")
        if current_node_id:
            return f"In progress — generating '{current_node_id}'"
        return f"In progress — {stage_label}"

    if workflow_status == "queued":
        return "Registered — build not started"

    return f"Status: {workflow_status}"


def build_workflow_progress(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a frontend-friendly progress snapshot for one workflow."""
    workflow_id = (workflow_id or "").strip()
    query = _process_configs_query(
        project_id=workflow_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    pc_doc = db[PROCESS_CONFIGS_COLLECTION].find_one(query)
    if not pc_doc:
        return {
            "success": False,
            "found": False,
            "workflowId": workflow_id,
            "error": "Workflow not found in Process_Configs.",
        }

    agentic = pc_doc.get("agentic_process") or {}
    workflow_status = agentic.get("status") or "queued"
    workflow_name = agentic.get("workflowName") or pc_doc.get("selectedName")

    cp = db[CHECKPOINT_COLLECTION].find_one(
        {"workflow_id": workflow_id, "subscriber_id": subscriber_id}
    )
    has_checkpoint = cp is not None
    checkpoint_status = cp.get("status") if cp else None
    pipeline_stage = cp.get("pipeline_stage") if cp else None
    phase_index = int(cp.get("phase_index") or 0) if cp else 0
    phase_total = int(cp.get("phase_total") or 0) if cp else 0
    last_error = cp.get("last_error") if cp else None
    scratch_state = cp.get("scratch_state") if cp else None
    stage0_session = cp.get("stage0_session") if cp else None
    stage0_action = cp.get("stage0_action") if cp else None
    checkpoint_updated_at = cp.get("updated_at") if cp else None
    current_node_id = (cp or {}).get("current_node_id")

    phase_graphs: List[Dict[str, Any]] = []
    if cp and cp.get("phase_graphs"):
        phase_graphs = list(cp.get("phase_graphs") or [])
    elif agentic.get("phaseGraphs"):
        # Fallback only — Process_Configs graphs are NOT updated per gap node.
        # Mid-execute % needs a live checkpoint; otherwise remaining stays at GENERATE:*.
        phase_graphs = list(agentic.get("phaseGraphs") or [])

    if not phase_total:
        phases_info = ((cp or {}).get("request_context") or {}).get("phases_info") or []
        phase_total = len(phase_graphs) or len(phases_info)

    pipeline_stage = _infer_pipeline_stage(
        workflow_status=workflow_status,
        pipeline_stage=pipeline_stage,
        phase_graphs=phase_graphs,
        scratch_state=scratch_state,
    )

    gap_progress = _gap_progress_from_phase_graphs(phase_graphs)
    billing = workflow_billing_from_doc(pc_doc)
    billing["reservationStatus"] = pc_doc.get("reservationStatus")

    is_paused = (
        workflow_status == "paused_credit"
        or checkpoint_status == "paused_credit"
    )
    can_resume = is_paused and has_checkpoint and checkpoint_status == "paused_credit"

    major_idx = _major_stage_index(
        workflow_status=workflow_status,
        pipeline_stage=pipeline_stage,
    )
    within_pct = _within_stage_percent(
        workflow_status=workflow_status,
        pipeline_stage=pipeline_stage,
        phase_index=phase_index,
        phase_total=phase_total,
        scratch_state=scratch_state,
        gap_progress=gap_progress,
        stage0_session=stage0_session,
    )
    # Coarse full-pipeline % (register→draft). Kept as pipelinePercentComplete.
    pipeline_percent = min(
        100.0,
        round(major_idx * 25.0 + within_pct * 0.25, 1),
    )
    # stagePercentComplete: 0–100 within the current pipeline stage only.
    stage_percent = round(float(within_pct), 1)
    if workflow_status == "draft":
        pipeline_percent = 100.0
        stage_percent = 100.0

    # overallPercentComplete: FE progress bars bind here.
    # During execute, use gap resolution 0–100 (not the old 75→100 coarse band).
    # During preview, use scratch sub-stage 0–100. Otherwise keep pipeline %.
    if workflow_status == "draft":
        overall_percent = 100.0
    elif pipeline_stage == "execute" or (
        workflow_status == "building"
        and (pipeline_stage is None or pipeline_stage == "execute")
    ):
        overall_percent = stage_percent
    elif pipeline_stage == "preview" or (
        workflow_status in ("building", "queued") and pipeline_stage == "preview"
    ):
        overall_percent = stage_percent
    elif workflow_status == "preview":
        overall_percent = 100.0
    else:
        overall_percent = pipeline_percent

    current_phase: Optional[Dict[str, Any]] = None
    if phase_graphs and phase_index < len(phase_graphs):
        g = (phase_graphs[phase_index].get("graph") or {})
        current_phase = {
            "index": phase_index,
            "phaseId": g.get("phaseId") or g.get("graphId"),
            "phaseName": g.get("phaseName") or g.get("name"),
        }

    # Phases fully done = all generatable nodes in that phase are resolved
    completed_phases = 0
    if phase_graphs and pipeline_stage == "execute":
        for phase in phase_graphs:
            graph = phase.get("graph") or {}
            nodes = [
                n for n in (graph.get("nodes") or []) if _is_generatable_node(n)
            ]
            if nodes and all(not _node_needs_gap_resolution(n) for n in nodes):
                completed_phases += 1
    elif phase_graphs:
        completed_phases = max(0, min(phase_index, len(phase_graphs)))

    scratch_detail: Optional[Dict[str, Any]] = None
    if pipeline_stage == "preview" and scratch_state:
        sub = scratch_state.get("sub_stage")
        scratch_detail = {
            "phaseId": scratch_state.get("phase_id"),
            "subStage": sub,
            "subStageLabel": _SCRATCH_SUB_STAGE_LABELS.get(sub or "", sub),
            "batchIndex": scratch_state.get("batch_index"),
            "routerId": scratch_state.get("router_id"),
            "subStagePercent": round(_scratch_sub_stage_fraction(sub) * 100.0, 1),
        }

    stage0_detail: Optional[Dict[str, Any]] = None
    if pipeline_stage == "stage0" or workflow_status == "stage0_in_progress":
        sess = stage0_session or {}
        state = sess.get("state") or {}
        stage0_detail = {
            "sessionId": sess.get("session_id"),
            "turnCount": int(sess.get("turn_count") or 0),
            "stage0bComplete": bool(state.get("stage0b_complete")),
            "lastAction": stage0_action,
            "unresolvedFlagCount": len(
                [
                    f for f in (state.get("detected_flags") or [])
                    if not (f or {}).get("resolved")
                ]
            ),
        }

    remaining_summary: Dict[str, Any] = {
        "phasesRemaining": max(0, (phase_total or 0) - completed_phases)
        if pipeline_stage in ("preview", "execute")
        else 0,
        "gapNodesRemaining": gap_progress["remaining"],
        "capRemainingUsd": billing.get("capRemainingUsd", 0.0),
    }
    if workflow_status == "preview" and pipeline_stage != "execute":
        remaining_summary["nextStep"] = "execute"
    elif can_resume:
        remaining_summary["nextStep"] = "resume"
    elif workflow_status == "draft":
        remaining_summary["nextStep"] = None
    elif workflow_status == "queued":
        remaining_summary["nextStep"] = "stage0_or_preview"

    updated_at = pc_doc.get("updated_at")
    return {
        "success": True,
        "found": True,
        "workflowId": workflow_id,
        "workflowName": workflow_name,
        "workflowStatus": workflow_status,
        "isPaused": is_paused,
        "canResume": can_resume,
        "resumeEndpoint": RESUME_ENDPOINT if can_resume else None,
        "stoppedAt": _stopped_at_label(
            workflow_status=workflow_status,
            pipeline_stage=pipeline_stage,
            phase_index=phase_index,
            phase_total=phase_total,
            scratch_state=scratch_state,
            last_error=last_error,
            stage0_action=stage0_action,
            stage0_guidance_flag_key=cp.get("stage0_guidance_flag_key") if cp else None,
            current_node_id=current_node_id,
        ),
        "pipelineStage": pipeline_stage,
        "pipelineStageLabel": _PIPELINE_STAGE_LABELS.get(
            pipeline_stage or "", pipeline_stage
        ),
        "overallPercentComplete": overall_percent,
        "stagePercentComplete": stage_percent,
        "pipelinePercentComplete": pipeline_percent,
        "phaseProgress": {
            "currentIndex": phase_index,
            "totalPhases": phase_total or len(phase_graphs),
            "completedPhases": completed_phases,
            "currentPhase": current_phase,
            "currentNodeId": current_node_id,
        },
        "gapProgress": gap_progress,
        "scratchProgress": scratch_detail,
        "stage0Progress": stage0_detail,
        "billing": billing,
        "remaining": remaining_summary,
        "lastError": last_error,
        "checkpointUpdatedAt": (
            checkpoint_updated_at.isoformat()
            if isinstance(checkpoint_updated_at, datetime)
            else checkpoint_updated_at
        ),
        "updatedAt": (
            updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at
        ),
    }
