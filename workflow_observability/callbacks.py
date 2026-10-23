"""LangGraph / LangChain callback handler for in-graph node and LLM spans."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from workflow_observability.models import SpanDocument, new_span_id, utc_now_iso

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover
    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


_INTERRUPT_TYPE_NAMES = {"GraphInterrupt", "NodeInterrupt", "Interrupt"}


def is_graph_interrupt(error: Any) -> bool:
    """LangGraph UI pauses surface as Interrupt / GraphInterrupt, not real failures."""
    if error is None:
        return False
    try:
        names = {type(error).__name__}
        names.update(base.__name__ for base in type(error).__mro__)
        if names & _INTERRUPT_TYPE_NAMES:
            return True
    except Exception:
        pass
    text = str(error or "")
    return "GraphInterrupt" in text or "Interrupt(" in text


class WorkflowGraphCallbackHandler(BaseCallbackHandler):
    def __init__(self, session: "ObservabilitySession"):
        self._session = session
        self._chain_starts: Dict[str, float] = {}
        self._chain_spans: Dict[str, str] = {}
        self._llm_starts: Dict[str, float] = {}
        self._llm_spans: Dict[str, str] = {}

    def _run_id(self, run_id: Any) -> str:
        return str(run_id or uuid.uuid4())

    def on_chain_start(self, serialized: Optional[Dict[str, Any]], inputs: Dict[str, Any], *, run_id: uuid.UUID, parent_run_id: Optional[uuid.UUID] = None, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        serialized = serialized or {}
        name = (
            kwargs.get("name")
            or serialized.get("name")
            or serialized.get("id")
            or "chain"
        )
        if isinstance(name, (list, tuple)):
            name = name[-1] if name else "chain"
        name = str(name)
        ignored = {
            "LangGraph",
            "RunnableSequence",
            "ChannelWrite",
            "Pregel",
            "CompiledStateGraph",
            "CompiledGraph",
        }
        if name.startswith("LangGraph") or name.startswith("__") or name in ignored:
            return
        span_id = new_span_id()
        self._chain_starts[rid] = time.perf_counter()
        self._chain_spans[rid] = span_id
        parent = self._session.current_node_span_id
        span = SpanDocument(
            span_id=span_id,
            trace_id=self._session.trace_id,
            parent_span_id=parent,
            kind="node",
            name=str(name),
            workflow_id=self._session.workflow_id,
            subscriber_id=self._session.subscriber_id,
            org_id=self._session.org_id,
            user_id=self._session.user_id,
            graph_type=self._session.graph_type,
            node_name=str(name),
            metadata={"run_id": rid},
        )
        self._session.emit_span(span, finalize=False)
        self._session.set_current_node(str(name), span_id)

    def on_chain_end(self, outputs: Dict[str, Any], *, run_id: uuid.UUID, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        span_id = self._chain_spans.pop(rid, None)
        started = self._chain_starts.pop(rid, None)
        if not span_id or started is None:
            return
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._session.finalize_span(
            span_id,
            status="success",
            latency_ms=latency_ms,
            completion=outputs,
        )

    def on_chain_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        span_id = self._chain_spans.pop(rid, None)
        started = self._chain_starts.pop(rid, None)
        if not span_id or started is None:
            return
        latency_ms = (time.perf_counter() - started) * 1000.0
        if is_graph_interrupt(error):
            self._session.finalize_span(
                span_id,
                status="paused",
                latency_ms=latency_ms,
            )
            return
        self._session.finalize_span(
            span_id,
            status="error",
            latency_ms=latency_ms,
            error=str(error),
        )

    def on_llm_start(self, serialized: Optional[Dict[str, Any]], prompts: list[str], *, run_id: uuid.UUID, parent_run_id: Optional[uuid.UUID] = None, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        span_id = new_span_id()
        self._llm_starts[rid] = time.perf_counter()
        self._llm_spans[rid] = span_id
        model = (serialized or {}).get("name") or (kwargs.get("metadata") or {}).get("ls_model_name") or "llm"
        parent = self._session.current_node_span_id
        span = SpanDocument(
            span_id=span_id,
            trace_id=self._session.trace_id,
            parent_span_id=parent,
            kind="llm_graph",
            name=str(model),
            model=str(model),
            workflow_id=self._session.workflow_id,
            subscriber_id=self._session.subscriber_id,
            org_id=self._session.org_id,
            user_id=self._session.user_id,
            graph_type=self._session.graph_type,
            node_name=self._session.current_node_name,
            prompt=prompts,
            metadata={"run_id": rid},
        )
        self._session.emit_span(span, finalize=False)

    def on_llm_end(self, response: Any, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        span_id = self._llm_spans.pop(rid, None)
        started = self._llm_starts.pop(rid, None)
        if not span_id or started is None:
            return
        latency_ms = (time.perf_counter() - started) * 1000.0
        completion = None
        token_usage = None
        model = None
        try:
            generations = getattr(response, "generations", None) or []
            if generations and generations[0]:
                completion = getattr(generations[0][0], "text", None)
            llm_output = getattr(response, "llm_output", None) or {}
            token_usage = llm_output.get("token_usage")
            model = llm_output.get("model_name")
        except Exception:
            pass
        self._session.finalize_span(
            span_id,
            status="success",
            latency_ms=latency_ms,
            completion=completion,
            token_usage=token_usage,
            model=model,
        )

    def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        rid = self._run_id(run_id)
        span_id = self._llm_spans.pop(rid, None)
        started = self._llm_starts.pop(rid, None)
        if not span_id or started is None:
            return
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._session.finalize_span(
            span_id,
            status="error",
            latency_ms=latency_ms,
            error=str(error),
        )
