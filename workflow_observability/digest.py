"""Payload digest helpers — keep hot-path serialization small."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def digest_payload(value: Any, *, max_chars: int = 512) -> dict:
    text = _to_text(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    preview = text[:max_chars]
    return {
        "digest": digest,
        "size_bytes": len(text.encode("utf-8", errors="replace")),
        "preview": preview,
    }


def prepare_payload(value: Any, mode: str) -> Any:
    if mode == "off" or value is None:
        return None
    if mode == "full":
        if isinstance(value, (str, int, float, bool, dict, list)):
            return value
        return _to_text(value)
    return digest_payload(value)
