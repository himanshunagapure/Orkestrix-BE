"""Issue 7 (live 2026-09-18): gap-tool syntax repair helpers.

Live LinkedIn ``profile_fetching_tool`` failed all 6 attempts with empty
``try``/``except`` suites. Backend functions have a dedicated syntax fixer;
gap tools previously only regenerated. These helpers are import-safe
(no crewai / Gemini) so unit tests can cover the live error class.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import textwrap
from typing import Any, Dict, List, Optional, Tuple

# Gemini FinishReason-style syntax messages from the live log.
_SYNTAX_TOKENS = (
    "python syntax error",
    "syntaxerror",
    "indented block",
    "invalid syntax",
    "expected 'except'",
    'expected "except"',
    "expected 'finally'",
    'expected "finally"',
)

_COMPOUND_HEADER = re.compile(
    r"^(\s*)(?:async\s+)?"
    r"(?:"
    r"def\s+\w+\s*\([^)]*\)(?:\s*->\s*[^:#]+)?"
    r"|if\b[^:]+"
    r"|elif\b[^:]+"
    r"|else"
    r"|for\b[^:]+"
    r"|while\b[^:]+"
    r"|try"
    r"|except\b[^:]*"
    r"|finally"
    r"|with\b[^:]+"
    r"|class\s+\w+[^:]*"
    r")\s*:\s*(?:#.*)?$"
)

_TRY_HEADER = re.compile(r"^\s*try\s*:\s*(?:#.*)?$")
_EXCEPT_OR_FINALLY = re.compile(r"^\s*(except\b[^:]*|finally)\s*:\s*(?:#.*)?$")


def normalize_gap_function_code(function_code: str) -> str:
    return textwrap.dedent(
        (function_code or "").replace("\r\n", "\n").replace("\t", "    ")
    ).strip()


def wrap_gap_tool_function_code_for_parse(function_code: str) -> str:
    """Same indent wrap ``_validate_gap_tool_executable`` uses for ast.parse."""
    clean = normalize_gap_function_code(function_code)
    body = "\n".join(f"        {line}" for line in clean.splitlines()) or "        pass"
    return f"def _gap_tool_syntax_check():\n{body}\n"


def gap_tool_function_code_syntax_error(function_code: str) -> Optional[str]:
    """Human-readable SyntaxError for gap-tool function_code, or None if it parses."""
    if not str(function_code or "").strip():
        return "empty function_code"
    try:
        ast.parse(wrap_gap_tool_function_code_for_parse(function_code))
        return None
    except SyntaxError as exc:
        return f"Python syntax error in function_code: {exc}"


def is_gap_tool_syntax_rejection(reason: Optional[str]) -> bool:
    """True when a gap-tool validation rejection is a Python syntax problem."""
    if not reason:
        return False
    lowered = reason.lower()
    return any(tok in lowered for tok in _SYNTAX_TOKENS)


def gap_tool_model_for_attempt(
    attempt_index: int,
    last_rejection_reason: Optional[str],
    *,
    base_model: str,
    fix_model: str,
    already_escalated: bool = False,
) -> str:
    """Escalate flash → pro after a syntax rejection, and stay on pro afterwards."""
    if already_escalated or (
        attempt_index > 0 and is_gap_tool_syntax_rejection(last_rejection_reason)
    ):
        return fix_model
    return base_model


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _fill_empty_compound_suites(lines: List[str]) -> bool:
    """Insert ``raise`` into compound statements that have no indented body."""
    changed = False
    i = 0
    while i < len(lines):
        match = _COMPOUND_HEADER.match(lines[i])
        if not match:
            i += 1
            continue
        indent = len(match.group(1))
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        while j < len(lines) and lines[j].lstrip().startswith("#"):
            j += 1
            while j < len(lines) and not lines[j].strip():
                j += 1
        has_body = j < len(lines) and _indent(lines[j]) > indent
        if not has_body:
            lines.insert(i + 1, (" " * (indent + 4)) + "raise")
            changed = True
            i += 2
            continue
        i += 1
    return changed


def _close_bare_try_blocks(lines: List[str]) -> bool:
    """Add ``except Exception: raise`` after ``try`` suites that have no handler."""
    changed = False
    i = 0
    while i < len(lines):
        if not _TRY_HEADER.match(lines[i]):
            i += 1
            continue
        indent = _indent(lines[i])
        j = i + 1
        while j < len(lines):
            stripped = lines[j].lstrip()
            if not stripped or stripped.startswith("#"):
                j += 1
                continue
            if _indent(lines[j]) > indent:
                j += 1
                continue
            break
        has_handler = (
            j < len(lines)
            and _indent(lines[j]) == indent
            and _EXCEPT_OR_FINALLY.match(lines[j]) is not None
        )
        if not has_handler:
            lines[j:j] = [
                (" " * indent) + "except Exception:",
                (" " * (indent + 4)) + "raise",
            ]
            changed = True
            i = j + 2
            continue
        i += 1
    return changed


def heuristic_fix_gap_tool_syntax(function_code: str) -> Optional[str]:
    """Fill empty try/except suites. Returns repaired body or None if unchanged.

    Uses ``raise`` rather than ``pass`` so mock-code detection does not reject
    the repair. Live Issue 7 errors were empty ``except`` / missing ``except``.
    """
    original = normalize_gap_function_code(function_code)
    if not original:
        return None
    if gap_tool_function_code_syntax_error(original) is None:
        return None

    lines = original.splitlines()
    changed = False
    for _ in range(8):
        if gap_tool_function_code_syntax_error("\n".join(lines)) is None:
            break
        step = _fill_empty_compound_suites(lines) or _close_bare_try_blocks(lines)
        if not step:
            break
        changed = True

    if not changed:
        return None
    repaired = "\n".join(lines)
    # Only keep the rewrite when it actually parses. A partial/mangled
    # rewrite is worse input for the dedicated fixer than the original.
    if gap_tool_function_code_syntax_error(repaired) is None:
        return repaired
    return None


def build_gap_tool_syntax_fix_prompt(
    *,
    tool_key: str,
    function_code: str,
    imports: List[str],
    syntax_error: str,
) -> str:
    """Prompt for a dedicated syntax-only LLM patch (does not regenerate the tool)."""
    import_block = json.dumps(
        [str(x).strip() for x in (imports or []) if str(x).strip()],
        indent=2,
    )
    return f"""You are a Python fixer. A generated gap-tool function_code failed ast.parse.
