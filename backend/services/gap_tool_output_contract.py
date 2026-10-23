"""Issue 9: gap-tool outputParameters follow the workflow io_contract.

The graph-declared io_contract is authoritative. LLM-invented output names
must not be stored as capability metadata. Transport keys that appear on
error returns are ignored as extras, but they still count when the contract
itself declares them (e.g. a graph output named ``status``).
"""
from __future__ import annotations

import ast
import json
import textwrap
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# These keys may appear on error returns; they are not extras unless the
# workflow contract itself declares them as outputs.
GAP_TRANSPORT_OUTPUT_KEYS = frozenset(
    {"error", "code", "retryable", "ok", "message", "status"}
)

NormalizeFn = Callable[[Any], List[Dict[str, Any]]]


def gap_output_param_names(params: Any) -> List[str]:
    names: List[str] = []
    seen: set = set()
    for p in params or []:
        if not isinstance(p, dict):
            continue
        n = str(p.get("name") or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        names.append(n)
    return names


def gap_top_level_return_dict_keys(function_code: str) -> Optional[Set[str]]:
    """String keys of top-level ``return {..}`` dicts, excluding nested helpers.

    Returns None when the body is empty, unparseable, or has no dict returns
    (e.g. ``return result``), so callers can fall back to metadata only.
    """
    src = str(function_code or "").replace("\r\n", "\n").replace("\t", "    ")
    src = textwrap.dedent(src).strip()
    if not src:
        return None
    body = "\n".join(f"    {line}" for line in src.splitlines()) or "    pass"
    try:
        tree = ast.parse(f"def _run():\n{body}\n")
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    keys: Set[str] = set()
    found_dict_return = False

    def _walk(node: ast.AST) -> None:
        nonlocal found_dict_return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                found_dict_return = True
                for k in child.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            _walk(child)

    _walk(tree.body[0])
    if not found_dict_return:
        return None
    return keys


def classify_gap_output_contract(
    llm_out_params: List[Dict[str, Any]],
    contract_out_params: List[Dict[str, Any]],
    function_code: str = "",
) -> Tuple[str, List[str], List[str]]:
    """
    Compare LLM outputParameters (and return-dict keys, when visible) to the
    workflow io_contract.

    Returns (action, extra_names, missing_names):
      - ``ok`` — every contract name is present; extras are only transport keys
      - ``retry_missing_contract`` — contract is non-empty and neither metadata
        nor visible return dicts use any contract name (empty list or a full rename)
      - ``targeted_fix`` — overlap exists, but extras (non-transport) or
        missing contract names remain
    """
    contract_names = gap_output_param_names(contract_out_params)
    llm_names = gap_output_param_names(llm_out_params)
    if not contract_names:
        return "ok", [], []
    contract_set = set(contract_names)
    llm_set = set(llm_names)
    produced = set(llm_set)
    code_keys = gap_top_level_return_dict_keys(function_code) if function_code else None
    if code_keys is not None:
        produced |= code_keys
    # When return dicts are visible, they decide extras/missing (metadata is
    # pinned either way). Otherwise fall back to outputParameters.
    compare_from = code_keys if code_keys is not None else llm_set
    extra = sorted(
        n for n in compare_from - contract_set if n not in GAP_TRANSPORT_OUTPUT_KEYS
    )
    missing_list = sorted(contract_set - compare_from)
    overlap = contract_set & produced
    if not overlap:
        return "retry_missing_contract", extra, missing_list
    if extra or missing_list:
        return "targeted_fix", extra, missing_list
    return "ok", [], []


def pin_gap_tool_output_parameters(
    parsed: Dict[str, Any],
    io_contract: Dict[str, Any],
    *,
    normalize: Optional[NormalizeFn] = None,
) -> None:
    """io_contract output names/types win. LLM may only fill descriptions."""
    def _norm(raw: Any) -> List[Dict[str, Any]]:
        if normalize is not None:
            return normalize(raw or [])
        out: List[Dict[str, Any]] = []
        for p in raw or []:
            if isinstance(p, dict) and str(p.get("name") or "").strip():
                out.append(dict(p))
        return out

    llm_out_params = _norm(parsed.get("outputParameters") or [])
    contract_out_params = _norm(io_contract.get("outputParameters") or [])
    if not contract_out_params:
        parsed["outputParameters"] = llm_out_params
        return
    llm_by_name: Dict[str, Dict[str, Any]] = {}
    for p in llm_out_params:
        n = str(p.get("name") or "").strip()
        if n:
            llm_by_name[n] = p
    pinned: List[Dict[str, Any]] = []
    for p in contract_out_params:
        merged = dict(p)
        llm_match = llm_by_name.get(str(p.get("name") or "").strip())
        if llm_match:
            desc = str(llm_match.get("description") or "").strip()
            if desc:
                merged["description"] = desc
        pinned.append(merged)
    parsed["outputParameters"] = pinned


def build_gap_tool_output_contract_fix_prompt(
    *,
    tool_key: str,
    io_contract: Dict[str, Any],
    function_code: str,
    imports: List[str],
    extra_names: List[str],
    missing_names: List[str],
    normalize: Optional[NormalizeFn] = None,
) -> str:
    contract_outs = (
        normalize(io_contract.get("outputParameters") or [])
        if normalize is not None
        else list(io_contract.get("outputParameters") or [])
    )
    return f"""Rewrite a generated tool so its outputs match the AUTHORITATIVE workflow contract.

The previous generation invented or omitted output field names. That is a spec
mismatch. You must fix function_code (and imports if needed) so every return
dict uses EXACTLY the contract output names. Do not invent new output fields.

Tool name (keep unchanged): {tool_key}

AUTHORITATIVE outputParameters (the only allowed output keys besides optional
error/code/retryable on failure):
{json.dumps(contract_outs, indent=2)}

Invented extra names that MUST be removed or renamed onto a contract key:
{json.dumps(extra_names)}

Contract names that were missing and MUST appear on every success return:
{json.dumps(missing_names)}

Current imports:
{json.dumps(imports, indent=2)}

Current function_code (_run body only, no imports, no def _run):
{function_code}

Rules:
1. Return a single JSON object with keys: function_code, imports, outputParameters.
2. outputParameters MUST be exactly the authoritative list above (same names).
3. function_code is ONLY the _run body. Map invented keys onto contract names
   when they mean the same thing (e.g. total_count → fetched_candidates_count).
4. Failure returns may include "error" (and optional code/retryable) AND must
   still include every contract key with safe empty defaults.
5. Do not add input parameters. Do not change the tool's core behavior.
"""


def apply_gap_output_contract_fix(
    candidate: Dict[str, Any],
    io_contract: Dict[str, Any],
    fixed: Dict[str, Any],
    *,
    normalize: Optional[NormalizeFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Apply a targeted-fix LLM JSON object. Pins metadata to the real contract."""
    fc = str(fixed.get("function_code") or "").strip()
    if not fc:
        return None, "output-contract targeted fix returned empty function_code"
    contract_out = io_contract.get("outputParameters") or []
    if normalize is not None:
        contract_out = normalize(contract_out)
        llm_out = normalize(fixed.get("outputParameters") or [])
    else:
        llm_out = [
            p for p in (fixed.get("outputParameters") or [])
            if isinstance(p, dict)
        ]
    action, _extra_after, missing_after = classify_gap_output_contract(
        llm_out, contract_out, function_code=fc
    )
    if action == "retry_missing_contract":
        return (
            None,
            "output-contract targeted fix still missing contract output names: "
            + ", ".join(missing_after),
        )
    contract_set = set(gap_output_param_names(contract_out))
    code_keys = gap_top_level_return_dict_keys(fc)
    if code_keys is not None and contract_set:
        still_missing = sorted(contract_set - code_keys)
        if still_missing:
            return (
                None,
                "output-contract targeted fix still missing contract output names: "
                + ", ".join(still_missing),
            )
    updated = dict(candidate)
    updated["function_code"] = fc
    if isinstance(fixed.get("imports"), list) and fixed.get("imports"):
        updated["imports"] = fixed["imports"]
    pin_gap_tool_output_parameters(updated, io_contract, normalize=normalize)
    return updated, None
