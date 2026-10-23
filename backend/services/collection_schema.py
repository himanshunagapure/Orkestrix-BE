"""MongoDB collection schema inspection for code generation and capability sync."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from services.database import Database

logger = logging.getLogger(__name__)


def get_main_db_name() -> str:
    db_name = os.getenv("MONGODB_DB_NAME")
    if db_name:
        return db_name
    config_path = os.getenv("CONFIG_PATH", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        return str(cfg.get("devDb") or "saas-uat")
    except Exception:
        return "saas-uat"


def subscriber_id_from_project_id(project_id: str) -> Optional[str]:
    """Parse subscriber id from project_id (``<prefix>_sb_<subscriber_id>``)."""
    if not project_id:
        return None
    separator = None
    if "_sb_" in project_id:
        separator = "_sb_"
    elif "-sb-" in project_id:
        separator = "-sb-"
    if not separator:
        return None
    parts = project_id.rsplit(separator, 1)
    if len(parts) != 2 or not (parts[1] or "").strip():
        return None
    return (parts[1] or "").strip()


def subscriber_db_name_from_subscriber_id(subscriber_id: Optional[str]) -> Optional[str]:
    sid = (subscriber_id or "").strip()
    if not sid:
        return None
    return f"{get_main_db_name()}_sb_{sid}"


def resolve_subscriber_db_name(
    *,
    project_id: str,
    subscriber_id: Optional[str] = None,
) -> Optional[str]:
    """Subscriber DB for schema probes: explicit subscriber_id or parsed from project_id."""
    db_name = subscriber_db_name_from_subscriber_id(subscriber_id)
    if db_name:
        return db_name
    sid = subscriber_id_from_project_id(project_id)
    return subscriber_db_name_from_subscriber_id(sid)


def collections_for_data_source(data_source: Dict[str, Any]) -> List[str]:
    """Collection names referenced by one data source."""
    if not isinstance(data_source, dict):
        return []
    operation = data_source.get("operation") or {}
    if not isinstance(operation, dict):
        operation = {}
    names: List[str] = []
    seen: set[str] = set()

    def _add(val: Any) -> None:
        if isinstance(val, str):
            n = val.strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        elif isinstance(val, list):
            for item in val:
                _add(item)

    _add(operation.get("collection"))
    _add(operation.get("readCollections"))
    _add(operation.get("writeCollections"))
    return names


def collections_from_data_sources(data_sources: List[Dict[str, Any]]) -> List[str]:
    """Unique collection names across all data sources."""
    seen: set[str] = set()
    out: List[str] = []
    for ds in data_sources or []:
        if (ds.get("type") or "dynamic").lower() == "external":
            continue
        for name in collections_for_data_source(ds):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _value_to_type_tag(v: Any) -> str:
    if v is None:
        return "Null"
    if isinstance(v, ObjectId):
        return "ObjectId"
    if isinstance(v, datetime):
        return "Date"
    if isinstance(v, bool):
        return "Boolean"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "Number"
    if isinstance(v, str):
        return "Text"
    if isinstance(v, list):
        return "Array"
    if isinstance(v, dict):
        return "Object"
    return "Any"


def derive_collection_schema_from_sample(
    sample: Dict[str, Any], *, max_fields: int = 80
) -> Dict[str, str]:
    if not isinstance(sample, dict):
        return {}
    out: Dict[str, str] = {}
    keys = list(sample.keys())
    keys.sort(key=lambda k: (0 if k == "_id" else 1, str(k)))
    for k in keys[:max_fields]:
        try:
            out[str(k)] = _value_to_type_tag(sample.get(k))
        except Exception:
            out[str(k)] = "Any"
    return out


async def subscriber_database_exists(db_name: str) -> bool:
    """True if the subscriber database has been created (first /dynamic/execute write creates it)."""
    if not db_name:
        return False
    try:
        if Database.client is None and Database.native_async_client is None:
            Database.connect_db()
        client = Database.native_async_client or Database.client
        if client is None:
            return False
        names = await client.list_database_names()
        return db_name in names
    except Exception as exc:
        logger.warning("Could not list databases for subscriber check: %s", exc)
        return False


async def fetch_latest_sample_doc_in_db(
    collection_name: str, *, db_name: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not collection_name or not isinstance(collection_name, str):
        return None
    try:
        if Database.client is None and Database.native_async_client is None:
            Database.connect_db()
        if db_name:
            if Database.native_async_client is not None:
                coll = Database.native_async_client[db_name][collection_name]
                doc = await coll.find_one({}, sort=[("_id", -1)])
            elif Database.client is not None:
                coll = Database.client[db_name][collection_name]
                doc = await coll.find_one({}, sort=[("_id", -1)])
            else:
                return None
        else:
            coll = Database.get_collection(collection_name)
            doc = await coll.find_one({}, sort=[("_id", -1)])
        return dict(doc) if isinstance(doc, dict) else None
    except Exception as exc:
        logger.warning(
            "Schema sample fetch failed for %s (db=%s): %s",
            collection_name,
            db_name or "main",
            exc,
        )
        return None


async def build_collection_schemas(
    collections: List[str],
    *,
    subscriber_db_name: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Shape: { "collection": { "fields": {field: typeTag}, "sampled": bool } }
    Plus ``__meta__`` with probe context (not a real collection).
    """
    out: Dict[str, Dict[str, Any]] = {}
    subscriber_db_exists = True
    probe_skipped = False

    if subscriber_db_name:
        subscriber_db_exists = await subscriber_database_exists(subscriber_db_name)
        if not subscriber_db_exists:
            probe_skipped = True
            logger.info(
                "Subscriber DB %s does not exist yet (first screen) — "
                "all collections treated as NEW (snake_case only)",
                subscriber_db_name,
            )
            for name in collections or []:
                if name and name not in out:
                    out[name] = {"fields": {}, "sampled": False}
            out["__meta__"] = {
                "subscriber_db_exists": False,
                "probe_skipped": True,
                "any_sampled": False,
            }
            return out

    for name in collections or []:
        if not name or name in out:
            continue
        sample = await fetch_latest_sample_doc_in_db(name, db_name=subscriber_db_name)
        if not sample:
            out[name] = {"fields": {}, "sampled": False}
            continue
        out[name] = {
            "fields": derive_collection_schema_from_sample(sample),
            "sampled": True,
        }

    any_sampled = any(
        (info or {}).get("sampled")
        for key, info in out.items()
        if key != "__meta__"
    )
    out["__meta__"] = {
        "subscriber_db_exists": subscriber_db_exists,
        "probe_skipped": probe_skipped,
        "any_sampled": any_sampled,
    }
    return out


