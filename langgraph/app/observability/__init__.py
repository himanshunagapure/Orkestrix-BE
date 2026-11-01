"""LangGraph worker shim — re-exports workflow_observability for executor imports."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflow_observability import (  # noqa: E402
    ObservabilitySession,
    get_session,
    maybe_session_for_instance,
    reset_session,
    set_session,
)

__all__ = [
    "ObservabilitySession",
    "get_session",
    "maybe_session_for_instance",
    "reset_session",
    "set_session",
]
