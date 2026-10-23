"""Environment-driven configuration for workflow observability."""

from __future__ import annotations

import os
from typing import Literal

PayloadMode = Literal["digest", "full", "off"]

SPANS_COLLECTION = "workflow_obs_spans"
TRACES_COLLECTION = "workflow_obs_traces"
TOKEN_COLLECTION = "llm_token_count"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def obs_enabled() -> bool:
    return _truthy(os.getenv("WORKFLOW_OBS_ENABLED", "false"))


def payload_mode() -> PayloadMode:
    mode = str(os.getenv("WORKFLOW_OBS_PAYLOAD_MODE", "digest")).strip().lower()
    if mode in {"digest", "full", "off"}:
        return mode  # type: ignore[return-value]
    return "digest"


def mongo_uri() -> str:
    return os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"


def mongo_db_name() -> str:
    return os.getenv("MONGO_DB_NAME") or os.getenv("MONGODB_DB_NAME") or "aiqod-dev"
