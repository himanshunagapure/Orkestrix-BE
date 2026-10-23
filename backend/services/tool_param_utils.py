"""Helpers for compiling tool _run signatures with optional/default parameters."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence


def coerce_required_flag(value: Any, *, default: bool = True) -> bool:
    """
    Normalize JSON/LLM ``required`` flags.

    ``bool("false")`` is True in Python, so stringy false must be handled explicitly.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"false", "0", "no", "n", "none", "null", ""}:
        return False
    if text in {"true", "1", "yes", "y"}:
        return True
    return default


def format_tool_run_param(
    name: str,
    *,
    annotation: str = "Any",
    required: bool = True,
    default_value: Any = None,
) -> str:
    """
    Format one parameter for a dynamically compiled tool ``_run`` signature.

    When ``required`` is False, the parameter always gets a default (``default_value``
    when provided, otherwise ``None``). Required parameters only get a default when
    ``default_value`` is not ``None``.
    """
    if not required:
        return f", {name}: {annotation} = {repr(default_value)}"
    if default_value is not None:
        return f", {name}: {annotation} = {repr(default_value)}"
    return f", {name}: {annotation}"


def format_tool_run_param_from_mapping(
    param: Mapping[str, Any],
    *,
    annotation: str = "Any",
) -> str:
    """Format a tool parameter dict (``name``, ``required``, ``default_value``)."""
    name = param.get("name")
    if not name:
        return ""
    return format_tool_run_param(
        str(name),
        annotation=annotation,
        required=coerce_required_flag(param.get("required"), default=True),
        default_value=param.get("default_value"),
    )


def _param_is_required(param: Any) -> bool:
    if isinstance(param, Mapping):
        return coerce_required_flag(param.get("required"), default=True)
    return coerce_required_flag(getattr(param, "required", True), default=True)


def _param_has_explicit_default(param: Any) -> bool:
    if isinstance(param, Mapping):
        return param.get("default_value") is not None
    return getattr(param, "default_value", None) is not None


def ordered_tool_params(params: Sequence[Any] | None) -> list[Any]:
    """
    Python forbids a non-default arg after a default arg.

    Emit required (no default) params first, then optional/defaulted params,
    preserving relative order within each group.
    """
    items = [p for p in (params or []) if p is not None]
    leading: list[Any] = []
    trailing: list[Any] = []
    for param in items:
        if (not _param_is_required(param)) or _param_has_explicit_default(param):
            trailing.append(param)
        else:
            leading.append(param)
    return leading + trailing


def format_tool_run_params(
    params: Iterable[Any] | None,
    *,
    annotation_for: Callable[[Any], str] | None = None,
) -> str:
    """Format a full ``_run`` parameter list, reordered to be valid Python."""
    annotation_for = annotation_for or (lambda _p: "Any")
    parts: list[str] = []
    for param in ordered_tool_params(list(params or [])):
        if isinstance(param, Mapping):
            if not param.get("name"):
                continue
            parts.append(
                format_tool_run_param_from_mapping(
                    param, annotation=annotation_for(param)
                )
            )
            continue
        name = getattr(param, "name", None)
        if not name:
            continue
        parts.append(
            format_tool_run_param(
                str(name),
                annotation=annotation_for(param),
                required=_param_is_required(param),
                default_value=getattr(param, "default_value", None),
            )
        )
    return "".join(parts)