def format_collection_schemas_for_prompt(
    collection_schemas: Dict[str, Dict[str, Any]],
    *,
    subscriber_db_name: Optional[str],
) -> str:
    if not collection_schemas:
        return ""
    meta = collection_schemas.get("__meta__") or {}
    lines = [
        "PRE-GENERATION DATABASE SCHEMA SNAPSHOT (inspect before writing DB code):",
        f"Database: {subscriber_db_name or get_main_db_name() + ' (main — no subscriber id)'}",
    ]
    if meta.get("subscriber_db_exists") is False:
        lines.append(
            "FIRST SCREEN: Subscriber database is not created yet (it is created on first "
            "/dynamic/execute). ALL collections below are NEW — use snake_case param keys only; "
            "do NOT use resolve_field_name."
        )
    else:
        lines.append(
            "Use EXACT field names from sampled collections. EMPTY = new collection → snake_case only."
        )
    for name in sorted(collection_schemas.keys()):
        if name == "__meta__":
            continue
        info = collection_schemas[name] or {}
        if info.get("sampled"):
            fields = info.get("fields") or {}
            parts = [f"{k} ({v})" for k, v in sorted(fields.items())[:50]]
            lines.append(f"  - {name}: HAS DATA — fields: {', '.join(parts) or '(none)'}")
        else:
            lines.append(
                f"  - {name}: EMPTY or missing — treat as NEW (snake_case); do not use resolve_field_name"
            )
    return "\n".join(lines)


_COLLECTION_NAME_PATTERNS = (
    re.compile(r"collection\s+['\"`]([a-zA-Z_][\w]*)['\"`]", re.IGNORECASE),
    re.compile(r"['\"`]([a-zA-Z_][\w]*)['\"`]\s+(?:database\s+)?collection", re.IGNORECASE),
    re.compile(r"(?:into|from|in|to)\s+(?:the\s+)?['\"`]([a-zA-Z_][\w]*)['\"`]", re.IGNORECASE),
    re.compile(r"DB\s+`([a-zA-Z_][\w]*)`", re.IGNORECASE),
    re.compile(r"Collection:\s*([a-zA-Z_][\w]*)", re.IGNORECASE),
)


