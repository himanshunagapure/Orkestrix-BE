import asyncio
import ast
import base64
import contextvars
import functools
import hashlib
import json
import keyword
import logging
import math
import os
import re
import string
import textwrap
import time
import urllib
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type, Union
from urllib.parse import urlparse

import httpx
from services.gemini_service import (
    get_gemini_api_key,
    sync_generate,
    async_generate,
    GeminiGenerateConfig,
    get_or_create_cache,
    google_search_tool,
)
from services.workflow_budget_service import BillingError
from pydantic import BaseModel, ValidationError, create_model
try:
    from pydantic import RootModel
except ImportError:
    # Fallback for Pydantic v1: define RootModel as a dummy wrapper
    class RootModel(BaseModel):
        __root__: Any
from bson import ObjectId

from services.collection_schema import (
    build_collection_schemas,
    collections_for_data_source,
    collections_from_data_sources,
    format_collection_schemas_for_prompt,
    get_main_db_name,
    resolve_subscriber_db_name,
)
from services.database import Database, _normalize_update_doc
from services.credential_service import CredentialService
from repositories.function_repository import FunctionRepository
from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response
from services.data_model_utils import (
    collect_referenced_item_model_names,
    format_ir_model_fields_for_prompt,
    item_model_name_from_items,
    normalize_data_models_to_format1,
)
from services.models_exec_utils import (
    missing_referenced_model_names,
    prepare_models_exec_source,
    rebuild_compiled_models,
)
from services.backend_conformance import (
    is_blocking_conformance_failure,
    is_nontrivial_domain_datasource,
    nested_object_array_field_names,
    run_conformance_gate,
    schema_field_names_from_datasource,
    sort_field_names_from_logic,
    write_path_requires_insert,
)
from services.provider_operation import (
    classify_provider_spec,
    extract_provider_operations_from_text,
    format_pinned_provider_spec,
)
from services.persistence_link import (
    format_pinned_persistence_spec,
    function_code_matches_persistence_fixture,
    resolve_persistence_links,
)

logger = logging.getLogger(__name__)

# Injected into LLM prompts; signature stays backward-compatible, body must not use wrappers.
def _runtime_execute_rules(*, requires_schema_probe: bool = False) -> str:
    base = """
RUNTIME EXECUTION (/aiqod-agent/agent/dynamic/execute):
- Keep signature: async def <id>(*, params, db_wrapper=None, secure_http=None, request_meta=None)
- db_wrapper and secure_http are ALWAYS None — do NOT check if they are None or return errors about them
- NEVER: if db_wrapper is None / if not db_wrapper / await db_wrapper.* / "db_wrapper is required"
- NEVER: if secure_http is None / if not secure_http / await secure_http.*
- Database: from services.database import Database — class methods: insert_one, update_one, upsert_one, delete_one, find_one, find_to_list, aggregate_to_list
- ❌ NEVER Database.find(...) — that class method does not exist. One doc: await Database.find_one(collection, filters). Many docs: await Database.find_to_list(collection, filters, length=100)
- ❌ NEVER Database.get_sync_collection(...) — that returns a synchronous pymongo handle and fails in the async sandbox. Use Database class methods only.
- SPEC FIDELITY: read params with EXACT inputModel field names (params.get("selected_jd_id") — never invent aliases like "jd_id" when the model says selected_jd_id). Use EXACT Mongo collection names from operation.collection / readCollections / writeCollections — do not pluralize or rename. If databaseLogic names an LLM model id (gemini-…), use that exact id in the request URL.
- CREDENTIALS (sandbox — CRITICAL): API keys / tokens / passwords are injected ONLY into request_meta["credentials"]. ALWAYS: creds = (request_meta or {}).get("credentials") or {}; key = creds.get("EXACT_NAME"). ❌ NEVER: os.environ.get(...) / os.getenv(...) / params.get("gemini_api_key") / params.get("GEMINI_API_KEY") for secrets — those are ALWAYS missing in the sandbox worker.
- Also OK: coll = Database.get_collection(name); doc = await coll.find_one(filters)
- HTTP: import requests or httpx and call them directly
SANDBOX MONGODB (subprocess — CRITICAL):
- NEVER asyncio.to_thread / loop.run_in_executor / functools.partial for MongoDB (causes can't start new thread / no event loop errors)
- NEVER sync pymongo: collection.find_one(...) without await
- Do NOT instantiate Database() — use Database class methods only
- Database.update_one(collection, filters, fields): pass a plain field dict only — NOT {"$set": fields}; the helper adds $set automatically
- ❌ Database.update_one is NOT upsert. Zero matched = silent no-write (no error). For a new document on a write path, use await Database.insert_one(collection, document). Only use update_one when the filter is guaranteed to match an existing document.
- For insert-or-update: await Database.upsert_one(collection, filters, fields) — plain field dict (helper adds $set). NEVER raw coll.update_one(..., {"$set": doc}, upsert=True).
- CURSORS (sandbox uses PyMongo native async; main app uses Motor — patterns MUST work for both):
  * Prefer: docs = await Database.aggregate_to_list(collection_name, pipeline)
  * Prefer: docs = await Database.find_to_list(collection_name, filters, length=100)
  * One document: doc = await Database.find_one(collection_name, filters)
  * Or: docs = await Database.cursor_to_list(collection.aggregate(pipeline))
  * Or: docs = await Database.cursor_to_list(collection.find(query), length=100)
  * ❌ NEVER: await collection.aggregate(pipeline).to_list(...)  # aggregate is a coroutine in sandbox
  * ❌ NEVER: await collection.aggregate(pipeline)  # returns a cursor, not documents
  * ❌ NEVER: await collection.find(query)  # returns a cursor, not documents
  * ❌ NEVER: await Database.find(...)  # does not exist; use find_one / find_to_list
  * Value-returning helpers (await the Database class method): find_one, insert_one, update_one, upsert_one, delete_one
  * Also OK: await collection.find_one(query) / await collection.count_documents(query) after get_collection
"""
    if requires_schema_probe:
        return base + """
EXISTING COLLECTION (schema probe required for this function):
- Import: from services.field_utils import resolve_field_name
- Use: field_db_name = await resolve_field_name(collection_name, "logicalName")  # BOTH args REQUIRED
- ❌ NEVER: await resolve_field_name(collection_name)  # missing logical_name → TypeError at runtime
- ✅ ALWAYS: await resolve_field_name(collection_name, "user_email")  # collection + logical name
- CRITICAL: the second argument must be the EXACT logical field name from databaseLogic /
  schema / outputFieldMappings (e.g. "total_years_of_experience", "network_distance").
  ❌ NEVER abbreviate or invent short aliases ("exp", "dist", "yr", "net") — resolve_field_name
  is NOT a fuzzy matcher; unknown names fall back to the literal and Mongo sorts on a missing field.
- Do NOT define nested resolve_field_name/to_snake_case inline — use services.field_utils only
- NEVER Database() — resolve_field_name uses Database.get_collection internally
- New documents still use await Database.insert_one even when the collection already exists. update_one on an empty/unmatched filter does not create a row.
"""
    return base + """
NEW COLLECTION / INSERT-ONLY (this function):
- Map snake_case params directly to document fields; ONE await Database.insert_one(collection, document)
- Do NOT import or call resolve_field_name; do NOT probe schema with sample find_one per field
"""


def _arcadedb_runtime_execute_rules() -> str:
    return """
RUNTIME EXECUTION (ArcadeDB — /aiqod-agent/agent/dynamic/execute):
- Keep signature: async def <id>(*, params, db_wrapper=None, secure_http=None, request_meta=None)
- db_wrapper and secure_http are ALWAYS None — do NOT check them, do NOT use them
- Graph access: from services.database import Database
- Query/write: rows = await Database.run_arcadedb_command(command, params, language="cypher")
- NEVER pass database= to run_arcadedb_command — subscriber routing is automatic (same identity as MongoDB's subscriber DB)
- command is a Cypher string; params is a flat dict of bind variables referenced with $name in the command
- rows is always List[Dict[str, Any]] — one dict per result record; empty list means no matches
- Use MERGE (not CREATE) when the node/edge should be created only if it does not already exist
- HTTP: import requests or httpx and call them directly
"""


def _build_arcadedb_generation_system_instructions(
    *,
    input_model_info: str = "",
    response_model_info: str = "",
    dependency_info_section: str = "",
    security_feedback: str = "",
) -> str:
    """System instructions for ArcadeDB-backed (Cypher/graph) function generation."""
    return (
        """You are an expert Python backend developer generating secure async functions backed by ArcadeDB (graph database).

CRITICAL REQUIREMENTS:
1. Generate ONLY the function code, no markdown fences, no explanations
2. Function must be async and match signature: async def {id}(*, params, db_wrapper=None, secure_http=None, request_meta=None)
"""
        + _arcadedb_runtime_execute_rules()
        + """
3. CRITICAL IMPORT RULE - ALL IMPORTS MUST BE INSIDE THE FUNCTION BODY (same as any dynamic function)
3a. NEVER use relative imports or import other generated functions — sandbox functions run via exec() in isolation
4. Include comprehensive error handling with try-except blocks, and log every error:
   import logging; logger = logging.getLogger(__name__); logger.error(f"Error in {func_id}: {{str(e)}}", exc_info=True)
5. Return JSON-serializable dictionaries with proper error messages
6. For missing credentials/config, return an error dict that STILL matches the response model schema
7. Validate all required parameters and provide helpful error messages
8. Always include a docstring explaining what the function does
9. CRITICAL ANTI-PLACEHOLDER RULES: never generate placeholder/TODO/simulated code; the code must be fully complete and production-ready
10. CRITICAL: Return field names must EXACTLY match the response model structure provided below
11. CRITICAL: Any non-Optional response field MUST always get a real value of the correct type (never null/None)
12. CRITICAL: You MUST NOT invent extra fields not in the response model, and MUST NOT omit required fields

GRAPH DATA MODELING (CRITICAL):
- Choose a node label and property names that clearly reflect the entity (e.g. (:User {{user_id: $user_id}}))
- Use MERGE for idempotent node/edge creation, MATCH for reads, SET for property updates, DETACH DELETE for removals
- Store IDs as plain strings/uuids in properties (e.g. user_id, order_id) — do not rely on ArcadeDB's internal RID for application logic
- CRITICAL — NEVER serialize a list of nodes/edges into a JSON string property on a single vertex
  (e.g. NEVER do `SET g.nodes_json = json.dumps(nodes)`). That stores a document inside a graph database
  and defeats the entire purpose of using ArcadeDB — none of that data is traversable or queryable as a
  graph. If the input is a whole topology (a list of node records plus a list of edge records), you MUST
  create one real vertex per node record and one real relationship per edge record, as shown below.
- ArcadeDB's Cypher does not support a dynamic/parameterized relationship type in MERGE — use one fixed
  relationship type (e.g. :CONNECTS_TO) and store the specific edge id/type as a property on that
  relationship, rather than trying to bind the type itself to a variable.
- Example pattern (single entity):
```python
async def create_user_node(*, params, db_wrapper=None, secure_http=None, request_meta=None):
    \"\"\"Create or update a User node in the graph.\"\"\"
    import logging
    from services.database import Database
    logger = logging.getLogger(__name__)
    try:
        user_id = params.get("user_id")
        name = params.get("name", "")
        if not user_id:
            return {{"error": "user_id is required", "success": False}}
        command = "MERGE (u:User {{user_id: $user_id}}) SET u.name = $name RETURN u"
        rows = await Database.run_arcadedb_command(command, {{"user_id": user_id, "name": name}}, language="cypher")
        return {{"success": True, "data": rows, "error": ""}}
    except Exception as e:
        logger.error(f"Error in create_user_node: {{str(e)}}", exc_info=True)
        return {{"success": False, "data": [], "error": str(e)}}
```
- Example pattern (a whole topology — list of nodes + list of edges — one real vertex per node,
  one real relationship per edge, NOT a JSON blob):
```python
async def save_graph_topology(*, params, db_wrapper=None, secure_http=None, request_meta=None):
    \"\"\"Persist a designed graph as real ArcadeDB vertices and relationships.\"\"\"
    import logging
    from services.database import Database
    logger = logging.getLogger(__name__)
    try:
        nodes = params.get("graph_nodes") or []
        edges = params.get("graph_edges") or []
        for node in nodes:
            await Database.run_arcadedb_command(
                "MERGE (n:GraphNode {{node_id: $node_id}}) "
                "SET n.node_label = $node_label, n.node_type = $node_type, "
                "n.position_x = $position_x, n.position_y = $position_y",
                {{
                    "node_id": node.get("node_id"),
                    "node_label": node.get("node_label", ""),
                    "node_type": node.get("node_type", ""),
                    "position_x": node.get("position_x", 0.0),
                    "position_y": node.get("position_y", 0.0),
                }},
                language="cypher",
            )
        for edge in edges:
            await Database.run_arcadedb_command(
                "MATCH (a:GraphNode {{node_id: $source_node_id}}), (b:GraphNode {{node_id: $target_node_id}}) "
                "MERGE (a)-[r:CONNECTS_TO {{edge_id: $edge_id}}]->(b)",
                {{
                    "edge_id": edge.get("edge_id"),
                    "source_node_id": edge.get("source_node_id"),
                    "target_node_id": edge.get("target_node_id"),
                }},
                language="cypher",
            )
        return {{"success": True, "error": ""}}
    except Exception as e:
        logger.error(f"Error in save_graph_topology: {{str(e)}}", exc_info=True)
        return {{"success": False, "error": str(e)}}
```

HTTP REQUESTS:
- Use requests or httpx directly, with timeouts and error handling, same as any other function

EXTERNAL LIBRARIES:
- Import and use any Python package needed for the task; only import what you actually use

FILE & BINARY DATA FROM FRONTEND (if this function handles file uploads):
- Normalize incoming file content (list of ints, base64 string, or bytes) into raw bytes before processing

"""
        + _arcadedb_runtime_execute_rules()
        + """
SECURITY RULES (CRITICAL):
- Never execute arbitrary code from params (no eval, exec, compile with user input)
- Always validate and sanitize user inputs
- Avoid infinite loops or excessive resource consumption
- Always validate URLs before making external HTTP requests
"""
        + input_model_info
        + response_model_info
        + dependency_info_section
        + security_feedback
    )


