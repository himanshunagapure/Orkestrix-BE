"""Executable conformance gate for generated backend functions (Issue 2).

Runs a compiled function against fake Database / HTTP adapters and asserts
declared output keys and write side-effects. Never treats generic CRUD
(recordId / collectionName / status) as equivalent to a domain contract.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type, Union, get_args, get_origin

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Keys the old generic CRUD / HTTP templates returned. Presence of *only*
# these keys is never enough when the response model declares domain fields.
_CRUD_TEMPLATE_KEYS = {
    "recordId",
    "collectionName",
    "status",
    "action",
    "count",
    "items",
    "data",
    "matched",
    "modified",
    "deleted",
    "error",
    "error_type",
    "type",
    "function",
    "help",
    "supported_actions",
    "raw",
}

# Issue 5: empty/missing required success outputs are structural — block publish.
# Write-side fixture misses stay advisory (fake adapter may not fully exercise writes).
# Issue 9: omitting seeded nested object arrays from mapped rows is structural too.
_BLOCKING_CONFORMANCE_KINDS = frozenset(
    {
        "empty_required_output",
        "missing_required_output",
        "generic_crud",
        "missing_credential",
        "select_existing",
        "invalid_return_type",
        "missing_nested_array",
    }
)

_NONTRIVIAL_LOGIC_MARKERS = (
    "gemini",
    "llm",
    "branch",
    "if existing",
    "if new",
    "jd_source",
    "extract",
    "unipile",
    "http",
    "https://",
    "score",
    "upsert",
    "custom",
    "call ",
    "paginat",
    "retry",
    "databaseLogic",
)


def is_blocking_conformance_failure(failure_kind: Optional[str]) -> bool:
    """True when a conformance failure must block publish / trigger retry (Issue 5)."""
    if not failure_kind:
        return False
    return str(failure_kind) in _BLOCKING_CONFORMANCE_KINDS


def _gate_fail(
    *,
    reason: str,
    failure_kind: str,
    result: Any = None,
    writes: Optional[List[tuple]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "failure_kind": failure_kind,
        "result": result,
        "writes": list(writes or []),
    }


def _gate_ok(
    *,
    result: Any = None,
    writes: Optional[List[tuple]] = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "reason": None,
        "failure_kind": None,
        "result": result,
        "writes": list(writes or []),
    }


def _annotation_is_optional(ann: Any) -> bool:
    origin = get_origin(ann)
    if origin is Union:
        return type(None) in get_args(ann)
    return False


def _unwrap_optional_annotation(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin is Union:
        args = [a for a in get_args(ann) if a is not type(None)]
        return args[0] if args else ann
    return ann


def _is_basemodel_type(tp: Any) -> bool:
    try:
        return isinstance(tp, type) and issubclass(tp, BaseModel)
    except TypeError:
        return False


def list_item_model_type(ann: Any) -> Optional[Type[BaseModel]]:
    """Return the item model type for ``list[SomeModel]`` / ``Optional[list[SomeModel]]``."""
    inner = _unwrap_optional_annotation(ann)
    origin = get_origin(inner)
    if origin not in (list, List):
        return None
    args = get_args(inner)
    if not args:
        return None
    item = args[0]
    if _is_basemodel_type(item):
        return item  # type: ignore[return-value]
    return None


def nested_object_array_field_names(
    response_model: Optional[Type[BaseModel]],
) -> List[str]:
    """Field names that are ``list[BaseModel]`` on row/item models (Issue 9).

    For ``FetchCandidatesResponse.candidates: list[Candidate]`` with
    ``Candidate.experience: list[ExperienceItem]``, returns ``["experience", ...]``.
    Also returns top-level ``list[Item]`` fields when the item looks like a nested
    object (name ends with ``Item`` or has few scalar fields).
    """
    if response_model is None:
        return []
    names: List[str] = []
    seen: Set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for fname, field in (response_model.model_fields or {}).items():
        item = list_item_model_type(getattr(field, "annotation", None))
        if item is None:
            continue
        nested_on_item = False
        for n2, f2 in (item.model_fields or {}).items():
            if list_item_model_type(getattr(f2, "annotation", None)) is not None:
                nested_on_item = True
                _add(n2)
        if not nested_on_item:
            # Top-level list[ExperienceItem]-style field
            item_name = getattr(item, "__name__", "") or ""
            if item_name.endswith("Item") or len(item.model_fields or {}) <= 6:
                _add(fname)
    return names


def _sample_model_as_dict(model_cls: Type[BaseModel]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, field in (model_cls.model_fields or {}).items():
        out[name] = _sample_for_annotation(getattr(field, "annotation", str))
    return out


def declared_output_keys(response_model: Optional[Type[BaseModel]]) -> List[str]:
    if response_model is None:
        return []
    fields = getattr(response_model, "model_fields", None)
    if not fields:
        return []
    return list(fields.keys())


def required_output_keys(response_model: Optional[Type[BaseModel]]) -> List[str]:
    if response_model is None:
        return []
    keys: List[str] = []
    for name, field in (response_model.model_fields or {}).items():
        is_req = True
        if hasattr(field, "is_required"):
            try:
                is_req = bool(field.is_required())
            except Exception:
                is_req = field.is_required if isinstance(field.is_required, bool) else True
        if _annotation_is_optional(getattr(field, "annotation", None)):
            is_req = False
        if getattr(field, "default", None) not in (None, ...) and not callable(
            getattr(field, "default_factory", None)
        ):
            # Has a concrete default — treat as optional for emptiness checks.
            if not is_req:
                continue
        if is_req:
            keys.append(name)
    return keys


def _sample_for_annotation(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin is Union:
        args = [a for a in get_args(ann) if a is not type(None)]
        return _sample_for_annotation(args[0]) if args else None
    if origin in (list, List):
        args = get_args(ann)
        if args and _is_basemodel_type(args[0]):
            return [_sample_model_as_dict(args[0])]  # type: ignore[arg-type]
        return []
    if origin in (dict, Dict):
        return {}
    if _is_basemodel_type(ann):
        return _sample_model_as_dict(ann)  # type: ignore[arg-type]
    if ann in (int,):
        return 1
    if ann in (float,):
        return 1.0
    if ann is bool:
        return True
    return "sample"


def sample_params_from_model(input_model: Optional[Type[BaseModel]]) -> Dict[str, Any]:
    if input_model is None:
        return {}
    params: Dict[str, Any] = {}
    for name, field in (input_model.model_fields or {}).items():
        params[name] = _sample_for_annotation(getattr(field, "annotation", str))
    return params


def _annotation_is_bool(ann: Any) -> bool:
    if ann is bool:
        return True
    origin = get_origin(ann)
    if origin is Union:
        return bool in get_args(ann)
    return False


def _existing_record_params(
    input_model: Optional[Type[BaseModel]],
) -> Optional[Dict[str, Any]]:
    """Params for the select-existing branch (`is_new=False` + identity id).

    Returns None when the input model has no create/new boolean — the Rec3
    process_jd bug (ignoring selected_jd_id) is invisible unless this branch runs.
    """
    if input_model is None:
        return None
    fields = input_model.model_fields or {}
    bool_name = None
    for name, field in fields.items():
        if _annotation_is_bool(getattr(field, "annotation", None)) and re.search(
            r"new|create", name, re.I
        ):
            bool_name = name
            break
    if not bool_name:
        return None
    params = sample_params_from_model(input_model)
    params[bool_name] = False
    identity = "existing-1"
    # Rec3 pattern: selected_jd_id + is_new. Do not treat every create/new bool
    # (e.g. create_invoice) as a select-existing branch.
    id_fields = [n for n in fields if n.lower().startswith("selected_")]
    if not id_fields:
        return None
    for name in id_fields:
        params[name] = identity
    if "candidate_count" in params:
        params["candidate_count"] = 5
    return params


def _identity_output_from_params(
    params: Dict[str, Any],
) -> Optional[Tuple[str, Any]]:
    """Map selected_jd_id='existing-1' → expected response jd_id='existing-1'."""
    for name, value in params.items():
        if not isinstance(value, str):
            continue
        if name.lower().startswith("selected_"):
            return (re.sub(r"^selected_", "", name, flags=re.I), value)
    return None


def _declared_collection(data_source: Dict[str, Any]) -> Optional[str]:
    operation = data_source.get("operation") or {}
    if not isinstance(operation, dict):
        return None
    collection = operation.get("collection")
    if isinstance(collection, str) and collection.strip():
        return collection.strip()
    reads = operation.get("readCollections") or []
    if reads and isinstance(reads[0], str) and reads[0].strip():
        return reads[0].strip()
    return None


def schema_field_names_from_datasource(data_source: Dict[str, Any]) -> List[str]:
    """Field names from outputFieldMappings / sort clauses in databaseLogic (Issue 8)."""
    names: List[str] = []
    seen: Set[str] = set()

    def _add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        text = raw.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", text):
            return
        snake = re.sub(r"[\s\-]+", "_", text)
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", snake).lower()
        snake = re.sub(r"_+", "_", snake).strip("_")
        if not snake or snake in seen:
            return
        seen.add(snake)
        names.append(snake)

    operation = data_source.get("operation") or {}
    if isinstance(operation, dict):
        for mapping_key in ("outputFieldMappings", "inputFieldMappings"):
            mapping = operation.get(mapping_key)
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                _add(key)
                if isinstance(value, str):
                    _add(value)
                elif isinstance(value, dict):
                    for nested in value.values():
                        _add(nested)

    logic = data_source.get("databaseLogic") or ""
    for match in re.finditer(
        r"""\(\s*['\"]([A-Za-z][A-Za-z0-9_]*)['\"]\s*,\s*-?\d+\s*\)""",
        logic,
    ):
        _add(match.group(1))
    for match in re.finditer(
        r"""['\"]([a-z][a-z0-9]*(?:_[a-z0-9]+)+)['\"]""",
        logic,
    ):
        _add(match.group(1))
    return names


def sort_field_names_from_logic(database_logic: str) -> List[str]:
    """Field names that appear in sort=[('field', n)] clauses (Issue 8 preference)."""
    names: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(
        r"""\(\s*['\"]([A-Za-z][A-Za-z0-9_]*)['\"]\s*,\s*-?\d+\s*\)""",
        database_logic or "",
    ):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _sample_value_for_schema_field(field_name: str) -> Any:
    """Typed placeholder for find-seed docs — avoid float('sample_score') crashes."""
    n = (field_name or "").lower()
    if n in {"_id", "id"} or n.endswith("_id"):
        return "fake_id"
    numeric_tokens = (
        "score",
        "count",
        "years",
        "amount",
        "rate",
        "distance",
        "age",
        "num",
        "total",
        "percent",
        "qty",
        "quantity",
        "price",
        "weight",
    )
    if any(tok in n for tok in numeric_tokens):
        return 1.0
    if n.endswith("_at") or "date" in n or n.endswith("_time"):
        return "2026-01-01T00:00:00"
    if n.startswith("is_") or n.startswith("has_") or n in {"active", "enabled"}:
        return True
    return f"sample_{field_name}"


def _seed_find_query_document(
    adapter: "FakeDatabaseAdapter",
    data_source: Dict[str, Any],
    params: Dict[str, Any],
    response_model: Optional[Type[BaseModel]] = None,
) -> Optional[str]:
    """Issue 8/9: seed find collections with mapping/sort/nested-array fields from the IR.

    Ensures the fake adapter's mock document includes every field named in
    ``operation.outputFieldMappings`` or sort clauses in ``databaseLogic``, plus
    nested ``list[BaseModel]`` arrays declared on the response row model.
    """
    operation = data_source.get("operation") or {}
    action = str(operation.get("action") or "").lower() if isinstance(operation, dict) else ""
    if action not in ("find", "find_one", "list", "read", "search", "aggregate"):
        return None
    collection = _declared_collection(data_source)
    if not collection:
        return None
    doc: Dict[str, Any] = {"_id": "fake_find_doc"}
    for field_name in schema_field_names_from_datasource(data_source):
        doc.setdefault(field_name, _sample_value_for_schema_field(field_name))
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", str(key)):
            doc.setdefault(str(key), value)
    # Issue 9: seed nested object arrays (experience/education) onto the find doc
    if response_model is not None:
        for fname, field in (response_model.model_fields or {}).items():
            item = list_item_model_type(getattr(field, "annotation", None))
            if item is None:
                continue
            for n2, f2 in (item.model_fields or {}).items():
                nested_item = list_item_model_type(getattr(f2, "annotation", None))
                if nested_item is None:
                    continue
                doc.setdefault(n2, [_sample_model_as_dict(nested_item)])
            # Copy scalar-ish item fields onto the seed doc so list rows can map them
            for n2, f2 in (item.model_fields or {}).items():
                if n2 in doc or n2 in {"error"}:
                    continue
                if list_item_model_type(getattr(f2, "annotation", None)) is not None:
                    continue
                doc.setdefault(
                    n2, _sample_for_annotation(getattr(f2, "annotation", str))
                )
    adapter.store[collection].append(doc)
    return collection


def missing_nested_object_arrays_in_result(
    result: Any,
    response_model: Optional[Type[BaseModel]],
) -> List[str]:
    """Issue 9: paths like ``candidates[].experience`` missing/empty after a seeded find."""
    if response_model is None or not isinstance(result, dict):
        return []
    missing: List[str] = []
    for fname, field in (response_model.model_fields or {}).items():
        item = list_item_model_type(getattr(field, "annotation", None))
        if item is None:
            continue
        nested_names = [
            n2
            for n2, f2 in (item.model_fields or {}).items()
            if list_item_model_type(getattr(f2, "annotation", None)) is not None
        ]
        if not nested_names:
            continue
        rows = result.get(fname)
        if not isinstance(rows, list) or not rows:
            continue
        row0 = rows[0] if isinstance(rows[0], dict) else None
        if not isinstance(row0, dict):
            continue
        for n2 in nested_names:
            val = row0.get(n2)
            if not isinstance(val, list) or len(val) == 0:
                missing.append(f"{fname}[].{n2}")
    return missing


def _seed_existing_document(
    adapter: "FakeDatabaseAdapter",
    data_source: Dict[str, Any],
    response_model: Optional[Type[BaseModel]],
    params: Dict[str, Any],
) -> Optional[str]:
    collection = _declared_collection(data_source)
    if not collection:
        return None
    doc: Dict[str, Any] = {"_id": "fake_existing"}
    if response_model is not None:
        for name, field in (response_model.model_fields or {}).items():
            if name == "error":
                continue
            doc[name] = _sample_for_annotation(getattr(field, "annotation", str))
    ident = _identity_output_from_params(params)
    if ident:
        out_key, ident_value = ident
        doc[out_key] = ident_value
        doc[f"selected_{out_key}"] = ident_value
    for link in data_source.get("persistenceLinks") or []:
        if not isinstance(link, dict):
            continue
        reader = link.get("reader") or {}
        key = reader.get("key") or reader.get("valueFrom")
        if key and ident:
            doc[str(key)] = ident[1]
    adapter.store[collection].append(doc)
    return collection


def _doc_matches_filters(
    doc: Dict[str, Any], filters: Optional[Dict[str, Any]]
) -> bool:
    if not filters or not isinstance(filters, dict):
        return True
    for key, expected in filters.items():
        if str(key).startswith("$"):
            continue
        if isinstance(expected, dict) and any(
            str(op).startswith("$") for op in expected
        ):
            continue
        if doc.get(key) != expected:
            return False
    return True


def _apply_fake_updates(doc: Dict[str, Any], updates: Optional[Dict[str, Any]]) -> None:
    payload = dict(updates or {})
    if not payload:
        return
    if any(str(key).startswith("$") for key in payload):
        set_fields = payload.get("$set")
        if isinstance(set_fields, dict):
            doc.update(set_fields)
        return
    doc.update(payload)


def gemini_models_in_text(text: str) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(
        r"\b(gemini-[a-z0-9]+(?:[.-][a-z0-9]+)*)\b", text or "", re.I
    ):
        model_id = match.group(1)
        key = model_id.lower()
        if key not in seen:
            seen.add(key)
            out.append(model_id)
    return out


def source_missing_database_logic_models(
    source_code: str, data_source: Dict[str, Any]
) -> Optional[str]:
    required = gemini_models_in_text(data_source.get("databaseLogic") or "")
    if not required:
        return None
    src_lower = (source_code or "").lower()
    missing = [mid for mid in required if mid.lower() not in src_lower]
    if not missing:
        return None
    return (
        "databaseLogic requires LLM model "
        + ", ".join(repr(m) for m in missing)
        + " but generated source uses a different/missing model id"
    )


def is_nontrivial_domain_datasource(data_source: Dict[str, Any]) -> bool:
    """True when generic CRUD cannot satisfy this data source."""
    operation = data_source.get("operation") or {}
    action = str(operation.get("action") or "").lower()
    if action in ("custom", "llm", "gemini"):
        return True
    logic = (data_source.get("databaseLogic") or "").strip()
    desc = (data_source.get("description") or "").strip()
    text = f"{logic}\n{desc}".lower()
    if len(logic) >= 200:
        return True
    if any(marker in text for marker in _NONTRIVIAL_LOGIC_MARKERS):
        return True
    if operation.get("writeCollections") and action not in ("insert", "update", "delete"):
        return True
    return False


def write_path_requires_insert(data_source: Optional[Dict[str, Any]]) -> bool:
    """True when new documents must be created with insert_one, not update_one.

    Database.update_one is not upsert. Rec3 process_jd used update_one on an
    empty collection and silently wrote nothing.

    Require a call-shaped ``insert_one(`` in databaseLogic/description — not the
    helper list ``insert_one / update_one / find_one`` copied from platform rules.
    Read-only actions never require insert.
    """
    if not data_source:
        return False
    operation = data_source.get("operation") or {}
    action = str(operation.get("action") or "").lower()
    if action in ("find", "read", "list", "aggregate"):
        return False
    if action in ("insert", "create"):
        return True
    text = " ".join(
        str(data_source.get(key) or "")
        for key in ("databaseLogic", "description")
    )
    return bool(re.search(r"\binsert_one\s*\(", text))


def source_missing_required_insert(
    source_code: str, data_source: Dict[str, Any]
) -> Optional[str]:
    if not write_path_requires_insert(data_source):
        return None
    if re.search(r"\binsert_one\s*\(", source_code or ""):
        return None
    return (
        "databaseLogic/operation requires Database.insert_one for new documents "
        "but generated source never calls insert_one. Database.update_one is NOT "
        "upsert — zero matched = silent no-write"
    )


def source_looks_like_generic_crud(source_code: str) -> bool:
    if not source_code:
        return False
    return (
        '"recordId"' in source_code
        and '"collectionName"' in source_code
        and ("insert_one" in source_code or "Database.insert_one" in source_code)
    )


class FakeCursor:
    def __init__(self, docs: List[Dict[str, Any]]):
        self._docs = list(docs)

    def skip(self, n: int) -> "FakeCursor":
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int) -> "FakeCursor":
        self._docs = self._docs[: int(n)]
        return self

    def sort(self, *_args: Any, **_kwargs: Any) -> "FakeCursor":
        return self

    def __aiter__(self) -> "FakeCursor":
        self._iter = iter(self._docs)
        return self

    async def __anext__(self) -> Dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        docs = list(self._docs)
        if length is not None:
            docs = docs[: int(length)]
        return docs


