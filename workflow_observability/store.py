"""Mongo persistence for workflow observability spans."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from workflow_observability.config import (
    SPANS_COLLECTION,
    TOKEN_COLLECTION,
    TRACES_COLLECTION,
    mongo_db_name,
    mongo_uri,
)
from workflow_observability.writer import AsyncSpanWriter

logger = logging.getLogger("workflow_observability.store")

_store: Optional["SpanStore"] = None
_store_lock = threading.Lock()


class SpanStore:
    def __init__(self):
        self._client = None
        self._db = None
        self._writer: Optional[AsyncSpanWriter] = None
        self._indexes_ready = False

    def _connect(self):
        if self._client is not None:
            return
        try:
            from pymongo import MongoClient
        except Exception as exc:
            logger.warning("pymongo unavailable: %s", exc)
            return
        try:
            self._client = MongoClient(mongo_uri(), serverSelectionTimeoutMS=3000)
            self._db = self._client[mongo_db_name()]
            self._ensure_indexes()
            self._writer = AsyncSpanWriter(self._upsert_sync)
        except Exception as exc:
            logger.warning("workflow obs mongo connect failed: %s", exc)
            self._client = None
            self._db = None

    def _ensure_indexes(self) -> None:
        if self._indexes_ready or self._db is None:
            return
        spans = self._db[SPANS_COLLECTION]
        spans.create_index("span_id", unique=True)
        spans.create_index("trace_id")
        spans.create_index([("subscriberId", 1), ("started_at", -1)])
        spans.create_index("kind")
        spans.create_index("workflow_id")
        spans.create_index("userId")
        traces = self._db[TRACES_COLLECTION]
        traces.create_index("trace_id", unique=True)
        traces.create_index([("workflow_id", 1), ("started_at", -1)])
        traces.create_index("subscriberId")
        traces.create_index("userId")
        try:
            self._db[TOKEN_COLLECTION].create_index("execution_id")
        except Exception:
            pass
        self._indexes_ready = True

    def upsert_trace(self, trace_doc: dict) -> None:
        self._connect()
        if self._writer is None:
            return
        self._writer.enqueue({"_collection": TRACES_COLLECTION, **trace_doc})

    def _upsert_sync(self, doc: dict) -> None:
        if self._db is None:
            return
        collection = doc.pop("_collection", SPANS_COLLECTION)
        if collection == TRACES_COLLECTION:
            trace_id = doc.get("trace_id")
            if not trace_id:
                return
            try:
                started_at = doc.pop("started_at", None)
                update: Dict[str, Any] = {"$set": doc}
                if started_at:
                    update["$setOnInsert"] = {"started_at": started_at}
                self._db[TRACES_COLLECTION].update_one(
                    {"trace_id": trace_id},
                    update,
                    upsert=True,
                )
            except Exception as exc:
                logger.warning("trace upsert failed: %s", exc)
            return
        if collection == TOKEN_COLLECTION:
            try:
                self._db[TOKEN_COLLECTION].insert_one(doc)
            except Exception as exc:
                logger.warning("llm_token_count insert failed: %s", exc)
            return
        span_id = doc.get("span_id")
        if not span_id:
            return
        # Preserve original started_at across running → success/error updates.
        started_at = doc.pop("started_at", None)
        update: Dict[str, Any] = {"$set": doc}
        if started_at:
            update["$setOnInsert"] = {"started_at": started_at}
        self._db[SPANS_COLLECTION].update_one({"span_id": span_id}, update, upsert=True)

    def write_span(self, doc: dict) -> None:
        self._connect()
        if self._writer is None:
            return
        self._writer.enqueue(doc)
        if str(doc.get("kind") or "") in {"llm_tool", "llm_ui"}:
            self._enqueue_token_dual_write(doc)

    def _enqueue_token_dual_write(self, span_doc: dict) -> None:
        if self._writer is None:
            return
        usage = span_doc.get("token_usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        token_doc = {
            "_collection": TOKEN_COLLECTION,
            "process": "ai-screen-builder",
            "operation": "workflow-execution",
            "sub_operation": span_doc.get("kind") or "llm",
            "execution_id": span_doc.get("trace_id"),
            "workflow_id": span_doc.get("workflow_id"),
            "node_name": span_doc.get("node_name"),
            "model": span_doc.get("model"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "status": span_doc.get("status") or "success",
            "timestamp": span_doc.get("ended_at") or span_doc.get("started_at"),
            "metadata": {
                "kind": span_doc.get("kind"),
                "span_id": span_doc.get("span_id"),
                "provider": span_doc.get("provider"),
                "graph_type": span_doc.get("graph_type"),
            },
        }
        self._writer.enqueue(token_doc)

    def flush(self, *, timeout: float = 5.0) -> None:
        """Drain the background writer — required before a sandbox process exits."""
        if self._writer is None:
            return
        try:
            self._writer.close(timeout=timeout)
        except Exception:
            pass
        self._writer = AsyncSpanWriter(self._upsert_sync)

    def get_trace_spans(self, trace_id: str) -> list[dict]:
        self._connect()
        if self._db is None:
            return []
        cursor = self._db[SPANS_COLLECTION].find({"trace_id": trace_id}).sort("started_at", 1)
        return list(cursor)

    def find_instances(self, query: dict, *, limit: int = 25) -> List[dict]:
        """Read LangGraph workflow instances (shared Mongo DB) for UI correlation."""
        self._connect()
        if self._db is None:
            return []
        try:
            cursor = self._db["instances"].find(query).sort("updated_at", -1).limit(limit)
            return list(cursor)
        except Exception as exc:
            logger.debug("instances lookup failed: %s", exc)
            return []

    def aggregate(self, pipeline: list[dict]) -> list[dict]:
        self._connect()
        if self._db is None:
            return []
        return list(self._db[SPANS_COLLECTION].aggregate(pipeline))


def get_span_store() -> SpanStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SpanStore()
    return _store


def set_span_store_for_tests(store: Any) -> None:
    """Replace the process-wide store. Tests only."""
    global _store
    with _store_lock:
        _store = store


def reset_span_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