def extract_collection_names_from_text(*texts: Optional[str]) -> List[str]:
    """Heuristic extraction of Mongo collection identifiers from planner/step text."""
    seen: set[str] = set()
    out: List[str] = []
    skip = {
        "true",
        "false",
        "null",
        "none",
        "string",
        "number",
        "boolean",
        "object",
        "array",
        "success",
        "failure",
        "error",
        "data",
        "status",
    }
    for text in texts:
        if not text or not isinstance(text, str):
            continue
        for pat in _COLLECTION_NAME_PATTERNS:
            for m in pat.finditer(text):
                name = (m.group(1) or "").strip()
                if not name or name.lower() in skip or len(name) < 2:
                    continue
                if name not in seen:
                    seen.add(name)
                    out.append(name)
    return out


def normalize_stored_collection_schemas(
    stored: Any,
) -> Dict[str, Dict[str, Any]]:
    """
    Normalize Process_Configs / Stage0 / API schema payloads into
    ``{coll: {fields: {name: type}, sampled: bool}}``.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not stored:
        return out

    def _add(name: str, fields: Dict[str, str], *, sampled: bool) -> None:
        n = (name or "").strip()
        if not n or n == "__meta__":
            return
        existing = out.get(n) or {"fields": {}, "sampled": False}
        merged_fields = dict(existing.get("fields") or {})
        merged_fields.update({str(k): str(v) for k, v in (fields or {}).items() if k})
        out[n] = {
            "fields": merged_fields,
            "sampled": bool(existing.get("sampled") or sampled or bool(merged_fields)),
        }

    if isinstance(stored, dict):
        # Probe shape already, or {name: {fields: ...}} / {name: [field,...]}
        if any(k != "__meta__" and isinstance(v, dict) and "fields" in v for k, v in stored.items()):
            for k, v in stored.items():
                if k == "__meta__" or not isinstance(v, dict):
                    continue
                fields_raw = v.get("fields") or {}
                if isinstance(fields_raw, dict):
                    fields = {str(fk): str(fv) for fk, fv in fields_raw.items()}
                elif isinstance(fields_raw, list):
                    fields = {}
                    for item in fields_raw:
                        if isinstance(item, dict):
                            fname = item.get("name") or item.get("field")
                            if fname:
                                fields[str(fname)] = str(
                                    item.get("type") or item.get("dataType") or "string"
                                )
                        elif isinstance(item, str):
                            fields[item] = "string"
                else:
                    fields = {}
                _add(str(k), fields, sampled=bool(v.get("sampled") or fields))
            return out
        # Stage0-ish single map of collection → field list
        for k, v in stored.items():
            if k == "__meta__":
                continue
            if isinstance(v, list):
                fields = {}
                for item in v:
                    if isinstance(item, dict):
                        fname = item.get("name") or item.get("field")
                        if fname:
                            fields[str(fname)] = str(
                                item.get("type") or item.get("dataType") or "string"
                            )
                    elif isinstance(item, str):
                        fields[item] = "string"
                _add(str(k), fields, sampled=bool(fields))
            elif isinstance(v, dict) and not any(
                x in v for x in ("fields", "sampled", "collection_name", "collectionName")
            ):
                # Treat as field→type map
                _add(str(k), {str(fk): str(fv) for fk, fv in v.items()}, sampled=True)

    elif isinstance(stored, list):
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            name = (
                entry.get("collection_name")
                or entry.get("collectionName")
                or entry.get("name")
                or entry.get("collection")
            )
            fields: Dict[str, str] = {}
            fields_raw = entry.get("fields") or entry.get("schema") or []
            if isinstance(fields_raw, dict):
                fields = {str(fk): str(fv) for fk, fv in fields_raw.items()}
            elif isinstance(fields_raw, list):
                for item in fields_raw:
                    if isinstance(item, dict):
                        fname = item.get("name") or item.get("field")
                        if fname:
                            fields[str(fname)] = str(
                                item.get("type") or item.get("dataType") or "string"
                            )
                    elif isinstance(item, str):
                        fields[item] = "string"
            if name:
                _add(str(name), fields, sampled=bool(fields))

    return out


def merge_collection_schemas(
    *sources: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Merge schema maps; prefer sampled field maps when combining."""
    out: Dict[str, Dict[str, Any]] = {}
    meta: Dict[str, Any] = {}
    for src in sources:
        if not src:
            continue
        for name, info in src.items():
            if name == "__meta__":
                if isinstance(info, dict):
                    meta.update(info)
                continue
            if not isinstance(info, dict):
                continue
            existing = out.get(name) or {"fields": {}, "sampled": False}
            fields = dict(existing.get("fields") or {})
            fields.update(
                {str(k): str(v) for k, v in (info.get("fields") or {}).items() if k}
            )
            out[name] = {
                "fields": fields,
                "sampled": bool(
                    existing.get("sampled") or info.get("sampled") or bool(fields)
                ),
            }
    if meta:
        out["__meta__"] = meta
    return out


