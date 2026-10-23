"""Runtime helper for generated tools/functions — wrap an LLM call explicitly."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from workflow_observability.config import obs_enabled
from workflow_observability.context import get_session, get_tool_span_id
from workflow_observability.models import SpanDocument, new_span_id, utc_now_iso


def observe_llm(
    *,
    provider: str,
    model: str,
    prompt: Any,
    call: Callable[[], Any],
    kind: Optional[str] = None,
) -> Any:
    """Time `call()`, emit an llm_* span, and return the raw response.

    Safe no-op when observability is disabled or no session is active.
    """
    if not obs_enabled():
        return call()

    started = time.perf_counter()
    try:
        resp = call()
        latency_ms = (time.perf_counter() - started) * 1000.0
        _emit(provider, model, prompt, resp, latency_ms, kind, status="success")
        return resp
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        _emit(provider, model, prompt, None, latency_ms, kind, status="error", error=str(exc))
        raise


def _emit(
    provider: str,
    model: str,
    prompt: Any,
    completion: Any,
    latency_ms: float,
    kind: Optional[str],
    *,
    status: str,
    error: Optional[str] = None,
) -> None:
    session = get_session()
    parent = get_tool_span_id()
    if session is None:
        return
    resolved_kind = kind or ("llm_tool" if parent else "llm_graph")
    text = None
    try:
        text = getattr(completion, "text", None) or completion
    except Exception:
        text = completion
    span = SpanDocument(
        span_id=new_span_id(),
        trace_id=session.trace_id,
        parent_span_id=parent or session.current_node_span_id,
        kind=resolved_kind,
        name=f"{provider}:{model}",
        status=status,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        prompt=prompt,
        completion=text,
        error=error,
        ended_at=utc_now_iso(),
        workflow_id=session.workflow_id,
        subscriber_id=session.subscriber_id,
        org_id=session.org_id,
        user_id=session.user_id,
        graph_type=session.graph_type,
        node_name=session.current_node_name,
    )
    try:
        session.emit_span(span, finalize=True)
    except Exception:
        pass
