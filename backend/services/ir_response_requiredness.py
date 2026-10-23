"""Issue 7: keep graph-declared outputs required on IR response models.

Screen 1's graph required jd_id / candidate_count / jd_*, but IR repair
deterministically set every response-model field to required:false so a
partial error dict would still validate. Backend codegen then compiled
Optional fields and could omit every graph-required value.

This module is the deterministic pin: error stays optional; declared
outputs that exist on a response model stay required.

Issue 6: UI action fields (user_action, submitted, …) must not live on
find/find_one (list-query) response models — they belong on a separate
submit/action datasource.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ERROR_FIELD = "error"
_WRITE_HTTP_METHODS = {"POST", "PUT", "PATCH"}
_WRITE_ACTIONS = {"insert", "update", "upsert", "create", "write"}
_READ_QUERY_ACTIONS = {
    "find",
    "find_one",
    "list",
    "read",
    "aggregate",
    "get",
    "search",
}
# Heuristic names for UI/event outputs that must not pollute query responses.
# Do NOT include bare "action" — it matches operation.action / domain fields and
# would corrupt find databaseLogic (e.g. scrubbing `action: "find"`).
_UI_ACTION_FIELDS = frozenset({
    "user_action",
    "userAction",
    "submitted",
    "confirmed",
    "proceed",
})
# Empty / placeholder values only — never scrub `action: "find"` etc.
_EMPTY_LOGIC_VALUE = r"(?:''|\"\"|null|None)"


def _param_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    return str(getattr(item, "name", "") or "").strip()


def _param_required(item: Any) -> Optional[bool]:
    if isinstance(item, dict):
        if "required" not in item:
            return None
        return bool(item.get("required"))
    if not hasattr(item, "required"):
        return None
    return bool(getattr(item, "required"))


def graph_declared_output_is_required(item: Any) -> bool:
    """True when a listed graph output must stay required.

    GenerationIOParam defaults required=False, so only an explicit True
    on declaredOutputs is treated as the graph contract. IR
    outputParameters treat a missing flag as required (listed = contract).
    """
    if not _param_name(item):
        return False
    req = _param_required(item)
    if req is False:
        return False
    if isinstance(item, dict) and "required" not in item:
        return True
    return bool(req)


def declared_output_required_names(output_parameters: Optional[Iterable[Any]]) -> Set[str]:
    names: Set[str] = set()
    for item in output_parameters or []:
        name = _param_name(item)
        if not name or name == _ERROR_FIELD:
            continue
        if graph_declared_output_is_required(item):
            names.add(name)
    return names


def response_model_names_from_ir(ir_schema: Optional[Dict[str, Any]]) -> Set[str]:
    names: Set[str] = set()
    for ds in (ir_schema or {}).get("dataSources") or []:
        if not isinstance(ds, dict):
            continue
        model = ds.get("responseModel")
        if isinstance(model, str) and model.strip():
            names.add(model.strip())
    return names


def _is_response_model(model_name: str, response_names: Set[str]) -> bool:
    return model_name in response_names or str(model_name).endswith("Response")


def _ensure_field_spec(model_def: Dict[str, Any], field_name: str, *, required: bool) -> None:
    spec = model_def.get(field_name)
    if isinstance(spec, dict):
        spec["required"] = required
        if "type" not in spec:
            spec["type"] = "string"
        return
    if isinstance(spec, str) and spec.strip():
        model_def[field_name] = {"type": spec, "required": required}
        return
    model_def[field_name] = {"type": "string", "required": required}


def ensure_response_error_field(ir_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add optional ``error`` on response models. Does not weaken other fields."""
    response_names = response_model_names_from_ir(ir_schema)
    models = ir_schema.get("dataModels")
    if not isinstance(models, dict):
        return ir_schema
    for model_name, model_def in models.items():
        if model_def == "empty" or not isinstance(model_def, dict):
            continue
        if not _is_response_model(str(model_name), response_names):
            continue
        _ensure_field_spec(model_def, _ERROR_FIELD, required=False)
    return ir_schema


