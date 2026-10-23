"""Install SDK + HTTP patches inside the agent sandbox subprocess."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from workflow_observability.config import obs_enabled, payload_mode
from workflow_observability.digest import prepare_payload
from workflow_observability.models import SpanDocument
from workflow_observability.sdk_patch import sdk_patch_context
from workflow_observability.store import get_span_store


def _emit_llm_ui(span: SpanDocument, *, workflow_id, subscriber_id, org_id, user_id, node_name, function_id) -> None:
    span.workflow_id = workflow_id
    span.subscriber_id = subscriber_id
    span.org_id = org_id
    span.user_id = user_id
    span.node_name = node_name
    span.metadata = dict(span.metadata or {})
    span.metadata.setdefault("function_id", function_id)
    span.metadata.setdefault("source", "sandbox")
    mode = payload_mode()
    doc = span.to_mongo()
    doc["prompt"] = prepare_payload(doc.get("prompt"), mode)
    doc["completion"] = prepare_payload(doc.get("completion"), mode)
    get_span_store().write_span(doc)


@contextmanager
def sandbox_observe(request_meta: Optional[Dict[str, Any]], function_name: str) -> Iterator[None]:
    """Patch LLM SDKs/HTTP for one sandbox function run; flush spans on exit."""
    meta = request_meta or {}
    if not obs_enabled():
        yield
        return
    trace_id = meta.get("execution_id") or meta.get("executionId")
    if not trace_id:
        yield
        return

    parent_span_id = meta.get("_obs_span_id") or meta.get("_obs_ui_span_id")
    workflow_id = meta.get("workflow_id") or meta.get("workflowId")
    subscriber_id = meta.get("subscriber_id") or meta.get("subscriberId")
    org_id = meta.get("org_id") or meta.get("orgId")
    user_id = meta.get("user_id") or meta.get("userId")
    node_name = meta.get("screen_id") or meta.get("screenId")

    def emit(span: SpanDocument) -> None:
        _emit_llm_ui(
            span,
            workflow_id=workflow_id,
            subscriber_id=subscriber_id,
            org_id=org_id,
            user_id=user_id,
            node_name=node_name,
            function_id=function_name,
        )

    try:
        with sdk_patch_context(
            emit_llm_span=emit,
            parent_span_id=parent_span_id,
            trace_id=str(trace_id),
            kind="llm_ui",
            metadata={"function_id": function_name, "screen_id": node_name},
        ):
            yield
    finally:
        try:
            get_span_store().flush(timeout=2.0)
        except Exception:
            pass
