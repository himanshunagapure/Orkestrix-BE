"""Span and trace document models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Python attrs → Mongo field names for tenant identity columns.
_MONGO_IDENTITY_FIELDS = {
    "subscriber_id": "subscriberId",
    "org_id": "orgId",
    "user_id": "userId",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_span_id() -> str:
    return f"span_{uuid.uuid4().hex[:16]}"


@dataclass
class SpanDocument:
    span_id: str
    trace_id: str
    kind: str
    name: str
    status: str = "running"
    parent_span_id: Optional[str] = None
    workflow_id: Optional[str] = None
    subscriber_id: Optional[str] = None
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    node_name: Optional[str] = None
    graph_type: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    latency_ms: Optional[float] = None
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    prompt: Optional[Any] = None
    completion: Optional[Any] = None
    token_usage: Optional[Dict[str, int]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mongo(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "metadata": self.metadata or {},
        }
        for key in (
            "parent_span_id",
            "workflow_id",
            "subscriber_id",
            "org_id",
            "user_id",
            "node_name",
            "graph_type",
            "model",
            "provider",
            "latency_ms",
            "ended_at",
            "prompt",
            "completion",
            "token_usage",
            "error",
        ):
            value = getattr(self, key)
            if value is not None:
                doc[_MONGO_IDENTITY_FIELDS.get(key, key)] = value
        return doc
