"""
Worker-side helpers for Mongo collection `workflow_build_jobs`.

Lives entirely under langgraph/ — the exec deploy (PM2 104) does not include
backend/. Agent API enqueue/progress stays in backend/build_services/.

Keep field names / statuses in sync with backend/build_services/job.py.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

JOB_COLLECTION = "workflow_build_jobs"

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_PAUSED_CREDIT = "PAUSED_CREDIT"

BUILD_STREAM_NAME = os.getenv("BUILD_STREAM_NAME", "build_tasks")
BUILD_GROUP_NAME = os.getenv("BUILD_GROUP_NAME", "build_workers")


def _utcnow() -> datetime:
    return datetime.utcnow()


def get_job_by_id(db, job_id: str) -> Optional[Dict[str, Any]]:
    return db[JOB_COLLECTION].find_one({"jobId": job_id})


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
    db[JOB_COLLECTION].update_one(
        {"jobId": job_id},
        {
            "$set": {
                "status": STATUS_COMPLETED,
                "result": result,
                "error": None,
                "finishedAt": now,
                "updatedAt": now,
            }
        },
    )


def pause_job_credit(db, *, job_id: str, error: Any = None) -> None:
    """Mark job PAUSED_CREDIT after agent BillingError (HTTP 402)."""
    now = _utcnow()
    err_obj = None
    if error is not None:
        err_obj = error if isinstance(error, dict) else {"message": str(error)}
    db[JOB_COLLECTION].update_one(
        {"jobId": job_id},
        {
            "$set": {
                "status": STATUS_PAUSED_CREDIT,
                "error": err_obj,
                "finishedAt": now,
                "updatedAt": now,
            }
        },
    )


def fail_job(db, *, job_id: str, error: Any) -> None:
    now = _utcnow()
    err_obj = error if isinstance(error, dict) else {"message": str(error)}
    db[JOB_COLLECTION].update_one(
        {"jobId": job_id},
        {
            "$set": {
                "status": STATUS_FAILED,
                "error": err_obj,
                "finishedAt": now,
                "updatedAt": now,
            }
        },
    )


def requeue_running_job(
    db,
    *,
    job_id: str,
    max_attempts: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Stale RUNNING → QUEUED (bump attempt) or FAILED if attempts exceeded.
    Returns updated doc if requeued, None if failed/not claimable.
    """
    from pymongo import ReturnDocument

    now = _utcnow()
    job = get_job_by_id(db, job_id)
    if not job or job.get("status") != STATUS_RUNNING:
        return None
    attempt = int(job.get("attempt") or 1)
    if attempt >= max_attempts:
        fail_job(
            db,
            job_id=job_id,
            error={
                "message": (
                    f"Build job exceeded max attempts ({max_attempts}) "
                    "while stuck RUNNING"
                ),
                "code": "MAX_ATTEMPTS",
            },
        )
        return None
    return db[JOB_COLLECTION].find_one_and_update(
        {"jobId": job_id, "status": STATUS_RUNNING},
        {
            "$set": {
                "status": STATUS_QUEUED,
                "workerId": None,
                "startedAt": None,
                "updatedAt": now,
            },
            "$inc": {"attempt": 1},
        },
        return_document=ReturnDocument.AFTER,
    )


def xadd_build_message(
    valkey_client,
    *,
    action: str,
    job_id: str,
    workflow_id: str,
    subscriber_id: str,
) -> str:
    stream = os.getenv("BUILD_STREAM_NAME", BUILD_STREAM_NAME)
    msg_id = valkey_client.xadd(
        stream,
        {
            "action": action,
            "job_id": job_id,
            "workflow_id": workflow_id,
            "subscriber_id": subscriber_id,
        },
    )
    return str(msg_id)
