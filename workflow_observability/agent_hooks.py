"""Agent-side hooks for /dynamic/execute UI backend function calls."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from workflow_observability.config import obs_enabled
from workflow_observability.models import SpanDocument, new_span_id, utc_now_iso
from workflow_observability.store import get_span_store

logger = logging.getLogger(__name__)

_PENDING_STATUSES = ("Pending Review", "waiting_human", "paused")


def _meta_get(meta: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = meta.get(key)
        if value is not None and value != "":
            return value
    return None


def enrich_request_meta_from_pending_ui(
    request_meta: Optional[Dict[str, Any]],
    *,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fill execution_id / ui parentage from a live paused workflow instance.

    Older Angular screens often call /dynamic/execute with only
    ``request_meta.source=angular_app`` and never forward ``execution_id``.
    Without it, ui_function / llm_ui spans are dropped. Recover correlation
    from the LangGraph ``instances`` collection when a matching pause exists.
    """
    meta = dict(request_meta) if isinstance(request_meta, dict) else {}
    params = params if isinstance(params, dict) else {}

    if not _meta_get(meta, "execution_id", "executionId"):
        for key in ("execution_id", "executionId"):
            if params.get(key):
                meta["execution_id"] = params[key]
                break
    if not _meta_get(meta, "workflow_id", "workflowId"):
        for key in ("workflow_id", "workflowId"):
            if params.get(key):
                meta["workflow_id"] = params[key]
                break
    if not _meta_get(meta, "screen_id", "screenId"):
        for key in ("screen_id", "screenId"):
            if params.get(key):
                meta["screen_id"] = params[key]
                break
    if not meta.get("_obs_ui_span_id") and params.get("_obs_ui_span_id"):
        meta["_obs_ui_span_id"] = params["_obs_ui_span_id"]

    if _meta_get(meta, "execution_id", "executionId"):
        return meta
    if not obs_enabled():
        return meta

    screen_id = _meta_get(meta, "screen_id", "screenId")
    user_id = _meta_get(meta, "user_id", "userId")
    subscriber_id = _meta_get(meta, "subscriber_id", "subscriberId")
    org_id = _meta_get(meta, "org_id", "orgId")
    workflow_id = _meta_get(meta, "workflow_id", "workflowId")

    # Never correlate on subscriber alone — that can attach spans to the wrong
    # concurrent execution. Require screen_id (Referer/FE) and/or user_id.
    if not screen_id and not user_id:
        logger.debug(
            "Skipping pending-UI correlation: need screen_id or user_id "
            "(subscriber_id=%s)",
            subscriber_id,
        )
        return meta

    query: Dict[str, Any] = {
        "status": {"$in": list(_PENDING_STATUSES)},
        "pending_ui_request": {"$exists": True, "$ne": None},
    }
    if subscriber_id:
        query["subscriber_id"] = subscriber_id
    if org_id:
        query["org_id"] = org_id
    if workflow_id:
        query["workflow_id"] = workflow_id
    # screen_id is specific enough; only constrain by user when screen is unknown.
    if user_id and not screen_id:
        query["$or"] = [
            {"created_by": user_id},
            {"assigned_to": user_id},
            {"assigned_to.users": user_id},
        ]

    try:
        store = get_span_store()
        find_instances = getattr(store, "find_instances", None)
        if not callable(find_instances):
            return meta
        docs = find_instances(query, limit=25)
        chosen = None
        for doc in docs:
            pending = doc.get("pending_ui_request") or {}
            if not isinstance(pending, dict):
                continue
            ui_data = pending.get("ui_data") if isinstance(pending.get("ui_data"), dict) else {}
            pending_screen = (
                pending.get("screen_id")
                or ui_data.get("screen_id")
                or ui_data.get("screenId")
            )
            # When the caller knows the screen, require an exact match.
            if screen_id and str(pending_screen or "") != str(screen_id):
                continue
            chosen = doc
            break

        if not chosen:
            return meta

        pending = chosen.get("pending_ui_request") or {}
        ui_data = pending.get("ui_data") if isinstance(pending.get("ui_data"), dict) else {}
        exec_id = chosen.get("execution_id") or ui_data.get("execution_id")
        if not exec_id:
            return meta

        meta["execution_id"] = str(exec_id)
        meta.setdefault("workflow_id", chosen.get("workflow_id") or ui_data.get("workflow_id"))
        meta.setdefault(
            "screen_id",
            pending.get("screen_id") or ui_data.get("screen_id") or chosen.get("current_node"),
        )
        if not meta.get("_obs_ui_span_id"):
            span = ui_data.get("_obs_ui_span_id")
            if span:
                meta["_obs_ui_span_id"] = span
        if not _meta_get(meta, "user_id", "userId") and chosen.get("created_by"):
            meta["user_id"] = chosen.get("created_by")
            meta["userId"] = chosen.get("created_by")
        if not _meta_get(meta, "subscriber_id", "subscriberId") and chosen.get("subscriber_id"):
            meta["subscriber_id"] = chosen.get("subscriber_id")
            meta["subscriberId"] = chosen.get("subscriber_id")
        if not _meta_get(meta, "org_id", "orgId") and chosen.get("org_id"):
            meta["org_id"] = chosen.get("org_id")
            meta["orgId"] = chosen.get("org_id")
        meta["_obs_correlated_from"] = "pending_ui_instance"
        logger.info(
            "Correlated /dynamic/execute to pending UI execution_id=%s screen_id=%s",
            meta.get("execution_id"),
            meta.get("screen_id"),
        )
    except Exception as exc:
        logger.debug("pending UI correlation skipped: %s", exc)
    return meta


