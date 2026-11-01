"""
Async preview runner for build jobs (Phase 3).

Lives under langgraph/ — does not import backend/. Invokes the agent
sync POST /plan/scratch/preview over HTTP. Worker holds the long connection;
FE only polls /workflow/progress.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

PREVIEW_PATH = "/aiqod-agent/agent/agentic/plan/scratch/preview"

# Fields accepted by AgenticPlanRequest — do not send extras (e.g. workflowId).
_PREVIEW_REQUEST_KEYS = frozenset(
    {
        "projectId",
        "orgId",
        "subscriberId",
        "userId",
        "prompt",
        "steps",
        "suggestedReports",
        "selectedName",
        "selectedType",
        "selectedCategory",
        "token",
        "minimumCredits",
        "useScratchPlanner",
        "stage0Handoff",
        "mode",
        "allCollections",
        "collectionMetadata",
        "kgSelectedCollections",
    }
)


@dataclass
class PreviewOutcome:
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


def _preview_timeout_sec() -> int:
    return int(
        os.getenv(
            "BUILD_PREVIEW_TIMEOUT_SEC",
            os.getenv("BUILD_EXECUTE_TIMEOUT_SEC", str(60 * 60)),
        )
    )


def compact_preview_result(body: Dict[str, Any], *, workflow_id: str) -> Dict[str, Any]:
    """Small job.result — full phaseGraphs stay in Process_Configs."""
    phase_graphs = body.get("phaseGraphs") or []
    return {
        "workflowId": body.get("workflowId") or workflow_id,
        "workflowName": body.get("workflowName"),
        "status": "preview",
        "phaseCount": len(phase_graphs) if isinstance(phase_graphs, list) else 0,
        "warnings": body.get("warnings") or [],
        "mode": body.get("mode"),
        "analysis_source": body.get("analysis_source"),
        "hasPhaseGraphs": bool(phase_graphs),
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


def _normalize_preview_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map job.request to AgenticPlanRequest shape.
    Scratch preview uses projectId (=== workflowId after register).
    """
    raw = dict(job.get("request") or {})
    workflow_id = (
        raw.get("projectId")
        or raw.get("workflowId")
        or job.get("workflowId")
        or ""
    ).strip()
    if workflow_id:
        raw.setdefault("projectId", workflow_id)
    if job.get("subscriberId") and not raw.get("subscriberId"):
        raw["subscriberId"] = job["subscriberId"]
    if job.get("orgId") and not raw.get("orgId"):
        raw["orgId"] = job["orgId"]
    if job.get("userId") and not raw.get("userId"):
        raw["userId"] = job["userId"]
    return {k: v for k, v in raw.items() if k in _PREVIEW_REQUEST_KEYS}


def run_preview(*, job: Dict[str, Any]) -> PreviewOutcome:
    """
    Call agent sync /scratch/preview with job['request'] payload.

    Success → Process_Configs already status=preview.
    HTTP 402 → BillingError / credit pause path on agent.
    """
    payload = _normalize_preview_payload(job)
    project_id = (payload.get("projectId") or "").strip()
    has_prompt = bool(payload.get("prompt") and str(payload.get("prompt")).strip())
    has_steps = bool(payload.get("steps"))

    if not project_id:
        return PreviewOutcome(
            ok=False,
            error={
                "message": "preview job missing projectId/workflowId in request",
            },
        )
    if has_prompt == has_steps:
        return PreviewOutcome(
            ok=False,
            error={
                "message": (
                    "preview payload must include exactly one of "
                    "'prompt' (non-empty) or 'steps' "
                    "(same body as POST /scratch/preview)"
                ),
            },
        )

    url = f"{_agent_base_url()}{PREVIEW_PATH}"
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_build_headers(payload), method="POST"
    )
    timeout = _preview_timeout_sec()
    print(
        f"[Build] preview POST {url} project_id={project_id} "
        f"timeout_sec={timeout}"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
            body = json.loads(raw) if raw else {}
            return PreviewOutcome(
                ok=True,
                result=compact_preview_result(body, workflow_id=project_id),
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
            return PreviewOutcome(
                ok=False,
                paused_credit=True,
                error=detail if isinstance(detail, dict) else {"message": str(detail)},
                http_status=402,
            )
        return PreviewOutcome(
            ok=False,
            error={
                "message": f"preview HTTP {exc.code}",
                "detail": detail,
            },
            http_status=exc.code,
        )
    except Exception as exc:
        return PreviewOutcome(
            ok=False,
            error={"message": f"preview request failed: {exc}"},
        )
