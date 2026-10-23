"""
Angular stack-level conventions sourced from the vendored ui-ux-pro-max
``data/stacks/angular.csv`` file (v2.11.0).

This module is intentionally separate from the theme pipeline — it injects
Angular-idiom guidance into the FE codegen prompt as supplementary context,
not as part of the design-direction lookup.
"""

from __future__ import annotations

import csv
import functools
import logging
from typing import List

from .theme_resolver import VENDOR_ROOT

logger = logging.getLogger(__name__)

_ANGULAR_CSV = VENDOR_ROOT / "data" / "stacks" / "angular.csv"


@functools.lru_cache(maxsize=1)
def _load_guidelines() -> List[dict]:
    """Read angular.csv once, cache forever (CSV never changes at runtime)."""
    if not _ANGULAR_CSV.is_file():
        logger.warning("Angular stack CSV not found at %s", _ANGULAR_CSV)
        return []
    rows: List[dict] = []
    with open(_ANGULAR_CSV, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    return rows


def get_angular_stack_guidelines(severity_filter: str = "High") -> str:
    """
    Return a formatted string of Angular stack guidelines for prompt injection.

    Only includes rows whose ``Severity`` matches ``severity_filter``
    (default: High). Lower-severity items are omitted to keep the prompt
    concise — they are available via the source CSV if needed later.

    The output format is deliberately terse — one line per guideline — so it
    reads as a quick reference rather than a tutorial.
    """
    rows = _load_guidelines()
    if not rows:
        return ""

    # Group by Category for readability
    by_category: dict[str, List[dict]] = {}
    for row in rows:
        if (row.get("Severity") or "").strip() != severity_filter:
            continue
        cat = (row.get("Category") or "General").strip()
        by_category.setdefault(cat, []).append(row)

    if not by_category:
        return ""

    lines: List[str] = [
        "## Angular Stack Conventions (High-severity patterns — follow these in all generated code)\n"
    ]
    for category, items in by_category.items():
        lines.append(f"\n### {category}")
        for item in items:
            guideline = (item.get("Guideline") or "").strip()
            do_text = (item.get("Do") or "").strip()
            dont_text = (item.get("Don't") or "").strip()
            if not guideline:
                continue
            parts = [f"- **{guideline}**"]
            if do_text:
                parts.append(f"  ✅ Do: {do_text}")
            if dont_text:
                parts.append(f"  ❌ Don't: {dont_text}")
            lines.append("\n".join(parts))

    return "\n".join(lines) + "\n"