class FakeCollection:
    def __init__(self, name: str, adapter: "FakeDatabaseAdapter"):
        self.name = name
        self.adapter = adapter

    def find(self, filters: Any = None, *_args: Any, **_kwargs: Any) -> FakeCursor:
        query = filters if isinstance(filters, dict) else _kwargs.get("filter")
        docs = [
            dict(doc)
            for doc in (self.adapter.store.get(self.name) or [])
            if _doc_matches_filters(doc, query if isinstance(query, dict) else None)
        ]
        return FakeCursor(docs)

    def aggregate(self, *_args: Any, **_kwargs: Any) -> FakeCursor:
        return FakeCursor(list(self.adapter.store.get(self.name, [])))

    async def find_one(self, filters: Any = None, *_args: Any, **_kwargs: Any) -> Optional[Dict[str, Any]]:
        query = filters if isinstance(filters, dict) else _kwargs.get("filter")
        for doc in self.adapter.store.get(self.name) or []:
            if _doc_matches_filters(doc, query if isinstance(query, dict) else None):
                return dict(doc)
        return None

    async def insert_one(self, payload: Dict[str, Any]) -> SimpleNamespace:
        await self.adapter.insert_one(self.name, payload)
        return SimpleNamespace(inserted_id="fake_id")

    async def update_one(
        self, filters: Dict[str, Any], updates: Dict[str, Any], *_args: Any, **_kwargs: Any
    ) -> SimpleNamespace:
        result = await self.adapter.update_one(self.name, filters, updates)
        return SimpleNamespace(
            matched_count=int(result.get("matched") or 0),
            modified_count=int(result.get("modified") or 0),
        )

    async def delete_one(self, filters: Dict[str, Any]) -> SimpleNamespace:
        await self.adapter.delete_one(self.name, filters)
        return SimpleNamespace(deleted_count=1)