def prepare_dynamic_function_observation(
    *,
    request_meta: Optional[Dict[str, Any]],
    function_id: str,
    project_id: Optional[str] = None,
) -> Optional[str]:
    """Open a running ui_function span and stamp ids onto request_meta for the sandbox."""
    if not obs_enabled():
        return None
    meta = request_meta if isinstance(request_meta, dict) else {}
    trace_id = meta.get("execution_id") or meta.get("executionId")
    if not trace_id:
        logger.debug(
            "Skipping ui_function span for %s: missing execution_id in request_meta",
            function_id,
        )
        return None
    span_id = meta.get("_obs_span_id") or new_span_id()
    meta["_obs_span_id"] = span_id
    span = SpanDocument(
        span_id=span_id,
        trace_id=str(trace_id),
        parent_span_id=meta.get("_obs_ui_span_id"),
        kind="ui_function",
        name=function_id,
        status="running",
        workflow_id=meta.get("workflow_id") or meta.get("workflowId"),
        subscriber_id=meta.get("subscriber_id") or meta.get("subscriberId"),
        org_id=meta.get("org_id") or meta.get("orgId"),
        user_id=meta.get("user_id") or meta.get("userId"),
        node_name=meta.get("screen_id") or meta.get("screenId"),
        metadata={
            "project_id": project_id or meta.get("project_id"),
            "source": meta.get("source"),
            "function_id": function_id,
            "correlated_from": meta.get("_obs_correlated_from"),
        },
    )
    try:
        get_span_store().write_span(span.to_mongo())
    except Exception:
        pass
    return span_id


def observe_dynamic_function_execution(
    *,
    request_meta: Optional[Dict[str, Any]] = None,
    function_id: str,
    success: bool,
    latency_ms: Optional[float] = None,
    error: Optional[str] = None,
    project_id: Optional[str] = None,
) -> None:
    """Finalize the UI backend function span correlated to a workflow trace."""
    if not obs_enabled():
        return
    meta = request_meta or {}
    trace_id = meta.get("execution_id") or meta.get("executionId")
    if not trace_id:
        return

    span_id = meta.get("_obs_span_id") or new_span_id()
    # Omit started_at so upserts preserve the original running-span timestamp
    # (Mongo $setOnInsert / in-memory merge).
    span = SpanDocument(
        span_id=span_id,
        trace_id=str(trace_id),
        parent_span_id=meta.get("_obs_ui_span_id"),
        kind="ui_function",
        name=function_id,
        status="success" if success else "error",
        workflow_id=meta.get("workflow_id") or meta.get("workflowId"),
        subscriber_id=meta.get("subscriber_id") or meta.get("subscriberId"),
        org_id=meta.get("org_id") or meta.get("orgId"),
        user_id=meta.get("user_id") or meta.get("userId"),
        node_name=meta.get("screen_id") or meta.get("screenId"),
        latency_ms=latency_ms,
        ended_at=utc_now_iso(),
        error=error,
        metadata={
            "project_id": project_id or meta.get("project_id"),
            "source": meta.get("source"),
            "function_id": function_id,
            "correlated_from": meta.get("_obs_correlated_from"),
        },
    )
    doc = span.to_mongo()
    doc.pop("started_at", None)
    try:
        get_span_store().write_span(doc)
    except Exception:
        pass
