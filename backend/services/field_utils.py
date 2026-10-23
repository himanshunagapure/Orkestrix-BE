"""Field-name resolution for dynamic backend functions (existing MongoDB collections)."""
from __future__ import annotations

import logging
import re
from typing import Optional

from services.database import Database

logger = logging.getLogger(__name__)

# Cache per (collection, logical_name) within a worker process to avoid repeated find_one.
_field_cache: dict[tuple[str, str], str] = {}


def to_snake_case(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower()
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


async def resolve_field_name(collection_name: str, logical_name: str) -> str:
    """Map a logical/param field name to the actual MongoDB field name.

    Uses one sample document per collection (cached). Empty collection → snake_case fallback.
    Safe for /dynamic/execute sandbox: uses ``Database.get_collection`` only (no Database()).
    """
    cache_key = (collection_name, logical_name)
    if cache_key in _field_cache:
        return _field_cache[cache_key]

    resolved = to_snake_case(logical_name)
    try:
        coll = Database.get_collection(collection_name)
        sample = await coll.find_one({}, sort=[("_id", -1)])
        if not sample:
            _field_cache[cache_key] = resolved
            return resolved

        actual_fields = list(sample.keys())
        if logical_name in actual_fields:
            resolved = logical_name
        else:
            lower_map = {k.lower(): k for k in actual_fields}
            if logical_name.lower() in lower_map:
                resolved = lower_map[logical_name.lower()]
            else:

                def _normalize(s: str) -> str:
                    return s.lower().replace("_", "").replace("-", "").replace(" ", "")

                normalized_map = {_normalize(k): k for k in actual_fields}
                normalized_input = _normalize(logical_name)
                if normalized_input in normalized_map:
                    resolved = normalized_map[normalized_input]
                else:
                    snake_version = to_snake_case(logical_name)
                    if snake_version in actual_fields:
                        resolved = snake_version
                    elif snake_version.lower() in lower_map:
                        resolved = lower_map[snake_version.lower()]
                    else:
                        resolved = snake_version
    except Exception as exc:
        logger.warning(
            "Field resolution failed for %r in %r: %s",
            logical_name,
            collection_name,
            exc,
        )
        resolved = to_snake_case(logical_name)

    _field_cache[cache_key] = resolved
    return resolved


def clear_field_cache() -> None:
    """Clear resolution cache (tests or between executions if needed)."""
    _field_cache.clear()
