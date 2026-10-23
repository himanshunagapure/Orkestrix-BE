"""
Persist workflow-builder progress for credit-exhaustion resume.

Heavy state (phases, graphs, prior outputs) lives in ``workflow_build_checkpoints``.
``Process_Configs`` carries billing fields, ``agentic_process.status`` (owned by the
external job runner), and ``creditPauseStatus`` (owned exclusively by this module).

Status authority:
  * ``workflow_build_checkpoints.status == "paused_credit"``  — primary; has full
    resume context but may be absent if the checkpoint was not yet written or was
    cleaned up.
  * ``Process_Configs.creditPauseStatus == "paused_credit"``  — durable fallback;
    written by set_workflow_agentic_status and never touched by external job runners
    because it lives outside the ``agentic_process`` sub-document they manage.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from services.workflow_budget_service import (
    PROCESS_CONFIGS_COLLECTION,
    _process_configs_query,
    _to_object_id,
    workflow_billing_from_doc,
)

logger = logging.getLogger(__name__)

CHECKPOINT_COLLECTION = "workflow_build_checkpoints"
RESUME_ENDPOINT = "/aiqod-agent/agent/agentic/workflow/resume"


def ensure_checkpoint_indexes(db) -> None:
    coll = db[CHECKPOINT_COLLECTION]
    coll.create_index("workflow_id", unique=True)
    coll.create_index("subscriber_id")


def write_checkpoint(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: str,
    pipeline_stage: str,
    phase_index: int,
    phase_total: int,
    phase_graphs: Optional[list] = None,
    prior_phase_outputs: Optional[dict] = None,
    workflow_registry: Optional[dict] = None,
    scratch_state: Optional[dict] = None,
    stage0_session: Optional[dict] = None,
    stage0_action: Optional[str] = None,
    stage0_skip_remaining: bool = False,
    stage0_guidance_flag_key: Optional[str] = None,
    stage0_guidance_user_question: Optional[str] = None,
    request_context: Optional[dict] = None,
    last_error: Optional[dict] = None,
    warnings: Optional[list] = None,
    current_node_id: Optional[str] = None,
) -> None:
    now = datetime.utcnow()
    db[CHECKPOINT_COLLECTION].update_one(
        {"workflow_id": workflow_id, "subscriber_id": subscriber_id},
        {
            "$set": {
                "workflow_id": workflow_id,
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "user_id": user_id,
                "status": status,
                "pipeline_stage": pipeline_stage,
                "phase_index": phase_index,
                "phase_total": phase_total,
                "phase_graphs": phase_graphs or [],
                "prior_phase_outputs": prior_phase_outputs or {},
                "workflow_registry": workflow_registry or {},
                "scratch_state": scratch_state,
                "stage0_session": stage0_session,
                "stage0_action": stage0_action,
                "stage0_skip_remaining": stage0_skip_remaining,
                "stage0_guidance_flag_key": stage0_guidance_flag_key,
                "stage0_guidance_user_question": stage0_guidance_user_question,
                "request_context": request_context or {},
                "last_error": last_error,
                "warnings": warnings or [],
                "current_node_id": current_node_id,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    logger.debug(
        "[checkpoint] write_checkpoint workflow_id=%s status=%s pipeline_stage=%s "
        "phase_index=%s/%s phase_graphs=%s current_node_id=%s",
        workflow_id,
        status,
        pipeline_stage,
        phase_index,
        phase_total,
        len(phase_graphs or []),
        current_node_id,
    )


def read_checkpoint(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
) -> Optional[dict]:
    return db[CHECKPOINT_COLLECTION].find_one(
        {"workflow_id": workflow_id, "subscriber_id": subscriber_id}
    )


def delete_checkpoint(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
) -> None:
    db[CHECKPOINT_COLLECTION].delete_one(
        {"workflow_id": workflow_id, "subscriber_id": subscriber_id}
    )
    logger.info(
        "[checkpoint] delete_checkpoint workflow_id=%s subscriber_id=%s",
        workflow_id,
        subscriber_id,
    )


def set_workflow_agentic_status(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str],
    user_id: Optional[str],
    status: str,
) -> None:
    """Update agentic_process.status and creditPauseStatus on Process_Configs.

    ``creditPauseStatus`` is set to ``"paused_credit"`` when *status* is
    ``"paused_credit"``, and cleared (set to ``None``) for every other status
    transition.  This field is never written by external job runners, making it
    a durable fallback indicator for the /resume endpoint.
    """
    now = datetime.utcnow()
    credit_pause_status = "paused_credit" if status == "paused_credit" else None
    fields: Dict[str, Any] = {
        "agentic_process.status": status,
        "creditPauseStatus": credit_pause_status,
        "updated_at": now,
    }
    query = _process_configs_query(
        project_id=workflow_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    result = db[PROCESS_CONFIGS_COLLECTION].update_one(query, {"$set": fields})
    if result.matched_count == 0:
        # Retry with workflow_id + subscriber only (execute headers may omit org/user).
        fallback = {
            "engineType": "agentic_graph",
            "agentic_process.workflow_id": workflow_id,
            "subscriberId": _to_object_id(subscriber_id) or subscriber_id,
        }
        result = db[PROCESS_CONFIGS_COLLECTION].update_one(fallback, {"$set": fields})
    if result.matched_count == 0:
        logger.warning(
            "set_workflow_agentic_status: no Process_Configs for workflow_id=%s",
            workflow_id,
        )


def enrich_billing_error_detail(
    detail: Dict[str, Any],
    *,
    db,
    workflow_id: Optional[str],
    subscriber_id: Optional[str],
) -> Dict[str, Any]:
    """Attach resume metadata for 402 responses when a checkpoint exists."""
    detail["resumeEndpoint"] = RESUME_ENDPOINT
    if not workflow_id:
        return detail

    detail["workflowId"] = workflow_id
    if not subscriber_id:
        return detail

    pc_doc = db[PROCESS_CONFIGS_COLLECTION].find_one(
        _process_configs_query(
            project_id=workflow_id,
            org_id=None,
            subscriber_id=subscriber_id,
            user_id=None,
        ),
        {
            "predictedCost": 1,
            "budgetSpentUsd": 1,
            "overflowSpentUsd": 1,
        },
    )
    detail.update(workflow_billing_from_doc(pc_doc))

    cp = read_checkpoint(db=db, workflow_id=workflow_id, subscriber_id=subscriber_id)
    if not cp:
        return detail

    if cp.get("status") == "paused_credit":
        detail["status"] = "paused_credit"
    detail["pipelineStage"] = cp.get("pipeline_stage")
    detail["phaseIndex"] = cp.get("phase_index")
    detail["phaseTotal"] = cp.get("phase_total")
    if cp.get("last_error"):
        detail["lastError"] = cp["last_error"]
    return detail
