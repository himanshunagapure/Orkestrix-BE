"""
Durable workflow build jobs (Mongo) + Valkey enqueue (agent API side).

Does not touch the runtime LangGraph stream (VALKEY_STREAM_NAME).

Worker claim/complete/fail lives in langgraph/services/build_job.py — keep
collection/status field names in sync. Do not import this module from worker.py.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JOB_COLLECTION = "workflow_build_jobs"

JOB_TYPE_PREVIEW = "preview"
JOB_TYPE_EXECUTE = "execute"
JOB_TYPE_RESUME = "resume"
VALID_JOB_TYPES = frozenset({JOB_TYPE_PREVIEW, JOB_TYPE_EXECUTE, JOB_TYPE_RESUME})

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_PAUSED_CREDIT = "PAUSED_CREDIT"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

# New build queue only — never reuse VALKEY_STREAM_NAME / agentic_tasks*
BUILD_STREAM_NAME_DEFAULT = "build_tasks"
BUILD_GROUP_NAME_DEFAULT = "build_workers"
MAX_QUEUE_LENGTH = int(os.getenv("MAX_QUEUE_LENGTH", "10000"))


def _build_stream_name() -> str:
    return os.getenv("BUILD_STREAM_NAME", BUILD_STREAM_NAME_DEFAULT)


def _build_group_name() -> str:
    return os.getenv("BUILD_GROUP_NAME", BUILD_GROUP_NAME_DEFAULT)


# Back-compat aliases (resolved at import; prefer helpers for live env)
BUILD_STREAM_NAME = _build_stream_name()
BUILD_GROUP_NAME = _build_group_name()


def ensure_job_indexes(db) -> None:
    coll = db[JOB_COLLECTION]
    coll.create_index(
        [("workflowId", 1), ("subscriberId", 1), ("jobType", 1), ("status", 1)],
        name="workflow_sub_type_status",
    )
    coll.create_index([("status", 1), ("createdAt", 1)], name="status_created")
    coll.create_index("jobId", unique=True, name="jobId_unique")


def _utcnow() -> datetime:
    return datetime.utcnow()


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def find_active_job(
    db,
    *,
    workflow_id: str,
    subscriber_id: str,
    job_type: str,
) -> Optional[Dict[str, Any]]:
    return db[JOB_COLLECTION].find_one(
        {
            "workflowId": workflow_id,
            "subscriberId": subscriber_id,
            "jobType": job_type,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        sort=[("createdAt", -1)],
    )


def get_job_by_id(db, job_id: str) -> Optional[Dict[str, Any]]:
    return db[JOB_COLLECTION].find_one({"jobId": job_id})


def get_latest_job_for_workflow(
    db,
    *,
    workflow_id: str,
    subscriber_id: str,
) -> Optional[Dict[str, Any]]:
    """Prefer an active job; otherwise the most recently updated job."""
    active = db[JOB_COLLECTION].find_one(
        {
            "workflowId": workflow_id,
            "subscriberId": subscriber_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        sort=[("updatedAt", -1)],
    )
    if active:
        return active
    return db[JOB_COLLECTION].find_one(
        {"workflowId": workflow_id, "subscriberId": subscriber_id},
        sort=[("updatedAt", -1)],
    )


def create_job_document(
    *,
    job_id: str,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str],
    user_id: Optional[str],
    job_type: str,
    pipeline_stage: str,
    request_payload: Dict[str, Any],
) -> Dict[str, Any]:
    now = _utcnow()
    return {
        "jobId": job_id,
        "workflowId": workflow_id,
        "subscriberId": subscriber_id,
        "orgId": org_id,
        "userId": user_id,
        "jobType": job_type,
        "status": STATUS_QUEUED,
        "pipelineStage": pipeline_stage,
        "request": request_payload or {},
        "result": None,
        "error": None,
        "attempt": 1,
        "workerId": None,
        "valkeyMessageId": None,
        "createdAt": now,
        "startedAt": None,
        "finishedAt": None,
        "updatedAt": now,
    }


def insert_job(db, doc: Dict[str, Any]) -> Dict[str, Any]:
    db[JOB_COLLECTION].insert_one(doc)
    return doc


def update_job_fields(db, job_id: str, fields: Dict[str, Any]) -> None:
    fields = {**fields, "updatedAt": _utcnow()}
    db[JOB_COLLECTION].update_one({"jobId": job_id}, {"$set": fields})


def claim_job(
    db,
    *,
    job_id: str,
    worker_id: str,
) -> Optional[Dict[str, Any]]:
    """Atomically QUEUED → RUNNING. Returns updated doc or None if not claimable."""
    from pymongo import ReturnDocument

    now = _utcnow()
    return db[JOB_COLLECTION].find_one_and_update(
        {"jobId": job_id, "status": STATUS_QUEUED},
        {
            "$set": {
                "status": STATUS_RUNNING,
                "workerId": worker_id,
                "startedAt": now,
                "updatedAt": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def complete_job(
    db,
    *,
    job_id: str,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    now = _utcnow()
    update_job_fields(
        db,
        job_id,
        {
            "status": STATUS_COMPLETED,
            "result": result,
            "error": None,
            "finishedAt": now,
        },
    )


def fail_job(db, *, job_id: str, error: Any) -> None:
    now = _utcnow()
    err_obj = error if isinstance(error, dict) else {"message": str(error)}
    update_job_fields(
        db,
        job_id,
        {
            "status": STATUS_FAILED,
            "error": err_obj,
            "finishedAt": now,
        },
    )


def pause_job_credit(db, *, job_id: str, error: Any = None) -> None:
    """Mark job PAUSED_CREDIT (agent may call this; worker uses langgraph copy)."""
    now = _utcnow()
    fields: Dict[str, Any] = {
        "status": STATUS_PAUSED_CREDIT,
        "finishedAt": now,
    }
    if error is not None:
        fields["error"] = error if isinstance(error, dict) else {"message": str(error)}
    update_job_fields(db, job_id, fields)


def set_job_valkey_message_id(db, job_id: str, message_id: str) -> None:
    update_job_fields(db, job_id, {"valkeyMessageId": message_id})


def _get_valkey_client():
    """Lazy Valkey/Redis client for build stream only. Reads env at call time."""
    host = os.getenv("VALKEY_HOST", "localhost")
    port = int(os.getenv("VALKEY_PORT", "6380"))
    try:
        import valkey

        return valkey.Valkey(host=host, port=port, decode_responses=True)
    except ImportError:
        pass
    try:
        import redis

        return redis.Redis(host=host, port=port, decode_responses=True)
    except ImportError as exc:
        raise RuntimeError(
            "Neither valkey nor redis package is installed; cannot enqueue build jobs."
        ) from exc


def enqueue_build_message(
    *,
    action: str,
    job_id: str,
    workflow_id: str,
    subscriber_id: str,
) -> str:
    """
    XADD onto BUILD_STREAM_NAME. Does not touch VALKEY_STREAM_NAME.
    Returns Valkey message id.
    """
    client = _get_valkey_client()
    stream = _build_stream_name()
    payload = {
        "action": action,
        "job_id": job_id,
        "workflow_id": workflow_id,
        "subscriber_id": subscriber_id,
    }
    message_id = client.xadd(stream, payload, maxlen=MAX_QUEUE_LENGTH)
    logger.info(
        "[build_job] enqueued stream=%s action=%s job_id=%s msg_id=%s",
        stream,
        action,
        job_id,
        message_id,
    )
    return str(message_id)


def create_or_get_and_enqueue(
    db,
    *,
    job_type: str,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    pipeline_stage: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent enqueue: return existing QUEUED/RUNNING job, else create + xadd.

    Returns dict suitable for API response:
      { jobId, workflowId, jobType, status, reused: bool }
    """
    if job_type not in VALID_JOB_TYPES:
        raise ValueError(f"Invalid jobType {job_type!r}")

    workflow_id = (workflow_id or "").strip()
    subscriber_id = (subscriber_id or "").strip()
    if not workflow_id or not subscriber_id:
        raise ValueError("workflowId and subscriberId are required")

    existing = find_active_job(
        db,
        workflow_id=workflow_id,
        subscriber_id=subscriber_id,
        job_type=job_type,
    )
    if existing:
        logger.info(
            "[build_job] reuse active job_id=%s status=%s workflow_id=%s job_type=%s",
            existing.get("jobId"),
            existing.get("status"),
            workflow_id,
            job_type,
        )
        return {
            "jobId": existing["jobId"],
            "workflowId": workflow_id,
            "jobType": job_type,
            "status": existing["status"],
            "reused": True,
            "progressEndpoint": "/aiqod-agent/agent/agentic/workflow/progress",
        }

    if pipeline_stage:
        stage = pipeline_stage
    elif job_type == JOB_TYPE_PREVIEW:
        stage = "preview"
    elif job_type == JOB_TYPE_RESUME:
        stage = "execute"  # caller should pass checkpoint pipeline_stage when known
    else:
        stage = "execute"

    job_id = _new_job_id()
    doc = create_job_document(
        job_id=job_id,
        workflow_id=workflow_id,
        subscriber_id=subscriber_id,
        org_id=org_id,
        user_id=user_id,
        job_type=job_type,
        pipeline_stage=stage,
        request_payload=payload or {},
    )
    insert_job(db, doc)

    try:
        msg_id = enqueue_build_message(
            action=job_type,
            job_id=job_id,
            workflow_id=workflow_id,
            subscriber_id=subscriber_id,
        )
        set_job_valkey_message_id(db, job_id, msg_id)
    except Exception:
        # Mark failed so FE does not wait forever on a QUEUED job with no message
        fail_job(
            db,
            job_id=job_id,
            error={"message": "Failed to enqueue build job to Valkey"},
        )
        logger.exception(
            "[build_job] Valkey enqueue failed job_id=%s stream=%s",
            job_id,
            _build_stream_name(),
        )
        raise

    return {
        "jobId": job_id,
        "workflowId": workflow_id,
        "jobType": job_type,
        "status": STATUS_QUEUED,
        "reused": False,
        "progressEndpoint": "/aiqod-agent/agent/agentic/workflow/progress",
    }


def job_public_fields(job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fields merged into /workflow/progress."""
    if not job:
        return {
            "jobId": None,
            "jobType": None,
            "jobStatus": None,
            "resultReady": False,
            "jobError": None,
        }
    status = job.get("status")
    return {
        "jobId": job.get("jobId"),
        "jobType": job.get("jobType"),
        "jobStatus": status,
        "resultReady": status == STATUS_COMPLETED,
        "jobError": job.get("error"),
    }
