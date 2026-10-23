"""In-memory span store for tests — same interface as SpanStore, no Mongo."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _nested_get(doc: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, clause) for clause in (expected or [])):
                return False
            continue
        if key == "$in":
            continue
        if "." in key:
            actual = _nested_get(doc, key)
        else:
            actual = doc.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected:
                exists = key in doc if "." not in key else _nested_get(doc, key, default=Ellipsis) is not Ellipsis
                if bool(expected["$exists"]) != bool(exists):
                    return False
            if "$ne" in expected:
                if actual == expected["$ne"]:
                    return False
            if "$in" in expected:
                if actual not in expected["$in"]:
                    return False
                continue
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            continue
        if actual != expected:
            return False
    return True


class InMemorySpanStore:
    def __init__(self):
        self.spans: Dict[str, dict] = {}
        self.traces: Dict[str, dict] = {}
        self.tokens: List[dict] = []
        self.instances: List[dict] = []

    def write_span(self, doc: dict) -> None:
        span_id = doc.get("span_id")
        if not span_id:
            return
        existing = self.spans.get(span_id, {})
        merged = {**existing, **doc}
        if existing.get("started_at") and "started_at" not in doc:
            merged["started_at"] = existing["started_at"]
        elif existing.get("started_at") and doc.get("started_at"):
            # Finalize updates must not clobber the original start time.
            merged["started_at"] = existing["started_at"]
        self.spans[span_id] = merged
        if str(merged.get("kind") or "") in {"llm_tool", "llm_ui"}:
            usage = merged.get("token_usage") or {}
            self.tokens.append(
                {
                    "execution_id": merged.get("trace_id"),
                    "workflow_id": merged.get("workflow_id"),
                    "kind": merged.get("kind"),
                    "span_id": span_id,
                    "model": merged.get("model"),
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(
                        usage.get("total_tokens")
                        or (
                            int(usage.get("prompt_tokens") or 0)
                            + int(usage.get("completion_tokens") or 0)
                        )
                    ),
                }
            )

    def upsert_trace(self, trace_doc: dict) -> None:
        trace_id = trace_doc.get("trace_id")
        if not trace_id:
            return
        existing = self.traces.get(trace_id, {})
        merged = {**existing, **trace_doc}
        if existing.get("started_at"):
            merged["started_at"] = existing["started_at"]
        self.traces[trace_id] = merged

    def flush(self, *, timeout: float = 5.0) -> None:
        return

    def get_trace_spans(self, trace_id: str) -> list[dict]:
        rows = [s for s in self.spans.values() if s.get("trace_id") == trace_id]
        return sorted(rows, key=lambda s: s.get("started_at") or "")

    def find_instances(self, query: dict, *, limit: int = 25) -> list[dict]:
        docs = getattr(self, "instances", None) or []
        matched = [d for d in docs if _matches(d, query)]
        return matched[:limit]

    def aggregate(self, pipeline: list[dict]) -> list[dict]:
        docs: List[dict] = list(self.spans.values())
        for stage in pipeline or []:
            if "$match" in stage:
                query = stage["$match"] or {}
                docs = [d for d in docs if _matches(d, query)]
            elif "$group" in stage:
                docs = self._group(docs, stage["$group"])
        return docs

    def _group(self, docs: List[dict], spec: dict) -> list[dict]:
        id_spec = spec.get("_id")
        buckets: Dict[Any, dict] = {}
        latencies: Dict[Any, List[float]] = {}

        def resolve_id(doc: dict):
            if isinstance(id_spec, dict):
                value = {}
                for k, expr in id_spec.items():
                    field = expr[1:] if isinstance(expr, str) and expr.startswith("$") else expr
                    value[k] = doc.get(field)
                return tuple(sorted(value.items())), value
            field = str(id_spec).lstrip("$") if id_spec else None
            val = doc.get(field) if field else None
            return val, val

        for doc in docs:
            key, id_value = resolve_id(doc)
            bucket = buckets.setdefault(key, {"_id": id_value})
            lats = latencies.setdefault(key, [])
            for out_key, expr in spec.items():
                if out_key == "_id" or not isinstance(expr, dict):
                    continue
                if "$sum" in expr:
                    summed = expr["$sum"]
                    if summed == 1:
                        bucket[out_key] = int(bucket.get(out_key, 0) or 0) + 1
                    else:
                        bucket[out_key] = float(bucket.get(out_key, 0) or 0) + self._sum_expr(doc, summed)
                elif "$avg" in expr:
                    field = expr["$avg"]
                    name = field[1:] if isinstance(field, str) and field.startswith("$") else field
                    val = doc.get(name)
                    if val is not None:
                        lats.append(float(val))
            if lats:
                bucket["avg_latency_ms"] = sum(lats) / len(lats)
        return list(buckets.values())

    def _sum_expr(self, doc: dict, expr: Any) -> float:
        if expr == 1:
            return 1
        if isinstance(expr, dict) and "$ifNull" in expr:
            field, default = expr["$ifNull"]
            name = field[1:] if isinstance(field, str) and field.startswith("$") else field
            val = _nested_get(doc, name, default)
            try:
                return float(val or 0)
            except (TypeError, ValueError):
                return 0
        if isinstance(expr, str) and expr.startswith("$"):
            val = _nested_get(doc, expr[1:], 0)
            try:
                return float(val or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def kinds(self, trace_id: Optional[str] = None) -> List[str]:
        rows = self.spans.values()
        if trace_id:
            rows = [s for s in rows if s.get("trace_id") == trace_id]
        return [s.get("kind") for s in rows]
