"""
Async resume runner for build jobs (Phase 4).

Calls agent sync POST /workflow/resume (unchanged public sync endpoint),
same pattern as preview → /scratch/preview and execute → /plan/execute.
Async enqueue is only via POST /workflow/jobs { jobType: "resume" }.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

RESUME_PATH = "/aiqod-agent/agent/agentic/workflow/resume"


@dataclass
class ResumeOutcome:
    ok: bool
    paused_credit: bool = False
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    http_status: Optional[int] = None


def _agent_base_url() -> str:
    return (
        os.getenv("BUILD_AGENT_BASE_URL")
        or os.getenv("AGENT_BASE_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")


def _resume_timeout_sec() -> int:
    return int(
        os.getenv(
            "BUILD_RESUME_TIMEOUT_SEC",
            os.getenv("BUILD_EXECUTE_TIMEOUT_SEC", str(3 * 60 * 60)),
        )
    )


def compact_resume_result(
    body: Dict[str, Any],
    *,
    workflow_id: str,
    pipeline_stage: Optional[str],
) -> Dict[str, Any]:
    phase_graphs = body.get("phaseGraphs") or []
    stage = pipeline_stage or "execute"
    status = "preview" if stage in ("preview", "stage0") else "draft"
    return {
        "workflowId": body.get("workflowId") or workflow_id,
        "workflowName": body.get("workflowName"),
        "status": status,
        "pipelineStage": stage,
        "phaseCount": len(phase_graphs) if isinstance(phase_graphs, list) else 0,
        "warnings": body.get("warnings") or [],
        "hasPhaseGraphs": bool(phase_graphs),
        "hasCombinedLanggraphCode": bool(body.get("combinedLanggraphCode")),
    }


def _build_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for header, key in (
        ("x-subscriber-id", "subscriberId"),
        ("x-org-id", "orgId"),
        ("x-user-id", "userId"),
    ):
        val = payload.get(key)
        if val:
            headers[header] = str(val)
    token = payload.get("token")
    if token:
        headers["Authorization"] = (
            token if str(token).lower().startswith("bearer ") else f"Bearer {token}"
        )
    return headers


def run_resume(*, job: Dict[str, Any]) -> ResumeOutcome:
    """
    Call agent sync /workflow/resume with job request ids.
    """
    raw = dict(job.get("request") or {})
    workflow_id = (
        raw.get("workflowId") or job.get("workflowId") or ""
    ).strip()
    subscriber_id = (
        raw.get("subscriberId") or job.get("subscriberId") or ""
    ).strip()
    if not workflow_id or not subscriber_id:
        return ResumeOutcome(
            ok=False,
            error={"message": "resume job missing workflowId or subscriberId"},
        )

    payload = {
        "workflowId": workflow_id,
        "subscriberId": subscriber_id,
    }
    for key in ("orgId", "userId", "token"):
        val = raw.get(key) or job.get(key)
        if val:
            payload[key] = val

    url = f"{_agent_base_url()}{RESUME_PATH}"
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_build_headers(payload), method="POST"
    )
    timeout = _resume_timeout_sec()
    print(
        f"[Build] resume POST {url} workflow_id={workflow_id} "
        f"timeout_sec={timeout}"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
            body = json.loads(raw_body) if raw_body else {}
            return ResumeOutcome(
                ok=True,
                result=compact_resume_result(
                    body,
                    workflow_id=workflow_id,
                    pipeline_stage=job.get("pipelineStage"),
                ),
                http_status=status,
            )
    except urllib.error.HTTPError as exc:
        err_body: Any
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"message": str(exc)}
        detail = err_body.get("detail", err_body) if isinstance(err_body, dict) else err_body
        if exc.code == 402:
            return ResumeOutcome(
                ok=False,
                paused_credit=True,
                error=detail if isinstance(detail, dict) else {"message": str(detail)},
                http_status=402,
            )
        return ResumeOutcome(
            ok=False,
            error={
                "message": f"resume HTTP {exc.code}",
                "detail": detail,
            },
            http_status=exc.code,
        )
    except Exception as exc:
        return ResumeOutcome(
            ok=False,
            error={"message": f"resume request failed: {exc}"},
        )