def format_collection_schemas_for_planner(
    collection_schemas: Dict[str, Dict[str, Any]],
    *,
    subscriber_db_name: Optional[str] = None,
) -> str:
    """
    Planner-facing schema block for generationDescription quality.

    Requires tools/UI nodes that touch these collections to copy field:type into
    their descriptions (Task 6).
    """
    usable = {
        k: v
        for k, v in (collection_schemas or {}).items()
        if k != "__meta__" and isinstance(v, dict) and (v.get("fields") or v.get("sampled"))
    }
    if not usable:
        return ""

    lines = [
        "═══════════════════════════════════════════════════════",
        "KNOWN COLLECTION SCHEMAS (authoritative — copy into node descriptions)",
        "═══════════════════════════════════════════════════════",
        f"Database: {subscriber_db_name or get_main_db_name()}",
        "When a tool or ui_screen reads/writes any collection below, its description",
        "MUST include the exact collection name AND field:type schema, e.g.",
        "  Collection 'mna_leads' schema: {company_name:string, current_tab:string, is_deleted:string}.",
        "Do NOT invent fields. Prefer these exact names/types over paraphrases.",
        "",
    ]
    for name in sorted(usable.keys()):
        info = usable[name] or {}
        fields = info.get("fields") or {}
        if fields:
            parts = [f"{k}:{v}" for k, v in sorted(fields.items())[:60]]
            lines.append(f"  Collection `{name}` schema: {{{', '.join(parts)}}}")
        elif info.get("sampled"):
            lines.append(f"  Collection `{name}`: exists but fields unknown — do not invent keys")
        else:
            lines.append(
                f"  Collection `{name}`: NEW / empty — use snake_case fields from the step contract"
            )
    lines.append("")
    return "\n".join(lines)


async def resolve_schemas_for_planner(
    *,
    texts: Optional[List[Optional[str]]] = None,
    collection_names: Optional[List[str]] = None,
    project_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    workflow_registry: Optional[Dict[str, Any]] = None,
    stored_schemas: Any = None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """
    Build a planner schema map from registry + stored Process_Configs/Stage0 + live probe.

    Returns ``(schemas_dict, prompt_text)``. Prompt text is empty when nothing useful.
    """
    names: List[str] = []
    seen: set[str] = set()

    def _add_name(n: Any) -> None:
        if not isinstance(n, str):
            return
        name = n.strip()
        if name and name not in seen and name != "__meta__":
            seen.add(name)
            names.append(name)

    for n in collection_names or []:
        _add_name(n)
    for t in texts or []:
        for n in extract_collection_names_from_text(t):
            _add_name(n)

    reg = workflow_registry or {}
    cols = reg.get("collections") if isinstance(reg, dict) else None
    if isinstance(cols, dict):
        for n in cols.keys():
            _add_name(n)

    stored_norm = normalize_stored_collection_schemas(stored_schemas)
    for n in stored_norm.keys():
        _add_name(n)

    if not names and not stored_norm:
        return {}, ""

    subscriber_db = resolve_subscriber_db_name(
        project_id=project_id or "",
        subscriber_id=subscriber_id,
    )

    probed: Dict[str, Dict[str, Any]] = {}
    if names:
        try:
            probed = await build_collection_schemas(
                names, subscriber_db_name=subscriber_db
            )
        except Exception as exc:
            logger.warning("Planner schema probe failed: %s", exc)
            probed = {n: {"fields": {}, "sampled": False} for n in names}

    # Registry fields (may include types as string values)
    registry_schemas: Dict[str, Dict[str, Any]] = {}
    if isinstance(cols, dict):
        for coll_name, info in cols.items():
            if not isinstance(info, dict):
                continue
            fields_raw = info.get("fields") or {}
            fields: Dict[str, str] = {}
            if isinstance(fields_raw, dict):
                for fk, fv in fields_raw.items():
                    if isinstance(fv, dict):
                        fields[str(fk)] = str(
                            fv.get("type") or fv.get("dataType") or "string"
                        )
                    else:
                        fields[str(fk)] = str(fv or "string")
            if fields:
                registry_schemas[str(coll_name)] = {
                    "fields": fields,
                    "sampled": True,
                }

    merged = merge_collection_schemas(stored_norm, registry_schemas, probed)
    prompt = format_collection_schemas_for_planner(
        merged, subscriber_db_name=subscriber_db
    )
    return merged, prompt
