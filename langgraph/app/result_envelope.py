"""Classify tool return values as success vs failure (Issue 5).

Tools historically return plain dicts. After Issue 7, a failure may still include
every declared output key as an empty default plus ``error``, so missing fields
are not a valid failure signal.

Detection order:
  1. explicit boolean ``ok``
  2. non-empty ``error`` (string or ``{message|error}`` dict)
  3. ``status`` in error/failed/failure when there is no substantive payload
  4. otherwise success

On success, callers store the original dict (or ``data`` when the tool used an
explicit envelope) so existing ``node_outputs[nodeId][field]`` bindings keep working.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_META_KEYS = frozenset(
    {"ok", "error", "code", "retryable", "data", "status", "message", "traceback"}
)
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})
_ABORT_ACTIONS = frozenset({"abort", "cancel", "fail"})


class ToolNodeFailed(RuntimeError):
    """Raised when the operator aborts a paused tool error instead of retrying."""

    def __init__(self, envelope: Dict[str, Any], tool_name: str = ""):
        self.envelope = envelope
        self.tool_name = tool_name
        super().__init__(envelope.get("error") or f"Tool {tool_name or 'call'} failed")


def infer_error_code(message: str) -> str:
    low = (message or "").lower()
    if any(tok in low for tok in ("credential", "api key", "apikey", "api_key")):
        return "MISSING_CREDENTIAL"
    if any(tok in low for tok in ("not configured", "missing config", "configuration needed")):
        return "CONFIGURATION_NEEDED"
    if "timeout" in low or "timed out" in low:
        return "TIMEOUT"
    if "rate limit" in low or "429" in low:
        return "RATE_LIMIT"
    if "not found in registry" in low:
        return "TOOL_NOT_FOUND"
    return "TOOL_FAILED"


def infer_retryable(code: Optional[str], message: str = "") -> bool:
    if code == "TOOL_NOT_FOUND":
        return False
    return True


def _has_substantive_payload(raw: dict) -> bool:
    for key, value in raw.items():
        if key in _META_KEYS:
            continue
        if value not in (None, "", [], {}, False):
            return True
    return False


def interpret_tool_result(raw: Any) -> Dict[str, Any]:
    """Return ``{ok, data, error, code, retryable}`` for a tool return value."""
    if not isinstance(raw, dict):
        return {
            "ok": True,
            "data": raw,
            "error": None,
            "code": None,
            "retryable": False,
        }

    if isinstance(raw.get("ok"), bool):
        ok = bool(raw["ok"])
        data = raw.get("data")
        if data is None:
            data = {k: v for k, v in raw.items() if k not in ("ok", "error", "code", "retryable")}
        err = raw.get("error")
        err_s = str(err).strip() if err not in (None, "") else None
        code = raw.get("code") or (infer_error_code(err_s or "") if not ok else None)
        retryable = raw.get("retryable")
        if retryable is None:
            retryable = False if ok else infer_retryable(code, err_s or "")
        return {
            "ok": ok,
            "data": {} if data is None else data,
            "error": None if ok else (err_s or "Tool failed"),
            "code": None if ok else code,
            "retryable": False if ok else bool(retryable),
        }

    err = raw.get("error")
    if isinstance(err, str) and err.strip():
        code = raw.get("code") or infer_error_code(err)
        retryable = raw.get("retryable")
        if retryable is None:
            retryable = infer_retryable(code, err)
        return {
            "ok": False,
            "data": raw,
            "error": err.strip(),
            "code": code,
            "retryable": bool(retryable),
        }
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("error") or "").strip()
        if msg:
            code = raw.get("code") or infer_error_code(msg)
            return {
                "ok": False,
                "data": raw,
                "error": msg,
                "code": code,
                "retryable": bool(raw.get("retryable", infer_retryable(code, msg))),
            }

    status = str(raw.get("status") or "").strip().lower()
    if status in _FAILURE_STATUSES and not _has_substantive_payload(raw):
        msg = str(raw.get("message") or raw.get("error") or "Tool failed").strip() or "Tool failed"
        code = raw.get("code") or infer_error_code(msg)
        return {
            "ok": False,
            "data": raw,
            "error": msg,
            "code": code,
            "retryable": bool(raw.get("retryable", infer_retryable(code, msg))),
        }

    return {
        "ok": True,
        "data": raw,
        "error": None,
        "code": None,
        "retryable": False,
    }


def success_payload(envelope: Dict[str, Any], raw: Any) -> Any:
    """Value to store in ``node_outputs`` on success (never wrap in ``{ok,data}``)."""
    if not envelope.get("ok", True):
        return None
    if isinstance(raw, dict) and raw.get("ok") is True and "data" in raw:
        data = raw.get("data")
        return {} if data is None else data
    return raw


def resume_action(resume: Any) -> str:
    """Normalize a LangGraph resume payload into ``retry`` / ``abort`` / ````."""
    if resume is True:
        return "retry"
    if not isinstance(resume, dict):
        return ""
    if resume.get("retry") is True:
        return "retry"
    action = str(resume.get("action") or "").strip().lower()
    if action in _ABORT_ACTIONS:
        return "abort"
    if action in ("retry", "resume"):
        return "retry"
    return action


def pick_tool_error_assignee(history: Optional[list], instance_assigned_to: Any = None, fallback: Any = "human_user") -> Any:
    """Keep the prior UI assignee so a tool-error pause still appears in My Tasks."""
    if instance_assigned_to and instance_assigned_to != "human_user":
        return instance_assigned_to
    for step in reversed(history or []):
        assigned = step.get("assigned_to") if isinstance(step, dict) else None
        if assigned and assigned != "human_user":
            return assigned
    return fallback or "human_user"


def is_tool_error_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("type") == "tool_error":
        return True
    ui_data = payload.get("ui_data")
    return isinstance(ui_data, dict) and bool(ui_data.get("_tool_error"))


def build_tool_error_interrupt(tool_name: str, envelope: Dict[str, Any], node_id: Optional[str] = None) -> Dict[str, Any]:
    ui_data = {
        "error": envelope.get("error"),
        "code": envelope.get("code"),
        "retryable": envelope.get("retryable", True),
        "tool_name": tool_name,
        "_tool_error": True,
    }
    if node_id:
        ui_data["node_id"] = node_id
    payload = {
        "type": "tool_error",
        "tool_name": tool_name,
        "error": envelope.get("error"),
        "code": envelope.get("code"),
        "retryable": envelope.get("retryable", True),
        "assigned_to": "human_user",
        "ui_data": ui_data,
    }
    if node_id:
        payload["node_id"] = node_id
    return payload


def tool_error_sse_payloads(
    execution_id: str,
    workflow_id: str,
    paused_node_id: str,
    ui_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the pause payload + SSE bodies. No screen_id (do not mount a UI bundle)."""
    if not isinstance(ui_payload, dict):
        ui_payload = {"type": "tool_error", "error": str(ui_payload)}
    assigned_role = ui_payload.get("assigned_to") or "human_user"
    ui_data = ui_payload.get("ui_data") if isinstance(ui_payload.get("ui_data"), dict) else {}
    ui_data.setdefault("error", ui_payload.get("error"))
    ui_data.setdefault("code", ui_payload.get("code"))
    ui_data.setdefault("retryable", ui_payload.get("retryable", True))
    ui_data.setdefault("tool_name", ui_payload.get("tool_name"))
    ui_data["_tool_error"] = True
    ui_data["execution_id"] = execution_id
    ui_data["workflow_id"] = workflow_id
    ui_data["node_id"] = paused_node_id
    pause_payload = dict(ui_payload)
    pause_payload["type"] = "tool_error"
    pause_payload["ui_data"] = ui_data
    pause_payload["execution_id"] = execution_id
    pause_payload["assigned_to"] = assigned_role
    pause_payload["node_id"] = paused_node_id
    pause_payload.pop("screen_id", None)
    event_data = {
        "error": pause_payload.get("error"),
        "code": pause_payload.get("code"),
        "retryable": pause_payload.get("retryable", True),
        "tool_name": pause_payload.get("tool_name"),
        "node_id": paused_node_id,
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "assigned_to": assigned_role,
        "ui_data": ui_data,
    }
    history_outputs = {
        "error": pause_payload.get("error"),
        "code": pause_payload.get("code"),
        "retryable": pause_payload.get("retryable", True),
    }
    return {
        "pause_payload": pause_payload,
        "tool_error_event": event_data,
        "ui_request_event": pause_payload,
        "history_outputs": history_outputs,
        "assigned_role": assigned_role,
    }


def run_tool_until_success(
    execute_fn: Callable[[str, dict], Any],
    tool_name: str,
    params: dict,
    interrupt_fn: Optional[Callable[[dict], Any]] = None,
) -> Any:
    """Run ``execute_fn`` and pause via ``interrupt`` until success or abort.

    Default resume (empty payload / ``action: retry``) retries the same call.
    ``action: abort|cancel|fail`` raises ``ToolNodeFailed``.
    """
    if interrupt_fn is None:
        from langgraph.types import interrupt as interrupt_fn  # type: ignore

    while True:
        result = execute_fn(tool_name, params)
        envelope = interpret_tool_result(result)
        if envelope.get("ok", True):
            payload = success_payload(envelope, result)
            return result if payload is None else payload
        resume = interrupt_fn(build_tool_error_interrupt(tool_name, envelope))
        if resume_action(resume) == "abort":
            raise ToolNodeFailed(envelope, tool_name=tool_name)