def _output_field_type(outputs: Iterable[Any], field_name: str) -> str:
    for item in outputs or []:
        if _param_name(item) != field_name:
            continue
        if isinstance(item, dict):
            raw = item.get("type") or item.get("dataType") or item.get("datatype")
        else:
            raw = getattr(item, "type", None) or getattr(item, "dataType", None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "string"


def _is_write_datasource(ds: Dict[str, Any]) -> bool:
    # POST+find is common for list queries — never treat read actions as writes.
    if is_read_query_datasource(ds):
        return False
    method = str(((ds.get("config") or {}) if isinstance(ds.get("config"), dict) else {}).get("method") or "").upper()
    action = str(((ds.get("operation") or {}) if isinstance(ds.get("operation"), dict) else {}).get("action") or "").lower()
    if method in _WRITE_HTTP_METHODS:
        return True
    return action in _WRITE_ACTIONS


def is_read_query_datasource(ds: Dict[str, Any]) -> bool:
    """True for find/list-style datasources that must not carry UI action fields."""
    if not isinstance(ds, dict):
        return False
    op = ds.get("operation") if isinstance(ds.get("operation"), dict) else {}
    action = str((op or {}).get("action") or "").lower()
    if action in _READ_QUERY_ACTIONS:
        return True
    writes = (op or {}).get("writeCollections") or []
    if writes:
        return False
    # Custom/empty action with only reads and no writes → treat as query.
    reads = (op or {}).get("readCollections") or []
    if action in ("", "custom") and reads and not writes:
        return True
    return False


def is_ui_action_field_name(name: str) -> bool:
    return str(name or "").strip() in _UI_ACTION_FIELDS


def _models_used_only_by_read_queries(ir_schema: Dict[str, Any]) -> Set[str]:
    """Response model names referenced only by read/query datasources."""
    usage: Dict[str, List[bool]] = {}
    for ds in ir_schema.get("dataSources") or []:
        if not isinstance(ds, dict):
            continue
        model = ds.get("responseModel")
        if not isinstance(model, str) or not model.strip():
            continue
        name = model.strip()
        usage.setdefault(name, []).append(is_read_query_datasource(ds))
    return {name for name, flags in usage.items() if flags and all(flags)}


def _scrub_ui_action_fields_from_logic(logic: str, field_names: Set[str]) -> str:
    """Remove empty UI-action placeholders from find-datasource databaseLogic.

    Only strips assignments to empty/null placeholders (e.g. ``user_action=''``).
    Does not touch real values like ``action: "find"``.
    """
    if not logic or not field_names:
        return logic
    text = logic
    empty = _EMPTY_LOGIC_VALUE
    for name in sorted(field_names, key=len, reverse=True):
        patterns = [
            rf",\s*and\s+{re.escape(name)}\s*[:=]\s*{empty}",
            rf",\s*{re.escape(name)}\s*[:=]\s*{empty}",
            rf"\band\s+{re.escape(name)}\s*[:=]\s*{empty}",
            rf"['\"]{re.escape(name)}['\"]\s*:\s*{empty}\s*,?\s*",
            rf"{re.escape(name)}\s*[:=]\s*{empty}\s*,?\s*",
        ]
        for pat in patterns:
            text = re.sub(pat, "", text, flags=re.IGNORECASE)
    # Tidy leftover double spaces / commas before closing braces
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r"\{\s*,", "{", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def strip_ui_action_fields_from_read_datasources(
    ir_schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Issue 6: remove UI action fields from find/find_one response models.

    Keeps those fields on write/submit/action datasources. Also scrubs
    databaseLogic instructions that force empty action returns from queries.
    Returns (schema, list of repair notes).
    """
    notes: List[str] = []
    if not isinstance(ir_schema, dict):
        return ir_schema, notes

    models = ir_schema.get("dataModels")
    if not isinstance(models, dict):
        return ir_schema, notes

    read_only_models = _models_used_only_by_read_queries(ir_schema)
    if not read_only_models:
        # Still scrub logic on read DS even if model is shared (rare).
        pass

    for ds in ir_schema.get("dataSources") or []:
        if not isinstance(ds, dict) or not is_read_query_datasource(ds):
            continue
        ds_id = str(ds.get("id") or "unknown")
        model_name = ds.get("responseModel")
        removed: Set[str] = set()

        if (
            isinstance(model_name, str)
            and model_name.strip()
            and model_name.strip() in read_only_models
        ):
            model_def = models.get(model_name.strip())
            if isinstance(model_def, dict):
                for field_name in list(model_def.keys()):
                    if not is_ui_action_field_name(field_name):
                        continue
                    del model_def[field_name]
                    removed.add(field_name)

        logic = ds.get("databaseLogic")
        if isinstance(logic, str) and logic.strip():
            # Only scrub fields we removed, or known UI-action placeholders —
            # never a broad pass that could touch unrelated tokens.
            scrub_names = removed or set(_UI_ACTION_FIELDS)
            cleaned = _scrub_ui_action_fields_from_logic(logic, scrub_names)
            if cleaned != logic:
                ds["databaseLogic"] = cleaned
                if not removed:
                    notes.append(
                        f"Issue 6: scrubbed empty UI action placeholders from "
                        f"databaseLogic of read datasource '{ds_id}'"
                    )

        if removed:
            notes.append(
                f"Issue 6: removed UI action field(s) {sorted(removed)} from "
                f"read datasource '{ds_id}' responseModel '{model_name}'"
            )

    return ir_schema, notes


def read_datasource_ui_action_field_errors(ir_schema: Dict[str, Any]) -> List[str]:
    """Validator: find/find_one response models must not include UI action fields."""
    errors: List[str] = []
    models = ir_schema.get("dataModels") if isinstance(ir_schema, dict) else None
    if not isinstance(models, dict):
        return errors
    read_only_models = _models_used_only_by_read_queries(ir_schema)
    for ds in (ir_schema or {}).get("dataSources") or []:
        if not isinstance(ds, dict) or not is_read_query_datasource(ds):
            continue
        model_name = ds.get("responseModel")
        if not isinstance(model_name, str) or model_name.strip() not in read_only_models:
            continue
        model_def = models.get(model_name.strip())
        if not isinstance(model_def, dict):
            continue
        bad = sorted(k for k in model_def if is_ui_action_field_name(k))
        if bad:
            errors.append(
                f"read datasource '{ds.get('id')}' responseModel '{model_name}' "
                f"must not include UI action field(s) {bad}; those belong on a "
                f"submit/action datasource"
            )
    return errors


def _response_model_for_datasource_id(
    ir_schema: Dict[str, Any], data_source_id: str
) -> Optional[str]:
    want = str(data_source_id or "").strip()
    if not want:
        return None
    for ds in ir_schema.get("dataSources") or []:
        if not isinstance(ds, dict):
            continue
        if str(ds.get("id") or "").strip() != want:
            continue
        model = ds.get("responseModel")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def _extract_from_response_model(
    ir_schema: Dict[str, Any], field_name: str
) -> Optional[str]:
    """If outputParameters declare extractFrom for this field, return that model."""
    for param in ir_schema.get("outputParameters") or []:
        if not isinstance(param, dict) or _param_name(param) != field_name:
            continue
        ef = param.get("extractFrom")
        if not isinstance(ef, dict):
            continue
        ds_id = ef.get("dataSourceId")
        if not ds_id:
            continue
        model = _response_model_for_datasource_id(ir_schema, str(ds_id))
        if model:
            return model
    return None


def _target_models_for_missing_fields(
    ir_schema: Dict[str, Any],
    response_names: Set[str],
    *,
    field_names: Optional[Set[str]] = None,
) -> List[str]:
    """Response models that should receive graph outputs omitted by the LLM.

    Prefer ``extractFrom.dataSourceId`` when declared (call with a single field
    for that to apply). UI action fields never land on find/list query response
    models. Non-UI fields use write models when present, else all response models.
    """
    write_models: List[str] = []
    all_models: List[str] = []
    non_read_models: List[str] = []
    seen_write: Set[str] = set()
    seen_all: Set[str] = set()
    seen_non_read: Set[str] = set()
    fields = field_names or set()
    only_ui_action = bool(fields) and all(is_ui_action_field_name(f) for f in fields)

    # Single-field extractFrom wins when unambiguous.
    if len(fields) == 1:
        extracted = _extract_from_response_model(ir_schema, next(iter(fields)))
        if extracted:
            # UI action must not be forced onto a read-only extractFrom target.
            if only_ui_action:
                for ds in ir_schema.get("dataSources") or []:
                    if not isinstance(ds, dict):
                        continue
                    model = ds.get("responseModel")
                    if (
                        isinstance(model, str)
                        and model.strip() == extracted
                        and is_read_query_datasource(ds)
                    ):
                        extracted = None
                        break
            if extracted:
                return [extracted]

    for ds in ir_schema.get("dataSources") or []:
        if not isinstance(ds, dict):
            continue
        model = ds.get("responseModel")
        if not isinstance(model, str) or not model.strip():
            continue
        name = model.strip()
        if name not in seen_all:
            all_models.append(name)
            seen_all.add(name)
        if _is_write_datasource(ds) and name not in seen_write:
            write_models.append(name)
            seen_write.add(name)
        if not is_read_query_datasource(ds) and name not in seen_non_read:
            non_read_models.append(name)
            seen_non_read.add(name)

    if only_ui_action:
        # Never invent UI action fields onto a find/list response.
        return write_models or non_read_models
    if write_models:
        return write_models
    if all_models:
        return all_models
    return sorted(response_names)


def restore_declared_output_requiredness(
    ir_schema: Dict[str, Any],
    extra_required_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Pin graph-declared outputs to required:true on matching response fields.

    Convenience fields not in the output contract are left alone. ``error``
    stays optional. Also stamps outputParameters[].required=true for pinned names.
    Fields the LLM omitted from every response model are added on write
    (POST/insert) dataSources, or the first dataSource if none are writes.

    ``extra_required_names`` is the workflow IO contract (detected/planner
    outputs). It is merged in so IR repair cannot drop the graph contract by
    deleting outputParameters rows.

    After pinning, Issue 6 strips UI action fields from read/query response
    models so find datasources stay free of user_action / confirmed / etc.
    """
    if not isinstance(ir_schema, dict):
        return ir_schema

    outputs = ir_schema.get("outputParameters")
    if not isinstance(outputs, list):
        outputs = []
        ir_schema["outputParameters"] = outputs

    required_names = declared_output_required_names(outputs)
    for raw in extra_required_names or []:
        name = str(raw or "").strip()
        if name and name != _ERROR_FIELD:
            required_names.add(name)
    ensure_response_error_field(ir_schema)

    response_names = response_model_names_from_ir(ir_schema)
    models = ir_schema.get("dataModels")
    if not isinstance(models, dict):
        models = {}
        ir_schema["dataModels"] = models

    read_only_models = _models_used_only_by_read_queries(ir_schema)

    present_on_any: Set[str] = set()
    for model_name, model_def in models.items():
        if model_def == "empty" or not isinstance(model_def, dict):
            continue
        if not _is_response_model(str(model_name), response_names):
            continue
        for field_name in required_names:
            if field_name not in model_def:
                continue
            # Issue 6: do not pin UI action fields onto query-only models —
            # strip them instead of promoting required:true.
            if (
                is_ui_action_field_name(field_name)
                and str(model_name) in read_only_models
            ):
                continue
            _ensure_field_spec(model_def, field_name, required=True)
            present_on_any.add(field_name)

    missing = required_names - present_on_any
    if missing:
        # Per-field targeting so extractFrom.dataSourceId is honored (Issue 6/7).
        for field_name in sorted(missing):
            for model_name in _target_models_for_missing_fields(
                ir_schema, response_names, field_names={field_name}
            ):
                model_def = models.get(model_name)
                if model_def == "empty" or not isinstance(model_def, dict):
                    if model_name in response_names or str(model_name).endswith("Response"):
                        model_def = {}
                        models[model_name] = model_def
                    else:
                        continue
                # Final guard: never add UI action fields onto read-only models.
                if (
                    is_ui_action_field_name(field_name)
                    and model_name in read_only_models
                ):
                    continue
                _ensure_field_spec(model_def, field_name, required=True)
                spec = model_def.get(field_name)
                if isinstance(spec, dict):
                    spec["type"] = _output_field_type(outputs, field_name)

    for param in outputs:
        if not isinstance(param, dict):
            continue
        name = _param_name(param)
        if name in required_names:
            param["required"] = True

    existing = {_param_name(p) for p in outputs}
    for field_name in sorted(required_names):
        if field_name in existing:
            continue
        outputs.append({
            "name": field_name,
            "type": _output_field_type(outputs, field_name),
            "required": True,
        })
        existing.add(field_name)

    # Issue 6 durable strip after pin/add
    strip_ui_action_fields_from_read_datasources(ir_schema)

    return ir_schema


def response_model_requiredness_errors(ir_schema: Dict[str, Any]) -> List[str]:
    """Errors when a declared required output is optional/missing on a response model."""
    errors: List[str] = []
    required_names = declared_output_required_names(ir_schema.get("outputParameters") or [])
    if not required_names:
        return errors
    response_names = response_model_names_from_ir(ir_schema)
    models = ir_schema.get("dataModels")
    if not isinstance(models, dict):
        return [
            f"declaredOutputs {sorted(required_names)} have no dataModels to check"
        ]

    checked_any = False
    for model_name, model_def in models.items():
        if model_def == "empty" or not isinstance(model_def, dict):
            continue
        if not _is_response_model(str(model_name), response_names):
            continue
        checked_any = True
        for field_name in sorted(required_names):
            if field_name not in model_def:
                continue
            spec = model_def[field_name]
            required = spec.get("required") if isinstance(spec, dict) else None
            if required is False:
                errors.append(
                    f"response model '{model_name}' field '{field_name}' is "
                    f"required:false but the graph declared it as a required output"
                )
    present: Set[str] = set()
    for model_name, model_def in (models or {}).items():
        if model_def == "empty" or not isinstance(model_def, dict):
            continue
        if not _is_response_model(str(model_name), response_names):
            continue
        present.update(k for k in required_names if k in model_def)
    for field_name in sorted(required_names - present):
        # Issue 6: UI action fields may live only on submit DS or via
        # sourceComponent — missing from every response model is OK when
        # there is no non-read datasource to host them.
        if is_ui_action_field_name(field_name):
            non_read = _target_models_for_missing_fields(
                ir_schema, response_names, field_names={field_name}
            )
            if not non_read:
                continue
        errors.append(
            f"declared required output '{field_name}' is missing from every "
            f"response model"
        )
    if not checked_any and required_names:
        errors.append(
            "declared required outputs have no response model in dataModels"
        )
    errors.extend(read_datasource_ui_action_field_errors(ir_schema))
    return errors


def capability_weakens_required_declared_outputs(
    declared_outputs: Optional[Iterable[Any]],
    cap_output_parameters: Optional[Iterable[Any]],
) -> List[str]:
    """6p check: graph-required declaredOutputs must not be required:false on the capability."""
    reasons: List[str] = []
    cap_by_name: Dict[str, Any] = {}
    for item in cap_output_parameters or []:
        name = _param_name(item)
        if name:
            cap_by_name[name] = item

    for declared in declared_outputs or []:
        name = _param_name(declared)
        if not name:
            continue
        req = _param_required(declared)
        if req is not True:
            continue
        cap_item = cap_by_name.get(name)
        if cap_item is None:
            continue
        if _param_required(cap_item) is False:
            reasons.append(
                f"declaredOutput '{name}' is required on the graph but capability "
                f"outputParameters marks it required=false"
            )
    return reasons
