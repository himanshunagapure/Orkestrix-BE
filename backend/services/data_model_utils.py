"""
Shared IR dataModels helpers.

Keeps format-1 normalization free of heavy AIProcessor / BackendGenerator imports
so both generators can share one contract without circular or slow imports.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Allowed primitive types for dataModels field specs (format 1).
_DATA_MODEL_FIELD_TYPES = frozenset({
    "string", "int", "float", "bool", "datetime",
    "dict", "list", "object", "number", "any",
})

_DATA_MODEL_TYPE_ALIASES: Dict[str, str] = {
    "integer": "int",
    "boolean": "bool",
    "text": "string",
    "email": "string",
    "password": "string",
    "url": "string",
    "phone": "string",
    "file": "string",
    "date": "datetime",
    "array": "list",
    "map": "dict",
}


def _normalize_data_model_type(raw_type: Any) -> str:
    """Map a raw type string to a canonical dataModel type."""
    if not isinstance(raw_type, str) or not raw_type.strip():
        return "string"
    t = raw_type.strip().lower()
    if t in _DATA_MODEL_FIELD_TYPES:
        return t
    return _DATA_MODEL_TYPE_ALIASES.get(t, "string")


def _normalize_items_spec(items: Any) -> Dict[str, Any]:
    """
    Normalize a list field/root `items` spec.

    Preserves `model` (item model reference) even when `type` is absent.
    Bare strings that are primitives become {type}; other bare strings are
    treated as model names ({model}) so PascalCase refs survive normalize.
    """
    if isinstance(items, str):
        raw = items.strip()
        lower = raw.lower()
        if lower in _DATA_MODEL_FIELD_TYPES or lower in _DATA_MODEL_TYPE_ALIASES:
            return {"type": _normalize_data_model_type(raw)}
        if raw:
            return {"model": raw}
        return {"type": "object"}

    if isinstance(items, dict):
        out = dict(items)
        if "type" in out:
            out["type"] = _normalize_data_model_type(out.get("type"))
        # Keep `model` as-is when present (even without type).
        return out

    return {"type": "object"}


def normalize_data_models_to_format1(
    data_models: Optional[Dict[str, Any]],
    *,
    legacy_required_default: bool = False,
) -> Dict[str, Any]:
    """
    Normalize IR dataModels to format 1 only: field → { type, required }.

    Legacy bare strings (\"field\": \"string\") are converted to
    { \"type\": \"string\", \"required\": legacy_required_default }.

    Default for legacy conversion is required=False to preserve historical
    Optional behavior from _compile_pydantic_models' old string branch.

    List-root models ({ \"type\": \"list\", \"items\": ... }) and \"empty\"
    models are left structurally intact (items types / model refs normalized).
    """
    if not isinstance(data_models, dict):
        return {}

    normalized: Dict[str, Any] = {}
    for model_name, model_def in data_models.items():
        if model_def == "empty" or model_def is None:
            normalized[model_name] = "empty"
            continue

        if not isinstance(model_def, dict):
            logger.warning(
                "normalize_data_models_to_format1: skipping model %r with non-dict def %r",
                model_name,
                type(model_def).__name__,
            )
            continue

        # List-root model: { "type": "list", "items": ... }
        if model_def.get("type") == "list" and "items" in model_def:
            normalized[model_name] = {
                "type": "list",
                "items": _normalize_items_spec(model_def.get("items")),
            }
            continue

        new_fields: Dict[str, Any] = {}
        for field_name, field_spec in model_def.items():
            if field_spec == "empty":
                continue

            if isinstance(field_spec, str):
                new_fields[field_name] = {
                    "type": _normalize_data_model_type(field_spec),
                    "required": bool(legacy_required_default),
                }
                continue

            if isinstance(field_spec, dict):
                field_out = dict(field_spec)
                raw_type = field_out.get("type", "string")
                field_out["type"] = _normalize_data_model_type(raw_type)
                if "required" not in field_out:
                    field_out["required"] = False
                else:
                    field_out["required"] = bool(field_out["required"])
                if field_out["type"] == "list" and "items" in field_out:
                    field_out["items"] = _normalize_items_spec(field_out["items"])
                new_fields[field_name] = field_out
                continue

            logger.warning(
                "normalize_data_models_to_format1: model %s field %s has unexpected "
                "spec type %s; coercing to optional string",
                model_name,
                field_name,
                type(field_spec).__name__,
            )
            new_fields[field_name] = {"type": "string", "required": False}

        normalized[model_name] = new_fields

    return normalized


def item_model_name_from_items(items: Any) -> Optional[str]:
    """Return items.model when present and non-empty."""
    if isinstance(items, str) and items.strip():
        lower = items.strip().lower()
        if lower in _DATA_MODEL_FIELD_TYPES or lower in _DATA_MODEL_TYPE_ALIASES:
            return None
        return items.strip()
    if isinstance(items, dict):
        model = items.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def collect_referenced_item_model_names(
    data_models: Dict[str, Any],
    root_model_names: List[str],
) -> List[str]:
    """
    Walk root models' list fields (and nested list-root defs) and collect
    unique `items.model` names that exist in data_models (BFS order).
    """
    if not isinstance(data_models, dict):
        return []

    ordered: List[str] = []
    seen: Set[str] = set()
    queue: List[str] = [
        n for n in root_model_names
        if isinstance(n, str) and n in data_models
    ]

    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        model_def = data_models.get(name)
        if not isinstance(model_def, dict) or model_def == "empty":
            continue

        # List-root
        if model_def.get("type") == "list":
            ref = item_model_name_from_items(model_def.get("items"))
            if ref and ref in data_models and ref not in seen:
                ordered.append(ref)
                queue.append(ref)
            continue

        for field_spec in model_def.values():
            if not isinstance(field_spec, dict):
                continue
            if field_spec.get("type") != "list":
                continue
            ref = item_model_name_from_items(field_spec.get("items"))
            if ref and ref in data_models and ref not in seen:
                ordered.append(ref)
                queue.append(ref)

    return ordered


def format_ir_model_fields_for_prompt(model_def: Any) -> Any:
    """
    LLM-friendly view of one IR model def (fields + items refs).
    Returns "empty", a list-root summary, or {field: {type, required, items?}}.
    """
    if model_def == "empty" or model_def is None:
        return "empty"
    if not isinstance(model_def, dict):
        return str(model_def)

    if model_def.get("type") == "list":
        return {
            "type": "list",
            "items": model_def.get("items"),
        }

    out: Dict[str, Any] = {}
    for field_name, field_spec in model_def.items():
        if not isinstance(field_spec, dict):
            out[field_name] = field_spec
            continue
        entry: Dict[str, Any] = {
            "type": field_spec.get("type", "string"),
            "required": bool(field_spec.get("required", False)),
        }
        if entry["type"] == "list" and "items" in field_spec:
            entry["items"] = field_spec.get("items")
        out[field_name] = entry
    return out
