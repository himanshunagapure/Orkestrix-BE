"""
Capability repository sync: adapters that populate capability_repository from
tools (tool_registry + JSON files), screen_generations, and Workflow_details.

See docs/CAPABILITY_REPOSITORY_TECHNICAL_DESIGN.md.
"""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from bson import ObjectId

from repositories.capability_repository import CapabilityRepository
from services.collection_schema import (
    build_collection_schemas as _build_collection_schemas,
    subscriber_db_name_from_subscriber_id as _subscriber_db_name_from_subscriber_id,
)
from services.database import Database


# Canonical data types for I/O parameters (design doc)
DATA_TYPES = ("Text", "Number", "Boolean", "Any", "Object", "Array")

def _get_main_db_name() -> str:
    """
    Best-effort main DB name resolution (matches services.database connect_db fallback):
    - Prefer env MONGODB_DB_NAME
    - Else read config.json (or CONFIG_PATH) devDb
    """
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


def _subscriber_db_name_from_subscriber_id(subscriber_id: Optional[str]) -> Optional[str]:
    """
    Convention used across backend: <main_db_name>_sb_<subscriber_id>
    """
    sid = (subscriber_id or "").strip()
    if not sid:
        return None
    return f"{_get_main_db_name()}_sb_{sid}"


def _value_to_type_tag(v: Any) -> str:
    # Keep these tags stable and LLM-friendly.
    if v is None:
        return "Null"
    # ObjectId first (it subclasses something that can confuse naive checks)
    if isinstance(v, ObjectId):
        return "ObjectId"
    # datetime
    if isinstance(v, datetime):
        return "Date"
    # primitives
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


def _derive_collection_schema_from_sample(sample: Dict[str, Any], *, max_fields: int = 80) -> Dict[str, str]:
    """
    Shallow schema: field -> type tag.
    Uses a single sample document (latest) for performance and safety.
    """
    if not isinstance(sample, dict):
        return {}
    out: Dict[str, str] = {}
    # Stable ordering: _id first then alpha
    keys = list(sample.keys())
    keys.sort(key=lambda k: (0 if k == "_id" else 1, str(k)))
    for k in keys[:max_fields]:
        try:
            out[str(k)] = _value_to_type_tag(sample.get(k))
        except Exception:
            out[str(k)] = "Any"
    return out


async def _fetch_latest_sample_doc(collection_name: str) -> Optional[Dict[str, Any]]:
    """
    Fetch one latest doc for schema inspection. Returns a plain dict or None.
    """
    if not collection_name or not isinstance(collection_name, str):
        return None
    try:
        coll = Database.get_collection(collection_name)
        # Use _id desc so we likely get newest ObjectId doc; safe even if empty.
        doc = await coll.find_one({}, sort=[("_id", -1)])
        return dict(doc) if isinstance(doc, dict) else None
    except Exception:
        return None


