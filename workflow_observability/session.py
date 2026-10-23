"""Observability session lifecycle for a single workflow execution."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from workflow_observability.callbacks import WorkflowGraphCallbackHandler
from workflow_observability.config import obs_enabled, payload_mode
from workflow_observability.context import reset_tool_span_id, set_tool_span_id
from workflow_observability.digest import prepare_payload
from workflow_observability.models import SpanDocument, new_span_id, utc_now_iso
from workflow_observability.sdk_patch import sdk_patch_context
from workflow_observability.store import get_span_store


class ObservabilitySession:
    def __init__(
        self,
        *,
        trace_id: str,
        workflow_id: str,
        subscriber_id: Optional[str] = None,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
        graph_type: str = "FREEZE",
        created_by: Optional[str] = None,
        thread_id: Optional[str] = None,
    ):
        self.trace_id = trace_id
        self.workflow_id = workflow_id
        self.subscriber_id = subscriber_id
        self.org_id = org_id
        # Prefer explicit user_id; fall back to created_by (instance starter).
        self.user_id = user_id or created_by
        self.graph_type = graph_type
        self.created_by = created_by or user_id
        self.thread_id = thread_id
        self._store = get_span_store()
        self._open_spans: Dict[str, SpanDocument] = {}
        self.current_node_name: Optional[str] = None
        self.current_node_span_id: Optional[str] = None
        self._trace_started_at = utc_now_iso()
        self._payload_mode = payload_mode()
        self._last_ui_span_id: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return obs_enabled()

    def start_trace(self) -> None:
        if not self.enabled:
            return
        try:
            self._store.upsert_trace(
                {
                    "trace_id": self.trace_id,
                    "workflow_id": self.workflow_id,
                    "subscriberId": self.subscriber_id,
                    "orgId": self.org_id,
                    "userId": self.user_id,
                    "graph_type": self.graph_type,
                    "status": "running",
                    "started_at": self._trace_started_at,
                }
            )
        except Exception:
            pass

    def finish_trace(self, status: str) -> None:
        if not self.enabled:
            return
        try:
            self._store.upsert_trace(
                {
                    "trace_id": self.trace_id,
                    "workflow_id": self.workflow_id,
                    "subscriberId": self.subscriber_id,
                    "orgId": self.org_id,
                    "userId": self.user_id,
                    "graph_type": self.graph_type,
                    "status": status,
                    "started_at": self._trace_started_at,
                    "ended_at": utc_now_iso(),
                }
            )
        except Exception:
            pass

    def make_graph_callback(self) -> WorkflowGraphCallbackHandler:
        return WorkflowGraphCallbackHandler(self)

    def set_current_node(self, node_name: str, span_id: Optional[str]) -> None:
        self.current_node_name = node_name
        self.current_node_span_id = span_id

    def ensure_node_span(self, node_name: Optional[str]) -> Optional[str]:
        """Create a node span when LangGraph callbacks did not (typical FREEZE tools)."""
        if self.current_node_span_id:
            return self.current_node_span_id
        name = node_name or "node"
        span_id = new_span_id()
        span = SpanDocument(
            span_id=span_id,
            trace_id=self.trace_id,
            kind="node",
            name=name,
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id,
            graph_type=self.graph_type,
            node_name=name,
            metadata={"source": "ensure_node_span"},
        )
        self.emit_span(span, finalize=False)
        self.set_current_node(name, span_id)
        return span_id

    def enrich_last_llm_graph(
        self,
        *,
        node_name: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Backup token fill when graph callbacks missed usage_metadata."""
        if not self.enabled or total_tokens <= 0:
            return
        usage = {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
        }
        target = None
        for span in reversed(list(self._open_spans.values())):
            if span.kind != "llm_graph":
                continue
            if span.node_name == node_name:
                target = span
                break
            if target is None:
                target = span
        if target is not None and not target.token_usage:
            self.finalize_span(
                target.span_id,
                status=target.status or "success",
                token_usage=usage,
                model=model or target.model,
            )
            return
        self.ensure_node_span(node_name)
        span = SpanDocument(
            span_id=new_span_id(),
            trace_id=self.trace_id,
            parent_span_id=self.current_node_span_id,
            kind="llm_graph",
            name=model or "llm",
            model=model,
            status="success",
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id,
            graph_type=self.graph_type,
            node_name=node_name,
            token_usage=usage,
            ended_at=utc_now_iso(),
            metadata={"source": "usage_scrape"},
        )
        self.emit_span(span, finalize=True)

    def emit_span(self, span: SpanDocument, *, finalize: bool = True) -> None:
        if not self.enabled:
            return
        doc = span.to_mongo()
        doc["prompt"] = prepare_payload(doc.get("prompt"), self._payload_mode)
        doc["completion"] = prepare_payload(doc.get("completion"), self._payload_mode)
        self._open_spans[span.span_id] = span
        try:
            self._store.write_span(doc)
        except Exception:
            pass

    def finalize_span(
        self,
        span_id: str,
        *,
        status: str,
        latency_ms: Optional[float] = None,
        prompt: Any = None,
        completion: Any = None,
        token_usage: Optional[dict] = None,
        model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        span = self._open_spans.get(span_id)
        if span is None:
            span = SpanDocument(
                span_id=span_id,
                trace_id=self.trace_id,
                kind="unknown",
                name=span_id,
            )
        span.status = status
        span.ended_at = utc_now_iso()
        if latency_ms is not None:
            span.latency_ms = latency_ms
        if prompt is not None:
            span.prompt = prompt
        if completion is not None:
            span.completion = completion
        if token_usage is not None:
            span.token_usage = token_usage
        if model is not None:
            span.model = model
        if error is not None:
            span.error = error
        doc = span.to_mongo()
        doc["prompt"] = prepare_payload(doc.get("prompt"), self._payload_mode)
        doc["completion"] = prepare_payload(doc.get("completion"), self._payload_mode)
        try:
            self._store.write_span(doc)
        except Exception:
            pass

    @contextmanager
    def tool_execution(self, tool_name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return

        span_id = new_span_id()
        parent = self.ensure_node_span(self.current_node_name or tool_name)
        token = set_tool_span_id(span_id)
        started = time.perf_counter()
        span = SpanDocument(
            span_id=span_id,
            trace_id=self.trace_id,
            parent_span_id=parent,
            kind="tool",
            name=tool_name,
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id,
            graph_type=self.graph_type,
            node_name=self.current_node_name,
            metadata={"tool_name": tool_name},
        )
        self.emit_span(span, finalize=False)
        try:
            with sdk_patch_context(
                emit_llm_span=self._emit_nested_llm,
                parent_span_id=span_id,
                trace_id=self.trace_id,
                kind="llm_tool",
                metadata={"tool_name": tool_name, "node_name": self.current_node_name},
            ):
                yield
            latency_ms = (time.perf_counter() - started) * 1000.0
            self.finalize_span(span_id, status="success", latency_ms=latency_ms)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            self.finalize_span(span_id, status="error", latency_ms=latency_ms, error=str(exc))
            raise
        finally:
            reset_tool_span_id(token)

    def _emit_nested_llm(self, span: SpanDocument) -> None:
        span.workflow_id = self.workflow_id
        span.subscriber_id = self.subscriber_id
        span.org_id = self.org_id
        span.user_id = self.user_id
        span.graph_type = self.graph_type
        span.node_name = self.current_node_name
        self.emit_span(span, finalize=True)

    def record_ui_pause(self, *, node_name: str, screen_id: str) -> Optional[str]:
        if not self.enabled:
            return None
        span_id = new_span_id()
        span = SpanDocument(
            span_id=span_id,
            trace_id=self.trace_id,
            parent_span_id=self.current_node_span_id,
            kind="ui",
            name=screen_id,
            status="paused",
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id,
            graph_type=self.graph_type,
            node_name=node_name,
            ended_at=utc_now_iso(),
            metadata={"screen_id": screen_id},
        )
        self.emit_span(span, finalize=True)
        self._last_ui_span_id = span_id
        return span_id

    def record_human_resume(
        self,
        *,
        node_name: Optional[str],
        payload: dict,
        userid: Optional[str] = None,
        roleid: Optional[str] = None,
        ui_span_id: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        parent = ui_span_id or self._last_ui_span_id
        span = SpanDocument(
            span_id=new_span_id(),
            trace_id=self.trace_id,
            parent_span_id=parent,
            kind="human",
            name=node_name or "human_resume",
            status="success",
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id or userid,
            graph_type=self.graph_type,
            node_name=node_name,
            ended_at=utc_now_iso(),
            metadata={
                "userid": userid,
                "roleid": roleid,
                "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            },
        )
        self.emit_span(span, finalize=True)

    def record_node_failure(self, node_name: str, error: str) -> None:
        if not self.enabled:
            return
        span = SpanDocument(
            span_id=new_span_id(),
            trace_id=self.trace_id,
            kind="node",
            name=node_name,
            status="error",
            workflow_id=self.workflow_id,
            subscriber_id=self.subscriber_id,
            org_id=self.org_id,
            user_id=self.user_id,
            graph_type=self.graph_type,
            node_name=node_name,
            error=error,
            ended_at=utc_now_iso(),
        )
        self.emit_span(span, finalize=True)


def maybe_session_for_instance(instance: Any, graph_type: str) -> Optional[ObservabilitySession]:
    if not obs_enabled():
        return None
    execution_id = getattr(instance, "execution_id", None)
    workflow_id = getattr(instance, "workflow_id", None)
    if not execution_id or not workflow_id:
        return None
    created_by = getattr(instance, "created_by", None)
    return ObservabilitySession(
        trace_id=str(execution_id),
        workflow_id=str(workflow_id),
        subscriber_id=getattr(instance, "subscriber_id", None),
        org_id=getattr(instance, "org_id", None),
        user_id=created_by,
        graph_type=str(graph_type or "FREEZE"),
        created_by=created_by,
        thread_id=getattr(instance, "thread_id", None),
    )
