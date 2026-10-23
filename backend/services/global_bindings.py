"""Issue 8 — namespaced prior-phase global bindings.

Prior-phase outputs used to be keyed by bare field name (`status`, `count`).
When two nodes emitted the same field, the planner dropped the second and the
runtime bridge last-writer-won in `global_inputs`. Canonical refs are:

    global.<sourceNodeId>.<field>     prior-phase node output
    global.<field>                    system/config globals, or unique-field alias

Keep this module importable from planner + agent_new. Generated FREEZE graphs
inline a copy of ``lookup_global_binding_value`` (they do not import backend).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

SYSTEM_GLOBAL_SOURCES = frozenset({"system_config", "system", "global", ""})

_TEMPLATE_INNER_RE = re.compile(r"\{\{([^}]+)\}\}")
_NODE_ID_STATE_KEY_RE = re.compile(r"[^a-zA-Z0-9_]")


def canonical_state_node_id(node_id: str) -> str:
    """Match codegen: non-word chars in nodeIds become '_' in node_outputs keys."""
    return _NODE_ID_STATE_KEY_RE.sub("_", str(node_id or ""))


def prior_phase_global_name(source_node_id: str, field_name: str) -> str:
    """Canonical GlobalVar.name / boundTo suffix for a prior-phase field."""
    field = (field_name or "").strip()
    source = (source_node_id or "").strip()
    if not field:
        return ""
    if "." in field:
        return field
    if not source or source in SYSTEM_GLOBAL_SOURCES:
        return field
    return f"{source}.{field}"


def global_field_basename(name: str) -> str:
    name = (name or "").strip()
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return name


def iter_template_binding_refs(template: str) -> List[Tuple[str, str]]:
    """Yield ``(source, param)`` from ``{{...}}`` templates.

    - ``{{global.nodeId.field}}`` → ``("global", "nodeId.field")``
    - ``{{global.field}}`` → ``("global", "field")``
    - ``{{nodeId.param}}`` → ``("nodeId", "param")`` (param may contain extra dots)
    - ``pick:`` inners are skipped
    """
    refs: List[Tuple[str, str]] = []
    for inner in _TEMPLATE_INNER_RE.findall(template or ""):
        inner = (inner or "").strip()
        if not inner or inner.startswith("pick:"):
            continue
        if inner.startswith("global."):
            rest = inner[len("global.") :].strip()
            if rest:
                refs.append(("global", rest))
            continue
        if "." in inner:
            src, prm = inner.split(".", 1)
            src, prm = src.strip(), prm.strip()
            if src and prm:
                refs.append((src, prm))
    return refs


def lookup_node_output_field(node_outputs: Optional[Dict[str, Any]], node_id: str, field: str) -> Any:
    no = node_outputs or {}
    candidates = [node_id]
    canon = canonical_state_node_id(node_id)
    if canon and canon not in candidates:
        candidates.append(canon)
    for nid in candidates:
        node_out = no.get(nid)
        if isinstance(node_out, dict):
            if field in node_out:
                return node_out.get(field)
            continue
        if isinstance(node_out, str):
            try:
                parsed = json.loads(node_out)
            except Exception:
                continue
            if isinstance(parsed, dict) and field in parsed:
                return parsed.get(field)
    return None


def lookup_global_binding_value(
    global_inputs: Optional[Dict[str, Any]],
    node_outputs: Optional[Dict[str, Any]],
    key: str,
) -> Any:
    """Resolve a ``global.*`` key without collapsing colliding bare names.

    Namespaced keys (``nodeId.field``) look up that node first, then the exact
    namespaced slot in ``global_inputs``. They do **not** fall back to the
    flattened last-wins ``global_inputs[field]``.
    Bare keys use ``global_inputs[key]`` only (legacy ``{{global.status}}``).
    """
    gi = global_inputs or {}
    key = (key or "").strip()
    if not key:
        return None
    if "." in key:
        nid, field = key.split(".", 1)
        val = lookup_node_output_field(node_outputs, nid, field)
        if val is not None:
            return val
        if key in gi:
            return gi.get(key)
        return None
    if key in gi:
        return gi.get(key)
    return None


def promote_node_outputs_to_global_inputs(
    node_outputs: Optional[Dict[str, Any]],
    existing_global_inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bridge helper: keep last-wins flatten **and** namespaced ``nodeId.field`` keys."""
    promoted: Dict[str, Any] = dict(existing_global_inputs or {})
    for nid, nout in (node_outputs or {}).items():
        if not isinstance(nout, dict):
            continue
        promoted.update(nout)
        nid_s = str(nid)
        for k, v in nout.items():
            if k is None:
                continue
            promoted[f"{nid_s}.{k}"] = v
    return promoted


def unique_bare_aliases(names: Iterable[str]) -> Dict[str, str]:
    """Map unique basename → canonical namespaced name (for validation aliases)."""
    items = [n for n in names if n]
    counts: Dict[str, int] = {}
    for n in items:
        base = global_field_basename(n)
        counts[base] = counts.get(base, 0) + 1
    aliases: Dict[str, str] = {}
    for n in items:
        base = global_field_basename(n)
        if counts.get(base) == 1 and n != base:
            aliases[base] = n
    return aliases
