"""
In-process pub/sub registry for real-time workflow status (SSE).
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

TERMINAL_EVENT_TYPES = {"completed", "error"}


class WorkflowStreamRegistry:
    """
    Fan-out pub/sub keyed by workflowId. Multiple subscribers (e.g. two
    browser tabs / two users viewing the same workflow) each get their
    own queue and all receive every published event for that workflowId.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, workflow_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers[workflow_id].add(q)
        logger.info(
            "workflow_stream subscribe workflow_id=%s subscriber_count=%d",
            workflow_id,
            len(self._subscribers[workflow_id]),
        )
        return q

    async def unsubscribe(self, workflow_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(workflow_id)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                del self._subscribers[workflow_id]
        logger.info("workflow_stream unsubscribe workflow_id=%s", workflow_id)

    async def publish(self, workflow_id: str, event: Dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(workflow_id, ()))
        if not queues:
            logger.info(
                "workflow_stream publish workflow_id=%s type=%s subscriber_count=0 (no live subscribers)",
                workflow_id,
                event.get("type"),
            )
            return
        for q in queues:
            await q.put(event)
        logger.info(
            "workflow_stream publish workflow_id=%s type=%s subscriber_count=%d",
            workflow_id,
            event.get("type"),
            len(queues),
        )


workflow_stream_registry = WorkflowStreamRegistry()