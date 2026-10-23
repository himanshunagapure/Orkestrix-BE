"""Background async writer for span documents."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger("workflow_observability.writer")


class AsyncSpanWriter:
    """Enqueue span writes on a background thread so the hot path stays fast."""

    def __init__(self, sink: Callable[[dict], None], *, max_queue: int = 10000):
        self._sink = sink
        self._queue: queue.Queue[Optional[dict]] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="workflow-obs-writer", daemon=True)
        self._thread.start()
        self.errors: list[str] = []

    def enqueue(self, doc: dict) -> None:
        try:
            self._queue.put_nowait(doc)
        except queue.Full:
            msg = "workflow obs span queue full; dropping span"
            logger.warning(msg)
            self.errors.append(msg)

    def close(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if item is None:
                break
            try:
                self._sink(item)
            except Exception as exc:
                msg = f"span write failed: {exc}"
                logger.warning(msg)
                self.errors.append(msg)