async def _resolve_external_api_spec_with_grounding(
    description: str,
    database_logic: str,
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Use Gemini + Google Search grounding to discover the external API spec
    (endpoint URL, HTTP method, request/response format) for a backend dataSource
    whose databaseLogic does not already contain a concrete endpoint.

    Returns a text block injected into the code-generation prompt so the
    backend_generator can emit correct API calls without guessing.
    Returns None on failure (non-fatal — generation proceeds without grounding).
    """
    if not get_gemini_api_key():
        return None

    cred_list = [
        {"name": c.get("name"), "type": c.get("type"), "service": c.get("service")}
        for c in (available_credentials or [])
        if c.get("name")
    ]

    research_prompt = f"""You are a technical researcher. Use Google Search to find the CURRENT, OFFICIAL API documentation for the external service described below.

External API task description:
{description.strip()}

Additional backend logic context:
{database_logic.strip() or "(none)"}

Configured project credentials (infer which provider this is):
{json.dumps(cred_list, indent=2) if cred_list else "[]"}

Instructions:
1. Search for the official API documentation for the provider/service implied by the description above.
2. Return ONLY the information needed to make a single HTTP call for this specific task:
   - Exact endpoint URL (full, with any required path parameters noted)
   - HTTP method (GET / POST / PUT / PATCH / DELETE)
   - Required request headers (e.g. Authorization type: Bearer / API-Key / Basic)
   - Request body or query parameter field names and their types
   - Key response field names and their types
   - Any important error codes or status meanings
3. If multiple API versions exist, use the latest stable version.
4. Keep the answer concise — one section only, plain text, no markdown headers.
5. If the provider is not a publicly documented API (i.e. it appears private/internal),
   say "PRIVATE_API — no public documentation available" and stop.

Return plain text, concise, ready to be injected into a code-generation prompt."""

    started_at = datetime.utcnow()
    try:
        response = await async_generate(
            "gemini-3-flash-preview",
            research_prompt,
            config=GeminiGenerateConfig(
                temperature=0.1,
                tools=google_search_tool(),
            ),
        )
        text = (response.text or "").strip()
        if not text or "PRIVATE_API" in text:
            return None
        usage = extract_token_usage_from_response(response)
        record_llm_call(
            project_id=project_id or "unknown",
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="backend-generation",
            sub_operation="external-api-spec-grounding",
            model="gemini-3-flash-preview",
            prompt_tokens=usage.get("prompt_tokens") or len(research_prompt) // 4,
            completion_tokens=usage.get("completion_tokens") or len(text) // 4,
            total_tokens=(usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0),
            status="success",
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
        logger.info("[backend-gen/grounding] Resolved external API spec (%d chars)", len(text))
        return text[:6000]  # cap to avoid blowing context window
    except BillingError:
        raise
    except Exception as exc:
        logger.warning("[backend-gen/grounding] External API spec lookup failed (non-fatal): %s", exc)
        record_llm_call(
            project_id=project_id or "unknown",
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="backend-generation",
            sub_operation="external-api-spec-grounding",
            model="gemini-3-flash-preview",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            status="error",
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
        return None


def _build_backend_generation_system_instructions(
    *,
    requires_schema_probe: bool,
    input_model_info: str = "",
    response_model_info: str = "",
    dependency_info_section: str = "",
    security_feedback: str = "",
) -> str:
    """Build system instructions for backend function generation (uncached path)."""
    return (
            """You are an expert Python backend developer generating secure async functions.

            CRITICAL REQUIREMENTS:
            1. Generate ONLY the function code, no markdown fences, no explanations
            2. Function must be async and match signature: async def {id}(*, params, db_wrapper=None, secure_http=None, request_meta=None)
            2b. RUNTIME EXECUTION — CRITICAL (read carefully):
            Schema mode for this function: {schema_mode}
            """
            + _runtime_execute_rules(requires_schema_probe=requires_schema_probe)
            + """
            3. CRITICAL IMPORT RULE - ALL IMPORTS MUST BE INSIDE THE FUNCTION BODY:
               ❌ WRONG (imports outside function):
               ```python
               import hashlib
               from datetime import datetime
               
               async def my_function(*, params, ...):
                   # function code
               ```
               
               ✅ CORRECT (imports inside function):
               ```python
               async def my_function(*, params, db_wrapper=None, secure_http=None, request_meta=None):
                   \"\"\"Docstring here\"\"\"
                   import hashlib
                   from datetime import datetime
                   from services.database import Database
                   from bson import ObjectId
                   
                   import logging
                   logger = logging.getLogger(__name__)
                   
                   try:
                       # Your implementation here
                       pass
                   except Exception as e:
                       # ALWAYS log errors with full context
                       logger.error(f"Error in my_function: {{str(e)}}", exc_info=True)
                       # Return error info even with default/empty fields
                       return {{
                           "error": str(e),
                           "error_type": type(e).__name__,
                           # Include default/empty fields to match response model
                       }}
               ```

            3a. CRITICAL — NEVER USE RELATIVE IMPORTS OR IMPORT OTHER GENERATED FUNCTIONS:
               Sandbox functions run in isolation via exec() with __name__="__main__" — they are NOT part of a Python package.
               ❌ FORBIDDEN (will crash at runtime with ImportError):
                  from .generate_embeddings import generate_embeddings
                  from .fetch_historical_projects import fetch_historical_projects
                  from .other_function import other_function
               ✅ ALLOWED imports only:
                  - Absolute package imports of installed libraries (e.g. from services.database import Database, import httpx)
                  - Stdlib imports (e.g. import math, from typing import List)
               To reuse dependency logic: INLINE it as local helpers inside this function, OR read pre-computed
               dependency outputs from params / request_meta. Do NOT import other generated functions.

            3. Include comprehensive error handling with try-except blocks
            4. ERROR LOGGING AND VISIBILITY:
               - ALWAYS log errors using: import logging; logger = logging.getLogger(__name__)
               - Log errors with full context: logger.error(f"Error in {func_id}: {str(e)}", exc_info=True)
               - When returning error responses, ALWAYS include error details in the response
               - Even when returning empty/default fields to match response model, include error information
               - Example error handling pattern:
                 ```python
                 import logging
                 logger = logging.getLogger(__name__)
                 
                 try:
                     # Your code here
                     result = some_operation()
                     return {{"status": "success", "data": result}}
                 except Exception as e:
                     # ALWAYS log the error
                     logger.error(f"Error in {func_id}: {{str(e)}}", exc_info=True)
                     # Return error info even with default fields
                     return {{
                         "status": "error",
                         "error": str(e),
                         "error_type": type(e).__name__,
                         "data": {{}},  # Empty/default data to match model
                         "message": f"Operation failed: {{str(e)}}"
                     }}
                 ```
            5. Return JSON-serializable dictionaries with proper error messages
            5. IMPORTANT - Library Usage Strategy:
               PREFER ALREADY-INSTALLED LIBRARIES:
               Example:
               - For password hashing: Use hashlib (stdlib) instead of bcrypt
                 Example: hashlib.sha256(password.encode()).hexdigest()
               - For HTTP requests: Use httpx or requests (already installed)
               - For database: Use Database.get_collection() (already available)
               - For JSON: Use json (stdlib)
               - For dates: Use datetime (stdlib)
               
               ONLY USE EXTERNAL LIBRARIES WHEN ABSOLUTELY NECESSARY:
               Example:
               - OCR: pytesseract, PIL (if image processing required)
               - PDF: PyPDF2, pdfplumber (if PDF parsing required)
               - Payment: stripe (if payment processing required)
               - Data science: pandas, numpy (if complex data processing required) etc.
               
               BEFORE importing an external library, ask yourself:
               - Is this library absolutely required for the task?

            6. For missing credentials/config, return error dict that STILL matches the response model schema (correct field names & types)
            7. Validate all required parameters and provide helpful error messages
            8. For external APIs: handle missing URL, auth tokens, timeouts gracefully
            9. For database operations: handle missing collection, invalid queries gracefully
            10. Always include docstring explaining what the function does
            11. CRITICAL ANTI-PLACEHOLDER RULES:
                - NEVER generate placeholder, TODO, or simulated code
                - The generated code MUST be fully complete and production-ready
                - Users have NO ACCESS to modify this code - it must work as-is
                - If you cannot implement something fully, return a proper error message instead of placeholder code
            12. COMPLETE IMPLEMENTATION REQUIREMENTS:
                - For file upload: Actually save the file to provided path or return error if not possible    
                - For API calls: Actually make the HTTP request or return error about missing config or invalid request
                - For database: Actually query/insert/update or return error about missing collection or invalid query
                - For OCR/PDF: Actually process the file or return error about missing library
                - NO "example" data, NO "mock" responses, NO "simulated" logic or placeholder code
            13. CRITICAL: Return field names must EXACTLY match the response model structure provided below
            14. CRITICAL: For any field in the response model that is NOT Optional/Union with None, you MUST always return a value of the correct type (never null/None).
                - Example: if the model defines "userId: <class 'str'>", then always return a string (use "" when you have no value), never null.
            15. CRITICAL: You MUST NOT invent extra fields that are not in the response model, and you MUST NOT omit required fields.
                - Every response must be a dict that can be passed directly into ResponseModel(**result) without raising a ValidationError.
            15b. NESTED OBJECT ARRAYS :
                - When the response model (or an item model inside a list field) declares
                  ``list[SomeItemModel]`` fields (e.g. experience: list[ExperienceItem],
                  education: list[EducationItem]), you MUST map those arrays from each DB
                  document into the returned dict/row.
                - Do NOT silently omit them because they are not scalars. Do NOT shove
                  object arrays into a string-field loop (optional_str_fields).
                - Example:
                  ```python
                  raw_exp = doc.get("experience") or []
                  candidate["experience"] = [
                      {{
                          "company": str(item.get("company") or ""),
                          "title": str(item.get("title") or ""),
                          "duration": str(item.get("duration") or ""),
                      }}
                      for item in raw_exp
                      if isinstance(item, dict)
                  ]
                  ```
            16. When using datetimes, use "from datetime import datetime" and ALWAYS call datetime.utcnow() (do NOT use datetime.datetime.utcnow()) so it works in the sandbox.
            17. access urllib.parse as urllib.parse, NOT as a separate import. Use "import urllib" then "urllib.parse.quote()" (DO NOT use "from urllib import parse" or "import urllib.parse")
            18. CRITICAL - MongoDB ObjectId and DateTime Serialization: MongoDB documents contain types that are NOT JSON-serializable.
                - ALWAYS convert ObjectId to string: str(doc["_id"]) if isinstance(doc["_id"], ObjectId)
                - CORRECT: isinstance(doc["_id"], ObjectId)
                - INCORRECT: isinstance(doc["_id"], bson.ObjectId)
                - ALWAYS convert datetime to ISO string: doc["created_at"].isoformat() if isinstance(doc["created_at"], datetime)
                - ALWAYS convert date to ISO string: doc["date"].isoformat() if isinstance(doc["date"], date)
                - CRITICAL - _id Field Format Handling:
                  * The _id field may be stored as ObjectId OR as a plain string (24-char hex or custom hash)
                  * When querying by _id, use safe_id_query() helper to handle both formats (see DATABASE ACCESS section)
                  * When returning documents, ALWAYS convert _id ObjectId to string: str(doc["_id"]) if isinstance(doc["_id"], ObjectId)
                  * This prevents mismatches where some functions expect ObjectId("...") but _id is stored as plain string. you MUST handle both string and ObjectId formats to avoid mismatches
                - Use this pattern for ALL database operations that return documents:

        9. DATABASE FIELD NAMING CONVENTIONS (CRITICAL):

        9.1 CORE PRINCIPLES:
        - NEW collections: ALWAYS use snake_case (document_id, file_name, pdf_url, created_at)
        - EXISTING collections: Inspect schema first, then use actual field names
        - NEVER invent camelCase or mixed formats for new data

        9.2 HELPERS (INSIDE THE FUNCTION):
        Put imports and safe_id_query INSIDE the function body. Never use Database().

        9.3 EXISTING COLLECTIONS (READ / UPDATE — schema probe REQUIRED):
        When databaseLogic says to inspect schema or operation reads/updates an existing collection:
        ```python
        from services.database import Database
        from services.field_utils import resolve_field_name

        collection_name = "loan_rules"  # from operation.collection or readCollections
        interest_field = await resolve_field_name(collection_name, "interest_rate")
        coll = Database.get_collection(collection_name)
        doc = await coll.find_one({{}})
        value = doc.get(interest_field, "") if doc else ""
        ```
        - resolve_field_name ALWAYS needs (collection_name, logical_name) — never one argument
        - resolve_field_name does ONE cached sample find_one per collection (not per field)
        - resolve_field_name(logical_name) requires the EXACT field name from databaseLogic /
          schema (e.g. "total_years_of_experience") — NEVER abbreviations like "exp" or "dist"
        - Do NOT define nested resolve_field_name — import from services.field_utils only

        9.4 NEW COLLECTIONS (INSERT — no schema probe):
        When creating new documents in a new collection, map snake_case params directly:
        ```python
        from services.database import Database
        document = {{
            "applicant_name": params.get("applicant_name"),
            "loan_amount": float(params.get("loan_amount")),
        }}
        result = await Database.insert_one("user_app", document)
        ```

        9.5 CRITICAL REMINDERS:
        - NEW inserts: snake_case from params; ONE await Database.insert_one — never resolve_field_name
        - EXISTING reads/updates: await resolve_field_name(collection, logical_name) from services.field_utils
        - Empty collection: resolve_field_name falls back to snake_case automatically
        - Database.update_one is NOT upsert — new documents still use insert_one even on an existing collection
        - Document lookup: try application_id field first, then _id fallback
        - Never run_in_executor/to_thread for MongoDB

            DATABASE ACCESS:
            - You can access the database directly using: from services.database import Database
            - Example: collection = Database.get_collection("collection_name")
            - CRITICAL: ALL MongoDB operations MUST use async/await on the main event loop — NEVER use synchronous operations
            - ❌ WRONG: collection.find_one(query) without await; asyncio.to_thread(...); loop.run_in_executor(..., find_one/insert_one)
            - ❌ WRONG: resolve_field_name with sample find_one per input field before a simple insert
            - ✅ CORRECT: result = await Database.insert_one(collection_name, document); app_id = result["inserted_id"]
            - ✅ CORRECT: doc = await collection.find_one(query)  # only when a read is required
            - CRITICAL — find / aggregate cursors (sandbox = PyMongo native async; Motor elsewhere):
              * ❌ WRONG: await collection.aggregate(pipeline).to_list(length=None)
                (sandbox: aggregate() is a coroutine → 'coroutine' object has no attribute 'to_list')
              * ❌ WRONG: await collection.aggregate(pipeline)  # AsyncCommandCursor can't be awaited as docs
              * ❌ WRONG: await collection.find(query)  # AsyncCursor can't be used in 'await' expression
              * ✅ CORRECT (preferred helpers):
                  docs = await Database.aggregate_to_list("users", pipeline)
                  docs = await Database.find_to_list("users", {{"age": {{"$gt": 18}}}}, length=100)
              * ✅ CORRECT (when you already have a collection handle):
                  docs = await Database.cursor_to_list(collection.aggregate(pipeline))
                  docs = await Database.cursor_to_list(collection.find({{"age": {{"$gt": 18}}}}), length=100)
              * ✅ ALSO OK: cursor = collection.find(query); docs = await cursor.to_list(length=100)
                (find returns a cursor immediately in both drivers)
              * ✅ ALSO OK dual-compat aggregate:
                  _agg = collection.aggregate(pipeline)
                  cursor = await _agg if hasattr(_agg, "__await__") else _agg
                  docs = await cursor.to_list(length=None)
            - Then use Database class methods with await: find_one, find_to_list, insert_one, update_one, delete_one
            - ❌ NEVER Database.find(...) — does not exist; one doc = Database.find_one, many = Database.find_to_list
            - Also OK: coll = Database.get_collection(name); doc = await coll.find_one(query)
            - CRITICAL - Database.insert_one() RETURN FORMAT:
              * Database.insert_one(collection_name, document) returns: {{"inserted_id": str(...)}}
              * The inserted_id is ALREADY a string (not ObjectId), so use it directly: result["inserted_id"]
              * ❌ WRONG: result["_id"] or result.inserted_id or str(result["_id"])
              * ✅ CORRECT: result = await Database.insert_one(collection_name, document); record_id = result["inserted_id"]
              * Example:
                result = await Database.insert_one("my_collection", {{"name": "test"}})
                inserted_id = result["inserted_id"]  # Already a string, use directly
            - ALWAYS convert ObjectId to string: str(doc["_id"]) if isinstance(doc["_id"], ObjectId)
            - ALWAYS convert datetime/date to ISO string: doc["created_at"].isoformat() if isinstance(doc["created_at"], datetime)
            - ALWAYS convert Decimal to float: float(doc["amount"]) if isinstance(doc["amount"], Decimal)
            - CRITICAL - Document ID Lookup: In this system the document identifier is stored as document_id (a normal field), NOT as MongoDB's _id. When looking up by documentId:
              * First try: query = {{"document_id": safe_id_query(documentId)}}  (application field)
              * If not found, try: query = {{"_id": safe_id_query(documentId)}}   (MongoDB _id fallback for LLM code that assumed documentId mapped to _id)
              * Prefer document_id-only when the collection uses document_id; _id is fallback only.
            - Example database code:
            ```python
                from services.database import Database
                from bson import ObjectId
                from bson.errors import InvalidId
                from datetime import datetime, date
                from decimal import Decimal
                import logging
                logger = logging.getLogger(__name__)
                
                def safe_id_query(_id_value):
                    \"\"\"Convert _id value to appropriate format for MongoDB query.\"\"\"
                    if _id_value is None:
                        return None
                    if isinstance(_id_value, ObjectId):
                        return _id_value
                    if isinstance(_id_value, str):
                        if len(_id_value) == 24:
                            try:
                                return ObjectId(_id_value)
                            except (InvalidId, ValueError):
                                return _id_value
                        return _id_value
                    return str(_id_value)
                
                try:
                    collection = Database.get_collection("users")
                    
                    # Example: Find by documentId - try document_id first (app field), then _id fallback
                    doc_id = params.get("id") or params.get("documentId")
                    if doc_id:
                        # Try document_id first (our app stores document identifier here, not in _id)
                        query = {{"document_id": safe_id_query(doc_id)}}
                        doc = await collection.find_one(query)
                        # If not found, try _id (fallback for LLM code that assumed documentId mapped to _id)
                        if not doc:
                            query = {{"_id": safe_id_query(doc_id)}}
                            doc = await collection.find_one(query)
                        
                        docs = [doc] if doc else []
                    else:
                        # Find / aggregate multiple documents (Motor + sandbox safe)
                        docs = await Database.cursor_to_list(
                            collection.find({{"age": {{"$gt": 18}}}}), length=100
                        )
                        # Aggregation example:
                        # docs = await Database.aggregate_to_list("users", [{{"$match": {{"age": {{"$gt": 18}}}}}}])
                    
                    # Convert non-serializable types (including _id)
                    for doc in docs:
                        for key, value in list(doc.items()):
                            if isinstance(value, ObjectId):
                                doc[key] = str(value)
                            elif isinstance(value, (datetime, date)):
                                doc[key] = value.isoformat()
                            elif isinstance(value, Decimal):
                                doc[key] = float(value)
                    
                    return {{"data": docs, "count": len(docs)}}
                except Exception as e:
                    logger.error(f"Database operation failed: {{str(e)}}", exc_info=True)
                    return {{
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "data": [],
                        "count": 0
                    }}
            ```

            HTTP REQUESTS:
            - You can use ANY HTTP library: requests, httpx, etc.
            - Example with requests: import requests; response = requests.get(url, headers=headers)
            - Example with httpx: import httpx; async with httpx.AsyncClient() as client: response = await client.get(url)
            - Always handle errors and timeouts appropriately
            - Example:
            ```python
                import requests
                import logging
                logger = logging.getLogger(__name__)
                
                try:
                    response = requests.get(
                        url,
                        headers={{"Authorization": f"Bearer {{token}}"}},
                        timeout=10
                    )
                    response.raise_for_status()
                    return {{"data": response.json(), "status": "success"}}
                except requests.RequestException as e:
                    # ALWAYS log errors
                    logger.error(f"HTTP request failed: {{str(e)}}", exc_info=True)
                    # Return error info with default fields
                    return {{
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "status": "error",
                        "data": {{}}  # Empty data to match model
                    }}
            ```

            EXTERNAL LIBRARIES:
            - You can import and use any Python package needed for the function
            - Just import what you need, don't import unnecessary libraries

            FILE & BINARY DATA FROM FRONTEND (CRITICAL):
            - Frontend clients (Angular and others) send file content in JSON-safe formats, not guaranteed to be raw bytes.
              Typical patterns you MUST handle robustly include at least:
                - {"file": {"filename": "...", "content": [37,80,68,70,...]}}      # list of integers (0-255)
                - {"file": {"filename": "...", "content": "<base64-encoded>"}}     # base64 string
                - {"file": {"filename": "...", "content": <raw-bytes-if-present>}} # bytes (e.g. from other callers)
            - Normalize whatever you receive into bytes *before* passing to pdf/image/document libraries:
              Example pattern (ADAPT FIELD NAMES / ERROR FIELDS TO THE ACTUAL REQUEST/RESPONSE MODELS):
                file_obj = params.get("file") or {}
                raw_content = file_obj.get("content")
                file_bytes: bytes
                if isinstance(raw_content, bytes):
                    file_bytes = raw_content
                elif isinstance(raw_content, list):
                    # JSON-encoded Uint8Array: list of ints 0–255
                    file_bytes = bytes(int(b) & 0xFF for b in raw_content)
                elif isinstance(raw_content, str):
                    import base64
                    try:
                        file_bytes = base64.b64decode(raw_content)
                    except Exception:
                        # Use the actual response model schema for field names/types here
                        return {
                            "status": "error",
                            "message": "Invalid base64 file content."
                        }
                else:
                    # Use the actual response model schema for field names/types here
                    return {
                        "status": "error",
                        "message": "Unsupported file content format."
                    }

            """
            + _runtime_execute_rules(requires_schema_probe=requires_schema_probe)
            + """
            SECURITY RULES (CRITICAL):
            - Never execute arbitrary code from params (no eval, exec, compile with user input)
            - Never use eval(), exec(), compile() with user-provided data
            - Always validate and sanitize user inputs
            - Avoid infinite loops or excessive resource consumption
            - Always validate URLs before making requests
            - Don't perform bitcoin mining or excessive CPU-intensive operations
            - Be mindful of resource usage (memory, CPU, network)
            """
            + input_model_info
            + response_model_info
            + dependency_info_section
            + security_feedback
        )



_BACKEND_CACHE_TTL_SECONDS = 3600
# Bump when system instructions change so stale Gemini caches are not reused.
_BACKEND_FN_GEN_CACHE_PREFIX_PROBE = "backend-fn-gen-v3-probe"
_BACKEND_FN_GEN_CACHE_PREFIX_INSERT = "backend-fn-gen-v3-insert"
_BACKEND_FN_GEN_CACHE_PREFIX_ARCADEDB = "backend-fn-gen-v3-arcadedb"


def _build_backend_generation_cached_system_text(
    *, requires_schema_probe: bool, target_source: str = "mongodb"
) -> str:
    """Static system text for explicit context cache (mongo probe/insert, or arcadedb)."""
    if target_source == "arcadedb":
        return _build_arcadedb_generation_system_instructions()
    return _build_backend_generation_system_instructions(
        requires_schema_probe=requires_schema_probe,
        input_model_info="",
        response_model_info="",
        dependency_info_section="",
        security_feedback="",
    )


def _build_backend_generation_cached_user_content(
    user_prompt: str,
    *,
    input_model_info: str = "",
    response_model_info: str = "",
    dependency_info_section: str = "",
    security_feedback: str = "",
) -> str:
    """User content for cached path — dynamic system sections + original user prompt."""
    dynamic_parts = [
        input_model_info,
        response_model_info,
        dependency_info_section,
        security_feedback,
    ]
    dynamic = "".join(p for p in dynamic_parts if p)
    if dynamic.strip():
        return f"{dynamic.rstrip()}\n\n{user_prompt}"
    return user_prompt


async def _get_backend_fn_gen_cached_content(
    model: str,
    *,
    requires_schema_probe: bool,
    target_source: str = "mongodb",
) -> Optional[str]:
    """
    Explicit context cache for static backend function-generation system prompts.

    Cost optimization only — returns None on failure so callers keep working
    via the existing uncached [system_instructions, user_prompt] path.
    """
    try:
        system_text = _build_backend_generation_cached_system_text(
            requires_schema_probe=requires_schema_probe,
            target_source=target_source,
        )
        if target_source == "arcadedb":
            cache_key_prefix = _BACKEND_FN_GEN_CACHE_PREFIX_ARCADEDB
            display_name = "backend-generator-fn-gen-arcadedb"
        else:
            cache_key_prefix = (
                _BACKEND_FN_GEN_CACHE_PREFIX_PROBE
                if requires_schema_probe
                else _BACKEND_FN_GEN_CACHE_PREFIX_INSERT
            )
            display_name = (
                "backend-generator-fn-gen-probe"
                if requires_schema_probe
                else "backend-generator-fn-gen-insert"
            )
        cache_key = (
            cache_key_prefix
            + "_"
            + hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
        )
        return await get_or_create_cache(
            model=model,
            system_text=system_text,
            ttl_seconds=_BACKEND_CACHE_TTL_SECONDS,
            cache_key=cache_key,
            api_key=get_gemini_api_key() or None,
            display_name=display_name,
        )
    except Exception as exc:
        logger.warning(
            "Backend function-gen cache unavailable, continuing uncached: %s",
            exc,
        )
        return None


_BACKEND_SECURITY_VERIFY_CACHE_PREFIX = "backend-security-verify-v1"

_SECURITY_VERIFY_CACHED_SYSTEM_TEMPLATE = """You are a security expert reviewing Python function code for HIGHLY DANGEROUS patterns ONLY. You are also code reviewer analysing code quality.
            
            ⚠️ CRITICAL: This is a LIGHT security check for OBVIOUSLY DANGEROUS code ONLY.
            
            You MUST be VERY PERMISSIVE. Only flag code that is CLEARLY and IMMEDIATELY malicious or dangerous.
            File operations, API calls, database operations, and normal business logic are EXPECTED and MUST NOT be flagged.
            
            Flag ONLY if code contains OBVIOUSLY malicious patterns:
            1. Direct code execution: eval(), exec(), compile() with user input
            2. Command injection: os.system(), subprocess.call() with unsanitized input
            3. Obvious malicious patterns: cryptocurrency mining, intentional infinite loops, fork bombs, excessive CPU/memory consumption, denial of service
            4. Clear backdoors: listening on network ports, spawning reverse shells
            5. Comments about TODOs or simulated code or placeholder code that is not intended to be executed improvements needed
            6. Ensure upload/save/download directories exist BEFORE saving/downloading files
            7. Simulated code

            ⚠️ CODE ERRORS (MUST FIX):
            1. Missing imports (ObjectId, datetime, logging, etc.)
            2. Type mismatches (comparing bool with str, wrong types)
            3. Missing 'await' for async operations (MongoDB, HTTP)
            4. Missing os.makedirs before file operations
            5. Not converting ObjectId/datetime to JSON-serializable (str/ISO)
            6. Missing error handling / try-except blocks
            7. Missing logging setup (import logging, logger = ...)
            8. Response field names don't match response model
            9. Returning None/null for required fields
            10. Using datetime.datetime instead of datetime
            11. Using bson.ObjectId instead of ObjectId
            12. TODO/placeholder/simulated code logic.
            13. During document extraction, the document may be text-based or scanned. First attempt text extraction. If the extracted text is empty or contains only form-feed characters (), classify the document as scanned and perform OCR. For OCR, convert PDF pages to images at 300 DPI and extract text using Tesseract with --oem 1 --psm 6.
            14. API keys, tokens, and passwords: MUST be read from request_meta["credentials"], NEVER hardcoded. Correct pattern: api_key = request_meta.get("credentials", {{}}).get("gemini_api_key") then check "if not api_key: return ... error". Wrong: hardcoded strings like "AIza...", "sk-...", or variables set to literal secrets.
            
            API KEYS, TOKENS, AND SECRETS (correct usage - MUST enforce):
            - All API keys, bearer tokens, passwords, and other secrets MUST be read from request_meta["credentials"] using the exact credential name (e.g. request_meta.get("credentials", {{}}).get("gemini_api_key")).
            - The function MUST accept request_meta as a parameter and use it to obtain secrets. If a required credential is missing, return a clear error (e.g. "gemini_api_key credential is required") instead of using a hardcoded value.
            - NEVER allow: hardcoded API keys, tokens, passwords, or secret strings in the code (e.g. "AIzaSy...", "sk-...", SMTP passwords, OAuth secrets). Flag these as unsafe and suggest using request_meta["credentials"].
            
            CREDENTIAL USAGE RULES:
            Refer to the AVAILABLE CREDENTIALS section in the user request below when checking credential handling in the function source code.
            

            DO NOT FLAG (these are ALLOWED and EXPECTED in a light security check):
            - File uploads/downloads/saves to ANY directory (including /tmp, hardcoded paths like '/home/user/Projects/screen-builder/backend/data', or user-provided paths) - this is NORMAL and EXPECTED
            - Using os.path.join(), os.path.exists(), os.makedirs() - these are STANDARD file operations
            - Reading files from paths (whether from database, params) - this is NORMAL operation
            - File processing operations (PDF parsing, image processing, OCR, etc.) - this is NORMAL
            - Using libraries like pytesseract, PIL, pandas, requests, httpx - these are STANDARD tools
            - Normal HTTP requests or API calls - this is EXPECTED behavior
            - Normal database operations - this is EXPECTED behavior
            - Hardcoded paths or directories - this is ACCEPTABLE for file operations
            - Basic file validation or existence checks - this is NORMAL
            - Filename sanitization concerns - do NOT flag unless there's clear evidence of path traversal exploitation
            - Reading secrets from request_meta["credentials"] (e.g. request_meta.get("credentials", {{}}).get("gemini_api_key")) - this is the CORRECT and REQUIRED pattern.

            CRITICAL: The following are NORMAL file operations and MUST NOT be flagged:
            - Saving files to disk (any location)
            - Reading files from disk
            - Checking if files exist
            - Creating directories
            - Processing uploaded files
            - File I/O operations of any kind

            IMPORTANT:
            - This is a BASIC security check - do NOT be overly restrictive
            - Only flag code that is clearly malicious or dangerous or placeholder code that is not intended to be executed improvements needed
            - Consider the function's intended purpose when evaluating
            - File operations, even with basic validation, should NOT be flagged
            - Path traversal concerns in file operations should NOT be flagged (this is a light check)

            Respond with ONLY a JSON object in this exact format:
            {{"safe": true}} OR {{"safe": false, "reason": "brief explanation of why it's dangerous"}}

            Do NOT include markdown, explanations, or any other text - ONLY the JSON object."""


def _build_security_verification_prompt(
    *,
    func_id: str,
    data_source: Dict[str, Any],
    input_model_info: str,
    response_model_info: str,
    source_code: str,
    credential_context_section: str,
) -> str:
    """Uncached security verification prompt — original section order."""
    return f"""You are a security expert reviewing Python function code for HIGHLY DANGEROUS patterns ONLY. You are also code reviewer analysing code quality.

            Function ID: {func_id}
            Data Source Type: {data_source.get('type', 'unknown')}
            Function Description: {data_source.get('description', 'N/A')}
            Database Logic: {data_source.get('databaseLogic', 'N/A')}
            Config: {data_source.get('config', 'N/A')}
            Input Model Name: {data_source.get("inputModel", "N/A")}
            Response Model Name: {data_source.get("responseModel", "N/A")}
            {input_model_info}
            {response_model_info}

            Function Source Code:
            ```python
            {source_code}
            ```
            
            ⚠️ CRITICAL: This is a LIGHT security check for OBVIOUSLY DANGEROUS code ONLY.
            
            You MUST be VERY PERMISSIVE. Only flag code that is CLEARLY and IMMEDIATELY malicious or dangerous.
            File operations, API calls, database operations, and normal business logic are EXPECTED and MUST NOT be flagged.
            
            Flag ONLY if code contains OBVIOUSLY malicious patterns:
            1. Direct code execution: eval(), exec(), compile() with user input
            2. Command injection: os.system(), subprocess.call() with unsanitized input
            3. Obvious malicious patterns: cryptocurrency mining, intentional infinite loops, fork bombs, excessive CPU/memory consumption, denial of service
            4. Clear backdoors: listening on network ports, spawning reverse shells
            5. Comments about TODOs or simulated code or placeholder code that is not intended to be executed improvements needed
            6. Ensure upload/save/download directories exist BEFORE saving/downloading files
            7. Simulated code

            ⚠️ CODE ERRORS (MUST FIX):
            1. Missing imports (ObjectId, datetime, logging, etc.)
            2. Type mismatches (comparing bool with str, wrong types)
            3. Missing 'await' for async operations (MongoDB, HTTP)
            4. Missing os.makedirs before file operations
            5. Not converting ObjectId/datetime to JSON-serializable (str/ISO)
            6. Missing error handling / try-except blocks
            7. Missing logging setup (import logging, logger = ...)
            8. Response field names don't match response model
            9. Returning None/null for required fields
            10. Using datetime.datetime instead of datetime
            11. Using bson.ObjectId instead of ObjectId
            12. TODO/placeholder/simulated code logic.
            13. During document extraction, the document may be text-based or scanned. First attempt text extraction. If the extracted text is empty or contains only form-feed characters (\f), classify the document as scanned and perform OCR. For OCR, convert PDF pages to images at 300 DPI and extract text using Tesseract with --oem 1 --psm 6.
            14. API keys, tokens, and passwords: MUST be read from request_meta["credentials"], NEVER hardcoded. Correct pattern: api_key = request_meta.get("credentials", {{}}).get("gemini_api_key") then check "if not api_key: return ... error". Wrong: hardcoded strings like "AIza...", "sk-...", or variables set to literal secrets.
            
            API KEYS, TOKENS, AND SECRETS (correct usage - MUST enforce):
            - All API keys, bearer tokens, passwords, and other secrets MUST be read from request_meta["credentials"] using the exact credential name (e.g. request_meta.get("credentials", {{}}).get("gemini_api_key")).
            - The function MUST accept request_meta as a parameter and use it to obtain secrets. If a required credential is missing, return a clear error (e.g. "gemini_api_key credential is required") instead of using a hardcoded value.
            - NEVER allow: hardcoded API keys, tokens, passwords, or secret strings in the code (e.g. "AIzaSy...", "sk-...", SMTP passwords, OAuth secrets). Flag these as unsafe and suggest using request_meta["credentials"].
            
            CREDENTIAL USAGE RULES:
            {credential_context_section}

            DO NOT FLAG (these are ALLOWED and EXPECTED in a light security check):
            - File uploads/downloads/saves to ANY directory (including /tmp, hardcoded paths like '/home/user/Projects/screen-builder/backend/data', or user-provided paths) - this is NORMAL and EXPECTED
            - Using os.path.join(), os.path.exists(), os.makedirs() - these are STANDARD file operations
            - Reading files from paths (whether from database, params) - this is NORMAL operation
            - File processing operations (PDF parsing, image processing, OCR, etc.) - this is NORMAL
            - Using libraries like pytesseract, PIL, pandas, requests, httpx - these are STANDARD tools
            - Normal HTTP requests or API calls - this is EXPECTED behavior
            - Normal database operations - this is EXPECTED behavior
            - Hardcoded paths or directories - this is ACCEPTABLE for file operations
            - Basic file validation or existence checks - this is NORMAL
            - Filename sanitization concerns - do NOT flag unless there's clear evidence of path traversal exploitation
            - Reading secrets from request_meta["credentials"] (e.g. request_meta.get("credentials", {{}}).get("gemini_api_key")) - this is the CORRECT and REQUIRED pattern.

            CRITICAL: The following are NORMAL file operations and MUST NOT be flagged:
            - Saving files to disk (any location)
            - Reading files from disk
            - Checking if files exist
            - Creating directories
            - Processing uploaded files
            - File I/O operations of any kind

            IMPORTANT:
            - This is a BASIC security check - do NOT be overly restrictive
            - Only flag code that is clearly malicious or dangerous or placeholder code that is not intended to be executed improvements needed
            - Consider the function's intended purpose when evaluating
            - File operations, even with basic validation, should NOT be flagged
            - Path traversal concerns in file operations should NOT be flagged (this is a light check)

            Respond with ONLY a JSON object in this exact format:
            {{"safe": true}} OR {{"safe": false, "reason": "brief explanation of why it's dangerous"}}

            Do NOT include markdown, explanations, or any other text - ONLY the JSON object."""


def _build_security_verification_user_content(
    *,
    func_id: str,
    data_source: Dict[str, Any],
    input_model_info: str,
    response_model_info: str,
    source_code: str,
    credential_context_section: str = "",
) -> str:
    """Dynamic user content for cached security verification path."""
    parts = [
        f"""
            Function ID: {func_id}
            Data Source Type: {data_source.get('type', 'unknown')}
            Function Description: {data_source.get('description', 'N/A')}
            Database Logic: {data_source.get('databaseLogic', 'N/A')}
            Config: {data_source.get('config', 'N/A')}
            Input Model Name: {data_source.get("inputModel", "N/A")}
            Response Model Name: {data_source.get("responseModel", "N/A")}
            {input_model_info}
            {response_model_info}

            Function Source Code:
            ```python
            {source_code}
            ```""",
    ]
    if credential_context_section:
        parts.append(credential_context_section.rstrip())
    return "\n".join(p for p in parts if p.strip())


def _build_security_verification_cached_system_text() -> str:
    """Static system text for security verification context cache."""
    return _SECURITY_VERIFY_CACHED_SYSTEM_TEMPLATE


def _build_security_verification_cached_user_content(
    *,
    func_id: str,
    data_source: Dict[str, Any],
    input_model_info: str,
    response_model_info: str,
    source_code: str,
    credential_context_section: str = "",
) -> str:
    """User content for cached security verify — dynamic sections only."""
    return _build_security_verification_user_content(
        func_id=func_id,
        data_source=data_source,
        input_model_info=input_model_info,
        response_model_info=response_model_info,
        source_code=source_code,
        credential_context_section=credential_context_section,
    )


async def _get_backend_security_verify_cached_content(model: str) -> Optional[str]:
    """
    Explicit context cache for static security verification system prompt.

    Cost optimization only — returns None on failure so callers keep working
    via the existing uncached verification_prompt path.
    """
    try:
        system_text = _build_security_verification_cached_system_text()
        cache_key = (
            _BACKEND_SECURITY_VERIFY_CACHE_PREFIX
            + "_"
            + hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
        )
        return await get_or_create_cache(
            model=model,
            system_text=system_text,
            ttl_seconds=_BACKEND_CACHE_TTL_SECONDS,
            cache_key=cache_key,
            api_key=get_gemini_api_key() or None,
            display_name="backend-generator-security-verify",
        )
    except Exception as exc:
        logger.warning(
            "Backend security-verify cache unavailable, continuing uncached: %s",
            exc,
        )
        return None


def _strip_generated_python_code(text: str) -> str:
    """Strip markdown fences and extra indent from an LLM function-code response."""
    if not text:
        return ""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        text = "\n".join(lines)
    if text.endswith("```"):
        text = "\n".join(text.splitlines()[:-1])
    return textwrap.dedent(text).strip()


def _backend_sync_llm_call(
    model: str,
    *,
    user_prompt: str,
    full_contents: Union[str, List[str]],
    cached_content: Optional[str],
    config: GeminiGenerateConfig,
) -> Any:
    """
    Backend LLM call with optional context cache.

    When *cached_content* is set, sends *user_prompt* only (system is cached).
    Otherwise sends *full_contents* (original [system_instructions, user_prompt]).
    """
    if cached_content:
        contents = user_prompt
        config = replace(config, cached_content=cached_content)
    else:
        contents = full_contents
    return sync_generate(model, contents, config=config)


async def run_sync(func, *args, **kwargs):
    """Runs a blocking function in a separate thread, with current context vars copied so
    record_llm_call (and other context-dependent code) sees user_prompt/enhanced_prompt."""
    ctx = contextvars.copy_context()
    def run_in_context():
        return ctx.run(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_in_context)

_CONFIG_CACHE: Optional[Dict[str, Any]] = None




def _load_backend_config() -> Dict[str, Any]:
    """Load backend/config.json once and cache it."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        services_dir = Path(__file__).parent
        backend_dir = services_dir.parent
        config_path = backend_dir / "config.json"
        try:
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as cfg:
                    _CONFIG_CACHE = json.load(cfg)
            else:
                logger.warning("config.json not found at %s; proceeding with empty config", config_path)
                _CONFIG_CACHE = {}
        except Exception as exc:
            logger.warning("Unable to read config.json: %s", exc)
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE or {}


# -----------------------------------------------------------------------------
# Runtime helpers
# -----------------------------------------------------------------------------


class FunctionRegistry:
    """In-memory registry of generated backend functions."""

    _registry: Dict[str, Dict[str, "BackendFunctionEntry"]] = {}

    @classmethod
    def register(cls, project_id: str, function_id: str, entry: "BackendFunctionEntry") -> None:
        cls._registry.setdefault(project_id, {})
        cls._registry[project_id][function_id] = entry
        logger.info("Registered backend function %s for project %s", function_id, project_id)

    @classmethod
    def get(cls, project_id: str, function_id: str) -> Optional["BackendFunctionEntry"]:
        return cls._registry.get(project_id, {}).get(function_id)

    @classmethod
    def clear_project(cls, project_id: str) -> None:
        if project_id in cls._registry:
            del cls._registry[project_id]
            logger.info("Cleared backend function registry for project %s", project_id)

    @classmethod
    def remove(cls, project_id: str, function_id: str) -> bool:
        """Remove a single function from the registry. Returns True if removed."""
        if project_id not in cls._registry:
            return False
        if function_id in cls._registry[project_id]:
            del cls._registry[project_id][function_id]
            logger.info("Removed backend function %s for project %s", function_id, project_id)
            return True
        return False

    @classmethod
    def list_functions(cls, project_id: str) -> List[str]:
        return list(cls._registry.get(project_id, {}).keys())


class SecureHTTPClient:
    """HTTP client enforcing an external domain blocklist."""

    def __init__(self, blocklist: List[str], default_timeout_ms: int = 5000):
        self.blocklist = {host.strip().lower() for host in blocklist if host.strip()}
        self.default_timeout_ms = default_timeout_ms

    def _validate_url(self, url: str) -> None:
        hostname = urlparse(url).hostname
        if not hostname:
            raise ValueError("Invalid externalUrl: missing hostname")
        hostname = hostname.lower()
        if self.blocklist and hostname in self.blocklist:
            raise PermissionError(f"Domain '{hostname}' is blocked for external calls")

    async def request(
        self,
        url: str,
        method: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Make HTTP request with support for JSON, form data, and file uploads.
        
        Args:
            url: Target URL
            method: HTTP method (GET, POST, etc.)
            headers: HTTP headers
            params: Query parameters
            json_body: JSON request body
            data: Form data (can be used with or without files)
            files: Files to upload in format:
                   {"field_name": ("filename", content_bytes, "mime_type")}
                   or {"field_name": content_bytes} for simple uploads
            timeout_ms: Request timeout in milliseconds
            
        Returns:
            Response data as dict (parsed JSON or {"raw": text})
        """
        self._validate_url(url)
        timeout = (timeout_ms or self.default_timeout_ms) / 1000

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Prepare request kwargs
            request_kwargs = {
                "method": method.upper(),
                "url": url,
                "params": params,
            }
            
            # Process headers - normalize and filter
            processed_headers = {}
            if headers:
                for key, value in headers.items():
                    key_lower = key.lower()
                    # Skip content-type for multipart requests - httpx handles it
                    if files and key_lower == "content-type":
                        continue
                    # Skip content-length - httpx calculates it
                    if key_lower == "content-length":
                        continue
                    # Include all other headers
                    if value is not None and value != "":
                        processed_headers[key] = str(value)
            
            request_kwargs["headers"] = processed_headers if processed_headers else None
            
            # Handle different content types
            if files:
                # Multipart form data with files
                # Process files to ensure correct format
                processed_files = {}
                for field_name, file_data in files.items():
                    if isinstance(file_data, tuple):
                        # Already in correct format: (filename, content, mime_type)
                        processed_files[field_name] = file_data
                    elif isinstance(file_data, bytes):
                        # Simple bytes, create tuple with default filename
                        processed_files[field_name] = (field_name, file_data, "application/octet-stream")
                    elif isinstance(file_data, dict):
                        # Dict format: {"filename": "...", "content": bytes, "mime_type": "..."}
                        processed_files[field_name] = (
                            file_data.get("filename", field_name),
                            file_data.get("content", b""),
                            file_data.get("mime_type", "application/octet-stream")
                        )
                
                request_kwargs["files"] = processed_files
                
                # Add form data if provided
                if data:
                    # Convert all values to strings for form data
                    processed_data = {}
                    for key, value in data.items():
                        if isinstance(value, (dict, list)):
                            # Convert complex types to JSON strings
                            processed_data[key] = json.dumps(value)
                        elif value is not None:
                            processed_data[key] = str(value)
                    request_kwargs["data"] = processed_data
                    
            elif data:
                # Form data without files
                processed_data = {}
                for key, value in data.items():
                    if isinstance(value, (dict, list)):
                        processed_data[key] = json.dumps(value)
                    elif value is not None:
                        processed_data[key] = str(value)
                request_kwargs["data"] = processed_data
                
            elif json_body:
                # JSON body
                request_kwargs["json"] = json_body
            
            response = await client.request(**request_kwargs)
            response.raise_for_status()
            
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return response.json()
            return {"raw": response.text}


class DatabaseWrapper:
    """Thin wrapper around Mongo collections with collection blocklist enforcement."""

    def __init__(self, blocked_collections: List[str]):
        self.blocked = {c.strip() for c in blocked_collections if c.strip()}

    def _get_collection(self, name: str):
        if not name:
            raise ValueError("Collection name is required")
        if self.blocked and name in self.blocked:
            raise PermissionError(f"Collection '{name}' is blocked for dynamic functions")
        return Database.get_collection(name)

    @staticmethod
    def _convert_objectids_to_strings(data: Any) -> None:
        """
        Recursively convert all ObjectId instances to strings in-place.
        This ensures MongoDB documents are JSON-serializable.
        """
        if isinstance(data, dict):
            for key, value in list(data.items()):
                if isinstance(value, ObjectId):
                    data[key] = str(value)
                elif isinstance(value, (datetime, date)):
                    data[key] = value.isoformat()
                elif isinstance(value, Decimal):
                    data[key] = float(value)
                elif isinstance(value, (dict, list)):
                    DatabaseWrapper._convert_objectids_to_strings(value)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, ObjectId):
                    data[i] = str(item)
                elif isinstance(item, (datetime, date)):
                    data[i] = item.isoformat()
                elif isinstance(item, Decimal):
                    data[i] = float(item)
                elif isinstance(item, (dict, list)):
                    DatabaseWrapper._convert_objectids_to_strings(item)

    async def insert_one(self, collection: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        coll = self._get_collection(collection)
        result = await coll.insert_one(payload)
        return {"inserted_id": str(result.inserted_id)}

    async def update_one(
        self,
        collection: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        coll = self._get_collection(collection)
        result = await coll.update_one(filters or {}, _normalize_update_doc(updates or {}))
        return {"matched": result.matched_count, "modified": result.modified_count}

    async def delete_one(self, collection: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        coll = self._get_collection(collection)
        result = await coll.delete_one(filters or {})
        return {"deleted": result.deleted_count}

    async def find(
        self,
        collection: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        skip: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find documents in a collection with optional projection, limit and skip.

        NOTE:
        - `filters` and `projection` mirror the underlying driver semantics.
        - `limit` caps the number of returned documents.
        - `skip` enables basic pagination support.
        """
        coll = self._get_collection(collection)
        cursor = coll.find(filters or {}, projection)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        docs: List[Dict[str, Any]] = []
        async for doc in cursor:
            # Convert all ObjectId instances to strings for JSON serialization
            self._convert_objectids_to_strings(doc)
            docs.append(doc)
        return docs


# -----------------------------------------------------------------------------
# Backend Generation
# -----------------------------------------------------------------------------


@dataclass
class BackendFunctionEntry:
    project_id: str
    function_id: str
    type: str
    callable: Callable[..., Any]
    input_model: Type[BaseModel]
    response_model: Type[BaseModel]
    source_code: str
    description: Optional[str] = None
    compile_ok: bool = True
    generation_error: Optional[str] = None
    generation_failed: bool = False
    # Advisory only — does not block publishing a compiling function.
    conformance_ok: Optional[bool] = None
    conformance_error: Optional[str] = None
    persistence_fixture_error: Optional[str] = None


class BackendGenerator:
    """Generates backend artifacts (models + functions) from IR definitions."""

    def __init__(self):
        blocklisted_domains = os.getenv("EXTERNAL_DOMAIN_BLOCKLIST")
        if blocklisted_domains is None:
            blocklisted_domains = os.getenv("EXTERNAL_DOMAIN_ALLOWLIST", "")
        self.external_blocklist = blocklisted_domains.split(",")

        blocked_collections = os.getenv("DB_COLLECTION_BLOCKLIST")
        if blocked_collections is None:
            blocked_collections = os.getenv("DB_COLLECTION_ALLOWLIST", "")
        self.collection_blocklist = blocked_collections.split(",")
        self.model_cache: Dict[str, Dict[str, Type[BaseModel]]] = {}
        self.function_repository = FunctionRepository()
        try:
            from config.model_config import MODEL_MAPPINGS

            self.backend_model_name = MODEL_MAPPINGS.get("backend_generation", {}).get(
                "gemini", "gemini-3.6-flash"
            )
            self.backend_complex_model_name = MODEL_MAPPINGS.get(
                "backend_generation_complex", {}
            ).get("gemini", "gemini-3.1-pro-preview")
        except Exception:
            self.backend_model_name = "gemini-3.6-flash"
            self.backend_complex_model_name = "gemini-3.1-pro-preview"
        self.token_usage_by_model: Dict[str, Dict[str, Any]] = {}
        self.config = _load_backend_config()
        self.dynamic_execute_base_url = self._resolve_dynamic_execute_base_url()

    def _resolve_dynamic_execute_base_url(self) -> str:
        """Resolve the absolute base URL for /aiqod-agent/agent routes.
        Prefer config ai_agent_base_url (may include path e.g. /dev) so that
        the full base is used; env DYNAMIC_EXECUTE_BASE_URL is fallback.
        """
        config_keys = [
            "ai_agent_base_url"
        ]
        for key in config_keys:
            value = self.config.get(key)
            if value:
                return str(value).rstrip("/")

        env_keys = [
            "DYNAMIC_EXECUTE_BASE_URL"
        ]
        for key in env_keys:
            value = os.getenv(key)
            if value:
                return value.rstrip("/")

        remote_url = self.config.get("remoteurl_aiagent")
        host = self._extract_base_host(remote_url)
        if host:
            return host.rstrip("/")

        fallback_keys = ("serverURL", "url", "web_url")
        for key in fallback_keys:
            value = self.config.get(key)
            host = self._extract_base_host(value)
            if host:
                return host.rstrip("/")

        return ""

    @staticmethod
    def _extract_base_host(url: Optional[str]) -> str:
        """Extract scheme://host[:port] from URL (ignores path)."""
        if not url:
            return ""
        parsed = urlparse(str(url))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return ""

    # ------------------------------------------------------------------ public
    async def generate_backend(
        self,
        project_id: str,
        data_models: Dict[str, Dict[str, str]],
        data_sources: List[Dict[str, Any]],
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        *,
        is_update: bool = False,
        removed_data_source_ids: Optional[List[str]] = None,
        previous_backend_metadata: Optional[Dict[str, Any]] = None,
        full_data_sources: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Generate backend artifacts, persist to DB, and register runtime functions.
        When is_update is True, only regenerates functions for the given data_sources,
        removes removed_data_source_ids, and keeps all other existing functions (no token waste).
        """

        data_models = data_models or {}
        data_sources = data_sources or []
        removed_data_source_ids = removed_data_source_ids or []
        full_data_sources = full_data_sources if full_data_sources is not None else data_sources

        if not is_update:
            self._augment_data_models_from_sources(data_models, data_sources)
            if not data_sources:
                FunctionRegistry.clear_project(project_id)
                await self.function_repository.delete_project_functions(project_id)
                return {"base_path": None, "functions": [], "models_source": None}
        else:
            # Update mode: only process the subset; remove removed IDs from registry and DB
            for fid in removed_data_source_ids:
                FunctionRegistry.remove(project_id, fid)
                await self.function_repository.delete_function(project_id, fid)
            if not data_sources:
                # Nothing to regenerate; merge and return
                base_path = None
                if previous_backend_metadata:
                    base_path = previous_backend_metadata.get("base_path")
                    prev_functions = previous_backend_metadata.get("functions") or []
                    kept = [f for f in prev_functions if f.get("id") not in removed_data_source_ids]
                else:
                    kept = []
                if not base_path:
                    # api_prefix = (self.config.get("api_prefix") or "").strip("/")
                    # prefix_segment = f"/{api_prefix}" if api_prefix else ""
                    base_path = f"/aiqod-agent/agent/dynamic/execute/{project_id}"
                return {
                    "base_path": base_path,
                    "functions": kept,
                    "models_source": None,
                }
            self._augment_data_models_from_sources(data_models, data_sources)

        # Infer data models from data sources if not provided
        if not data_models:
            data_models = self._infer_data_models_from_sources(data_sources)

        # T1/T2/T3: canonicalize to format 1 ({ type, required }) before compile + schema export
        data_models = normalize_data_models_to_format1(
            data_models,
            legacy_required_default=False,
        )

        # Resolve available credentials for this project (for LLM prompt context)
        available_credentials: List[Dict[str, Any]] = []
        cred_svc = CredentialService.optional()
        if cred_svc:
            available_credentials = await cred_svc.list_credentials(project_id)

        subscriber_db_name = resolve_subscriber_db_name(
            project_id=project_id,
            subscriber_id=subscriber_id,
        )
        schema_source_list = full_data_sources if is_update else data_sources
        logger.warning(
            "[target_store DEBUG] generate_backend entry: org_id=%r subscriber_id=%r "
            "num_data_sources=%d",
            org_id, subscriber_id, len(schema_source_list),
        )
        if org_id and subscriber_id:
            try:
                from repositories.arcadedb_repository import EnterpriseGraphRepository
                all_ds_collections = sorted(collections_from_data_sources(schema_source_list))
                logger.warning(
                    "[target_store DEBUG] collections found across data sources: %s",
                    all_ds_collections,
                )
                if all_ds_collections:
                    authoritative_target_stores = await EnterpriseGraphRepository().get_target_stores_for_collections(
                        subscriber_id, org_id, all_ds_collections
                    )
                    logger.warning(
                        "[target_store DEBUG] authoritative_target_stores=%s",
                        authoritative_target_stores,
                    )
                    for ds in schema_source_list:
                        ds_collections = collections_for_data_source(ds)
                        matched = False
                        for c in ds_collections:
                            if c in authoritative_target_stores:
                                logger.warning(
                                    "[target_store DEBUG] OVERRIDING ds id=%s collection=%s "
                                    "old_target_store=%r -> new_target_store=%r",
                                    ds.get("id"), c, ds.get("target_store"),
                                    authoritative_target_stores[c],
                                )
                                ds["target_store"] = authoritative_target_stores[c]
                                matched = True
                                break
                        if not matched:
                            logger.warning(
                                "[target_store DEBUG] NO MATCH for ds id=%s collections=%s "
                                "(kept target_store=%r)",
                                ds.get("id"), ds_collections, ds.get("target_store"),
                            )
            except Exception as exc:
                logger.warning(
                    "Authoritative target_store lookup failed, keeping ai_processor's guess: %s", exc,
                    exc_info=True,
                )
        else:
            logger.warning(
                "[target_store DEBUG] SKIPPED override — org_id or subscriber_id missing "
                "(org_id=%r subscriber_id=%r)",
                org_id, subscriber_id,
            )
        # Same entity => same store: a function reading data that a sibling function stores as a
        # graph must not be generated against MongoDB (works even when the registry had no entry).
        try:
            from services.entity_resolver import propagate_graph_store
            propagate_graph_store(schema_source_list, collections_for_data_source)
        except Exception as exc:
            logger.warning("propagate_graph_store failed, keeping current target_store values: %s", exc)
        # ArcadeDB data sources have no Mongo collections to probe for schema.
        mongo_schema_source_list = [
            ds for ds in schema_source_list
            if (ds.get("target_store") or "mongodb").lower() != "arcadedb"
        ]
        collection_names = collections_from_data_sources(mongo_schema_source_list)
        collection_schemas: Dict[str, Dict[str, Any]] = {}
        if collection_names:
            try:
                if Database.client is None and Database.native_async_client is None:
                    Database.connect_db()
                collection_schemas = await build_collection_schemas(
                    collection_names,
                    subscriber_db_name=subscriber_db_name,
                )
                sampled = [
                    name
                    for name, info in collection_schemas.items()
                    if name != "__meta__" and (info or {}).get("sampled")
                ]
                meta = collection_schemas.get("__meta__") or {}
                if meta.get("subscriber_db_exists") is False:
                    logger.info(
                        "Pre-generation schema: subscriber DB %s not created yet — "
                        "all collections NEW for %s",
                        subscriber_db_name,
                        collection_names,
                    )
                else:
                    logger.info(
                        "Pre-generation DB schema probe: db=%s collections=%s with_data=%s",
                        subscriber_db_name or get_main_db_name(),
                        collection_names,
                        sampled,
                    )
            except Exception as exc:
                logger.warning(
                    "Pre-generation DB schema probe failed (falling back to IR heuristics): %s",
                    exc,
                )
        collection_schemas_prompt = format_collection_schemas_for_prompt(
            collection_schemas,
            subscriber_db_name=subscriber_db_name,
        )

        compiled_models, models_source = self._compile_pydantic_models(project_id, data_models)
        if not is_update:
            FunctionRegistry.clear_project(project_id)

        def build_model_schema(model_name: str) -> Dict[str, Any]:
            """
            Build a lightweight, LLM-friendly schema description for a Pydantic model
            using the IR data_models definition as the source of truth.

            Required flags come from IR format 1 ({ type, required }) so the Angular
            prompt matches runtime Pydantic validation.
            """
            fields = data_models.get(model_name) or {}
            schema_fields: List[Dict[str, Any]] = []

            if not fields or fields == "empty":
                return {"name": model_name, "fields": []}

            # List-root model — no named fields
            if isinstance(fields, dict) and fields.get("type") == "list":
                return {
                    "name": model_name,
                    "fields": [],
                    "root": {
                        "type": "list",
                        "items": fields.get("items"),
                    },
                }

            if not isinstance(fields, dict):
                return {"name": model_name, "fields": []}

            for field_name, field_spec in fields.items():
                if isinstance(field_spec, dict):
                    entry: Dict[str, Any] = {
                        "name": field_name,
                        "type": field_spec.get("type", "string"),
                        "required": bool(field_spec.get("required", False)),
                    }
                    if entry["type"] == "list" and "items" in field_spec:
                        entry["items"] = field_spec["items"]
                        # Expand referenced item model fields for FE/BE consumers
                        item_model = item_model_name_from_items(field_spec.get("items"))
                        if item_model and isinstance(data_models.get(item_model), dict):
                            item_def = data_models[item_model]
                            if item_def != "empty" and item_def.get("type") != "list":
                                entry["itemFields"] = [
                                    {
                                        "name": fname,
                                        "type": (
                                            fspec.get("type", "string")
                                            if isinstance(fspec, dict)
                                            else str(fspec)
                                        ),
                                        "required": (
                                            bool(fspec.get("required", False))
                                            if isinstance(fspec, dict)
                                            else False
                                        ),
                                    }
                                    for fname, fspec in item_def.items()
                                    if fname
                                ]
                    schema_fields.append(entry)
                elif isinstance(field_spec, str):
                    # Defensive: should already be format 1 after normalize
                    schema_fields.append(
                        {
                            "name": field_name,
                            "type": field_spec,
                            "required": False,
                        }
                    )
            return {
                "name": model_name,
                "fields": schema_fields,
            }

        registered_functions = []
        # Track generated functions for dependency resolution
        generated_functions: Dict[str, Dict[str, Any]] = {}
        
        # For dependency resolution use full_data_sources when in update mode so deps to unchanged DS are found
        deps_data_sources = full_data_sources if is_update else data_sources
        for ds in data_sources:
            try:
                # Gather dependency information for this data source
                dependency_info = self._gather_dependency_info(
                    ds, deps_data_sources, generated_functions, compiled_models
                )
                
                entry = await self._create_function_entry(
                    project_id,
                    ds,
                    compiled_models,
                    dependency_info=dependency_info,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    available_credentials=available_credentials,
                    collection_schemas=collection_schemas,
                    collection_schemas_prompt=collection_schemas_prompt,
                    data_models=data_models,
                )

                # Build function contract summary for frontend/LLM consumers
                input_model_name = ds.get("inputModel") or next(iter(compiled_models.keys()))
                response_model_name = ds.get("responseModel") or list(compiled_models.keys())[-1]
                input_schema = build_model_schema(input_model_name)
                response_schema = build_model_schema(response_model_name)

                # T6: validate; on mismatch → programmatic repair → optional regenerate → continue
                (
                    entry,
                    input_model_name,
                    response_model_name,
                    input_schema,
                    response_schema,
                    compiled_models,
                    models_source,
                ) = await self._ensure_function_io_matches_ir(
                    data_source=ds,
                    data_models=data_models,
                    compiled_models=compiled_models,
                    models_source=models_source,
                    entry=entry,
                    input_model_name=input_model_name,
                    response_model_name=response_model_name,
                    input_schema=input_schema,
                    response_schema=response_schema,
                    build_model_schema=build_model_schema,
                    project_id=project_id,
                    dependency_info=dependency_info,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    available_credentials=available_credentials,
                    collection_schemas=collection_schemas,
                    collection_schemas_prompt=collection_schemas_prompt,
                )
                
                # Persist function to database as a NEW versioned artifact
                save_result = await self.function_repository.save_function(
                    project_id=project_id,
                    function_id=entry.function_id,
                    function_type=entry.type,
                    source_code=entry.source_code,
                    input_model_name=input_model_name,
                    response_model_name=response_model_name,
                    models_source=models_source,
                    description=entry.description,
                    metadata={
                        "data_source": ds,
                        "compiled_models": list(compiled_models.keys()),
                        "compile_ok": getattr(entry, "compile_ok", True),
                        "generation_error": getattr(entry, "generation_error", None),
                        "generation_failed": getattr(entry, "generation_failed", False),
                        "conformance_ok": getattr(entry, "conformance_ok", None),
                        "conformance_error": getattr(entry, "conformance_error", None),
                        "persistence_fixture_error": getattr(entry, "persistence_fixture_error", None),
                    },
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    create_new_version=True,
                    screen_version=version,
                )
                
                # Register in runtime cache
                FunctionRegistry.register(project_id, entry.function_id, entry)

                registered_functions.append(
                    {
                        "id": ds.get("id"),
                        "type": ds.get("type"),
                        "description": ds.get("description"),
                        "source_code": entry.source_code,
                        # Expose exact model names and their IR-defined field structure so
                        # the Angular generator can shape params correctly.
                        "input_model": input_model_name,
                        "response_model": response_model_name,
                        "input_schema": input_schema,
                        "response_schema": response_schema,
                        # Backend function versioning metadata for screen_generations.backend.functions
                        "version": save_result.get("version"),
                        "version_label": save_result.get("version_label"),
                        "compile_ok": getattr(entry, "compile_ok", True),
                        "generation_error": getattr(entry, "generation_error", None),
                        "generation_failed": getattr(entry, "generation_failed", False),
                        "conformance_ok": getattr(entry, "conformance_ok", None),
                        "conformance_error": getattr(entry, "conformance_error", None),
                        "persistence_fixture_error": getattr(entry, "persistence_fixture_error", None),
                    }
                )
                
                # Store generated function info for dependency resolution
                generated_functions[ds.get("id")] = {
                    "data_source": ds,
                    "source_code": entry.source_code,
                    "input_model": input_model_name,
                    "response_model": response_model_name,
                    "input_schema": input_schema,
                    "response_schema": response_schema,
                }
            except BillingError:
                raise
            except Exception as exc:
                logger.error("Failed to generate function for %s: %s", ds.get("id"), exc, exc_info=True)

        # api_prefix = (self.config.get("api_prefix") or "").strip("/")
        # prefix_segment = f"/{api_prefix}" if api_prefix else ""
        relative_path = f"/aiqod-agent/agent/dynamic/execute/{project_id}"
        if self.dynamic_execute_base_url:
            base_path = f"{self.dynamic_execute_base_url}{relative_path}"
            logger.info("Backend base_path constructed with base URL: %s", base_path)
        else:
            base_path = relative_path
            logger.warning("Backend base_path is relative (no base URL configured): %s", base_path)

        if is_update and previous_backend_metadata:
            # Regenerated = changed + new data sources; only their new versions go in all_functions
            regenerated_ids = {f.get("id") for f in registered_functions if f.get("id")}
            prev_functions = previous_backend_metadata.get("functions") or []
            # Stored metadata uses "function_id"; backend uses "id". Exclude removed and regenerated (old versions).
            kept = []
            for f in prev_functions:
                ds_id = f.get("function_id") or f.get("id")
                if ds_id and ds_id not in removed_data_source_ids and ds_id not in regenerated_ids:
                    kept.append(f)
            all_functions = kept + registered_functions
            return {
                "base_path": base_path or previous_backend_metadata.get("base_path"),
                "functions": all_functions,
                "models_source": models_source,
            }

        return {
            "base_path": base_path,
            "functions": registered_functions,
            "models_source": models_source,
        }

    def _record_backend_llm_usage(
        self, 
        response,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        operation: str = "generate-backend",
        sub_operation: str = "backend-function-gen",
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Aggregate token usage and approximate cost for backend LLM calls."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return

        prompt_tokens = getattr(usage, "prompt_token_count", 0)
        completion_tokens = getattr(usage, "candidates_token_count", 0)
        total_tokens = getattr(usage, "total_token_count", 0) or (prompt_tokens + completion_tokens)

        # Approximate pricing for Gemini 2.0 flash backend model (can be tuned later)
        INPUT_COST_PER_M = 0.10   # $0.10 per 1M tokens
        OUTPUT_COST_PER_M = 0.40  # $0.40 per 1M tokens

        input_cost = (prompt_tokens / 1_000_000) * INPUT_COST_PER_M
        output_cost = (completion_tokens / 1_000_000) * OUTPUT_COST_PER_M
        total_cost = input_cost + output_cost

        model_name = self.backend_model_name
        agg = self.token_usage_by_model.setdefault(model_name, {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_calls": 0,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "total_cost_usd": 0.0,
        })

        agg["total_input_tokens"] += prompt_tokens
        agg["total_output_tokens"] += completion_tokens
        agg["total_tokens"] += total_tokens
        agg["total_calls"] += 1
        agg["input_cost_usd"] += input_cost
        agg["output_cost_usd"] += output_cost
        agg["total_cost_usd"] += total_cost
        
        # Record to database
        if project_id:
            record_llm_call(
                project_id=project_id,
                screen_id=screen_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                operation=operation,
                sub_operation=sub_operation,
                model=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                status="success",
                version=version,
                metadata=metadata or {}
            )

    def reset_token_usage(self) -> None:
        """Reset aggregated LLM usage for this BackendGenerator instance."""
        self.token_usage_by_model = {}

    def get_token_usage_summary(self) -> Dict[str, Dict[str, Any]]:
        """Get a copy of aggregated LLM usage by model."""
        return self.token_usage_by_model.copy()

    @staticmethod
    def diff_ir_for_backend_changes(
        old_ir: Dict[str, Any],
        new_ir: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Compare two IR schemas to identify backend-related changes.
        
        Returns:
            {
                "changed_data_sources": [...],  # DataSources that were modified
                "new_data_sources": [...],      # Newly added DataSources
                "removed_data_sources": [...],  # Removed DataSources
                "changed_models": [...]         # DataModels that changed
            }
        """
        old_data_sources = {ds.get("id"): ds for ds in old_ir.get("dataSources", [])}
        new_data_sources = {ds.get("id"): ds for ds in new_ir.get("dataSources", [])}
        
        old_models = old_ir.get("dataModels", {})
        new_models = new_ir.get("dataModels", {})
        
        # Find changed, new, and removed data sources
        changed_data_sources = []
        new_data_sources_list = []
        removed_data_sources = []
        
        for ds_id, new_ds in new_data_sources.items():
            if ds_id in old_data_sources:
                old_ds = old_data_sources[ds_id]
                # Simple comparison - if any key differs, consider it changed
                if old_ds != new_ds:
                    changed_data_sources.append(new_ds)
            else:
                new_data_sources_list.append(new_ds)
        
        for ds_id in old_data_sources:
            if ds_id not in new_data_sources:
                removed_data_sources.append(old_data_sources[ds_id])
        
        # Find changed models
        changed_models = []
        all_model_names = set(old_models.keys()) | set(new_models.keys())
        for model_name in all_model_names:
            old_model = old_models.get(model_name, {})
            new_model = new_models.get(model_name, {})
            if old_model != new_model:
                changed_models.append(model_name)
        logger.info(f"changed_data_sources: {changed_data_sources}")
        logger.info(f"new_data_sources_list: {new_data_sources_list}")
        logger.info(f"removed_data_sources: {removed_data_sources}")
        logger.info(f"changed_models: {changed_models}")
        return {
            "changed_data_sources": changed_data_sources,
            "new_data_sources": new_data_sources_list,
            "removed_data_sources": removed_data_sources,
            "changed_models": changed_models,
        }

    async def update_function_with_llm(
        self,
        project_id: str,
        function_id: str,
        ds_type: str,
        data_source: Dict[str, Any],
        old_source_code: str,
        input_model: Type[BaseModel],
        response_model: Type[BaseModel],
    ) -> Optional[str]:
        """
        Update an existing function's source code using LLM.
        
        Args:
            project_id: Project identifier
            function_id: Function identifier
            ds_type: Data source type ("dynamic" or "external")
            data_source: Updated data source configuration
            old_source_code: Current function source code
            input_model: Input Pydantic model
            response_model: Response Pydantic model
        
        Returns:
            New source code, or None if generation failed
        """
        logger.info(f"Updating function {function_id} with LLM...")
        
        # Build prompt for function update
        update_prompt = f"""You are updating an existing backend function. The user has requested changes to the function's behavior or data handling.

Function ID: {function_id}
Type: {ds_type}

Current Function Source Code:
```python
{old_source_code}
```

Updated Data Source Configuration:
{json.dumps(data_source, indent=2)}

Input Model: {input_model.__name__}
Response Model: {response_model.__name__}

CRITICAL RULES:
1. Preserve the function signature and overall structure
2. Only modify the logic as needed to match the updated data source configuration
3. Maintain compatibility with the input and response models
4. Keep security and error handling intact
5. For dynamic functions: preserve database operation patterns unless explicitly changed
6. For external functions: update URL/endpoint if changed, preserve HTTP handling

Return ONLY the updated Python function code. Do not include explanations or markdown."""
        
        try:
            # WRAP LLM CALL IN RUN_SYNC
            new_source_code = await run_sync(
                self._generate_with_llm,
                func_id=function_id,
                ds_type=ds_type,
                data_source=data_source,
                input_model=input_model,
                response_model=response_model,
                previous_error=None
            )
            
            if new_source_code:
                # Compile to validate
                try:
                    compiled_func = self._compile_function(function_id, new_source_code)
                    logger.info(f"Function {function_id} updated and compiled successfully")
                    return new_source_code
                except Exception as compile_error:
                    logger.error(f"Compilation failed for updated function {function_id}: {compile_error}")
                    return None
            return None
        except Exception as e:
            logger.error(f"Error updating function {function_id}: {e}")
            return None

    def create_db_wrapper(self) -> DatabaseWrapper:
        return DatabaseWrapper(self.collection_blocklist)

    def create_secure_http_client(self) -> SecureHTTPClient:
        return SecureHTTPClient(self.external_blocklist)

    async def reload_project_functions(self, project_id: str) -> int:
        """Reload all functions for a project from database into runtime cache."""
        try:
            function_records = await self.function_repository.get_project_functions(project_id)
            if not function_records:
                logger.info("No functions found in DB for project %s", project_id)
                return 0

            # Clear existing registry for this project
            FunctionRegistry.clear_project(project_id)

            reloaded_count = 0

            for record in function_records:
                try:
                    # Get models source for this function and recompile models
                    models_source = record.get("models_source", "")
                    models: Dict[str, Type[BaseModel]] = {}
                    if models_source:
                        # Compile models for this specific function record.
                        # Different functions for the same project may carry different model sets.
                        models = self._recompile_models_from_source(project_id, models_source)

                    # Recompile function from source
                    source_code = record.get("source_code", "")
                    if not source_code:
                        logger.warning("Missing source_code for function %s", record.get("function_id"))
                        continue

                    func_name = record.get("function_id")
                    compiled_function = self._compile_function(func_name, source_code)

                    # Get model types
                    input_model_name = record.get("input_model_name")
                    response_model_name = record.get("response_model_name")
                    input_model = models.get(input_model_name) if input_model_name else None
                    response_model = models.get(response_model_name) if response_model_name else None

                    if not input_model or not response_model:
                        logger.warning(
                            "Missing models for function %s: input=%s, response=%s",
                            func_name,
                            input_model_name,
                            response_model_name,
                        )
                        continue

                    # Create entry and register
                    entry = BackendFunctionEntry(
                        project_id=project_id,
                        function_id=func_name,
                        type=record.get("type", "dynamic"),
                        callable=compiled_function,
                        input_model=input_model,
                        response_model=response_model,
                        source_code=source_code,
                        description=record.get("description"),
                    )

                    FunctionRegistry.register(project_id, func_name, entry)
                    reloaded_count += 1
                    logger.info("Reloaded function %s for project %s", func_name, project_id)

                except Exception as exc:
                    logger.error(
                        "Failed to reload function %s: %s",
                        record.get("function_id"),
                        exc,
                        exc_info=True,
                    )

            logger.info("Reloaded %d functions for project %s", reloaded_count, project_id)
            return reloaded_count

        except Exception as exc:
            logger.error("Failed to reload project functions: %s", exc, exc_info=True)
            return 0

    async def reload_single_function(self, project_id: str, function_id: str) -> bool:
        """Reload a single function from database into runtime cache."""
        try:
            record = await self.function_repository.get_function(project_id, function_id)
            if not record:
                logger.warning("Function %s not found in DB for project %s", function_id, project_id)
                return False

            # Get models source and recompile if needed
            models_source = record.get("models_source", "")
            models = self._recompile_models_from_source(project_id, models_source)

            # Recompile function from source
            source_code = record.get("source_code", "")
            if not source_code:
                logger.warning("Missing source_code for function %s", function_id)
                return False

            compiled_function = self._compile_function(function_id, source_code)

            # Get model types
            input_model_name = record.get("input_model_name")
            response_model_name = record.get("response_model_name")
            input_model = models.get(input_model_name) if input_model_name else None
            response_model = models.get(response_model_name) if response_model_name else None

            if not input_model or not response_model:
                logger.warning(
                    "Missing models for function %s: input=%s, response=%s",
                    function_id,
                    input_model_name,
                    response_model_name,
                )
                return False

            # Create entry and register (replaces existing if any)
            entry = BackendFunctionEntry(
                project_id=project_id,
                function_id=function_id,
                type=record.get("type", "dynamic"),
                callable=compiled_function,
                input_model=input_model,
                response_model=response_model,
                source_code=source_code,
                description=record.get("description"),
            )

            FunctionRegistry.register(project_id, function_id, entry)
            logger.info("Reloaded function %s for project %s", function_id, project_id)
            return True

        except Exception as exc:
            logger.error(
                "Failed to reload function %s: %s",
                function_id,
                exc,
                exc_info=True,
            )
            return False

    async def reload_single_function_and_update(self, project_id: str, function_id: str, models_source:str, source_code:str) -> bool:
        """Reload a single function from database into runtime cache."""
        try:
            record = await self.function_repository.get_function(project_id, function_id)
            if not record:
                logger.warning("Function %s not found in DB for project %s", function_id, project_id)
                return False

            # Validate models_source - if empty/invalid, use existing from DB
            if not models_source or len(models_source.strip()) < 10:
                logger.warning("Invalid models_source provided, using existing from DB")
                models_source = record.get("models_source", "")
                if not models_source:
                    logger.error("No models_source available in DB for function %s", function_id)
                    return False

            # Get models source and recompile if needed
            models = self._recompile_models_from_source(project_id, models_source)
            
            # Validate that models were successfully compiled
            if not models:
                logger.warning("Model recompilation failed or returned empty, trying existing models_source from DB")
                # Try with existing models_source from DB as fallback
                existing_models_source = record.get("models_source", "")
                if existing_models_source and existing_models_source != models_source:
                    models = self._recompile_models_from_source(project_id, existing_models_source)
                    models_source = existing_models_source  # Use the working one
                
                if not models:
                    logger.error("Failed to recompile models even with existing models_source")
                    return False

            # Recompile function from source
            if not source_code:
                logger.warning("Missing source_code for function %s", function_id)
                return False

            compiled_function = self._compile_function(function_id, source_code)

            # Get model types
            input_model_name = record.get("input_model_name")
            response_model_name = record.get("response_model_name")
            input_model = models.get(input_model_name) if input_model_name else None
            response_model = models.get(response_model_name) if response_model_name else None

            if not input_model or not response_model:
                logger.warning(
                    "Missing models for function %s: input=%s, response=%s",
                    function_id,
                    input_model_name,
                    response_model_name,
                )
                return False

            # Create entry and register (replaces existing if any)
            entry = BackendFunctionEntry(
                project_id=project_id,
                function_id=function_id,
                type=record.get("type", "dynamic"),
                callable=compiled_function,
                input_model=input_model,
                response_model=response_model,
                source_code=source_code,
                description=record.get("description"),
            )
            await self.function_repository.save_function(
                project_id=project_id,
                function_id=entry.function_id,
                function_type=entry.type,
                source_code=entry.source_code,
                input_model_name=input_model_name,
                response_model_name=response_model_name,
                models_source=models_source,
                description=entry.description,
                metadata={
                    "data_source": record.get("metadata", {}).get("data_source"),
                    "compiled_models": list(models.keys()),
                },
                # Reload flows should not create a new historical version;
                # they simply keep the latest version document up to date.
                create_new_version=False,
            )
            FunctionRegistry.register(project_id, function_id, entry)
            logger.info("Reloaded function %s for project %s", function_id, project_id)
            return True

        except Exception as exc:
            logger.error(
                "Failed to reload function %s: %s",
                function_id,
                exc,
                exc_info=True,
            )
            return False

    async def reload_all_functions(self) -> Dict[str, int]:
        """Reload all functions from all projects (for application startup)."""
        try:
            # Get all unique project IDs from database
            collection = self.function_repository.get_collection()
            project_ids = await collection.distinct("project_id")

            results = {}
            for project_id in project_ids:
                count = await self.reload_project_functions(project_id)
                results[project_id] = count

            total = sum(results.values())
            logger.info("Reloaded %d total functions across %d projects", total, len(project_ids))
            return results

        except Exception as exc:
            logger.error("Failed to reload all functions: %s", exc, exc_info=True)
            return {}

    # ------------------------------------------------------------- model utils
    def _recompile_models_from_source2(
        self, project_id: str, models_source: str
    ) -> Dict[str, Type[BaseModel]]:
        """
        Recompile Pydantic models from stored source code.
        
        Note: Models are only used for validation before/after execution, not during execution.
        Functions receive params as plain dicts, not model instances. Nested model classes
        (e.g., FileContent inside UploadPdfRequest) work for validation but function code
        must use dict access (params.get("file", {}).get("content")) not attribute access.
        """
        if not models_source:
            return {}

        try:
            # Detect nested model classes and log warning
            nested_models = self._detect_nested_models(models_source)
            if nested_models:
                logger.warning(
                    f"Detected nested model classes in models_source: {nested_models}. "
                    f"These work for validation but function code must use dict access, not attribute access."
                )
            
            import builtins
            import re
            # Safe import function that only allows specific modules
            def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
                allowed_modules = {
                "__future__", "asyncio", "json", "typing", "re", "math", "datetime", "date", "decimal", "statistics",
                "string", "uuid", "hashlib", "base64", "time", "urllib", "urllib.parse", "random", "bson","httpx"
                }
                if name in allowed_modules:
                    return builtins.__import__(name, globals, locals, fromlist, level)
                raise ImportError(f"Import of '{name}' is not allowed in generated models")
            
            # Remove import statements since we provide types directly
            # This avoids __import__ issues while keeping the class definitions
            cleaned_source = models_source
            # Remove lines starting with "from" or "import" (but keep class definitions)
            lines = cleaned_source.split('\n')
            cleaned_lines = []
            for line in lines:
                stripped = line.strip()
                # Skip empty lines
                if not stripped:
                    cleaned_lines.append(line)
                    continue

                # Skip import statements since we provide BaseModel, Dict, List, Any directly
                if stripped.startswith('from ') or stripped.startswith('import '):
                    continue

                # Skip comment lines
                if stripped.startswith('#'):
                    continue

                cleaned_lines.append(line)
            cleaned_source = '\n'.join(cleaned_lines)
            
            # Execute the models source code in a restricted environment
            restricted_globals = {
                "__name__": "__main__",
                "__builtins__": {
                    "len": len,
                    "min": min,
                    "max": max,
                    "sum": sum,
                    "print": print,
                    "str": str,
                    "int": int,
                    "float": float,
                    "bool": bool,
                    "dict": dict,
                    "list": list,
                    "tuple": tuple,
                    "set": set,
                    "type": type,
                    "isinstance": isinstance,
                    "__import__": safe_import,
                    "__build_class__": builtins.__build_class__,
                },
                "BaseModel": BaseModel,
                "Dict": Dict,
                "List": List,
                "Any": Any,
                "Optional": Optional,
            }
            local_vars: Dict[str, Any] = {}
            exec(cleaned_source, restricted_globals, local_vars)

            # Extract all BaseModel subclasses
            compiled: Dict[str, Type[BaseModel]] = {}
            for name, obj in local_vars.items():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseModel)
                    and obj is not BaseModel
                ):
                    compiled[name] = obj

            # Cache models for this project
            self.model_cache[project_id] = compiled
            return compiled

        except Exception as exc:
            logger.error("Failed to recompile models from source: %s", exc, exc_info=True)
            return {}

    def _detect_nested_models(self, models_source: str) -> List[str]:
        """
        Detect if models_source contains nested Pydantic model class definitions.
        
        Returns:
            List of nested model class names found
        """
        import re
        nested_models = []
        
        # Pattern: class ModelName(BaseModel): 
        class_pattern = r'class\s+([A-Z][a-zA-Z0-9_]*)\s*\([^)]*BaseModel[^)]*\):'
        classes = re.findall(class_pattern, models_source)
        
        # Check if any class is used as a type annotation in another class
        for class_name in classes:
            # Look for usage like: field_name: ClassName or field_name: Optional[ClassName]
            # But exclude the class definition itself
            usage_pattern = rf':\s*(?:Optional\[)?{class_name}(?:\])?'
            matches = list(re.finditer(usage_pattern, models_source))
            class_def_matches = list(re.finditer(class_pattern, models_source))
            
            # Find the definition of this class
            class_def_start = None
            class_def_end = None
            for match in class_def_matches:
                if match.group(1) == class_name:
                    class_def_start = match.start()
                    # Find the end of the class (next class definition or end of string)
                    next_class = models_source.find('\nclass ', match.end())
                    if next_class != -1:
                        class_def_end = next_class
                    else:
                        class_def_end = len(models_source)
                    break
            
            # Check if usage appears after this class definition (meaning it's used in another class)
            if class_def_start is not None:
                for usage_match in matches:
                    if usage_match.start() > class_def_end:
                        nested_models.append(class_name)
                        break
        
        return list(set(nested_models))

    def _recompile_models_from_source(
        self, project_id: str, models_source: str
    ) -> Dict[str, Type[BaseModel]]:
        """Recompile Pydantic models from stored source code."""
        if not models_source:
            return {}

        try:
            import builtins
            import re
            
            # Safe import function that only allows specific modules
            def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
                allowed_modules = {
                "__future__", "asyncio", "json", "typing", "re", "math", "datetime", "date", "decimal", "statistics",
                "string", "uuid", "hashlib", "base64", "time", "urllib", "urllib.parse", "random", "bson","httpx"
                }
                if name in allowed_modules:
                    return builtins.__import__(name, globals, locals, fromlist, level)
                raise ImportError(f"Import of '{name}' is not allowed in generated models")
            
            # Enhanced cleaning: Remove import statements, comments, docstrings, and invalid syntax
            lines = models_source.split('\n')
            cleaned_lines = []
            in_docstring = False
            in_class_definition = False
            current_class_indent = 0
            current_class_line_idx = -1
            class_has_content = False
            
            for i, line in enumerate(lines):
                stripped = line.strip()
                
                # Skip empty lines (but remember them for potential class body fixes)
                if not stripped:
                    cleaned_lines.append(line)
                    continue
                
                # Skip import statements since we provide types directly
                if stripped.startswith('from ') or stripped.startswith('import '):
                    continue
                
                # Skip comment lines
                if stripped.startswith('#'):
                    continue
                
                # Track class definitions
                if stripped.startswith('class '):
                    # If previous class had no content, add 'pass' to it
                    if in_class_definition and not class_has_content and current_class_line_idx >= 0:
                        # Insert 'pass' after the class definition
                        indent = ' ' * (current_class_indent + 4)
                        cleaned_lines.append(f"{indent}pass")
                        logger.warning(f"Added 'pass' to empty class at line {current_class_line_idx}")
                    
                    in_class_definition = True
                    current_class_indent = len(line) - len(line.lstrip())
                    current_class_line_idx = i
                    class_has_content = False
                    cleaned_lines.append(line)
                    continue
                
                # Check if we're still inside a class
                if in_class_definition:
                    current_indent = len(line) - len(line.lstrip())
                    
                    # If we encounter a line at same or lower indent (and it's not empty/docstring), class ended
                    if current_indent <= current_class_indent and stripped and not stripped.startswith(('"""', "'''")):
                        # If previous class had no content, we need to add 'pass' before this line
                        if not class_has_content:
                            indent = ' ' * (current_class_indent + 4)
                            cleaned_lines.append(f"{indent}pass")
                            logger.warning(f"Added 'pass' to empty class before line {i}")
                        in_class_definition = False
                
                # Handle docstrings (triple-quoted strings)
                if '"""' in line or "'''" in line:
                    triple_double = line.count('"""')
                    triple_single = line.count("'''")
                    
                    if triple_double % 2 == 1:
                        in_docstring = not in_docstring
                        if not in_docstring:
                            continue
                    elif triple_single % 2 == 1:
                        in_docstring = not in_docstring
                        if not in_docstring:
                            continue
                    
                    if in_docstring:
                        continue
                
                # Skip lines that are inside docstrings
                if in_docstring:
                    continue
                
                # If we're inside a class definition, validate field syntax
                if in_class_definition and ':' in stripped and not stripped.startswith('class '):
                    # Check if this looks like a field definition
                    before_colon = stripped.split(':')[0].strip()
                    
                    # Skip 'pass' statements but mark class as having content
                    if stripped == 'pass':
                        cleaned_lines.append(line)
                        class_has_content = True
                        continue
                    
                    # Field names should be valid Python identifiers (no spaces)
                    # Valid: field_name: Type or field_name: Type = default
                    # Invalid: "Document Type: str" (space in field name)
                    # Invalid: "Invoice Line Items: list[Any]" (spaces in field name)
                    if ' ' in before_colon and not before_colon.startswith('#'):
                        # This is likely an invalid field definition, skip it
                        logger.warning(f"Skipping invalid field definition with space in name: {stripped[:80]}")
                        continue
                    
                    # Check for invalid Python identifiers as field names
                    # Use a simple validation: alphanumeric + underscores only
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', before_colon):
                        logger.warning(f"Skipping invalid field identifier: {before_colon}")
                        continue
                    
                    # This is a valid field - mark class as having content
                    class_has_content = True
                
                # Skip lines that reference undefined variables in field defaults
                # Check for common patterns like "= data" or "= some_undefined_var"
                if in_class_definition and '=' in stripped and ':' in stripped:
                    # Extract the default value part (after '=')
                    parts = stripped.split('=', 1)
                    if len(parts) == 2:
                        default_value = parts[1].strip()
                        # Check if it references a bare variable name (not a literal, function call, etc.)
                        # Simple heuristic: if it's a single word without quotes, brackets, or parentheses
                        if (default_value and 
                            not default_value.startswith(("'", '"', '[', '{', '(')) and
                            not default_value[0].isdigit() and
                            default_value not in ('None', 'True', 'False', '...') and
                            '(' not in default_value and
                            '.' not in default_value):
                            # This might be an undefined variable reference
                            logger.warning(f"Skipping line with potential undefined variable reference: {stripped[:80]}")
                            continue
                    
                    # Valid field with default - mark class as having content
                    class_has_content = True
                
                # If this is a valid line in a class, mark class as having content
                if in_class_definition and stripped and not stripped.startswith('#'):
                    class_has_content = True
                
                cleaned_lines.append(line)
            
            # Handle case where last class has no content
            if in_class_definition and not class_has_content:
                indent = ' ' * (current_class_indent + 4)
                cleaned_lines.append(f"{indent}pass")
                logger.warning(f"Added 'pass' to empty class at end of file")
            
            cleaned_source = '\n'.join(cleaned_lines)
            cleaned_source = prepare_models_exec_source(cleaned_source)
            
            # Execute the models source code in a restricted environment
            restricted_globals = {
                "__name__": "__main__",
                "__builtins__": {
                    "len": len,
                    "min": min,
                    "max": max,
                    "sum": sum,
                    "print": print,
                    "str": str,
                    "int": int,
                    "float": float,
                    "bool": bool,
                    "dict": dict,
                    "list": list,
                    "tuple": tuple,
                    "set": set,
                    "type": type,
                    "isinstance": isinstance,
                    "__import__": safe_import,
                    "__build_class__": builtins.__build_class__,
                },
                "BaseModel": BaseModel,
                "RootModel": RootModel,
                "Dict": Dict,
                "List": List,
                "Any": Any,
                "Optional": Optional,
            }
            local_vars: Dict[str, Any] = {}
            
            try:
                exec(cleaned_source, restricted_globals, local_vars)
            except (NameError, SyntaxError) as exec_error:
                # If execution fails, log the cleaned source for debugging
                logger.error(f"Failed to execute cleaned source. Error: {exec_error}")
                logger.debug(f"Cleaned source that failed:\n{cleaned_source}")
                
                # Try one more aggressive cleanup: remove any line with field definitions that have '=' 
                # and might reference undefined variables, and ensure all classes have at least 'pass'
                retry_lines = []
                in_retry_class = False
                retry_class_indent = 0
                retry_class_has_content = False
                
                for line in cleaned_lines:
                    stripped = line.strip()
                    
                    # Track class definitions
                    if stripped.startswith('class '):
                        # If previous class had no content, add 'pass'
                        if in_retry_class and not retry_class_has_content:
                            indent = ' ' * (retry_class_indent + 4)
                            retry_lines.append(f"{indent}pass")
                        
                        in_retry_class = True
                        retry_class_indent = len(line) - len(line.lstrip())
                        retry_class_has_content = False
                        retry_lines.append(line)
                        continue
                    
                    # Check if we're still inside a class
                    if in_retry_class:
                        current_indent = len(line) - len(line.lstrip())
                        if current_indent <= retry_class_indent and stripped and not stripped.startswith(('"""', "'''")):
                            if not retry_class_has_content:
                                indent = ' ' * (retry_class_indent + 4)
                                retry_lines.append(f"{indent}pass")
                            in_retry_class = False
                    
                    # Skip lines that look like field definitions with problematic defaults
                    if (in_retry_class and ':' in stripped and '=' in stripped and 
                        not stripped.startswith('class ') and stripped != 'pass'):
                        # Extract parts
                        colon_parts = stripped.split(':', 1)
                        if len(colon_parts) == 2:
                            field_part = colon_parts[0].strip()
                            type_and_default = colon_parts[1].strip()
                            
                            if '=' in type_and_default:
                                # Keep the line but mark class as having content
                                retry_class_has_content = True
                                retry_lines.append(line)
                                continue
                    
                    # Valid content
                    if in_retry_class and stripped and stripped != 'pass':
                        retry_class_has_content = True
                    
                    retry_lines.append(line)
                
                # Handle case where last class has no content
                if in_retry_class and not retry_class_has_content:
                    indent = ' ' * (retry_class_indent + 4)
                    retry_lines.append(f"{indent}pass")
                
                cleaned_source_retry = prepare_models_exec_source('\n'.join(retry_lines))
                
                try:
                    local_vars = {}
                    exec(cleaned_source_retry, restricted_globals, local_vars)
                    cleaned_source = cleaned_source_retry
                except Exception as retry_error:
                    logger.error(f"Retry execution also failed: {retry_error}")
                    logger.debug(f"Retry source:\n{cleaned_source_retry}")
                    missing = missing_referenced_model_names(cleaned_source_retry)
                    if missing:
                        logger.error(
                            "models_source references undefined model(s): %s",
                            ", ".join(sorted(missing)),
                        )
                    return {}
            
            compiled = rebuild_compiled_models(local_vars)
            if not compiled:
                missing = missing_referenced_model_names(cleaned_source)
                if missing:
                    logger.error(
                        "models_source references undefined model(s): %s",
                        ", ".join(sorted(missing)),
                    )

            # Cache models for this project
            self.model_cache[project_id] = compiled
            logger.info(f"Successfully recompiled {len(compiled)} models for project {project_id}")
            return compiled

        except Exception as exc:
            logger.error("Failed to recompile models from source: %s", exc, exc_info=True)
            return {}

    def _augment_data_models_from_sources(
        self,
        data_models: Dict[str, Dict[str, str]],
        data_sources: List[Dict[str, Any]],
    ) -> None:
        """Ensure every referenced model in data sources has a matching definition."""
        if not isinstance(data_models, dict):
            return

        primitive_defaults = {
            "object": "dict",
            "dict": "dict",
            "map": "dict",
            "list": "list",
            "array": "list",
            "string": "string",
            "text": "string",
            "int": "int",
            "integer": "int",
            "float": "float",
            "number": "number",
            "bool": "bool",
            "boolean": "bool",
        }

        existing_names = {name for name in data_models.keys()}
        lower_lookup = {name.lower(): name for name in existing_names}

        for ds in data_sources or []:
            if not isinstance(ds, dict):
                continue
            for attr in ("inputModel", "responseModel"):
                requested = ds.get(attr)
                if not requested or not isinstance(requested, str):
                    continue

                requested_stripped = requested.strip()
                if not requested_stripped:
                    continue

                existing = lower_lookup.get(requested_stripped.lower())
                if existing:
                    if existing != requested:
                        ds[attr] = existing
                    continue

                placeholder_type = primitive_defaults.get(requested_stripped.lower(), "dict")
                sanitized_name = self._sanitize_model_name(requested_stripped, existing_names)
                data_models[sanitized_name] = {
                    "value": {"type": placeholder_type, "required": False}
                }
                ds[attr] = sanitized_name
                existing_names.add(sanitized_name)
                lower_lookup[sanitized_name.lower()] = sanitized_name
                logger.info(
                    "Auto-created data model '%s' (placeholder type %s) for data source %s",
                    sanitized_name,
                    placeholder_type,
                    ds.get("id"),
                )

    def _sanitize_model_name(self, raw_name: str, existing_names: set) -> str:
        """Convert arbitrary strings into valid, unique Python class names."""
        cleaned = re.sub(r"\W+", "_", raw_name).strip("_")
        if not cleaned:
            cleaned = "AutoModel"
        if cleaned[0].isdigit():
            cleaned = f"Model_{cleaned}"

        parts = [part.capitalize() for part in cleaned.split("_") if part]
        candidate = "".join(parts) or "AutoModel"

        if keyword.iskeyword(candidate.lower()) or candidate in {"BaseModel", "object", "list", "dict"}:
            candidate = f"{candidate.capitalize()}Model"

        base = candidate
        suffix = 1
        while candidate in existing_names:
            candidate = f"{base}{suffix}"
            suffix += 1

        return candidate

    # ------------------------------------------------------------------ T6 IO vs IR
    @staticmethod
    def _pydantic_field_is_required(field: Any) -> bool:
        """Return True if a Pydantic model field is required at runtime."""
        is_required = getattr(field, "is_required", None)
        if callable(is_required):
            try:
                return bool(is_required())
            except TypeError:
                pass
        if isinstance(is_required, bool):
            return is_required
        # Fallbacks for older / unusual field objects
        default = getattr(field, "default", None)
        if default is ...:
            return True
        default_factory = getattr(field, "default_factory", None)
        if default_factory is not None:
            return False
        # Pydantic v2 uses PydanticUndefined for required fields with no default
        try:
            from pydantic_core import PydanticUndefined

            if default is PydanticUndefined:
                return True
        except Exception:
            pass
        # Optional fields usually default to None
        if default is None:
            return False
        return False

    @staticmethod
    def _ir_model_expected_fields(
        model_def: Any,
    ) -> Optional[Dict[str, bool]]:
        """
        Extract {field_name: required} from an IR dataModel definition.

        Returns:
          - {} for empty models (expect no named fields)
          - None for list-root models (skip named-field comparison)
          - {name: required} for normal format-1 object models
        """
        if not model_def or model_def == "empty":
            return {}
        if isinstance(model_def, dict) and model_def.get("type") == "list":
            return None  # list-root — no named business fields
        if not isinstance(model_def, dict):
            return {}

        expected: Dict[str, bool] = {}
        for field_name, field_spec in model_def.items():
            if isinstance(field_spec, dict) and "type" in field_spec:
                expected[field_name] = bool(field_spec.get("required", False))
        return expected

    def _compare_model_fields_to_ir(
        self,
        *,
        role: str,
        model_name: str,
        model_def: Any,
        pydantic_model: Optional[Type[BaseModel]],
        schema: Optional[Dict[str, Any]],
        function_id: str,
    ) -> List[str]:
        """Compare one compiled model + exported schema against IR dataModels fields."""
        errors: List[str] = []
        expected = self._ir_model_expected_fields(model_def)

        # Always require the compiled class to exist when we are comparing a named model
        if pydantic_model is None:
            errors.append(
                f"{function_id}: {role} model '{model_name}' not found in compiled models"
            )
            return errors

        # List-root: skip named-field comparison (RootModel.root is not an IR business field)
        if expected is None:
            return errors

        # --- compiled Pydantic fields ---
        compiled_fields = getattr(pydantic_model, "model_fields", None) or {}
        # RootModel exposes `root`; ignore it when comparing to empty/object IR
        compiled_names = {
            name for name in compiled_fields.keys() if name != "root"
        }
        expected_names = set(expected.keys())

        missing = expected_names - compiled_names
        extra = compiled_names - expected_names
        if missing:
            errors.append(
                f"{function_id}: compiled {role} model '{model_name}' missing "
                f"IR fields: {sorted(missing)}"
            )
        if extra:
            errors.append(
                f"{function_id}: compiled {role} model '{model_name}' has "
                f"extra fields not in IR: {sorted(extra)}"
            )

        for fname, ir_required in expected.items():
            if fname not in compiled_fields:
                continue
            compiled_required = self._pydantic_field_is_required(compiled_fields[fname])
            if compiled_required != ir_required:
                errors.append(
                    f"{function_id}: compiled {role} model '{model_name}.{fname}' "
                    f"required={compiled_required} but IR has required={ir_required}"
                )

        # --- exported schema (Angular contract) ---
        if schema is not None:
            schema_fields = schema.get("fields") or []
            if not isinstance(schema_fields, list):
                errors.append(
                    f"{function_id}: {role} schema for '{model_name}' has invalid fields"
                )
            else:
                schema_by_name = {
                    f.get("name"): f
                    for f in schema_fields
                    if isinstance(f, dict) and f.get("name")
                }
                expected_names = set(expected.keys())
                schema_names = set(schema_by_name.keys())

                missing = expected_names - schema_names
                extra = schema_names - expected_names
                if missing:
                    errors.append(
                        f"{function_id}: {role} schema for '{model_name}' missing "
                        f"IR fields: {sorted(missing)}"
                    )
                if extra:
                    errors.append(
                        f"{function_id}: {role} schema for '{model_name}' has "
                        f"extra fields not in IR: {sorted(extra)}"
                    )

                for fname, ir_required in expected.items():
                    sfield = schema_by_name.get(fname)
                    if not sfield:
                        continue
                    schema_required = bool(sfield.get("required", False))
                    if schema_required != ir_required:
                        errors.append(
                            f"{function_id}: {role} schema '{model_name}.{fname}' "
                            f"required={schema_required} but IR has required={ir_required}"
                        )

        return errors

    def _validate_function_io_vs_ir(
        self,
        *,
        data_source: Dict[str, Any],
        data_models: Dict[str, Any],
        compiled_models: Dict[str, Type[BaseModel]],
        entry: "BackendFunctionEntry",
        input_model_name: str,
        response_model_name: str,
        input_schema: Dict[str, Any],
        response_schema: Dict[str, Any],
    ) -> List[str]:
        """
        T6: Post-backend validation — generated function IO must match IR.

        Checks:
          1. Resolved input/response model names match dataSource (with same fallbacks)
          2. Entry.input_model / entry.response_model are the compiled classes for those names
          3. Compiled Pydantic field names + requiredness match IR dataModels
          4. Exported input_schema / response_schema field names + requiredness match IR
        """
        errors: List[str] = []
        function_id = data_source.get("id") or entry.function_id or "<unknown>"

        # 1. Model names must match dataSource (using the same fallback rules as generate_backend)
        ds_input = data_source.get("inputModel") or next(iter(compiled_models.keys()), None)
        ds_response = data_source.get("responseModel") or (
            list(compiled_models.keys())[-1] if compiled_models else None
        )

        if input_model_name != ds_input:
            errors.append(
                f"{function_id}: input_model '{input_model_name}' does not match "
                f"dataSource inputModel '{ds_input}'"
            )
        if response_model_name != ds_response:
            errors.append(
                f"{function_id}: response_model '{response_model_name}' does not match "
                f"dataSource responseModel '{ds_response}'"
            )

        # 2. Entry models must be the compiled classes for those names
        expected_input_cls = compiled_models.get(input_model_name) if input_model_name else None
        expected_response_cls = compiled_models.get(response_model_name) if response_model_name else None

        if not input_model_name or expected_input_cls is None:
            errors.append(
                f"{function_id}: input model '{input_model_name}' not found in compiled models"
            )
        elif entry.input_model is not expected_input_cls:
            errors.append(
                f"{function_id}: entry.input_model is not compiled model '{input_model_name}'"
            )

        if not response_model_name or expected_response_cls is None:
            errors.append(
                f"{function_id}: response model '{response_model_name}' not found in compiled models"
            )
        elif entry.response_model is not expected_response_cls:
            errors.append(
                f"{function_id}: entry.response_model is not compiled model '{response_model_name}'"
            )

        # 3 + 4. Field-level compare for input and response
        errors.extend(
            self._compare_model_fields_to_ir(
                role="input",
                model_name=input_model_name,
                model_def=data_models.get(input_model_name),
                pydantic_model=expected_input_cls,
                schema=input_schema,
                function_id=function_id,
            )
        )
        errors.extend(
            self._compare_model_fields_to_ir(
                role="response",
                model_name=response_model_name,
                model_def=data_models.get(response_model_name),
                pydantic_model=expected_response_cls,
                schema=response_schema,
                function_id=function_id,
            )
        )

        return errors

    def _assert_function_io_matches_ir(
        self,
        *,
        data_source: Dict[str, Any],
        data_models: Dict[str, Any],
        compiled_models: Dict[str, Type[BaseModel]],
        entry: "BackendFunctionEntry",
        input_model_name: str,
        response_model_name: str,
        input_schema: Dict[str, Any],
        response_schema: Dict[str, Any],
    ) -> None:
        """Raise ValueError if IO still diverges from IR (used after repair attempts)."""
        errors = self._validate_function_io_vs_ir(
            data_source=data_source,
            data_models=data_models,
            compiled_models=compiled_models,
            entry=entry,
            input_model_name=input_model_name,
            response_model_name=response_model_name,
            input_schema=input_schema,
            response_schema=response_schema,
        )
        if errors:
            msg = (
                f"Post-backend IO vs IR mismatch for '{data_source.get('id')}':\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
            logger.error(msg)
            raise ValueError(msg)

    def _programmatic_rebind_function_io(
        self,
        *,
        data_source: Dict[str, Any],
        data_models: Dict[str, Any],
        compiled_models: Dict[str, Type[BaseModel]],
        models_source: Optional[str],
        entry: "BackendFunctionEntry",
        build_model_schema: Callable[[str], Dict[str, Any]],
        project_id: str,
        errors: List[str],
    ) -> Tuple[
        "BackendFunctionEntry",
        str,
        str,
        Dict[str, Any],
        Dict[str, Any],
        Dict[str, Type[BaseModel]],
        Optional[str],
    ]:
        """
        Path A — programmatic repair (no LLM):
          1. Force model names from dataSource (same fallbacks as generate_backend)
          2. If compiled fields diverge from IR, recompile models from IR
          3. Rebind entry.input_model / entry.response_model to compiled classes
          4. Rebuild exported schemas from IR
        """
        function_id = data_source.get("id") or entry.function_id
        input_model_name = data_source.get("inputModel") or next(iter(compiled_models.keys()))
        response_model_name = (
            data_source.get("responseModel") or list(compiled_models.keys())[-1]
        )

        field_drift = any(
            ("missing IR fields" in e)
            or ("extra fields" in e)
            or ("required=" in e and "compiled" in e)
            for e in errors
        )
        if field_drift:
            logger.warning(
                "[T6 repair] Recompiling Pydantic models from IR for function %s",
                function_id,
            )
            compiled_models, models_source = self._compile_pydantic_models(
                project_id, data_models
            )

        input_cls = compiled_models.get(input_model_name)
        response_cls = compiled_models.get(response_model_name)
        if input_cls is None or response_cls is None:
            missing = []
            if input_cls is None:
                missing.append(input_model_name)
            if response_cls is None:
                missing.append(response_model_name)
            raise ValueError(
                f"[T6 repair] Cannot rebind function '{function_id}': "
                f"compiled models missing {missing}"
            )

        if entry.input_model is not input_cls or entry.response_model is not response_cls:
            logger.warning(
                "[T6 repair] Rebinding entry models for %s → input=%s response=%s",
                function_id,
                input_model_name,
                response_model_name,
            )
            entry = replace(entry, input_model=input_cls, response_model=response_cls)

        input_schema = build_model_schema(input_model_name)
        response_schema = build_model_schema(response_model_name)

        return (
            entry,
            input_model_name,
            response_model_name,
            input_schema,
            response_schema,
            compiled_models,
            models_source,
        )

    async def _ensure_function_io_matches_ir(
        self,
        *,
        data_source: Dict[str, Any],
        data_models: Dict[str, Any],
        compiled_models: Dict[str, Type[BaseModel]],
        models_source: Optional[str],
        entry: "BackendFunctionEntry",
        input_model_name: str,
        response_model_name: str,
        input_schema: Dict[str, Any],
        response_schema: Dict[str, Any],
        build_model_schema: Callable[[str], Dict[str, Any]],
        project_id: str,
        dependency_info: Optional[List[Dict[str, Any]]] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        collection_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
        collection_schemas_prompt: str = "",
    ) -> Tuple[
        "BackendFunctionEntry",
        str,
        str,
        Dict[str, Any],
        Dict[str, Any],
        Dict[str, Type[BaseModel]],
        Optional[str],
    ]:
        """
        T6 agentic gate: detect IO↔IR mismatches and repair before persist.

        Flow (never aborts the whole generate_backend batch):
          1. Validate.
          2. Path A — programmatic rebind/recompile/rebuild schemas (no LLM).
          3. Re-validate.
          4. Path B — if still mismatched, regenerate this function up to
             `_MAX_IO_REGEN_ATTEMPTS` times via LLM with corrected models,
             rebinding + re-validating after each attempt.
          5. If still mismatched, keep the last entry and continue — do not
             drop the function.
        """
        _MAX_IO_REGEN_ATTEMPTS = 3
        function_id = data_source.get("id") or entry.function_id

        errors = self._validate_function_io_vs_ir(
            data_source=data_source,
            data_models=data_models,
            compiled_models=compiled_models,
            entry=entry,
            input_model_name=input_model_name,
            response_model_name=response_model_name,
            input_schema=input_schema,
            response_schema=response_schema,
        )
        if not errors:
            return (
                entry,
                input_model_name,
                response_model_name,
                input_schema,
                response_schema,
                compiled_models,
                models_source,
            )

        logger.warning(
            "[T6] IO↔IR mismatch for %s (%d issue(s)) — starting repair:\n%s",
            function_id,
            len(errors),
            "\n".join(f"  - {e}" for e in errors),
        )

        # --- Path A: programmatic repair ---
        (
            entry,
            input_model_name,
            response_model_name,
            input_schema,
            response_schema,
            compiled_models,
            models_source,
        ) = self._programmatic_rebind_function_io(
            data_source=data_source,
            data_models=data_models,
            compiled_models=compiled_models,
            models_source=models_source,
            entry=entry,
            build_model_schema=build_model_schema,
            project_id=project_id,
            errors=errors,
        )

        errors = self._validate_function_io_vs_ir(
            data_source=data_source,
            data_models=data_models,
            compiled_models=compiled_models,
            entry=entry,
            input_model_name=input_model_name,
            response_model_name=response_model_name,
            input_schema=input_schema,
            response_schema=response_schema,
        )
        if not errors:
            logger.info("[T6 repair] Path A fixed IO↔IR for %s", function_id)
            return (
                entry,
                input_model_name,
                response_model_name,
                input_schema,
                response_schema,
                compiled_models,
                models_source,
            )

        # --- Path B: regenerate function up to N times with corrected models ---
        for regen_attempt in range(1, _MAX_IO_REGEN_ATTEMPTS + 1):
            logger.warning(
                "[T6 repair] Path B regenerate %d/%d for %s (%d issue(s) remain):\n%s",
                regen_attempt,
                _MAX_IO_REGEN_ATTEMPTS,
                function_id,
                len(errors),
                "\n".join(f"  - {e}" for e in errors),
            )
            try:
                entry = await self._create_function_entry(
                    project_id,
                    data_source,
                    compiled_models,
                    dependency_info=dependency_info,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    available_credentials=available_credentials,
                    collection_schemas=collection_schemas,
                    collection_schemas_prompt=collection_schemas_prompt,
                    data_models=data_models,
                )
            except BillingError:
                raise
            except Exception as regen_exc:
                logger.warning(
                    "[T6 repair] Path B regenerate %d/%d failed for %s: %s",
                    regen_attempt,
                    _MAX_IO_REGEN_ATTEMPTS,
                    function_id,
                    regen_exc,
                )
                if regen_attempt >= _MAX_IO_REGEN_ATTEMPTS:
                    logger.error(
                        "[T6 repair] Path B exhausted for %s — keeping last entry instead of dropping",
                        function_id,
                    )
                    break
                continue

            # Rebind after regenerate so contract stays pinned to IR
            (
                entry,
                input_model_name,
                response_model_name,
                input_schema,
                response_schema,
                compiled_models,
                models_source,
            ) = self._programmatic_rebind_function_io(
                data_source=data_source,
                data_models=data_models,
                compiled_models=compiled_models,
                models_source=models_source,
                entry=entry,
                build_model_schema=build_model_schema,
                project_id=project_id,
                errors=errors,
            )

            errors = self._validate_function_io_vs_ir(
                data_source=data_source,
                data_models=data_models,
                compiled_models=compiled_models,
                entry=entry,
                input_model_name=input_model_name,
                response_model_name=response_model_name,
                input_schema=input_schema,
                response_schema=response_schema,
            )
            if not errors:
                logger.info(
                    "[T6 repair] Path B regenerate %d/%d fixed IO↔IR for %s",
                    regen_attempt,
                    _MAX_IO_REGEN_ATTEMPTS,
                    function_id,
                )
                return (
                    entry,
                    input_model_name,
                    response_model_name,
                    input_schema,
                    response_schema,
                    compiled_models,
                    models_source,
                )

        # Exhausted — keep the last generated function rather than dropping it.
        msg = (
            f"Post-backend IO vs IR mismatch for '{function_id}' "
            f"after Path A + {_MAX_IO_REGEN_ATTEMPTS} Path B regenerates:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
        logger.error("%s — saving function anyway", msg)
        return (
            entry,
            input_model_name,
            response_model_name,
            input_schema,
            response_schema,
            compiled_models,
            models_source,
        )

    @staticmethod
    def _model_has_nested_item_model_ref(model_def: Any) -> bool:
        """True when a field-based model has a list field with items.model."""
        if not isinstance(model_def, dict) or model_def.get("type") == "list":
            return False
        for field_spec in model_def.values():
            if not isinstance(field_spec, dict):
                continue
            if field_spec.get("type") != "list":
                continue
            if item_model_name_from_items(field_spec.get("items")):
                return True
        return False

    def _resolve_list_field_py_type(
        self,
        items: Any,
        type_mapping: Dict[str, Any],
        compiled: Dict[str, Type[BaseModel]],
        *,
        model_name: str,
        field_name: str,
        allow_unresolved_fallback: bool = True,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """
        Resolve IR list `items` to a Python list type.

        Returns (py_type, unresolved_model_name).
        If items.model is set but not yet in compiled and allow_unresolved_fallback
        is False, returns (None, model_name) so the caller can defer.
        """
        if isinstance(items, str):
            # Bare string may be a primitive type OR an item model name.
            # Do not force {"type": ...} — that would drop model refs.
            model_ref = item_model_name_from_items(items)
            if model_ref:
                items = {"model": model_ref}
            elif items:
                items = {"type": items}
            else:
                items = {}
        if not isinstance(items, dict):
            items = {}

        item_model_name = item_model_name_from_items(items)
        if item_model_name:
            if item_model_name in compiled:
                return List[compiled[item_model_name]], None
            if not allow_unresolved_fallback:
                return None, item_model_name
            logger.warning(
                "List field %s.%s references items.model=%r which is not compiled; "
                "falling back to List[Dict[str, Any]]",
                model_name,
                field_name,
                item_model_name,
            )
            return List[Dict[str, Any]], item_model_name

        item_type = items.get("type", "any")
        if item_type == "object":
            return List[Dict[str, Any]], None
        return List[type_mapping.get(item_type, Any)], None

    def _emit_field_based_model(
        self,
        model_name: str,
        model_def: Dict[str, Any],
        type_mapping: Dict[str, Any],
        compiled: Dict[str, Type[BaseModel]],
        code_lines: List[str],
        *,
        allow_unresolved_item_fallback: bool = True,
    ) -> bool:
        """
        Compile one field-based model into compiled + code_lines.

        Returns False if deferred (unresolved items.model and fallback disabled).
        """
        # Pre-check: defer if any items.model is unresolved and fallback disabled
        if not allow_unresolved_item_fallback:
            for field_name, field_spec in model_def.items():
                if not isinstance(field_spec, dict) or field_spec.get("type") != "list":
                    continue
                py_type, unresolved = self._resolve_list_field_py_type(
                    field_spec.get("items"),
                    type_mapping,
                    compiled,
                    model_name=model_name,
                    field_name=field_name,
                    allow_unresolved_fallback=False,
                )
                if unresolved and py_type is None:
                    return False

        field_defs: Dict[str, Any] = {}
        code_lines.append(f"\nclass {model_name}(BaseModel):")

        for field_name, field_spec in model_def.items():
            if not isinstance(field_spec, dict):
                logger.warning(
                    "Skipping non-format-1 field %s.%s (type=%s); "
                    "expected {{type, required}} after normalize",
                    model_name,
                    field_name,
                    type(field_spec).__name__,
                )
                continue

            raw_type = field_spec.get("type", "string")
            required = bool(field_spec.get("required", False))

            if raw_type == "list":
                py_type, _ = self._resolve_list_field_py_type(
                    field_spec.get("items"),
                    type_mapping,
                    compiled,
                    model_name=model_name,
                    field_name=field_name,
                    allow_unresolved_fallback=True,
                )
                if py_type is None:
                    py_type = List[Any]
            else:
                py_type = type_mapping.get(raw_type, Any)

            default = ... if required else None
            field_defs[field_name] = (py_type, default)

            type_str = self._type_to_string(py_type)
            if required:
                code_lines.append(f"    {field_name}: {type_str}")
            else:
                code_lines.append(f"    {field_name}: Optional[{type_str}] = None")

        compiled_model = create_model(model_name, **field_defs)
        compiled[model_name] = compiled_model
        return True

    def _compile_pydantic_models(
        self,
        project_id: str,
        data_models: Dict[str, Dict[str, str]],
    ) -> Tuple[Dict[str, Type[BaseModel]], str]:
        # Format 1 only: field → { type, required }. Legacy bare strings are
        # converted with required=False (preserves historical Optional behavior).
        data_models = normalize_data_models_to_format1(
            data_models or {},
            legacy_required_default=False,
        )
        if not data_models:
            data_models = {
                "DefaultRequest": {
                    "payload": {"type": "dict", "required": False},
                },
                "DefaultResponse": {
                    "status": {"type": "string", "required": False},
                    "error": {"type": "string", "required": False},
                },
            }

        type_mapping = {
            "string": str,
            "int": int,
            "float": float,
            "bool": bool,
            "datetime": str,
            "dict": Dict[str, Any],
            "list": List[Any],
            "object": Dict[str, Any],
            "number": float,
            "any": Any,
        }

        compiled: Dict[str, Type[BaseModel]] = {}
        # Nested List[ItemModel] is preferred when items.model is set so codegen
        # and validation see real item fields. Params still arrive as dicts;
        # Pydantic coerces list-of-dicts into List[ItemModel] when validated.
        code_lines: List[str] = [
            "from __future__ import annotations",
            "from pydantic import BaseModel, RootModel",
            "from typing import Any, Dict, List, Optional",
        ]

        deferred_nested: List[str] = []

        # First pass: empty, list-root (defer compile), leaf field models
        for model_name, model_def in data_models.items():
            # Case 1: Empty model
            if not model_def or model_def == "empty":
                compiled_model = create_model(model_name)
                compiled[model_name] = compiled_model
                code_lines.append(f"\nclass {model_name}(BaseModel):")
                code_lines.append("    pass")
                continue

            # Case 2: List-type model (response IS a list)
            if isinstance(model_def, dict) and model_def.get("type") == "list":
                items_spec = model_def.get("items", {})
                item_model_name = item_model_name_from_items(items_spec)

                if item_model_name:
                    # Will compile in second pass after item model exists
                    code_lines.append(f"\nclass {model_name}(RootModel):")
                    code_lines.append(f"    root: List[{item_model_name}]")
                    continue
                else:
                    compiled_model = RootModel[List[Dict[str, Any]]]
                    compiled[model_name] = compiled_model
                    code_lines.append(f"\nclass {model_name}(RootModel):")
                    code_lines.append(f"    root: List[Dict[str, Any]]")
                    continue

            # Case 3: Field-based — defer if nested items.model (needs item class first)
            if self._model_has_nested_item_model_ref(model_def):
                deferred_nested.append(model_name)
                continue

            self._emit_field_based_model(
                model_name,
                model_def,
                type_mapping,
                compiled,
                code_lines,
                allow_unresolved_item_fallback=True,
            )

        # Second pass: list-root models that reference other models
        for model_name, model_def in data_models.items():
            if isinstance(model_def, dict) and model_def.get("type") == "list":
                item_model_name = item_model_name_from_items(model_def.get("items"))
                if item_model_name and item_model_name in compiled:
                    item_model = compiled[item_model_name]
                    compiled[model_name] = RootModel[List[item_model]]

        # Third pass: field-based models with nested List[ItemModel]
        remaining = list(deferred_nested)
        while remaining:
            next_remaining: List[str] = []
            progress = False
            for model_name in remaining:
                model_def = data_models.get(model_name)
                if not isinstance(model_def, dict):
                    continue
                ok = self._emit_field_based_model(
                    model_name,
                    model_def,
                    type_mapping,
                    compiled,
                    code_lines,
                    allow_unresolved_item_fallback=False,
                )
                if ok:
                    progress = True
                else:
                    next_remaining.append(model_name)
            if not progress:
                for model_name in next_remaining:
                    model_def = data_models.get(model_name)
                    if isinstance(model_def, dict):
                        self._emit_field_based_model(
                            model_name,
                            model_def,
                            type_mapping,
                            compiled,
                            code_lines,
                            allow_unresolved_item_fallback=True,
                        )
                break
            remaining = next_remaining

        models_source = "\n".join(code_lines)
        self.model_cache[project_id] = compiled
        return compiled, models_source

    def _type_to_string(self, py_type) -> str:
        """Convert Python type to string representation for code generation."""
        if hasattr(py_type, '__origin__'):
            # Handle generic types like List[Dict[str, Any]]
            origin = py_type.__origin__.__name__
            args = ', '.join(self._type_to_string(arg) for arg in py_type.__args__)
            return f"{origin}[{args}]"
        elif hasattr(py_type, '__name__'):
            return py_type.__name__
        else:
            return str(py_type)

    # --------------------------------------------------------- function utils
    def _gather_dependency_info(
        self,
        current_ds: Dict[str, Any],
        all_data_sources: List[Dict[str, Any]],
        generated_functions: Dict[str, Dict[str, Any]],
        compiled_models: Dict[str, Type[BaseModel]],
    ) -> List[Dict[str, Any]]:
        """
        Gather complete information about functions that the current data source depends on.
        
        Returns:
            List of dependency information dictionaries, each containing:
            - data_source: The full data source definition
            - source_code: Generated source code (if available)
            - input_model: Input model name and schema
            - response_model: Response model name and schema
            - operation: Operation details (for dynamic functions)
            - description: Function description
        """
        dependencies_info = []
        current_dependencies = current_ds.get("dependencies", [])
        
        if not current_dependencies:
            return dependencies_info
        
        # Build lookup map for all data sources
        ds_map = {ds.get("id"): ds for ds in all_data_sources if ds.get("id")}
        
        for dep in current_dependencies:
            if not isinstance(dep, dict):
                continue
            
            dep_id = dep.get("dataSourceId")
            if not dep_id:
                continue
            
            # Find the dependent data source
            dep_ds = ds_map.get(dep_id)
            if not dep_ds:
                logger.warning(
                    "Dependency %s not found in data sources for function %s",
                    dep_id,
                    current_ds.get("id"),
                )
                continue
            
            # Gather information about the dependency
            dep_info = {
                "data_source": dep_ds,
                "dependency_config": dep,  # Includes outputMapping, executionOrder, required
            }
            
            # If function has already been generated, include its source code
            if dep_id in generated_functions:
                gen_func = generated_functions[dep_id]
                dep_info["source_code"] = gen_func.get("source_code")
                dep_info["input_model"] = gen_func.get("input_model")
                dep_info["response_model"] = gen_func.get("response_model")
                dep_info["input_schema"] = gen_func.get("input_schema")
                dep_info["response_schema"] = gen_func.get("response_schema")
            else:
                # Function not yet generated, but we can still provide its definition
                dep_info["input_model"] = dep_ds.get("inputModel")
                dep_info["response_model"] = dep_ds.get("responseModel")
                
                # Build model schemas from compiled models
                input_model_name = dep_ds.get("inputModel")
                response_model_name = dep_ds.get("responseModel")
                
                if input_model_name and input_model_name in compiled_models:
                    input_model = compiled_models[input_model_name]
                    dep_info["input_schema"] = {
                        "name": input_model_name,
                        "fields": {
                            name: str(field.annotation)
                            for name, field in input_model.model_fields.items()
                        },
                    }
                
                if response_model_name and response_model_name in compiled_models:
                    response_model = compiled_models[response_model_name]
                    dep_info["response_schema"] = {
                        "name": response_model_name,
                        "fields": {
                            name: str(field.annotation)
                            for name, field in response_model.model_fields.items()
                        },
                    }
            
            # Include operation details for dynamic functions
            if dep_ds.get("type") == "dynamic":
                dep_info["operation"] = dep_ds.get("operation", {})
                dep_info["database_logic"] = dep_ds.get("databaseLogic", "")
            
            # Include config for external functions
            if dep_ds.get("type") == "external":
                dep_info["config"] = dep_ds.get("config", {})
            
            dep_info["description"] = dep_ds.get("description", "")
            dependencies_info.append(dep_info)
        
        return dependencies_info

    @staticmethod
    def _python_syntax_issue(source_code: str) -> Optional[str]:
        """Return a SyntaxError description (with the offending line) or None if parseable."""
        if not source_code:
            return "SyntaxError: empty source code"
        try:
            ast.parse(source_code)
            return None
        except SyntaxError as exc:
            offending = ""
            if exc.lineno:
                lines = source_code.splitlines()
                if 1 <= exc.lineno <= len(lines):
                    offending = lines[exc.lineno - 1].strip()
            detail = f"SyntaxError: {exc.msg} on line {exc.lineno}"
            if offending:
                detail = f"{detail} ({offending!r})"
            return detail

    @staticmethod
    def _uncompiled_function_stub(func_id: str, error: Optional[str]) -> Callable[..., Any]:
        """Runtime placeholder so a broken function can still be saved and registered."""
        message = (
            f"Backend function '{func_id}' was saved but did not compile"
            + (f": {error}" if error else ".")
        )

        async def _stub(*, params=None, db_wrapper=None, secure_http=None, request_meta=None):
            raise RuntimeError(message)

        _stub.__name__ = func_id
        return _stub

    @staticmethod
    def _generation_failed_callable(func_id: str, error: Optional[str]) -> Callable[..., Any]:
        """Runtime callable that refuses to silently succeed after generation_failed."""
        message = (
            f"Backend function '{func_id}' is marked generation_failed "
            "(generic CRUD fallback is prohibited)"
            + (f": {error}" if error else ".")
        )

        async def _stub(*, params=None, db_wrapper=None, secure_http=None, request_meta=None):
            raise RuntimeError(message)

        _stub.__name__ = func_id
        return _stub

    def _backend_generation_model(self, *, is_complex: bool, scratch_round: int = 0) -> str:
        """Flash for simple CRUD; pro for complex domain functions and scratch retries."""
        if is_complex or scratch_round > 0:
            return self.backend_complex_model_name
        return self.backend_model_name

    async def _fix_and_compile(
        self,
        func_id: str,
        source_code: str,
        issue: str,
        *,
        requires_schema_probe: bool,
        target_source: str = "mongodb",
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Tuple[str, Optional[Callable[..., Any]], Optional[str]]:
        """
        Dedicated fixer LLM: given broken source + the issue, return corrected code.

        Unlike generation retries (which re-run `_generate_with_llm` with error text
        appended), this call's only job is to patch the existing function.
        """
        if not source_code or not issue:
            return source_code, None, issue
        logger.info(
            "Dedicated fixer LLM for %s — issue: %s",
            func_id,
            (issue or "")[:300],
        )
        fixed = await run_sync(
            self._fix_generated_function_with_llm,
            func_id,
            source_code,
            issue,
            requires_schema_probe=requires_schema_probe,
            target_source=target_source,
            project_id=project_id,
            screen_id=screen_id,
            version=version,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            model_name=model_name,
        )
        if not fixed:
            logger.warning("Dedicated fixer returned no code for %s", func_id)
            return source_code, None, issue
        try:
            compiled = self._compile_function(func_id, fixed)
            logger.info("Dedicated fixer compiled successfully for %s", func_id)
            return fixed, compiled, None
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Dedicated fixer output still failed compile for %s: %s",
                func_id,
                err,
            )
            return fixed, None, err

    async def _create_function_entry(
        self,
        project_id: str,
        data_source: Dict[str, Any],
        models: Dict[str, Type[BaseModel]],
        dependency_info: Optional[List[Dict[str, Any]]] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        collection_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
        collection_schemas_prompt: str = "",
        data_models: Optional[Dict[str, Any]] = None,
    ) -> BackendFunctionEntry:
        """
        Create a BackendFunctionEntry for a given data source.

        Flow:
          1. Generate via `_generate_with_llm`. On generation retries, the same
             path is used with `previous_error` / `security_issue` in the prompt.
          2. If the generated code has a compile/syntax/security issue, a
             *separate* fixer LLM call patches that code (does not regenerate).
          3. Complex domain functions (LLM/HTTP/branching) start on the pro
             model; simple CRUD stays on flash and only escalates on scratch.
          4. Run an executable conformance gate (fake adapters). Empty/missing
             required success outputs are **blocking** (Issue 5): retry with the
             error in the prompt; after all retries mark generation_failed and
             do not publish. Write-fixture misses stay advisory.
          5. Never publish the old generic CRUD *template fallback*. If no LLM
             source compiles after retries, mark `generation_failed` and register
             a callable that raises.
        """
        func_id = data_source.get("id")
        if not func_id:
            raise ValueError("dataSource.id is required")

        ds_type = (data_source.get("type") or "dynamic").lower()
        target_source = (data_source.get("target_store") or "mongodb").lower()
        input_model_name = data_source.get("inputModel") or next(iter(models))
        response_model_name = data_source.get("responseModel") or list(models)[-1]
        input_model = models.get(input_model_name)
        response_model = models.get(response_model_name)
        if input_model is None or response_model is None:
            raise ValueError(f"Missing Pydantic model for {func_id}")

        # ArcadeDB has no resolve_field_name equivalent — Cypher MERGE/MATCH is schema-flexible.
        requires_schema_probe = (
            False
            if target_source == "arcadedb"
            else self._requires_schema_probe(data_source, collection_schemas=collection_schemas)
        )
        tracking_kw: Dict[str, Any] = {
            "project_id": project_id,
            "screen_id": screen_id,
            "version": version,
            "org_id": org_id,
            "subscriber_id": subscriber_id,
            "user_id": user_id,
        }

        last_error: Optional[str] = None
        security_issue: Optional[str] = None
        source_code: Optional[str] = None
        compiled_function: Optional[Callable[..., Any]] = None
        compile_ok = False
        conformance_ok: Optional[bool] = None
        generation_failed = False
        conformance_error: Optional[str] = None
        persistence_fixture_error: Optional[str] = None
        # Preserve the best compiling+publishable version across retries.
        # Blocking conformance failures are NOT stored as best (Issue 5).
        best_source: Optional[str] = None
        best_compiled: Optional[Callable[..., Any]] = None
        last_llm_source: Optional[str] = None
        blocking_conformance_failed = False

        max_attempts = 3
        max_scratch_rounds = 2
        is_complex = is_nontrivial_domain_datasource(data_source)
        if is_complex:
            logger.info(
                "Function %s classified as complex; generating with %s from first attempt",
                func_id,
                self.backend_complex_model_name,
            )

        async def _fix_issue(
            code: str,
            issue: str,
            *,
            attempt_no: int,
            kind: str,
            model_name: Optional[str] = None,
        ):
            logger.warning(
                "Function %s %s (attempt %d, model=%s): %s — applying dedicated fixer",
                func_id,
                kind,
                attempt_no,
                model_name or self.backend_model_name,
                issue,
            )
            return await self._fix_and_compile(
                func_id,
                code or "",
                issue,
                requires_schema_probe=requires_schema_probe,
                target_source=target_source,
                model_name=model_name,
                **tracking_kw,
            )

        async def _run_conformance(code: str, fn: Callable[..., Any]) -> Dict[str, Any]:
            """Run conformance; record ok/error. Returns the gate dict."""
            nonlocal conformance_ok, conformance_error
            try:
                gate = await run_conformance_gate(
                    func_id=func_id,
                    compiled_function=fn,
                    source_code=code,
                    data_source=data_source,
                    input_model=input_model,
                    response_model=response_model,
                    available_credentials=available_credentials or [],
                )
            except Exception as gate_exc:
                gate = {
                    "ok": False,
                    "reason": (
                        f"Conformance raised: {type(gate_exc).__name__}: {gate_exc}"
                    ),
                    "failure_kind": "raised",
                    "result": None,
                    "writes": [],
                }
            if gate.get("ok"):
                conformance_ok = True
                conformance_error = None
                logger.info("Function %s conformance passed", func_id)
            else:
                conformance_ok = False
                conformance_error = gate.get("reason") or "Conformance gate failed"
                kind = gate.get("failure_kind") or "other"
                if is_blocking_conformance_failure(kind):
                    logger.warning(
                        "Function %s blocking conformance failed (%s): %s",
                        func_id,
                        kind,
                        conformance_error,
                    )
                else:
                    logger.warning(
                        "Function %s advisory conformance failed (%s, non-blocking): %s",
                        func_id,
                        kind,
                        conformance_error,
                    )
            return gate

        for scratch_round in range(max_scratch_rounds):
            if scratch_round > 0:
                logger.warning(
                    "Function %s: generate+fix cycle failed; retrying from scratch "
                    "(round %d/%d, model=%s)",
                    func_id,
                    scratch_round + 1,
                    max_scratch_rounds,
                    self._backend_generation_model(
                        is_complex=is_complex, scratch_round=scratch_round
                    ),
                )
                security_issue = None

            for attempt in range(max_attempts):
                compiled_function = None
                is_last_attempt = (
                    attempt == max_attempts - 1
                    and scratch_round == max_scratch_rounds - 1
                )
                try:
                    retry_model = self._backend_generation_model(
                        is_complex=is_complex, scratch_round=scratch_round
                    )
                    source_code = await self._generate_function_source(
                        func_id,
                        ds_type,
                        data_source,
                        input_model=input_model,
                        response_model=response_model,
                        previous_error=last_error,
                        security_issue=security_issue,
                        dependency_info=dependency_info,
                        project_id=project_id,
                        screen_id=screen_id,
                        version=version,
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                        available_credentials=available_credentials or [],
                        collection_schemas=collection_schemas,
                        collection_schemas_prompt=collection_schemas_prompt,
                        data_models=data_models,
                        target_source=target_source,
                        model_name=retry_model,
                    )
                    if source_code:
                        last_llm_source = source_code
                    else:
                        last_error = (
                            last_error
                            or "LLM produced no function source; generic CRUD template fallback is prohibited"
                        )
                        logger.warning(
                            "Function %s: empty LLM source (attempt %d, model=%s): %s",
                            func_id,
                            attempt + 1,
                            retry_model,
                            last_error,
                        )
                        continue

                    # Syntax first — runtime-contract regex/LLM is unreliable on invalid Python
                    # (this is the `{ ... )` class of failures).
                    syntax_issue = self._python_syntax_issue(source_code or "")
                    if syntax_issue:
                        last_error = syntax_issue
                        source_code, compiled_function, last_error = await _fix_issue(
                            source_code or "",
                            last_error,
                            attempt_no=attempt + 1,
                            model_name=retry_model,
                            kind="syntax error",
                        )
                        if not compiled_function:
                            security_issue = None
                            continue
                        compiled_function = None  # contract may rewrite source; recompile after

                    try:
                        source_code = await self._ensure_runtime_contract(
                            source_code,
                            func_id,
                            requires_schema_probe=requires_schema_probe,
                            target_source=target_source,
                            data_source=data_source,
                            input_model=input_model,
                            response_model=response_model,
                            **tracking_kw,
                        )
                    except Exception as contract_exc:
                        last_error = f"{type(contract_exc).__name__}: {contract_exc}"
                        source_code, compiled_function, last_error = await _fix_issue(
                            source_code or "",
                            last_error,
                            attempt_no=attempt + 1,
                            model_name=retry_model,
                            kind="runtime contract failed",
                        )
                        if not compiled_function:
                            security_issue = None
                            continue

                    if not compiled_function:
                        try:
                            compiled_function = self._compile_function(func_id, source_code)
                        except Exception as compile_exc:
                            last_error = f"{type(compile_exc).__name__}: {compile_exc}"
                            source_code, compiled_function, last_error = await _fix_issue(
                                source_code or "",
                                last_error,
                                attempt_no=attempt + 1,
                                model_name=retry_model,
                                kind="compile failed",
                            )
                            if not compiled_function:
                                security_issue = None
                                continue

                    if not compiled_function or not source_code:
                        continue

                    compile_ok = True
                    last_error = None

                    persist_links = resolve_persistence_links(data_source=data_source)
                    persist_reason = function_code_matches_persistence_fixture(
                        source_code or "",
                        persist_links,
                        data_source=data_source,
                    )
                    if persist_reason:
                        last_error = persist_reason
                        persistence_fixture_error = persist_reason
                        logger.warning(
                            "Function %s persistence fixture failed (attempt %d): %s",
                            func_id,
                            attempt + 1,
                            persist_reason,
                        )
                        if not is_last_attempt:
                            compiled_function = None
                            compile_ok = False
                            continue
                        logger.warning(
                            "Function %s publishing despite persistence fixture mismatch (advisory)",
                            func_id,
                        )
                    else:
                        persistence_fixture_error = None

                    verification_result = await self._verify_function_security(
                        func_id,
                        source_code,
                        data_source,
                        input_model=input_model,
                        response_model=response_model,
                        available_credentials=available_credentials or [],
                        **tracking_kw,
                    )
                    if not verification_result.get("safe", False):
                        security_issue = verification_result.get(
                            "reason", "Security verification failed"
                        )
                        fixed_src, fixed_fn, _fix_err = await _fix_issue(
                            source_code or "",
                            security_issue,
                            attempt_no=attempt + 1,
                            model_name=retry_model,
                            kind="security verification failed",
                        )
                        if fixed_fn:
                            source_code = fixed_src
                            compiled_function = fixed_fn
                            security_issue = None
                            compile_ok = True
                        else:
                            if attempt < max_attempts - 1:
                                continue
                            logger.error(
                                "Function %s failed security verification after fixer; "
                                "continuing to conformance with last compiling version",
                                func_id,
                            )

                    if not (compiled_function and source_code):
                        continue

                    gate = await _run_conformance(source_code, compiled_function)
                    kind = gate.get("failure_kind")
                    if not gate.get("ok") and is_blocking_conformance_failure(kind):
                        last_error = (
                            f"CONFORMANCE BLOCKING ({kind}): {conformance_error}. "
                            "Required success outputs must be non-empty (or return a "
                            "declared error). Do not publish empty placeholders."
                        )
                        if not is_last_attempt:
                            compiled_function = None
                            compile_ok = False
                            continue
                        blocking_conformance_failed = True
                        logger.error(
                            "Function %s blocking conformance still failing on last "
                            "attempt — marking generation_failed (will not publish)",
                            func_id,
                        )
                        break

                    # Pass or advisory-only failure: eligible to publish.
                    best_source = source_code
                    best_compiled = compiled_function
                    blocking_conformance_failed = False
                    break
                except BillingError:
                    raise
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    security_issue = None
                    logger.warning(
                        "Attempt %d to generate backend function %s failed: %s",
                        attempt + 1,
                        func_id,
                        last_error,
                        exc_info=True,
                    )
                    if source_code:
                        source_code, compiled_function, last_error = await _fix_issue(
                            source_code,
                            last_error,
                            attempt_no=attempt + 1,
                            model_name=retry_model,
                            kind="unexpected error",
                        )
                        if compiled_function and source_code:
                            gate = await _run_conformance(source_code, compiled_function)
                            kind = gate.get("failure_kind")
                            if (
                                not gate.get("ok")
                                and is_blocking_conformance_failure(kind)
                            ):
                                last_error = (
                                    f"CONFORMANCE BLOCKING ({kind}): {conformance_error}"
                                )
                                if not is_last_attempt:
                                    compiled_function = None
                                    compile_ok = False
                                    continue
                                blocking_conformance_failed = True
                                break
                            compile_ok = True
                            best_source = source_code
                            best_compiled = compiled_function
                            blocking_conformance_failed = False
                            break

            # Stop scratch rounds once we have a publishable compiling function.
            if best_compiled and not blocking_conformance_failed:
                break
            if blocking_conformance_failed and scratch_round == max_scratch_rounds - 1:
                break

        if best_compiled and not blocking_conformance_failed:
            source_code = best_source
            compiled_function = best_compiled
            compile_ok = True
            if conformance_ok is None and source_code and compiled_function:
                await _run_conformance(source_code, compiled_function)
        elif blocking_conformance_failed:
            if not source_code:
                source_code = last_llm_source or ""
            generation_failed = True
            compile_ok = False
            compiled_function = self._generation_failed_callable(
                func_id, last_error or conformance_error
            )
            logger.error(
                "Function %s marked generation_failed (blocking conformance): %s",
                func_id,
                last_error or conformance_error,
            )

        if not source_code:
            source_code = last_llm_source or ""
            logger.error(
                "No usable source produced for %s after generate+fix+scratch; "
                "generic CRUD template fallback is prohibited",
                func_id,
            )
            last_error = last_error or (
                "LLM produced no function source; generic CRUD template fallback is prohibited"
            )

        # generation_failed when we cannot publish a valid compiling LLM body.
        if not compiled_function:
            generation_failed = True
            logger.error(
                "Function %s marked generation_failed (no compiling source): %s",
                func_id,
                last_error,
            )
            compiled_function = self._generation_failed_callable(func_id, last_error)
            compile_ok = False

        return BackendFunctionEntry(
            project_id=project_id,
            function_id=func_id,
            type=ds_type,
            callable=compiled_function,
            input_model=input_model,
            response_model=response_model,
            source_code=source_code or "",
            description=data_source.get("description"),
            compile_ok=compile_ok,
            generation_error=None if (compile_ok and not generation_failed) else last_error,
            generation_failed=generation_failed,
            conformance_ok=conformance_ok,
            conformance_error=conformance_error,
            persistence_fixture_error=persistence_fixture_error,
        )

    async def _write_back_graph_schema(
        self,
        source_code: str,
        data_source: Dict[str, Any],
        *,
        org_id: str,
        subscriber_id: str,
    ) -> None:
        """
        Best-effort Phase-2 write-back: extract vertex property names actually used in the
        generated Cypher and persist them via EnterpriseGraphRepository, so the NEXT function
        touching the same collection gets them back as an authoritative hint instead of
        guessing its own field names.
        """
        try:
            func_collections = collections_for_data_source(data_source)
            if not func_collections:
                return
            from services.entity_resolver import encode_graph_fields, extract_cypher_shape
            shape = extract_cypher_shape(source_code)
            props = set(shape["props"])
            if not props:
                return
            from repositories.arcadedb_repository import EnterpriseGraphRepository
            # Persist labels + relationship types + a group (root label) next to the properties, so a
            # LATER function in ANY project of this subscriber can bind to this entity by name and
            # generate Cypher against the real labels instead of guessing them.
            from services.entity_resolver import split_schema_fields
            group = shape["labels"][0] if shape["labels"] else func_collections[0]
            repo = EnterpriseGraphRepository()
            for entity_name in dict.fromkeys([*func_collections, *shape["labels"]]):
                # MERGE with what is already registered: a read function references only a few
                # properties and must never shrink the schema the writing function registered.
                old = split_schema_fields(
                    (await repo.get_schemas_for_collections(subscriber_id, org_id, [entity_name])).get(entity_name) or []
                )
                fields = encode_graph_fields(
                    {*props, *(f["name"] for f in old["props"])},
                    {*shape["labels"], *old["labels"]},
                    {*shape["relationships"], *old["relationships"]},
                    old["group"] or group,
                )
                await repo.upsert_schema(
                    subscriber_id, org_id, entity_name, fields, target_store="arcadedb"
                )
        except Exception as exc:
            logger.warning("[target_store DEBUG] graph schema write-back failed: %s", exc, exc_info=True)

    async def _generate_function_source(
        self,
        func_id: str,
        ds_type: str,
        data_source: Dict[str, Any],
        *,
        input_model: Optional[Type[BaseModel]] = None,
        response_model: Optional[Type[BaseModel]] = None,
        previous_error: Optional[str] = None,
        security_issue: Optional[str] = None,
        dependency_info: Optional[List[Dict[str, Any]]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        collection_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
        collection_schemas_prompt: str = "",
        data_models: Optional[Dict[str, Any]] = None,
        target_source: str = "mongodb",
        model_name: Optional[str] = None,
    ) -> Optional[str]:
        is_arcadedb = target_source == "arcadedb"
        requires_schema_probe = (
            False
            if is_arcadedb
            else self._requires_schema_probe(data_source, collection_schemas=collection_schemas)
        )
        model_name = model_name or self.backend_model_name

        graph_schema_hint = ""
        if is_arcadedb and org_id and subscriber_id:
            try:
                from repositories.arcadedb_repository import EnterpriseGraphRepository
                func_collections = collections_for_data_source(data_source)
                if func_collections:
                    graph_schemas = await EnterpriseGraphRepository().get_schemas_for_collections(
                        subscriber_id, org_id, func_collections
                    )
                    lines = ["Graph collections used by this function:"]
                    from services.entity_resolver import split_schema_fields
                    for c in func_collections:
                        fields = graph_schemas.get(c)
                        if fields:
                            split = split_schema_fields(fields)
                            parts = [f"{f['name']} ({f.get('type', 'string')})" for f in split["props"]]
                            shape_txt = ""
                            if split["labels"]:
                                shape_txt += f" Vertex labels: {', '.join(split['labels'])}."
                            if split["relationships"]:
                                shape_txt += f" Relationship types: {', '.join(split['relationships'])}."
                            lines.append(
                                f"  - {c}: ALREADY HAS A GRAPH SCHEMA — reuse these EXACT vertex labels, "
                                f"relationship types and property names, do not invent new ones.{shape_txt} "
                                f"Properties: {', '.join(parts)}"
                            )
                        else:
                            lines.append(
                                f"  - {c}: NEW graph collection — choose one clear vertex label "
                                f"and a small set of property names for it"
                            )
                    graph_schema_hint = "\n".join(lines)
            except Exception as exc:
                logger.warning(
                    "[target_store DEBUG] graph schema probe failed for %s: %s", func_id, exc, exc_info=True,
                )

        fn_gen_cached_content = await _get_backend_fn_gen_cached_content(
            model_name,
            requires_schema_probe=requires_schema_probe,
            target_source=target_source,
        )

        # Decide how to supply the external API spec to the code generator.
        #
        # classify_provider_spec() returns one of three states:
        #   pinned      — user / Stage 0 gave a concrete host (URL or host:port DSN).
        #                 Inject the pinned spec verbatim; never run grounding.
        #   model_known — well-known public API (Stripe, GitHub, …); LLM writes from
        #                 training knowledge. No grounding, no spec injection needed.
        #   unknown     — HTTP required but neither a host nor a public provider found.
        #                 Run grounding once as a last resort.
        api_spec_research: Optional[str] = None
        if ds_type == "external" and not previous_error:
            db_logic = data_source.get("databaseLogic") or ""
            desc = data_source.get("description") or ""
            ops = extract_provider_operations_from_text(desc, db_logic)
            spec_class = classify_provider_spec(ops, desc, db_logic)
            logger.info(
                "[backend-gen] external spec_class=%s for '%s'", spec_class, func_id
            )

            if spec_class == "pinned":
                # Inject pinned spec so the LLM uses the correct host/method/path.
                pinned_text = format_pinned_provider_spec(ops) if ops else None
                if pinned_text:
                    api_spec_research = pinned_text
                    logger.info(
                        "[backend-gen] Pinned spec injected for '%s' (%d chars)",
                        func_id, len(pinned_text),
                    )

            elif spec_class == "unknown":
                # Last resort: grounding only when we have no host and no public provider.
                logger.info(
                    "[backend-gen] spec=unknown for '%s' — attempting grounding lookup",
                    func_id,
                )
                grounded_text = await _resolve_external_api_spec_with_grounding(
                    description=desc,
                    database_logic=db_logic,
                    available_credentials=available_credentials,
                    project_id=project_id,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                if grounded_text:
                    api_spec_research = grounded_text
                    logger.info(
                        "[backend-gen] Grounding returned API spec for '%s' (%d chars)",
                        func_id, len(grounded_text),
                    )
            # model_known → no action; LLM writes from training knowledge

        persist_links = resolve_persistence_links(data_source=data_source)
        persistence_spec = format_pinned_persistence_spec(persist_links) if persist_links else None

        llm_source = await run_sync(
            self._generate_with_llm,
            func_id,
            ds_type,
            data_source,
            input_model=input_model,
            response_model=response_model,
            previous_error=previous_error,
            security_issue=security_issue,
            dependency_info=dependency_info,
            project_id=project_id,
            screen_id=screen_id,
            version=version,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            available_credentials=available_credentials or [],
            collection_schemas=collection_schemas,
            collection_schemas_prompt=collection_schemas_prompt,
            fn_gen_cached_content=fn_gen_cached_content,
            data_models=data_models,
            target_source=target_source,
            graph_schema_hint=graph_schema_hint,
            api_spec_research=api_spec_research,
            persistence_spec=persistence_spec,
            model_name=model_name,
        )

        if llm_source:
            logger.info("Generated function %s using LLM (%s)", func_id, model_name)
            if is_arcadedb and org_id and subscriber_id:
                await self._write_back_graph_schema(
                    llm_source, data_source, org_id=org_id, subscriber_id=subscriber_id
                )
            return llm_source

        logger.error(
            "LLM generation failed for %s (model=%s); generic CRUD/template fallback is prohibited",
            func_id,
            model_name,
        )
        return None

    @staticmethod
    def _requires_schema_probe_heuristic(data_source: Dict[str, Any]) -> bool:
        """IR-text/action fallback when DB pre-probe is unavailable or collections are empty."""
        operation = data_source.get("operation") or {}
        action = (operation.get("action") or "insert").lower()
        text = " ".join(
            [
                (data_source.get("databaseLogic") or "").lower(),
                (data_source.get("description") or "").lower(),
            ]
        )

        if operation.get("readCollections"):
            return True
        if action in ("find", "update", "delete", "aggregate", "read"):
            return True

        existing_markers = (
            "inspect the",
            "inspect schema",
            "sample document",
            "actual field names",
            "existing collection",
            "already populated",
            "previously saved",
            "fetch from",
            "read from",
            "load from",
        )
        if any(marker in text for marker in existing_markers):
            return True

        if action == "insert":
            if any(
                marker in text
                for marker in (
                    "update existing",
                    "upsert",
                    "modify existing",
                    "patch existing",
                )
            ):
                return True
            return False

        return False

    @staticmethod
    def _requires_schema_probe(
        data_source: Optional[Dict[str, Any]],
        *,
        collection_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> bool:
        """Whether generated code should use services.field_utils.resolve_field_name."""
        if not data_source:
            return False
        if (data_source.get("type") or "dynamic").lower() == "external":
            return False

        operation = data_source.get("operation") or {}
        if isinstance(operation, dict) and "schemaProbe" in operation:
            return bool(operation["schemaProbe"])

        meta = (collection_schemas or {}).get("__meta__") or {}
        if meta.get("subscriber_db_exists") is False:
            return False

        # Pure inserts (no reads): keep snake_case / NEW-insert path even if the
        # target collection already has sample docs. Auto-enabling probe for
        # inserts caused resolve_field_name misuse and incomplete arity calls.
        action = (operation.get("action") or "insert").lower()
        if action == "insert" and not operation.get("readCollections"):
            return BackendGenerator._requires_schema_probe_heuristic(data_source)

        cols = collections_for_data_source(data_source)
        if collection_schemas is not None and cols:
            if any((collection_schemas.get(c) or {}).get("sampled") for c in cols):
                return True
            # Probe ran: subscriber DB missing, empty DB, or empty collections → all NEW.
            if "__meta__" in collection_schemas:
                return False
            return BackendGenerator._requires_schema_probe_heuristic(data_source)

        return BackendGenerator._requires_schema_probe_heuristic(data_source)

    @staticmethod
    def _code_without_comments(source_code: str) -> str:
        """Strip # comments for static contract checks (avoids false positives in comments)."""
        lines = []
        for line in source_code.splitlines():
            lines.append(line.split("#", 1)[0])
        return "\n".join(lines)

    @staticmethod
    def _logical_name_to_snake(name: str) -> str:
        if not name:
            return name
        text = re.sub(r"[\s\-]+", "_", name)
        text = re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower()
        text = re.sub(r"_+", "_", text)
        return text.strip("_")

    @classmethod
    def _logical_fields_from_datasource(
        cls, data_source: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Snake_case field names declared in mappings / databaseLogic (for Issue 8)."""
        if not data_source:
            return []
        return list(schema_field_names_from_datasource(data_source))

    @staticmethod
    def _resolve_field_name_literals(source_code: str) -> List[str]:
        """Second-arg string literals from resolve_field_name(collection, \"name\")."""
        return [
            m.group(1)
            for m in re.finditer(
                r"""resolve_field_name\s*\(\s*[^,\)]+,\s*['\"]([^'\"]+)['\"]""",
                source_code or "",
            )
        ]

    @staticmethod
    def _logical_name_is_abbreviation_of(short: str, full: str) -> bool:
        """True when *short* looks like an abbreviation of a token in *full*.

        Rec3: ``exp`` → ``total_years_of_experience``, ``dist`` → ``network_distance``.
        Exact token matches (``score`` in ``score``) are not abbreviations.
        """
        short_n = (short or "").strip().lower()
        full_n = (full or "").strip().lower()
        if not short_n or not full_n or short_n == full_n:
            return False
        if "_" in short_n:
            return False
        if len(short_n) < 3 or len(short_n) >= len(full_n):
            return False
        stop = {"of", "the", "a", "an", "to", "and", "or", "by", "for", "in", "on"}
        tokens = [t for t in full_n.split("_") if t and t not in stop]
        for token in tokens:
            if token.startswith(short_n) and len(token) > len(short_n):
                return True
        return False

    @classmethod
    def _best_abbreviation_expansion(
        cls,
        short: str,
        candidates: List[str],
        data_source: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Pick the best full name for an abbreviated resolve_field_name literal.

        Prefers sort-clause fields, then last-token matches (``exp`` →
        ``..._experience``), so Rec3 does not suggest
        ``score_breakdown_experience`` over ``total_years_of_experience``.
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        sort_fields = set(
            sort_field_names_from_logic(
                (data_source or {}).get("databaseLogic") or ""
            )
        )
        pool = [c for c in candidates if c in sort_fields] or list(candidates)

        short_n = (short or "").strip().lower()
        stop = {"of", "the", "a", "an", "to", "and", "or", "by", "for", "in", "on"}

        def _last_token(full: str) -> str:
            tokens = [t for t in full.lower().split("_") if t and t not in stop]
            return tokens[-1] if tokens else full.lower()

        last_hits = [
            c
            for c in pool
            if _last_token(c).startswith(short_n)
            and len(_last_token(c)) > len(short_n)
        ]
        pool = last_hits or pool

        # Prefer longer / more specific names when multiple last-token matches
        # (total_years_of_experience over score_breakdown_experience).
        pool.sort(key=lambda c: (-len(c), c))
        return pool[0]

    @classmethod
    def _resolve_field_abbreviation_issue(
        cls,
        source_code: str,
        data_source: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Issue 8: reject abbreviated resolve_field_name logical names."""
        if not source_code or not data_source:
            return None
        declared = cls._logical_fields_from_datasource(data_source)
        if not declared:
            return None
        literals = cls._resolve_field_name_literals(
            cls._code_without_comments(source_code)
        )
        if not literals:
            return None
        declared_set = set(declared)
        problems: List[str] = []
        for lit in literals:
            lit_snake = cls._logical_name_to_snake(lit)
            if lit_snake in declared_set or lit in declared_set:
                continue
            matches = [
                full
                for full in declared
                if cls._logical_name_is_abbreviation_of(lit_snake, full)
            ]
            if not matches:
                continue
            best = cls._best_abbreviation_expansion(lit_snake, matches, data_source)
            if not best:
                continue
            problems.append(
                f"resolve_field_name(..., {lit!r}) must use the EXACT "
                f"logical name {best!r} from databaseLogic/mappings — "
                "do not abbreviate (resolve_field_name is not a fuzzy matcher)"
            )
        if not problems:
            return None
        return problems[0]

    @staticmethod
    def _infer_logical_name_from_resolve_lhs(lhs: str) -> Optional[str]:
        """Infer logical field name from assignment LHS like user_email_field → user_email."""
        name = (lhs or "").strip()
        if not name:
            return None
        for suffix in (
            "_field",
            "_db_name",
            "_db_field",
            "_key",
            "_name",
            "_col",
            "_column",
        ):
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                break
        name = BackendGenerator._logical_name_to_snake(name)
        return name or None

    @staticmethod
    def _resolve_field_name_call_has_incomplete_arity(source_code: str) -> bool:
        """True when any resolve_field_name(...) call has fewer than 2 arguments."""
        for match in re.finditer(r"\bresolve_field_name\s*\(", source_code or ""):
            start = match.end()
            depth = 1
            i = start
            while i < len(source_code) and depth > 0:
                ch = source_code[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
            if depth != 0:
                return True
            args_src = source_code[start : i - 1].strip()
            if not args_src:
                return True
            # Count top-level commas (ignore nested calls/parens).
            depth = 0
            commas = 0
            for ch in args_src:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 0:
                    commas += 1
            if commas < 1:
                return True
        return False

    @classmethod
    def _remove_nested_def(cls, code: str, func_name: str) -> str:
        """Remove a nested def/async def block by name."""
        lines = code.splitlines()
        out: List[str] = []
        i = 0
        def_pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(func_name)}\s*\(")
        while i < len(lines):
            if def_pat.match(lines[i]):
                base_indent = len(lines[i]) - len(lines[i].lstrip())
                i += 1
                while i < len(lines):
                    if lines[i].strip() and (
                        len(lines[i]) - len(lines[i].lstrip()) <= base_indent
                    ):
                        break
                    i += 1
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)

    @staticmethod
    def _arcadedb_contract_issue(source_code: str) -> Optional[str]:
        """Detect patterns that fail on /dynamic/execute for ArcadeDB-backed functions."""
        if not source_code:
            return None
        check_src = BackendGenerator._code_without_comments(source_code)
        checks = [
            (r"if\s+db_wrapper\s+is\s+None", "must not guard on db_wrapper is None"),
            (r"if\s+not\s+db_wrapper\b", "must not treat db_wrapper as required (if not db_wrapper)"),
            (r"await\s+db_wrapper\.", "graph access must use Database.run_arcadedb_command, not db_wrapper"),
            (r"if\s+secure_http\s+is\s+None", "must not guard on secure_http is None"),
            (r"if\s+not\s+secure_http\b", "must not treat secure_http as required (if not secure_http)"),
            (r"await\s+secure_http\.", "HTTP must use requests/httpx directly, not secure_http"),
            (r"Database\.get_collection\s*\(", "targetSource is arcadedb — use Database.run_arcadedb_command, not get_collection"),
            (r"Database\.(?:insert_one|update_one|delete_one|find_to_list|aggregate_to_list)\s*\(", "targetSource is arcadedb — use Database.run_arcadedb_command, not MongoDB helpers"),
            (r"run_arcadedb_command\s*\([^)]*\bdatabase\s*=", "do not pass database= to run_arcadedb_command — subscriber routing is automatic"),
        ]
        for pattern, message in checks:
            if re.search(pattern, check_src):
                return message
        json_dump_match = re.search(r"json\.dumps\(\s*([a-zA-Z_][a-zA-Z0-9_]*)", check_src)
        if json_dump_match and re.search(r"node|edge", json_dump_match.group(1), re.IGNORECASE) \
                and "run_arcadedb_command" in check_src:
            return (
                f"function serializes {json_dump_match.group(1)} with json.dumps into an "
                f"ArcadeDB property — a node/edge list must become real vertices/relationships "
                f"(loop + MERGE per node, MATCH+MERGE per edge), not a JSON string property"
            )
        return None

    @staticmethod
    def _source_reads_param(source_code: str, field: str) -> bool:
        """True when `params` is read for this exact inputModel field name."""
        if not field or not source_code:
            return True
        f = re.escape(field)
        return bool(
            re.search(
                rf"""(?<![A-Za-z0-9_])params\s*(?:\.\s*get\s*\(\s*['"]{f}['"]|\s*\[\s*['"]{f}['"]|\.\s*{f}\b)|['"]{f}['"]\s+in\s+params\b""",
                source_code,
            )
        )

    @staticmethod
    def _params_mentioned_in_logic(database_logic: str) -> List[str]:
        logic = database_logic or ""
        names: List[str] = []
        seen: Set[str] = set()
        for pat in (
            r"\bparams\.([A-Za-z_][A-Za-z0-9_]*)",
            r"""params\s*\.\s*get\s*\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""",
            r"""params\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""",
        ):
            for match in re.finditer(pat, logic):
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    @staticmethod
    def _gemini_models_in_text(text: str) -> List[str]:
        if not text:
            return []
        out: List[str] = []
        seen: Set[str] = set()
        for match in re.finditer(
            r"\b(gemini-[a-z0-9]+(?:[.-][a-z0-9]+)*)\b", text, re.I
        ):
            model_id = match.group(1)
            key = model_id.lower()
            if key not in seen:
                seen.add(key)
                out.append(model_id)
        return out

    @staticmethod
    def _declared_collection_names(data_source: Optional[Dict[str, Any]]) -> List[str]:
        names = list(collections_for_data_source(data_source or {}))
        seen = set(names)
        for link in (data_source or {}).get("persistenceLinks") or []:
            if not isinstance(link, dict):
                continue
            coll = (link.get("collection") or "").strip()
            if coll and coll not in seen:
                seen.add(coll)
                names.append(coll)
        return names

    @staticmethod
    def _declared_credential_names(data_source: Optional[Dict[str, Any]]) -> List[str]:
        """Credential names from config.credentials and databaseLogic request_meta lookups."""
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
            r"""\[['\"]credentials['\"]\]\s*\[['\"]([^'"]+)['"]\]""",
            logic,
            re.I,
        ):
            _add(match.group(1))
        for match in re.finditer(
            r"""\.get\(\s*['"]credentials['"]\s*\)[^\n]{0,60}\.get\(\s*['"]([^'"]+)['"]""",
            logic,
            re.I,
        ):
            _add(match.group(1))
        return names

    @classmethod
    def _datasource_requires_credentials(
        cls, data_source: Optional[Dict[str, Any]]
    ) -> bool:
        if not data_source:
            return False
        if cls._declared_credential_names(data_source):
            return True
        logic = (data_source.get("databaseLogic") or "").lower()
        if "request_meta" in logic and "credentials" in logic:
            return True
        if "gemini" in logic or "api_key" in logic or "llm" in logic:
            cfg = data_source.get("config") or {}
            if isinstance(cfg, dict) and cfg.get("credentials"):
                return True
        return False

    @staticmethod
    def _source_reads_request_meta_credentials(source_code: str) -> bool:
        if not source_code:
            return False
        return bool(
            re.search(
                r"""request_meta[^\n]{0,120}['"]credentials['"]"""
                r"""|['"]credentials['"][^\n]{0,80}request_meta""",
                source_code,
                re.I,
            )
        )

    @staticmethod
    def _is_secret_credential_key(key: str, declared: Optional[Sequence[str]] = None) -> bool:
        """True for API/secret credential names — not ordinary form fields like password/csrf_token."""
        if not key:
            return False
        key_l = key.lower()
        declared_l = {str(n).lower() for n in (declared or ())}
        if key_l in declared_l:
            return True
        # LLM-hallucinated secret names (never user-input fields).
        known = {
            "gemini_api_key",
            "google_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "unipile_api_key",
            "api_key",
            "x-api-key",
            "x_api_key",
        }
        if key_l in known:
            return True
        if key_l.endswith("_api_key") or key_l.endswith("apikey"):
            return True
        # SCREAMING_SNAKE env vars for keys/tokens/secrets when this DS declares credentials
        # or the name is clearly a provider API secret.
        if key.isupper() and (
            key.endswith("_API_KEY")
            or key.endswith("_TOKEN")
            or key.endswith("_SECRET")
            or key.endswith("APIKEY")
        ):
            if declared_l or any(
                p in key for p in ("GEMINI", "GOOGLE", "OPENAI", "ANTHROPIC", "UNIPILE")
            ):
                return True
        return False

    @classmethod
    def _resolve_primary_credential_key(
        cls, key: str, declared: Sequence[str]
    ) -> str:
        """Map hallucinated aliases (api_key / gemini_api_key) onto declared IR names."""
        key_l = key.lower()
        for name in declared:
            if str(name).lower() == key_l:
                return str(name)
        gemini_declared = [
            str(n)
            for n in declared
            if "gemini" in str(n).lower() or "google" in str(n).lower()
        ]
        if key_l in {"api_key", "google_api_key", "gemini_api_key"} and gemini_declared:
            return gemini_declared[0]
        return key

    @classmethod
    def _credential_lookup_expr(
        cls,
        key: str,
        declared: Sequence[str],
        *,
        default_expr: Optional[str] = None,
    ) -> str:
        """Build ``((request_meta or {}).get("credentials") or {}).get(...)`` for a secret key."""
        primary = cls._resolve_primary_credential_key(key, declared)
        variants: List[str] = []
        seen: Set[str] = set()
        for candidate in (primary, primary.upper(), primary.lower()):
            if candidate and candidate not in seen:
                seen.add(candidate)
                variants.append(candidate)
        # Gemini IR often declares GEMINI_API_KEY while prompts teach gemini_api_key.
        if "gemini" in primary.lower() or "google" in primary.lower():
            for extra in ("gemini_api_key", "GEMINI_API_KEY", "google_api_key"):
                if extra not in seen:
                    seen.add(extra)
                    variants.append(extra)

        parts = [
            f'((request_meta or {{}}).get("credentials") or {{}}).get({v!r})'
            for v in variants
        ]
        expr = " or ".join(parts)
        if default_expr is not None:
            default_expr = default_expr.strip()
            # Drop nested-call defaults (unbalanced / another getenv) — credentials.get is enough.
            if (
                default_expr
                and default_expr not in ("None",)
                and "(" not in default_expr
            ):
                expr = f"({expr}) or ({default_expr})"
        return expr

    @classmethod
    def _fix_credential_access_patterns(
        cls,
        source_code: str,
        data_source: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Rewrite env/params secret lookups to request_meta['credentials'] (Issue 3)."""
        if not source_code or not cls._datasource_requires_credentials(data_source):
            return source_code
        declared = cls._declared_credential_names(data_source)
        code = source_code

        def _should_rewrite(key: str) -> bool:
            return cls._is_secret_credential_key(key, declared)

        # Capture a simple default (no nested calls). Complex defaults are dropped.
        simple_default = (
            r"None|True|False|\d+(?:\.\d+)?"
            r"|'(?:[^'\\]|\\.)*'"
            r'|"(?:[^"\\]|\\.)*"'
        )

        def _rewrite_env_get(match: re.Match) -> str:
            key = match.group(1)
            if not _should_rewrite(key):
                return match.group(0)
            return cls._credential_lookup_expr(
                key, declared, default_expr=match.group(2)
            )

        code = re.sub(
            rf"""os\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Za-z0-9_]+)['"]\s*(?:,\s*({simple_default}))?\s*\)""",
            _rewrite_env_get,
            code,
        )
        # Remaining secret env gets (complex default) — rewrite without preserving default.
        code = re.sub(
            r"""os\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Za-z0-9_]+)['"]\s*(?:,\s*[^)]+)?\s*\)""",
            lambda m: (
                cls._credential_lookup_expr(m.group(1), declared)
                if _should_rewrite(m.group(1))
                else m.group(0)
            ),
            code,
        )

        code = re.sub(
            r"""os\.environ\s*\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""",
            lambda m: (
                cls._credential_lookup_expr(m.group(1), declared)
                if _should_rewrite(m.group(1))
                else m.group(0)
            ),
            code,
        )

        code = re.sub(
            rf"""params\s*\.\s*get\s*\(\s*['"]([A-Za-z0-9_]+)['"]\s*(?:,\s*({simple_default}))?\s*\)""",
            lambda m: (
                cls._credential_lookup_expr(
                    m.group(1), declared, default_expr=m.group(2)
                )
                if _should_rewrite(m.group(1))
                else m.group(0)
            ),
            code,
        )
        code = re.sub(
            r"""params\s*\.\s*get\s*\(\s*['"]([A-Za-z0-9_]+)['"]\s*(?:,\s*[^)]+)?\s*\)""",
            lambda m: (
                cls._credential_lookup_expr(m.group(1), declared)
                if _should_rewrite(m.group(1))
                else m.group(0)
            ),
            code,
        )

        code = re.sub(
            r"""params\s*\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""",
            lambda m: (
                cls._credential_lookup_expr(m.group(1), declared)
                if _should_rewrite(m.group(1))
                else m.group(0)
            ),
            code,
        )

        if code != source_code:
            logger.info(
                "Rewrote env/params credential lookups to request_meta['credentials'] "
                "(declared=%s)",
                declared or "inferred",
            )
        return code

    @classmethod
    def _credential_access_issue(
        cls,
        source_code: str,
        data_source: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Reject remaining env/params secret lookups after deterministic rewrite."""
        if not source_code or not cls._datasource_requires_credentials(data_source):
            return None
        check_src = cls._code_without_comments(source_code)
        declared = cls._declared_credential_names(data_source)

        env_hits = []
        for match in re.finditer(
            r"""os\.(?:environ(?:\.get|\s*\[\s*)|getenv)\s*\(?\s*['"]([A-Za-z0-9_]+)['"]""",
            check_src,
        ):
            key = match.group(1)
            if cls._is_secret_credential_key(key, declared):
                env_hits.append(key)
        if env_hits:
            return (
                "generated code still reads secrets from os.environ/os.getenv "
                + ", ".join(repr(k) for k in env_hits)
                + " after rewrite. ALWAYS use request_meta['credentials'] "
                + (
                    f"with exact name(s) {declared}. "
                    if declared
                    else "using the credential name from config.credentials. "
                )
            )

        param_hits = []
        for match in re.finditer(
            r"""params\s*(?:\.\s*get\s*\(\s*['"]([^'"]+)['"]|\s*\[\s*['"]([^'"]+)['"])""",
            check_src,
        ):
            key = match.group(1) or match.group(2) or ""
            if cls._is_secret_credential_key(key, declared):
                param_hits.append(key)
        if param_hits:
            return (
                "generated code still reads secrets from params "
                + ", ".join(repr(k) for k in param_hits)
                + ". Credentials are NEVER in params — use request_meta['credentials']"
                + (f" with exact name(s) {declared}." if declared else ".")
            )

        if not cls._source_reads_request_meta_credentials(check_src):
            return (
                "dataSource requires credentials but generated code never reads "
                "request_meta['credentials']. "
                + (
                    f"Use: creds = (request_meta or {{}}).get('credentials') or {{}}; "
                    f"api_key = creds.get({declared[0]!r}) "
                    if declared
                    else "Read the credential name declared in config.credentials / databaseLogic. "
                )
                + "NEVER use os.environ or params for API keys."
            )

        if declared:
            declared_ok = False
            for name in declared:
                variants = {name, name.lower(), name.upper()}
                for v in variants:
                    if re.search(
                        rf"""(?:credentials|creds)[^\n]{{0,100}}['"]{re.escape(v)}['"]"""
                        rf"""|['"]{re.escape(v)}['"][^\n]{{0,40}}(?:credentials|creds)"""
                        rf"""|\.get\(\s*['"]{re.escape(v)}['"]""",
                        check_src,
                        re.I,
                    ):
                        declared_ok = True
                        break
                if declared_ok:
                    break
            if not declared_ok:
                alias_ok = bool(
                    re.search(
                        r"""(?:credentials|creds)[^\n]{0,100}['"]gemini_api_key['"]"""
                        r"""|\.get\(\s*['"]gemini_api_key['"]""",
                        check_src,
                        re.I,
                    )
                ) and any("gemini" in n.lower() for n in declared)
                if not alias_ok:
                    return (
                        "dataSource declares credentials "
                        + ", ".join(repr(n) for n in declared)
                        + " but generated code does not look them up on "
                        "request_meta['credentials']. Use the exact name "
                        f"(e.g. creds.get({declared[0]!r}))."
                    )
        return None

    @classmethod
    def _collection_literals_in_source(cls, source_code: str) -> List[str]:
        found: List[str] = []
        seen: Set[str] = set()
        patterns = (
            r"""Database\.(?:find_one|find_to_list|insert_one|update_one|delete_one|get_collection|aggregate_to_list)\s*\(\s*['"]([^'"]+)['"]""",
            r"""resolve_field_name\s*\(\s*['"]([^'"]+)['"]""",
            r"""(?<![A-Za-z0-9_])(?:collection_name|collection)\s*=\s*['"]([^'"]+)['"]""",
        )
        for pat in patterns:
            for match in re.finditer(pat, source_code):
                name = match.group(1).strip()
                if name and name not in seen:
                    seen.add(name)
                    found.append(name)
        return found

    @classmethod
    def _spec_fidelity_prompt_section(
        cls,
        data_source: Optional[Dict[str, Any]],
        input_model: Optional[Type[BaseModel]],
    ) -> str:
        fields: List[str] = []
        if input_model is not None and getattr(input_model, "model_fields", None):
            fields = [
                name
                for name in input_model.model_fields
                if not (
                    name == "value"
                    and getattr(input_model, "__name__", "")
                    in {"Empty", "EmptyRequest", "EmptyParams"}
                )
            ]
        collections = cls._declared_collection_names(data_source)
        models = cls._gemini_models_in_text(
            (data_source or {}).get("databaseLogic") or ""
        )
        creds = cls._declared_credential_names(data_source)
        requires_insert = write_path_requires_insert(data_source)
        logical_fields = cls._logical_fields_from_datasource(data_source)
        if (
            not fields
            and not collections
            and not models
            and not creds
            and not requires_insert
            and not logical_fields
        ):
            return ""
        lines = [
            "SPEC FIDELITY (generation is rejected and retried if you violate these):",
        ]
        if fields:
            examples = ", ".join(f'params.get("{n}")' for n in fields[:8])
            lines.append(
                f"- You MUST read these inputModel fields by EXACT name: {fields}. "
                f"Example: {examples}. Do not invent aliases."
            )
        if collections:
            lines.append(
                f"- Mongo collection names MUST be exactly {collections}. "
                "Do not pluralize, camelCase, or otherwise rename them."
            )
        if models:
            lines.append(
                f"- LLM model id(s) from databaseLogic MUST appear verbatim in the source: {models}."
            )
        if creds:
            examples = " or ".join(f'creds.get("{n}")' for n in creds[:4])
            lines.append(
                f"- Secrets MUST come from request_meta['credentials'] using exact name(s) {creds}. "
                f"Example: creds = (request_meta or {{}}).get('credentials') or {{}}; api_key = {examples}. "
                "NEVER os.environ.get / os.getenv / params.get for API keys."
            )
        if requires_insert:
            lines.append(
                "- New documents MUST use await Database.insert_one(collection, document). "
                "Database.update_one is NOT upsert: zero matched = silent no-write. "
                "Only update_one when the filter is guaranteed to match an existing document."
            )
        if logical_fields:
            sample = ", ".join(repr(n) for n in logical_fields[:8])
            lines.append(
                f"- resolve_field_name / sort field names MUST use these EXACT logical "
                f"names from databaseLogic/mappings: [{sample}]. "
                'Never abbreviate (e.g. "exp" for total_years_of_experience, '
                '"dist" for network_distance). resolve_field_name is not a fuzzy matcher.'
            )
        return "\n            " + "\n            ".join(lines)

    @staticmethod
    def _source_calls_insert_one(source_code: str) -> bool:
        return bool(re.search(r"\binsert_one\s*\(", source_code or ""))

    @classmethod
    def _spec_fidelity_issue(
        cls,
        source_code: str,
        data_source: Optional[Dict[str, Any]] = None,
        input_model: Optional[Type[BaseModel]] = None,
        response_model: Optional[Type[BaseModel]] = None,
    ) -> Optional[str]:
        """Reject codegen that ignores inputModel fields, IR collections, or pinned LLM model ids."""
        if not source_code or not data_source:
            return None
        check_src = cls._code_without_comments(source_code)
        model_fields = (
            set(getattr(input_model, "model_fields", {}) or {})
            if input_model is not None
            else set()
        )
        skip_fields: Set[str] = set()
        model_name = getattr(input_model, "__name__", "") if input_model is not None else ""
        if model_fields <= {"value"} or model_name in {
            "Empty",
            "EmptyRequest",
            "EmptyParams",
        }:
            skip_fields.add("value")

        must_read: List[str] = []
        seen_must: Set[str] = set()
        if input_model is not None:
            for name, field in (input_model.model_fields or {}).items():
                if name in skip_fields:
                    continue
                is_req = True
                if hasattr(field, "is_required"):
                    try:
                        is_req = bool(field.is_required())
                    except Exception:
                        is_req = True
                if is_req and name not in seen_must:
                    seen_must.add(name)
                    must_read.append(name)
            for name in cls._params_mentioned_in_logic(
                data_source.get("databaseLogic") or ""
            ):
                if (
                    name in model_fields
                    and name not in skip_fields
                    and name not in seen_must
                ):
                    seen_must.add(name)
                    must_read.append(name)

        missing = [n for n in must_read if not cls._source_reads_param(check_src, n)]
        if missing:
            return (
                "generated code does not read inputModel field(s) "
                + ", ".join(repr(n) for n in missing)
                + ' via params.get("<name>") / params["<name>"]. '
                "Use the EXACT inputModel names; do not invent aliases "
                '(e.g. params.get("jd_id") when the model has selected_jd_id).'
            )

        declared = cls._declared_collection_names(data_source)
        if declared:
            used = cls._collection_literals_in_source(check_src)
            undeclared = [n for n in used if n not in declared]
            if undeclared:
                return (
                    "generated code uses Mongo collection "
                    + ", ".join(repr(n) for n in undeclared)
                    + f" but the IR declares {declared}. Use the EXACT collection name(s); "
                    "do not pluralize or rename them."
                )

        required_models = cls._gemini_models_in_text(
            data_source.get("databaseLogic") or ""
        )
        if required_models:
            src_lower = check_src.lower()
            missing_models = [
                mid for mid in required_models if mid.lower() not in src_lower
            ]
            if missing_models:
                return (
                    "databaseLogic requires LLM model "
                    + ", ".join(repr(m) for m in missing_models)
                    + " but generated source uses a different/missing model id. "
                    "Use that exact model id in the API URL."
                )

        cred_issue = cls._credential_access_issue(check_src, data_source=data_source)
        if cred_issue:
            return cred_issue

        abbrev_issue = cls._resolve_field_abbreviation_issue(check_src, data_source)
        if abbrev_issue:
            return abbrev_issue

        nested_issue = cls._nested_object_array_mapping_issue(
            check_src, response_model=response_model
        )
        if nested_issue:
            return nested_issue

        if write_path_requires_insert(data_source) and not cls._source_calls_insert_one(
            check_src
        ):
            return (
                "databaseLogic/operation requires Database.insert_one for new documents. "
                "Database.update_one is NOT upsert — zero matched = silent no-write. "
                "Use insert_one for new rows; use update_one only when the filter is "
                "guaranteed to match an existing document."
            )
        return None

    @classmethod
    def _nested_object_array_mapping_issue(
        cls,
        source_code: str,
        *,
        response_model: Optional[Type[BaseModel]] = None,
    ) -> Optional[str]:
        """Issue 9: reject codegen that never references nested list[Model] fields."""
        if not source_code or response_model is None:
            return None
        required_names = nested_object_array_field_names(response_model)
        if not required_names:
            return None
        check_src = cls._code_without_comments(source_code)
        missing = [
            name
            for name in required_names
            if not re.search(
                rf"""['\"]{re.escape(name)}['\"]""",
                check_src,
            )
        ]
        if not missing:
            return None
        return (
            "response model declares nested object-array field(s) "
            + ", ".join(repr(n) for n in missing)
            + " (list[ItemModel]) but generated source never references them. "
            "Map these arrays from each DB document into the returned row "
            "(e.g. candidate['experience'] = [...]); do not omit non-scalar arrays."
        )
    @staticmethod
    def _runtime_contract_issue(
        source_code: str, *, requires_schema_probe: bool = False, target_source: str = "mongodb"
    ) -> Optional[str]:
        """Detect patterns that fail on /dynamic/execute or sandbox MongoDB execution."""
        if not source_code:
            return None
        if target_source == "arcadedb":
            return BackendGenerator._arcadedb_contract_issue(source_code)
        check_src = BackendGenerator._code_without_comments(source_code)
        aliases = BackendGenerator._database_class_aliases(source_code)
        alias_pat = "|".join(
            re.escape(a) for a in sorted(aliases, key=len, reverse=True)
        )
        if re.search(rf"\b(?:{alias_pat})\.find\s*\(", check_src):
            return (
                "Database.find() does not exist — use await Database.find_to_list(collection, filters) "
                "or await Database.find_one(collection, filters)"
            )
        if re.search(rf"\b(?:{alias_pat})\.get_sync_collection\s*\(", check_src):
            return (
                "Database.get_sync_collection() is not available in the async sandbox — "
                "use await Database.upsert_one(collection, filters, updates) or other Database class methods"
            )
        if re.search(r"upsert\s*=\s*True", check_src):
            return (
                "do not pass upsert=True on collection.update_one — "
                "use await Database.upsert_one(collection, filters, updates)"
            )
        checks = [
            (r"if\s+db_wrapper\s+is\s+None", "must not guard on db_wrapper is None"),
            (r"if\s+db_wrapper\s*==\s*None", "must not guard on db_wrapper == None"),
            (r"if\s+not\s+db_wrapper\b", "must not treat db_wrapper as required (if not db_wrapper)"),
            (r"db_wrapper\s+is\s+required", "must not return db_wrapper is required errors"),
            (r"await\s+db_wrapper\.", "database must use Database from services.database, not db_wrapper"),
            (r"\bdb_wrapper\.(?:insert|update|delete|find|get_)", "database must use Database, not db_wrapper methods"),
            (r"if\s+secure_http\s+is\s+None", "must not guard on secure_http is None"),
            (r"if\s+secure_http\s*==\s*None", "must not guard on secure_http == None"),
            (r"if\s+not\s+secure_http\b", "must not treat secure_http as required (if not secure_http)"),
            (r"secure_http\s+is\s+required", "must not return secure_http is required errors"),
            (r"await\s+secure_http\.", "HTTP must use requests/httpx directly, not secure_http"),
            (r"run_in_executor\s*\(", "must not use run_in_executor for MongoDB — await Database/collection methods on the event loop"),
            (r"asyncio\.to_thread\s*\(", "must not use asyncio.to_thread for MongoDB — await Database/collection methods on the event loop"),
            (
                r"(?:async\s+)?def\s+resolve_field_name\s*\(",
                "do not define resolve_field_name inline — use from services.field_utils import resolve_field_name",
            ),
            (
                r"(?:^\s*\w+\s*=\s*|\bawait\s+)Database\s*\(\s*\)",
                "do not instantiate Database() — use Database.insert_one/get_collection class methods",
            ),
            (
                r"await\s+[\w\.]+\.aggregate\s*\([\s\S]*?\)\s*\.to_list\s*\(",
                "do not chain await coll.aggregate(...).to_list(...) — use await Database.cursor_to_list(coll.aggregate(...)) or Database.aggregate_to_list (sandbox aggregate is a coroutine)",
            ),
        ]
        # Bare await on find/aggregate cursors (not cursor_to_list / to_list)
        if BackendGenerator._has_bare_await_mongo_cursor(check_src):
            return (
                "do not await find()/aggregate() as documents — use "
                "await Database.cursor_to_list(coll.find(...)/coll.aggregate(...)) "
                "or await cursor.to_list(...)"
            )
        if BackendGenerator._resolve_field_name_call_has_incomplete_arity(check_src):
            return (
                "resolve_field_name requires TWO arguments: "
                'await resolve_field_name(collection_name, "logical_name") — '
                "never call resolve_field_name(collection_name) alone"
            )
        if not requires_schema_probe:
            checks.append(
                (
                    r"\bresolve_field_name\s*\(",
                    "new collection insert — use snake_case param keys directly; do not call resolve_field_name",
                )
            )
        # Sync pymongo on a collection handle (not await Database.*) — avoid docstring/comment false positives
        sync_coll_call = re.compile(
            r"^\s*(?:\w+\s*=\s*)?(?:(?!await\b).)*\b(collection|coll)\.(?:find_one|insert_one|update_one|delete_one)\s*\(",
            re.MULTILINE,
        )
        for match in sync_coll_call.finditer(check_src):
            line_start = check_src.rfind("\n", 0, match.start()) + 1
            line = check_src[line_start : check_src.find("\n", match.start())]
            code_part = line.split("#", 1)[0]
            if "await" in code_part:
                continue
            rel = match.start() - line_start
            if "#" in line[:rel]:
                continue
            return (
                "MongoDB collection calls must be awaited on the event loop "
                "(e.g. coll = Database.get_collection(...); doc = await coll.find_one(...))"
            )
        for pattern, message in checks:
            if re.search(pattern, check_src):
                return message
        return None

    @staticmethod
    def _skip_indented_block(lines: List[str], start: int) -> int:
        """Return line index after the block body following lines[start] (an if-guard)."""
        if start >= len(lines):
            return start
        guard_indent = len(lines[start]) - len(lines[start].lstrip())
        i = start + 1
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            line_indent = len(lines[i]) - len(lines[i].lstrip())
            if line_indent <= guard_indent:
                break
            i += 1
        return i

    @staticmethod
    def _call_attr_name(node: Any) -> Optional[str]:
        """Return attribute name for Call(func=Attribute(..., attr=name)), else None."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    @staticmethod
    def _database_class_aliases(source_code: str) -> Set[str]:
        """Names bound to the Database class (including ``import Database as Db``)."""
        aliases: Set[str] = {"Database"}
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return aliases
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module != "services.database" and not module.endswith(".database"):
                continue
            for alias in node.names:
                if alias.name == "Database":
                    aliases.add(alias.asname or "Database")
        return aliases

    @staticmethod
    def _call_receiver_is_database_class(call: ast.Call, aliases: Set[str]) -> bool:
        if not isinstance(call.func, ast.Attribute):
            return False
        recv = call.func.value
        if isinstance(recv, ast.Name) and recv.id in aliases:
            return True
        if isinstance(recv, ast.Attribute) and recv.attr == "Database":
            return True
        return False

    @classmethod
    def _has_bare_await_mongo_cursor(cls, source_code: str) -> bool:
        """True when code awaits find()/aggregate() as if they returned documents."""
        aliases = cls._database_class_aliases(source_code)
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            alias_pat = "|".join(
                re.escape(a) for a in sorted(aliases, key=len, reverse=True)
            )
            return bool(
                re.search(
                    rf"await\s+(?!(?:{alias_pat})\.)[\w\.]+\.(?:find|aggregate)\s*\([^)]*\)\s*(?![\s\n]*\.)",
                    source_code,
                )
            )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            awaited = node.value
            attr = cls._call_attr_name(awaited)
            if attr in ("find", "aggregate"):
                # Database.find is a missing class method, not a Mongo cursor.
                if (
                    attr == "find"
                    and isinstance(awaited, ast.Call)
                    and cls._call_receiver_is_database_class(awaited, aliases)
                ):
                    continue
                return True
        return False

    @classmethod
    def _fix_mongo_async_cursor_patterns(cls, source_code: str) -> str:
        """
        Rewrite Motor-style cursor misuse so code works under sandbox PyMongo async.

        Fixes:
        - await coll.aggregate(...).to_list(...)  → await Database.cursor_to_list(coll.aggregate(...), ...)
        - await coll.find(...).to_list(...)       → await Database.cursor_to_list(coll.find(...), ...)
          (find chain already works, but normalizing is harmless and consistent)
        - await coll.find(...) / await coll.aggregate(...)  → await Database.cursor_to_list(...)
        """
        if not source_code or (
            ".aggregate(" not in source_code and ".find(" not in source_code
        ):
            return source_code

        aliases = cls._database_class_aliases(source_code)

        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return cls._fix_mongo_async_cursor_patterns_regex(source_code)

        class _Rewriter(ast.NodeTransformer):
            changed = False

            def _wrap_cursor_to_list(self, cursor_call: ast.Call, to_list_call: Optional[ast.Call]):
                keywords = []
                if to_list_call is not None:
                    if to_list_call.args:
                        keywords.append(
                            ast.keyword(arg="length", value=to_list_call.args[0])
                        )
                    else:
                        for kw in to_list_call.keywords:
                            if kw.arg == "length":
                                keywords.append(kw)
                                break
                self.changed = True
                return ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="Database", ctx=ast.Load()),
                            attr="cursor_to_list",
                            ctx=ast.Load(),
                        ),
                        args=[cursor_call],
                        keywords=keywords,
                    )
                )

            def visit_Await(self, node: ast.Await):
                self.generic_visit(node)
                awaited = node.value

                # await coll.aggregate(...).to_list(...)  OR  await coll.find(...).to_list(...)
                if (
                    isinstance(awaited, ast.Call)
                    and isinstance(awaited.func, ast.Attribute)
                    and awaited.func.attr == "to_list"
                    and isinstance(awaited.func.value, ast.Call)
                ):
                    inner = awaited.func.value
                    inner_attr = (
                        inner.func.attr
                        if isinstance(inner.func, ast.Attribute)
                        else None
                    )
                    if inner_attr in ("aggregate", "find") and not (
                        inner_attr == "find"
                        and BackendGenerator._call_receiver_is_database_class(
                            inner, aliases
                        )
                    ):
                        # Skip if already Database.cursor_to_list / aggregate_to_list
                        return self._wrap_cursor_to_list(inner, awaited)

                # await coll.find(...) / await coll.aggregate(...)
                attr = (
                    awaited.func.attr
                    if isinstance(awaited, ast.Call)
                    and isinstance(awaited.func, ast.Attribute)
                    else None
                )
                if attr in ("find", "aggregate"):
                    # Database.find is a missing class method — do not wrap as a cursor.
                    if attr == "find" and BackendGenerator._call_receiver_is_database_class(
                        awaited, aliases
                    ):
                        return node
                    return self._wrap_cursor_to_list(awaited, None)

                return node

        rewriter = _Rewriter()
        new_tree = rewriter.visit(tree)
        if not rewriter.changed:
            return source_code

        # Prefer regex rewrite so we keep original indentation/comments.
        # Never ast.unparse the whole function — that rewrites unrelated code and
        # can introduce subtle breakage.
        regex_fixed = cls._fix_mongo_async_cursor_patterns_regex(source_code)
        if regex_fixed != source_code:
            return regex_fixed

        # AST saw a rare pattern regex missed — apply a line-local fallback via
        # unparsing only the changed await expressions is complex; leave unchanged
        # and let contract validation / LLM fix handle it.
        logger.warning(
            "Mongo cursor AST pattern detected but regex rewrite made no changes; "
            "leaving source unchanged for safer handling"
        )
        return source_code

    @staticmethod
    def _fix_mongo_async_cursor_patterns_regex(source_code: str) -> str:
        """Regex fallback for chained await coll.aggregate/find(...).to_list(...)."""

        def _match_call_span(src: str, open_paren_idx: int) -> Optional[int]:
            """Return index after the matching ')' for src[open_paren_idx] == '('."""
            if open_paren_idx >= len(src) or src[open_paren_idx] != "(":
                return None
            depth = 0
            in_str: Optional[str] = None
            escape = False
            i = open_paren_idx
            while i < len(src):
                ch = src[i]
                if escape:
                    escape = False
                elif in_str:
                    if ch == "\\":
                        escape = True
                    elif ch == in_str:
                        in_str = None
                else:
                    if ch in ("'", '"'):
                        in_str = ch
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            return i + 1
                i += 1
            return None

        code = source_code
        # Rewrite from the end so indices stay valid.
        # `(find)` must not match find_one / find_to_list; Database.find is not a cursor.
        aliases = BackendGenerator._database_class_aliases(source_code)
        pattern = re.compile(r"await\s+([\w\.]+)\.(aggregate|find)\s*\(")
        matches = list(pattern.finditer(code))
        for match in reversed(matches):
            recv = match.group(1)
            leaf = recv.split(".")[-1]
            if leaf in aliases or recv.endswith(".Database"):
                continue
            await_start = match.start()
            call_open = match.end() - 1  # '(' of aggregate/find
            call_end = _match_call_span(code, call_open)
            if call_end is None:
                continue
            rest = code[call_end:]
            to_list_m = re.match(r"\s*\.to_list\s*\(", rest)
            if to_list_m:
                to_list_open = call_end + to_list_m.end() - 1
                to_list_end = _match_call_span(code, to_list_open)
                if to_list_end is None:
                    continue
                cursor_expr = code[match.start(1) : call_end]
                to_list_args = code[to_list_open + 1 : to_list_end - 1].strip()
                if to_list_args:
                    replacement = (
                        f"await Database.cursor_to_list({cursor_expr}, {to_list_args})"
                    )
                else:
                    replacement = f"await Database.cursor_to_list({cursor_expr})"
                code = code[:await_start] + replacement + code[to_list_end:]
            else:
                # Bare await coll.find/aggregate(...) — only rewrite when the next
                # non-space token is not a chained method (sort/skip/limit/to_list).
                if re.match(r"\s*\.", rest):
                    continue
                cursor_expr = code[match.start(1) : call_end]
                replacement = f"await Database.cursor_to_list({cursor_expr})"
                code = code[:await_start] + replacement + code[call_end:]

        return code

    @classmethod
    def _insert_after_logger_import(cls, code: str, import_line: str) -> str:
        new_lines: List[str] = []
        inserted = False
        for line in code.splitlines():
            new_lines.append(line)
            if not inserted and re.match(r"\s*logger\s*=\s*logging\.getLogger", line):
                indent = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}{import_line}")
                inserted = True
        if not inserted:
            for idx, line in enumerate(new_lines):
                if "async def " in line:
                    indent = "    "
                    new_lines.insert(idx + 1, f"{indent}{import_line}")
                    inserted = True
                    break
        return "\n".join(new_lines)

    @classmethod
    def _fix_runtime_contract_deterministic(
        cls, source_code: str, *, requires_schema_probe: bool = False, target_source: str = "mongodb"
    ) -> str:
        """Patch common db_wrapper/secure_http/schema patterns without LLM."""
        if not source_code:
            return source_code

        lines = source_code.splitlines()
        out: List[str] = []
        i = 0
        guard_re = re.compile(
            r"^\s*if\s+(?:db_wrapper|secure_http)\s+is\s+None\s*:|"
            r"^\s*if\s+not\s+(?:db_wrapper|secure_http)\b"
        )
        while i < len(lines):
            if guard_re.match(lines[i]):
                i = cls._skip_indented_block(lines, i)
                continue
            out.append(lines[i])
            i += 1

        code = "\n".join(out)
        code = re.sub(r"await\s+db_wrapper\.", "await Database.", code)
        code = re.sub(
            r"db_wrapper\s+is\s+required[^\"']*",
            "database unavailable",
            code,
        )
        code = re.sub(
            r"secure_http\s+is\s+required[^\"']*",
            "http client unavailable",
            code,
        )

        # ArcadeDB has no resolve_field_name/cursor concerns — stop after db_wrapper/secure_http cleanup.
        if target_source == "arcadedb":
            if "Database." in code and "from services.database import Database" not in code:
                code = cls._insert_after_logger_import(code, "from services.database import Database")
            return code

        # Inline resolve_field_name → shared helper (existing collections) or snake_case (new inserts).
        had_nested = bool(
            re.search(r"(?:async\s+)?def\s+resolve_field_name\s*\(", code)
        )
        code = cls._remove_nested_def(code, "resolve_field_name")
        code = re.sub(
            r"(\w+)\s*=\s*await\s+resolve_field_name\s*\(\s*([^,]+),\s*([^,]+),\s*[^)]+\)",
            r"\1 = await resolve_field_name(\2, \3)",
            code,
        )
        code = re.sub(
            r"await\s+resolve_field_name\s*\(\s*([^,]+),\s*([^,]+),\s*[^)]+\)",
            r"await resolve_field_name(\1, \2)",
            code,
        )

        # Fix incomplete arity: resolve_field_name(collection) → add logical_name from LHS
        # or collapse to snake_case string for NEW inserts.
        def _rewrite_one_arg_assign(match: re.Match) -> str:
            lhs = match.group(1)
            collection_expr = match.group(2).strip()
            logical = cls._infer_logical_name_from_resolve_lhs(lhs)
            if not logical:
                return match.group(0)
            if requires_schema_probe:
                return f'{lhs} = await resolve_field_name({collection_expr}, "{logical}")'
            return f'{lhs} = "{logical}"'

        code = re.sub(
            r"(\w+)\s*=\s*await\s+resolve_field_name\s*\(\s*([^,\)]+)\s*\)",
            _rewrite_one_arg_assign,
            code,
        )

        if requires_schema_probe:
            if had_nested or re.search(r"\bresolve_field_name\s*\(", code):
                if "from services.field_utils import resolve_field_name" not in code:
                    code = cls._insert_after_logger_import(
                        code,
                        "from services.field_utils import resolve_field_name",
                    )
        else:
            code = re.sub(
                r"(\w+)\s*=\s*await\s+resolve_field_name\s*\(\s*[^,]+,\s*[\"']([^\"']+)[\"']\s*\)",
                lambda m: f'{m.group(1)} = "{cls._logical_name_to_snake(m.group(2))}"',
                code,
            )
            code = re.sub(
                r"await\s+resolve_field_name\s*\(\s*[^,]+,\s*[\"']([^\"']+)[\"']\s*\)",
                lambda m: f'"{cls._logical_name_to_snake(m.group(1))}"',
                code,
            )
            # Also collapse any remaining 1-arg forms without a usable LHS rewrite
            code = re.sub(
                r"(\w+)\s*=\s*await\s+resolve_field_name\s*\(\s*[^,\)]+\s*\)",
                lambda m: (
                    f'{m.group(1)} = "{cls._infer_logical_name_from_resolve_lhs(m.group(1))}"'
                    if cls._infer_logical_name_from_resolve_lhs(m.group(1))
                    else m.group(0)
                ),
                code,
            )
            code = re.sub(
                r"^\s*from services\.field_utils import resolve_field_name\s*\n",
                "",
                code,
                flags=re.MULTILINE,
            )

        # Database() instance pattern → class methods (no LLM needed).
        code = re.sub(r"^\s*\w+\s*=\s*Database\s*\(\s*\)\s*\n", "", code, flags=re.MULTILINE)
        code = re.sub(r"\bdb\.get_collection\b", "Database.get_collection", code)
        code = re.sub(r"\bawait\s+db\.insert_one\b", "await Database.insert_one", code)
        code = re.sub(r"\bawait\s+db\.update_one\b", "await Database.update_one", code)
        code = re.sub(r"\bawait\s+db\.delete_one\b", "await Database.delete_one", code)
        code = re.sub(r"\bawait\s+db\.find_one\b", "await Database.find_one", code)
        code = re.sub(r"\bawait\s+db\.find_to_list\b", "await Database.find_to_list", code)
        code = re.sub(r"\bdb\.insert_one\b", "Database.insert_one", code)

        # Motor-style aggregate/find await misuse → Database.cursor_to_list (sandbox-safe)
        code = cls._fix_mongo_async_cursor_patterns(code)

        if ("Database." in code or "await Database" in code) and (
            "from services.database import Database" not in code
        ):
            inserted = False
            new_lines: List[str] = []
            for line in code.splitlines():
                new_lines.append(line)
                if not inserted and re.match(
                    r"\s*logger\s*=\s*logging\.getLogger", line
                ):
                    indent = line[: len(line) - len(line.lstrip())]
                    new_lines.append(f"{indent}from services.database import Database")
                    inserted = True
            if not inserted:
                for idx, line in enumerate(new_lines):
                    if line.strip().startswith('"""') or line.strip().startswith(
                        "'''"
                    ):
                        continue
                    if "async def " in line:
                        continue
                    indent = "    "
                    new_lines.insert(idx + 1, f"{indent}from services.database import Database")
                    inserted = True
                    break
            code = "\n".join(new_lines)

        return code

    def _fix_runtime_contract_with_llm(
        self,
        func_id: str,
        source_code: str,
        issue: str,
        *,
        requires_schema_probe: bool = False,
        target_source: str = "mongodb",
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        if not get_gemini_api_key():
            return None

        if target_source == "arcadedb":
            db_rules = (
                "- Graph access: `from services.database import Database` then "
                "`rows = await Database.run_arcadedb_command(command, params, language=\"cypher\")`\n"
                "            - NEVER pass database= to run_arcadedb_command — subscriber routing is automatic\n"
                "            - Use MERGE for idempotent create, MATCH for reads, SET for updates, DETACH DELETE for removals"
            )
            runtime_rules_text = _arcadedb_runtime_execute_rules()
        else:
            db_rules = (
                "- For ALL database work: `from services.database import Database` then await "
                "Database.insert_one / update_one / delete_one / find_one / find_to_list "
                "(prefer over raw coll.insert_one). NEVER Database.find(...)\n"
                "            - NEVER asyncio.to_thread, run_in_executor, or functools.partial for MongoDB\n"
                "            - CURSORS: never `await coll.aggregate(p).to_list(...)` or `await coll.find(q)` / "
                "`await coll.aggregate(p)`.\n"
                "              Use `await Database.cursor_to_list(coll.aggregate(p))` or "
                "`await Database.aggregate_to_list(name, p)`\n"
                "              (sandbox PyMongo: aggregate is a coroutine; Motor returns a cursor — cursor_to_list handles both)\n"
                f"            - {'EXISTING collection: use from services.field_utils import resolve_field_name; await resolve_field_name(collection_name, EXACT_logical_name from databaseLogic — never abbreviations like exp/dist)' if requires_schema_probe else 'NEW collection insert: map snake_case params directly; do NOT use resolve_field_name'}"
            )
            runtime_rules_text = _runtime_execute_rules(requires_schema_probe=requires_schema_probe)

        prompt = f"""Fix this Python async backend function for runtime execution via /aiqod-agent/agent/dynamic/execute.

            ISSUE TO FIX: {issue}

            If the issue lists required param names, Mongo collection names, or an LLM model id,
            use those EXACT strings. Do not invent aliases (jd_id vs selected_jd_id) or pluralize
            collection names (job_descriptions vs job_description).

            RUNTIME RULES (CRITICAL):
            - KEEP the exact function signature: async def {func_id}(*, params, db_wrapper=None, secure_http=None, request_meta=None)
            - /dynamic/execute does NOT inject db_wrapper or secure_http — they are always None at runtime
            - NEVER check `if db_wrapper is None` or return errors about db_wrapper being required
            - NEVER check `if secure_http is None` or return errors about secure_http being required
            {db_rules}            - For ALL HTTP calls: import requests or httpx and call them directly
            - Keep db_wrapper and secure_http in the signature only for backward compatibility; do not use them in the body
            - Keep all imports inside the function body
            - Preserve business logic, params handling, and response shape
            - CRITICAL: output must be valid Python with consistent 4-space indentation; never merge tokens (e.g. `not x` not `notx`, `async def foo` not `async deffoo`)
            {runtime_rules_text}

            CURRENT CODE:
            ```python
            {source_code}
            ```

            Return ONLY the corrected function code, no markdown fences, no explanations."""

        try:
            response = sync_generate(
                self.backend_model_name,
                prompt,
                config=GeminiGenerateConfig(temperature=0.1, max_output_tokens=8192),
            )
            self._record_backend_llm_usage(
                response,
                project_id=project_id,
                screen_id=screen_id,
                sub_operation="backend-runtime-contract-fix",
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                metadata={"function_id": func_id, "issue": issue},
            )
            text = (response.text or "").strip()
            if not text:
                return None
            if text.startswith("```"):
                lines = text.splitlines()
                lines = lines[1:]
                text = "\n".join(lines)
            if text.endswith("```"):
                text = "\n".join(text.splitlines()[:-1])
            text = textwrap.dedent(text).strip()
            if f"def {func_id}" not in text:
                return None
            if "bson.ObjectId" in text:
                text = text.replace("bson.ObjectId", "ObjectId")
            return text
        except BillingError:
            raise
        except Exception as exc:
            logger.warning("Runtime contract LLM fix failed for %s: %s", func_id, exc)
            return None

    def _fix_generated_function_with_llm(
        self,
        func_id: str,
        source_code: str,
        issue: str,
        *,
        requires_schema_probe: bool = False,
        target_source: str = "mongodb",
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        Dedicated fixer agent: patch an already-generated function for a known issue.

        This is NOT the generation retry path (`_generate_with_llm` + error in prompt).
        The model sees the current source and the exact error (e.g. `{` closed with `)`).
        """
        if not get_gemini_api_key():
            return None

        runtime_rules_text = (
            _arcadedb_runtime_execute_rules()
            if target_source == "arcadedb"
            else _runtime_execute_rules(requires_schema_probe=requires_schema_probe)
        )
        prompt = f"""You are a Python fixer. A generated async backend function failed validation.
Your ONLY job is to fix the reported issue and return a complete, valid function.

ISSUE TO FIX (must be resolved — do not ignore):
{issue}

FIX RULES:
- KEEP the exact signature: async def {func_id}(*, params, db_wrapper=None, secure_http=None, request_meta=None)
- Return the FULL function (imports inside the body). No markdown fences, no explanations.
- Output MUST be valid Python that compiles with ast.parse / exec.
- Dict literals open with '{{' and MUST close with '}}' — never with ')' .
- List literals open with '[' and MUST close with ']' — never with ')' .
- Function calls / tuples open with '(' and MUST close with ')' — never with '}}' .
- Preserve business logic, params handling, and response field names unless they cause the error.
- ALL imports stay INSIDE the function body.
- db_wrapper and secure_http are always None at runtime — do not use them in the body.
{runtime_rules_text}

CURRENT CODE:
```python
{source_code}
```

Return ONLY the corrected function code."""

        try:
            fixer_model = model_name or self.backend_model_name
            response = sync_generate(
                fixer_model,
                prompt,
                config=GeminiGenerateConfig(temperature=0.1, max_output_tokens=8192),
            )
            self._record_backend_llm_usage(
                response,
                project_id=project_id,
                screen_id=screen_id,
                sub_operation="backend-function-fix",
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                metadata={
                    "function_id": func_id,
                    "issue": (issue or "")[:500],
                    "model": fixer_model,
                },
            )
            text = _strip_generated_python_code((response.text or "") if response else "")
            if not text:
                return None
            if f"def {func_id}" not in text:
                logger.warning(
                    "Dedicated fixer for %s returned code without function definition",
                    func_id,
                )
                return None
            if "bson.ObjectId" in text:
                text = text.replace("bson.ObjectId", "ObjectId")
            return text
        except BillingError:
            raise
        except Exception as exc:
            logger.warning("Dedicated fixer LLM failed for %s: %s", func_id, exc)
            return None

    async def _ensure_runtime_contract(
        self,
        source_code: str,
        func_id: str,
        *,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        max_fix_attempts: int = 2,
        requires_schema_probe: bool = False,
        target_source: str = "mongodb",
        data_source: Optional[Dict[str, Any]] = None,
        input_model: Optional[Type[BaseModel]] = None,
        response_model: Optional[Type[BaseModel]] = None,
    ) -> str:
        """Validate generated code against /dynamic/execute; fix before compile/save if needed."""
        code = source_code
        llm_fixes = 0

        def _issue(blob: str) -> Optional[str]:
            hit = self._runtime_contract_issue(
                blob, requires_schema_probe=requires_schema_probe, target_source=target_source
            )
            if hit:
                return hit
            return self._spec_fidelity_issue(
                blob,
                data_source=data_source,
                input_model=input_model,
                response_model=response_model,
            )

        def _patch(blob: str) -> str:
            out = self._fix_runtime_contract_deterministic(
                blob, requires_schema_probe=requires_schema_probe, target_source=target_source
            )
            return self._fix_credential_access_patterns(
                out, data_source=data_source
            )

        # Always normalize Motor-vs-sandbox cursor patterns + credential lookups first.
        code = _patch(code)

        for attempt in range(max_fix_attempts):
            issue = _issue(code)
            if not issue:
                if llm_fixes:
                    logger.info(
                        "Function %s passed runtime contract check after %d LLM fix(es)",
                        func_id,
                        llm_fixes,
                    )
                return code

            code = _patch(code)
            issue = _issue(code)
            if not issue:
                logger.info(
                    "Function %s fixed via deterministic runtime contract patch",
                    func_id,
                )
                return code

            logger.warning(
                "Function %s runtime contract issue (attempt %d/%d): %s — applying LLM fix",
                func_id,
                attempt + 1,
                max_fix_attempts,
                issue,
            )
            fixed = await run_sync(
                self._fix_runtime_contract_with_llm,
                func_id,
                code,
                issue,
                requires_schema_probe=requires_schema_probe,
                target_source=target_source,
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            if fixed:
                code = _patch(fixed)
                llm_fixes += 1
                if not _issue(code):
                    logger.info(
                        "Function %s passed runtime contract check after LLM fix",
                        func_id,
                    )
                    return code
            else:
                logger.warning(
                    "Runtime contract LLM fix unavailable for %s (attempt %d)",
                    func_id,
                    attempt + 1,
                )

        code = _patch(code)
        issue = _issue(code)
        if issue:
            raise ValueError(
                f"Function {func_id} still violates /dynamic/execute runtime contract: {issue}"
            )
        logger.info(
            "Function %s passed runtime contract after final deterministic patch",
            func_id,
        )
        return code

    def _normalize_response_fields(self, response: Dict[str, Any], response_model: Type[BaseModel]) -> Dict[str, Any]:
        """
        Normalize response field names to match the response model.
        Handles common field name mismatches like status->success, user_id->userId, etc.
        """
        if not isinstance(response, dict):
            return response
        
        # Get expected field names and types from the model
        model_fields = response_model.model_fields
        model_field_names = set(model_fields.keys())
        normalized = response.copy()
        
        # Common field name mappings (snake_case to camelCase)
        field_mappings = {
            "user_id": "userId",
            "order_id": "orderId",
            "product_id": "productId",
            "customer_id": "customerId",
            "item_id": "itemId",
            "account_id": "accountId",
        }
        
        # Apply mappings if target field exists in model and source field doesn't
        for old_name, new_name in field_mappings.items():
            if old_name in normalized and new_name in model_field_names and new_name not in normalized:
                normalized[new_name] = normalized.pop(old_name)
        
        # Handle status -> success conversion
        if "status" in normalized and "success" in model_field_names and "success" not in normalized:
            status_val = normalized.get("status")
            success_field = model_fields.get("success")
            
            # Check if success field expects a boolean
            if success_field:
                success_type = str(success_field.annotation)
                if "bool" in success_type.lower():
                    # Convert status string to boolean
                    if isinstance(status_val, str):
                        normalized["success"] = status_val.lower() == "success"
                    elif isinstance(status_val, bool):
                        normalized["success"] = status_val
                    else:
                        # Try to infer boolean from other types
                        normalized["success"] = bool(status_val) if status_val else False
                else:
                    # If success is not bool, just copy the value
                    normalized["success"] = status_val
            else:
                # Default: try to convert string "success" to boolean True
                if isinstance(status_val, str) and status_val.lower() == "success":
                    normalized["success"] = True
                else:
                    normalized["success"] = status_val
            
            # Remove status if it's not expected in the model
            if "status" not in model_field_names:
                normalized.pop("status", None)
        
        # --------------------------------------------------------------
        # Enforce strict schema types to avoid ValidationError at runtime
        # while still preserving the model's field set.
        # --------------------------------------------------------------
        def _default_for_annotation(annotation: Any, explicit_default: Any) -> Any:
            """Return a safe default for a given field annotation."""
            # If the model provides an explicit default, prefer that
            if explicit_default is not None:
                return explicit_default

            ann_str = str(annotation)
            ann_str_lower = ann_str.lower()

            if "bool" in ann_str_lower:
                return False
            if "int" in ann_str_lower:
                return 0
            if "float" in ann_str_lower or "decimal" in ann_str_lower or "number" in ann_str_lower:
                return 0.0
            if "list" in ann_str_lower:
                return []
            if "dict" in ann_str_lower or "mapping" in ann_str_lower:
                return {}
            # Default fallback for strings or unknowns
            return ""

        # Drop any fields that are not part of the model schema
        for key in list(normalized.keys()):
            if key not in model_field_names:
                normalized.pop(key, None)

        def _annotation_allows_none(annotation: Any) -> bool:
            ann_str = str(annotation).lower()
            return "nonetype" in ann_str or "optional" in ann_str or "none" in ann_str

        # Ensure every model field exists and has a non-null, type-safe value
        for field_name, field_info in model_fields.items():
            # Special handling for error field - preserve it even if None/empty,
            # but only when the annotation actually allows None.
            if field_name == "error":
                allows_none = _annotation_allows_none(field_info.annotation)
                if field_name not in normalized:
                    normalized[field_name] = None if allows_none else ""
                elif normalized[field_name] is None and not allows_none:
                    normalized[field_name] = ""
                continue
            
            if field_name not in normalized:
                normalized[field_name] = _default_for_annotation(
                    field_info.annotation,
                    getattr(field_info, "default", None),
                )
                continue

            value = normalized.get(field_name)
            # Treat explicit None as missing for non-optional fields and coerce
            if value is None:
                # Check if field is optional (has default of None)
                field_default = getattr(field_info, "default", None)
                if field_default is None:
                    # Only coerce to default if the annotation doesn't allow None
                    if not _annotation_allows_none(field_info.annotation):
                        normalized[field_name] = _default_for_annotation(
                            field_info.annotation,
                            field_default,
                        )

        return normalized

    async def _verify_function_security(
        self,
        func_id: str,
        source_code: str,
        data_source: Dict[str, Any],
        input_model: Optional[Type[BaseModel]] = None,
        response_model: Optional[Type[BaseModel]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Verify that generated function code is safe and not dangerous.
        
        Checks for:
        - Hacking attempts (SQL injection, code injection, etc.)
        - Resource abuse (bitcoin mining, infinite loops, excessive CPU/memory)
        - SSRF attacks (internal network access)
        - Malicious code execution
        - Other dangerous patterns
        
        Args:
            func_id: Function identifier
            source_code: Generated function source code
            data_source: Data source configuration
            input_model: Input Pydantic model
            response_model: Response Pydantic model
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        
        Returns:
            {"safe": bool, "reason": str} - reason only provided if safe=False
        """
        if not get_gemini_api_key():
            # If no LLM available, skip verification (not safe, but allow through)
            logger.warning("No LLM model available for security verification, skipping check")
            return {"safe": True}
        
        # Extract input model structure
        input_model_info = ""
        if input_model:
            in_fields = input_model.model_fields
            in_field_info = {name: str(field.annotation) for name, field in in_fields.items()}
            input_model_info = f"""
            
            INPUT MODEL STRUCTURE (Expected `params` structure):
            Model Name: {input_model.__name__}
            Fields: {json.dumps(in_field_info, indent=2)}
            """
        
        # Extract response model structure
        response_model_info = ""
        if response_model:
            model_fields = response_model.model_fields
            field_info = {name: str(field.annotation) for name, field in model_fields.items()}
            response_model_info = f"""
            
            RESPONSE MODEL STRUCTURE (Expected return value structure):
            Model Name: {response_model.__name__}
            Fields: {json.dumps(field_info, indent=2)}
            """
        
        # Credential context: tell LLM which credential names exist and to use request_meta["credentials"]
        available_creds = available_credentials or []
        credential_context_section = ""
        if available_creds:
            credential_context_section = f"""
            AVAILABLE CREDENTIALS FOR THIS PROJECT (use exact 'name' when reading from request_meta["credentials"]):
            {json.dumps(available_creds, indent=2)}

            CREDENTIAL USAGE RULES:
            - NEVER hardcode API keys, tokens, or passwords in the generated code.
            - Read secrets ONLY from request_meta["credentials"] using the exact 'name' from the list above.
            - Example: creds = (request_meta or {{}}).get("credentials") or {{}}; token = creds.get("github_token")
            - If a required credential is missing, return a structured error that matches the response model, e.g. {{"error": "Missing credential 'github_token'. Ask admin to configure it."}}
            - A non-empty "error" string is a hard runtime failure shown to the user; include declared output keys with empty defaults. Do not wrap in {{"ok": false, "data": ...}}.
            - Do NOT log or print raw secret values; only log credential names if needed.
            
            """
        
        verification_prompt = _build_security_verification_prompt(
            func_id=func_id,
            data_source=data_source,
            input_model_info=input_model_info,
            response_model_info=response_model_info,
            source_code=source_code,
            credential_context_section=credential_context_section,
        )
        security_verify_cached_content = await _get_backend_security_verify_cached_content(
            self.backend_model_name,
        )
        security_verify_config = GeminiGenerateConfig(
            temperature=0.1, max_output_tokens=1000
        )
        cached_security_user_prompt = _build_security_verification_cached_user_content(
            func_id=func_id,
            data_source=data_source,
            input_model_info=input_model_info,
            response_model_info=response_model_info,
            source_code=source_code,
            credential_context_section=credential_context_section,
        )

        try:
            response = await run_sync(
                _backend_sync_llm_call,
                self.backend_model_name,
                user_prompt=cached_security_user_prompt,
                full_contents=verification_prompt,
                cached_content=security_verify_cached_content,
                config=security_verify_config,
            )
            self._record_backend_llm_usage(
                response,
                project_id=project_id,
                screen_id=screen_id,
                sub_operation="backend-security-check",
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                metadata={"function_id": func_id}
            )
            
            if not response:
                logger.warning("Empty response from security verification LLM")
                return {"safe": True}  # Fail open for basic check
            
            text = (response.text or "").strip()
            
            # Clean up markdown if present
            if text.startswith("```"):
                lines = text.splitlines()
                lines = lines[1:]
                text = "\n".join(lines)
            if text.endswith("```"):
                text = "\n".join(text.splitlines()[:-1])
            
            text = text.strip()
            
            # Try to parse JSON response
            try:
                result = json.loads(text)
                safe = result.get("safe", True)
                reason = result.get("reason", "")
                
                if not safe:
                    logger.warning(
                        "Function %s flagged as potentially dangerous: %s",
                        func_id,
                        reason,
                    )
                
                return {"safe": safe, "reason": reason}
            except json.JSONDecodeError:
                # If JSON parsing fails, check if response contains danger keywords
                danger_keywords = ["unsafe", "dangerous", "malicious", "hack", "injection", "exploit"]
                text_lower = text.lower()
                if any(keyword in text_lower for keyword in danger_keywords):
                    logger.warning("Security verification response contains danger keywords, marking as unsafe")
                    return {"safe": False, "reason": "Security check detected potential issues"}
                # Otherwise, assume safe (fail open for basic check)
                return {"safe": True}
                
        except BillingError:
            raise
        except Exception as exc:
            logger.error("Security verification failed: %s", exc, exc_info=True)
            # Fail open for basic check - don't block functions due to verification errors
            return {"safe": True}

    def _generate_with_llm(
        self,
        func_id: str,
        ds_type: str,
        data_source: Dict[str, Any],
        *,
        input_model: Optional[Type[BaseModel]] = None,
        response_model: Optional[Type[BaseModel]] = None,
        previous_error: Optional[str] = None,
        security_issue: Optional[str] = None,
        dependency_info: Optional[List[Dict[str, Any]]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        collection_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
        collection_schemas_prompt: str = "",
        fn_gen_cached_content: Optional[str] = None,
        data_models: Optional[Dict[str, Any]] = None,
        target_source: str = "mongodb",
        graph_schema_hint: str = "",
        api_spec_research: Optional[str] = None,
        persistence_spec: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        Attempt to generate function code via Gemini for richer logic.
        
        Args:
            func_id: Function identifier
            ds_type: Data source type ("dynamic" or "external")
            data_source: Data source configuration
            input_model: Input Pydantic model
            response_model: Response Pydantic model
            previous_error: Previous compilation error for retry
            security_issue: Previous security issue for retry
            dependency_info: Information about dependent functions
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        """
        if not get_gemini_api_key():
            logger.warning("Gemini model not configured, skipping LLM generation for %s", func_id)
            return None

        is_arcadedb = target_source == "arcadedb"
        requires_schema_probe = (
            False
            if is_arcadedb
            else self._requires_schema_probe(data_source, collection_schemas=collection_schemas)
        )
        schema_mode = (
            "ARCADEDB_GRAPH (Cypher via Database.run_arcadedb_command; no resolve_field_name)"
            if is_arcadedb
            else "EXISTING_COLLECTION (use services.field_utils.resolve_field_name)"
            if requires_schema_probe
            else "NEW_COLLECTION_INSERT (snake_case params only; no resolve_field_name)"
        )
        func_collections = [] if is_arcadedb else collections_for_data_source(data_source)
        func_schema_hint = ""
        if collection_schemas and func_collections:
            probe_meta = collection_schemas.get("__meta__") or {}
            lines = ["Collections used by this function:"]
            if probe_meta.get("subscriber_db_exists") is False:
                lines.append(
                    "  (Subscriber DB not created yet — first screen; all collections are NEW)"
                )
            for c in func_collections:
                info = collection_schemas.get(c) or {}
                if info.get("sampled"):
                    fields = info.get("fields") or {}
                    parts = [f"{k} ({v})" for k, v in sorted(fields.items())[:30]]
                    lines.append(f"  - {c}: HAS DATA — use fields: {', '.join(parts)}")
                else:
                    lines.append(f"  - {c}: EMPTY — snake_case only")
            func_schema_hint = "\n".join(lines)

        # Get input/response model structures for prompt so the LLM cannot "guess"
        # field names or types – it must follow these contracts exactly.
        input_model_info = ""
        if input_model:
            in_fields = input_model.model_fields
            in_field_info = {name: str(field.annotation) for name, field in in_fields.items()}
            input_model_info = f"""
            
            INPUT MODEL STRUCTURE (CRITICAL - Incoming `params` MUST match these exact field names):
            {json.dumps(in_field_info, indent=2)}
            
            IMPORTANT:
            - The request body will be shaped as: {{"function_id": "{func_id}", "params": {{...}}, "request_meta": {{...}}}}
            - `params` MUST be a dict whose keys and value types match the INPUT MODEL above.
            - You MUST read every inputModel field you need via params.get("<exact_name>") using those exact names.
            - You MUST NOT expect or read fields from `params` that are not present in the INPUT MODEL.
            - You MUST NOT rename fields (e.g., do NOT use `jd_id` if the model defines `selected_jd_id`; do NOT use `user_id` if the model defines `userId`).
            """

        # Dump IR field shapes for this function's models + referenced item models only
        # (not the whole screen). Gives list-item field names when items.model is wired.
        referenced_models_info = ""
        if isinstance(data_models, dict) and data_models:
            root_names: List[str] = []
            in_name = data_source.get("inputModel")
            out_name = data_source.get("responseModel")
            if isinstance(in_name, str) and in_name:
                root_names.append(in_name)
            if isinstance(out_name, str) and out_name and out_name not in root_names:
                root_names.append(out_name)
            item_names = collect_referenced_item_model_names(data_models, root_names)
            dump: Dict[str, Any] = {}
            for name in root_names + item_names:
                if name in data_models:
                    dump[name] = format_ir_model_fields_for_prompt(data_models[name])
            if dump:
                referenced_models_info = f"""

            IR MODEL FIELD DETAILS (this dataSource only — input/response + referenced items.model):
            {json.dumps(dump, indent=2)}

            When iterating list fields that declare items.model, use that item model's field
            names exactly. Do not invent parallel field names. When comparing user-collected
            values to item fields, follow the value representation stated in databaseLogic
            (same representation on both sides).
            """
                input_model_info = (input_model_info or "") + referenced_models_info

        response_model_info = ""
        if response_model:
            model_fields = response_model.model_fields
            field_info = {name: str(field.annotation) for name, field in model_fields.items()}
            response_model_info = f"""
            
            RESPONSE MODEL STRUCTURE (CRITICAL - Your return value MUST match these exact field names):
            {json.dumps(field_info, indent=2)}
            
            CRITICAL RESPONSE FORMAT RULES:
            1. If the response model is a RootModel[List[...]], return the list DIRECTLY
            Example: return [{{...}}, {{...}}]  # NOT {{"items": [{{...}}]}}

            2. If the response model has "items" field, wrap the list
            Example: return {{"items": [{{...}}], "count": 2}}

            3. Field names must EXACTLY match the model (case-sensitive)
            - userId NOT user_id
            - success NOT status (unless model defines "status")

            4. NEVER return null/None for required fields - provide type-safe defaults:
            - string: ""
            - int/float/number: 0
            - bool: False
            - list: []
            - dict: {{}}

            4b. NESTED OBJECT ARRAYS:
            - For any list[ItemModel] field on a row/item model (e.g. experience,
              education), copy the array from the DB document into the mapped row.
            - Do not omit these keys; do not coerce object arrays through str().
            
            5. ERROR HANDLING IN RESPONSES (CRITICAL):
            - If the response model includes an "error" field, ALWAYS populate it when errors occur
            - When returning error responses, include ALL model fields with appropriate values:
              * Set "error" field to the error message string
              * Set other fields to empty/default values (empty string "", 0, False, [], {{}})
              * Example error response:
                {{
                  "documentId": "",
                  "fileName": "",
                  "filePath": "",
                  "error": "Failed to upload PDF: Invalid file format"
                }}
            - The "error" field is optional in the model, so success responses don't need to include it
            - NEVER omit fields from error responses - always return all model fields
            
            IMPORTANT: Your function must return a dictionary with these EXACT field names. 
            For example, if the model expects "success" (bool), return "success": True, NOT "status": "success".
            If the model expects "userId" (string), return "userId": "...", NOT "user_id": "...".
            """

        dependency_info_section = ""
        if dependency_info:
            dep_details = []
            for dep in dependency_info:
                dep_ds = dep.get("data_source", {})
                dep_id = dep_ds.get("id", "unknown")
                dep_config = dep.get("dependency_config", {})
                output_mapping = dep_config.get("outputMapping", {})
                execution_order = dep_config.get("executionOrder", 0)
                required = dep_config.get("required", True)
                
                dep_detail = f"""
            DEPENDENCY: {dep_id} (Execution Order: {execution_order}, Required: {required})
            Description: {dep.get("description", "N/A")}
            """
                
                # Include input/output model schemas
                if dep.get("input_schema"):
                    dep_detail += f"""
            Input Model ({dep.get("input_model", "N/A")}):
            {json.dumps(dep["input_schema"], indent=2)}
            """
                
                if dep.get("response_schema"):
                    dep_detail += f"""
            Response Model ({dep.get("response_model", "N/A")}):
            {json.dumps(dep["response_schema"], indent=2)}
            """
                
                # Include operation details for dynamic functions
                if dep.get("operation"):
                    dep_detail += f"""
            Operation Details:
            {json.dumps(dep["operation"], indent=2)}
            """
                    if dep.get("database_logic"):
                        dep_detail += f"""
            Database Logic: {dep["database_logic"]}
            """
                
                # Include config for external functions
                if dep.get("config"):
                    dep_detail += f"""
            External API Config:
            {json.dumps(dep["config"], indent=2)}
            """
                
                # Include output mapping (how this dependency's output maps to current function's input)
                if output_mapping:
                    dep_detail += f"""
            Output Mapping (how {dep_id}'s output maps to this function's input):
            {json.dumps(output_mapping, indent=2)}
            """
                
                # Include source code if available (function already generated)
                if dep.get("source_code"):
                    dep_detail += f"""
            Source Code (for reference - DO NOT copy directly, understand the logic):
            ```python
            {dep["source_code"]}
            ```
            """
                
                dep_details.append(dep_detail)
            
            if dep_details:
                dependency_info_section = f"""
            
            FUNCTION DEPENDENCIES (CRITICAL - This function depends on other functions):
            This function has dependencies that MUST be executed first. You need to understand:
            1. What each dependency function does (from its description and operation/config)
            2. What data each dependency returns (from response model schema)
            3. How to map dependency outputs to this function's inputs (from outputMapping)
            4. The execution order (lower numbers execute first)
            
            IMPORTANT DEPENDENCY RULES:
            - Dependencies with lower executionOrder MUST execute before this function
            - Use the outputMapping to extract the correct fields from dependency responses
            - If a dependency is marked as required=True, you MUST handle its output
            - If a dependency fails, handle the error appropriately based on whether it's required
            - NEVER import dependency functions (from .dep_id import dep_id or similar) — sandbox has no package parent
            - To reuse dependency logic: INLINE it as local helpers, or read outputs from params/request_meta
            
            {chr(10).join(dep_details)}
            
            DEPENDENCY USAGE PATTERN:
            - If dependencies are already executed externally, their outputs may be available in params or request_meta
            - Prefer reading dependency outputs from params/request_meta; if you need the logic yourself, INLINE it from the source-code reference (do NOT import the other function)
            - NEVER use relative imports (from .module import ...) or treat sibling generated functions as importable modules
            - Always validate dependency outputs match expected types before using them
            - Map dependency output fields to your input model fields using the outputMapping provided
            """

        security_feedback = ""
        if security_issue:
            security_feedback = f"""
            
            SECURITY VERIFICATION FAILED - CODE IS INCOMPLETE OR DANGEROUS (YOU MUST FIX THIS):
            {security_issue}
            
            YOU MUST REGENERATE THE FUNCTION WITH THESE FIXES:
            - Address the security concerns while maintaining functionality.
            - Ensure the code is safe and does not contain extremely dangerous patterns.
            - The generated code MUST be fully complete and production-ready.
            - Users have NO ACCESS to modify this code - it must work as-is.
            - If you cannot implement something fully, return a proper error message instead of placeholder code.
            DO NOT repeat the same mistake. Carefully review the security issue and implement the fix properly.
            """

        # Credential context: tell LLM which credential names exist and to use request_meta["credentials"]
        available_creds = available_credentials or []
        credential_context_section = ""
        if available_creds:
            credential_context_section = f"""
            AVAILABLE CREDENTIALS FOR THIS PROJECT (use exact 'name' when reading from request_meta["credentials"]):
            {json.dumps(available_creds, indent=2)}

            CREDENTIAL USAGE RULES:
            - NEVER hardcode API keys, tokens, or passwords in the generated code.
            - Read secrets ONLY from request_meta["credentials"] using the exact 'name' from the list above.
            - Example: creds = (request_meta or {{}}).get("credentials") or {{}}; token = creds.get("github_token")
            - If a required credential is missing, return a structured error that matches the response model, e.g. {{"error": "Missing credential 'github_token'. Ask admin to configure it."}}
            - A non-empty "error" string is a hard runtime failure shown to the user; include declared output keys with empty defaults. Do not wrap in {{"ok": false, "data": ...}}.
            - Do NOT log or print raw secret values; only log credential names if needed.
            
            GEMINI/LLM API CALLS (when dataSource description mentions Gemini, classification, extraction, or LLM processing):
            - Look for credentials with names like "gemini_api_key", "google_api_key", or service="Gemini" in the available credentials list above.
            - NEVER read URLs from request_meta["config"] for Gemini API calls - construct the URL using the API key from credentials.
            - Construct the Gemini API URL: https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={{{{api_key}}}}
            - Use proper Gemini API request payload format (JSON with "contents" array):
              {{"contents": [{{"parts": [{{"text": "your prompt here"}}]}}]}}
            - Example pattern:
              ```python
              creds = (request_meta or {{}}).get("credentials") or {{}}
              gemini_key = creds.get("gemini_api_key") or creds.get("google_api_key")
              if not gemini_key:
                  return {{"error": "Missing credential 'gemini_api_key'. Configure it in project credentials.", ...}}
              
              url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={{{{gemini_key}}}}"
              payload = {{"contents": [{{"parts": [{{"text": f"Your classification prompt: {{{{raw_text}}}}"}}]}}]}}
              response = requests.post(url, json=payload, timeout=30)
              response.raise_for_status()
              result = response.json()
              text_response = result["candidates"][0]["content"]["parts"][0]["text"]
              # Parse JSON from text_response, sanitize markdown fences, handle errors
              ```
            - For classification/extraction tasks: construct a clear prompt describing what to extract and the JSON format expected.
            - Handle JSON parsing from LLM text responses (may include markdown code fences like ```json ... ```).
            - Sanitize responses: remove markdown fences, trim whitespace, validate JSON schema.
            - Implement retry logic (max 2 retries) on JSON decode or schema validation failure.

            EMAIL/SMTP CREDENTIALS (when dataSource sends email):
            - ALWAYS: Read email secrets from request_meta["credentials"] using WELL-KNOWN keys. The platform injects decrypted values from the credentials collection:
              ```python
              creds = (request_meta or {{}}).get("credentials") or {{}}
              sender = creds.get("email_sender")
              password = creds.get("email_password")
              smtp_server = creds.get("smtp_server") or "smtp.gmail.com"
              smtp_port = creds.get("smtp_port") or 587
              if not sender or not password:
                  return {{
                      "success": False,
                      "message": "Email credentials not configured. Configure 'email_sender' and 'email_password' in project credentials.",
                      "error": "Missing email credentials",
                  }}
              ```
              Then use `sender`, `password`, `smtp_server`, `smtp_port` with smtplib.
            - Do NOT read any secrets credentials from config.json, env files, or hardcoded strings; only from request_meta["credentials"] with the keys above.
            """

        if is_arcadedb:
            system_instructions = _build_arcadedb_generation_system_instructions(
                input_model_info=input_model_info,
                response_model_info=response_model_info,
                dependency_info_section=dependency_info_section,
                security_feedback=security_feedback,
            )
        else:
            system_instructions = _build_backend_generation_system_instructions(
                requires_schema_probe=requires_schema_probe,
                input_model_info=input_model_info,
                response_model_info=response_model_info,
                dependency_info_section=dependency_info_section,
                security_feedback=security_feedback,
            )

        previous_error_info = ""
        if previous_error:
            previous_error_info = f"""
            
            PREVIOUS ATTEMPT ERROR (YOU MUST FIX THIS):
            The last generated function for id "{func_id}" failed with the following error during compilation or validation:
            {previous_error}
            
            Common fixes:
            - If "name 'X' is not defined": Move the import statement INSIDE the function body
            - If "attempted relative import with no known parent package": Remove from .xxx import yyy — inline dependency logic or read outputs from params/request_meta; never import other generated functions
            - If compilation error: Check syntax and indentation
            - If validation error: Ensure return values match response model exactly
            - If runtime contract error: remove db_wrapper/secure_http None guards; use Database and requests/httpx instead
            - If MongoDB threading/event-loop error: remove run_in_executor/to_thread; use await Database methods
            - If schema mode is NEW_COLLECTION_INSERT: remove resolve_field_name; use snake_case params + Database.insert_one
            - If schema mode is EXISTING_COLLECTION: use from services.field_utils import resolve_field_name (not inline def)
            
            You are now generating a corrected version. Avoid repeating the same mistake and ensure the new code compiles successfully.
            """

        dependency_note = ""
        if dependency_info:
            dep_ids = [dep.get("data_source", {}).get("id", "unknown") for dep in dependency_info]
            dependency_note = f"""
            
            DEPENDENCIES: This function depends on: {', '.join(dep_ids)}
            Review the dependency information above to understand:
            - What each dependency function does and returns
            - How to map dependency outputs to this function's inputs
            - The execution order and required status of each dependency
            """
        
        db_schema_section = ""
        if collection_schemas_prompt:
            db_schema_section = f"\n{collection_schemas_prompt}\n"
        if func_schema_hint:
            db_schema_section += f"\n{func_schema_hint}\n"
        if graph_schema_hint:
            db_schema_section += f"\n{graph_schema_hint}\n"

        runtime_rules_block = (
            _arcadedb_runtime_execute_rules()
            if is_arcadedb
            else _runtime_execute_rules(requires_schema_probe=requires_schema_probe)
        )
        db_requirement_bullets = (
            "- Graph operations: await Database.run_arcadedb_command(command, params, language=\"cypher\") "
            "(import from services.database); never pass database= explicitly\n"
            "            - Use MERGE for idempotent create, MATCH for reads, SET for updates, DETACH DELETE for removals"
            if is_arcadedb
            else (
                "- For database operations: await Database.insert_one/update_one/delete_one/find_one/find_to_list "
                "(import from services.database). NEVER Database.find(...) — that method does not exist.\n"
                f'            - {"For EXISTING collections: from services.field_utils import resolve_field_name; await resolve_field_name(collection, logical_name)" if requires_schema_probe else "For NEW inserts: map snake_case params directly; do NOT use resolve_field_name"}\n'
                '            - For updates: await Database.update_one(collection, filters, {"field": value}) — plain fields only, never {"$set": ...}. NOT upsert: new documents MUST use insert_one; unmatched update_one writes nothing.\n'
                "            - NEVER use asyncio.to_thread or run_in_executor for MongoDB"
            )
        )

        api_spec_section = ""
        if api_spec_research:
            api_spec_section = f"""
            EXTERNAL API SPECIFICATION (use this EXACTLY):
            {api_spec_research}

            CRITICAL: Use the endpoint URL, HTTP method, request parameters, and response field
            names from the spec above. Do NOT deviate, guess field names, or substitute a
            different host or path.
            """
        persistence_section = ""
        if persistence_spec:
            persistence_section = f"""
            {persistence_spec}

            CRITICAL: Writer and reader MUST use the SAME Mongo field name (correlationKey /
            filterKey). Do not persist or query a different batch key.
            """
        spec_fidelity_section = self._spec_fidelity_prompt_section(
            data_source, input_model
        )

        user_prompt = f"""Generate a Python async function with the following specification:

            {runtime_rules_block}
            {db_schema_section}
            {api_spec_section}
            {persistence_section}
            {spec_fidelity_section}
            Function ID: {func_id}
            Type: {ds_type}
            Schema mode: {schema_mode}
            Data Source Configuration:
            {json.dumps(data_source, indent=2)}
            {credential_context_section}
            {dependency_note}
            {previous_error_info}

            Expected signature:
            async def {func_id}(*, params, db_wrapper=None, secure_http=None, request_meta=None):
                \"\"\"[Your docstring here]\"\"\"
                # PUT ALL IMPORTS HERE (inside function body)
                # Your implementation with error handling

            Key requirements for this function:
            - Import and use ONLY the libraries you need for this specific function.
            - ALL imports must be INSIDE the function body, not outside it.
            - NEVER use relative imports (from .module import ...). Sandbox functions run in isolation via exec(). To use dependency logic, inline it or read dependency outputs from params/request_meta — do not import other generated functions.
            - ALWAYS import logging and create logger: import logging; logger = logging.getLogger(__name__)
            - ALWAYS log errors: logger.error(f"Error in {func_id}: {{str(e)}}", exc_info=True)
            - NEVER silently swallow errors - always log them before returning error responses
            {db_requirement_bullets}            - NEVER guard on db_wrapper or secure_http being None — they are always None at runtime; do not use them in the body
            - For HTTP requests: Use requests, httpx, or aiohttp directly (import the library you need)
            - For specialized tasks: Import appropriate libraries (OCR, PDF parsing, payment processing, etc.)
            - For API keys, tokens, or auth (including email/SMTP): read ONLY from request_meta["credentials"] using the exact name from config.credentials / databaseLogic (e.g. GEMINI_API_KEY). NEVER os.environ.get / os.getenv / params.get("gemini_api_key") — those are ALWAYS missing in the sandbox. NEVER hardcode secrets or use config.json. For email/SMTP, use keys like "email_sender", "email_password", "smtp_server", "smtp_port" as shown above.
            - Return helpful error messages if required config is missing
            - Handle all exceptions and return structured error responses WITH ERROR LOGGING
            - Validate params and provide clear error messages for invalid input
            - CRITICAL: When returning error responses, include error details even if returning empty/default fields
            - CRITICAL: Return values must match the response model structure EXACTLY (field names must match)
            - CRITICAL: Convert ObjectId, datetime, Decimal to JSON-serializable types before returning

            Examples:
            - OCR function: import pytesseract; from PIL import Image
            - PDF function: import PyPDF2 or import pdfplumber
            - Payment function: import stripe
            - Database function: from services.database import Database
            - HTTP function: import requests or import httpx

            Return ONLY the function code, no markdown, no explanations."""

        fn_gen_config = GeminiGenerateConfig(temperature=0.2, max_output_tokens=8192)
        cached_user_prompt = _build_backend_generation_cached_user_content(
            user_prompt,
            input_model_info=input_model_info,
            response_model_info=response_model_info,
            dependency_info_section=dependency_info_section,
            security_feedback=security_feedback,
        )
        llm_model = model_name or self.backend_model_name

        try:
            response = _backend_sync_llm_call(
                llm_model,
                user_prompt=cached_user_prompt,
                full_contents=[system_instructions, user_prompt],
                cached_content=fn_gen_cached_content,
                config=fn_gen_config,
            )
            # Record token usage for this backend LLM call
            self._record_backend_llm_usage(
                response,
                project_id=project_id,
                screen_id=screen_id,
                sub_operation="backend-function-gen",
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                metadata={"function_id": func_id, "data_source_type": ds_type}
            )

            if not response:
                logger.warning("Empty response object from Gemini for %s", func_id)
                return None

            text = ""
            try:
                text = (response.text or "").strip()
            except Exception:
                text = ""

            if not text and getattr(response, "candidates", None):
                for candidate in response.candidates:
                    parts = getattr(candidate, "content", None) and getattr(candidate.content, "parts", None)
                    if not parts:
                        continue
                    part_text = "\n".join(getattr(part, "text", "") for part in parts if getattr(part, "text", ""))
                    if part_text:
                        text = part_text.strip()
                        break

            if not text:
                ratings = getattr(response.candidates[0], "safety_ratings", None) if getattr(response, "candidates", None) else None
                logger.warning(
                    "Gemini returned no textual content for %s. Safety ratings: %s",
                    func_id,
                    ratings,
                )
                return None
            
            # Clean up markdown fences if present
            if text.startswith("```"):
                lines = text.splitlines()
                # Remove first line (```python or ```)
                lines = lines[1:]
                text = "\n".join(lines)
            if text.endswith("```"):
                text = "\n".join(text.splitlines()[:-1])
            
            text = textwrap.dedent(text).strip()

            if f"def {func_id}" not in text:
                logger.warning("Generated code for %s missing expected function definition", func_id)
                return None
            
            # Basic security validation - check for dangerous patterns
            dangerous_patterns = [
                r'\beval\s*\(',
                r'\bexec\s*\(',
                r'\b__import__\s*\(',
                r'\bcompile\s*\(',
            ]
            
            for pattern in dangerous_patterns:
                if re.search(pattern, text):
                    # Treat dangerous patterns as a generation *error* so the retry
                    # loop in `_create_function_entry` can surface this message via
                    # `previous_error` back into the next LLM prompt.
                    message = (
                        f"Generated code for {func_id} contains dangerous pattern: {pattern}"
                    )
                    logger.warning(message)
                    # Raising here ensures `_create_function_entry` captures this in
                    # `last_error` and passes it to the LLM on the next attempt.
                    raise ValueError(message)

            if 'bson.ObjectId' in text:
                logger.warning(
                    "Generated code for %s contains 'bson.ObjectId' - fixing to use 'ObjectId' directly",
                    func_id
                )
                # Replace bson.ObjectId with ObjectId
                text = text.replace('bson.ObjectId', 'ObjectId')

            return text
        except BillingError:
            raise
        except Exception as exc:
            logger.warning("Gemini function generation failed for %s: %s", func_id, exc)
            return None

    def _compile_function(self, func_name: str, source_code: str) -> Callable[..., Any]:
        """
        Compile function code for validation.
        syntax + structure validation, not runtime execution.

        LLM Code
        ↓
        Syntax + import validation
        ↓
        Async contract enforcement
        ↓
        Ready for sandbox execution
        
        NOTE: This is for compilation/validation only. Actual execution happens in
        the sandbox executor which has full Python environment. We allow all imports
        here to match the sandbox environment.
        """
        import builtins
        
        # Full Python environment - allow all imports (matching sandbox executor)
        # The actual execution happens in sandbox_function_executor with full restrictions
        # removed, so we need to allow the same imports here for compilation
        execution_globals = {
            "__builtins__": builtins.__dict__,  # Full builtins access
            "__name__": "__main__",
            # Provide common modules that might be imported
            "asyncio": asyncio,
            "json": json,
            "re": re,
            "math": math,
            "datetime": datetime,
            "timedelta": timedelta,
            "date": date,
            "Decimal": Decimal,
            "string": string,
            "uuid": uuid,
            "hashlib": hashlib,
            "base64": base64,
            "time": time,
            "urllib": urllib,
            "logger": logger,
            "ObjectId": ObjectId,
            # Provide Database access for direct imports
            "Database": Database,
        }
        
        # Try to import common external libraries that might be used
        # If they're not installed, that's fine - they'll fail at runtime in sandbox
        try:
            import requests
            execution_globals["requests"] = requests
        except ImportError:
            pass
        
        try:
            import httpx
            execution_globals["httpx"] = httpx
        except ImportError:
            pass
        
        # Allow imports from services module (for Database, etc.)
        import sys
        import importlib
        backend_dir = Path(__file__).parent.parent
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        
        local_vars: Dict[str, Any] = {}
        
        try:
            # Compile the source code
            exec(source_code, execution_globals, local_vars)
        except ImportError as e:
            # Import errors during compilation are OK since imports are inside function
            # They'll be resolved during actual execution in the sandbox
            logger.warning(f"Import error during compilation (expected if imports are inside function): {e}")
            # Re-execute without caring about imports
            pass
        except Exception as e:
            # Other errors should still be raised
            logger.error(f"Compilation error for {func_name}: {e}")
            raise
        
        func = local_vars.get(func_name)
        if not func or not asyncio.iscoroutinefunction(func):
            raise ValueError(f"Generated function {func_name} is not async")
        return func

    def _infer_data_models_from_sources(self, data_sources: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        """Infer appropriate data models from data source definitions (format 1)."""
        data_models = {}

        def _f(type_name: str, required: bool = False) -> Dict[str, Any]:
            return {"type": type_name, "required": required}
        
        for ds in data_sources:
            ds_type = (ds.get("type") or "dynamic").lower()
            func_id = ds.get("id", "unknown")
            
            # Create input model
            input_model_name = ds.get("inputModel") or f"{func_id.capitalize()}Input"
            if input_model_name not in data_models:
                if ds_type == "external":
                    # For external APIs, create flexible input model
                    data_models[input_model_name] = {
                        "params": _f("dict"),
                        "headers": _f("dict"),
                        "query": _f("dict"),
                    }
                else:
                    # For dynamic/database operations
                    operation = ds.get("operation", {})
                    action = (operation.get("action") or "insert").lower()
                    if action in ["insert", "update"]:
                        data_models[input_model_name] = {
                            "payload": _f("dict"),
                            "filters": _f("dict"),
                            "updates": _f("dict"),
                        }
                    else:
                        data_models[input_model_name] = {
                            "filters": _f("dict"),
                            "projection": _f("dict"),
                            "limit": _f("int"),
                        }
            
            # Create response model
            response_model_name = ds.get("responseModel") or f"{func_id.capitalize()}Response"
            if response_model_name not in data_models:
                data_models[response_model_name] = {
                    "status": _f("string"),
                    "data": _f("dict"),
                    "message": _f("string"),
                    "error": _f("string"),
                }
        
        # Ensure we have at least one model
        if not data_models:
            data_models = {
                "GenericRequest": {"payload": _f("dict")},
                "GenericResponse": {
                    "status": _f("string"),
                    "data": _f("dict"),
                    "message": _f("string"),
                    "error": _f("string"),
                },
            }
        
        return data_models

__all__ = [
    "BackendGenerator",
    "FunctionRegistry",
    "SecureHTTPClient",
    "DatabaseWrapper",
    "BackendFunctionEntry",
]

