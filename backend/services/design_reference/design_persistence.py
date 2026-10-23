"""
Project-level design system persistence — Master + Overrides pattern.

Storage layout on disk
----------------------
{storage_root}/
└── {project_slug}/
    ├── master.json        # theme_preferences dict (primary JSON cache)
    ├── MASTER.md          # human-readable; written by the vendored engine
    └── pages/
        ├── {screen_slug}.json   # page-level override theme_preferences
        └── {screen_slug}.md     # page-level override MD (engine format)

Retrieval order in theme_resolver.resolve_theme_preferences
-----------------------------------------------------------
1. Explicit-signal short-circuit (hex / brand) — always highest priority.
2. Page-level override JSON (if screen_id provided and override exists).
3. Project MASTER JSON (if project_id provided and master exists).
4. Design engine call (generates + persists on the way out).

Force-refresh path
------------------
Triggered by workflow_context["_design_override_requested"] = True.
Re-runs the engine and writes/replaces the page override JSON for screen_id,
leaving the project MASTER untouched.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _safe_slug(name: str, fallback: str = "default") -> str:
    """Slugify *name* into a safe single filesystem path segment.

    Only [a-z0-9_-] survives; every other character collapses into '-'.
    This prevents path-traversal via crafted project or screen IDs.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name).lower()).strip("-")
    return slug or fallback


def project_dir(project_id: str, storage_root: Path) -> Path:
    return storage_root / _safe_slug(project_id)


def master_json_path(project_id: str, storage_root: Path) -> Path:
    return project_dir(project_id, storage_root) / "master.json"


def page_json_path(project_id: str, screen_id: str, storage_root: Path) -> Path:
    return project_dir(project_id, storage_root) / "pages" / f"{_safe_slug(screen_id)}.json"


# ---------------------------------------------------------------------------
# Existence checks
# ---------------------------------------------------------------------------

def has_master(project_id: str, storage_root: Path) -> bool:
    return master_json_path(project_id, storage_root).is_file()


def has_page_override(project_id: str, screen_id: str, storage_root: Path) -> bool:
    if not screen_id:
        return False
    return page_json_path(project_id, screen_id, storage_root).is_file()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_master(project_id: str, storage_root: Path) -> Optional[Dict[str, Any]]:
    """Return stored theme_preferences dict for the project, or None."""
    path = master_json_path(project_id, storage_root)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info(
                "Design system cache hit (master) project=%s direction=%s",
                project_id,
                data.get("direction"),
            )
            return data
    except Exception as exc:
        logger.warning("Could not read master design cache for %s: %s", project_id, exc)
    return None


def load_page_override(
    project_id: str, screen_id: str, storage_root: Path
) -> Optional[Dict[str, Any]]:
    """Return stored page-override theme_preferences, or None."""
    path = page_json_path(project_id, screen_id, storage_root)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info(
                "Design system cache hit (page override) project=%s screen=%s",
                project_id,
                screen_id,
            )
            return data
    except Exception as exc:
        logger.warning(
            "Could not read page design cache for %s/%s: %s", project_id, screen_id, exc
        )
    return None


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_master(
    project_id: str,
    theme_preferences: Dict[str, Any],
    design_system_dict: Dict[str, Any],
    storage_root: Path,
) -> None:
    """
    Persist project master:
    1. Write theme_preferences as master.json (primary cache read by future calls).
    2. Write MASTER.md via the engine's persist_design_system (human-readable reference).
    """
    proj_dir = project_dir(project_id, storage_root)
    proj_dir.mkdir(parents=True, exist_ok=True)
    # 1. JSON cache
    master_path = proj_dir / "master.json"
    master_path.write_text(
        json.dumps(theme_preferences, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Persisted design system master for project %s → %s", project_id, master_path)
    # 2. Human-readable MASTER.md
    _write_engine_md(design_system_dict, storage_root=storage_root)


def save_page_override(
    project_id: str,
    screen_id: str,
    theme_preferences: Dict[str, Any],
    design_system_dict: Dict[str, Any],
    storage_root: Path,
) -> None:
    """
    Persist page-level override:
    1. Write theme_preferences as {screen_slug}.json.
    2. Write page override MD via the engine.
    """
    pages_dir = project_dir(project_id, storage_root) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    # 1. JSON cache
    override_path = pages_dir / f"{_safe_slug(screen_id)}.json"
    override_path.write_text(
        json.dumps(theme_preferences, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "Persisted design system page override for %s / %s → %s",
        project_id,
        screen_id,
        override_path,
    )
    # 2. Human-readable page override MD
    _write_engine_md(design_system_dict, storage_root=storage_root, page=screen_id)


# ---------------------------------------------------------------------------
# Engine MASTER.md writer (internal)
# ---------------------------------------------------------------------------

def _write_engine_md(
    design_system_dict: Dict[str, Any],
    *,
    storage_root: Path,
    page: Optional[str] = None,
) -> None:
    """
    Call the vendored engine's persist_design_system to write MASTER.md
    (and optionally a page override MD).  Failures are logged and suppressed
    so a missing MASTER.md never breaks the generation pipeline — the JSON
    cache is the primary store.
    """
    try:
        # Ensure the engine dir is on sys.path before importing.
        from services.design_reference.theme_resolver import (  # type: ignore[attr-defined]
            _ensure_engine_on_path,
        )
        _ensure_engine_on_path()
        from design_system import persist_design_system  # type: ignore[import]

        persist_design_system(
            design_system_dict,
            page=page,
            output_dir=str(storage_root),
        )
    except Exception as exc:
        logger.warning(
            "Could not write engine MASTER.md (page=%s): %s. "
            "JSON cache is intact — pipeline continues normally.",
            page,
            exc,
        )
