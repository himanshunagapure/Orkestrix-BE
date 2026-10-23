"""
Design-reference integration layer for ui-ux-pro-max (pinned v2.11.0).

Vendored engine + CSV data live at ``backend/design/design_reference/``
(see ``VENDORED_FROM.md``). This package is the in-process API used by
``ai_processor`` — do not shell out to search.py.
"""

from .theme_resolver import (
    resolve_theme_preferences,
    map_design_system_to_theme_preferences,
    detect_explicit_theme_signal,
    VENDOR_ROOT,
    VENDORED_TAG,
)
from .stack_guidelines import get_angular_stack_guidelines
from .design_persistence import (
    has_master,
    has_page_override,
    load_master,
    load_page_override,
    save_master,
    save_page_override,
)

__all__ = [
    # Theme resolution
    "resolve_theme_preferences",
    "map_design_system_to_theme_preferences",
    "detect_explicit_theme_signal",
    "VENDOR_ROOT",
    "VENDORED_TAG",
    # Stack guidelines
    "get_angular_stack_guidelines",
    # Design persistence (Phase 6)
    "has_master",
    "has_page_override",
    "load_master",
    "load_page_override",
    "save_master",
    "save_page_override",
]
