"""
Job-aware workflow build progress for POST /agentic/workflow/progress.

Wraps services.build_progress.build_workflow_progress and adds job fields.
Does not replace checkpoint/billing logic.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from build_services.job import get_latest_job_for_workflow, job_public_fields
from services.build_progress import build_workflow_progress as _base_progress


def build_workflow_progress(
    *,
    db,
    workflow_id: str,
    subscriber_id: str,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    result = _base_progress(
        db=db,
        workflow_id=workflow_id,
        subscriber_id=subscriber_id,
        org_id=org_id,
        user_id=user_id,
    )
    # Always attach job fields so FE can poll jobStatus even before / while
    # Process_Configs exists (async jobs enqueue independently of progress base).
    job = get_latest_job_for_workflow(
        db, workflow_id=workflow_id, subscriber_id=subscriber_id
    )
    result.update(job_public_fields(job))

    # Enrich headline when an async job is in flight but checkpoint text is weak.
    if job and job.get("status") in ("QUEUED", "RUNNING"):
        job_type = (job.get("jobType") or "build").strip()
        stopped = (result.get("stoppedAt") or "").strip()
        if job.get("status") == "QUEUED" and (
            not stopped or stopped.startswith("Registered")
        ):
            result["stoppedAt"] = f"Queued — waiting for worker ({job_type})"
        elif job.get("status") == "RUNNING" and (
            not stopped
            or stopped.startswith("Registered")
            or stopped.startswith("Status:")
        ):
            result["stoppedAt"] = f"In progress — {job_type} job running"

    return result