async def _fetch_latest_sample_doc_in_db(collection_name: str, *, db_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Tenant-aware schema sample fetch.

    IMPORTANT: We must NOT rely on SUBSCRIBER_DB_NAME env here, because capability sync writes
    to the *main* capability_repository collection. Instead, we directly target the subscriber DB.
    """
    if not collection_name or not isinstance(collection_name, str):
        return None
    try:
        # Ensure Database client exists
        if Database.client is None:
            Database.connect_db(loop=Database.loop)
        if db_name:
            coll = Database.client[db_name][collection_name]
        else:
            coll = Database.get_collection(collection_name)
        doc = await coll.find_one({}, sort=[("_id", -1)])
        return dict(doc) if isinstance(doc, dict) else None
    except Exception:
        return None


async def _build_collection_schemas(
    collections: List[str],
    *,
    subscriber_db_name: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Build collectionSchemas map:
      { "collection": { "fields": {field: typeTag, ...}, "sampled": bool } }
    """
    out: Dict[str, Dict[str, Any]] = {}
    seen: set[str] = set()
    for c in collections or []:
        if not isinstance(c, str):
            continue
        name = c.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        sample = await _fetch_latest_sample_doc_in_db(name, db_name=subscriber_db_name)
        if not sample:
            out[name] = {"fields": {}, "sampled": False}
            continue
        out[name] = {"fields": _derive_collection_schema_from_sample(sample), "sampled": True}
    return out


def _extract_datastores_from_ir(ir_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract LLM-planner-friendly persistence metadata from a screen IR schema.

    Why this exists:
    - The agentic planner only sees capability {inputParameters, outputParameters, description}.
      For UI screens, the "backend" behavior (collections read/written + CRUD intent) is otherwise hidden.
    - We persist a lightweight summary in capability_repository so planning can make correct
      fetch/store decisions without inspecting implementation code.
    """
    if not isinstance(ir_schema, dict):
        return {"storeType": "mongodb", "readCollections": [], "writeCollections": [], "collections": []}

    read_cols: set[str] = set()
    write_cols: set[str] = set()
    all_cols: set[str] = set()

    def _add_cols(val: Any, target: set[str]):
        if not val:
            return
        if isinstance(val, str):
            v = val.strip()
            if v:
                target.add(v)
                all_cols.add(v)
            return
        if isinstance(val, list):
            for x in val:
                if isinstance(x, str) and x.strip():
                    target.add(x.strip())
                    all_cols.add(x.strip())

    # Preferred: explicit dataSources[].operation.{readCollections, writeCollections}
    data_sources = ir_schema.get("dataSources") or []
    if isinstance(data_sources, list):
        for ds in data_sources:
            if not isinstance(ds, dict):
                continue
            op = ds.get("operation") or {}
            if not isinstance(op, dict):
                continue
            _add_cols(op.get("readCollections"), read_cols)
            _add_cols(op.get("writeCollections"), write_cols)
            # Some IRs only specify singular "collection"
            _add_cols(op.get("collection"), all_cols)

    # Fallback: some IRs may have a top-level databaseLogic block (legacy)
    db_logic = ir_schema.get("databaseLogic") or {}
    if isinstance(db_logic, dict):
        _add_cols(db_logic.get("readCollections"), read_cols)
        _add_cols(db_logic.get("writeCollections"), write_cols)
        _add_cols(db_logic.get("collection"), all_cols)

    # If we only saw "collection", treat it as write by default (most screens "save to X collection")
    if all_cols and not read_cols and not write_cols:
        write_cols |= set(all_cols)

    # Keep stable ordering for prompt determinism
    read_list = sorted(read_cols)
    write_list = sorted(write_cols)
    coll_list = sorted(all_cols | read_cols | write_cols)

    return {
        "storeType": "mongodb",
        "readCollections": read_list,
        "writeCollections": write_list,
        "collections": coll_list,
    }


def _normalize_param(
    item: Any,
    default_required: bool = True,
) -> Dict[str, Any]:
    """Convert a single param (dict or list item) to canonical shape."""
    if isinstance(item, dict):
        name = item.get("name") or item.get("param_name") or ""
        data_type = item.get("dataType") or item.get("param_type") or item.get("type") or "Any"
        if isinstance(data_type, str) and data_type not in DATA_TYPES:
            data_type = "Any"
        return {
            "name": name,
            "dataType": data_type if isinstance(data_type, str) else "Any",
            "required": item.get("required", default_required),
            "description": item.get("description") or "",
        }
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return {
            "name": str(item[0]),
            "dataType": "Any",
            "required": default_required,
            "description": str(item[1]) if len(item) > 1 else "",
        }
    return {"name": "", "dataType": "Any", "required": default_required, "description": ""}


def normalize_input_parameters(raw: Any) -> List[Dict[str, Any]]:
    """Normalize inputParameters to canonical list format."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [_normalize_param(p, default_required=True) for p in raw if p]
    if isinstance(raw, dict):
        return [
            _normalize_param({"name": k, **(v if isinstance(v, dict) else {"description": str(v)})})
            for k, v in raw.items()
        ]
    return []


def normalize_output_parameters(raw: Any) -> List[Dict[str, Any]]:
    """Normalize outputParameters to canonical list format (required=False not used)."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [_normalize_param(p, default_required=False) for p in raw if p]
    if isinstance(raw, dict):
        return [
            _normalize_param({**({"name": k, **(v if isinstance(v, dict) else {"description": str(v)})}), "required": False})
            for k, v in raw.items()
        ]
    return []


def _tenant_from_doc(doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    org = doc.get("orgId")
    sub = doc.get("subscriberId")
    uid = doc.get("userId")
    return {
        "orgId": str(org) if org else None,
        "subscriberId": str(sub) if sub else None,
        "userId": str(uid) if uid else None,
    }


# ---------------------------------------------------------------------------
# Tools adapter: get_available_tools() + backend/data/tools/*.json (no bots)
# Tools are always stored with orgId, subscriberId, userId = null (global).
# ---------------------------------------------------------------------------


def _tool_description_from_instance(tool: Any) -> str:
    desc = getattr(tool, "description", None)
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc.strip()
    return str(desc).strip()


def _extract_dict_after_prefix(text: str, prefix: str) -> Optional[Dict[str, Any]]:
    """Find prefix in text, then parse a Python dict (brace-balanced) and return it."""
    idx = text.find(prefix)
    if idx < 0:
        return None
    start = text.find("{", idx + len(prefix))
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return ast.literal_eval(text[start : i + 1])
                except (ValueError, SyntaxError):
                    return None
    return None


def _parse_static_tool_description(description: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Parse the enriched tool description format used by agent YAML / listing:
      "Tool Name: X\n\nTool Arguments: {'a': {'description': None, 'type': 'str'}, ...}\n\nTool Description: Human description"
    Returns (human_description, input_parameters_list).
    If the format is not present, returns (description, []).
    """
    if not description:
        return ("", [])
    # Prefer human description from "Tool Description:" when present
    human_desc = description
    if "Tool Description:" in description:
        match_desc = re.search(r"Tool Description:\s*(.+?)(?=\s*Tool Name:|\s*Tool Arguments:|\Z)", description, re.DOTALL)
        if match_desc:
            human_desc = match_desc.group(1).strip()
    # Extract Tool Arguments dict (Python repr with nested dicts)
    args_dict = _extract_dict_after_prefix(description, "Tool Arguments:")
    input_params: List[Dict[str, Any]] = []
    if args_dict and isinstance(args_dict, dict):
        for param_name, param_info in args_dict.items():
            if isinstance(param_info, dict):
                data_type = param_info.get("type", "Any")
                if isinstance(data_type, str) and data_type not in DATA_TYPES:
                    data_type = "Text" if "str" in data_type else "Number" if "int" in data_type or "float" in data_type else "Boolean" if "bool" in data_type else "Any"
                input_params.append({
                    "name": param_name,
                    "dataType": data_type,
                    "required": True,
                    "description": param_info.get("description") or "",
                })
            else:
                input_params.append({"name": param_name, "dataType": "Any", "required": True, "description": ""})
    return (human_desc, input_params)


async def sync_tools(
    capability_repo: CapabilityRepository,
    get_available_tools: Callable[[], Dict[str, Any]],
    tools_path: str,
) -> int:
    """
    Sync all tools into capability_repository using the platform tenant (aiqod_orgId / aiqod_subscriberId).
    Uses get_available_tools() for names/descriptions and tools_path/*.json for JSON tool parameters.
    Returns count of capabilities upserted.
    """
    # Platform-global tools are stored under the platform org/sub, never all-null.
    from repositories.capability_repository import _PLATFORM_TENANT
    tenant: Dict[str, Optional[str]] = _PLATFORM_TENANT
    now = datetime.utcnow()
    available = get_available_tools()
    json_tools: Dict[str, Dict[str, Any]] = {}
    if tools_path and os.path.isdir(tools_path):
        for fname in os.listdir(tools_path):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(tools_path, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("name")
                if name:
                    json_tools[name] = data
            except Exception:
                continue

    count = 0
    # Sync union of code tools (get_available_tools) and JSON-defined tools.
    # Previously, JSON files only enriched existing code tools, which meant
    # JSON-only tools were never registered in capability_repository.
    all_tool_names = set(available.keys()) | set(json_tools.keys())
    for tool_name in sorted(all_tool_names):
        # Skip bot-suffixed tools (deprecated bots collection)
        if "_bot" in tool_name or "_uibot" in tool_name:
            continue
        tool_instance = available.get(tool_name)
        raw_description = _tool_description_from_instance(tool_instance)
        input_params: List[Dict[str, Any]] = []
        output_params: List[Dict[str, Any]] = []  # JSON tools have no output schema; static tools have no schema
        version = "1"
        tags: List[str] = []
        json_data = json_tools.get(tool_name)
        if json_data:
            input_params = normalize_input_parameters(json_data.get("parameters", []))
            # Read outputParameters if the tool JSON carries them (gap-generated tools always do).
            output_params = normalize_output_parameters(json_data.get("outputParameters") or [])
            version = str(json_data.get("version", 1))
            tags = list(json_data.get("keywords", []))
            # Prefer code tool description if available, otherwise JSON description.
            description = raw_description or json_data.get("description", "")
        else:
            # Static code tools: parse description for "Tool Arguments:" and "Tool Description:" if present
            description, parsed_inputs = _parse_static_tool_description(raw_description)
            if parsed_inputs:
                input_params = parsed_inputs
            else:
                description = raw_description

        capability = {
            "capabilityId": f"tool:{tool_name}",
            "kind": "tool",
            "name": tool_name,
            "description": description or "",
            "inputParameters": input_params,
            "outputParameters": output_params,
            "version": version,
            "isLatest": True,
            "status": "active",
            "source": {
                "sourceType": "get_available_tools",
                "sourceRef": {"toolName": tool_name},
                "sourceUpdatedAt": None,
                "syncedAt": now,
            },
            "runtime": {"tool": {"toolName": tool_name}},
            "searchText": f"{tool_name}\n{description}",
            "tags": tags,
        }
        await capability_repo.upsert_capability(tenant, capability)
        await capability_repo.set_is_latest_for_capability(tenant, capability["capabilityId"])
        count += 1
    return count


# ---------------------------------------------------------------------------
# UI screens adapter: screen_generations
# ---------------------------------------------------------------------------


async def sync_screen_single(
    capability_repo: CapabilityRepository,
    screen_doc: Dict[str, Any],
) -> bool:
    """
    Upsert one ui_screen capability from a screen_generations document.
    Call after create_screen_generation or save_screen_build_metadata.
    """
    tenant = _tenant_from_doc(screen_doc)
    screen_id = screen_doc.get("screen_id") or ""
    if not screen_id:
        return False
    ir_schema = screen_doc.get("ir_schema") or {}
    name = screen_doc.get("screen_name") or screen_doc.get("prompt") or screen_id
    description = screen_doc.get("description") or screen_doc.get("prompt") or ""
    version = screen_doc.get("version") or "1.0"
    now = datetime.utcnow()
    datastore = _extract_datastores_from_ir(ir_schema)
    # Schema snapshot for collections this screen reads/writes.
    collections_for_schema = []
    try:
        collections_for_schema = list((datastore or {}).get("collections") or [])
    except Exception:
        collections_for_schema = []
    subscriber_id = screen_doc.get("subscriberId")
    subscriber_id_str = str(subscriber_id) if subscriber_id else None
    subscriber_db_name = _subscriber_db_name_from_subscriber_id(subscriber_id_str)
    collection_schemas = await _build_collection_schemas(
        collections_for_schema,
        subscriber_db_name=subscriber_db_name,
    )
    collection_schemas = {
        k: v for k, v in collection_schemas.items() if k != "__meta__"
    }

    capability = {
        "capabilityId": f"ui:{screen_id}",
        "kind": "ui_screen",
        "name": name,
        "description": description,
        "inputParameters": normalize_input_parameters(ir_schema.get("inputParameters", [])),
        "outputParameters": normalize_output_parameters(ir_schema.get("outputParameters", [])),
        # Expose persistence intent for agentic planning (collections + read/write).
        "dataStores": [datastore] if datastore else [],
        # Expose shallow schema snapshots so planner can pick correct fields.
        # Shape:
        #   { "collection": { "fields": {field: typeTag}, "sampled": true|false } }
        "collectionSchemas": collection_schemas,
        "version": version,
        "isLatest": True,
        "status": "active",
        "source": {
            "sourceType": "screen_generations",
            "sourceRef": {"screenId": screen_id},
            "sourceUpdatedAt": screen_doc.get("updated_at"),
            "syncedAt": now,
        },
        "runtime": {
            "ui": {
                "screenId": screen_id,
                "projectId": screen_doc.get("project_id"),
            }
        },
        "searchText": f"{name}\n{description}",
        "tags": list(screen_doc.get("tags", [])),
    }
    await capability_repo.upsert_capability(tenant, capability)
    await capability_repo.set_is_latest_for_capability(tenant, capability["capabilityId"])
    return True


async def sync_screens_for_tenant(
    capability_repo: CapabilityRepository,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    screen_repo: Any,
) -> int:
    """
    Load all screen_generations for the tenant (non-deleted, latest per screen) and upsert.
    Returns count of capabilities synced.
    """
    collection = screen_repo.get_collection()
    query: Dict[str, Any] = {"deleted": {"$ne": True}}
    if org_id is not None:
        try:
            query["orgId"] = ObjectId(org_id) if isinstance(org_id, str) else org_id
        except Exception:
            pass
    if subscriber_id is not None:
        try:
            query["subscriberId"] = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
        except Exception:
            pass
    if user_id is not None:
        try:
            query["userId"] = ObjectId(user_id) if isinstance(user_id, str) else user_id
        except Exception:
            pass

    cursor = collection.find(query).sort("version", -1).sort("created_at", -1)
    seen: Dict[str, bool] = {}
    count = 0
    async for doc in cursor:
        # One capability per (project_id, screen_id) - take latest version only for rebuild
        key = f"{doc.get('project_id')}:{doc.get('screen_id')}"
        if key in seen:
            continue
        seen[key] = True
        # Convert for sync_screen_single (need plain dict; motor cursor returns dict)
        d = dict(doc)
        d["_id"] = str(doc.get("_id", ""))
        if await sync_screen_single(capability_repo, d):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Agents adapter: Workflow_details (stable only)
# ---------------------------------------------------------------------------


def _first_step_input_params(workflow_json: Dict[str, Any]) -> Any:
    steps = workflow_json.get("steps") or []
    if not steps:
        return []
    first = steps[0] if isinstance(steps[0], dict) else None
    return (first or {}).get("inputParameters", [])


def _last_step_output_params(workflow_json: Dict[str, Any]) -> Any:
    steps = workflow_json.get("steps") or []
    if not steps:
        return []
    last = steps[-1] if isinstance(steps[-1], dict) else None
    return (last or {}).get("outputParameters", [])


async def sync_workflow_single(
    capability_repo: CapabilityRepository,
    workflow_doc: Dict[str, Any],
) -> bool:
    """
    Upsert one agent capability from a Workflow_details document.
    Only registers if workflow_json.agentState == "stable" (or missing, treated as stable).
    Call after Workflow_details insert/update.
    """
    wj = workflow_doc.get("workflow_json") or {}
    workflow_id = wj.get("workflowId") or ""
    if not workflow_id:
        return False
    agent_state = wj.get("agentState", "stable")
    if agent_state != "stable":
        return False

    tenant = _tenant_from_doc(workflow_doc)
    name = wj.get("workflowName") or workflow_id
    desc = wj.get("description") or ""
    data_flow = wj.get("dataFlowExplanation") or ""
    description = f"{desc}\n\n{data_flow}".strip()
    version = wj.get("version") or wj.get("updatedAt") or "1"
    if hasattr(version, "isoformat"):
        version = version.isoformat() if version else "1"
    version = str(version)
    now = datetime.utcnow()

    capability = {
        "capabilityId": f"agent:workflow:{workflow_id}",
        "kind": "agent",
        "name": name,
        "description": description,
        "inputParameters": normalize_input_parameters(_first_step_input_params(wj)),
        "outputParameters": normalize_output_parameters(_last_step_output_params(wj)),
        "version": version,
        "isLatest": True,
        "status": "active",
        "source": {
            "sourceType": "workflow_details",
            "sourceRef": {"workflowId": workflow_id},
            "sourceUpdatedAt": workflow_doc.get("updatedAt"),
            "syncedAt": now,
        },
        "runtime": {
            "agent": {
                "workflowId": workflow_id,
                "workflowName": name,
                "agentState": "stable",
            }
        },
        "searchText": f"{name}\n{description}",
        "tags": list(wj.get("tags", [])),
    }
    await capability_repo.upsert_capability(tenant, capability)
    await capability_repo.set_is_latest_for_capability(tenant, capability["capabilityId"])
    return True


async def sync_agents_for_tenant(
    capability_repo: CapabilityRepository,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> int:
    """
    Load all Workflow_details for the tenant with agentState=stable (or missing) and upsert.
    Returns count of capabilities synced.
    """
    coll = Database.get_collection("Workflow_details")
    # Include workflows where agentState is "stable" or not set (treat missing as stable)
    query: Dict[str, Any] = {
        "$or": [
            {"workflow_json.agentState": "stable"},
            {"workflow_json.agentState": {"$exists": False}},
        ]
    }
    if org_id is not None:
        try:
            query["orgId"] = ObjectId(org_id) if isinstance(org_id, str) else org_id
        except Exception:
            pass
    if subscriber_id is not None:
        try:
            query["subscriberId"] = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
        except Exception:
            pass
    if user_id is not None:
        try:
            query["userId"] = ObjectId(user_id) if isinstance(user_id, str) else user_id
        except Exception:
            pass

    count = 0
    cursor = coll.find(query)
    async for doc in cursor:
        if await sync_workflow_single(capability_repo, dict(doc)):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Full rebuild
# ---------------------------------------------------------------------------


async def rebuild_capability_repository(
    capability_repo: CapabilityRepository,
    get_available_tools: Callable[[], Dict[str, Any]],
    tools_path: str,
    screen_repo: Any,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, int]:
    """
    Full rebuild: sync tools (global), then screens and agents for the tenant.
    Returns dict with keys: tools, ui_screens, agents.
    """
    counts = {"tools": 0, "ui_screens": 0, "agents": 0}
    counts["tools"] = await sync_tools(capability_repo, get_available_tools, tools_path)
    counts["ui_screens"] = await sync_screens_for_tenant(
        capability_repo, org_id, subscriber_id, user_id, screen_repo
    )
    counts["agents"] = await sync_agents_for_tenant(
        capability_repo, org_id, subscriber_id, user_id
    )
    return counts
