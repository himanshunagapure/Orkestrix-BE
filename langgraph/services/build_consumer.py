"""
Build-stream consumer (preview / execute / resume jobs).

Separate from runtime agentic_tasks handling in worker.py.
Uses BUILD_STREAM_NAME only — never VALKEY_STREAM_NAME.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from services.build_job import (
    BUILD_GROUP_NAME as _DEFAULT_BUILD_GROUP,
    BUILD_STREAM_NAME as _DEFAULT_BUILD_STREAM,
    JOB_COLLECTION,
    claim_job,
    complete_job,
    fail_job,
    get_job_by_id,
    pause_job_credit,
    requeue_running_job,
    xadd_build_message,
)

BUILD_STREAM_NAME = os.getenv("BUILD_STREAM_NAME", _DEFAULT_BUILD_STREAM)
BUILD_GROUP_NAME = os.getenv("BUILD_GROUP_NAME", _DEFAULT_BUILD_GROUP)
MAX_BUILD_WORKERS = max(1, int(os.getenv("MAX_BUILD_WORKERS", "1")))
# Build executes can run 20–100+ min. Do NOT reuse the runtime 10m claim timeout
# or pending messages get "recovered" while execute is still in flight.
_EXECUTE_TIMEOUT_SEC = int(os.getenv("BUILD_EXECUTE_TIMEOUT_SEC", str(3 * 60 * 60)))
BUILD_CLAIM_TIMEOUT_MS = int(
    os.getenv(
        "BUILD_CLAIM_TIMEOUT_MS",
        str(max(_EXECUTE_TIMEOUT_SEC * 1000 + 600_000, 4 * 60 * 60 * 1000)),
    )
)
# RUNNING jobs older than this are treated as crashed workers
BUILD_STALE_RUNNING_MS = int(
    os.getenv("BUILD_STALE_RUNNING_MS", str(BUILD_CLAIM_TIMEOUT_MS))
)
MAX_BUILD_ATTEMPTS = max(1, int(os.getenv("MAX_BUILD_ATTEMPTS", "3")))


def setup_build_valkey_group(valkey_client) -> None:
    try:
        valkey_client.xgroup_create(
            BUILD_STREAM_NAME, BUILD_GROUP_NAME, id="0", mkstream=True
        )
        print(
            f"[Build] Created Valkey Consumer Group: {BUILD_GROUP_NAME} "
            f"on {BUILD_STREAM_NAME}"
        )
    except Exception as e:
        # valkey.exceptions.ResponseError — keep soft import of exception type
        if "BUSYGROUP" not in str(e):
            raise


def create_build_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=MAX_BUILD_WORKERS)


def _job_running_is_stale(job: dict) -> bool:
    from datetime import datetime

    started = job.get("startedAt") or job.get("updatedAt")
    if started is None:
        return True
    if not isinstance(started, datetime):
        return False
    age_ms = (datetime.utcnow() - started).total_seconds() * 1000
    return age_ms >= BUILD_STALE_RUNNING_MS


def _run_claimed_job(mongo_db, job: dict, action: str) -> None:
    """
    Dispatch by action after claim.
    Phase 2–4: preview, execute, and resume are real.
    """
    job_id = job["jobId"]
    action = (action or job.get("jobType") or "").strip().lower()

    if action == "execute":
        from services.build_execute import run_execute

        outcome = run_execute(job=job)
        if outcome.ok:
            complete_job(mongo_db, job_id=job_id, result=outcome.result)
            print(f"[Build] job_id={job_id} execute COMPLETED")
            return
        if outcome.paused_credit:
            pause_job_credit(mongo_db, job_id=job_id, error=outcome.error)
            print(f"[Build] job_id={job_id} execute PAUSED_CREDIT")
            return
        fail_job(
            mongo_db,
            job_id=job_id,
            error=outcome.error or {"message": "execute failed"},
        )
        print(f"[Build] job_id={job_id} execute FAILED")
        return

    if action == "preview":
        from services.build_preview import run_preview

        outcome = run_preview(job=job)
        if outcome.ok:
            complete_job(mongo_db, job_id=job_id, result=outcome.result)
            print(f"[Build] job_id={job_id} preview COMPLETED")
            return
        if outcome.paused_credit:
            pause_job_credit(mongo_db, job_id=job_id, error=outcome.error)
            print(f"[Build] job_id={job_id} preview PAUSED_CREDIT")
            return
        fail_job(
            mongo_db,
            job_id=job_id,
            error=outcome.error or {"message": "preview failed"},
        )
        print(f"[Build] job_id={job_id} preview FAILED")
        return

    if action == "resume":
        from services.build_resume import run_resume

        outcome = run_resume(job=job)
        if outcome.ok:
            complete_job(mongo_db, job_id=job_id, result=outcome.result)
            print(f"[Build] job_id={job_id} resume COMPLETED")
            return
        if outcome.paused_credit:
            pause_job_credit(mongo_db, job_id=job_id, error=outcome.error)
            print(f"[Build] job_id={job_id} resume PAUSED_CREDIT")
            return
        fail_job(
            mongo_db,
            job_id=job_id,
            error=outcome.error or {"message": "resume failed"},
        )
        print(f"[Build] job_id={job_id} resume FAILED")
        return

    fail_job(
        mongo_db,
        job_id=job_id,
        error={"message": f"Unknown build action: {action!r}"},
    )


def process_build_task(
    valkey_client,
    mongo_db,
    consumer_name: str,
    message_id,
    message_data,
) -> None:
    """
    Claim QUEUED job → run action → COMPLETED / PAUSED_CREDIT / FAILED → xack.
    """
    print(
        f"[Build] Processing {message_id} action={message_data.get('action')} "
        f"on thread: {threading.current_thread().name}"
    )
    job_id = message_data.get("job_id")
    try:
        if not job_id:
            raise ValueError("Missing job_id in build stream message")

        existing = get_job_by_id(mongo_db, job_id)
        if not existing:
            print(f"[Build] job_id={job_id} not found — acking")
            valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
            return

        status = existing.get("status")
        if status == "RUNNING":
            if _job_running_is_stale(existing):
                requeued = requeue_running_job(
                    mongo_db, job_id=job_id, max_attempts=MAX_BUILD_ATTEMPTS
                )
                if requeued:
                    try:
                        xadd_build_message(
                            valkey_client,
                            action=requeued.get("jobType") or "execute",
                            job_id=job_id,
                            workflow_id=requeued.get("workflowId") or "",
                            subscriber_id=requeued.get("subscriberId") or "",
                        )
                        print(
                            f"[Build] job_id={job_id} stale RUNNING → requeued "
                            f"attempt={requeued.get('attempt')}"
                        )
                    except Exception as exc:
                        fail_job(
                            mongo_db,
                            job_id=job_id,
                            error={"message": f"requeue xadd failed: {exc}"},
                        )
                        print(f"[Build] job_id={job_id} requeue xadd failed: {exc}")
                else:
                    print(f"[Build] job_id={job_id} stale RUNNING → FAILED (max attempts)")
            else:
                # Another worker still owns an in-flight job — do not re-run.
                print(
                    f"[Build] job_id={job_id} still RUNNING (in flight) — ack only"
                )
            valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
            return

        if status != "QUEUED":
            print(
                f"[Build] job_id={job_id} status={status} — skip/ack"
            )
            valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
            return

        claimed = claim_job(mongo_db, job_id=job_id, worker_id=consumer_name)
        if not claimed:
            print(f"[Build] job_id={job_id} could not claim — acking")
            valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
            return

        _run_claimed_job(
            mongo_db,
            claimed,
            action=message_data.get("action") or claimed.get("jobType"),
        )
        valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
        print(f"[Build] job_id={job_id} acknowledged.")

    except Exception as e:
        print(f"[Build] Error processing message {message_id}: {e}")
        if job_id:
            try:
                fail_job(mongo_db, job_id=job_id, error={"message": str(e)})
            except Exception:
                pass
        try:
            valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
            print(f"[Build] Poison pill {message_id} discarded.")
        except Exception:
            pass


def poll_build_once(
    valkey_client,
    mongo_db,
    build_pool: ThreadPoolExecutor,
    consumer_name: str,
    claim_timeout_ms: int | None = None,
) -> bool:
    """
    One claim/read cycle on the build stream.
    Returns True if any work was submitted (caller may continue the outer loop).
    """
    timeout_ms = (
        BUILD_CLAIM_TIMEOUT_MS if claim_timeout_ms is None else claim_timeout_ms
    )
    pending_messages = valkey_client.xpending_range(
        name=BUILD_STREAM_NAME,
        groupname=BUILD_GROUP_NAME,
        min="-",
        max="+",
        count=10,
    )
    message_ids_to_claim = [
        msg["message_id"]
        for msg in pending_messages
        if msg["time_since_delivered"] >= timeout_ms
    ]
    if message_ids_to_claim:
        claimed_messages = valkey_client.xclaim(
            name=BUILD_STREAM_NAME,
            groupname=BUILD_GROUP_NAME,
            consumername=consumer_name,
            min_idle_time=timeout_ms,
            message_ids=message_ids_to_claim,
        )
        if claimed_messages:
            print(
                f"[Build] Recovered {len(claimed_messages)} abandoned "
                f"tasks on {BUILD_STREAM_NAME}."
            )
            for message_id, message_data in claimed_messages:
                build_pool.submit(
                    process_build_task,
                    valkey_client,
                    mongo_db,
                    consumer_name,
                    message_id,
                    message_data,
                )
            return True

    build_messages = valkey_client.xreadgroup(
        groupname=BUILD_GROUP_NAME,
        consumername=consumer_name,
        streams={BUILD_STREAM_NAME: ">"},
        count=1,
        # block=0 means "block forever" in the Redis protocol — omit to get
        # a truly non-blocking call; runtime loop already blocks on its stream.
    )
    if not build_messages:
        return False

    for _stream, message_list in build_messages:
        for message_id, message_data in message_list:
            build_pool.submit(
                process_build_task,
                valkey_client,
                mongo_db,
                consumer_name,
                message_id,
                message_data,
            )
    return True


def log_build_ready() -> None:
    base = (
        os.getenv("BUILD_AGENT_BASE_URL")
        or os.getenv("AGENT_BASE_URL")
        or "http://127.0.0.1:8000"
    )
    print(
        f"[Build] Listening on stream={BUILD_STREAM_NAME} "
        f"group={BUILD_GROUP_NAME} max_workers={MAX_BUILD_WORKERS} "
        f"agent={base} claim_timeout_ms={BUILD_CLAIM_TIMEOUT_MS}"
    )


def recover_stale_build_jobs(mongo_db, valkey_client) -> int:
    """
    On worker startup: re-queue stale RUNNING build jobs (Phase 4 / §6.5).
    Returns number of jobs requeued or failed.
    """
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(milliseconds=BUILD_STALE_RUNNING_MS)
    cursor = mongo_db[JOB_COLLECTION].find(
        {
            "status": "RUNNING",
            "$or": [
                {"updatedAt": {"$lt": cutoff}},
                {"startedAt": {"$lt": cutoff}},
            ],
        }
    )
    handled = 0
    for job in cursor:
        job_id = job.get("jobId")
        if not job_id:
            continue
        requeued = requeue_running_job(
            mongo_db, job_id=job_id, max_attempts=MAX_BUILD_ATTEMPTS
        )
        handled += 1
        if not requeued:
            print(f"[Build][Recovery] job_id={job_id} FAILED (max attempts)")
            continue
        try:
            xadd_build_message(
                valkey_client,
                action=requeued.get("jobType") or "execute",
                job_id=job_id,
                workflow_id=requeued.get("workflowId") or "",
                subscriber_id=requeued.get("subscriberId") or "",
            )
            print(
                f"[Build][Recovery] job_id={job_id} requeued "
                f"attempt={requeued.get('attempt')}"
            )
        except Exception as exc:
            fail_job(
                mongo_db,
                job_id=job_id,
                error={"message": f"startup requeue xadd failed: {exc}"},
            )
            print(f"[Build][Recovery] job_id={job_id} xadd failed: {exc}")
    if handled:
        print(f"[Build][Recovery] processed {handled} stale RUNNING job(s)")
    return handled
