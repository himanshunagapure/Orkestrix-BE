#!/usr/bin/env python3
"""
Test the same design-resolution path used by ai_processor.

Uses:
  build_design_engine_query → generate_design_system_dict → map_design_system_to_theme_preferences
  (same as resolve_theme_preferences, but never reads/writes the project cache)

Usage:
  cd /home/user/Projects/screen-builder
  PYTHONPATH=backend venv/bin/python tests_scripts/test_design_engine_style.py "hospital patient intake form"
  PYTHONPATH=backend venv/bin/python tests_scripts/test_design_engine_style.py
  PYTHONPATH=backend venv/bin/python tests_scripts/test_design_engine_style.py --compare-enhanced "recipe page"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Allow running from repo root without installing the package.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.design_reference.theme_resolver import (
    build_design_engine_query,
    generate_design_system_dict,
    map_design_system_to_theme_preferences,
)


def preview_design_for_prompt(
    user_prompt: str,
    *,
    enhanced_prompt: str = "",
    project_name: str = "StyleProbe",
) -> Dict[str, Any]:
    """
    Return what style/direction the current pipeline would pick for *user_prompt*.

    Mirrors production resolve_theme_preferences (engine path), without cache I/O.
    """
    query = build_design_engine_query(user_prompt, enhanced_prompt)
    design_system = generate_design_system_dict(query, project_name=project_name)
    prefs = map_design_system_to_theme_preferences(design_system)

    style = design_system.get("style") or {}
    return {
        "user_prompt": user_prompt,
        "engine_query": query,
        "productCategory": prefs.get("productCategory") or design_system.get("category"),
        "direction": prefs.get("direction"),
        "style_raw": style.get("name") if isinstance(style, dict) else style,
        "pattern": prefs.get("pattern"),
        "primary": prefs.get("primary"),
        "secondary": prefs.get("secondary"),
        "accent": prefs.get("accent"),
        "background": prefs.get("background"),
        "text": prefs.get("text"),
        "typography": prefs.get("typography"),
        "effects": prefs.get("effects"),
        "antiPatterns": prefs.get("antiPatterns"),
        "usedGenericReasoningFallback": prefs.get("usedGenericReasoningFallback"),
        "source": prefs.get("source"),
    }


def _print_result(result: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return

    print("=" * 72)
    print(f"User prompt:  {result['user_prompt']}")
    print(f"Engine query: {result['engine_query']}")
    print("-" * 72)
    print(f"Category:     {result.get('productCategory')}")
    print(f"Direction:    {result.get('direction')}")
    print(f"Style (raw):  {result.get('style_raw')}")
    print(f"Pattern:      {result.get('pattern')}")
    print(
        f"Colors:       primary={result.get('primary')} "
        f"secondary={result.get('secondary')} accent={result.get('accent')}"
    )
    print(f"              bg={result.get('background')} text={result.get('text')}")
    typo = result.get("typography") or {}
    print(f"Typography:   display={typo.get('display')} body={typo.get('body')}")
    print(f"AntiPatterns: {result.get('antiPatterns')}")
    print(f"Generic FB:   {result.get('usedGenericReasoningFallback')}")
    print("=" * 72)


_DEFAULT_PROMPTS = [
    "Collect recipe information and generate a recipe page.",
    "Collect personal information and generate a beautiful portfolio website preview.",
    "hospital patient intake form and appointment scheduler",
]

# Boilerplate that used to pollute BM25 when the full enhanced prompt was the query.
_FAKE_ENHANCED_TAIL = (
    " Prefer a clear visual direction suited to the product domain. "
    "Create a Mobile-first and fully responsive design. "
    "Ensure accessibility and professional layout."
)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview design style/direction for a user prompt (production path)."
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="User prompt text. If omitted, runs three built-in sample prompts.",
    )
    parser.add_argument(
        "--enhanced",
        default="",
        help="Optional enhanced-prompt text (only used if user prompt is too short).",
    )
    parser.add_argument(
        "--compare-enhanced",
        action="store_true",
        help="Also show what happens if the old path fed Mobile-first enhanced text as the query.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON instead of a summary.",
    )
    args = parser.parse_args(argv)

    prompts = [" ".join(args.prompt).strip()] if args.prompt else list(_DEFAULT_PROMPTS)
    prompts = [p for p in prompts if p]
    if not prompts:
        parser.error("Provide a prompt or run with no args for samples.")

    for prompt in prompts:
        result = preview_design_for_prompt(
            prompt,
            enhanced_prompt=args.enhanced,
        )
        _print_result(result, as_json=args.json)

        if args.compare_enhanced:
            # Old buggy behavior: feed enhanced text (with Mobile-first) as the engine query.
            from services.design_reference.theme_resolver import (
                generate_design_system_dict as _gen,
                map_design_system_to_theme_preferences as _map,
            )

            old_query = (prompt + _FAKE_ENHANCED_TAIL).strip()
            old_ds = _gen(old_query, project_name="OldPath")
            # Bypass enforce? No — generate_design_system_dict now enforces.
            # Show raw BM25-only by calling generator.generate directly:
            from services.design_reference.theme_resolver import (
                _load_generator_class,
                _ensure_engine_on_path,
            )

            _ensure_engine_on_path()
            Generator = _load_generator_class()
            raw = Generator().generate(old_query, project_name="OldPathRaw")
            print("OLD PATH (raw BM25 on enhanced+Mobile-first, no priority enforce):")
            print(f"  Category:  {raw.get('category')}")
            print(f"  Style:     {(raw.get('style') or {}).get('name')}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