Your ONLY job is to fix the syntax error. Do not regenerate the tool from scratch.
Do not change the IO contract, API calls, or return keys unless they cause the syntax error.

TOOL KEY: {tool_key}

SYNTAX ERROR (must be resolved):
{syntax_error}

FIX RULES:
- Return ONE JSON object with keys "function_code" (string) and "imports" (array of strings).
- function_code is the tool BODY only: no class, no def _run, no markdown fences.
- Every try/except/finally/if/for/while/with/else/def must have an indented body.
- Never leave `except:` or `try:` empty. On error use `raise` or return a dict that includes
  every existing output key plus "error". Do NOT use a bare `pass`.
- If a `try:` has no except/finally, add `except Exception: raise` (or a return-with-error).
- Keep existing imports unless a missing import caused the error.
- Output MUST parse with ast.parse.

CURRENT IMPORTS:
{import_block}

CURRENT function_code:
```python
{function_code}
```
"""


def apply_gap_syntax_fix(
    candidate: Dict[str, Any],
    fixer: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Overlay fixer function_code/imports onto the existing candidate."""
    if not isinstance(fixer, dict):
        return None, "syntax fixer returned no object"
    fc = str(fixer.get("function_code") or "").strip()
    if not fc:
        return None, "syntax fixer returned empty function_code"
    updated = copy.deepcopy(candidate)
    updated["function_code"] = fc
    raw_imports = fixer.get("imports")
    if isinstance(raw_imports, list) and raw_imports:
        updated["imports"] = [str(x).strip() for x in raw_imports if str(x).strip()]
    return updated, None
