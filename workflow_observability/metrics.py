"""Read-side metrics aggregation over workflow observability spans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from workflow_observability.store import SpanStore, get_span_store

LLM_KINDS = ("llm_graph", "llm_tool", "llm_ui")

# USD per 1M tokens — conservative defaults for cost estimates.
DEFAULT_RATES = {
    "prompt": 0.10,
    "completion": 0.40,
}


class WorkflowObsMetrics:
    def __init__(self, store: Optional[SpanStore] = None):
        self._store = store or get_span_store()

    def trace_timeline(self, execution_id: str) -> List[dict]:
        spans = self._store.get_trace_spans(execution_id)
        return [
            {
                "span_id": s.get("span_id"),
                "parent_span_id": s.get("parent_span_id"),
                "kind": s.get("kind"),
                "name": s.get("name"),
                "status": s.get("status"),
                "latency_ms": s.get("latency_ms"),
                "model": s.get("model"),
                "provider": s.get("provider"),
                "token_usage": s.get("token_usage"),
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                "node_name": s.get("node_name"),
                "error": s.get("error"),
                "metadata": s.get("metadata") or {},
            }
            for s in spans
        ]

    def summary(
        self,
        *,
        workflow_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Dict[str, Any]:
        match: Dict[str, Any] = {}
        if workflow_id:
            match["workflow_id"] = workflow_id
        if subscriber_id:
            match["subscriberId"] = subscriber_id
        if since or until:
            started_at: Dict[str, Any] = {}
            if since:
                started_at["$gte"] = since
            if until:
                started_at["$lte"] = until
            match["started_at"] = started_at

        kind_rows = self._store.aggregate(
            [
                {"$match": match} if match else {"$match": {}},
                {
                    "$group": {
                        "_id": {"kind": "$kind", "status": "$status"},
                        "count": {"$sum": 1},
                        "avg_latency_ms": {"$avg": "$latency_ms"},
                        "total_prompt_tokens": {
                            "$sum": {"$ifNull": ["$token_usage.prompt_tokens", 0]}
                        },
                        "total_completion_tokens": {
                            "$sum": {"$ifNull": ["$token_usage.completion_tokens", 0]}
                        },
                    }
                },
            ]
        )
        by_kind: Dict[str, Any] = {}
        by_status: Dict[str, int] = {}
        for row in kind_rows:
            kind = (row.get("_id") or {}).get("kind") or "unknown"
            status = (row.get("_id") or {}).get("status") or "unknown"
            bucket = by_kind.setdefault(kind, {"total": 0, "by_status": {}})
            count = int(row.get("count") or 0)
            bucket["total"] += count
            bucket["by_status"][status] = {
                "count": count,
                "avg_latency_ms": row.get("avg_latency_ms"),
                "total_prompt_tokens": int(row.get("total_prompt_tokens") or 0),
                "total_completion_tokens": int(row.get("total_completion_tokens") or 0),
            }
            by_status[status] = by_status.get(status, 0) + count

        model_rows = self._store.aggregate(
            [
                {
                    "$match": {
                        **match,
                        "kind": {"$in": list(LLM_KINDS)},
                    }
                },
                {
                    "$group": {
                        "_id": {"model": "$model", "kind": "$kind"},
                        "count": {"$sum": 1},
                        "avg_latency_ms": {"$avg": "$latency_ms"},
                        "prompt_tokens": {"$sum": {"$ifNull": ["$token_usage.prompt_tokens", 0]}},
                        "completion_tokens": {
                            "$sum": {"$ifNull": ["$token_usage.completion_tokens", 0]}
                        },
                    }
                },
            ]
        )
        by_model: Dict[str, Any] = {}
        tokens_by_kind = {k: {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0} for k in LLM_KINDS}
        prompt_total = 0
        completion_total = 0
        for row in model_rows:
            model = (row.get("_id") or {}).get("model") or "unknown"
            kind = (row.get("_id") or {}).get("kind") or "llm"
            prompt = int(row.get("prompt_tokens") or 0)
            completion = int(row.get("completion_tokens") or 0)
            count = int(row.get("count") or 0)
            entry = by_model.setdefault(
                model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_kind": {}}
            )
            entry["calls"] += count
            entry["prompt_tokens"] += prompt
            entry["completion_tokens"] += completion
            entry["by_kind"][kind] = {
                "calls": count,
                "avg_latency_ms": row.get("avg_latency_ms"),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            }
            if kind in tokens_by_kind:
                tokens_by_kind[kind]["prompt_tokens"] += prompt
                tokens_by_kind[kind]["completion_tokens"] += completion
                tokens_by_kind[kind]["calls"] += count
            prompt_total += prompt
            completion_total += completion

        cost_usd = (prompt_total / 1_000_000.0) * DEFAULT_RATES["prompt"] + (
            completion_total / 1_000_000.0
        ) * DEFAULT_RATES["completion"]

        ui_pauses = (by_kind.get("ui") or {}).get("total") or 0
        human_resumes = (by_kind.get("human") or {}).get("total") or 0
        interference_rate = (human_resumes / ui_pauses) if ui_pauses else None

        node_total = (by_kind.get("node") or {}).get("total") or 0
        node_success = ((by_kind.get("node") or {}).get("by_status") or {}).get("success", {}).get("count") or 0
        node_error = ((by_kind.get("node") or {}).get("by_status") or {}).get("error", {}).get("count") or 0
        utility_rate = (node_success / node_total) if node_total else None

        return {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "filters": {
                "workflow_id": workflow_id,
                "subscriberId": subscriber_id,
                "since": since,
                "until": until,
            },
            "by_kind": by_kind,
            "by_status": by_status,
            "by_model": by_model,
            "tokens": {
                "prompt_tokens": prompt_total,
                "completion_tokens": completion_total,
                "total_tokens": prompt_total + completion_total,
                "by_kind": tokens_by_kind,
            },
            "cost_usd_estimate": round(cost_usd, 6),
            "human": {
                "ui_pauses": ui_pauses,
                "resumes": human_resumes,
                "interference_rate": interference_rate,
            },
            "step_utility": {
                "nodes": node_total,
                "success": node_success,
                "failed": node_error,
                "utility_rate": utility_rate,
            },
        }
