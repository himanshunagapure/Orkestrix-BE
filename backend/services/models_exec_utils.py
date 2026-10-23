"""Utilities for exec'ing stored Pydantic models_source on lazy function reload."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Set, Type

from pydantic import BaseModel

try:
    from pydantic import RootModel
except ImportError:
    RootModel = BaseModel  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

_MODELS_EXEC_PREAMBLE_TYPES = frozenset(
    {"BaseModel", "RootModel", "Optional", "Dict", "List", "Any", "Union", "Tuple"}
)


def prepare_models_exec_source(cleaned_source: str) -> str:
    """
    Make stored models_source safe to exec on lazy reload.

    Older generated sources could list parent models before nested item models
    (e.g. SubmitAssessmentRequest before MCQQuestion), which raises NameError
    during class body evaluation. Future annotations plus topological ordering
    fixes that without changing validation semantics.
    """
    if not cleaned_source or not cleaned_source.strip():
        return cleaned_source

    source = cleaned_source
    if "from __future__ import annotations" not in source:
        source = "from __future__ import annotations\n" + source

    class_pattern = re.compile(
        r"^class\s+([A-Z][a-zA-Z0-9_]*)\s*\([^)]*\):",
        re.MULTILINE,
    )
    matches = list(class_pattern.finditer(source))
    if len(matches) <= 1:
        return source

    first_class_start = matches[0].start()
    preamble = source[:first_class_start].rstrip()

    blocks: Dict[str, str] = {}
    class_names: List[str] = []
    for idx, match in enumerate(matches):
        name = match.group(1)
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(source)
        blocks[name] = source[start:end].rstrip()
        class_names.append(name)

    ref_pattern = re.compile(r"\b([A-Z][a-zA-Z0-9_]*)\b")
    known = set(class_names)
    deps: Dict[str, Set[str]] = {name: set() for name in class_names}
    for name, block in blocks.items():
        for ref in ref_pattern.findall(block):
            if ref == name or ref in _MODELS_EXEC_PREAMBLE_TYPES:
                continue
            if ref in known:
                deps[name].add(ref)

    ordered: List[str] = []
    visited: Set[str] = set()
    temp_mark: Set[str] = set()

    def visit(node: str) -> None:
        if node in temp_mark or node in visited:
            return
        temp_mark.add(node)
        for dep in deps.get(node, ()):
            visit(dep)
        temp_mark.remove(node)
        visited.add(node)
        ordered.append(node)

    for name in class_names:
        visit(name)

    body = "\n\n".join(blocks[name] for name in ordered)
    if preamble:
        return f"{preamble}\n\n{body}"
    return body


def missing_referenced_model_names(models_source: str) -> Set[str]:
    """Return model names referenced in annotations but not defined as classes."""
    class_pattern = re.compile(
        r"^class\s+([A-Z][a-zA-Z0-9_]*)\s*\([^)]*\):",
        re.MULTILINE,
    )
    defined = set(class_pattern.findall(models_source or ""))
    if not defined:
        return set()

    ref_pattern = re.compile(r"\b([A-Z][a-zA-Z0-9_]*)\b")
    skip = defined | _MODELS_EXEC_PREAMBLE_TYPES
    referenced = {name for name in ref_pattern.findall(models_source or "") if name not in skip}
    return referenced - defined


def rebuild_compiled_models(local_vars: Dict[str, Any]) -> Dict[str, Type[BaseModel]]:
    """Extract BaseModel subclasses from exec locals and resolve forward refs."""
    compiled: Dict[str, Type[BaseModel]] = {}
    candidates: List[Type[BaseModel]] = []

    for name, obj in local_vars.items():
        if not isinstance(obj, type):
            continue
        if not issubclass(obj, BaseModel):
            continue
        if obj is BaseModel:
            continue
        if obj is RootModel:
            continue
        candidates.append(obj)
        compiled[name] = obj

    for model_cls in candidates:
        rebuild = getattr(model_cls, "model_rebuild", None)
        if callable(rebuild):
            try:
                rebuild(_types_namespace=local_vars)
            except Exception as rebuild_error:
                logger.debug(
                    "model_rebuild failed for %s: %s",
                    getattr(model_cls, "__name__", model_cls),
                    rebuild_error,
                )

    return compiled
