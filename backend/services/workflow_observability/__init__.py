"""Agent-side workflow observability hooks."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from workflow_observability.agent_hooks import (
    enrich_request_meta_from_pending_ui,
    observe_dynamic_function_execution,
    prepare_dynamic_function_observation,
)

__all__ = [
    "enrich_request_meta_from_pending_ui",
    "observe_dynamic_function_execution",
    "prepare_dynamic_function_observation",
]