class FakeDatabaseAdapter:
    def __init__(self) -> None:
        self.store: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.writes: List[tuple] = []
        self.reads: List[tuple] = []
        self.updates_on_empty: List[str] = []

    def get_collection(self, collection_name: str) -> FakeCollection:
        return FakeCollection(collection_name, self)

    async def insert_one(self, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._do_insert_one(collection_name, payload)

    def insert_one_sync(
        self, collection_name: str, payload: Dict[str, Any], *_args: Any, **_kwargs: Any
    ) -> Dict[str, Any]:
        return self._do_insert_one(collection_name, payload)

    def _do_insert_one(self, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        doc = dict(payload or {})
        doc.setdefault("_id", "fake_id")
        self.store[collection_name].append(doc)
        self.writes.append(("insert", collection_name, doc))
        return {"inserted_id": "fake_id"}

    async def update_one(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        return self._do_update_one(collection_name, filters, updates)

    def update_one_sync(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        return self._do_update_one(collection_name, filters, updates)

    def _do_update_one(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.writes.append(("update", collection_name, {"filters": filters, "updates": updates}))
        docs = self.store[collection_name]
        if not docs:
            self.updates_on_empty.append(collection_name)
        for doc in docs:
            if _doc_matches_filters(doc, filters):
                _apply_fake_updates(doc, updates)
                return {"matched": 1, "modified": 1}
        return {"matched": 0, "modified": 0}

    async def delete_one(self, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        return self._do_delete_one(collection_name, filters)

    def delete_one_sync(
        self, collection_name: str, filters: Dict[str, Any], *_args: Any, **_kwargs: Any
    ) -> Dict[str, Any]:
        return self._do_delete_one(collection_name, filters)

    def _do_delete_one(self, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        self.writes.append(("delete", collection_name, filters))
        return {"deleted": 1}

    def get_sync_collection(self, collection_name: str) -> Any:
        raise AttributeError(
            "Database.get_sync_collection is not available in the async sandbox. "
            "Use await Database.upsert_one(...) or Database.upsert_one_sync(...) "
            f"(collection={collection_name!r})."
        )

    def _apply_upsert(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        docs = self.store[collection_name]
        for doc in docs:
            if _doc_matches_filters(doc, filters):
                _apply_fake_updates(doc, updates)
                self.writes.append(
                    ("upsert", collection_name, {"filters": filters, "updates": updates})
                )
                return {"matched": 1, "modified": 1, "upserted_id": None}
        doc: Dict[str, Any] = dict(filters or {})
        payload = dict(updates or {})
        if any(str(key).startswith("$") for key in payload):
            set_fields = payload.get("$set")
            if isinstance(set_fields, dict):
                doc.update(set_fields)
        else:
            doc.update(payload)
        doc.setdefault("_id", "fake_upsert_id")
        self.store[collection_name].append(doc)
        self.writes.append(("upsert", collection_name, doc))
        return {"matched": 0, "modified": 0, "upserted_id": "fake_upsert_id"}

    async def upsert_one(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        return self._apply_upsert(collection_name, filters, updates)

    def upsert_one_sync(
        self,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        return self._apply_upsert(collection_name, filters, updates)

    async def find_one(
        self,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        return self._do_find_one(collection_name, filters)

    def find_one_sync(
        self,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        return self._do_find_one(collection_name, filters)

    def _do_find_one(
        self,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        self.reads.append(("find_one", collection_name, filters))
        for doc in self.store.get(collection_name, []):
            if _doc_matches_filters(doc, filters):
                return dict(doc)
        return None

    async def find_to_list(
        self,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> List[Dict[str, Any]]:
        self.reads.append(("find_to_list", collection_name, filters))
        return [
            dict(doc)
            for doc in self.store.get(collection_name, [])
            if _doc_matches_filters(doc, filters)
        ]

    async def cursor_to_list(self, maybe_cursor: Any, length: Optional[int] = None) -> List[Dict[str, Any]]:
        if hasattr(maybe_cursor, "to_list"):
            return await maybe_cursor.to_list(length)
        docs: List[Dict[str, Any]] = []
        async for doc in maybe_cursor:
            docs.append(doc)
            if length is not None and len(docs) >= length:
                break
        return docs

    async def aggregate_to_list(
        self,
        collection_name: str,
        pipeline: List[Dict[str, Any]],
        length: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        docs = list(self.store.get(collection_name, []))
        if length is not None:
            docs = docs[: int(length)]
        return docs

    async def resolve_cursor(self, maybe_cursor_or_coro: Any) -> Any:
        if asyncio.iscoroutine(maybe_cursor_or_coro):
            return await maybe_cursor_or_coro
        return maybe_cursor_or_coro


class FakeHttpResponse:
    def __init__(self, payload: Any = None, text: Optional[str] = None):
        self._payload = payload if payload is not None else {}
        self.status_code = 200
        self.text = text if text is not None else (
            json.dumps(self._payload) if not isinstance(self._payload, str) else self._payload
        )
        self.headers = {"content-type": "application/json"}
        self.content = self.text.encode("utf-8") if isinstance(self.text, str) else b"{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        if isinstance(self._payload, (dict, list)):
            return self._payload
        try:
            import json as _json

            return _json.loads(self.text)
        except Exception:
            return {"raw": self.text}


def _gemini_fake_payload() -> Dict[str, Any]:
    """Plausible generateContent body so domain Gemini callers can finish the gate."""
    sample_jd = {
        "job_title": "Software Engineer",
        "job_category": "Engineering",
        "company_name": "Sample Co",
        "department": "R&D",
        "location": "sample",
        "work_mode": "Remote",
        "employment_type": "Full-Time",
        "seniority_level": "Mid-Level",
        "min_years_experience": 3,
        "max_years_experience": None,
        "management_experience_required": False,
        "required_skills": ["Python"],
        "preferred_skills": [],
        "tools_and_technologies": ["Git"],
        "education_level": "Bachelor's",
        "certifications": [],
        "key_responsibilities": ["Build features"],
        "domain_expertise": ["Software"],
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "benefits": [],
    }
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(sample_jd)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }


def _is_gemini_url(url: Any) -> bool:
    u = str(url or "").lower()
    return "generativelanguage.googleapis.com" in u or "generatecontent" in u


class FakeAsyncClient:
    def __init__(self, recorder: List[Dict[str, Any]], *args: Any, **kwargs: Any):
        self.recorder = recorder
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.recorder.append({"method": method, "url": url, **kwargs})
        if _is_gemini_url(url):
            return FakeHttpResponse(_gemini_fake_payload())
        return FakeHttpResponse({"ok": True, "id": "fake_external_id", "items": []})

    async def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return await self.request("DELETE", url, **kwargs)


class FakeRequestsModule:
    """Minimal requests stand-in; Gemini URLs get a structured generateContent body."""

    def __init__(self, recorder: List[Dict[str, Any]]):
        self.recorder = recorder

    def request(self, method: str, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.recorder.append({"method": method, "url": url, **kwargs})
        if _is_gemini_url(url):
            return FakeHttpResponse(_gemini_fake_payload())
        return FakeHttpResponse({"ok": True, "id": "fake_external_id", "items": []})

    def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return self.request("DELETE", url, **kwargs)


_CONFORMANCE_PATCH_LOCK = asyncio.Lock()


def _build_request_meta(
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    *,
    declared_credential_names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    creds: Dict[str, Any] = {
        "gemini_api_key": "fake-gemini-key",
        "google_api_key": "fake-gemini-key",
        "GEMINI_API_KEY": "fake-gemini-key",
        "api_key": "fake-key",
        "token": "fake-token",
        "unipile_api_key": "fake-unipile-key",
        "X-API-KEY": "fake-unipile-key",
    }
    for c in available_credentials or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("credential_name") or c.get("key")
        val = c.get("value") or c.get("secret") or "fake-credential"
        if name:
            creds[str(name)] = val
    for name in declared_credential_names or []:
        if not name:
            continue
        n = str(name).strip()
        if not n:
            continue
        creds.setdefault(n, "fake-credential")
        creds.setdefault(n.lower(), "fake-credential")
        creds.setdefault(n.upper(), "fake-credential")
    return {
        "token": "fake-token",
        "api_key": "fake-key",
        "credentials": creds,
    }


def _declared_credential_names_from_ds(data_source: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()

    def _add(raw: Any) -> None:
        if isinstance(raw, str):
            n = raw.strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)
        elif isinstance(raw, list):
            for item in raw:
                _add(item)

    cfg = (data_source or {}).get("config") or {}
    if isinstance(cfg, dict):
        _add(cfg.get("credentials"))
    logic = (data_source or {}).get("databaseLogic") or ""
    for match in re.finditer(
        r"""\[['\"]credentials['\"]\]\s*\[['\"]([^'"]+)['"]\]"""
        r"""|(?:credentials['\"]\]|\.get\(\s*['"]credentials['"]\s*\)[^\n]{0,40})\.get\(\s*['"]([^'"]+)['"]""",
        logic,
        re.I,
    ):
        _add(match.group(1) or match.group(2))
    return names


_MISSING_CREDENTIAL_ERROR = re.compile(
    r"(api[_\s-]?key|credential|token|secret).{0,40}(required|missing)"
    r"|(required|missing).{0,40}(api[_\s-]?key|credential|token|secret)"
    r"|gemini.{0,20}(required|missing|not (?:set|configured|found))",
    re.I,
)


def result_looks_like_missing_credential(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    err = result.get("error")
    if not isinstance(err, str) or not err.strip():
        return False
    return bool(_MISSING_CREDENTIAL_ERROR.search(err))


def _install_database_fakes(adapter: FakeDatabaseAdapter) -> Callable[[], None]:
    from services import database as database_mod

    cls = database_mod.Database
    originals = {
        "insert_one": cls.insert_one,
        "insert_one_sync": getattr(cls, "insert_one_sync", None),
        "update_one": cls.update_one,
        "update_one_sync": getattr(cls, "update_one_sync", None),
        "upsert_one": getattr(cls, "upsert_one", None),
        "upsert_one_sync": getattr(cls, "upsert_one_sync", None),
        "delete_one": cls.delete_one,
        "delete_one_sync": getattr(cls, "delete_one_sync", None),
        "get_collection": cls.get_collection,
        "get_sync_collection": getattr(cls, "get_sync_collection", None),
        "find_one": cls.find_one,
        "find_one_sync": getattr(cls, "find_one_sync", None),
        "find_to_list": cls.find_to_list,
        "cursor_to_list": cls.cursor_to_list,
        "aggregate_to_list": cls.aggregate_to_list,
        "resolve_cursor": cls.resolve_cursor,
    }
    # Bound adapter methods: Database.insert_one(coll, payload) → adapter.insert_one(coll, payload)
    cls.insert_one = adapter.insert_one  # type: ignore[method-assign]
    cls.insert_one_sync = adapter.insert_one_sync  # type: ignore[method-assign]
    cls.update_one = adapter.update_one  # type: ignore[method-assign]
    cls.update_one_sync = adapter.update_one_sync  # type: ignore[method-assign]
    cls.upsert_one = adapter.upsert_one  # type: ignore[method-assign]
    cls.upsert_one_sync = adapter.upsert_one_sync  # type: ignore[method-assign]
    cls.delete_one = adapter.delete_one  # type: ignore[method-assign]
    cls.delete_one_sync = adapter.delete_one_sync  # type: ignore[method-assign]
    cls.get_collection = adapter.get_collection  # type: ignore[method-assign]
    cls.get_sync_collection = adapter.get_sync_collection  # type: ignore[method-assign]
    cls.find_one = adapter.find_one  # type: ignore[method-assign]
    cls.find_one_sync = adapter.find_one_sync  # type: ignore[method-assign]
    cls.find_to_list = adapter.find_to_list  # type: ignore[method-assign]
    cls.cursor_to_list = adapter.cursor_to_list  # type: ignore[method-assign]
    cls.aggregate_to_list = adapter.aggregate_to_list  # type: ignore[method-assign]
    cls.resolve_cursor = adapter.resolve_cursor  # type: ignore[method-assign]
    # Do NOT patch Database.find — that class method does not exist in production.
    # Patching it here made conformance succeed for Database.find(...) while the
    # sandbox crashed with AttributeError.

    def restore() -> None:
        for name, orig in originals.items():
            if orig is None:
                if hasattr(cls, name):
                    delattr(cls, name)
            else:
                setattr(cls, name, orig)

    return restore


def _install_httpx_fakes(recorder: List[Dict[str, Any]]) -> Callable[[], None]:
    try:
        import httpx
    except Exception:
        return lambda: None

    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> FakeAsyncClient:
        return FakeAsyncClient(recorder, *args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc, assignment]

    def restore() -> None:
        httpx.AsyncClient = original  # type: ignore[misc]

    return restore


def _install_requests_fakes(recorder: List[Dict[str, Any]]) -> Callable[[], None]:
    import sys

    fake = FakeRequestsModule(recorder)
    original = sys.modules.get("requests")
    sys.modules["requests"] = fake  # type: ignore[assignment]

    def restore() -> None:
        if original is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = original

    return restore


def _present_keys(result: Any) -> Set[str]:
    if isinstance(result, dict):
        return set(result.keys())
    return set()


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if value == "":
        return True
    return False


def _write_side_effect_reason(
    func_id: str,
    data_source: Dict[str, Any],
    adapter: FakeDatabaseAdapter,
    *,
    declared_error: bool,
) -> Optional[str]:
    write_collections = list(
        (data_source.get("operation") or {}).get("writeCollections") or []
    )
    if not write_collections or declared_error:
        return None
    written = {w[1] for w in adapter.writes}
    missing_writes = [c for c in write_collections if c not in written]
    if missing_writes:
        return (
            f"Function '{func_id}' declared writeCollections {list(write_collections)} "
            f"but conformance fakes saw writes to {sorted(written) or 'nothing'}."
        )
    if not write_path_requires_insert(data_source):
        return None
    inserted = {w[1] for w in adapter.writes if w[0] == "insert"}
    # At least one declared write collection must be created via insert_one.
    # Do not require insert on every writeCollection — mixed insert+update is valid.
    if inserted.intersection(write_collections):
        return None
    updated = [w[1] for w in adapter.writes if w[0] == "update"]
    empty_updates = list(getattr(adapter, "updates_on_empty", []) or [])
    extra = ""
    if updated or empty_updates:
        extra = (
            f" Saw update_one on {updated or empty_updates}; "
            "Database.update_one is NOT upsert — an empty collection yields "
            "matched=0 and the document is silently not saved."
        )
    return (
        f"Function '{func_id}' declared writeCollections {list(write_collections)} "
        f"and must insert new documents via Database.insert_one, but conformance "
        f"saw inserts to {sorted(inserted) or 'nothing'}."
        + extra
        + " Use insert_one for new rows; only update_one when the filter is guaranteed to match."
    )


async def _invoke_compiled_with_fakes(
    compiled_function: Callable[..., Any],
    *,
    params: Dict[str, Any],
    request_meta: Dict[str, Any],
    adapter: FakeDatabaseAdapter,
    http_calls: List[Dict[str, Any]],
    timeout_s: float,
) -> Tuple[Any, Optional[str]]:
    async with _CONFORMANCE_PATCH_LOCK:
        restore_db = _install_database_fakes(adapter)
        restore_http = _install_httpx_fakes(http_calls)
        restore_requests = _install_requests_fakes(http_calls)
        try:
            result = await asyncio.wait_for(
                compiled_function(
                    params=params,
                    db_wrapper=None,
                    secure_http=None,
                    request_meta=request_meta,
                ),
                timeout=timeout_s,
            )
            return result, None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            restore_db()
            restore_http()
            restore_requests()


async def run_conformance_gate(
    *,
    func_id: str,
    compiled_function: Callable[..., Any],
    source_code: str,
    data_source: Dict[str, Any],
    input_model: Optional[Type[BaseModel]],
    response_model: Optional[Type[BaseModel]],
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """Execute *compiled_function* with fakes.

    Returns ``{ok, reason, failure_kind, result, writes}``.
    ``failure_kind`` drives Issue 5 publish policy via
    :func:`is_blocking_conformance_failure`.
    """
    declared = declared_output_keys(response_model)
    required = required_output_keys(response_model)
    domain_keys = [k for k in declared if k not in _CRUD_TEMPLATE_KEYS]

    if source_looks_like_generic_crud(source_code) and (
        domain_keys or is_nontrivial_domain_datasource(data_source)
    ):
        return _gate_fail(
            failure_kind="generic_crud",
            reason=(
                f"Function '{func_id}' looks like the generic CRUD template "
                f"(recordId/collectionName/status) but the data source declares "
                f"domain outputs {domain_keys or declared} / non-trivial databaseLogic. "
                "Generic CRUD fallback is prohibited."
            ),
        )

    model_mismatch = source_missing_database_logic_models(source_code, data_source)
    if model_mismatch:
        return _gate_fail(
            failure_kind="model_mismatch",
            reason=f"Function '{func_id}' {model_mismatch}.",
        )

    insert_mismatch = source_missing_required_insert(source_code, data_source)
    if insert_mismatch:
        return _gate_fail(
            failure_kind="missing_insert",
            reason=f"Function '{func_id}' {insert_mismatch}.",
        )

    request_meta = _build_request_meta(
        available_credentials,
        declared_credential_names=_declared_credential_names_from_ds(data_source),
    )

    existing_params = _existing_record_params(input_model)
    if existing_params:
        existing_adapter = FakeDatabaseAdapter()
        existing_http: List[Dict[str, Any]] = []
        seeded = _seed_existing_document(
            existing_adapter, data_source, response_model, existing_params
        )
        ident = _identity_output_from_params(existing_params)
        if seeded and ident:
            existing_result, existing_exc = await _invoke_compiled_with_fakes(
                compiled_function,
                params=existing_params,
                request_meta=request_meta,
                adapter=existing_adapter,
                http_calls=existing_http,
                timeout_s=timeout_s,
            )
            if existing_exc:
                return _gate_fail(
                    failure_kind="select_existing",
                    reason=(
                        f"Function '{func_id}' select-existing branch "
                        f"(is_new=False) raised: {existing_exc}"
                    ),
                    writes=list(existing_adapter.writes),
                )
            out_key, ident_value = ident
            got = (
                existing_result.get(out_key)
                if isinstance(existing_result, dict)
                else None
            )
            if got != ident_value:
                err = (
                    existing_result.get("error")
                    if isinstance(existing_result, dict)
                    else None
                )
                return _gate_fail(
                    failure_kind="select_existing",
                    reason=(
                        f"Function '{func_id}' select-existing branch (is_new=False) "
                        f"must return {out_key}={ident_value!r} from seeded collection "
                        f"{seeded!r} (got {got!r}, error={err!r}). "
                        "Read the inputModel identity field (e.g. selected_jd_id) and "
                        "query the IR collection; do not mint a new id or call Gemini."
                    ),
                    result=existing_result,
                    writes=list(existing_adapter.writes),
                )

    adapter = FakeDatabaseAdapter()
    http_calls: List[Dict[str, Any]] = []
    sample = sample_params_from_model(input_model)
    # Issue 8/9: find datasources seed mock docs with mapping/sort/nested-array fields.
    _seed_find_query_document(
        adapter, data_source, sample, response_model=response_model
    )
    result, invoke_exc = await _invoke_compiled_with_fakes(
        compiled_function,
        params=sample,
        request_meta=request_meta,
        adapter=adapter,
        http_calls=http_calls,
        timeout_s=timeout_s,
    )
    if invoke_exc:
        return _gate_fail(
            failure_kind="raised",
            reason=(
                f"Function '{func_id}' raised during conformance run: {invoke_exc}"
            ),
            writes=list(adapter.writes),
        )

    declared_creds = _declared_credential_names_from_ds(data_source)
    if declared_creds and result_looks_like_missing_credential(result):
        return _gate_fail(
            failure_kind="missing_credential",
            reason=(
                f"Function '{func_id}' returned a missing-credential error "
                f"{(result or {}).get('error')!r} even though request_meta['credentials'] "
                f"was injected with {declared_creds + ['gemini_api_key', 'GEMINI_API_KEY']}. "
                "Read secrets from request_meta['credentials'] using the exact declared name; "
                "NEVER os.environ.get or params.get for API keys."
            ),
            result=result,
            writes=list(adapter.writes),
        )

    if isinstance(result, list):
        # RootModel[List[...]] — list return is acceptable when that is the contract.
        write_issue = _write_side_effect_reason(
            func_id, data_source, adapter, declared_error=False
        )
        if write_issue:
            return _gate_fail(
                failure_kind="write_not_confirmed",
                reason=write_issue,
                result=result,
                writes=list(adapter.writes),
            )
        return _gate_ok(result=result, writes=list(adapter.writes))

    if not isinstance(result, dict):
        return _gate_fail(
            failure_kind="invalid_return_type",
            reason=(
                f"Function '{func_id}' must return a dict matching the response model "
                f"(got {type(result).__name__})"
            ),
            result=result,
            writes=list(adapter.writes),
        )

    present = _present_keys(result)
    # Optional response fields may be omitted; only required keys must be present.
    missing = [k for k in required if k not in present]
    if missing:
        return _gate_fail(
            failure_kind="missing_required_output",
            reason=(
                f"Function '{func_id}' return keys {sorted(present)} missing declared "
                f"required outputs {missing}. Do not return generic CRUD keys in place of the contract."
            ),
            result=result,
            writes=list(adapter.writes),
        )

    empty_required = [k for k in required if k != "error" and _is_empty(result.get(k))]
    # Allow empty required fields only when a declared error is populated (failure path).
    declared_error = "error" in declared and not _is_empty(result.get("error"))
    if empty_required and not declared_error:
        return _gate_fail(
            failure_kind="empty_required_output",
            reason=(
                f"Function '{func_id}' returned empty/null required outputs {empty_required} "
                "with no declared error. Generic/empty success is not acceptable."
            ),
            result=result,
            writes=list(adapter.writes),
        )

    # Issue 9: seeded nested object arrays (experience/education) must appear on mapped rows.
    if not declared_error:
        nested_missing = missing_nested_object_arrays_in_result(result, response_model)
        if nested_missing:
            return _gate_fail(
                failure_kind="missing_nested_array",
                reason=(
                    f"Function '{func_id}' omitted nested object-array field(s) "
                    f"{nested_missing} from the mapped response even though the "
                    "conformance fixture seeded them on the find document. "
                    "Map list[Model] fields (e.g. experience/education) into the "
                    "return dict — do not silently drop non-scalar arrays."
                ),
                result=result,
                writes=list(adapter.writes),
            )

    write_issue = _write_side_effect_reason(
        func_id, data_source, adapter, declared_error=declared_error
    )
    if write_issue:
        return _gate_fail(
            failure_kind="write_not_confirmed",
            reason=write_issue,
            result=result,
            writes=list(adapter.writes),
        )

    if domain_keys:
        returned_domain = [k for k in domain_keys if k in present]
        crud_only = present <= _CRUD_TEMPLATE_KEYS
        if crud_only and returned_domain == []:
            return _gate_fail(
                failure_kind="generic_crud",
                reason=(
                    f"Function '{func_id}' returned only generic CRUD keys {sorted(present)}; "
                    f"expected domain outputs {domain_keys}."
                ),
                result=result,
                writes=list(adapter.writes),
            )

    return _gate_ok(result=result, writes=list(adapter.writes))
