"""
Async execute runner for build jobs (Phase 2).
bild
Lives under langgraph/ — does not import backend/. Invokes the agent
sync POST /plan/execute over HTTP so gap/codegen/billing stay on PM2 76.
The worker holds the long connection; FE only polls /workflow/progress.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

EXECUTE_PATH = "/aiqod-agent/agent/agentic/plan/execute"
# Long builds (20–100+ min). Worker↔agent only — not the browser.
DEFAULT_TIMEOUT_SEC = int(os.getenv("BUILD_EXECUTE_TIMEOUT_SEC", str(3 * 60 * 60)))


@dataclass
class ExecuteOutcome:
    """Normalized result for process_build_task."""

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


def compact_execute_result(body: Dict[str, Any], *, workflow_id: str) -> Dict[str, Any]:
    """Small job.result — full graphs stay in Process_Configs."""
    phase_graphs = body.get("phaseGraphs") or []
    reports = body.get("reports") or []
    return {
        "workflowId": workflow_id,
        "workflowName": body.get("workflowName"),
        "status": "draft",
        "phaseCount": len(phase_graphs) if isinstance(phase_graphs, list) else 0,
        "warnings": body.get("warnings") or [],
        "dashboardScreenId": body.get("dashboardScreenId"),
        "dashboardUrl": body.get("dashboardUrl"),
        "reportCount": len(reports) if isinstance(reports, list) else 0,
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


def run_execute(*, job: Dict[str, Any]) -> ExecuteOutcome:
    """
    Call agent sync /plan/execute with job['request'] payload.

    Success → Process_Configs already draft + checkpoint cleared by agent.
    HTTP 402 → BillingError path already wrote paused_credit checkpoint.
    """
    payload = dict(job.get("request") or {})
    workflow_id = (
        payload.get("workflowId")
        or job.get("workflowId")
        or ""
    ).strip()
    if workflow_id and not payload.get("workflowId"):
        payload["workflowId"] = workflow_id
    if job.get("subscriberId") and not payload.get("subscriberId"):
        payload["subscriberId"] = job["subscriberId"]
    if job.get("orgId") and not payload.get("orgId"):
        payload["orgId"] = job["orgId"]
    if job.get("userId") and not payload.get("userId"):
        payload["userId"] = job["userId"]

    if not payload.get("workflowId"):
        return ExecuteOutcome(
            ok=False,
            error={"message": "execute job missing workflowId in request"},
        )
    if not payload.get("phaseGraphs"):
        return ExecuteOutcome(
            ok=False,
            error={
                "message": "execute job request.phaseGraphs is required "
                "(same body as POST /plan/execute)"
            },
        )

    url = f"{_agent_base_url()}{EXECUTE_PATH}"
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_build_headers(payload), method="POST"
    )
    timeout = DEFAULT_TIMEOUT_SEC
    print(
        f"[Build] execute POST {url} workflow_id={workflow_id} "
        f"timeout_sec={timeout}"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
            body = json.loads(raw) if raw else {}
            return ExecuteOutcome(
                ok=True,
                result=compact_execute_result(body, workflow_id=workflow_id),
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
            return ExecuteOutcome(
                ok=False,
                paused_credit=True,
                error=detail if isinstance(detail, dict) else {"message": str(detail)},
                http_status=402,
            )
        return ExecuteOutcome(
            ok=False,
            error={
                "message": f"execute HTTP {exc.code}",
                "detail": detail,
            },
            http_status=exc.code,
        )
    except Exception as exc:
        return ExecuteOutcome(
            ok=False,
            error={"message": f"execute request failed: {exc}"},
        )
