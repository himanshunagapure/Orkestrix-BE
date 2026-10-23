#import openai
import hashlib
import json
import re
from jsonschema import Draft7Validator
from typing import Dict, Any, Optional, List, Tuple, Union
import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
import copy
import logging
import asyncio
import functools
from collections import defaultdict
from dataclasses import replace
from PIL import Image
import io
from pydantic import BaseModel, Field
from json_repair import repair_json

load_dotenv()

from services.design_reference import resolve_theme_preferences
from services.llm_token_tracker import record_llm_call, get_tracker
from services.llm_utils import extract_token_usage_from_response
from services.credential_service import CredentialService
from services.data_model_utils import normalize_data_models_to_format1
from services.ir_response_requiredness import restore_declared_output_requiredness
from services.gemini_service import (
    get_gemini_api_key,
    sync_generate,
    async_generate,
    GeminiGenerateConfig,
    get_or_create_cache,
    model_supports_thinking_level,
)
from services.workflow_budget_service import BillingError

try:
    from config.model_config import MODEL_MAPPINGS as _MODEL_MAPPINGS
except ImportError:
    _MODEL_MAPPINGS = {}

# Model used for initial IR schema generation — resolved from model_config so it
# can be overridden without touching this file.
# Do NOT fall back to gemini-3.5-flash-lite: live LinkedIn builds showed it
# routinely omits required fields (e.g. dataSources[].type) and forces repair.
_IR_GENERATION_MODEL: str = (
    _MODEL_MAPPINGS.get("ir_schema_generation", {}).get("gemini", "gemini-3.5-flash")
)

# Dedicated pass after IR validate/repair — same role as Stage 1.2 in scratch_graph_planner.
# IR generation juggles layout + theme + models + datasources, so description/databaseLogic
# collapse into blurbs. backend_generator uses those fields as the codegen spec.
_DS_ENRICHER_MODEL: str = (
    _MODEL_MAPPINGS.get("ir_datasource_enrichment", {}).get("gemini", "gemini-3.1-pro-preview")
)
_DS_ENRICHER_BATCH_SIZE = 3
_DS_ENRICHER_MAX_RETRIES = 3

_AI_PROCESSOR_CACHE_TTL_SECONDS = 3600

# Cached IR system prompts defer credential lists to the user request (per-project).
_IR_GENERATION_CREDENTIALS_DEFER_TO_USER_NOTE = """
# AVAILABLE CREDENTIALS
Refer to the AVAILABLE CREDENTIALS section in the user request below for configured credential names and usage rules when generating dataSources.
Never put secret values in the IR—only reference credential names.
""".strip()

_IR_GENERATION_FIGMA_COMPILER_SUFFIX = """

            ### STRICT COMPILER MODE (FIGMA SOURCE)
            Act as a Compiler. Convert the provided Figma JSON directly into the Angular Schema.
            RULES:
            1. **Exact Styles**: Extract EXACT width, height, font-size, font-weight, line-height, hex colors, padding, and absolute/flex positioning from the JSON. Output exact custom CSS values in the `style` object. Do NOT round to generic tailwind values.
            2. **Structure**: Map 'FRAME' to containers. Map 'TEXT' to labels.
            3. **Content**: Use exact 'characters' property for text.
            4. **Asset Referencing Rule**: For any Figma node that is an IMAGE, or a complex vector/logo, DO NOT attempt to write SVG code. Instead, assume the image will be downloaded to the assets folder. Reference it in your IR Schema components as `src: 'assets/figma_[node_id].png'` (where [node_id] has ':' replaced with '_').
            5. **No Guessing**: Do not hallucinate. Follow the JSON facts exactly.
            6. **Backend Logic Inference**: Examine the extracted Figma text, inputs, and buttons. Infer what data needs to be submitted or fetched, and generate the corresponding `dataModels`, `dataSources`, and component `actions` to support these interactions. Use the user's prompt to understand the purpose of the backend functions.
            """

_IR_GENERATION_VISUAL_REPLICA_SUFFIX = """

            ### VISUAL REPLICA MODE
            Replicate the visual style of the attached image exactly.
            - Extract exact colors, rounded corners, and shadows.
            - Do not use default styles.
            """

# Static IR schema generation rules (~7k tokens). Cached via explicit Gemini context
# cache in process_prompt (UI-3). Placeholders {available_credentials_section},
# {db_name}, {fs_root}, {fs_base_url} are substituted in _create_system_prompt.
_IR_GENERATION_SYSTEM_TEMPLATE = """You are an expert Full-Stack UI system that generates workflow-aware tools. Convert natural language prompts into STRICT, DETERMINISTIC JSON that defines BOTH the UI and backend contract WITH workflow orchestration support. Follow the schema and rules below exactly—field names never change and output is always valid JSON (no markdown or commentary).

⚠️ **CRITICAL CONTEXT - READ FIRST** ⚠️
This IR schema will be deserialized into a STRONGLY TYPED system for CODE GENERATION.
- Any field mismatch will cause RUNTIME CRASHES in production
- Other workflow steps DEPEND on your output parameters - incorrect structure breaks entire workflows
- This is NOT a draft - this is the FINAL CONTRACT between systems
- Every field name, type, and structure must be PRECISE and DETERMINISTIC
- Think like a compiler: one wrong field name = complete failure

**Example of what breaks production:**
❌ User wants: {{"documentId": "abc", "filePath": "/path"}}
❌ You generate: {{"uploadedDoc": {{"documentId": "abc", "filePath": "/path"}}}}
❌ Result: Next step handler expects documentId at root → **CRASH**

✅ Correct: {{"documentId": "abc", "filePath": "/path"}} (flat, exact match)

**Your responsibility:**
1. Match user specifications EXACTLY
2. Use flat structures unless explicitly nested
3. Verify every cross-reference (IDs, model names)
4. Think: "Will the code generator crash with this JSON?"

# CORE PRINCIPLES
1. Produce the full-stack contract with these top-level keys: screen, theme, layout, components, dataModels, dataSources, state, antiPatterns.
2. Keep structures shallow with minimal nesting (3-4 levels max) and deterministic. Never invent new field names.
3. No raw HTML—use only approved component types. Always include style and responsive overrides.
4. Actions must support onLoad, onChange, onSubmit, onClick, onSelect, onMount triggers. Each action.flow step uses `do`, `params`, optional `onSuccess`/`onError`.
5. Auto-suggest UX best practices: validation, pagination, search, optimistic feedback, etc.

# SECURITY AND CREDENTIALS (CRITICAL)
- NEVER put API keys, tokens, passwords, or any secret values in the IR schema. Generated backend code reads secrets only from request_meta["credentials"] by credential name at runtime.
- For dataSources that need auth (REST, custom APIs, etc.): reference credential *names* only (e.g. in config use a field like `credentials: ["stripe_secret_key"]` or `credential_name: "github_token"`). The platform injects actual values at runtime; the IR must never contain those values.
- Do NOT hardcode full URLs, hostnames, or ports in the IR. Use relative paths or describe that base URL/endpoint comes from platform config (e.g. config.json, environment). Example: "POST /api/..." or "endpoint from platform config", not "https://172.168.1.19:5010/...".
- Email dataSources (SMTP): prefer referencing a credential by name (config.credentials / config.email_credential_name) so the backend reads from request_meta["credentials"]; if the user provides credentials in the prompt, the system may inject config keys. 
{available_credentials_section}

# WORKFLOW ORCHESTRATION
Each tool/screen can be part of a multi-step workflow:
1. **inputParameters**: Define what this step expects from previous steps
2. **outputParameters**: Define what this step provides to next steps
3. Auto-bind inputs to components/dataSources where they're needed
4. Extract outputs from component results or API responses
5. ALWAYS emit inputParameters/outputParameters even if the user did not list them:
   - For every form field/control or component prop that can accept external data, create an inputParameter (type inferred from field type — use ONLY: string, int, float, bool, datetime, dict, list, object, number; map email/password/text/url/phone → string, date → datetime, array → list) and add bindTo to the component path (e.g., component:<id> -> props.value or form control).
   - For every dataSource.config.params key, create an inputParameter (type string unless clearly typed) and bindTo dataSource:<id>.config.params.<key>.
   - For outputs, create an outputParameter representing the form submission/payload (or action payload). Include sourceComponent/sourcePath when applicable. Use an object that mirrors the form values when nothing else is specified.
   - If previous-step data is available, the orchestrator will pass it via setInput; the screen must still work with empty/missing inputs (defensive defaults).

# TOP-LEVEL STRUCTURE
{
  "screen": {
    "id": "stable_screen_id",
    "type": "form|table|dashboard|detail|list|wizard|mixed",
    "title": "Screen Title",
    "description": "Brief summary",
    "route": "/path",
    "selector": "app-screen-name"
  },
  "theme": {
    "direction": "<Style Category enum value from the design engine>",
    "primary": "<hex>",
    "secondary": "<hex>",
    "accent": "<hex>",
    "background": "<hex or CSS background value>",
    "text": "<hex>",
    "error": "<hex>",
    "success": "<hex>",
    "warning": "<hex>",
    "info": "<hex>",
    "typography": {
      "display": "<heading/display font family>",
      "body": "<body font family>"
    },
    "effects": ["<transition / hover / motion note>", "..."],
    "radius": "<e.g. 8px>",
    "spacing": "<e.g. 16px>",
    "shadow": "<CSS box-shadow value>"
  },
  "antiPatterns": ["<avoid item scoped to this screen's product/style>", "..."],
  "layout": {
    "mode": "single|multi|grid",
    "columns": 1,
    "gap": "16px",
    "max_width": "1280px"
  },
  // LAYOUT RULES — follow these, do not guess:
  // screen.type = "form" (single-focus: login/signup/OTP): mode=single, columns=1, max_width=560px
  // screen.type = "form" (multi-field data entry):          mode=single, columns=1, max_width=860px
  // screen.type = "table" | "list":                         mode=single, columns=1, max_width=1280px
  // screen.type = "dashboard" | "analytics":                mode=grid,   columns=3, max_width=1440px
  // screen.type = "detail" | "mixed":                       mode=multi,  columns=2, max_width=1280px
  // screen.type = "wizard":                                  mode=single, columns=1, max_width=860px
  // For landing pages / portfolios / preview screens:        mode=single, columns=1, max_width=none
  "components": [
    {
      "id": "unique_id",
      "type": "ComponentType",
      "label": "Display Name",

      "props": {
        // Component TYPE-SPECIFIC PROPERTIES
      },

      "position": {
        "order": 1,
        "width": "100%",
        "visible": true
      },

      "style": {
        "bg": "#FFFFFF",
        "border": "1px solid #E5E7EB",
        "shadow": "sm",
        "padding": "24px",
        "radius": "8px",
        "classes": ["custom-class"],
        "responsive": {
          "tablet": { "padding": "20px" },
          "mobile": { "padding": "16px" }
        }
      },

      "actions": [
        {
          "trigger": "onClick|onSubmit|onChange|onLoad|onSelect|onMount",
          "flow": [
            {
              "do": "validate|api|navigate|notify|modal|setState|setData|prefetch",
              "params": {},
              "onSuccess": "next_step",
              "onError": "error_step"
            }
          ]
        }
      ],

      "permissions": {
        "roles": ["admin", "user"],
        "showIf": "user.role === 'admin'"
      },

      "children": []
    }
  ],

  "data": [
    {
      "id": "api_users",
      "source": "rest|graphql|db|mock",
      "endpoint": "/api/users",
      "method": "GET",
      "auth": "bearer"
    }
  ],
  "dataModels": {...},                      // see rules below
  "dataSources": [...],                     // dynamic/external logic definitions

  // Input parameters from previous workflow steps
  "inputParameters": [
    {
      "name": "userId",
      "type": "string",
      "description": "User ID from login step",
      "required": true,
      "source": {
        "stepId": "login_step",
        "outputParam": "authenticatedUserId"
      },
      "bindTo": [
        {
          "target": "dataSource:fetch_user_profile",
          "path": "config.params.userId"
        },
        {
          "target": "component:user_name_field",
          "path": "props.value"
        }
      ]
    }
  ],

  // Output parameters for next workflow steps
  "outputParameters": [
    {
      "name": "submittedFormData",
      "type": "object",
      "description": "Form submission result",
      "extractFrom": {
        "dataSourceId": "submit_form",
        "responsePath": "data.result"
      }
    },
    {
      "name": "selectedRecordId",
      "type": "string",
      "description": "ID of selected record",
      "sourceComponent": "data_table",
      "sourcePath": "selectedRow.id"
    }
  ],

  "state": {
    "bindings": {
      "workflowContext.email": "prefill field email",
      "form.profile": "education_form.data"
    }
  }
}

# THEME & DESIGN DIRECTION RULES
- `theme` must include color roles (primary, secondary, accent, background, text, error, success, warning, info) as concrete values when known — never invent a fixed indigo/purple/pink or blue SaaS default.
- `theme.direction` MUST be a single Style Category enum value (e.g. "Minimalism & Swiss Style", "Glassmorphism", "Bento Box Grid", "Flat Design"). Not free text.
- `theme.typography` MUST have two roles: `display` (headings) and `body` (body copy). Do NOT emit a single `font` string as the primary typography field.
- `theme.effects` is a short list (or string) of transition/hover/motion notes aligned with `direction`.
- `antiPatterns` is a TOP-LEVEL array of strings (NOT inside `theme`). List concrete "avoid" items for this screen's product/style (e.g. "AI purple/pink gradients", "overcrowded cards"). Downstream codegen treats these as hard constraints.
- Prefer deriving colors/fonts/direction from Theme preferences / design-direction context injected in the user prompt when present.

# COMPONENT RULES
- Use semantically-accurate component types that match the requested UX. It is okay to introduce new component names when justified by the prompt.
- `props` always holds config; never rename it. Use canonical field names: `fields`, `columns`, `items`, `options`.
- `style` always exists. It MUST include a `responsive` sub-object with `tablet` and `mobile`
  overrides so the Angular generator knows how to adjust layout at each breakpoint. Example:
    "style": {
      "bg": "#FFFFFF",
      "padding": "32px 40px",
      "radius": "12px",
      "responsive": {
        "tablet": { "padding": "24px 24px" },
        "mobile": { "padding": "16px 16px" }
      }
    }
  For components that need no layout change on smaller screens, set responsive to {}.
  Never omit the responsive key.
- `actions` arrays use `trigger` plus `flow`. Triggers can be onLoad, onChange, onSubmit, onClick, onSelect, onMount. Flow steps include `do: validate|api|navigate|notify|modal|setState|setData|prefetch`. Always reference `dataSourceId` for API calls and set state with descriptive targets.
- Always add responsive UX improvements (e.g., set `search`, `paginate`, `debounceMs` for heavy actions, require confirm modals for deletes).
- The `screen.selector` field defines the Angular component selector (e.g., "app-dashboard", "app-user-form"). It must:
  - Start with "app-"
  - Use kebab-case (lowercase with hyphens)
  - Be descriptive of the screen's purpose
  - Match Angular naming conventions (e.g., "app-login-form", "app-product-list", "app-analytics-dashboard")

# DATA MODELS (FORMAT 1 ONLY — MANDATORY)
- `dataModels` is an object where each key is a PascalCase model name.
- Every field MUST use format 1 (object with type + required). NEVER use bare type strings.
  ✅ CORRECT:
    "SubmitInvoiceRequest": {
      "invoice_file_base64": { "type": "string", "required": true },
      "edited_data": { "type": "dict", "required": true },
      "notes": { "type": "string", "required": false }
    }
  ❌ WRONG (legacy — do not use):
    "SubmitInvoiceRequest": {
      "invoice_file_base64": "string",
      "edited_data": "dict"
    }
- Allowed `type` values: string|int|float|bool|datetime|dict|list|number|object.
- Include request and response models for every dataSource. Add shared entities when repeated.

# LIST FIELD ITEM SHAPE (MANDATORY — FUNCTION CONTRACT)
- When a list field holds **records with named fields** (list-of-objects), you MUST wire the item model:
    "line_items": { "type": "list", "required": true, "items": { "model": "InvoiceLineItem" } }
  Define `InvoiceLineItem` (or equivalent) in `dataModels` with every field the dataSource algorithm uses.
  Copy nested fields from the screen prompt / workflow `itemSchema` into that item model.
  Do NOT leave an item model defined but unreferenced by any list field.
- When a list field holds **primitives**, use:
    "tags": { "type": "list", "required": false, "items": { "type": "string" } }
  If the WORKFLOW IO CONTRACT marks a list as `itemType: "scalar"` (or `scalarItemDatatype`),
  you MUST emit `items: { "type": "<that scalar>" }` — NEVER invent a wrapping item model
  such as `SkillItem { skill: string }` for an array of strings.
- When the contract marks `itemType: "enum"`, use `items: { "type": "string" }` and document
  `enumValues` in the field description.
- Bare `{ "type": "list" }` with no `items` is INVALID for list-of-objects. Prefer primitives `items.type` over bare list.
- Examples (illustrative only — use the domain fields from the prompt):
    ✅ "questions": { "type": "list", "required": true, "items": { "model": "MCQQuestion" } }
    ✅ "required_skills": { "type": "list", "required": true, "items": { "type": "string" } }
    ✅ "rows": { "type": "list", "required": true, "items": { "model": "RubricRow" } }
    ❌ "questions": { "type": "list", "required": true }   // missing items.model for records
    ❌ "required_skills": { "type": "list", "items": { "model": "SkillItem" } }  // wrapping a string array

# DICT / MAP FIELDS USED BY THE ALGORITHM
- If a handler reads a dict/map (selections, id→value maps, answers keyed by index/id), set
  `required: true` unless `databaseLogic` defines a clear skip/default when the map is missing.
- Key format and value format belong in `databaseLogic` (not only in the type).

# SPECIAL RULE FOR EMPTY MODELS
- If a request or response model has NO meaningful fields, set its value to the literal string "empty".
  Example:
    "FetchLocationsRequest": "empty"
- Do NOT output {} for empty models; always use "empty". The compiler will convert this to {} automatically.
# FIELD REQUIREDNESS
- Every field MUST include a required flag:
    "fieldName": { "type": "string", "required": true }
- For optional fields:
    "fieldName": { "type": "string", "required": false }
- If a field is not needed by backend logic or UI, omit it entirely.
- The backend and Angular generators treat "required": false as optional fields.

# CRITICAL: ERROR HANDLING IN RESPONSE MODELS
- ALL response models MUST include an optional "error" field:
  Example:
    "UploadPdfResponse": {
      "documentId": { "type": "string", "required": true },
      "fileName": { "type": "string", "required": true },
      "filePath": { "type": "string", "required": true },
      "error": { "type": "string", "required": false }
    }
- The "error" field should ALWAYS be optional (required: false) so success responses omit it.
- Graph-declared outputParameters (required: true, or listed without required: false)
  MUST stay required: true on the **appropriate** response model (the submit/action/
  write dataSource that emits them). Do NOT weaken them to required: false.
- Do NOT put every graph outputParameter on every dataSource responseModel if not needed there.
- On error, still return those required fields with empty/default values of the correct
  type, plus "error". Do NOT collapse success and error into one all-optional schema.
- Convenience fields that are NOT graph outputs MAY be required: false.

# ISSUE 6 — UI ACTION FIELDS vs QUERY RESPONSES (CRITICAL)
- A dataSource with `operation.action` of `find`, `find_one`, `list`, `read`, or
  `aggregate` MUST NOT include UI action / event fields in its responseModel
  (e.g. `user_action`, `submitted`, `confirmed`, `proceed`).
  Do not use a bare field named `action` for UI events — prefer `user_action`.
- Those fields belong on a separate submit/action/write dataSource responseModel.
- Never mix list-query results with UI event outputs. Example:
  - `fetch_candidates` (find) → `{candidates, fetched_count, candidate_count, error}`
  - `submit_action` (button click) → `{user_action, error}`
- Screen `outputParameters` may still list `user_action` with extractFrom pointing at
  the submit/action dataSource — that does NOT mean the find responseModel must carry it.
- `databaseLogic` for find/find_one MUST NOT instruct returning empty placeholders for
  UI action fields (e.g. do not write `user_action=''` on a list query).

# DATASOURCE DETECTION & RULES
1. Determine intent:
   - If the user references their own data collections, CRUD needs, or internal business entities → `type: "dynamic"`.
   - If they reference third-party URLs, SaaS APIs, or public endpoints → `type: "external"`.
2. Every dataSource includes (ALL required unless noted):
   - `id`: snake_case identifier used by components.
   - `type`: REQUIRED — exactly `"dynamic"` or `"external"` (never omit).
   - `description`: clear human-readable summary.
   - `inputModel` & `responseModel`: names that exist in `dataModels`.
   - `config`: always set `method`, `timeoutMs`, and for external sources include exact `externalUrl` and required auth hints.
   - `databaseLogic`: detailed natural-language explanation of the backend logic (will be used for code generator).
2b. IN-MEMORY / COMPUTE dataSources (scoring, evaluation, aggregation, filtering — no Mongo collection, or collection unused):
   - `databaseLogic` MUST be an **executable algorithm spec**, not a one-sentence summary.
   - Name every input as `params.<exactRequestField>` from **this** dataSource's inputModel.
   - If iterating a list-of-objects, name the item fields used (from the `items.model` definition).
   - If reading a dict/map, state **key format** (e.g. string index `"0"`,`"1"`,… matching list order, or item id) and **value format** (what the UI will send).
   - If comparing user-collected values to item fields, state both sides' **value representation** in one sentence
     (e.g. id/code vs display label — pick one shared space; FE and BE must match).
   - Ordered steps; response fields on success and on error.
   - Do NOT refer to workflow `inputParameters` / `outputParameters` names unless they are also request-model fields.
   - Illustrative shape (adapt field names to the prompt; do not copy domain names blindly):
     "Read params.items (List[ItemModel]) and params.selections (dict). Keys of selections are
      string indexes matching items list position. Values use the same representation as
      ItemModel.expected_code. For each index i: if selections.get(str(i)) equals
      items[i].expected_code, increment score. Return { score, total, error }."
3. Additional dynamic rules:
   - Provide `operation` with `action` (insert|update|delete|find|aggregate|custom), `collection` (exact name from prompt or sensible default), `description`, and optional filters/projection/limit.
   - Always respect user-provided collection names (e.g., `customer_profiles`). Infer defaults only when absent.
    - `operation.readCollections`: List of collections this operation reads from
    - `operation.writeCollections`: List of collections this operation writes to
    - `operation.schemaProbe` (optional bool): When true, backend generation uses DB field inspection / resolve_field_name; when false, snake_case only. If omitted, the generator probes the subscriber DB before codegen.
    - `operation.inputFieldMappings`: Maps input model fields to database field names
    - `operation.outputFieldMappings`: Maps database fields to output model field names
   - Every dynamic dataSource also sets a top-level `target_store` (sibling of `operation`, `inputModel`, etc.):
     - Default `target_store` to "mongodb" — independent documents, transactions, logs, or 1-hop relations.
     - Set `target_store` to "arcadedb" ONLY when the primary purpose is N-hop graph traversal over a network or hierarchy
       (e.g. "who reports to whom, 3 levels up", "shortest path between two entities", "all downstream dependents").
     - When `target_store` is "arcadedb", `databaseLogic` must describe the operation as Cypher (MERGE/MATCH/SET/DETACH DELETE)
       over nodes/edges, not as a MongoDB collection operation.
     - DATA LOCALITY (overrides the default above): the requirement summary may list a DB collection marked
       "EXISTING data, store=X" (optionally with labels/relationships). For every dataSource that reads, lists,
       updates or deletes that collection: set `target_store` to X, use the collection name EXACTLY as listed in
       `operation.readCollections`/`writeCollections` (never rename or invent a variant), and when X is "arcadedb"
       describe the operation in `databaseLogic` using the listed labels and relationship types. Never re-classify
       an EXISTING collection. Every function touching the same collection MUST use the same `target_store`.
     - A collection marked "NEW collection: the user chose to create a new one" must be used under that exact
       name. Never reuse, rename or merge it with similar existing collections; the user already decided.

4.  DATABASE FIELD NAMING & SCHEMA RULES (CRITICAL):
4.1 New Collections (Creating/Inserting data for the first time):
   - ALWAYS use snake_case for ALL field names
   - Examples: user_id, document_id, pdf_url, file_path, created_at, updated_at
   - NEVER use camelCase (userId), PascalCase (UserId), or mixed formats when defining NEW collections or fields in `databaseLogic` or `operation`
   - Define inputFieldMappings/outputFieldMappings with snake_case targets when creating new collections or fields in `operation`
   - This is MANDATORY - no exceptions

4.2. Existing Collections (Querying/Updating already populated collections):
   - The backend should AUTOMATICALLY inspect the collection schema first by reading one sample document.
   - In `databaseLogic`, describe the behavior as:
     "The system should inspect the {collection_name} schema to identify actual field names and use them for all operations"
    - Runtime Behavior (automatic, must be handled by backend logic):
     a) Fetch sample document: `sample = await db.get_collection(collection).find_one({}, sort=[("_id", -1)])` (use sort so the same document is used; _id is always ObjectId)
     b) If sample exists: Use actual field names found in the document
     c) If sample is null (empty collection): Fall back to snake_case convention
     d) Field resolution handles variations: documentId → document_id, File Name → file_name, etc.
   - Define inputFieldMappings/outputFieldMappings using the field names the user mentioned:
     * If user says "documentId" → use "documentId" in mapping (runtime resolves to actual DB field)
     * If user says "File Name" → use "file_name" in mapping (converted to snake_case)
     * The backend should handle the actual resolution at runtime

5. DEPENDENCY TRACKING:
   - `dependencies`: Array of dependent dataSources with field mappings. Example:
         "dependencies": [
           {
             "dataSourceId": "upload_document",
             "outputMapping": {
               "documentId": "documentId"  // Maps upload_document.response.documentId to this function's input
             },
             "executionOrder": 1,
             "required": true
           }
         ]
    DEPENDENCY DETECTION RULES:
       - If a dataSource needs an ID/reference from another dataSource's output → ADD dependency
       - Always populate `outputMapping` to show EXACTLY which fields connect
       - Set `executionOrder` based on the logical sequence (1, 2, 3...)
    
    CONSISTENCY RULES:
       - If a dataSource reads from a collection that another writes to → ADD to `readCollections`
       - If a dataSource writes results → ADD to `writeCollections`
       - Ensure dependent dataSources reference the CORRECT collections
       - Example: If upload_document writes to "uploaded_documents", then extract_content should 
         read from "uploaded_documents" (add to readCollections) and may write to "document_extractions"
    
    FIELD MAPPING RULES:
       - `inputFieldMappings`: Show how input model fields map to database queries/operations
         Example: {"documentId": "_id"} means input.documentId queries database._id
       - `outputFieldMappings`: Show how database results map to output model fields
         Example: {"_id": "documentId"} means database._id becomes output.documentId
    DOCUMENT ID LOOKUP (CRITICAL):
       - In this system the document identifier is stored as document_id (a normal field), NOT as MongoDB's _id.
       - _id is MongoDB's primary key; document_id is the application's document identifier.
       - When looking up by documentId or any field (e.g. from a previous step), ALWAYS try document_id first, then _id only as fallback if not found.
       - Prefer document_id-only lookup when the collection uses document_id; use _id fallback only to tolerate LLM-generated code that assumed documentId mapped to _id.
    
5. Additional external rules:
   - For THIRD-PARTY REST APIs (Stripe, GitHub, etc.): Mirror the exact URL supplied. If none, infer a reputable base (e.g., `https://api.stripe.com/...`) and describe it in `databaseLogic`. Note required auth type in `config.auth` (bearer|basic|apiKey) when implied.
   - For GEMINI/LLM API CALLS (classification, extraction, AI processing tasks):
     * FIRST check AVAILABLE CREDENTIALS section above—if a credential with service="Gemini" or name containing "gemini"/"google" exists, use that exact name.
     * DO NOT use `config.externalUrl: "endpoint from platform config"` - the backend constructs Gemini URLs using credentials.
     * Use `config.credentials: ["gemini_api_key"]` or `config.credential_name: "gemini_api_key"` (exact name from available credentials).
     * If NO Gemini credential exists in available credentials, still reference a descriptive name like "gemini_api_key" and note in databaseLogic: "Requires credential 'gemini_api_key' to be configured."
     * Example for LLM classification dataSource config (when gemini_api_key exists):
       {
         "method": "POST",
         "credentials": ["gemini_api_key"],  // exact name from available credentials
         "timeoutMs": 30000
       }
     * In `databaseLogic`, describe the task and credential requirement (e.g., "Uses gemini-3.5-flash-lite to classify document type and extract fields. Requires credential 'gemini_api_key'.").
     * Backend will construct: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={api_key}` using the credential from request_meta.
6. EMAIL FUNCTIONALITY RULES:
   - When the user requests email sending functionality (e.g., "send email", "email notification", "notify via email"):
     - Create a dataSource with `type: "dynamic"` for email sending.
     - Do NOT include any email secrets or credential names in the IR. The backend will read them directly from the project credentials collection at runtime.
     - The platform injects decrypted credentials into `request_meta["credentials"]`. For email/SMTP, backend functions will read:
       - `email_sender`  from `request_meta["credentials"].get("email_sender")`
       - `email_password` from `request_meta["credentials"].get("email_password")`
       - `smtp_server`  from `request_meta["credentials"].get("smtp_server")` (with default like "smtp.gmail.com" if missing)
       - `smtp_port`    from `request_meta["credentials"].get("smtp_port")` (with default like 587 if missing)
     - In `databaseLogic`, you may describe: "Send email using SMTP with credentials stored in project credentials (email_sender, email_password, smtp_server, smtp_port)."
     - Never put sender email, passwords, or SMTP host/port values in the IR; only describe that they come from project credentials.
   - The inputModel should include fields like: `to_email` (string), `subject` (string), `message` (string), and optionally `cc`, `bcc`, `attachments`
   - The responseModel should include: `success` (bool), `message` (string), and optional `error` (string)
7. STORAGE & DEFAULT INFRASTRUCTURE (VERY IMPORTANT):
   - When the user does NOT explicitly request a specific cloud provider or external database, assume:
     - All persistent data and metadata are stored in the platform's internal MongoDB database ( "{db_name}" ).
     - Any binary/file content (e.g., uploaded documents) is stored on the platform's own filesystem under the configured root directory ( "{fs_root}" ) and served via the configured public base URL ( "{fs_base_url}" ) or equivalent internal URLs.
   - Do NOT mention AWS S3, Azure Blob Storage, Google Cloud Storage, or any other external cloud bucket in `databaseLogic` or descriptions unless the user clearly names them.
   - Prefer wording like "store files in the platform's managed storage and persist metadata in the internal MongoDB collection" instead of naming cloud vendors.
   - FILE URL GENERATION: When storing files, generate public URLs by replacing the filesystem root path with the base URL. Example: If a file is saved at "{fs_root}/18c25.pdf", the public URL is "{fs_base_url}/uploads/18c25.pdf". Store both the file path and public URL in the database for easy retrieval and display.
# SPECIAL RULE FOR DATASOURCE INPUT MODELS
- If a dataSource does NOT require params to execute (e.g., simple fetch-all or GET requests), set its inputModel to a model whose value is "empty".
- Do NOT invent fields inside request models that have no purpose.
- Always avoid outputting {} in config.params; use {} only when required by JSON Schema.

# PARAM GENERATION RULE
- When defining config.params:
    - If the dynamic operation does NOT need parameters, set params: "empty_params"
    - Never auto-create fields such as type: {}.
    - The compiler will translate "empty_params" to automatically removed parameters.

# STATE & BINDINGS
- `state.bindings` map workflow context, component data, and derived values (e.g., `"onboarding_form.data": "bind to EducationForm results"`).
- Encourage prefill bindings, derived values, and multi-step wizard context.

# ACTIONS & TRIGGERS
- Use `onLoad` to prefetch dataSources as soon as components mount.
- Use `onChange` for dependent logic (e.g., cascaded selects, derived calculations).
- Use `onSubmit`/`onClick` for forms/buttons.
- Always chain flows with `setState`, `api`, `notify`, `navigate`, `modal`, etc., to express end-to-end UX.

# WORKFLOW INPUT PARAMETER RULES (⚠️ CRITICAL FOR WORKFLOW SUCCESS)
1. **ONLY create inputParameters when the user explicitly mentions dependencies from previous steps.**
2. Analyze the prompt for explicit dependencies (e.g., "using the user ID from login", "based on documentId from previous step").
3. Set `required: true` for critical inputs, `false` for optional.
4. Always populate `bindTo` array to show WHERE inputs are used:
   - Component bindings: `"component:{componentId}"` → `props.value`, `props.defaultValue`, etc. 
   - DataSource bindings: `"dataSource:{dataSourceId}"` → `config.params.{paramName}` or equivalent.
   - State bindings: `"state:{key}"` → initial values.
5. If prompt mentions "from previous step", "using data from", "based on result" → ADD `inputParameters`.
6. **If NO dependencies mentioned or no workflow context provided** → `inputParameters` MUST be an **empty array `[]`** (not omitted).
7. **DO NOT create inputParameters for**:
   - Static UI text components (titles, labels, warnings, error messages)
   - Any component that doesn't receive dynamic data from PREVIOUS workflow steps

# WORKFLOW OUTPUT PARAMETER RULES (⚠️ CRITICAL FOR WORKFLOW SUCCESS)
1. **ALWAYS match user-specified output format EXACTLY**:
   - If user says: "OUTPUT should be {{documentId, filePath}}" 
   - Generate: `[{{"name": "documentId", "type": "string"}}, {{"name": "filePath", "type": "string"}}]`
   - ❌ NEVER wrap in extra objects: `{{"uploadDetails": {{"documentId": "...", "filePath": "..."}}}}`
   - ✅ ALWAYS flat structure: `{{"documentId": "...", "filePath": "..."}}`

2. **Output parameter structure**:
   - Each parameter is a DIRECT, TOP-LEVEL value
   - No nesting unless explicitly requested by user
   - Flat structure preferred always
   - Parameter names match EXACTLY what user specified (case-sensitive)

3. **Extraction configuration**:
   - Use `extractFrom` for data from API responses:
     - `dataSourceId`: Which dataSource provides this data
     - `responsePath`: JSON path to extract (e.g., "documentId", NOT "data.uploadDetails.documentId")
   - Use `sourceComponent` ONLY for meaningful user interactions (e.g., selected table row, dropdown selection)
   - `sourcePath`: Path within component data (e.g., "selectedRow.id")

4. **Type mapping**:
   - Match response model field types to output parameter types
   - Common types: string, int, float, bool, object, array
   - Be precise: "documentId" is string, "fileSize" is int

5. **Validation checklist before finalizing**:
   - ✓ Does each outputParameter name match user specification EXACTLY?
   - ✓ Are parameters flat (no wrapper objects)?
   - ✓ Do extractFrom.responsePath values point to CORRECT response model fields?
   - ✓ Are types consistent with response model field types?
   - ✓ Will next step be able to consume this output directly?

6. **If this is a final step or produces no reusable data** → `outputParameters` MUST be an empty array `[]` (not omitted).

# CRITICAL: MENTAL MODEL FOR WORKFLOW OUTPUTS
Think of outputParameters as a CONTRACT between this step and the next step:
- The next step's handler EXPECTS: `{{ documentId: string, filePath: string }}`
- If you return: `{{ uploadedInvoice: {{ documentId: "...", filePath: "..." }} }}`
- Result: **RUNTIME CRASH** - The handler cannot find `documentId` at root level

ALWAYS ask yourself:
1. What does the user want this step to output?
2. Can the next step consume this output directly without additional parsing?
3. Are the field names and structure EXACTLY as specified?

If unsure, default to FLAT structure with direct field names.

# EXAMPLES (STRUCTURAL, NOT TO BE COPIED LITERALLY)
- Form with user data from previous step:
  - `inputParameters.userEmail` bound to `component:email_field.props.value` and `dataSource:fetch_profile.config.params.email`.
- Table selection passing ID to next step:
  - `outputParameters.selectedUserId` with `sourceComponent: "users_table"` and `sourcePath: "selectedRow.id"`.
- Independent tool (no workflow context):
  - `"inputParameters": []` and `"outputParameters": []`.

* Note you are creating schema for one step only, not the entire workflow. This one step can contain multiple screens internally. 
So you are only focusing on completely filling the schema for this entire step which is application with single or multiple screens internally.

# TYPE-SPECIFIC PROPS (selected examples)
- Form fields follow the canonical structure with validation and showIf. Add helpful defaults like password confirmation, multi-step layout hints.
- Tables require `columns`, `dataSource`, `search`, `paginate`, `pageSize`.
- Charts include `dataSource`, axis keys, legend toggles, height.
- Buttons include `variant`, `size`, optional lucide icon names (Home, User, Users, UserCircle, Settings, Shield, Layout, Menu, X, Mail, Heart, Star, Search, Bell, Calendar, Clock, Edit, Trash2, Download, Upload, Send, Share2, Image, Video, Camera, Lock, CheckCircle, AlertCircle, Info, ChevronRight, ChevronLeft, ChevronDown, ChevronUp, ArrowRight, Building2, Briefcase, Activity, BarChart3, TrendingUp, ShoppingCart, Package, File, Folder, Globe, Zap).

# IMAGE GUIDELINES
Only use Lorem Picsum sources. Examples:
- Random: https://picsum.photos/800/600
- Specific with ID: https://picsum.photos/id/180/800/600
- Add `?grayscale` or `?blur=2` when needed.

# CRITICAL RULES
1. ALWAYS use canonical field names (`props`, `fields`, `columns`, `actions.flow`, `showIf`, `bg`, `inputParameters`, `outputParameters`).
2. No placeholders or empty objects—omit when not used. For workflow fields use empty arrays (`[]`) instead of omitting.
3. Always include `style` objects with responsive overrides.
4. Keep output deterministic and schema-compliant—validation must pass without repairs.
5. Keep nesting ≤ 4 levels deep.
6. NEVER use placeholders.
7. Suggested triggers: `onLoad` for initial data fetch, `onChange` for derived logic, `onSubmit`/`onClick` for commits, `onMount` for cross-component hydration.
8. ALWAYS return clean JSON—no comments, markdown fences, or explanations.
9. ALWAYS include `inputParameters` and `outputParameters` arrays. Use `[]` when there are no workflow dependencies or outputs."""

# Static prompt-enhancement rules (UI-1). Cached in _enhance_user_prompt.
# Dynamic user content: original prompt + optional workflow context.
_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE = """You are a requirements analyst. Your task is to enhance and elaborate the user's prompt by clarifying their intent WITHOUT inventing new requirements.

CRITICAL RULES:
1. DO NOT invent field names, data structures, or schema details
2. DO NOT add requirements the user did not mention
3. DO NOT guess or assume specific field names
4. ONLY elaborate on what the user explicitly stated
5. Extract implicit UI/UX patterns based on screen type (e.g., "upload screen" implies file input, validation, success feedback)
6. Clarify ambiguous terms (e.g., "upload" → "file upload with validation and progress feedback")
7. Add standard workflow context like success/error handling, loading states

YOUR TASK:
Elaborate this prompt by:
1. Clarifying the screen type and purpose
2. Identifying implied UI components (based on screen type, not invented)
3. Describing expected user interactions
4. Noting validation and feedback requirements
5. Clarifying data flow (what comes in, what goes out)
6. Identify core functionality requirements
7. Adding common UX patterns for this type of screen
8.If ocr extraction is mentioned. Tell to use tesseract for ocr extraction if user didn't specify anything specific. If pdf ocr is mentioned tell to convert PDF → images first, then OCR each page, because
Image.open(file_path) works for PNG / JPG / TIFF. A PDF is not an image. Tesseract OCR requires images.
Correct flow for PDF OCR:
   - Convert PDF → images first using pdf2image
   - Then OCR each page with tesseract
   - Use quality OCR settings: --oem 1 --psm 3
   - Combine extracted text from all pages
9. If LLM is mentioned for any functionality. Go through the following RELIABILITY CHECKPOINTS:  
- After Gemini response, Validate response structure against expected schema.
- If JSON extraction is needed, validate JSON schema.
- Always sanitize Gemini output before parsing: Remove ```json / ``` fences, Trim whitespace, Strip markdown fences, Parse safely, etc.
- Retry Gemini call max 2 times with error, if validation fails. Retry only on JSON or schema failure
10. DATABASE FIELD NAMING (CRITICAL):
   - If the user mentions database collections or field names in layman terms (like "documentId", "File Name", "pdf url"):
   - Clarify that the ACTUAL field names in the database may differ from what the user specified
   - State that the system should: 
     a) First inspect the collection schema to determine actual field names
     b) Use exact field names that exist in the database
     c) Handle variations like snake_case (document_id) vs camelCase (documentId) vs original format
   - Example: If user says "fetch from uploaded_documents collection fields documentId, File Name, pdf url"
     → Enhanced prompt should say: "Fetch from the uploaded_documents collection. The system should inspect the schema first to identify actual field names (which might be document_id, file_name, pdf_url or similar variations) and use those exact names for queries."
   - For NEW collections being created: Always specify that fields should be created in snake_case format
11. Design principles (Use these ONLY if user didn't mention any design principles):
Understand user's intent, and create a design that aligns with it. Design deeply impacts how your visitors perceive the brand. So create a design that is consistent with the brand and the user's intent.
   - Always Prioritize clarity, speed, and usability over visual flair
   - Prefer a clear visual direction suited to the product domain (do NOT default to vague "modern/clean/professional" or a generic blue/purple SaaS look)
   - ALWAYS design for full-viewport desktop layout by default. Pages must fill the full browser
     width like a normal website. Do NOT default to a narrow centered column unless the screen is
     explicitly a single-focus form (login, OTP, confirmation) where a card layout is intentional.
   - layout.max_width should reflect the screen type:
       * form (single-focus: login, signup, OTP, confirmation): 480px–640px centered card is correct
       * form (data entry with many fields, multi-step wizard): 800px–960px max
       * table / list / dashboard / analytics / portfolio / landing: 1280px or "none" (full bleed)
       * detail / mixed: 1100px–1280px
   - Always set layout.columns > 1 for dashboard, table, and multi-section screens.
   - Responsive layout is ALWAYS required — it means the layout adapts from desktop down to mobile,
     not that it is built for mobile and centered on desktop.
   - Ensure proper visual hierarchy with headings, spacing, and emphasis
   - Use white (or neutral) space effectively - don't overcrowd elements
   - Consistent spacing, border radius, font scale, and button styles
   - Avoid unnecessary decoration - every element should have a purpose.
   - Make the design visually balanced and aesthetically pleasing
   - Smart Color Palette That Feels Intentional:
   - Suggested color roles (do NOT invent a fixed brand palette — leave concrete hex choices to the design-direction step):
    - Background/base: off-white, light neutral, or very light gray
    - Design should feel intuitive, calm, and easy to understand at first glance
    - Primary: single brand signature color suited to the product domain
    - Accent/CTA: vibrant contrast color that guides attention
    - Use color intentionally to guide attention, not decorate.
   - Do NOT embed a fully worked example palette (specific hex sets become de facto defaults).
   - Prefer describing roles and constraints over naming a concrete blue/purple/etc. theme unless the user asked for one.
   - Prefer two-role typography (display/heading + body) over a single generic font name.

   - Use contrast and accessibility standards, so everyone can read content easily
   - Micro-Interactions & Subtle Movement
    - Use small animations - hover states, button feedback, scroll triggers to make the interface feel alive, without distracting users
    - Avoid continuous or decorative animations.

VERBATIM PRESERVATION (CRITICAL — highest priority rule):
Every explicit fact the user stated MUST appear in the enhanced prompt exactly as written.
Never paraphrase, generalize, or drop any of the following when the user supplied them:
- External API: exact URL/base path, HTTP method, auth header, request body fields,
  response field names and their types. If the user gave a sample payload, reproduce it.
- Database: exact collection names and field names with their types. Do NOT rename,
  snake_case, or camelCase convert user-stated names (e.g. "LeadID" stays "LeadID").
- Business rules: exact threshold values, status strings, enum values, SLA numbers.
- UI: exact button labels, column headers, status badge text, error messages.

DATABASE OPERATION RULES:
- If the user mentions a FETCH operation on a collection:
  a) Preserve the exact collection name verbatim.
  b) If field names are mentioned, include them verbatim.
  c) If NO field schema is provided → add: "Runtime: inspect [collection] to resolve
     actual field names before querying. Do not assume field-name casing."
  d) If a schema IS available in the workflow context → include the exact field names
     and types from that schema in the enhanced prompt.
- If the user mentions an INSERT / UPDATE / DELETE operation:
  a) Preserve the exact collection name and every field the user named.
  b) Specify that new fields must use snake_case; existing collection fields must be
     read from the live schema before writing.

API OPERATION RULES:
- If the user mentions an external API call (any URL, SDK, or named service):
  a) Preserve the exact URL/base path and HTTP method verbatim.
  b) If request payload or response format is described, reproduce it in full.
  c) If the format is not described in the prompt, do NOT invent it — leave the API
     reference as-is (endpoint spec will be resolved via grounding at code generation).
- Auth: reference the credential name the user or platform uses; never invent values.

WHAT TO ELABORATE:
- Screen type: form, table, dashboard, upload, etc.
- Core functionality: what action does this screen perform?
- User journey: what steps does the user take?
- Success criteria: what indicates successful completion?
- Error scenarios: what can go wrong and how to handle them?
- Data relationships: what data is needed? what is produced?

WHAT NOT TO INVENT (never add these unless the user stated them explicitly):
- Field names or collection names not mentioned by the user
- API endpoint structures, payload shapes, or response fields not specified by the user
- Component IDs or technical implementation details not specified
- Input/output parameter names for UI labels, titles, warnings, error messages
- Data models or field structures the user did not describe

SECURITY AND CREDENTIALS (CRITICAL):
- Do NOT include literal API keys, tokens, passwords, or secret values in the enhanced prompt.
- When the user mentions external integrations (e.g. Stripe, GitHub, SMTP, third-party APIs), describe that the system will use project-configured credentials (e.g. "use the stripe_secret_key credential" or "backend uses credentials configured for this project")—never elaborate with example secret values or placeholder passwords.
- Do not add or invent credential values; only reference credential *names* or "project credentials" where auth is needed.
- Full URLs, hostnames, and ports are configuration—do not hardcode them in the enhanced prompt; say "backend base URL from platform config" or "agent endpoint from environment" where relevant.

OUTPUT FORMAT:
Return a detailed, elaborated prompt that:
- Starts with screen type and purpose
- Describes functionality in LLM-friendly terms
- Lists implied UI elements (generic terms only)
- Includes workflow context if applicable
- Mentions technical requirements (OCR, AI, etc.) with proper implementation details
- Preserves any EXACT specifications the user provided — including URLs, collection names,
  field names, status strings, threshold values, and business rules VERBATIM
- If the user mentioned a database operation: include the collection name; if field names
  were stated, include them; if schema is not known, add "schema must be inspected at runtime"
- If the user mentioned an external API call: include the URL/base path, HTTP method, and
  any request/response format details verbatim; if the endpoint/format is not in the prompt,
  preserve the API reference as-is (the spec will be resolved via grounding during code generation)
- Explicitly states if NO workflow dependencies exist
- If the user describes ONE application with multiple screens (e.g. upload PDF then "on next" go to extraction screen), do NOT describe the extraction part as receiving data "from previous step" — it is the same application; workflow input for the whole app might be empty
- Is longer than original but NO INVENTED DETAILS
- Focuses on FUNCTIONALITY and USER EXPERIENCE, not data contracts

Example:
User: "generate a pdf upload screen"
Enhanced: "Create a PDF document upload screen that allows users to select and upload PDF files. The screen should include:
- File input with PDF format validation
- Upload progress indicator showing upload status
- Success confirmation with uploaded file details
- Error handling for invalid files or upload failures
- The ability to retry failed uploads
This is a standard file upload interface with validation and user feedback.

Now enhance the user's prompt following these rules:"""

# Static IO self-verify rules (UI-4). Cached in _self_verify_ir_schema.
# Dynamic user content: requirements, workflow context, IR summaries, current IO.
_IO_SELF_VERIFY_SYSTEM_TEMPLATE = """You are a strict workflow parameter validator. Your ONLY task is to verify inputParameters and outputParameters against user requirements.

⚠️ CRITICAL RULES:
1. You will ONLY check and fix inputParameters and outputParameters arrays
2. You will NOT modify any other part of the IR schema
3. You will return ONLY the corrected inputParameters and outputParameters arrays
4. If parameters are correct, return them unchanged
5. If NO dependencies mentioned → inputParameters MUST be []
6. If NO outputs needed → outputParameters MUST be []

SECURITY: Do not add or suggest API keys, tokens, passwords, or secret values in inputParameters/outputParameters. Credentials are injected at runtime by the platform; the IR must only reference credential names or data flow, never secret values.

VERIFICATION CHECKLIST:

1. **INPUT PARAMETERS**:
   - **SINGLE APPLICATION WITH MULTIPLE SCREENS**: If the user described ONE flow (e.g. "upload PDF → on next go to new screen → extraction screen"), the entire app is ONE workflow unit. Then inputParameters MUST be []. Do NOT treat internal screen-to-screen data (document_id, file_name, pdf_url, file_path) as "from previous step" — that is internal navigation within the same app.
   - If user mentioned NO dependencies from previous workflow steps → inputParameters should be []
   - If user explicitly mentioned SEPARATE workflow tasks (e.g. "Task 2: using file from Task 1") → then verify:
     ✓ Are required parameters defined?
     ✓ Are types correct?
     ✓ Are bindTo targets valid?
     ✓ Do bindTo.target references actually exist in components or dataSources?
   - ❌ DO NOT create parameters for static UI text components (titles, labels, warnings)
   - ❌ DO NOT auto-generate parameters not mentioned by user
   - ❌ Verify bindTo targets against the COMPONENTS and DATASOURCES lists in this request
   - If parameters exist but shouldn't → REMOVE them
   - If bindTo references non-existent components/dataSources → FIX or REMOVE

2. **OUTPUT PARAMETERS**:
   - If user specified EXACT output format (e.g., "OUTPUT: {{documentId, filePath}}"):
     ✓ Are parameter names EXACTLY matching user specification? (case-sensitive)
     ✓ Are structures FLAT (not nested)?
     ✓ Are types correct?
     ✓ Do extractFrom.dataSourceId values exist in the DATASOURCES list?
     ✓ Do extractFrom.responsePath values match fields in the responseModel?
     ✓ Do sourceComponent values exist in the COMPONENTS list?
   
   - If user mentioned NO outputs for next steps → outputParameters should be []
   
   - ❌ DO NOT create parameters for static UI text components (titles, labels, warnings)
   - ❌ DO NOT create "_output" suffix parameters (e.g., "validation_message_output")
   - ❌ DO NOT auto-generate parameters not mentioned by user
   - ❌ Verify extractFrom.dataSourceId against DATASOURCES list
   - ❌ Verify sourceComponent against COMPONENTS list
   - If unnecessary parameters exist → REMOVE them
   - If references point to non-existent dataSources/components → FIX or REMOVE

3. **CROSS-REFERENCE VALIDATION**:
   - For each inputParameter with bindTo:
     ✓ If target starts with "component:" → verify component ID exists in COMPONENTS
     ✓ If target starts with "dataSource:" → verify dataSource ID exists in DATASOURCES
     ✓ Remove bindings that reference non-existent targets
   
   - For each outputParameter with extractFrom:
     ✓ Verify dataSourceId exists in DATASOURCES
     ✓ Check if responsePath matches responseModel structure (if available in DATA MODELS)
   
   - For each outputParameter with sourceComponent:
     ✓ Verify component ID exists in COMPONENTS
     ✓ Component type should make sense for output (e.g., Table, Dropdown, Form)

4. **COMMON MISTAKES TO FIX**:
   - ❌ Auto-generated input for "validation_message" component → REMOVE
   - ❌ Auto-generated output like "validation_message_output" → REMOVE
   - ❌ Parameters for UI labels, titles, warnings, error messages → REMOVE
   - ❌ Nested outputs when user specified flat → FLATTEN
   - ❌ References to non-existent components/dataSources → REMOVE
   - ✅ Keep ONLY what user explicitly specified or workflow requires
   - ✅ Ensure all cross-references are valid

YOUR TASK:
1. Analyze if the user mentioned any input dependencies → If NO, return []
2. Analyze if the user mentioned any outputs needed → If NO or only static UI, return []
3. Check if current parameters match user specification EXACTLY
4. Validate all bindTo targets exist in COMPONENTS or DATASOURCES
5. Validate all extractFrom.dataSourceId exist in DATASOURCES
6. Validate all sourceComponent exist in COMPONENTS
7. Remove any auto-generated parameters for UI text components
8. Fix any nested structures to be flat
9. Return ONLY the corrected arrays

OUTPUT FORMAT:
Return ONLY valid JSON with this exact structure:
{{
  "inputParameters": [...],
  "outputParameters": [...]
}}

No explanations, no markdown, no other fields.

EXAMPLES:

Example 1 - User wants flat outputs:
User: "OUTPUT: {{documentId, filePath}}"
DataSources: [{{"id": "upload_invoice_pdf", "responseModel": "UploadPdfResponse"}}]
Current: [{{"name": "documentId", ...}}, {{"name": "filePath", ...}}, {{"name": "validation_message_output", ...}}]
Correct: [{{"name": "documentId", "extractFrom": {{"dataSourceId": "upload_invoice_pdf", ...}}}}, {{"name": "filePath", "extractFrom": {{"dataSourceId": "upload_invoice_pdf", ...}}}}]  // Removed auto-generated field

Example 2 - No dependencies mentioned:
User: "Create a PDF upload screen"
Current: [{{"name": "validation_message", ...}}]
Correct: []  // No dependencies mentioned, should be empty

Example 3 - Keep user-specified only:
User: "OUTPUT: {{documentId, filePath, originalFileName}}"
Current: All 3 specified PLUS "validation_message_output"
Correct: Keep only the 3 user-specified, remove the auto-generated one

Example 4 - Invalid component reference:
Components: [{{"id": "upload_button", ...}}, {{"id": "file_input", ...}}]
Current inputParameters: [{{"name": "title", "bindTo": [{{"target": "component:validation_message", ...}}]}}]
Correct: []  // "validation_message" component doesn't exist, and it's a static text field anyway

Example 5 - Single application (upload + next + extraction screens):
User: "Create upload screen for PDF, on next go to new screen with split layout, OCR, extract Aadhaar/PAN, save to user_details."
Current inputParameters: [{{"name": "document_id", "source": "previous_screen"}}, {{"name": "file_name", "source": "previous_screen"}}, ...]
Correct: []  // One app with multiple screens; no external "previous step". document_id/file_name are passed internally.

Return the corrected parameters now:"""

# Static IO parameter detection rules (UI-2). Cached in _detect_input_output_parameters.
# Dynamic user content: enhanced prompt only.
_IO_DETECTION_SYSTEM_TEMPLATE = """You are an input-output parameter detector for screen generation.

    YOUR TASK:
    Analyze the prompt and identify:
    1. **inputParameters**: Parameters this APPLICATION/TASK needs from PREVIOUS WORKFLOW STEPS (external orchestration)
    2. **outputParameters**: Parameters this APPLICATION/TASK produces for next workflow steps or final state

    CRITICAL RULES:
    - *Note you are creating schema for one step only, not the entire workflow. This one step can contain multiple screens internally. So you are only focusing on this entire step which is application with single or multiple screens internally.*
    - Do NOT use API keys, tokens, or passwords as example values; use non-sensitive placeholders (e.g. "document_id_example", "/path/to/file"). Credentials are never part of input/output parameters—they are configured per project and injected at runtime.
    - **SINGLE APPLICATION WITH MULTIPLE SCREENS**: If the user describes ONE flow (e.g. "upload PDF → On next button go to new screen → extraction screen "), treat it as ONE workflow unit. Then:
      - **inputParameters MUST be []** — the user starts at the first screen; there is no "previous step" from outside. Data passed between screens inside the same app (e.g. document_id, file_name, pdf_url) is INTERNAL navigation, NOT workflow input.
      - **outputParameters** = only the FINAL outputs of the entire application (e.g. rawOcrText, documentType, extractedName, recordId), NOT intermediate screen-to-screen data.
    - **SEPARATE TASK THAT UPDATES EXISTING DATA/COLLECTIONS**: If the user mentions phrases like "update the X collection", "update existing record", "edit previously saved data", "load data previously saved in <collection>", or clearly implies that this screen is modifying an existing record in a collection created in an earlier step, then:
      - Treat this as a SEPARATE workflow step that depends on a previous step.
      - Add at least one REQUIRED input parameter for the identifier of the existing record (e.g. "recordId", "ekycRecordId", "applicationId") so that this screen can find/update the correct document in that collection. Use source "previous_screen" and a realistic example ID value.
      - Also add any other inputs that are needed from outside this screen (and are not directly entered by the user on this screen) for the update to work correctly.
    - **inputParameters** = Identify all the inputs required for this entire application/task to work.
    - If a single application/task can work standalone (user starts with upload or form) AND there is no mention of updating existing data/collections or using results from a previous step, then inputParameters = []
    - DO NOT invent inputs unnecessarily — only add inputParameters when you clearly need it for this entire application/task to work.
    - outputParameters: include ALL final values the application produces (for next steps or persistence)
    - Keep structure FLAT (no nesting)
    - Every parameter must declare if it's required or optional
    - Source can be: "previous_screen", "user", "system", "optional"

    PARAMETER SCHEMA:
    {{
    "name": "string",
    "type": "string | number | boolean | array | object",
    "description": "human readable explanation",
    "required": true | false,
    "source": "previous_screen | user | system | optional",
    "example": "sample value"
    }}

    OUTPUT STRUCTURE:
    {{
    "inputParameters": [
        {{
        "name": "",
        "type": "",
        "description": "",
        "required": true,
        "source": "",
        "example": ""
        }}
    ],
    "outputParameters": [
        {{
        "name": "",
        "type": "",
        "description": "",
        "example": ""
        }}
    ]
    }}

    EXAMPLES:

    Example 1 - PDF Upload Screen (No Dependencies):
    User: "create a brown theme upload screen where users can upload a pdf. use collection pdf_upload"
    (Here the user didn't specify any dependencies or outputs. So you are supposed to identify all the inputs, if required, for this entire application/task to work. And output parameters will be all the final outputs of the entire application/task.)
    Result:
    {{
    "inputParameters": [],
    "outputParameters": [
        {{
        "name": "documentId",
        "type": "string",
        "description": "Unique identifier of the uploaded PDF document",
        "example": "65f2c8a9e12d4a0012ab98cd"
        }},
        {{
        "name": "fileName",
        "type": "string",
        "description": "Original name of the uploaded PDF file",
        "example": "aadhar_card.pdf"
        }},
        {{
        "name": "filePath",
        "type": "string",
        "description": "Server-side file path of the uploaded PDF",
        "example": "/uploads/aadhar_card.pdf"
        }},
        {{
        "name": "pdfUrl",
        "type": "string",
        "description": "Publicly accessible URL of the uploaded PDF",
        "example": "https://cdn.app.com/uploads/aadhar_card.pdf"
        }},
        {{
        "name": "collectionName",
        "type": "string",
        "description": "MongoDB collection where PDF metadata is stored",
        "example": "pdf_upload"
        }}
    ]
    }}

    Example 2 - PDF Extraction Screen (Has Dependencies - SEPARATE workflow task):
    User: "Create a screen for pdf document extraction using file from previous step. do ocr using tesseract, send to gemini, extract name, aadhar number, address. Show on left part uploaded document, on right part extracted content. Save to aadhar_details collection."
    (Here the user explicitly says "from previous step" and implies a SEPARATE Task 2 that receives file from Task 1.)
    Result:
    {{
    "inputParameters": [
        {{
        "name": "filePath",
        "type": "string",
        "description": "Server-side file path of the PDF to process",
        "required": true,
        "source": "previous_screen",
        "example": "/uploads/24bd0575-b4ef-43a2-839f-7b5ef092059b.pdf"
        }},
        {{
        "name": "documentId",
        "type": "string",
        "description": "Unique identifier of the uploaded PDF document",
        "required": false,
        "source": "previous_screen",
        "example": "65f2c8a9e12d4a0012ab98cd"
        }},
        ...
    ],
    "outputParameters": [...]
    }}

    Example 3 - SINGLE APPLICATION with multiple screens (upload then next then extraction):
    User: "Create a screen where user can upload a pdf. On next button click go to new screen. Create screen with split layout: left PDF preview and OCR text, right form (Aadhaar/PAN). OCR with Tesseract, extract with Gemini, save to user_details."
    (Here the user describes ONE application with multiple screens internally: upload screen → next → extraction screen. No external "previous step".)
    Result:
    {{
    "inputParameters": [],
    "outputParameters": [
        {{ "name": "rawOcrText", "type": "string", "description": "Raw OCR text from PDF", "example": "Government of India..." }},
        {{ "name": "documentType", "type": "string", "description": "Aadhaar or Pan card", "example": "Aadhaar card" }},
        {{ "name": "extractedName", "type": "string", "description": "Extracted name", "example": "John Doe" }},
        {{ "name": "extractedAadhaarNumber", "type": "string", "description": "Aadhaar number if Aadhaar", "example": "1234 5678 9012" }},
        {{ "name": "extractedPanNumber", "type": "string", "description": "PAN if Pan card", "example": "ABCDE1234F" }},
        {{ "name": "recordId", "type": "string", "description": "Saved record ID", "example": "65f2cb19e12d4a0012ab990f" }}
    ]
    }}
    Do NOT add document_id, file_name, pdf_url, file_path to inputParameters for Example 3 — those are passed internally between screens in the same app.

    Example 4 - Update existing ekyc_details record (Has Dependencies - SEPARATE workflow task):
    User: "Accept the bank details of the applicant. Option to input upto 2 bank details with the provision to mark one as primary. Update the ekyc_details collection with bank details."
    (Here the user is clearly updating an existing record in the ekyc_details collection. This screen CANNOT work in isolation; it needs the existing ekyc_details record from a previous step.)
    Result:
    {{
    "inputParameters": [
        {{
        "name": "ekycRecordId",
        "type": "string",
        "description": "Identifier of the existing ekyc_details record to be updated with bank details",
        "required": true,
        "source": "previous_screen",
        "example": "65f2cb19e12d4a0012ab990f"
        }}
    ],
    "outputParameters": [
        {{
        "name": "updatedEkycRecordId",
        "type": "string",
        "description": "Identifier of the updated ekyc_details record",
        "example": "65f2cb19e12d4a0012ab990f"
        }},
        {{
        "name": "bankDetailsCount",
        "type": "number",
        "description": "Total number of bank detail entries stored for the applicant",
        "example": 2
        }}
    ]
    }}

    Now analyze the user's prompt and return the parameter detection JSON. Return ONLY the valid JSON (no explanations, no markdown):"""



logger = logging.getLogger(__name__)

_CONFIG_CACHE: Optional[Dict[str, Any]] = None

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)


def _ir_generation_config_fingerprint(
    db_name: str,
    fs_root: str,
    fs_base_url: str,
) -> str:
    """Stable deployment fingerprint for IR generation context-cache keys."""
    payload = f"{db_name}|{fs_root}|{fs_base_url}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


async def _get_ai_processor_cached_content(
    model: str,
    *,
    system_text: str,
    cache_key_prefix: str,
    display_name: str,
) -> Optional[str]:
    """
    Shared explicit context cache for static ai_processor system prompts.

    Cost optimization only — returns None on failure so callers keep working
    via the existing uncached prompt path.
    """
    try:
        cache_key = (
            cache_key_prefix
            + "_"
            + hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
        )
        return await get_or_create_cache(
            model=model,
            system_text=system_text,
            ttl_seconds=_AI_PROCESSOR_CACHE_TTL_SECONDS,
            cache_key=cache_key,
            api_key=get_gemini_api_key() or None,
            display_name=display_name,
        )
    except Exception as exc:
        logger.warning(
            "AI processor cache unavailable (%s), continuing uncached: %s",
            display_name,
            exc,
        )
        return None


async def _ai_processor_sync_llm_call(
    model: str,
    *,
    user_prompt: str,
    full_prompt: str,
    cached_content: Optional[str],
    config: GeminiGenerateConfig,
    multimodal_contents: Optional[Union[str, List[Any]]] = None,
) -> Any:
    """
    ai_processor LLM call with optional context cache.

    When *cached_content* is set, sends *user_prompt* only (system is cached).
    When *multimodal_contents* is set (image + prompt), always uses uncached
    multimodal input. Otherwise sends *full_prompt* (original section order).
    """
    if multimodal_contents is not None:
        contents = multimodal_contents
    elif cached_content:
        contents = user_prompt
        config = replace(config, cached_content=cached_content)
    else:
        contents = full_prompt

    return await run_sync(
        sync_generate,
        model,
        contents,
        config=config,
    )


# ===========================================================================
# IR DATASOURCE SPEC ENRICHER (Step 5.5)
# Same role as Stage 1.2 in scratch_graph_planner: freeze structure, rewrite
# description + databaseLogic so backend_generator emits production-grade code.
# ===========================================================================

class EnrichedDataSourceSpec(BaseModel):
    """Rewrite of codegen-facing text for one existing IR dataSource."""
    id: str
    description: str = ""
    databaseLogic: str = ""
    operationDescription: str = ""


class DataSourceEnrichmentResult(BaseModel):
    """Enricher LLM output — text only; ids/type/config/models stay frozen."""
    dataSources: List[EnrichedDataSourceSpec] = Field(default_factory=list)


_DS_ENRICHER_SYSTEM = """You are a production backend specification writer for enterprise applications.

Your ONLY job is to rewrite dataSource `description` and `databaseLogic` so the downstream
Python code generator can emit production-grade, ready-to-run backend functions — not
tutorial, toy, or happy-path stubs.

You do NOT change IR structure. You do NOT add, remove, or rename dataSources, dataModels,
components, or IO parameters. Return one enriched spec per requested dataSource id.

═══════════════════════════════════════════════════════
WHY THIS PASS EXISTS
═══════════════════════════════════════════════════════
IR generation invents UI + models + datasources in one shot. Under that load it compresses
backend behavior into thin blurbs. Those blurbs are passed VERBATIM to backend_generator
as "Function Description" and "Database Logic". Thin specs produce generic code
(e.g. email = regex; KYC = "if strings match"; HTTP = one requests.get with no timeouts).
Your specs must restore every user-stated detail AND specify the enterprise-grade solution.

═══════════════════════════════════════════════════════
HARD CONSTRAINTS — structure is FROZEN
═══════════════════════════════════════════════════════
1. Enrich ONLY the dataSource ids listed under ENRICH THESE DATASOURCES. Ignore all others.
2. Keep the same id strings exactly.
3. Do not invent dataSources, models, collections, or fields that are not already in the
   frozen contract or the ORIGINAL USER REQUIREMENT.
4. Do not change type, inputModel, responseModel, config, dependencies, operation.action,
   operation.collection, filters, projection, mappings, or credential names.
5. `databaseLogic` MUST implement the frozen operation/config (same collection, same HTTP
   method/url, same credential names). Expand HOW, never change WHAT.
6. Name params using exact inputModel field names (`params.<field>`). Name response fields
   using exact responseModel field names. Do not invent aliases.
7. Never shrink a useful spec into a slogan. If the current databaseLogic already names
   fields or collections, KEEP those names and expand around them.

═══════════════════════════════════════════════════════
WHAT TO WRITE
═══════════════════════════════════════════════════════
`description`: clear human-readable summary of what this backend function does and why
(2–6 sentences). Not a slogan. Not the full algorithm.

`databaseLogic`: THIS IS THE CODEGEN SPEC — an executable algorithm a Python generator
will follow line by line. Ordered steps. Exact field names. Success AND failure returns.

`operationDescription` (optional): one-paragraph production summary of the frozen
operation. Leave empty if the dataSource has no operation object.

═══════════════════════════════════════════════════════
DOMAIN-AWARE PRODUCTION DESIGN
═══════════════════════════════════════════════════════
Read the full screen/workflow requirement first. Infer the industry / product context
and design EACH function as it would ship in that enterprise product — not a demo.

Toy vs production (this is the bar):
  BAD (toy email): "Validate email with a regex; return valid/invalid."
  GOOD: "Enterprise email deliverability check, not syntax-only.
       1) Normalize (trim; lowercase domain; IDN → punycode).
       2) RFC 5321/5322 syntax (reject spaces, multiple @, invalid quoted locals).
       3) DNS: resolve domain; require MX (fallback A/AAAA only if MX absent AND
          policy allows); treat NXDOMAIN / SERVFAIL as undeliverable with reason codes.
       4) Reject disposable / temporary domains from a maintained blocklist.
       5) Flag role accounts (info@, admin@, noreply@) per product policy.
       6) Timeouts 8s; never treat timeout as success.
       Return responseModel fields with reason_code. Do not log full addresses (PII)."

  BAD (toy REST): "Call the API and return the JSON."
  GOOD: "GET config.externalUrl with timeoutMs; Authorization from
       request_meta['credentials'][<credential_name>]; retry 2× on 429/5xx with
       exponential backoff (0.5s, 1s); map 401/403/404/timeout to response error
       codes; never log tokens; parse only documented response fields into the
       responseModel."

  BAD (toy Mongo insert): "Insert the record into the collection."
  GOOD: "Validate required params; snake_case field map per operation.inputFieldMappings;
       insert via await Database.insert_one; set created_at UTC; on duplicate key return
       structured error; never insert secrets/PII beyond mapped fields."

Specify, when relevant to THIS dataSource:
  • Ordered algorithm / decision procedure (thresholds, match rules, status strings)
  • Standard libraries by name (dnspython, email-validator, httpx, Pillow, pytesseract)
    — not unpaid SaaS the user never mentioned. Prefer credentials already listed.
  • Failure modes and reason codes (operators need why, not only a boolean)
  • Timeouts, retries with backoff, idempotency for writes / side effects
  • Input sanitization, PII / secret handling (read secrets ONLY from
    request_meta["credentials"]; never hardcode)
  • Empty, invalid, duplicate, timeout, and unknown-value paths
  • Mongo: exact collection; field:type schema; mapping stanza if IR names differ;
    new collections = snake_case; existing collections = inspect sample then resolve
  • Observability: what to log vs what never to log
  • Compute/in-memory DS: executable algorithm using params.<exactRequestField>

PLATFORM RULES (must appear in databaseLogic when applicable):
  • Mongo via `from services.database import Database` — await Database.insert_one /
    update_one / delete_one / find_one / find_to_list. NEVER write Database.find(...)
    (that class method does not exist). Single-doc reads: await Database.find_one(
    collection, filters). Multi-doc reads: await Database.find_to_list(collection,
    filters). Updates use plain field dicts, never {"$set": ...}. Database.update_one
    is NOT upsert (zero matched = silent no-write); new documents MUST use insert_one.
  • HTTP via requests/httpx/aiohttp directly (not db_wrapper/secure_http — those are None).
  • File storage: platform FS (default root/base URL provided), not S3/GCS unless the
    user named them.
  • All imports inside the generated function body (sandbox exec).

═══════════════════════════════════════════════════════
FIDELITY RULES — USER-STATED FACTS MUST APPEAR IN databaseLogic
═══════════════════════════════════════════════════════
These are the highest-priority constraints. Always check the ORIGINAL USER REQUIREMENT
before writing databaseLogic.

1. API SPECS FROM ORIGINAL REQUIREMENT — if the user stated any of the following for
   this dataSource's operation, they MUST appear verbatim in databaseLogic:
   • Exact base URL or endpoint path (e.g. "https://api.example.com/v1/leads")
   • HTTP method (GET / POST / PUT / PATCH / DELETE)
   • Request payload field names and their types
   • Response field names and their expected values
   • Auth header name or credential key name
   → If a spec is partially described, reproduce the known parts and add:
     "Remaining request/response fields must be obtained from [API name] docs."

2. DATABASE SCHEMA FROM IR — always use the relatedDataModels schemas to name exact
   fields in databaseLogic. Do NOT use generic "document" or "record" placeholders when
   the actual field names are available in relatedDataModels.
   • Map inputModel fields → exact Mongo field names via operation.inputFieldMappings
     when present; otherwise use the inputModel field name directly.
   • On INSERT/UPDATE: list every field being written with its source (params.<field>)
     and type. Set created_at/updated_at UTC timestamps for every write.
   • On FIND/FIND_ONE: name the exact filter keys and projection fields.

3. MISSING SCHEMA — if the operation targets a collection but relatedDataModels does
   not include its schema:
   → Include in databaseLogic:
     "Runtime: inspect the [collection_name] collection to discover actual field names
      before querying. Resolve casing (snake_case vs camelCase) from live schema.
      Do NOT assume field names."

4. VERBATIM BUSINESS RULES — exact threshold values, status strings, enum values,
   branch conditions, error codes, and SLA durations stated by the user MUST appear
   literally in databaseLogic. Never paraphrase or generalize them.

5. PINNED PERSISTENCE LINK — if ORIGINAL USER REQUIREMENT contains a
   PINNED PERSISTENCE LINK block, copy collection + correlationKey/filterKey
   verbatim into databaseLogic ONCE (never paste the same PINNED block
   more than once). If databaseLogic already contains a PINNED PERSISTENCE
   LINK block, keep a single copy — do not stack duplicates.
   Writer and reader MUST use that exact Mongo field.
   Do NOT substitute a different batch key (for example jd_id instead of
   candidates_batch_id, or the reverse).

DATA RULES:
  • No bare object/array JSON blobs. Scalars are named fields.
  • Every field mentioned must exist on this dataSource's inputModel or responseModel
    (or a nested items.model already in dataModels).
  • Files: base64 or URL string + separate extracted fields.
  • Collection names consistent with frozen operation.collection / sibling dataSources.

Output valid JSON matching the schema — no markdown, no explanation."""

_DS_ENRICHER_JSON_SCHEMA = """Required JSON schema:
{
  "dataSources": [
    {
      "id": "snake_case_id",
      "description": "Clear human-readable summary of this backend function.",
      "databaseLogic": "Production-grade executable algorithm spec for the code generator.",
      "operationDescription": "Optional. Production summary of the frozen operation, or empty string."
    }
  ]
}"""

_DS_ENRICHER_CACHED_SYSTEM = f"{_DS_ENRICHER_SYSTEM}\n\n{_DS_ENRICHER_JSON_SCHEMA}"


def _ai_processor_response_text(response: Any) -> str:
    try:
        text = response.text or ""
        if text:
            return text
    except Exception:
        pass
    if getattr(response, "candidates", None):
        for c in response.candidates:
            parts = getattr(c, "content", None) and getattr(c.content, "parts", None)
            if parts:
                joined = "\n".join(
                    getattr(p, "text", "") for p in parts if getattr(p, "text", "")
                )
                if joined:
                    return joined
    return ""


def _ds_spec_collapsed(original: str, incoming: str) -> bool:
    """True when incoming looks like slogan-collapse, not a denser rewrite."""
    orig = (original or "").strip()
    inc = (incoming or "").strip()
    if not orig or not inc:
        return False
    return len(inc) < max(80, int(0.5 * len(orig)))


def _compact_datasource_for_enricher(ds: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": ds.get("id"),
        "type": ds.get("type"),
        "description": ds.get("description") or "",
        "inputModel": ds.get("inputModel"),
        "responseModel": ds.get("responseModel"),
        "config": ds.get("config"),
        "operation": ds.get("operation"),
        "databaseLogic": ds.get("databaseLogic") or "",
        "dependencies": ds.get("dependencies"),
    }


def _expand_related_model_names(data_models: Any, names: set) -> set:
    """Include nested model names referenced by the already-selected models."""
    expanded = {n for n in names if isinstance(n, str) and n}
    if not expanded or not isinstance(data_models, dict):
        return expanded
    blob = json.dumps(
        {k: data_models[k] for k in expanded if k in data_models},
        default=str,
    )
    for key in data_models:
        if key not in expanded and f'"{key}"' in blob:
            expanded.add(key)
    return expanded


def _pick_related_data_models(
    data_models: Any,
    names: set,
) -> Any:
    names = _expand_related_model_names(data_models, names or set())
    if isinstance(data_models, dict):
        if len(data_models) <= 12 or not names:
            return data_models
        picked = {k: v for k, v in data_models.items() if k in names}
        return picked or data_models
    if isinstance(data_models, list):
        if len(data_models) <= 12 or not names:
            return data_models
        picked = [
            m for m in data_models
            if isinstance(m, dict) and m.get("name") in names
        ]
        return picked or data_models
    return data_models or {}


def _compact_workflow_context_for_enricher(workflow_context: Optional[Any]) -> str:
    if not workflow_context:
        return "(none)"
    if isinstance(workflow_context, str):
        text = workflow_context
    elif isinstance(workflow_context, dict):
        skip = {"_design_override_requested"}
        compact = {
            k: v for k, v in workflow_context.items()
            if k not in skip
        }
        text = json.dumps(compact, indent=2, default=str)
    else:
        try:
            text = json.dumps(workflow_context, indent=2, default=str)
        except Exception:
            text = str(workflow_context)
    if len(text) > 12000:
        return text[:12000] + "\n…(truncated)"
    return text


def _compact_credentials_for_enricher(
    available_credentials: Optional[List[Dict[str, Any]]],
) -> str:
    if not available_credentials:
        return (
            "None currently configured. Reference descriptive credential names in "
            "databaseLogic (e.g. gemini_api_key) and note they must be configured. "
            "Runtime reads secrets from request_meta['credentials'] only."
        )
    cred_list = [
        {
            "name": c.get("name"),
            "type": c.get("type"),
            "service": c.get("service"),
        }
        for c in available_credentials
        if isinstance(c, dict)
    ]
    return json.dumps(cred_list, indent=2)


def _resolve_enricher_ds_id(
    raw_id: str,
    ds_ids: Dict[str, str],
    ds_ids_lower: Dict[str, List[str]],
) -> Optional[str]:
    nid = (raw_id or "").strip()
    if not nid:
        return None
    if nid in ds_ids:
        return ds_ids[nid]
    matches = ds_ids_lower.get(nid.lower()) or []
    if len(matches) == 1:
        return matches[0]
    return None


def _strip_json_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    return text


def _normalize_enriched_ds_item(item: Any) -> Any:
    if item is None:
        return item
    if not isinstance(item, dict):
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        elif hasattr(item, "dict") and callable(item.dict):
            item = item.dict()
        else:
            return item
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if not (out.get("databaseLogic") or "").strip():
        alt = out.get("database_logic")
        if alt:
            out["databaseLogic"] = alt
    if not (out.get("operationDescription") or "").strip():
        alt = out.get("operation_description")
        if alt:
            out["operationDescription"] = alt
    if not (out.get("id") or "").strip():
        alt = out.get("dataSourceId") or out.get("data_source_id")
        if alt:
            out["id"] = alt
    return out


def _coerce_ds_enrichment_payload(parsed: Any) -> Optional[Dict[str, Any]]:
    if parsed is None:
        return None
    if isinstance(parsed, DataSourceEnrichmentResult):
        return parsed.model_dump()
    data: Any = parsed
    if not isinstance(data, (dict, list)):
        if hasattr(parsed, "model_dump"):
            data = parsed.model_dump()
        elif hasattr(parsed, "dict") and callable(parsed.dict):
            data = parsed.dict()
        else:
            return None
    if isinstance(data, list):
        return {"dataSources": [_normalize_enriched_ds_item(x) for x in data]}
    if not isinstance(data, dict):
        return None
    payload = dict(data)
    if "dataSources" not in payload:
        if "datasources" in payload:
            payload["dataSources"] = payload.get("datasources")
        elif "data_sources" in payload:
            payload["dataSources"] = payload.get("data_sources")
    items = payload.get("dataSources")
    if isinstance(items, list):
        payload["dataSources"] = [_normalize_enriched_ds_item(x) for x in items]
    elif not items and payload.get("id"):
        payload = {"dataSources": [_normalize_enriched_ds_item(payload)]}
    return payload


def _parse_json_object_relaxed(raw: str) -> Any:
    text = _strip_json_fences(raw)
    if not text:
        raise ValueError("enricher response was empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(repair_json(text))
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return json.loads(repair_json(snippet))
    raise ValueError("enricher response was not a JSON object")


def _parse_ds_enrichment_result(raw: str, parsed: Any) -> DataSourceEnrichmentResult:
    payload = _coerce_ds_enrichment_payload(parsed)
    has_items = bool(payload and payload.get("dataSources"))
    if not has_items and (raw or "").strip():
        try:
            raw_payload = _coerce_ds_enrichment_payload(_parse_json_object_relaxed(raw))
            if raw_payload and raw_payload.get("dataSources"):
                payload = raw_payload
        except Exception:
            if not payload:
                raise
    if not payload:
        raise ValueError("enricher response was not a JSON object")
    return DataSourceEnrichmentResult(**payload)


def _ds_enrichment_coverage_errors(
    result: DataSourceEnrichmentResult,
    batch: List[Dict[str, Any]],
    all_ids: Dict[str, str],
    all_ids_lower: Dict[str, List[str]],
) -> List[str]:
    by_id: Dict[str, EnrichedDataSourceSpec] = {}
    for item in result.dataSources or []:
        resolved = _resolve_enricher_ds_id(item.id or "", all_ids, all_ids_lower)
        logic = (item.databaseLogic or "").strip()
        if resolved and logic:
            by_id[resolved] = item

    errors: List[str] = []
    for ds in batch:
        ds_id = ds.get("id") or ""
        incoming = by_id.get(ds_id)
        if incoming is None:
            errors.append(f"missing non-empty databaseLogic for '{ds_id}'")
            continue
        orig_logic = (ds.get("databaseLogic") or "").strip()
        inc_logic = (incoming.databaseLogic or "").strip()
        if not orig_logic and len(inc_logic) < 120:
            errors.append(
                f"databaseLogic for '{ds_id}' is too thin ({len(inc_logic)} chars); "
                "expand to a production-grade executable spec"
            )
            continue
        if _ds_spec_collapsed(orig_logic, inc_logic):
            errors.append(
                f"databaseLogic for '{ds_id}' collapsed "
                f"({len(orig_logic)}→{len(inc_logic)} chars); expand to a production-grade spec"
            )
    return errors


def _apply_enriched_datasource_specs(
    data_sources: List[Dict[str, Any]],
    enriched: List[EnrichedDataSourceSpec],
    *,
    allowed_ids: Optional[set] = None,
) -> Tuple[int, List[str]]:
    """
    Merge enricher text onto existing dataSources. Structure is unchanged.

    Applies description / databaseLogic independently. Drops unknown ids,
    empty text, and slogan-collapsed databaseLogic rewrites.
    """
    allowed = allowed_ids if allowed_ids is not None else {
        ds.get("id") for ds in data_sources if ds.get("id")
    }
    ds_ids = {ds.get("id"): ds.get("id") for ds in data_sources if ds.get("id")}
    ds_ids_lower: Dict[str, List[str]] = defaultdict(list)
    for ds in data_sources:
        ds_id = ds.get("id")
        if ds_id:
            ds_ids_lower[ds_id.lower()].append(ds_id)

    by_id: Dict[str, EnrichedDataSourceSpec] = {}
    extras: List[str] = []
    for item in enriched or []:
        raw_id = (item.id or "").strip()
        if not raw_id:
            continue
        resolved = _resolve_enricher_ds_id(raw_id, ds_ids, ds_ids_lower)
        if resolved is None:
            extras.append(raw_id)
            continue
        if resolved not in allowed:
            continue
        by_id[resolved] = item

    warnings: List[str] = []
    if extras:
        warnings.append(
            f"enricher returned extra dataSource ids (ignored): {sorted(set(extras))}"
        )

    applied = 0
    for ds in data_sources:
        ds_id = ds.get("id")
        incoming = by_id.get(ds_id) if ds_id else None
        if not incoming:
            continue
        changed = False

        inc_desc = (incoming.description or "").strip()
        orig_desc = (ds.get("description") or "").strip()
        # description is a short summary (2–6 sentences). Do not use the
        # databaseLogic collapse guard — a tighter summary is expected.
        if inc_desc:
            too_thin = bool(orig_desc) and len(inc_desc) < 40 and len(inc_desc) < len(orig_desc)
            if too_thin:
                warnings.append(
                    f"enricher description for '{ds_id}' too thin "
                    f"({len(orig_desc)}→{len(inc_desc)} chars); keeping original"
                )
            else:
                ds["description"] = inc_desc
                changed = True

        inc_logic = (incoming.databaseLogic or "").strip()
        orig_logic = (ds.get("databaseLogic") or "").strip()
        if inc_logic and not orig_logic:
            if len(inc_logic) >= 120:
                ds["databaseLogic"] = inc_logic
                changed = True
            else:
                warnings.append(
                    f"enricher databaseLogic for '{ds_id}' too thin; keeping original"
                )
        elif inc_logic and not _ds_spec_collapsed(orig_logic, inc_logic):
            ds["databaseLogic"] = inc_logic
            changed = True
        elif inc_logic and _ds_spec_collapsed(orig_logic, inc_logic):
            warnings.append(
                f"enricher collapsed databaseLogic for '{ds_id}' "
                f"({len(orig_logic)}→{len(inc_logic)} chars); keeping original"
            )

        op_desc = (incoming.operationDescription or "").strip()
        if op_desc and isinstance(ds.get("operation"), dict):
            ds["operation"]["description"] = op_desc
            changed = True

        if changed:
            applied += 1

    missing = [
        ds.get("id")
        for ds in data_sources
        if ds.get("id") in allowed and ds.get("id") not in by_id
    ]
    if missing:
        warnings.append(f"enricher omitted dataSource ids: {missing}")

    return applied, warnings


def _build_ds_enricher_user_prompt(
    *,
    original_prompt: str,
    enhanced_prompt: str,
    workflow_context: Optional[Dict[str, Any]],
    available_credentials: Optional[List[Dict[str, Any]]],
    ir_schema: Dict[str, Any],
    batch: List[Dict[str, Any]],
    db_name: str,
    fs_root: str,
    fs_base_url: str,
) -> str:
    all_ds = ir_schema.get("dataSources") or []
    all_index = [
        {
            "id": ds.get("id"),
            "type": ds.get("type"),
            "inputModel": ds.get("inputModel"),
            "responseModel": ds.get("responseModel"),
            "description": (ds.get("description") or "")[:240],
        }
        for ds in all_ds
        if isinstance(ds, dict) and ds.get("id")
    ]
    model_names = set()
    for ds in batch:
        for key in ("inputModel", "responseModel"):
            name = ds.get(key)
            if isinstance(name, str) and name:
                model_names.add(name)
    related_models = _pick_related_data_models(ir_schema.get("dataModels"), model_names)
    batch_ids = [ds.get("id") for ds in batch]
    batch_block = "\n".join(
        f"  - {ds.get('id')} ({ds.get('type') or 'unknown'})"
        for ds in batch
    )
    frozen_contract = {
        "screen": ir_schema.get("screen"),
        "inputParameters": ir_schema.get("inputParameters") or [],
        "outputParameters": ir_schema.get("outputParameters") or [],
        "allDataSources": all_index,
        "relatedDataModels": related_models,
        "enrichTheseDataSources": [_compact_datasource_for_enricher(ds) for ds in batch],
    }
    return f"""ORIGINAL USER REQUIREMENT (authoritative — restore details):
{original_prompt or '(none)'}

ENHANCED SCREEN PROMPT:
{enhanced_prompt or '(none)'}

WORKFLOW / NODE CONTEXT:
{_compact_workflow_context_for_enricher(workflow_context)}

AVAILABLE CREDENTIALS (reference names only; never invent secret values):
{_compact_credentials_for_enricher(available_credentials)}

PLATFORM DEFAULTS:
  Mongo db: {db_name}
  File upload root: {fs_root}
  File base URL: {fs_base_url}

FROZEN IR CONTRACT (do not change ids/type/models/config/operation structure):
{json.dumps(frozen_contract, indent=2, default=str)}

ENRICH THESE DATASOURCES (return exactly these ids, nothing else):
{batch_block}

Rewrite each listed dataSource's description + databaseLogic into a production-grade
codegen spec. Apply ALL FIDELITY RULES from the system prompt:
• Check ORIGINAL USER REQUIREMENT for API specs (URL, method, request/response fields)
  and include them verbatim in databaseLogic.
• Use relatedDataModels field schemas to name exact Mongo fields in databaseLogic.
• If the collection schema is absent → add the runtime-inspect note (FIDELITY RULE 3).
• All user-stated status strings, thresholds, and error codes must appear literally.
• If ORIGINAL USER REQUIREMENT contains PINNED PERSISTENCE LINK, copy that
  collection + correlationKey/filterKey verbatim into databaseLogic 
Do not add dataSources or change IR structure.
Target ids: {", ".join(str(i) for i in batch_ids)}"""


_FLASH_MODEL = "gemini-3-flash-preview"
_PROMPT_ENHANCE_CACHE_PREFIX = "ir-prompt-enhance-v1"
_IO_VERIFY_CACHE_PREFIX = "ir-io-verify-v1"
_IO_DETECT_CACHE_PREFIX = "ir-io-detect-v1"

# ---------------------------------------------------------------------------
# LLM direction classifier
# ---------------------------------------------------------------------------
_DIRECTION_CLASSIFY_SYSTEM = (
    "You are a UI design direction classifier. "
    "You will receive a user prompt and a numbered list of design directions. "
    "Return ONLY the number of the best matching direction — nothing else, no explanation."
)


async def _classify_direction_via_llm(
    user_prompt: str,
    direction_enum: List[str],
) -> Optional[str]:
    """
    Ask the LLM to pick the best design direction for the prompt.

    This is a cheap micro-call (~200 input tokens, single number output) used to
    override the BM25 engine's direction when the prompt doesn't carry an explicit
    style term.  We use gemini-3.5-flash-lite at temperature=0 for determinism.

    Returns the matched direction string or None if the call fails / returns garbage.
    """
    numbered = "\n".join(f"{i+1}. {d}" for i, d in enumerate(direction_enum))
    user_content = (
        f"User prompt: {user_prompt[:500]}\n\n"
        f"Design directions:\n{numbered}\n\n"
        f"Return only the number of the best matching direction:"
    )
    full_prompt = f"{_DIRECTION_CLASSIFY_SYSTEM}\n\n{user_content}"
    config = GeminiGenerateConfig(temperature=0.0, max_output_tokens=8)
    try:
        response = await run_sync(sync_generate, "gemini-3.5-flash-lite", full_prompt, config=config)
        text = (response.text if hasattr(response, "text") else str(response)).strip()
        # Extract first integer from response
        m = re.search(r"\b(\d+)\b", text)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(direction_enum):
                return direction_enum[idx]
    except Exception as exc:
        logger.warning("LLM direction classifier failed (non-fatal): %s", exc)
    return None


def _build_workflow_context_block(
    workflow_context: Optional[Dict[str, Any]],
    *,
    for_enhancement: bool,
) -> str:
    """Format optional workflow context for enhancement or verify prompts."""
    if not workflow_context:
        return ""
    if for_enhancement:
        return f"""WORKFLOW CONTEXT:
This screen is part of a multi-step workflow.
- Step ID: {workflow_context.get("step_id", "N/A")}
- Previous steps: {workflow_context.get("previous_steps", [])}
- Available outputs from previous steps: {workflow_context.get("available_outputs", {})}

IMPORTANT: If the user specified specific output parameters or format, you MUST preserve them exactly.
"""
    return f"""WORKFLOW CONTEXT:
This screen is step {workflow_context.get("step_id", "unknown")} in a multi-step workflow.
- Available inputs from previous steps: {json.dumps(workflow_context.get("available_outputs", {}), indent=2)}
- This step must produce outputs that next steps can consume.
"""


def _build_prompt_enhancement_user_content(
    prompt: str,
    workflow_context: Optional[Dict[str, Any]] = None,
) -> str:
    workflow_block = _build_workflow_context_block(
        workflow_context,
        for_enhancement=True,
    )
    parts = [
        f'''USER'S ORIGINAL PROMPT:
"{prompt}"''',
    ]
    if workflow_block:
        parts.append(workflow_block.rstrip())
    return "\n\n".join(parts)


def _build_prompt_enhancement_full_prompt(
    prompt: str,
    workflow_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Uncached enhancement prompt — original section order (user before YOUR TASK)."""
    user_content = _build_prompt_enhancement_user_content(prompt, workflow_context)
    split = _PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE.index("\n\nYOUR TASK:")
    part_a = _PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE[:split]
    part_b = _PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE[split:]
    return f"{part_a}\n\n{user_content}{part_b}"


def _build_io_self_verify_user_content(
    *,
    enhanced_prompt: str,
    workflow_context: Optional[Dict[str, Any]] = None,
    components_summary: List[Dict[str, Any]],
    datasources_summary: List[Dict[str, Any]],
    datamodels_list: List[str],
    current_input_params: List[Dict[str, Any]],
    current_output_params: List[Dict[str, Any]],
) -> str:
    workflow_block = _build_workflow_context_block(
        workflow_context,
        for_enhancement=False,
    )
    parts = [
        f"""USER'S REQUIREMENTS (ENHANCED):
{enhanced_prompt}""",
    ]
    if workflow_block:
        parts.append(workflow_block.rstrip())
    parts.append(
        f"""IR SCHEMA CONTEXT (for validation reference):

COMPONENTS IN THIS SCREEN:
{json.dumps(components_summary, indent=2)}

DATASOURCES IN THIS SCREEN:
{json.dumps(datasources_summary, indent=2)}

DATA MODELS DEFINED:
{json.dumps(datamodels_list, indent=2)}

CURRENT INPUT PARAMETERS:
{json.dumps(current_input_params, indent=2)}

CURRENT OUTPUT PARAMETERS:
{json.dumps(current_output_params, indent=2)}"""
    )
    return "\n\n".join(parts)


def _build_io_self_verify_full_prompt(
    *,
    enhanced_prompt: str,
    workflow_context: Optional[Dict[str, Any]] = None,
    components_summary: List[Dict[str, Any]],
    datasources_summary: List[Dict[str, Any]],
    datamodels_list: List[str],
    current_input_params: List[Dict[str, Any]],
    current_output_params: List[Dict[str, Any]],
) -> str:
    """Uncached verify prompt — original section order (IR context before checklist)."""
    user_content = _build_io_self_verify_user_content(
        enhanced_prompt=enhanced_prompt,
        workflow_context=workflow_context,
        components_summary=components_summary,
        datasources_summary=datasources_summary,
        datamodels_list=datamodels_list,
        current_input_params=current_input_params,
        current_output_params=current_output_params,
    )
    split = _IO_SELF_VERIFY_SYSTEM_TEMPLATE.index("\n\nSECURITY:")
    part_a = _IO_SELF_VERIFY_SYSTEM_TEMPLATE[:split]
    part_b = _IO_SELF_VERIFY_SYSTEM_TEMPLATE[split:]
    return f"{part_a}\n\n{user_content}{part_b}"


def _build_io_detection_user_content(enhanced_prompt: str) -> str:
    return f"""    USER'S ENHANCED PROMPT:
    {enhanced_prompt}"""


def _build_io_detection_full_prompt(enhanced_prompt: str) -> str:
    """Uncached IO detection prompt — original section order (prompt before YOUR TASK)."""
    user_content = _build_io_detection_user_content(enhanced_prompt)
    split = _IO_DETECTION_SYSTEM_TEMPLATE.index("\n\n    YOUR TASK:")
    part_a = _IO_DETECTION_SYSTEM_TEMPLATE[:split]
    part_b = _IO_DETECTION_SYSTEM_TEMPLATE[split + 2 :]
    return f"{part_a}\n\n{user_content}\n\n{part_b}"


def _load_backend_config() -> Dict[str, Any]:
    """
    Load backend/config.json once and cache it.

    This lets IR generation align with the same environment defaults
    (MongoDB name, filesystem roots, public URLs) used by the backend.
    """
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
                _CONFIG_CACHE = {}
        except Exception:
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE or {}


_IO_TYPE_ALIASES = {
    "str": "string",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}
_IO_NUMERIC_TYPES = frozenset({"number", "int", "float"})


def _normalize_io_param_type(type_val: Any) -> str:
    """Normalize IO parameter types for deterministic comparison."""
    normalized = str(type_val or "string").strip().lower()
    if normalized in _IO_TYPE_ALIASES:
        return _IO_TYPE_ALIASES[normalized]
    if normalized in _IO_NUMERIC_TYPES:
        return "number"
    return normalized


def _normalize_io_params_for_compare(
    params: Optional[List[Dict[str, Any]]],
    *,
    is_input: bool,
) -> List[Tuple[str, str, Optional[bool]]]:
    """
    Normalize input/output parameter lists to (name, type, required) tuples.

    Extra IR fields (bindTo, extractFrom, etc.) are ignored — self-verify only
    reconciles the workflow IO contract names/types/required flags.
    """
    normalized: List[Tuple[str, str, Optional[bool]]] = []
    for param in params or []:
        if not isinstance(param, dict):
            continue
        name = (param.get("name") or "").strip()
        if not name:
            continue
        param_type = _normalize_io_param_type(param.get("type"))
        required: Optional[bool] = bool(param.get("required", True)) if is_input else None
        normalized.append((name, param_type, required))
    normalized.sort(key=lambda item: item[0])
    return normalized


def io_params_contract_match(
    detected_inputs: Optional[List[Dict[str, Any]]],
    detected_outputs: Optional[List[Dict[str, Any]]],
    ir_inputs: Optional[List[Dict[str, Any]]],
    ir_outputs: Optional[List[Dict[str, Any]]],
) -> bool:
    """True when IR IO arrays match the detected enhancement contract."""
    detected_in = _normalize_io_params_for_compare(detected_inputs, is_input=True)
    detected_out = _normalize_io_params_for_compare(detected_outputs, is_input=False)
    ir_in = _normalize_io_params_for_compare(ir_inputs, is_input=True)
    ir_out = _normalize_io_params_for_compare(ir_outputs, is_input=False)

    def _output_signature(
        params: List[Tuple[str, str, Optional[bool]]],
    ) -> List[Tuple[str, str]]:
        return [(name, param_type) for name, param_type, _ in params]

    return detected_in == ir_in and _output_signature(detected_out) == _output_signature(ir_out)


# Markers appended by workflow builder gap resolution (/agentic/plan/execute) and
# multi-step generate-angular-flow. When present, IO params are parsed without LLM.
WORKFLOW_IO_CONTRACT_SCREEN_MARKER = (
    "WORKFLOW IO CONTRACT FOR THIS SCREEN (do not ignore):"
)
WORKFLOW_IO_CONTRACT_STEP_MARKER = (
    "WORKFLOW IO CONTRACT FOR THIS STEP (do not ignore):"
)
_WORKFLOW_IO_CONTRACT_MARKERS = (
    WORKFLOW_IO_CONTRACT_SCREEN_MARKER,
    WORKFLOW_IO_CONTRACT_STEP_MARKER,
)


def _normalize_workflow_contract_param(
    param: Dict[str, Any],
    *,
    is_input: bool,
) -> Optional[Dict[str, Any]]:
    """Map workflow-builder IO rows (dataType) to IR detection shape (type)."""
    name = (param.get("name") or "").strip()
    if not name:
        return None

    raw_type = (
        param.get("type") or param.get("dataType") or param.get("datatype") or "string"
    )
    param_type = _normalize_io_param_type(raw_type)
    if param_type == "any":
        param_type = "string"

    normalized: Dict[str, Any] = {
        "name": name,
        "type": param_type,
        "description": (param.get("description") or "").strip(),
    }
    # Issue 7: keep graph requiredness on outputs too. Listed outputs default
    # required; inputs still default optional when the flag is omitted.
    if "required" in param:
        normalized["required"] = bool(param.get("required"))
    else:
        normalized["required"] = False if is_input else True
    if is_input:
        source = param.get("source")
        if source:
            normalized["source"] = source
    example = param.get("example")
    if example is not None and str(example).strip():
        normalized["example"] = example

    # Preserve list item kind so IR/codegen can emit string[] vs List[ItemModel].
    item_type = param.get("itemType")
    if item_type:
        normalized["itemType"] = item_type
    scalar_dt = param.get("scalarItemDatatype")
    if scalar_dt:
        normalized["scalarItemDatatype"] = scalar_dt
    enum_values = param.get("enumValues")
    if enum_values:
        normalized["enumValues"] = list(enum_values)

    # Preserve itemSchema for record-list params so the IR/Angular code-gen LLM knows
    # the record field structure — without this it sees only type="list" with no
    # detail and cannot generate correct iteration templates or backend handlers.
    item_schema = param.get("itemSchema")
    if item_schema and isinstance(item_schema, list):
        nested: List[Dict[str, Any]] = []
        for f in item_schema:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            row = _normalize_workflow_contract_param(f, is_input=False)
            if row:
                nested.append(row)
        if nested:
            normalized["itemSchema"] = nested

    return normalized


def _normalize_workflow_contract_params(
    params: Optional[List[Any]],
    *,
    is_input: bool,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in params or []:
        if not isinstance(item, dict):
            continue
        row = _normalize_workflow_contract_param(item, is_input=is_input)
        if row:
            normalized.append(row)
    return normalized


def parse_workflow_io_contract_from_prompt(
    prompt: str,
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """
    Extract machine-authored workflow IO contract embedded in a prompt.

    Used for /agentic/plan/execute gap UI screens and generate-angular-flow steps
    where IO is already defined by the graph planner.
    """
    if not prompt:
        return None

    marker: Optional[str] = None
    for candidate in _WORKFLOW_IO_CONTRACT_MARKERS:
        if candidate in prompt:
            marker = candidate
            break
    if not marker:
        return None

    tail = prompt.split(marker, 1)[1].strip()
    start = tail.find("{")
    if start < 0:
        return None

    try:
        payload, _ = json.JSONDecoder().raw_decode(tail, start)
    except json.JSONDecodeError:
        logger.warning("Failed to parse workflow IO contract JSON from prompt")
        return None

    if not isinstance(payload, dict):
        return None

    raw_inputs = payload.get("inputParameters")
    raw_outputs = payload.get("outputParameters")
    if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
        return None

    return {
        "inputParameters": _normalize_workflow_contract_params(
            raw_inputs,
            is_input=True,
        ),
        "outputParameters": _normalize_workflow_contract_params(
            raw_outputs,
            is_input=False,
        ),
    }


def prompt_has_workflow_io_contract(prompt: str) -> bool:
    """True when prompt carries an authoritative workflow-builder IO contract."""
    if not prompt:
        return False
    return any(marker in prompt for marker in _WORKFLOW_IO_CONTRACT_MARKERS)


# Design direction values — Style Category names from ui-ux-pro-max styles.csv (v2.11.0).
# Used by IR theme.direction enum; keep in sync when bumping the vendored design engine.
DESIGN_DIRECTION_ENUM = [
    "Minimalism & Swiss Style",
    "Neumorphism",
    "Glassmorphism",
    "Brutalism",
    "3D & Hyperrealism",
    "Vibrant & Block-based",
    "Dark Mode (OLED)",
    "Accessible & Ethical",
    "Claymorphism",
    "Aurora UI",
    "Retro-Futurism",
    "Flat Design",
    "Skeuomorphism",
    "Liquid Glass",
    "Motion-Driven",
    "Micro-interactions",
    "Inclusive Design",
    "Zero Interface",
    "Soft UI Evolution",
    "Hero-Centric Design",
    "Conversion-Optimized",
    "Feature-Rich Showcase",
    "Minimal & Direct",
    "Social Proof-Focused",
    "Interactive Product Demo",
    "Trust & Authority",
    "Storytelling-Driven",
    "Data-Dense Dashboard",
    "Heat Map & Heatmap Style",
    "Executive Dashboard",
    "Real-Time Monitoring",
    "Drill-Down Analytics",
    "Comparative Analysis Dashboard",
    "Predictive Analytics",
    "User Behavior Analytics",
    "Financial Dashboard",
    "Sales Intelligence Dashboard",
    "Neubrutalism",
    "Bento Box Grid",
    "Y2K Aesthetic",
    "Cyberpunk UI",
    "Organic Biophilic",
    "AI-Native UI",
    "Memphis Design",
    "Vaporwave",
    "Dimensional Layering",
    "Exaggerated Minimalism",
    "Kinetic Typography",
    "Parallax Storytelling",
    "Swiss Modernism 2.0",
    "HUD / Sci-Fi FUI",
    "Pixel Art",
    "Bento Grids",
    "Spatial UI (VisionOS)",
    "E-Ink / Paper",
    "Gen Z Chaos / Maximalism",
    "Biomimetic / Organic 2.0",
    "Anti-Polish / Raw Aesthetic",
    "Tactile Digital / Deformable UI",
    "Nature Distilled",
    "Interactive Cursor Design",
    "Voice-First Multimodal",
    "3D Product Preview",
    "Gradient Mesh / Aurora Evolved",
    "Editorial Grid / Magazine",
    "Chromatic Aberration / RGB Split",
    "Vintage Analog / Retro Film",
    "Bauhaus (包豪斯)",
    "Minimalist Monochrome",
    "Modern Dark Cinema",
    "High-Tech Boutique",
    "Terminal CLI",
    "Kinetic Brutalism",
    "Flat Design (Touch-First)",
    "Material You (MD3)",
    "Neo Brutalism",
    "Bold Typography Poster",
    "Academia (Scholarly)",
    "Cyberpunk HUD",
    "Bitcoin DeFi",
    # Claymorphism / Neumorphism already listed above (styles.csv "(Mobile)" variants map via aliases)
    "Enterprise SaaS",
    "Sketch Hand-Drawn",
]

# JSON Schema for IR validation (Full-Stack Contract, now workflow-aware)
IR_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "screen",
        "theme",
        "layout",
        "components",
        "dataModels",
        "dataSources",
        "inputParameters",
        "outputParameters",
    ],
    "properties": {
        "screen": {
            "type": "object",
            "required": ["type", "title"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["form", "table", "dashboard", "detail", "list", "wizard", "mixed"],
                },
                "title": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "route": {"type": "string"},
                "id": {"type": "string"},
                "selector": {"type": "string", "pattern": "^app-[a-z][a-z0-9-]*$"},
            },
        },
        "theme": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": DESIGN_DIRECTION_ENUM},
                "primary": {"type": "string", "minLength": 1},
                "secondary": {"type": "string", "minLength": 1},
                "accent": {"type": "string", "minLength": 1},
                "background": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1},
                "error": {"type": "string", "minLength": 1},
                "success": {"type": "string", "minLength": 1},
                "warning": {"type": "string", "minLength": 1},
                "info": {"type": "string", "minLength": 1},
                "typography": {
                    "type": "object",
                    "properties": {
                        "display": {"type": "string", "minLength": 1},
                        "body": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": True,
                },
                "effects": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    ],
                },
                # Legacy single-font field — prefer typography.display / typography.body
                "font": {"type": "string"},
                "radius": {"type": "string"},
                "spacing": {"type": "string"},
                "shadow": {"type": "string"},
            },
        },
        "antiPatterns": {
            "type": "array",
            "items": {"type": "string"},
        },
        "layout": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["single", "multi", "grid"],
                },
                "columns": {"type": "integer", "minimum": 1},
                "gap": {"type": "string"},
                "max_width": {"type": "string"},
            },
        },
        "components": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string"},
                    "label": {"type": "string"},
                    "props": {"type": "object"},
                    "position": {
                        "type": "object",
                        "properties": {
                            "order": {"type": "integer"},
                            "width": {"type": "string"},
                            "visible": {"type": "boolean"},
                        },
                    },
                    "style": {
                        "type": "object",
                        "properties": {
                            "responsive": {
                                "type": "object",
                                "properties": {
                                    "tablet": {"type": "object"},
                                    "mobile": {"type": "object"},
                                },
                            }
                        },
                    },
                    "actions": {"type": "array"},
                    "permissions": {"type": "object"},
                    "children": {"type": "array"},
                },
                "additionalProperties": True,
            },
        },
        "dataModels": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": True,
        },
        "dataSources": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "type", "config"],
                "additionalProperties": True,  # Allow flexibility
            },
        },
        # NEW: Input parameters expected from previous workflow steps
        "inputParameters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "type": {
                        "type": "string",
                        "enum": [
                            "string",
                            "int",
                            "float",
                            "bool",
                            "datetime",
                            "dict",
                            "list",
                            "object",
                            "number",
                        ],
                    },
                    "description": {"type": "string"},
                    "required": {"type": "boolean", "default": True},
                    "defaultValue": {},
                    "source": {
                        "type": "object",
                        "properties": {
                            "stepId": {"type": "string"},
                            "outputParam": {"type": "string"},
                        },
                    },
                    "bindTo": {
                        "type": "array",
                        "description": "Component/dataSource bindings",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "path": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        # NEW: Output parameters to pass to next workflow steps
        "outputParameters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "type": {
                        "type": "string",
                        "enum": [
                            "string",
                            "int",
                            "float",
                            "bool",
                            "datetime",
                            "dict",
                            "list",
                            "object",
                            "number",
                        ],
                    },
                    "description": {"type": "string"},
                    "sourceComponent": {"type": "string"},
                    "sourcePath": {"type": "string"},
                    "extractFrom": {
                        "type": "object",
                        "properties": {
                            "dataSourceId": {"type": "string"},
                            "responsePath": {"type": "string"},
                        },
                    },
                },
            },
        },
        "state": {
            "type": "object",
            "properties": {
                "bindings": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "defaults": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        },
        "data": {"type": "array"},
    },
}

class TokenMetrics:
    """Track token usage and costs across API calls"""
    
    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_calls = 0
        
        # Gemini 2.5 Flash pricing (as of Oct 2025)
        self.input_cost_per_1k = 0.00030  # $0.30 per 1M tokens
        self.output_cost_per_1k = 0.0025 # $2.50 per 1M tokens
    
    def add_usage(self, input_tokens: int, output_tokens: int):
        """Add token usage from a single API call"""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_calls += 1
    
    def get_cost(self) -> dict:
        """Calculate total cost"""
        input_cost = (self.total_input_tokens / 1000) * self.input_cost_per_1k
        output_cost = (self.total_output_tokens / 1000) * self.output_cost_per_1k
        total_cost = input_cost + output_cost
        
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_calls": self.total_calls,
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(total_cost, 6)
        }
    
    def reset(self):
        """Reset all metrics"""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_calls = 0

class AIProcessor:
    def __init__(self):
        #self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        if not get_gemini_api_key():
            raise ValueError("Failed to retrieve Gemini API key from Secret Manager")
        self.token_metrics = TokenMetrics()
        self.config = _load_backend_config()
        # Defaults used when the prompt is ambiguous about storage/DB locations
        self.default_db_name = self.config.get("devDb", "saas-uat")
        # File upload path - dedicated config variable for file uploads
        self.default_fs_root = self.config.get(
            "fileUploadPath",
            "/mountpool/public_path/aiqod-uat-fs/uploads",
        )
        self.default_fs_base_url = self.config.get(
            "LIBERTY_FS_BASE_URL",
            "https://uat-fs.aiqod.com:453/",
        )
        # Phase 6 — project-level design system persistence (Master + Overrides).
        # Defaults to a sibling of the file-upload directory so no extra config is needed.
        # Override via config key "designSystemsRoot" for custom deployments.
        self.design_systems_root = Path(
            self.config.get(
                "designSystemsRoot",
                str(Path(self.default_fs_root).parent / "design-systems"),
            )
        )
        # Email credentials: do NOT use config.json. Prefer credentials collection, then prompt.
        # Last enhanced prompt used during IR generation (for persistence in screen_generations v1 metadata)
        self.last_enhanced_prompt: Optional[str] = None

    async def _enhance_user_prompt(
        self, 
        prompt: str, 
        workflow_context: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """
        Enhance user prompt by elaborating requirements without inventing fields.
        Converts simple prompts into detailed, LLM-friendly requirement specifications.
        
        CRITICAL RULES:
        - DO NOT invent fields or data structures
        - DO NOT decide schema or field names
        - DO NOT guess requirements
        - ONLY elaborate what the user explicitly mentioned
        - Extract and clarify intent from user's words
        - Add common UI/UX patterns that match the screen type
        """
        workflow_io_contract = parse_workflow_io_contract_from_prompt(prompt)

        enhance_user_content = _build_prompt_enhancement_user_content(
            prompt, workflow_context
        )
        enhancement_full_prompt = _build_prompt_enhancement_full_prompt(
            prompt, workflow_context
        )
        enhance_cached_content = await _get_ai_processor_cached_content(
            _FLASH_MODEL,
            system_text=_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE,
            cache_key_prefix=_PROMPT_ENHANCE_CACHE_PREFIX,
            display_name="ai-processor-prompt-enhancement",
        )
        enhance_config = GeminiGenerateConfig(temperature=0.3)

        try:
            started_at = datetime.utcnow()
            # FIXED: Added await run_sync
            response = await _ai_processor_sync_llm_call(
                _FLASH_MODEL,
                user_prompt=enhance_user_content,
                full_prompt=enhancement_full_prompt,
                cached_content=enhance_cached_content,
                config=enhance_config,
            )
            finished_at = datetime.utcnow()
            
            # Extract enhanced prompt
            enhanced = ""
            try:
                enhanced = response.text.strip()
            except Exception:
                for chunk in response:
                    enhanced += chunk.text
            
            # Track token usage
            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )
            
            # Record to database
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "unknown",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="prompt-enhancement",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            print(f"\n[PROMPT ENHANCEMENT]")
            # print(f"Original: {prompt}...")
            # print(f"\nEnhanced: {enhanced}...")
            print(f"\nToken usage: {token_usage['total_tokens']}")
            
            if workflow_io_contract is not None:
                detected_io = workflow_io_contract
                print(
                    "\n[IO DETECTION] Skipped LLM — using workflow-builder IO contract "
                    f"({len(detected_io.get('inputParameters', []))} inputs, "
                    f"{len(detected_io.get('outputParameters', []))} outputs)"
                )
                logger.info(
                    "Skipped IO parameter detection LLM; parsed workflow IO contract from prompt"
                )
            else:
                # After getting the enhanced prompt, detect input-output parameters
                detected_io = await self._detect_input_output_parameters(
                    enhanced,
                    project_id,
                    screen_id,
                    version,
                    org_id,
                    subscriber_id,
                    user_id,
                )

            # Append detected parameters to enhanced prompt
            if detected_io:
                io_section = f"""

            INPUT-OUTPUT CONTRACT:
            The following parameters have been identified for workflow orchestration:

            INPUT PARAMETERS (from previous steps):
            {json.dumps(detected_io.get('inputParameters', []), indent=2)}

            OUTPUT PARAMETERS (for next steps):
            {json.dumps(detected_io.get('outputParameters', []), indent=2)}

            These parameters must be correctly implemented in the IR schema's inputParameters and outputParameters arrays."""
                
                enhanced = enhanced + io_section
                
                # Store detected IO for later use
                self._detected_io_params = detected_io

            # Deterministic fidelity: the enhancer may elaborate, but it must never
            # replace the original node spec. Prepend the original so every user-stated
            # fact survives even if the LLM rewrite dropped it.
            original = (prompt or "").strip()
            enhanced_body = (enhanced or "").strip()
            if original and enhanced_body:
                if original not in enhanced_body:
                    enhanced = (
                        "ORIGINAL NODE SPECIFICATION (verbatim — every fact here is "
                        "authoritative and must be implemented; later sections may only add detail):\n"
                        f"{original}\n\n"
                        "ENHANCED / ELABORATED REQUIREMENTS (additive only; do not drop or "
                        "contradict the original):\n"
                        f"{enhanced_body}"
                    )
            return enhanced if enhanced else prompt

        except BillingError:
            raise
        except Exception as e:
            print(f"⚠️ Prompt enhancement failed: {str(e)}, using original prompt")
            
            # Record failed attempt
            if project_id:
                record_llm_call(
                    project_id=project_id,
                    screen_id=screen_id,
                    operation="ir-generation",
                    sub_operation="prompt-enhancement",
                    model="gemini-3-flash-preview",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    status="failed",
                    version=version,
                    metadata={"error": str(e)},
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )

            if workflow_io_contract is not None:
                self._detected_io_params = workflow_io_contract
                logger.info(
                    "Prompt enhancement failed; using workflow IO contract from original prompt"
                )
            
            return prompt

    async def _detect_input_output_parameters(
        self,
        enhanced_prompt: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Detect inputParameters and outputParameters from enhanced prompt.
        Returns a structured contract defining all required inputs and emitted outputs.
        """
        
        io_detect_user_content = _build_io_detection_user_content(enhanced_prompt)
        io_detect_full_prompt = _build_io_detection_full_prompt(enhanced_prompt)
        io_detect_cached_content = await _get_ai_processor_cached_content(
            _FLASH_MODEL,
            system_text=_IO_DETECTION_SYSTEM_TEMPLATE,
            cache_key_prefix=_IO_DETECT_CACHE_PREFIX,
            display_name="ai-processor-io-detection",
        )
        io_detect_config = GeminiGenerateConfig(temperature=0.2)

        try:
            # FIXED: Added await run_sync
            started_at = datetime.utcnow()
            response = await _ai_processor_sync_llm_call(
                _FLASH_MODEL,
                user_prompt=io_detect_user_content,
                full_prompt=io_detect_full_prompt,
                cached_content=io_detect_cached_content,
                config=io_detect_config,
            )
            finished_at = datetime.utcnow()
            
            # Extract response
            detected_params = ""
            try:
                detected_params = response.text.strip()
            except Exception:
                for chunk in response:
                    detected_params += chunk.text
            
            # Track token usage
            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )
            
            # Record to database
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "unknown",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="input-output-detection",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            print(f"\n[INPUT-OUTPUT DETECTION]")
            print(f"Token usage: {token_usage['total_tokens']}")
            
            if detected_params:
                # Parse JSON response
                params_dict = self._parse_ai_response(detected_params)
                # LLM may return a list instead of dict; only use dict with expected keys
                if params_dict and isinstance(params_dict, dict):
                    print(f"Detected {len(params_dict.get('inputParameters', []))} input parameters")
                    print(f"Input parameters: {params_dict.get('inputParameters', [])}")
                    print(f"Detected {len(params_dict.get('outputParameters', []))} output parameters")
                    print(f"Output parameters: {params_dict.get('outputParameters', [])}")
                    return params_dict
                if params_dict is not None and not isinstance(params_dict, dict):
                    print(f"⚠️ Input-output detection: expected JSON object, got {type(params_dict).__name__}")
            
            # Return empty structure if detection fails
            return {"inputParameters": [], "outputParameters": []}

        except BillingError:
            raise
        except Exception as e:
            print(f"⚠️ Input-output detection failed: {str(e)}")
            
            # Record failed attempt
            if project_id:
                record_llm_call(
                    project_id=project_id,
                    screen_id=screen_id,
                    operation="ir-generation",
                    sub_operation="input-output-detection",
                    model="gemini-3-flash-preview",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    status="failed",
                    version=version,
                    metadata={"error": str(e)},
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
            
            return {"inputParameters": [], "outputParameters": []}

    def _detected_io_matches_ir_schema(self, ir_schema: Dict[str, Any]) -> bool:
        """
        True when IR inputParameters/outputParameters already match the IO contract
        detected during prompt enhancement — safe to skip LLM self-verification.
        """
        detected = getattr(self, "_detected_io_params", None)
        if not detected or not isinstance(detected, dict):
            return False
        return io_params_contract_match(
            detected.get("inputParameters", []),
            detected.get("outputParameters", []),
            ir_schema.get("inputParameters", []),
            ir_schema.get("outputParameters", []),
        )
            
    async def _self_verify_ir_schema(
        self, 
        ir_schema: Dict[str, Any], 
        enhanced_prompt: str,
        workflow_context: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Self-verification: LLM checks ONLY inputParameters and outputParameters against requirements.
        
        CRITICAL: This function ONLY verifies workflow parameters, NOT the entire IR schema.
        The LLM reviews only the inputParameters and outputParameters arrays and fixes issues.
        This prevents accidentally modifying or losing other parts of the IR schema.
        """
        # Extract current parameters for verification
        current_input_params = ir_schema.get('inputParameters', [])
        current_output_params = ir_schema.get('outputParameters', [])
        
        # Extract relevant IR schema context for validation
        components_summary = []
        for comp in ir_schema.get('components', []):
            components_summary.append({
                'id': comp.get('id'),
                'type': comp.get('type'),
                'label': comp.get('label', '')
            })
        
        datasources_summary = []
        for ds in ir_schema.get('dataSources', []):
            datasources_summary.append({
                'id': ds.get('id'),
                'type': ds.get('type'),
                'inputModel': ds.get('inputModel'),
                'responseModel': ds.get('responseModel')
            })
        
        datamodels_list = list(ir_schema.get('dataModels', {}).keys())
        
        verify_user_content = _build_io_self_verify_user_content(
            enhanced_prompt=enhanced_prompt,
            workflow_context=workflow_context,
            components_summary=components_summary,
            datasources_summary=datasources_summary,
            datamodels_list=datamodels_list,
            current_input_params=current_input_params,
            current_output_params=current_output_params,
        )
        verification_full_prompt = _build_io_self_verify_full_prompt(
            enhanced_prompt=enhanced_prompt,
            workflow_context=workflow_context,
            components_summary=components_summary,
            datasources_summary=datasources_summary,
            datamodels_list=datamodels_list,
            current_input_params=current_input_params,
            current_output_params=current_output_params,
        )
        verify_cached_content = await _get_ai_processor_cached_content(
            _FLASH_MODEL,
            system_text=_IO_SELF_VERIFY_SYSTEM_TEMPLATE,
            cache_key_prefix=_IO_VERIFY_CACHE_PREFIX,
            display_name="ai-processor-io-self-verify",
        )
        verify_config = GeminiGenerateConfig(temperature=0.1)

        try:
            started_at = datetime.utcnow()
            # FIXED: Added await run_sync
            response = await _ai_processor_sync_llm_call(
                _FLASH_MODEL,
                user_prompt=verify_user_content,
                full_prompt=verification_full_prompt,
                cached_content=verify_cached_content,
                config=verify_config,
            )
            finished_at = datetime.utcnow()
            
            # Extract verified response
            verified_response = ""
            try:
                verified_response = response.text or ""
            except Exception:
                if getattr(response, "candidates", None):
                    for c in response.candidates:
                        parts = getattr(c, "content", None) and getattr(c.content, "parts", None)
                        if parts:
                            verified_response = "\n".join(
                                getattr(p, "text", "") for p in parts if getattr(p, "text", "")
                            )
                            if verified_response:
                                break
            
            # Track tokens
            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )
            print(f"[TOKEN USAGE - Verification] Total: {token_usage['total_tokens']}")
            
            # Record to database
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "unknown",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="ir-validation-fix",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            if verified_response:
                verified_params = self._parse_ai_response(verified_response)
                if verified_params and isinstance(verified_params, dict):
                    # Extract verified parameters
                    verified_input = verified_params.get('inputParameters', current_input_params)
                    verified_output = verified_params.get('outputParameters', current_output_params)
                    
                    # Update only the parameters in the original schema
                    ir_schema['inputParameters'] = verified_input
                    ir_schema['outputParameters'] = verified_output
                    
                    print(f"✓ Self-verification completed")
                    print(f"  - Input parameters: {len(verified_input)}")
                    print(f"  - Output parameters: {len(verified_output)}")
                    print("IR schema after self-verification :",ir_schema)
                    return ir_schema
            
            print("⚠️ Self-verification returned empty, using original schema")
            return ir_schema
            
        except Exception as e:
            print(f"⚠️ Self-verification failed: {str(e)}, using original schema")
            return ir_schema


    def _extract_token_usage(self, response) -> dict:
        """Extract token usage from Gemini response metadata"""
        try:
            usage_metadata = getattr(response, 'usage_metadata', None)
            if usage_metadata:
                return {
                    "prompt_tokens": getattr(usage_metadata, 'prompt_token_count', 0),
                    "candidates_tokens": getattr(usage_metadata, 'candidates_token_count', 0),
                    "total_tokens": getattr(usage_metadata, 'total_token_count', 0)
                }
        except Exception as e:
            print(f"Warning: Could not extract token usage: {str(e)}")
        
        return {
            "prompt_tokens": 0,
            "candidates_tokens": 0,
            "total_tokens": 0
        }

    async def _enrich_datasource_batch(
        self,
        *,
        ir_schema: Dict[str, Any],
        batch: List[Dict[str, Any]],
        original_prompt: str,
        enhanced_prompt: str,
        workflow_context: Optional[Dict[str, Any]],
        available_credentials: Optional[List[Dict[str, Any]]],
        cached_content: Optional[str],
        batch_index: int,
        batch_count: int,
        project_id: Optional[str],
        screen_id: Optional[str],
        version: Optional[str],
        org_id: Optional[str],
        subscriber_id: Optional[str],
        user_id: Optional[str],
    ) -> DataSourceEnrichmentResult:
        all_ds = [ds for ds in (ir_schema.get("dataSources") or []) if isinstance(ds, dict) and ds.get("id")]
        all_ids = {ds["id"]: ds["id"] for ds in all_ds}
        all_ids_lower: Dict[str, List[str]] = defaultdict(list)
        for ds in all_ds:
            all_ids_lower[ds["id"].lower()].append(ds["id"])

        user_prompt = _build_ds_enricher_user_prompt(
            original_prompt=original_prompt,
            enhanced_prompt=enhanced_prompt,
            workflow_context=workflow_context,
            available_credentials=available_credentials,
            ir_schema=ir_schema,
            batch=batch,
            db_name=self.default_db_name,
            fs_root=self.default_fs_root,
            fs_base_url=self.default_fs_base_url,
        )
        full_prompt = f"{_DS_ENRICHER_CACHED_SYSTEM}\n\n{user_prompt}"
        last_error: Optional[str] = None
        last_result: Optional[DataSourceEnrichmentResult] = None

        for attempt in range(1, _DS_ENRICHER_MAX_RETRIES + 1):
            temperature = round(0.2 + (attempt - 1) * 0.1, 2)
            print(
                f"   -> Enricher batch {batch_index}/{batch_count} "
                f"attempt {attempt}/{_DS_ENRICHER_MAX_RETRIES} "
                f"ids={[ds.get('id') for ds in batch]} temp={temperature}"
            )
            config = GeminiGenerateConfig(
                temperature=temperature,
                thinking_level="high" if model_supports_thinking_level(_DS_ENRICHER_MODEL) else None,
                response_mime_type="application/json",
                response_schema=DataSourceEnrichmentResult,
                fallback_profile="planner",
            )
            started_at = datetime.utcnow()
            try:
                response = await _ai_processor_sync_llm_call(
                    _DS_ENRICHER_MODEL,
                    user_prompt=user_prompt,
                    full_prompt=full_prompt,
                    cached_content=cached_content,
                    config=config,
                )
                finished_at = datetime.utcnow()
                token_usage = self._extract_token_usage(response)
                self.token_metrics.add_usage(
                    token_usage["prompt_tokens"],
                    token_usage["candidates_tokens"],
                )
                usage = extract_token_usage_from_response(response)
                record_llm_call(
                    project_id=project_id or "unknown",
                    screen_id=screen_id,
                    operation="ir-generation",
                    sub_operation=f"datasource-spec-enricher-batch-{batch_index}-attempt-{attempt}",
                    model=_DS_ENRICHER_MODEL,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    status="success",
                    version=version,
                    started_at=started_at,
                    finished_at=finished_at,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    metadata={
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "data_source_ids": [ds.get("id") for ds in batch],
                    },
                )
                raw = _ai_processor_response_text(response)
                parsed = getattr(response, "parsed", None)
                result = _parse_ds_enrichment_result(raw, parsed)
                last_result = result
                coverage_errors = _ds_enrichment_coverage_errors(
                    result, batch, all_ids, all_ids_lower
                )
                if coverage_errors:
                    last_error = "; ".join(coverage_errors)
                    logger.warning(
                        "[ir/ds-enricher] Batch %s coverage failed (attempt %s): %s",
                        batch_index,
                        attempt,
                        last_error,
                    )
                    if attempt >= _DS_ENRICHER_MAX_RETRIES:
                        logger.warning(
                            "[ir/ds-enricher] Batch %s applying partial result after "
                            "coverage retries",
                            batch_index,
                        )
                        return result
                    retry_prefix = (
                        f"PREVIOUS ATTEMPT FAILED VALIDATION:\n{last_error}\n\n"
                        "Return a production-grade databaseLogic for EVERY requested id. "
                        "Do not omit dataSources. Do not collapse the spec into a slogan.\n\n"
                    )
                    base_user = _build_ds_enricher_user_prompt(
                        original_prompt=original_prompt,
                        enhanced_prompt=enhanced_prompt,
                        workflow_context=workflow_context,
                        available_credentials=available_credentials,
                        ir_schema=ir_schema,
                        batch=batch,
                        db_name=self.default_db_name,
                        fs_root=self.default_fs_root,
                        fs_base_url=self.default_fs_base_url,
                    )
                    user_prompt = retry_prefix + base_user
                    full_prompt = (
                        retry_prefix
                        + f"{_DS_ENRICHER_CACHED_SYSTEM}\n\n{base_user}"
                    )
                    if attempt < _DS_ENRICHER_MAX_RETRIES:
                        await asyncio.sleep(1.5 * attempt)
                    continue
                return result
            except BillingError:
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "[ir/ds-enricher] Batch %s attempt %s error: %s",
                    batch_index,
                    attempt,
                    exc,
                )
                if project_id:
                    record_llm_call(
                        project_id=project_id,
                        screen_id=screen_id,
                        operation="ir-generation",
                        sub_operation=f"datasource-spec-enricher-batch-{batch_index}-attempt-{attempt}",
                        model=_DS_ENRICHER_MODEL,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        status="failed",
                        version=version,
                        started_at=started_at,
                        finished_at=datetime.utcnow(),
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                        metadata={"error": str(exc)},
                    )
                if attempt < _DS_ENRICHER_MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                    continue
        if last_result is not None:
            logger.warning(
                "[ir/ds-enricher] Batch %s returning last parsed result after errors: %s",
                batch_index,
                last_error,
            )
            return last_result
        raise RuntimeError(
            f"[ir/ds-enricher] Batch {batch_index} failed after "
            f"{_DS_ENRICHER_MAX_RETRIES} attempts. Last error: {last_error}"
        )

    async def _enrich_datasource_specs(
        self,
        ir_schema: Dict[str, Any],
        *,
        original_prompt: str,
        enhanced_prompt: str,
        workflow_context: Optional[Dict[str, Any]] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Step 5.5 — Rewrite dataSource description/databaseLogic into production-grade
        codegen specs. IR structure is frozen. Failures are non-fatal.
        """
        data_sources = ir_schema.get("dataSources")
        if not isinstance(data_sources, list):
            print("   -> No dataSources list — skip enricher.")
            return ir_schema
        targets = [ds for ds in data_sources if isinstance(ds, dict) and ds.get("id")]
        if not targets:
            print("   -> No dataSources with ids — skip enricher.")
            return ir_schema

        cached_content = await _get_ai_processor_cached_content(
            _DS_ENRICHER_MODEL,
            system_text=_DS_ENRICHER_CACHED_SYSTEM,
            cache_key_prefix="ir-ds-enricher",
            display_name="ai-processor-ds-enricher",
        )

        batches = [
            targets[i : i + _DS_ENRICHER_BATCH_SIZE]
            for i in range(0, len(targets), _DS_ENRICHER_BATCH_SIZE)
        ]
        print(
            f"   -> Enriching {len(targets)} dataSource spec(s) "
            f"in {len(batches)} batch(es) of ≤{_DS_ENRICHER_BATCH_SIZE} "
            f"model={_DS_ENRICHER_MODEL}"
        )

        applied_total = 0
        for batch_index, batch in enumerate(batches, start=1):
            allowed_ids = {ds.get("id") for ds in batch}
            try:
                result = await self._enrich_datasource_batch(
                    ir_schema=ir_schema,
                    batch=batch,
                    original_prompt=original_prompt,
                    enhanced_prompt=enhanced_prompt,
                    workflow_context=workflow_context,
                    available_credentials=available_credentials,
                    cached_content=cached_content,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                applied, warnings = _apply_enriched_datasource_specs(
                    data_sources,
                    result.dataSources,
                    allowed_ids=allowed_ids,
                )
                applied_total += applied
                for warning in warnings:
                    logger.warning("[ir/ds-enricher] %s", warning)
                    print(f"   -> {warning}")
                print(
                    f"   -> Batch {batch_index}/{len(batches)} applied "
                    f"{applied}/{len(batch)} dataSource spec(s)"
                )
            except BillingError:
                raise
            except Exception as exc:
                msg = (
                    f"batch {batch_index} failed ({exc}); keeping original specs for "
                    f"{sorted(allowed_ids)}"
                )
                logger.warning("[ir/ds-enricher] %s", msg)
                print(f"   -> {msg}")

        print(
            f"   -> DataSource enricher done: applied {applied_total}/{len(targets)} spec(s)"
        )
        return ir_schema

    async def process_prompt(
        self,
        prompt: str,
        image_data: Optional[bytes] = None,       
        figma_json_str: Optional[str] = None,     
        screen_type: Optional[str] = None,
        theme_preferences: Optional[Dict[str, Any]] = None,
        workflow_context: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process natural language prompt (and optional Figma inputs) to generate UI schema.

        Pipeline: enhance prompt → generate IR → design pass → IO self-verify →
        validate/repair → dataSource spec enricher (description + databaseLogic only).
        """

        # STEP 1: ENHANCE USER PROMPT (Conditional Logic)
        print("\n[STEP 1/5] Enhancing user prompt...")
        
        if figma_json_str:
            print("   -> Skipping enhancement for Strict Figma Compiler Mode.")
            enhanced_prompt = prompt
        else:
            enhanced_prompt = await self._enhance_user_prompt(
                prompt,
                workflow_context,
                project_id,
                screen_id,
                version,
                org_id,
                subscriber_id,
                user_id,
            )
            
        self.last_enhanced_prompt = enhanced_prompt
        get_tracker().set_request_prompts(enhanced_prompt=enhanced_prompt)

        if not theme_preferences and not figma_json_str:
            # Phase 3: vendored ui-ux-pro-max design engine (replaces _extract_theme_from_prompt).
            # Explicit hex / "match our brand" on the user prompt short-circuits the lookup.
            # Skip for Figma compiler mode — design comes from the uploaded Figma, not BM25.
            # Phase 6: pass project_id + storage_root for persistence; honour
            # workflow_context["_design_override_requested"] to trigger a page-level override.
            _design_force_refresh = bool(
                workflow_context and workflow_context.get("_design_override_requested")
            )
            theme_preferences = resolve_theme_preferences(
                user_prompt=prompt,
                enhanced_prompt=enhanced_prompt,
                project_name=project_id or screen_id or "Screen",
                direction_enum=DESIGN_DIRECTION_ENUM,
                project_id=project_id or None,
                storage_root=self.design_systems_root if project_id else None,
                screen_id=screen_id or None,
                force_refresh=_design_force_refresh,
            )

            # When the design engine ran (BM25) and the direction it returned is
            # unreliable (generic reasoning fallback OR engine ran at all), override
            # the direction using a cheap LLM classification call.  The LLM already
            # knows what "sketch-style", "insurance", "fintech landing" etc. look like;
            # BM25 over product-category CSVs does not.
            _tp_source = theme_preferences.get("source")
            if _tp_source == "design_engine":
                _llm_direction = await _classify_direction_via_llm(prompt, DESIGN_DIRECTION_ENUM)
                if _llm_direction:
                    _old_dir = theme_preferences.get("direction")
                    theme_preferences["direction"] = _llm_direction
                    theme_preferences["_direction_source"] = "llm_classifier"
                    if _old_dir != _llm_direction:
                        print(
                            f"   -> Direction overridden by LLM classifier: "
                            f"{_old_dir!r} → {_llm_direction!r}"
                        )

            print(
                f"   -> Theme preferences source={theme_preferences.get('source')} "
                f"direction={theme_preferences.get('direction')} "
                f"generic_fallback={theme_preferences.get('usedGenericReasoningFallback')}"
            )
        if not screen_type:
            screen_type = self._determine_screen_type(enhanced_prompt)

        # STEP 2: CREATE GENERATION PROMPT
        print("\n[STEP 2/5] Creating IR schema generation prompt...")
        available_credentials: List[Dict[str, Any]] = []
        if project_id:
            cred_svc = CredentialService.optional()
            if cred_svc:
                try:
                    available_credentials = await cred_svc.list_credentials(project_id)
                except Exception as e:
                    logger.warning("Failed to list credentials for IR generation: %s", e)
        system_prompt = self._create_system_prompt(available_credentials=available_credentials)
        user_prompt = self._create_user_prompt(
            enhanced_prompt, screen_type, theme_preferences, workflow_context,
            available_credentials=available_credentials,
        )

        prompt_extras = ""
        ir_gen_mode_suffix = ""
        
        if figma_json_str:
            # --- COMPILER MODE ---
            ir_gen_mode_suffix = _IR_GENERATION_FIGMA_COMPILER_SUFFIX
            system_prompt += ir_gen_mode_suffix
            prompt_extras = f"\n### FIGMA JSON DATA:\n```json\n{figma_json_str}\n```"
            
        elif image_data:
            ir_gen_mode_suffix = _IR_GENERATION_VISUAL_REPLICA_SUFFIX
            system_prompt += ir_gen_mode_suffix

        full_prompt = f"{system_prompt}\n\n{user_prompt}\n{prompt_extras}"
        ir_gen_user_prompt = (
            f"{user_prompt}\n{prompt_extras}" if prompt_extras else user_prompt
        )

        # Context cache for static IR system rules (skip multimodal — image + cache untested).
        ir_gen_cached_content: Optional[str] = None
        if not image_data:
            cached_system_text = self._create_system_prompt(
                available_credentials=available_credentials,
                for_context_cache=True,
                mode_suffix=ir_gen_mode_suffix,
            )
            config_fp = _ir_generation_config_fingerprint(
                self.default_db_name,
                self.default_fs_root,
                self.default_fs_base_url,
            )
            cache_key_prefix = (
                f"ir-gen-figma-compiler_{config_fp}"
                if figma_json_str
                else f"ir-gen-base_{config_fp}"
            )
            ir_gen_cached_content = await _get_ai_processor_cached_content(
                _IR_GENERATION_MODEL,
                system_text=cached_system_text,
                cache_key_prefix=cache_key_prefix,
                display_name=(
                    "ai-processor-ir-gen-figma-compiler"
                    if figma_json_str
                    else "ai-processor-ir-gen-base"
                ),
            )

        ir_gen_config = GeminiGenerateConfig(
            temperature=0.1 if figma_json_str else 0.2,
        )

        try:
            # STEP 3: GENERATE IR SCHEMA (WITH RETRY LOGIC)
            print("\n[STEP 3/5] Generating IR schema...")
            
            max_retries = 3
            ir_schema = None
            last_error = None
            
            for attempt in range(1, max_retries + 1):
                try:
                    print(f"Attempt {attempt}/{max_retries}...")
                    
                    started_at = datetime.utcnow()
                    
                    multimodal_contents = None
                    if image_data:
                        try:
                            image = Image.open(io.BytesIO(image_data))
                            multimodal_contents = [full_prompt, image]
                            print("Attached Figma Image to prompt")
                        except Exception as img_err:
                            print(f"Failed to attach Figma image: {img_err}")

                    response = await _ai_processor_sync_llm_call(
                        _IR_GENERATION_MODEL,
                        user_prompt=ir_gen_user_prompt,
                        full_prompt=full_prompt,
                        cached_content=ir_gen_cached_content,
                        config=ir_gen_config,
                        multimodal_contents=multimodal_contents,
                    )
                    finished_at = datetime.utcnow()

                    token_usage = self._extract_token_usage(response)
                    self.token_metrics.add_usage(
                        token_usage["prompt_tokens"],
                        token_usage["candidates_tokens"],
                    )
                    print(
                        f"[TOKEN USAGE - IR Generation ({_IR_GENERATION_MODEL})] "
                        f"Prompt: {token_usage['prompt_tokens']}, "
                        f"Response: {token_usage['candidates_tokens']}, "
                        f"Total: {token_usage['total_tokens']}"
                    )
                    
                    # Record to database
                    usage = extract_token_usage_from_response(response)
                    record_llm_call(
                        project_id=project_id or "unknown",
                        screen_id=screen_id,
                        operation="ir-generation",
                        sub_operation=f"generate-ir-schema-attempt-{attempt}",
                        model=_IR_GENERATION_MODEL,
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        total_tokens=usage["total_tokens"],
                        status="success",
                        version=version,
                        started_at=started_at,
                        finished_at=finished_at,
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                    )

                    ai_response = ""
                    try:
                        ai_response = response.text or ""
                    except Exception:
                        if getattr(response, "candidates", None):
                            for c in response.candidates:
                                parts = (getattr(c, "content", None) and getattr(c.content, "parts", None))
                                if parts:
                                    ai_response = "\n".join(
                                        getattr(p, "text", "")
                                        for p in parts
                                        if getattr(p, "text", "")
                                    )
                                    if ai_response:
                                        break

                    # Debug logging
                    print(f"Raw AI Response length: {len(ai_response)}")
                    # print(f"Raw AI Response preview: {ai_response}")

                    # Try to parse the response
                    if ai_response:
                        ir_schema = self._parse_ai_response(ai_response)
                    
                    if ir_schema:
                        print(f"✓ Successfully parsed IR schema on attempt {attempt}")
                        break
                    else:
                        error_msg = "Failed to parse AI response into valid JSON"
                        print(f"✗ {error_msg} (attempt {attempt}/{max_retries})")
                        last_error = error_msg
                        
                        if attempt < max_retries:
                            print(f"Retrying IR schema generation...")
                            continue

                except BillingError:
                    raise
                except Exception as e:
                    error_msg = f"Error during IR schema generation: {str(e)}"
                    print(f"✗ {error_msg} (attempt {attempt}/{max_retries})")
                    last_error = error_msg
                    
                    if attempt < max_retries:
                        print(f"Retrying IR schema generation...")
                        continue
            
            # If all retries failed, raise the error
            if not ir_schema:
                final_error = f"Failed to generate valid IR schema after {max_retries} attempts. Last error: {last_error}"
                print(f"✗ {final_error}")
                raise Exception(final_error)

            # STEP 3.5: DESIGN PASS — apply resolved design tokens to UI fields only.
            # Runs after structural IR generation; skipped for Figma compiler mode
            # (design comes from the Figma image) and when no design tokens are available.
            if not figma_json_str and self._has_design_tokens(theme_preferences):
                ir_schema = await self._run_design_pass(
                    ir_schema,
                    theme_preferences,
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
            else:
                if figma_json_str:
                    print("\n[STEP 3.5] Design pass skipped (Figma compiler mode).")
                else:
                    print("\n[STEP 3.5] Design pass skipped (no design tokens available).")

            if not figma_json_str:
                if self._detected_io_matches_ir_schema(ir_schema):
                    print(
                        "\n[STEP 4/5] Self-verification skipped — "
                        "IR inputParameters/outputParameters match detected IO contract."
                    )
                    logger.info(
                        "Skipping IO self-verify: detected IO matches IR "
                        "inputParameters/outputParameters"
                    )
                else:
                    print("\n[STEP 4/5] Self-verifying IR schema...")
                    ir_schema = await self._self_verify_ir_schema(
                        ir_schema,
                        enhanced_prompt,
                        workflow_context,
                        project_id,
                        screen_id,
                        version,
                        org_id,
                        subscriber_id,
                        user_id,
                    )

            print("\n[STEP 5/5] Validating IR schema...")
            ir_schema, was_valid, repairs = await self.validate_and_repair_ir(
                ir_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
            )

            if not was_valid:
                print(f"[REPAIR] Applied {len(repairs)} repairs to IR schema")

            # STEP 5.5: DATASOURCE SPEC ENRICHER
            # Structure is frozen (ids/type/config/models/operation). Only rewrite
            # description + databaseLogic so backend_generator gets a production spec.
            print("\n[STEP 5.5] Enriching dataSource specs for backend codegen...")
            ir_schema = await self._enrich_datasource_specs(
                ir_schema,
                original_prompt=prompt,
                enhanced_prompt=enhanced_prompt,
                workflow_context=workflow_context,
                available_credentials=available_credentials,
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

            # Email credentials are NOT injected at IR level.
            # Backend code reads them directly from request_meta['credentials'].
            return ir_schema

        except Exception as e:
            print(f"✗ AI processing error: {str(e)}")
            raise

    async def generate_ir_schema(
        self,
        prompt: str,
        screen_type: Optional[str] = None,
        image_data: Optional[bytes] = None,
        figma_json_str: Optional[str] = None,
        theme_preferences: Optional[Dict[str, Any]] = None,
        workflow_context: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Backwards-compatible wrapper for legacy callers that still request a
        "raw IR schema" object. Delegates to process_prompt().
        """
        return await self.process_prompt(
            prompt=prompt,
            image_data=image_data,
            figma_json_str=figma_json_str,
            screen_type=screen_type,
            theme_preferences=theme_preferences,
            workflow_context=workflow_context,
            project_id=project_id,
            screen_id=screen_id,
            version=version,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
    
    def _determine_screen_type(self, prompt: str) -> str:
        """Determine screen type from prompt content"""
        prompt_lower = prompt.lower()
        
        if any(word in prompt_lower for word in ['form', 'input', 'field', 'submit', 'register', 'login']):
            return 'form'
        elif any(word in prompt_lower for word in ['table', 'list', 'data', 'employee', 'user', 'record']):
            return 'table'
        elif any(word in prompt_lower for word in ['dashboard', 'chart', 'kpi', 'metric', 'analytics']):
            return 'dashboard'
        else:
            return 'mixed'
    
    async def _extract_email_credentials_from_prompt(
        self, 
        prompt: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Deprecated: email credentials are no longer taken from the prompt.
        Users must configure email credentials in the project credentials collection.
        This helper is kept only for backward compatibility and always returns None.
        """
        return None
    
    async def _extract_email_credentials_with_llm(
        self, 
        prompt: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Use LLM to extract email credentials from natural language prompt"""
        extraction_prompt = f"""Extract email credentials from the following user prompt. 
The user may provide email credentials in natural language. Extract:
- sender email address (look for phrases like "my email", "use email", "email address", "from email")
- email password (look for phrases like "password", "email password", "smtp password")
- SMTP server (look for "smtp server", "smtp host")
- SMTP port (look for "smtp port")

User prompt:
"{prompt}"

Return ONLY a JSON object with the extracted values. Use null for fields not found. Format:
{{
  "sender": "email@example.com" or null,
  "password": "password123" or null,
  "smtp_server": "smtp.gmail.com" or null,
  "smtp_port": 587 or null
}}

If no email credentials are mentioned in the prompt, return {{"sender": null, "password": null, "smtp_server": null, "smtp_port": null}}.
Return ONLY the JSON, no explanations or markdown."""

        try:
            started_at = datetime.utcnow()
            response = await run_sync(
                sync_generate,
                "gemini-3.5-flash-lite",
                extraction_prompt,
                config=GeminiGenerateConfig(temperature=0.1),
            )
            finished_at = datetime.utcnow()
            
            # Track token usage
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="email-credential-extraction",
                model="gemini-3.5-flash-lite",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            # Extract response text
            ai_response = ""
            try:
                ai_response = response.text or ""
            except Exception:
                if getattr(response, "candidates", None):
                    for c in response.candidates:
                        parts = (
                            getattr(c, "content", None)
                            and getattr(c.content, "parts", None)
                        )
                        if parts:
                            ai_response = "\n".join(
                                getattr(p, "text", "")
                                for p in parts
                                if getattr(p, "text", "")
                            )
                            if ai_response:
                                break
            
            if ai_response:
                # Parse JSON response
                ai_response = ai_response.strip()
                # Remove markdown code blocks if present
                if ai_response.startswith('```'):
                    first_newline = ai_response.find('\n')
                    if first_newline != -1:
                        ai_response = ai_response[first_newline + 1:]
                if ai_response.endswith('```'):
                    last_marker = ai_response.rfind('```')
                    if last_marker != -1:
                        ai_response = ai_response[:last_marker]
                ai_response = ai_response.strip()
                
                # Try to extract JSON object
                json_match = re.search(r'\{.*\}', ai_response, re.DOTALL)
                if json_match:
                    json_str = json_match.group()
                    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)  # Remove trailing commas
                    parsed = json.loads(json_str)
                    
                    # Filter out null values and convert to proper types
                    credentials = {}
                    if parsed.get('sender') and parsed['sender'] != 'null':
                        credentials['sender'] = str(parsed['sender']).strip()
                    if parsed.get('password') and parsed['password'] != 'null':
                        credentials['password'] = str(parsed['password']).strip()
                    if parsed.get('smtp_server') and parsed['smtp_server'] != 'null':
                        credentials['smtp_server'] = str(parsed['smtp_server']).strip()
                    if parsed.get('smtp_port') and parsed['smtp_port'] != 'null':
                        try:
                            credentials['smtp_port'] = int(parsed['smtp_port'])
                        except (ValueError, TypeError):
                            pass
                    
                    if credentials:
                        print(f"[EMAIL] LLM extracted credentials: {list(credentials.keys())}")
                        return credentials
        except Exception as e:
            print(f"[EMAIL] LLM extraction failed: {str(e)}, falling back to regex")
        
        return None
    
    def _extract_email_credentials_with_regex(self, prompt: str) -> Dict[str, Any]:
        """Fallback regex-based extraction of email credentials"""
        prompt_lower = prompt.lower()
        credentials = {}
        
        # Sender email patterns (order matters - more specific first)
        sender_patterns = [
            r'use\s+(?:my\s+)?email\s+(?:address\s+)?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            r'(?:my\s+)?email\s+(?:address\s+)?(?:is|will be|:)\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            r'email\s+(?:address\s+)?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            r'sender\s*email[:\s]+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            r'from\s*email[:\s]+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            r'email\s*from[:\s]+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        ]
        for pattern in sender_patterns:
            match = re.search(pattern, prompt_lower, re.IGNORECASE)
            if match:
                credentials['sender'] = match.group(1)
                break
        
        # Email password patterns (order matters - more specific first)
        password_patterns = [
            r'(?:email\s+)?password\s+(?:is|will be|:)\s*([^\s,\.]+)',
            r'password\s+(?:is|will be|:)\s*([^\s,\.]+)',
            r'smtp\s*password[:\s]+([^\s,\.]+)',
            # Also catch patterns like "password will be 123abc" after email mention
            r'(?:and\s+)?password\s+(?:is|will be|:)\s*([^\s,\.]+)',
        ]
        for pattern in password_patterns:
            match = re.search(pattern, prompt_lower, re.IGNORECASE)
            if match:
                credentials['password'] = match.group(1)
                break
        
        # SMTP server patterns
        smtp_server_patterns = [
            r'smtp\s*server[:\s]+([a-zA-Z0-9.-]+)',
            r'smtp\s*host[:\s]+([a-zA-Z0-9.-]+)',
        ]
        for pattern in smtp_server_patterns:
            match = re.search(pattern, prompt_lower, re.IGNORECASE)
            if match:
                credentials['smtp_server'] = match.group(1)
                break
        
        # SMTP port patterns
        smtp_port_patterns = [
            r'smtp\s*port[:\s]+(\d+)',
        ]
        for pattern in smtp_port_patterns:
            match = re.search(pattern, prompt_lower, re.IGNORECASE)
            if match:
                credentials['smtp_port'] = int(match.group(1))
                break
        
        if credentials:
            print(f"[EMAIL] Regex extracted credentials: {list(credentials.keys())}")
        
        return credentials
    
    def _has_email_requirement(self, prompt: str, ir_schema: Dict[str, Any]) -> bool:
        """Check if the prompt or IR schema indicates email sending functionality"""
        prompt_lower = prompt.lower()
        # More specific keywords to avoid false positives (e.g., "email field" vs "send email")
        email_sending_keywords = [
            'send email', 'send an email', 'email notification', 'notify via email', 
            'notify by email', 'email alert', 'email reminder', 'email report',
            'mail to', 'send mail', 'email message', 'smtp'
        ]
        
        # Check prompt for email sending intent
        if any(keyword in prompt_lower for keyword in email_sending_keywords):
            return True
        
        # Check dataSources for email-related descriptions
        data_sources = ir_schema.get('dataSources', [])
        for ds in data_sources:
            if not isinstance(ds, dict):
                continue
            description = (ds.get('description') or '').lower()
            database_logic = (ds.get('databaseLogic') or '').lower()
            # Check for email sending keywords in dataSource
            if any(keyword in description or keyword in database_logic for keyword in email_sending_keywords):
                return True
            # Also check for SMTP-related terms
            if 'smtp' in description or 'smtp' in database_logic:
                return True
        
        return False
    
    def _create_system_prompt(
        self,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
        *,
        for_context_cache: bool = False,
        mode_suffix: str = "",
    ) -> str:
        """Create system prompt with workflow-aware full-stack IR schema."""
        db_name = self.default_db_name
        fs_root = self.default_fs_root
        fs_base_url = self.default_fs_base_url
        
        cred_section = ""
        if for_context_cache:
            cred_section = _IR_GENERATION_CREDENTIALS_DEFER_TO_USER_NOTE
        elif available_credentials:
            cred_list = [{"name": c.get("name"), "type": c.get("type"), "service": c.get("service")} for c in available_credentials]
            cred_section = f"""
# AVAILABLE CREDENTIALS (CRITICAL - READ FIRST)
The project has these credentials already configured. You MUST use them when generating dataSources:
{json.dumps(cred_list, indent=2)}

CREDENTIAL USAGE RULES:
1. **USE EXISTING CREDENTIALS**: For any dataSource that needs auth for a service listed above:
   - Check if a credential exists for that service (match by service name or credential name)
   - If yes: Reference it by exact name in dataSource config
   - Example: If "gemini_api_key" exists and you need Gemini API → use config.credentials: ["gemini_api_key"]

2. **MISSING CREDENTIALS**: If you need a credential that is NOT in the list above:
   - Add a descriptive credential name to config (e.g., "stripe_secret_key", "github_token")
   - In databaseLogic, note: "Requires credential 'stripe_secret_key' to be configured in project credentials"
   - The backend will check if it exists; if not, it will return an error asking the user to configure it

3. **CREDENTIAL MATCHING EXAMPLES**:
   - User has "gemini_api_key" (service: "Gemini") → For Gemini classification, use: config.credentials: ["gemini_api_key"]
   - User has "github_token" (service: "GitHub") → For GitHub API, use: config.credentials: ["github_token"]
   - User has "stripe_secret_key" (service: "Stripe") → For Stripe payments, use: config.credentials: ["stripe_secret_key"]
   - User needs SendGrid but has no credential → Use config.credentials: ["sendgrid_api_key"] and note in databaseLogic it needs configuration

4. **NEVER** invent credential values or put secret strings in the IR—only reference credential **names**.
"""
        else:
            cred_section = """
# AVAILABLE CREDENTIALS
No project credentials are currently configured. For dataSources that need external API auth:
- Use descriptive credential names in config.credentials or config.credential_name (e.g., "stripe_secret_key", "github_token", "gemini_api_key")
- In databaseLogic, note which credentials are required (e.g., "Requires credential 'gemini_api_key' to be configured")
- The backend will check at runtime; if missing, it will return an error asking the user to configure the credential
- Never put secret values in the IR—only reference credential names
- Do this only when the user mentions external API/service in the prompt or when you are sure that the service needs authentication.
"""
        print(f"cred_section: {cred_section}")
        prompt = _IR_GENERATION_SYSTEM_TEMPLATE
        
        # Replace placeholders with actual config values
        prompt = prompt.replace("{available_credentials_section}", cred_section)
        prompt = prompt.replace("{db_name}", db_name)
        prompt = prompt.replace("{fs_root}", fs_root)
        prompt = prompt.replace("{fs_base_url}", fs_base_url)
        
        return prompt + (mode_suffix or "")
    
    def _create_user_prompt(
        self,
        prompt: str,
        screen_type: str,
        theme_preferences: Dict[str, Any],
        workflow_context: Optional[Dict[str, Any]] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Create user prompt with optional workflow context."""
        workflow_section = ""
        if workflow_context:
            workflow_section = f"""
WORKFLOW CONTEXT:
- Step ID: {workflow_context.get('step_id', 'unknown')}
- Previous Steps: {json.dumps(workflow_context.get('previous_steps', []))}
- Available Inputs: {json.dumps(workflow_context.get('available_outputs', {}))}
- Next Steps: {json.dumps(workflow_context.get('next_steps', []))}

IMPORTANT:
- If this step needs data from previous steps, define inputParameters
- If this step produces data for next steps, define outputParameters
- Bind all inputs to components/dataSources where they're used
"""
        # Add detected input/output parameters section if available
        io_section = ""
        if hasattr(self, "_detected_io_params") and self._detected_io_params:
            io_params = self._detected_io_params
            io_section = f"""

DETECTED INPUT-OUTPUT PARAMETERS:
The following parameters have been automatically detected from the user's requirements:

Input Parameters (required from previous steps):
{json.dumps(io_params.get('inputParameters', []), indent=2)}

Output Parameters (to be emitted for next steps):
{json.dumps(io_params.get('outputParameters', []), indent=2)}

YOU MUST include these parameters in your IR schema's inputParameters and outputParameters arrays with the exact structure provided above.
Graph-declared outputParameters with required:true MUST appear as required:true fields on the **appropriate** dataSource response model (the submit/action/write dataSource that emits them — NOT every find/list query responseModel). Do not weaken them to required:false. UI action fields (user_action, submitted, confirmed, etc.) must NOT be placed on find/find_one responseModels.
"""

        cred_user_section = ""
        if available_credentials:
            cred_list = [{"name": c.get("name"), "type": c.get("type"), "service": c.get("service")} for c in available_credentials]
            cred_user_section = f"""
AVAILABLE CREDENTIALS (CRITICAL - Use these when generating dataSources):
{json.dumps(cred_list, indent=2)}

IMPORTANT INSTRUCTIONS:
1. For any external API/service mentioned in the prompt, FIRST check if a matching credential exists above
2. Match by service name (e.g., service="Gemini" for Gemini APIs, service="GitHub" for GitHub APIs)
3. If a credential exists for that service, use its exact name in the dataSource config.credentials
4. If NO credential exists but the service needs auth, reference a descriptive name (e.g., "stripe_secret_key") and note in databaseLogic that it needs configuration
5. Examples:
   - Prompt mentions "classify with Gemini" + credential "gemini_api_key" exists → use config.credentials: ["gemini_api_key"]
   - Prompt mentions "fetch GitHub repos" + credential "github_token" exists → use config.credentials: ["github_token"]
   - Prompt mentions "Stripe payment" + NO Stripe credential → use config.credentials: ["stripe_secret_key"] and note: "Requires 'stripe_secret_key' to be configured"
"""
        else:
            cred_user_section = """
AVAILABLE CREDENTIALS: None currently configured.
- For any dataSource needing external API auth, use descriptive credential names (e.g., "gemini_api_key", "github_token", "stripe_secret_key")
- In databaseLogic, note which credentials are required so the user knows to configure them
"""

        anti_patterns = []
        if isinstance(theme_preferences, dict):
            raw_anti = theme_preferences.get("antiPatterns") or theme_preferences.get("anti_patterns")
            if isinstance(raw_anti, list):
                anti_patterns = [a for a in raw_anti if isinstance(a, str) and a.strip()]
            elif isinstance(raw_anti, str) and raw_anti.strip():
                anti_patterns = [p.strip() for p in raw_anti.replace("+", ",").split(",") if p.strip()]

        anti_patterns_block = ""
        if anti_patterns:
            anti_patterns_block = (
                "\nAnti-patterns (HARD CONSTRAINTS — do not introduce these in theme, layout, or components):\n"
                + "\n".join(f"- {item}" for item in anti_patterns)
                + "\n"
            )
        else:
            anti_patterns_block = (
                "\nAnti-patterns: Derive a short top-level `antiPatterns` list for this product/style "
                "(concrete avoid items). Do not leave the array empty when a clear avoid-list is obvious.\n"
            )

        # Strip implementation-internal keys before sending to the LLM.
        # Keys like source/productCategory/pattern/usedGenericReasoningFallback are metadata
        # for logging only; they must not reach the LLM or the generated IR's theme object.
        _THEME_PREFS_LLM_KEYS = {
            "direction", "primary", "secondary", "accent", "background", "text",
            "typography", "effects", "antiPatterns", "googleFontsUrl",
        }
        theme_prefs_for_llm = (
            {k: v for k, v in theme_preferences.items() if k in _THEME_PREFS_LLM_KEYS}
            if isinstance(theme_preferences, dict)
            else (theme_preferences or {})
        )

        return f"""
Convert this into the Full-Stack IR schema with WORKFLOW SUPPORT (screen, theme, layout, components, dataModels, dataSources, inputParameters, outputParameters, state, antiPatterns):

Prompt: "{prompt}"
Screen Type: {screen_type}
Theme preferences (apply into theme.direction / theme color roles / theme.typography / theme.effects when present): {json.dumps(theme_prefs_for_llm)}
{anti_patterns_block}
{workflow_section}
{io_section}
{cred_user_section}

Focus on:
1. All UI components needed with actions/triggers (onLoad, onChange, onSubmit, etc.)
2. Proper component hierarchy, props, style, responsive overrides
3. Complete dataModels definitions for every request/response
4. Accurate dataSources with dynamic vs external detection, operation/databaseLogic, config details
5. State bindings tying workflowContext, derived data, and components together
6. **inputParameters**: Define expected inputs from previous steps with bindTo targets
7. **outputParameters**: Define outputs for next steps with extraction details
8. Theme: set `direction` (enum), `typography.display` + `typography.body`, `effects`, color roles; emit top-level `antiPatterns`
9. layout: set max_width and columns according to screen.type rules above. Desktop-first —
   pages must fill the full viewport width on a desktop browser. Examples like a form screen 
   case where a narrow centered max_width is appropriate, since its intentional.
10. Theme colors: do not invent a generic blue/purple palette; use theme preferences / direction tokens.

Return valid JSON only.
"""
    
    def _parse_ai_response(self, response: str) -> Dict[str, Any]:
        """Parse AI response and extract JSON with robust cleaning"""
        try:
            if not response:
                return None

            response = response.strip()
            
            # Remove code block markers
            if response.startswith('```'):
                # Find first newline after opening ```
                first_newline = response.find('\n')
                if first_newline != -1:
                    response = response[first_newline + 1:]
            
            if response.endswith('```'):
                # Find last ``` and remove it
                last_marker = response.rfind('```')
                if last_marker != -1:
                    response = response[:last_marker]
            
            response = response.strip()
            
            # Try to parse directly
            try:
                return json.loads(response)
            except json.JSONDecodeError:
                # Enhanced JSON extraction with better bracket matching
                # Find the outermost JSON object by counting braces
                stack = []
                start_idx = -1
                end_idx = -1
                
                for i, char in enumerate(response):
                    if char == '{':
                        if not stack:
                            start_idx = i
                        stack.append(char)
                    elif char == '}':
                        if stack and stack[-1] == '{':
                            stack.pop()
                            if not stack and start_idx != -1:
                                end_idx = i + 1
                                break
                
                if start_idx != -1 and end_idx != -1:
                    json_str = response[start_idx:end_idx]
                    
                    # Clean common JSON issues
                    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)  # Remove trailing commas
                    json_str = re.sub(r'//.*?$', '', json_str, flags=re.MULTILINE)  # Remove // comments
                    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)  # Remove /* */ comments
                    # Fix invalid backslash escapes (e.g. \z, \x) that cause "Invalid \escape"
                    json_str = re.sub(
                        r'\\(?!(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))(.)',
                        r'\1',
                        json_str,
                    )
                    
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError as e:
                        print(f"JSON Parse Error after cleaning: {str(e)}")
                        print(f"Problematic JSON (first 500 chars): {json_str[:500]}...")
                        # Last resort: try to fix common issues
                        json_str = self._fix_common_json_issues(json_str)
                        try:
                            return json.loads(json_str)
                        except:
                            return None
                else:
                    print("No valid JSON object found in response (bracket matching failed)")
                    return None
                        
        except json.JSONDecodeError as e:
            print(f"JSON Parse Error: {str(e)}")
            # Safe print of problematic response
            preview = response[:500] if response else ""
            print(f"Problematic response start: {preview}...")
            return None

    def _fix_common_json_issues(self, json_str: str) -> str:
        """Fix common JSON formatting issues including invalid escape sequences."""
        # Fix invalid backslash escapes (JSON only allows \" \\ \/ \b \f \n \r \t \uXXXX)
        # Replace \X with X when X is not a valid escape character (fixes "Invalid \\escape" errors)
        json_str = re.sub(
            r'\\(?!(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))(.)',
            r'\1',
            json_str,
        )

        # Fix unescaped newlines in strings
        json_str = re.sub(r'(?<!\\)(\n)(?=(?:[^"]*"[^"]*")*[^"]*$)', r'\\n', json_str)
        
        # Fix single quotes to double quotes (be careful with this)
        # Only replace single quotes that are clearly used as double quotes
        json_str = re.sub(r"(?<=[\{\[,:])\s*'([^']*?)'\s*(?=[\}\],:])", r'"\1"', json_str)
        
        # Remove any trailing commas again (more aggressively)
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        return json_str
    

    async def _json_validator(
        self, 
        json_str: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """Validate JSON string"""
        
        systemPrompt = """You are a JSON validator and fixer. Your task is to:
            1. Validate if the provided string is valid JSON
            2. If invalid, fix any syntax errors and return valid JSON
            3. Always return only the fixed JSON, no explanations

            If the JSON is already valid, return it as-is."""
        
        userPrompt = f"""
        JSON to validate:
        {json_str}
        """
        
        fullPrompt = f"{systemPrompt}\n\n{userPrompt}"
        started_at = datetime.utcnow()
        response = await run_sync(
            sync_generate,
            "gemini-3-flash-preview",
            fullPrompt,
            config=GeminiGenerateConfig(temperature=0.2),
        )
        finished_at = datetime.utcnow()
        
        # Track token usage
        usage = extract_token_usage_from_response(response)
        record_llm_call(
            project_id=project_id or "system",
            screen_id=screen_id,
            operation="ir-generation",
            sub_operation="json-validation",
            model="gemini-3-flash-preview",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            status="success",
            version=version,
            started_at=started_at,
            finished_at=finished_at,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        
        return response.text

    # ------------------------------------------------------------------
    # Phase 5 — Design pass (step 2 of initial generation)
    # ------------------------------------------------------------------

    @staticmethod
    def _has_design_tokens(theme_preferences: Optional[Dict[str, Any]]) -> bool:
        """True when theme_preferences carries at least one IR-visible design token."""
        if not isinstance(theme_preferences, dict):
            return False
        _TOKEN_KEYS = {
            "direction", "primary", "secondary", "accent",
            "background", "text", "typography", "effects", "antiPatterns",
        }
        return any(theme_preferences.get(k) for k in _TOKEN_KEYS)

    def _build_design_pass_user_prompt(
        self,
        ir_schema: Dict[str, Any],
        theme_preferences: Dict[str, Any],
    ) -> str:
        """
        Build the user-side prompt for the design pass.

        The design pass asks the LLM to apply resolved design tokens to the
        UI-only slice of the IR (theme, layout, components, antiPatterns)
        without touching the structural contract (dataModels, dataSources,
        actions, state.bindings).
        """
        ui_schema = self._ir_schema_for_ui_update(ir_schema)
        schema_str = json.dumps(ui_schema, indent=2)

        token_lines: List[str] = []
        if theme_preferences.get("direction"):
            token_lines.append(f"- direction: {theme_preferences['direction']}")
        for key in ("primary", "secondary", "accent", "background", "text"):
            if theme_preferences.get(key):
                token_lines.append(f"- {key}: {theme_preferences[key]}")
        typo = theme_preferences.get("typography")
        if isinstance(typo, dict):
            if typo.get("display"):
                token_lines.append(f"- typography.display: {typo['display']}")
            if typo.get("body"):
                token_lines.append(f"- typography.body: {typo['body']}")
        effects = theme_preferences.get("effects")
        if effects:
            token_lines.append(f"- effects: {json.dumps(effects)}")

        anti_patterns: List[str] = []
        raw_anti = theme_preferences.get("antiPatterns") or []
        if isinstance(raw_anti, list):
            anti_patterns = [a for a in raw_anti if isinstance(a, str) and a.strip()]

        tokens_block = (
            "\n".join(token_lines) if token_lines
            else "(no specific tokens — preserve existing theme as-is)"
        )
        ap_block = ""
        if anti_patterns:
            ap_block = (
                "\nAnti-patterns (HARD CONSTRAINTS — must not appear in generated UI):\n"
                + "\n".join(f"- {a}" for a in anti_patterns)
                + "\n"
            )

        return f"""
Current UI Schema (partial — screen/theme/layout/components/antiPatterns only):
{schema_str}

Task: Apply the following design tokens to the UI fields. The structural contract
(dataModels, dataSources, actions, state.bindings) is already correct and must NOT
change. This is a pure design-application pass.

Design tokens to apply:
{tokens_block}
{ap_block}
Rules:
1. Set `theme.direction` to the exact direction value listed above (do not invent a different style name).
2. Set theme color roles (primary, secondary, accent, background, text) to the values above.
3. Set `theme.typography.display` and `theme.typography.body` to the typography values above.
4. Set `theme.effects` to the effects list above (list of strings).
5. Set top-level `antiPatterns` to the list above.
6. Update component `prop.className` / style fields to reflect the direction and color palette —
   use Tailwind arbitrary values like `bg-[#RRGGBB]` for exact hex colors.
7. Preserve ALL component IDs, structural relationships, and non-visual component props exactly.

RESPONSE FORMAT (required):
Return JSON with this exact top-level shape:
{{
  "updated_ir_schema": {{
    "screen": {{ ... }},
    "theme": {{ ... }},
    "layout": {{ ... }},
    "components": [ ... ],
    "antiPatterns": [ ... ],
    "state": {{ ... }}
  }},
  "changes_summary": {{
    "modified_components": [],
    "fields_changed": ["theme", "antiPatterns", ...],
    "description": "Applied design tokens",
    "category": "ui"
  }}
}}
Inside updated_ir_schema include ONLY: screen, theme, layout, components, antiPatterns,
and state (omit state.bindings). Do NOT include dataModels, dataSources, or actions.
Return valid JSON only.
"""

    async def _run_design_pass(
        self,
        ir_schema: Dict[str, Any],
        theme_preferences: Dict[str, Any],
        *,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run a UI-scoped design pass after initial IR generation.

        Uses the existing UI-only update pathway (same system prompt, same UI
        key slicing, same merge logic as update_screen_ir with category="ui")
        but driven by an internally-generated design-token prompt rather than
        a user's free-form edit request.

        Returns the IR with UI fields updated; structural fields are untouched.
        On any failure the original schema is returned unchanged.
        """
        print("\n[STEP 3.5] Running design pass (applying design tokens to UI)...")
        try:
            design_system_prompt = self._create_update_system_prompt(category="ui")
            design_user_prompt = self._build_design_pass_user_prompt(
                ir_schema, theme_preferences
            )
            full_prompt = f"{design_system_prompt}\n\n{design_user_prompt}"

            started_at = datetime.utcnow()
            response = await async_generate(
                "gemini-3-flash-preview",
                full_prompt,
                config=GeminiGenerateConfig(temperature=0.1),
            )
            finished_at = datetime.utcnow()

            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )
            print(
                f"[TOKEN USAGE - Design Pass] Prompt: {token_usage['prompt_tokens']}, "
                f"Response: {token_usage['candidates_tokens']}, "
                f"Total: {token_usage['total_tokens']}"
            )

            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="design-pass",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                metadata={"category": "ui"},
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

            ai_response = ""
            try:
                ai_response = response.text or ""
            except Exception:
                if getattr(response, "candidates", None):
                    for c in response.candidates:
                        parts = getattr(c, "content", None) and getattr(
                            c.content, "parts", None
                        )
                        if parts:
                            ai_response = "\n".join(
                                getattr(p, "text", "")
                                for p in parts
                                if getattr(p, "text", "")
                            )
                            if ai_response:
                                break

            if not ai_response:
                logger.warning("Design pass returned empty response; skipping merge.")
                return ir_schema

            result = self._parse_update_response(ai_response)
            if not result or not isinstance(result, dict):
                logger.warning("Design pass response unparseable; skipping merge.")
                return ir_schema

            # Prefer the documented wrapper; accept a bare UI schema if the model
            # returns UI keys at the top level (same keys as _ir_schema_for_ui_update).
            partial = result.get("updated_ir_schema")
            if not isinstance(partial, dict):
                ui_keys = ("screen", "theme", "layout", "components", "antiPatterns", "state")
                if any(k in result for k in ui_keys):
                    partial = {k: result[k] for k in ui_keys if k in result}
                else:
                    partial = None

            if not partial:
                logger.warning("Design pass returned no updated_ir_schema; skipping.")
                return ir_schema

            merged = self._merge_update_response_into_current(ir_schema, partial, "ui")
            print("   -> Design pass applied successfully.")
            return merged

        except BillingError:
            raise
        except Exception as exc:
            logger.exception(
                "Design pass failed; continuing with original IR: %s", exc
            )
            return ir_schema

    # ------------------------------------------------------------------

    async def update_screen_ir(
        self,
        prompt: str,
        current_ir_schema: Dict[str, Any],
        chat_history: Optional[List[Dict[str, str]]] = None,
        theme_preferences: Optional[Dict[str, Any]] = None,
        category: Optional[str] = None,
        previous_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Update existing IR schema based on user modifications.
        Uses chat history to understand context and only modifies requested parts.
        
        Args:
            prompt: User's modification request
            current_ir_schema: The existing IR schema to modify
            chat_history: Previous chat messages for context
            theme_preferences: Theme settings
            category: Update category ("ui" or "behavior") for targeted prompts
            previous_prompt: The prompt that produced the current screen (for context)
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        
        Returns:
            (updated_ir_schema, changes_summary)
        """
        
        # Build context from chat history
        chat_context = ""
        if chat_history:
            chat_context = "Chat History (Only for context. Your main task is to update the IR schema based on the current user's prompt.):\n"
            for msg in chat_history[-10:]:  # Last 10 messages for context
                role = msg.get("role", "user")
                content = msg.get("content", "")
                chat_context += f"{role.upper()}: {content}\n"
        logger.info(f"chat_context: {chat_context}")
        
        if previous_prompt:
            chat_context += f"\nPrevious prompt that created this screen: \"{previous_prompt}\"\n"
        
        # Credentials only needed for behavioral changes (dataSources); skip for UI-only updates
        available_credentials: List[Dict[str, Any]] = []
        if category != "ui" and project_id:
            cred_svc = CredentialService.optional()
            if cred_svc:
                try:
                    available_credentials = await cred_svc.list_credentials(project_id)
                except Exception as e:
                    logger.warning("Failed to list credentials for IR update: %s", e)
        
        # Create specialized prompt for targeted updates (UI: partial schema; behavior: full)
        update_system_prompt = self._create_update_system_prompt(
            category=category,
            available_credentials=available_credentials,
        )
        update_user_prompt = self._create_update_user_prompt(
            prompt,
            current_ir_schema,
            chat_context,
            category,
            available_credentials=available_credentials,
        )
        
        full_prompt = f"{update_system_prompt}\n\n{update_user_prompt}"
        
        try:
            started_at = datetime.utcnow()
            response = await async_generate(
                "gemini-3-flash-preview",
                full_prompt,
                config=GeminiGenerateConfig(temperature=0.2),
            )
            finished_at = datetime.utcnow()
            
            # EXTRACT AND TRACK TOKENS
            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage['prompt_tokens'],
                token_usage['candidates_tokens']
            )
            
            # Record to database
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="update-screen-ir",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                metadata={"category": category} if category else {},
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id
            )
            
            print(f"\n[TOKEN USAGE - update_screen_ir] Prompt: {token_usage['prompt_tokens']}, "
                  f"Response: {token_usage['candidates_tokens']}, "
                  f"Total: {token_usage['total_tokens']}")

            # Extract response text
            ai_response = ''
            try:
                ai_response = response.text or ''
            except Exception:
                if getattr(response, 'candidates', None):
                    for c in response.candidates:
                        parts = getattr(c, 'content', None) and getattr(c.content, 'parts', None)
                        if parts:
                            ai_response = '\n'.join(getattr(p, 'text', '') for p in parts if getattr(p, 'text', ''))
                            if ai_response:
                                break
            
            print(f"Update response length: {len(ai_response)}")
            
            # Parse the response
            updated_schema = None
            changes_summary = {}
            
            if ai_response:
                result = self._parse_update_response(ai_response)
                if result:
                    partial_schema = result.get('updated_ir_schema')
                    changes_summary = result.get('changes_summary', {})
                    if partial_schema:
                        updated_schema = self._merge_update_response_into_current(
                            current_ir_schema, partial_schema, category
                        )
                    else:
                        updated_schema = current_ir_schema
                else:
                    updated_schema = None
            else:
                updated_schema = None
            
            if not updated_schema:
                print("Failed to parse update response, using current schema")
                updated_schema = current_ir_schema
                if not changes_summary:
                    changes_summary = {"status": "no_changes"}
            
            if updated_schema:
                print("\n[VALIDATION] Validating updated IR schema...")
                updated_schema, was_valid, repairs = await self.validate_and_repair_ir(
                    updated_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
                )
                
                if not was_valid:
                    print(f"[REPAIR] Applied {len(repairs)} repairs to updated schema")
                    changes_summary['repairs_applied'] = len(repairs)
                    
            return updated_schema, changes_summary
            
        except Exception as e:
            print(f"Error in update_screen_ir: {str(e)}")
            return current_ir_schema, {"error": str(e)}

    def _merge_update_response_into_current(
        self,
        current_ir_schema: Dict[str, Any],
        partial_ir_schema: Dict[str, Any],
        category: Optional[str],
    ) -> Dict[str, Any]:
        """Merge AI-update partial IR into current schema by category.
        UI: partial contains only screen, theme, layout, components, antiPatterns, state (no bindings); we replace those and keep dataModels, dataSources, actions, state.bindings from current.
        Behavior (UI+behavior): partial can contain any keys (UI and/or behavior); we merge every key present in partial into current so components, theme, dataSources, actions, etc. are all applied.
        """
        merged = copy.deepcopy(current_ir_schema)
        if category == "ui":
            ui_keys = ("screen", "theme", "layout", "components", "antiPatterns")
            for key in ui_keys:
                if key in partial_ir_schema:
                    merged[key] = copy.deepcopy(partial_ir_schema[key])
            if "state" in partial_ir_schema:
                merged_state = copy.deepcopy(merged.get("state") or {})
                current_state = current_ir_schema.get("state") or {}
                for k, v in (partial_ir_schema.get("state") or {}).items():
                    if k != "bindings":
                        merged_state[k] = copy.deepcopy(v)
                if "bindings" in current_state:
                    merged_state["bindings"] = copy.deepcopy(current_state["bindings"])
                merged["state"] = merged_state
        else:
            # behavior = UI + behavior: merge every key present in partial (components, theme, dataModels, dataSources, actions, state, etc.)
            array_merge_keys = {"dataSources", "dataModels", "components", "actions"}
            for key, value in partial_ir_schema.items():
                if value is None:
                    continue
                if key in array_merge_keys and isinstance(value, list) and isinstance(merged.get(key), list):
                    existing_by_id = {item.get("id"): item for item in merged[key] if isinstance(item, dict) and item.get("id")}
                    for item in value:
                        if isinstance(item, dict) and item.get("id"):
                            existing_by_id[item["id"]] = copy.deepcopy(item)
                    merged[key] = list(existing_by_id.values())
                else:
                    merged[key] = copy.deepcopy(value)
        return merged

    def _create_update_system_prompt(
        self,
        category: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """System prompt specifically designed for updating IR schemas. UI: partial schema in/out, no credentials. Behavior: full schema, return partial for merge."""
        if category == "ui":
            return """You are an expert at precisely updating UI specifications. This is a UI-ONLY update.

    You will receive a PARTIAL current schema containing ONLY: screen, theme, layout, components, antiPatterns, and state (without state.bindings). dataModels, dataSources, actions, and state.bindings are NOT sent and must NOT be in your response.

    Your task:
    1. UNDERSTAND the user's modification request (theme, labels, text, layout, component styles, design direction / antiPatterns only).
    2. PRESERVE all unmodified parts of the partial schema EXACTLY as-is.
    3. UPDATE ONLY the specific UI parts requested.
    4. Return a PARTIAL "updated_ir_schema" containing ONLY: screen, theme, layout, components, antiPatterns, and state (omit state.bindings). Do NOT include dataModels, dataSources, or actions.

    # CRITICAL FOR UI UPDATES:
    - Do NOT change component IDs.
    - Do NOT add dataModels, dataSources, actions, or state.bindings to your response.
    - Only change: theme (incl. direction, typography, effects), antiPatterns, layout, components (text, colors, props, styles), and non-bindings state.
    - Preserve component order and hierarchy unless explicitly asked to change.

    # RESPONSE FORMAT:
    Return JSON:
    {
    "updated_ir_schema": {
        "screen": { ... },
        "theme": { ... },
        "layout": { ... },
        "components": [ ... ],
        "antiPatterns": [ ... ],
        "state": { ... }
    },
    "changes_summary": {
        "modified_components": ["id1", "id2"],
        "fields_changed": ["color", "text", ...],
        "description": "Short description",
        "category": "ui"
    }
    }
    Return ONLY the keys listed above in updated_ir_schema. No dataModels, dataSources, actions, or state.bindings."""
        cred_note = ""
        if available_credentials:
            names = [c.get("name") for c in available_credentials if c.get("name")]
            cred_note = f"\n    - For dataSources that need auth: use ONLY these credential names in config: {json.dumps(names)}. Do NOT invent names."
        return f"""You are an expert at precisely updating UI and behavior specifications. This is a BEHAVIORAL update: UI + behavior (you may change both).

    You will receive the FULL current IR schema. You may change ANY part of it: add/update/delete components, change colors/theme/layout, add/remove/update backend functions (dataSources, dataModels, actions), update state.bindings. 
    Return a full updated schema OR a partial schema with only the top-level keys you changed; make sure everything is present in that top level key (your changes + previous schema). we will merge your response into the current schema based on top level key. 
    So if you miss anything inside the key it will be lost. So you have to be very careful about what you are returning. Preserve the existing structure, make your changes and return the final version.

    Your task:
    1. UNDERSTAND the user's modification request (UI and/or behavior: components, theme, dataSources, actions, bindings, etc.).
    2. Apply the requested changes. You may add/update/delete components, change colors, add/remove/update dataSources and actions.
    3. PRESERVE all unmodified parts of the current IR schema EXACTLY as-is. Preserve existing component IDs and dataSource IDs unless explicitly asked or required to change or remove them.
    4. Return "updated_ir_schema" as full schema or only the top-level keys you changed (e.g. screen, theme, layout, components, antiPatterns, dataModels, dataSources, actions, state).
    5. MAINTAIN structure and consistency of the schema.
    *(IMPORTANT)*:
    - You have to think what will be the solution which will solve user's query. Then make required modifications to IR schema. Keep in mind you are only updating the IR schema, not rewriting the entire application. So keep the existing UI structure and behavior unless it is required.

    # STRUCTURAL UI RULES:
    - For adding components: Add new components while preserving ALL existing components
    - For removing components: Remove only specified components, keep everything else
    - Maintain component order unless reordering is explicitly requested

     # BEHAVIORAL RULES:
    - For behavioral changes: ONLY adjust relevant actions, dataSources, or dataModels
    - Do NOT rename IDs of existing components or dataSources
    - Preserve UI structure when only backend logic changes
    - Only change the parts that are explicitly requested or required to change or remove them according to user's prompt.

    # BEHAVIOR = UI + BEHAVIOR:
    - Components: may be added, updated, or deleted.
    - UI: colors, theme, layout, labels, text may be changed.
    - Backend: dataSources, dataModels, actions may be added, removed, or updated.
    - state.bindings may be added or updated.
    - Maintain referential integrity: dataSources reference existing dataModels; component IDs referenced in layout/bindings must exist.

    # SECURITY AND CREDENTIALS:
    - Reference credential *names* only (e.g. credentials: ["stripe_secret_key"]). No API keys or secrets in the IR.{cred_note}

    # RESPONSE FORMAT:
    Return JSON:
    {{
    "updated_ir_schema": {{
        // Full schema OR only the top-level keys you changed (e.g. components, theme, dataSources, actions, state)
    }},
    "changes_summary": {{
        "modified_components": [],
        "fields_changed": ["components", "dataSources", "theme", ...],
        "description": "Short description",
        "category": "behavior"
    }}
    }}
    You may return a full schema or only the top-level keys you modified; we will merge into the current schema."""

    def _ir_schema_for_ui_update(self, current_ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Strip to UI-only keys for UI update prompt: screen, theme, layout, components, antiPatterns, state (without bindings)."""
        out = {}
        for key in ("screen", "theme", "layout", "components", "antiPatterns"):
            if key in current_ir_schema:
                out[key] = copy.deepcopy(current_ir_schema[key])
        state = current_ir_schema.get("state")
        if state is not None:
            state_copy = copy.deepcopy(state)
            state_copy.pop("bindings", None)
            if state_copy:
                out["state"] = state_copy
        return out

    def _create_update_user_prompt(
        self,
        prompt: str,
        current_ir_schema: Dict[str, Any],
        chat_context: str,
        category: Optional[str] = None,
        available_credentials: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Create user prompt for IR schema update. UI: send partial schema (no dataModels, dataSources, actions, state.bindings). Behavior: send full schema."""
        if category == "ui":
            schema_for_prompt = self._ir_schema_for_ui_update(current_ir_schema)
            current_schema_str = json.dumps(schema_for_prompt, indent=2)
            cred_block = ""
            category_hint = "\nIMPORTANT: This is a UI-only update. You received only UI-related keys (screen, theme, layout, components, antiPatterns, state without bindings). Return ONLY those keys with your updates; do NOT include dataModels, dataSources, actions, or state.bindings."
        else:
            current_schema_str = json.dumps(current_ir_schema, indent=2)
            cred_block = ""
            if available_credentials:
                cred_list = [{"name": c.get("name"), "type": c.get("type"), "service": c.get("service")} for c in available_credentials]
                cred_block = f"\n\nAvailable credentials for this project (use only these names in dataSources if modifying dataSources):\n{json.dumps(cred_list, indent=2)}"
            category_hint = ""
            if category == "behavior":
                category_hint = "\nIMPORTANT: This is a behavioral update (UI + behavior). You may add/update/delete components, change colors/theme/layout, and add/remove/update dataSources, dataModels, and actions. Return full schema or only the keys you changed; we will merge into the current schema.\n\n# COMPONENT ACTIONS FORMAT (for navigation, API calls, etc.):\n- Each component can have an `actions` array with trigger/flow steps\n- Trigger: `onClick`, `onSubmit`, `onChange`, `onLoad`, `onSelect`, `onMount`\n- Flow steps: `{ \"do\": \"validate|api|navigate|notify|modal|setState|setData|prefetch\", \"params\": {}, \"onSuccess\": \"...\", \"onError\": \"...\" }`\n- For NAVIGATION between screens in a multi-screen app (routing is DISABLED):\n  - Use `do: \"navigate\"` with `params: { \"targetScreen\": \"screen-id\" }`\n  - The targetScreen should match the logical screen identifier used in app.component.ts\n  - Example: `{ \"trigger\": \"onClick\", \"flow\": [{ \"do\": \"navigate\", \"params\": { \"targetScreen\": \"screen1\" } }] }`\n  - This tells the Angular generator to emit a @Output() navigate event that app.component.ts handles"
        
        return f"""
    Current IR Schema:
    {current_schema_str}

    {chat_context}

    User's Modification Request:
    "{prompt}"
    {category_hint}
    {cred_block}

    Task:
    1. Identify EXACTLY what the user wants to change
    2. Apply ONLY those changes to the schema
    3. Keep everything else identical - preserve all IDs, structure, and unmentioned fields
    4. Return the updated schema (partial OK for UI or behavior as instructed above)

    CRITICAL: You must keep the schema identical everywhere except where the user explicitly requests changes.
    Focus on precision - preserve the exact structure while making targeted updates.
    Return valid JSON only.
    """

    def _parse_update_response(self, response: str) -> Dict[str, Any]:
        """Parse the update response which should contain updated_ir_schema and changes_summary"""
        try:
            response = response.strip()
            
            # Remove markdown code blocks
            if response.startswith('```'):
                first_newline = response.find('\n')
                if first_newline != -1:
                    response = response[first_newline + 1:]
            
            if response.endswith('```'):
                last_marker = response.rfind('```')
                if last_marker != -1:
                    response = response[:last_marker]
            
            response = response.strip()
            
            # Try direct parse first
            try:
                result = json.loads(response)
                return result
            except json.JSONDecodeError:
                # Try to extract JSON object
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    json_str = json_match.group()
                    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
                    json_str = re.sub(r'//.*?$', '', json_str, flags=re.MULTILINE)
                    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
                    return json.loads(json_str)
        
        except Exception as e:
            print(f"Error parsing update response: {str(e)}")
        
        return None
    
    def validate_ir_schema(self, ir_schema: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate IR schema against JSON Schema.
        
        Returns:
            (is_valid, error_messages)
        """
        try:
            validator = Draft7Validator(IR_SCHEMA)
            errors = list(validator.iter_errors(ir_schema))
            
            if not errors:
                return True, []
            
            # Collect detailed error messages
            error_messages = []
            for error in errors:
                path = " -> ".join(str(p) for p in error.path) if error.path else "root"
                error_messages.append(f"At '{path}': {error.message}")
            
            return False, error_messages
            
        except Exception as e:
            return False, [f"Validation exception: {str(e)}"]

    def _analyze_validation_errors(self, validation_errors: List[str]) -> Dict[str, Any]:
        """
        Analyze validation errors to determine what needs fixing.
        
        Returns:
            Dictionary with:
            - only_non_workflow_errors: bool (True if no inputParameters/outputParameters errors)
            - problematic_fields: dict (fields that have errors)
            - fix_instructions: str (specific instructions for fixing)
        """
        workflow_error_patterns = ['inputParameters', 'outputParameters']
        state_error_patterns = ['state -> bindings']
        
        has_workflow_errors = any(
            any(pattern in err for pattern in workflow_error_patterns)
            for err in validation_errors
        )
        
        has_state_binding_errors = any(
            any(pattern in err for pattern in state_error_patterns)
            for err in validation_errors
        )
        
        problematic_fields = {}
        fix_instructions = []
        
        # Parse state.bindings errors
        if has_state_binding_errors:
            for err in validation_errors:
                if 'state -> bindings' in err:
                    # Extract field name and type issue
                    # Example: "At 'state -> bindings -> uploadSuccess': False is not of type 'string'"
                    parts = err.split("'")
                    if len(parts) >= 2:
                        field_path = parts[1]
                        field_name = field_path.split(' -> ')[-1]
                        problematic_fields[field_path] = err
                        
                        if 'False is not of type' in err:
                            fix_instructions.append(f"Convert state.bindings.{field_name}: False → 'false' (string)")
                        elif 'True is not of type' in err:
                            fix_instructions.append(f"Convert state.bindings.{field_name}: True → 'true' (string)")
                        elif 'None is not of type' in err:
                            fix_instructions.append(f"Convert state.bindings.{field_name}: None → 'null' (string)")
                        elif 'is not of type \'string\'' in err:
                            # Generic number/bool to string
                            fix_instructions.append(f"Convert state.bindings.{field_name} to string type")
        
        return {
            'only_non_workflow_errors': not has_workflow_errors,
            'has_state_binding_errors': has_state_binding_errors,
            'problematic_fields': problematic_fields,
            'fix_instructions': '\n'.join(fix_instructions) if fix_instructions else 'Convert all non-string values in state.bindings to strings'
        }

    @classmethod
    def _ir_error_top_level_keys(cls, validation_errors: List[str]) -> List[str]:
        """Top-level IR keys mentioned in JSON-schema error paths."""
        found: List[str] = []
        seen = set()
        for err in validation_errors or []:
            m = re.search(r"At '([^']+)'", str(err))
            path = m.group(1) if m else str(err)
            top = path.split("->")[0].strip()
            if not top or top == "root" or top not in cls._IR_TOP_LEVEL_KEYS:
                continue
            if top not in seen:
                seen.add(top)
                found.append(top)
        return found

    @classmethod
    def _ir_repair_fragments(
        cls, ir_schema: Dict[str, Any], validation_errors: List[str]
    ) -> Dict[str, Any]:
        """Subset of the IR that the repair LLM is allowed to rewrite."""
        keys = cls._ir_error_top_level_keys(validation_errors)
        src = ir_schema if isinstance(ir_schema, dict) else {}
        if keys:
            return {k: src[k] for k in keys if k in src}
        return {k: src[k] for k in cls._IR_TOP_LEVEL_KEYS if k in src}

    @classmethod
    def _merge_ir_repair_fragments(
        cls,
        original: Dict[str, Any],
        repaired: Dict[str, Any],
        allowed_keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Overlay repaired top-level keys onto the original IR.

        If ``allowed_keys`` is set, ignore extra keys the LLM invented so a
        fragment repair cannot rewrite inputParameters/outputParameters/etc.
        """
        if not isinstance(original, dict):
            return repaired if isinstance(repaired, dict) else {}
        if not isinstance(repaired, dict):
            return original
        merged = json.loads(json.dumps(original))
        allow = set(allowed_keys) if allowed_keys else None
        for k in cls._IR_TOP_LEVEL_KEYS:
            if k not in repaired:
                continue
            if allow is not None and k not in allow:
                continue
            merged[k] = repaired[k]
        return merged

    @staticmethod
    def _is_truncated_ir_repair_response(
        response: Any, token_usage: Optional[Dict[str, Any]] = None
    ) -> bool:
        """True when the repair model hit its output cap (unparseable dump)."""
        fr: Any = None
        try:
            cands = getattr(response, "candidates", None) or []
            if cands:
                fr = getattr(cands[0], "finish_reason", None)
        except Exception:
            fr = None
        reason = str(fr or "")
        reason_u = reason.upper()
        fr_name = str(getattr(fr, "name", "") or "").upper()
        # Gemini may return enum, "MAX_TOKENS", or integer 2 (FinishReason.MAX_TOKENS).
        if (
            "MAX_TOKEN" in reason_u
            or "MAX_TOKEN" in fr_name
            or fr == 2
            or reason_u in {"2", "MAX_TOKENS", "FINISHREASON.MAX_TOKENS"}
        ):
            return True
        usage = token_usage or {}
        out = int(usage.get("candidates_tokens") or usage.get("completion_tokens") or 0)
        # Cap is 8192; live dumps were 65532. Treat filling the cap as truncated.
        return out >= AIProcessor._IR_REPAIR_MAX_OUTPUT_TOKENS

    async def repair_ir_schema(
        self, 
        ir_schema: Dict[str, Any], 
        validation_errors: List[str],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Attempt to automatically repair invalid IR schema using LLM.
        
        Args:
            ir_schema: The invalid IR schema
            validation_errors: List of validation error messages
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        
        Returns:
            Repaired IR schema
        """
        error_analysis = self._analyze_validation_errors(validation_errors)
        fragment_keys = self._ir_error_top_level_keys(validation_errors)
        fragments = self._ir_repair_fragments(ir_schema, validation_errors)
        key_list = fragment_keys or list(fragments.keys())
        print(
            "Repairing IR fragments only "
            f"(keys={key_list}; skip full-schema rewrite)..."
        )

        repair_prompt = f"""You are a JSON repair specialist. Fix ONLY the broken IR fragments.

⚠️ CRITICAL RULES:
1. Return a single JSON object containing ONLY these top-level keys: {json.dumps(key_list)}
2. Do NOT return the rest of the IR. The backend merges your keys into the original schema.
3. Do NOT modify inputParameters or outputParameters unless they appear in the key list.
4. Keep existing valid values. Add missing required fields with sensible defaults.
5. dataSources[].type must be exactly "dynamic" or "external" when dataSources is included.

VALIDATION ERRORS TO FIX:
{chr(10).join(f"- {err}" for err in validation_errors)}

PROBLEMATIC FIELDS:
{json.dumps(error_analysis['problematic_fields'], indent=2)}

BROKEN FRAGMENTS:
{json.dumps(fragments, indent=2)}

SPECIFIC FIXES NEEDED:
{error_analysis['fix_instructions']}

Return ONLY the repaired fragment JSON."""

        try:
            started_at = datetime.utcnow()
            response = await run_sync(
                sync_generate,
                self._IR_REPAIR_MODEL,
                repair_prompt,
                config=GeminiGenerateConfig(
                    temperature=0.1,
                    max_output_tokens=self._IR_REPAIR_MAX_OUTPUT_TOKENS,
                ),
            )
            finished_at = datetime.utcnow()
            
            # EXTRACT AND TRACK TOKENS
            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage['prompt_tokens'],
                token_usage['candidates_tokens']
            )
            
            # Record to database
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="repair-ir-schema",
                model=self._IR_REPAIR_MODEL,
                is_retry=True,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                metadata={"error_count": len(validation_errors), "fragment_keys": key_list},
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            print(f"\n[TOKEN USAGE - repair_ir_schema] Prompt: {token_usage['prompt_tokens']}, "
                  f"Response: {token_usage['candidates_tokens']}, "
                  f"Total: {token_usage['total_tokens']}")

            truncated = self._is_truncated_ir_repair_response(response, usage) or (
                self._is_truncated_ir_repair_response(response, token_usage)
            )
            if truncated:
                print(
                    "IR repair LLM hit output token cap / MAX_TOKENS — "
                    "skipping truncated JSON, applying heuristic repairs"
                )
                return self._heuristic_repair(ir_schema, validation_errors)

            # Extract response
            ai_response = ''
            try:
                ai_response = response.text or ''
            except Exception:
                if getattr(response, 'candidates', None):
                    for c in response.candidates:
                        parts = getattr(c, 'content', None) and getattr(c.content, 'parts', None)
                        if parts:
                            ai_response = '\n'.join(
                                getattr(p, 'text', '') for p in parts if getattr(p, 'text', '')
                            )
                            if ai_response:
                                break
            
            if ai_response:
                repaired = self._parse_ai_response(ai_response)
                if repaired:
                    print("✓ Schema repaired by LLM (fragment merge)")
                    return self._merge_ir_repair_fragments(
                        ir_schema, repaired, allowed_keys=key_list
                    )
            
            # If LLM repair failed, apply basic heuristic repairs
            print("LLM repair failed, applying heuristic repairs")
            return self._heuristic_repair(ir_schema, validation_errors)

        except BillingError:
            raise
        except Exception as e:
            print(f"Error in repair_ir_schema: {str(e)}")
            return self._heuristic_repair(ir_schema, validation_errors)


    def _issue7_extra_required_output_names(self) -> List[str]:
        """Planner/detected IO contract names that must stay required on response models."""
        from services.ir_response_requiredness import declared_output_required_names

        detected = getattr(self, "_detected_io_params", None) or {}
        return sorted(declared_output_required_names(detected.get("outputParameters") or []))


    def _heuristic_repair(self, ir_schema: Dict[str, Any], validation_errors: List[str]) -> Dict[str, Any]:
        """
        Apply rule-based heuristic repairs to IR schema.
        Fallback when LLM repair fails.
        """
        repaired = json.loads(json.dumps(ir_schema))  # Deep copy

        # Prefer tokens already present on the IR; only fill missing structural keys.
        # Do not default typography to Inter — that is the generic AI-UI attractor we removed.
        existing_theme = repaired.get("theme") if isinstance(repaired.get("theme"), dict) else {}
        existing_typography = existing_theme.get("typography") if isinstance(existing_theme.get("typography"), dict) else {}
        legacy_font = existing_theme.get("font") if isinstance(existing_theme.get("font"), str) else None

        display_font = existing_typography.get("display") or legacy_font or "system-ui, sans-serif"
        body_font = existing_typography.get("body") or legacy_font or "system-ui, sans-serif"

        direction = existing_theme.get("direction")
        if direction not in DESIGN_DIRECTION_ENUM:
            direction = "Minimalism & Swiss Style"

        effects = existing_theme.get("effects")
        if isinstance(effects, str) and effects.strip():
            effects_value = [effects.strip()]
        elif isinstance(effects, list):
            effects_value = [e for e in effects if isinstance(e, str) and e.strip()]
        else:
            effects_value = ["Subtle hover transitions"]

        default_theme = {
            "direction": direction,
            "primary": "#3B82F6",
            "secondary": "#F3F4F6",
            "accent": "#10B981",
            "background": "#FFFFFF",
            "text": "#1F2937",
            "error": "#EF4444",
            "success": "#10B981",
            "typography": {
                "display": display_font,
                "body": body_font,
            },
            "effects": effects_value,
            "radius": "8px",
        }

        # Fix missing screen object
        if 'screen' not in repaired or not isinstance(repaired['screen'], dict):
            repaired['screen'] = {
                "type": "mixed",
                "title": "Generated Screen",
                "selector": "app-generated-screen"
            }
        else:
            # Fix screen.type
            if 'type' not in repaired['screen']:
                repaired['screen']['type'] = 'mixed'
            elif repaired['screen']['type'] not in ['form', 'table', 'dashboard', 'detail', 'list', 'wizard', 'mixed']:
                repaired['screen']['type'] = 'mixed'
            
            # Fix screen.title
            if 'title' not in repaired['screen'] or not repaired['screen']['title']:
                repaired['screen']['title'] = 'Generated Screen'
            
            # Fix screen.selector - generate from title if missing
            if 'selector' not in repaired['screen'] or not repaired['screen']['selector']:
                # Generate selector from title (convert to kebab-case)
                title = repaired['screen'].get('title', 'generated-screen')
                selector = 'app-' + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
                repaired['screen']['selector'] = selector
        
        # Ensure theme (new shape: direction, typography, effects; no Inter default)
        if 'theme' not in repaired or not isinstance(repaired['theme'], dict):
            repaired['theme'] = copy.deepcopy(default_theme)
        else:
            for k, v in default_theme.items():
                if k == "typography":
                    typography = repaired['theme'].get("typography")
                    if not isinstance(typography, dict):
                        repaired['theme']['typography'] = v.copy()
                    else:
                        typography.setdefault("display", v["display"])
                        typography.setdefault("body", v["body"])
                        # Migrate legacy font → typography when roles are still missing
                        if legacy_font:
                            typography.setdefault("display", legacy_font)
                            typography.setdefault("body", legacy_font)
                elif k == "effects":
                    if "effects" not in repaired['theme'] or repaired['theme']['effects'] in (None, "", []):
                        repaired['theme']['effects'] = list(v)
                elif k == "direction":
                    if repaired['theme'].get("direction") not in DESIGN_DIRECTION_ENUM:
                        repaired['theme']['direction'] = v
                else:
                    repaired['theme'].setdefault(k, v)

        # Ensure antiPatterns (top-level constraint list — not part of theme tokens)
        if 'antiPatterns' not in repaired or not isinstance(repaired['antiPatterns'], list):
            repaired['antiPatterns'] = []
        else:
            repaired['antiPatterns'] = [
                item for item in repaired['antiPatterns'] if isinstance(item, str) and item.strip()
            ]

        # Derive sensible layout defaults based on screen type
        _screen_type_for_repair = repaired.get('screen', {}).get('type', 'mixed')

        _LAYOUT_DEFAULTS_BY_TYPE = {
            'form':      {'mode': 'single', 'columns': 1, 'gap': '24px', 'max_width': '860px'},
            'wizard':    {'mode': 'single', 'columns': 1, 'gap': '24px', 'max_width': '860px'},
            'table':     {'mode': 'single', 'columns': 1, 'gap': '24px', 'max_width': '1280px'},
            'list':      {'mode': 'single', 'columns': 1, 'gap': '24px', 'max_width': '1280px'},
            'dashboard': {'mode': 'grid',   'columns': 3, 'gap': '24px', 'max_width': '1440px'},
            'detail':    {'mode': 'multi',  'columns': 2, 'gap': '24px', 'max_width': '1280px'},
            'mixed':     {'mode': 'multi',  'columns': 2, 'gap': '24px', 'max_width': '1280px'},
        }
        _layout_def = _LAYOUT_DEFAULTS_BY_TYPE.get(
            _screen_type_for_repair, _LAYOUT_DEFAULTS_BY_TYPE['mixed']
        )

        # Ensure layout
        if 'layout' not in repaired or not isinstance(repaired['layout'], dict):
            repaired['layout'] = dict(_layout_def)
        
        # Fix missing components array
        if 'components' not in repaired or not isinstance(repaired['components'], list):
            repaired['components'] = []
        
        # Ensure at least one component
        if len(repaired['components']) == 0:
            repaired['components'] = [{
                "id": "default_container",
                "type": "container",
                "props": {},
                "children": []
            }]
        
        # Fix each component
        for i, comp in enumerate(repaired['components']):
            if not isinstance(comp, dict):
                continue
            
            # Ensure required fields
            if 'id' not in comp or not comp['id']:
                comp['id'] = f"component_{i}"
            
            if 'type' not in comp or not comp['type']:
                comp['type'] = 'container'
            
            # Ensure props exists
            if 'props' not in comp:
                comp['props'] = {}
        
        # Fix theme colors if present
        if 'theme' in repaired and isinstance(repaired['theme'], dict):
            for color_key in ['primary', 'secondary', 'accent', 'background', 'text', 'error', 'success']:
                if color_key in repaired['theme']:
                    color = repaired['theme'][color_key]
                    if isinstance(color, str) and re.match(r'^#[0-9A-Fa-f]{6}$', color):
                        continue
                    if isinstance(color, str) and color.startswith('#') and len(color) == 4:
                            repaired['theme'][color_key] = f"#{color[1]*2}{color[2]*2}{color[3]*2}"
                    else:
                        repaired['theme'][color_key] = default_theme[color_key]
                else:
                    repaired['theme'][color_key] = default_theme[color_key]
        
        # Fix layout mode if present
        if 'layout' in repaired and isinstance(repaired['layout'], dict):
            if 'mode' in repaired['layout']:
                if repaired['layout']['mode'] not in ['single', 'multi', 'grid']:
                    repaired['layout']['mode'] = _layout_def['mode']
            else:
                repaired['layout']['mode'] = _layout_def['mode']
            repaired['layout'].setdefault('columns', _layout_def['columns'])
            repaired['layout'].setdefault('gap', _layout_def['gap'])
            repaired['layout'].setdefault('max_width', _layout_def['max_width'])

        # Ensure dataModels (format 1 only: { type, required })
        if 'dataModels' not in repaired or not isinstance(repaired['dataModels'], dict) or not repaired['dataModels']:
            repaired['dataModels'] = {
                "DefaultRequest": {
                    "id": {"type": "string", "required": False}
                },
                "DefaultResponse": {
                    "status": {"type": "string", "required": False},
                    "error": {"type": "string", "required": False},
                },
            }
        
        # Ensure dataSources
        if 'dataSources' not in repaired or not isinstance(repaired['dataSources'], list):
            repaired['dataSources'] = []
        if not repaired['dataSources']:
            repaired['dataSources'].append({
                "id": "default_dynamic_fn",
                "type": "dynamic",
                "description": "Fallback dynamic function",
                "inputModel": "DefaultRequest",
                "responseModel": "DefaultResponse",
                "config": {
                    "method": "POST",
                    "timeoutMs": 5000
                },
                "operation": {
                    "action": "insert",
                    "collection": "records",
                    "description": "Store payload into records collection"
                },
                "databaseLogic": "Insert payload into records collection and return success"
            })
        else:
            for ds in repaired['dataSources']:
                if not isinstance(ds, dict):
                    continue
                ds.setdefault('id', 'generated_source')
                ds.setdefault('type', 'dynamic')
                ds.setdefault('config', {})
                ds['config'].setdefault('method', 'POST')
                ds['config'].setdefault('timeoutMs', 5000)
                if ds['type'] == 'external':
                    ds['config'].setdefault('externalUrl', 'https://api.example.com')
                ds.setdefault('inputModel', next(iter(repaired['dataModels'].keys())))
                ds.setdefault('responseModel', list(repaired['dataModels'].keys())[-1])
                ds.setdefault('operation', {
                    "action": "custom",
                    "collection": "records",
                    "description": "Execute custom business logic"
                })
                ds.setdefault('databaseLogic', "Describe how this function interacts with the database or external API.")

        # Ensure state
        if 'state' not in repaired or not isinstance(repaired['state'], dict):
            repaired['state'] = {"bindings": {}}
        else:
            repaired['state'].setdefault('bindings', {})

        # NEW: Ensure inputParameters
        if 'inputParameters' not in repaired or not isinstance(repaired['inputParameters'], list):
            repaired['inputParameters'] = []

        for param in repaired.get('inputParameters', []):
            if not isinstance(param, dict):
                continue
            param.setdefault('name', 'unknown_input')
            param.setdefault('type', 'string')
            param.setdefault('required', False)
            # bindTo should always exist as a list for workflow bindings
            if 'bindTo' not in param or not isinstance(param['bindTo'], list):
                param['bindTo'] = []

        # NEW: Ensure outputParameters
        if 'outputParameters' not in repaired or not isinstance(repaired['outputParameters'], list):
            repaired['outputParameters'] = []

        for param in repaired.get('outputParameters', []):
            if not isinstance(param, dict):
                continue
            param.setdefault('name', 'unknown_output')
            param.setdefault('type', 'string')

        restore_declared_output_requiredness(
            repaired,
            extra_required_names=self._issue7_extra_required_output_names(),
        )
        return repaired


    def extract_workflow_metadata(ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract workflow metadata for orchestrator consumption.

        Returns:
            {
                "requires_inputs": ["param1", "param2"],
                "provides_outputs": ["output1", "output2"],
                "input_bindings": {
                    "param1": ["component:field1", "dataSource:api1"]
                }
            }
        """
        metadata = {
            "requires_inputs": [],
            "provides_outputs": [],
            "input_bindings": {},
        }

        # Extract required inputs
        for param in ir_schema.get("inputParameters", []):
            if not isinstance(param, dict):
                continue
            param_name = param.get("name")
            if not param_name:
                continue
            if param.get("required", True):
                metadata["requires_inputs"].append(param_name)

            # Extract bindings
            bindings = []
            for binding in param.get("bindTo", []):
                if not isinstance(binding, dict):
                    continue
                target = binding.get("target")
                if target:
                    bindings.append(target)
            if bindings:
                metadata["input_bindings"][param_name] = bindings

        # Extract outputs
        for param in ir_schema.get("outputParameters", []):
            if not isinstance(param, dict):
                continue
            param_name = param.get("name")
            if param_name:
                metadata["provides_outputs"].append(param_name)

        return metadata

    def _normalize_empty_models(self, ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converts any 'empty' types into actual empty model definitions.

        - If model = "empty"  → replace with {}
        - If field = "empty"  → remove the field
        """

        data_models = ir_schema.get("dataModels", {})

        if isinstance(data_models, str):
            try:
                data_models = json.loads(data_models)
            except json.JSONDecodeError:
                data_models = {}
            ir_schema["dataModels"] = data_models

        if not isinstance(data_models, dict):
            data_models = {}
            ir_schema["dataModels"] = data_models

        for model_name, fields in list(data_models.items()):

            # Case 1: Model is literally "empty"
            if fields == "empty":
                data_models[model_name] = {}
                continue

            # Case 2: Fields inside model use "empty"
            if isinstance(fields, dict):
                for field_name, field_type in list(fields.items()):
                    if field_type == "empty":
                        del data_models[model_name][field_name]

        return ir_schema

    # ------------------------------------------------------------------
    # Canonical type set accepted by IR_SCHEMA enum for *Parameters[].type
    # ------------------------------------------------------------------
    _VALID_PARAM_TYPES: frozenset = frozenset({
        "string", "int", "float", "bool", "datetime",
        "dict", "list", "object", "number",
    })

    # Map of non-schema types the LLM commonly emits → nearest valid type
    _PARAM_TYPE_MAP: Dict[str, str] = {
        "email":    "string",
        "password": "string",
        "text":     "string",
        "url":      "string",
        "phone":    "string",
        "file":     "string",
        "date":     "datetime",
        "array":    "list",
        "integer":  "int",
        "boolean":  "bool",
    }

    _VALID_DATASOURCE_TYPES: frozenset = frozenset({"dynamic", "external"})
    _IR_TOP_LEVEL_KEYS: Tuple[str, ...] = (
        "screen",
        "theme",
        "layout",
        "components",
        "dataModels",
        "dataSources",
        "state",
        "inputParameters",
        "outputParameters",
        "antiPatterns",
    )
    # Live Issue 4: uncapped repair dumped 65,532 tokens over ~3 minutes.
    _IR_REPAIR_MAX_OUTPUT_TOKENS = 20000
    _IR_REPAIR_MODEL = "gemini-3-flash-preview"

    @staticmethod
    def _infer_datasource_type(ds: Dict[str, Any]) -> str:
        """Return a valid dataSources[].type (dynamic|external).

        Prefer an already-valid type. Otherwise infer ``external`` when the
        config clearly targets a third-party URL/auth; default to ``dynamic``.
        """
        raw = str(ds.get("type") or "").strip().lower()
        if raw in AIProcessor._VALID_DATASOURCE_TYPES:
            return raw
        config = ds.get("config") if isinstance(ds.get("config"), dict) else {}
        external_url = (
            config.get("externalUrl")
            or config.get("url")
            or config.get("baseUrl")
            or ""
        )
        if isinstance(external_url, str) and external_url.strip():
            return "external"
        # Do not infer external from a truthy config.auth — dynamic Mongo
        # sources often carry credentials/auth hints without being REST.
        return "dynamic"

    def _normalize_ir_before_validation(self, ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Programmatic, deterministic normalization applied BEFORE every validation
        call (and again after each LLM repair pass).  No LLM involved — only
        mechanical fixes for issues the LLM causes deterministically.

        Fixes:
        1. screen.selector — derive from title unconditionally; never ask the LLM
           to freehand a regex-conformant string.
        2. state.bindings values — coerce non-string scalars (True/False/None/int)
           to their string equivalents.
        3. inputParameters / outputParameters [].type — map the extended set the
           system prompt inadvertently teaches (email, password, date, array…) to
           the four-enum subset the JSON Schema actually accepts.
        4. dataSources missing config / type — add minimal defaults so required-
           field checks never fire on trivially fixable schemas (Issue 3 live:
           missing dataSources[].type was burning LLM repair + budget every screen).
        5. dataModels — normalize to format 1 only ({ type, required }); convert
           legacy bare type strings so backend/Angular share one contract.
        6. theme — migrate legacy `font` into `typography.{display,body}`; coerce
           `effects` to string or string[].
        7. antiPatterns — ensure top-level list; hoist out of `theme` if nested.
        """
        schema = json.loads(json.dumps(ir_schema))  # deep copy, avoid mutation

        # --- 1. screen.selector derivation --------------------------------
        screen = schema.get("screen")
        if isinstance(screen, dict):
            title = screen.get("title") or "screen"
            current_sel = screen.get("selector") or ""
            if not re.match(r"^app-[a-z][a-z0-9-]*$", current_sel):
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "screen"
                # Regex requires first char after "app-" to be a lowercase letter
                if not slug[0].isalpha():
                    slug = f"screen-{slug}".strip("-")
                screen["selector"] = f"app-{slug}"

        # --- 2. state.bindings value coercion to strings ------------------
        state = schema.get("state")
        if isinstance(state, dict):
            bindings = state.get("bindings")
            if isinstance(bindings, dict):
                for key, val in list(bindings.items()):
                    if isinstance(val, bool):
                        bindings[key] = "true" if val else "false"
                    elif val is None:
                        bindings[key] = "null"
                    elif not isinstance(val, str):
                        bindings[key] = str(val)

        # --- 3. param type normalisation ----------------------------------
        for section in ("inputParameters", "outputParameters"):
            params = schema.get(section)
            if not isinstance(params, list):
                continue
            for param in params:
                if not isinstance(param, dict):
                    continue
                # Handle both 'type' and 'dataType' fields (workflow uses dataType)
                raw_type = str(param.get("type") or param.get("dataType") or "string").strip().lower()
                # Remove dataType if present - IR schema uses 'type'
                if "dataType" in param:
                    del param["dataType"]
                if raw_type not in self._VALID_PARAM_TYPES:
                    param["type"] = self._PARAM_TYPE_MAP.get(raw_type, "string")
                else:
                    param["type"] = raw_type

        # --- 4. dataSources missing config / type -------------------------
        for ds in schema.get("dataSources") or []:
            if not isinstance(ds, dict):
                continue
            if not isinstance(ds.get("config"), dict):
                ds["config"] = {"method": "POST", "timeoutMs": 5000}
            # Issue 3 (live 2026-09-18): LLM often omits required dataSources[].type.
            # Inject before validation so we never spend ~3min / $0.20 on LLM repair.
            ds["type"] = self._infer_datasource_type(ds)

        # --- 6. theme shape — migrate legacy font → typography roles ---------
        theme = schema.get("theme")
        if isinstance(theme, dict):
            typography = theme.get("typography")
            if not isinstance(typography, dict):
                typography = {}
            legacy_font = theme.get("font") if isinstance(theme.get("font"), str) and theme.get("font").strip() else None
            if legacy_font:
                typography.setdefault("display", legacy_font)
                typography.setdefault("body", legacy_font)
            if typography:
                theme["typography"] = typography
            # Normalize effects: keep string or list of strings only
            effects = theme.get("effects")
            if effects is not None and not isinstance(effects, (str, list)):
                theme["effects"] = str(effects)
            elif isinstance(effects, list):
                theme["effects"] = [e for e in effects if isinstance(e, str)]

        # --- 7. antiPatterns top-level constraint list ----------------------
        anti = schema.get("antiPatterns")
        if anti is None:
            # Accept accidental nesting under theme from older/partial LLM output
            if isinstance(theme, dict) and isinstance(theme.get("antiPatterns"), list):
                schema["antiPatterns"] = [
                    a for a in theme.pop("antiPatterns") if isinstance(a, str) and a.strip()
                ]
        elif not isinstance(anti, list):
            schema["antiPatterns"] = [str(anti)] if anti else []
        else:
            schema["antiPatterns"] = [a for a in anti if isinstance(a, str) and a.strip()]

        return schema

    async def _finalize_ir_after_repair(
        self,
        schema: Dict[str, Any],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Final IR finishing steps shared by all exit paths of validate_and_repair_ir.

        Order:
          1. _normalize_empty_models   — deterministic cleanup
          2. _ensure_workflow_io_parameters (T4) — infers IO params; flags uncovered
             required model fields in _ir_consistency_warnings
          3. _check_ir_internal_consistency (T5) — LLM resolves warnings; cleans key
          4. _ensure_ir_callability (T7) — final gate: required backend params derivable
             from UI/state/workflow IO; LLM repair if still failing
          5. restore_declared_output_requiredness (Issue 7 + Issue 6) — pin graph
             outputs required:true on the appropriate response models after T5/T7
             Path B, then strip UI action fields from find/list query responses
        """
        schema = self._normalize_empty_models(schema)
        schema = self._ensure_workflow_io_parameters(schema)
        schema = await self._check_ir_internal_consistency(
            schema, project_id, screen_id, version, org_id, subscriber_id, user_id
        )
        schema = await self._ensure_ir_callability(
            schema, project_id, screen_id, version, org_id, subscriber_id, user_id
        )
        # Issue 7: T5/T7 Path B may mark uncovered request fields optional.
        # Re-pin graph-declared outputs on response models after those passes.
        # Issue 6: same pass strips user_action/etc. from find/find_one models.
        schema = restore_declared_output_requiredness(
            schema,
            extra_required_names=self._issue7_extra_required_output_names(),
        )
        return schema

    async def validate_and_repair_ir(
        self, 
        ir_schema: Dict[str, Any],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool, List[str]]:
        """
        Complete validation and repair pipeline.

        Flow:
          1. Programmatic normalization (no LLM) — fixes deterministic issues
             before the schema even touches the validator.
          2. Validate.  If clean → done.
          3. Heuristic repair first (Issue 4). If that validates, skip the
             expensive LLM rewrite that historically hit the 65K output cap.
          4. Up to 2 fragment LLM repair attempts. Each pass sends only the
             broken top-level keys (not the full IR), capped at 8192 output
             tokens. Truncated replies fall back to heuristics immediately.
          5. _finalize_ir_after_repair — normalize empty models, infer workflow IO
             (T4), run IR consistency LLM (T5), callability gate (T7).

        Returns:
            (final_ir_schema, was_valid, applied_repairs)
            was_valid=True  → passed on first try (no repairs needed)
            was_valid=False → repairs were applied (or best-effort returned)
        """
        _MAX_REPAIR_ATTEMPTS = 2

        # Step 0: Programmatic normalization — always, no LLM, zero cost
        ir_schema = self._normalize_ir_before_validation(ir_schema)

        # Step 1: Validate after normalization
        is_valid, errors = self.validate_ir_schema(ir_schema)

        if is_valid:
            print("✓ IR schema is valid (passed after pre-validation normalization)")
            ir_schema = await self._finalize_ir_after_repair(
                ir_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
            )
            return ir_schema, True, []

        print(f"✗ IR schema validation failed with {len(errors)} errors")
        for err in errors:
            print(f"  - {err}")

        initial_errors = list(errors)

        # Issue 4: try deterministic heuristic repair BEFORE any LLM call.
        # Live LinkedIn builds spent ~3 min / $0.20 rewriting the full IR,
        # then fell back to this same heuristic after the model truncated.
        heuristic_schema = self._heuristic_repair(ir_schema, errors)
        heuristic_schema = self._normalize_ir_before_validation(heuristic_schema)
        heuristic_valid, heuristic_errors = self.validate_ir_schema(heuristic_schema)
        if heuristic_valid:
            print("✓ Schema repaired by heuristic (skipped LLM repair)")
            heuristic_schema = await self._finalize_ir_after_repair(
                heuristic_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
            )
            return heuristic_schema, False, initial_errors

        print(
            f"Heuristic repair still has {len(heuristic_errors)} error(s) — "
            "falling back to fragment LLM repair"
        )

        # Step 2: LLM repair loop — up to _MAX_REPAIR_ATTEMPTS passes
        active_schema = heuristic_schema
        active_errors = heuristic_errors

        for attempt in range(1, _MAX_REPAIR_ATTEMPTS + 1):
            print(f"\n[Repair {attempt}/{_MAX_REPAIR_ATTEMPTS}] Attempting LLM repair "
                  f"with {len(active_errors)} error(s) as context...")

            active_schema = await self.repair_ir_schema(
                active_schema, active_errors,
                project_id, screen_id, version, org_id, subscriber_id, user_id,
            )

            # Normalize again — LLM repair can introduce its own trivial issues
            active_schema = self._normalize_ir_before_validation(active_schema)

            is_valid_now, remaining_errors = self.validate_ir_schema(active_schema)

            if is_valid_now:
                print(f"✓ Schema repaired and validated on attempt {attempt}")
                active_schema = await self._finalize_ir_after_repair(
                    active_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
                )
                return active_schema, False, initial_errors

            print(f"✗ Repair attempt {attempt} incomplete — "
                  f"{len(remaining_errors)} error(s) remain")
            for err in remaining_errors:
                print(f"  - {err}")

            if attempt < _MAX_REPAIR_ATTEMPTS:
                print(f"  → Retrying with remaining errors as new context...")
                active_errors = remaining_errors  # feed remaining errors into next pass

        # All attempts exhausted — return best-effort schema
        print(f"✗ All {_MAX_REPAIR_ATTEMPTS} repair attempts exhausted. "
              f"Returning best-effort schema ({len(remaining_errors)} errors remain).")
        active_schema = await self._finalize_ir_after_repair(
            active_schema, project_id, screen_id, version, org_id, subscriber_id, user_id
        )
        return (
            active_schema,
            False,
            initial_errors + [
                f"Repair incomplete after {_MAX_REPAIR_ATTEMPTS} attempts: "
                + str(remaining_errors)
            ],
        )

    def _ensure_workflow_io_parameters(self, ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Populate inputParameters/outputParameters if missing or sparse by inspecting
        components and dataSources. Keeps the IR as the single source of truth for
        orchestrator bindings even when the user prompt does not list inputs/outputs.
        """
        schema = json.loads(json.dumps(ir_schema))  # deep copy

        # Convert to list if it's a dict/object
        if isinstance(schema.get("inputParameters"), dict):
            schema["inputParameters"] = []
        else:
            schema.setdefault("inputParameters", [])
        
        if isinstance(schema.get("outputParameters"), dict):
            schema["outputParameters"] = []
        else:
            schema.setdefault("outputParameters", [])

        existing_inputs = {p.get("name") for p in schema.get("inputParameters", []) if isinstance(p, dict)}
        existing_outputs = {p.get("name") for p in schema.get("outputParameters", []) if isinstance(p, dict)}

        def add_input(name: str, ptype: str, description: str = "", bind_to: Optional[list] = None):
            if not name or name in existing_inputs:
                return
            schema["inputParameters"].append({
                "name": name,
                "type": ptype or "string",
                "description": description,
                "required": False,
                "bindTo": bind_to or []
            })
            existing_inputs.add(name)

        def add_output(name: str, ptype: str, description: str = "", source_component: Optional[str] = None, source_path: Optional[str] = None):
            if not name or name in existing_outputs:
                return
            entry = {
                "name": name,
                "type": ptype or "object",
                "description": description,
                # Inferred UI fields are not the graph contract (Issue 7).
                "required": False,
            }
            if source_component:
                entry["sourceComponent"] = source_component
            if source_path:
                entry["sourcePath"] = source_path
            schema["outputParameters"].append(entry)
            existing_outputs.add(name)

        # Infer from dataSources params
        for ds in schema.get("dataSources", []) or []:
            if not isinstance(ds, dict):
                continue
            ds_id = ds.get("id")
            params = (ds.get("config") or {}).get("params") or {}
            if isinstance(params, dict):
                for key in params.keys():
                    bind_to = [{"target": f"dataSource:{ds_id}", "path": f"config.params.{key}"}] if ds_id else []
                    add_input(key, "string", f"Auto-inferred param for dataSource {ds_id}", bind_to)

        # Infer from components (form-like fields)
        for comp in schema.get("components", []) or []:
            if not isinstance(comp, dict):
                continue
            cid = comp.get("id")
            ctype = (comp.get("type") or "").lower()
            props = comp.get("props") or {}

            # Skip components that are purely navigation (have navigate actions)
            # These don't produce form data outputs - they trigger screen transitions
            actions = comp.get("actions") or []
            is_navigation = False
            for action in actions:
                if not isinstance(action, dict):
                    continue
                flow = action.get("flow") or []
                for step in flow:
                    if isinstance(step, dict) and step.get("do") == "navigate":
                        is_navigation = True
                        break
                if is_navigation:
                    break
            if is_navigation:
                continue

            # Fields array (common form structure)
            fields = props.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    fname = field.get("id") or field.get("name")
                    ftype = self._infer_field_type(field.get("type"))
                    if fname:
                        bind_to = [{"target": f"component:{cid}", "path": f"props.fields.{fname}.value"}] if cid else []
                        add_input(fname, ftype, f"Auto-inferred field on component {cid}", bind_to)
                # Also create an output for the form if not present
                if cid:
                    add_output(f"{cid}_output", "object", f"Auto-inferred output for component {cid}", source_component=cid, source_path="value")

            # Simple input-like components
            if ctype in ["input", "text", "textarea", "email", "password", "number", "date"]:
                fname = cid or ctype
                ftype = self._infer_field_type(props.get("type") or ctype)
                bind_to = [{"target": f"component:{cid}", "path": "props.value"}] if cid else []
                add_input(fname, ftype, f"Auto-inferred input for component {cid}", bind_to)
                if cid:
                    add_output(f"{cid}_output", ftype, f"Auto-inferred output for component {cid}", source_component=cid, source_path="value")

        # Fallback: ensure non-empty outputs if the screen likely collects data.
        # Only add the generic ``formOutput`` placeholder when there is nothing
        # more specific to put there — i.e. no real inputParameters were inferred.
        # When real fields exist, the IO contract prompt already enforces a 1-to-1
        # mapping, so the LLM will produce per-field outputs and ``formOutput`` is
        # unnecessary and confusing for users.
        if not schema["outputParameters"]:
            real_inputs = [
                ip for ip in (schema.get("inputParameters") or [])
                if isinstance(ip, dict) and (ip.get("name") or "").strip()
                not in ("", "formOutput", "output", "result")
            ]
            if not real_inputs:
                add_output(
                    "formOutput", "object",
                    "Auto-inferred form output",
                    source_component=None, source_path=None,
                )

        # T4: Walk dataModels required fields for dynamic dataSources.
        # Flag required model fields that have no matching component, config.params, or inputParameter.
        # Warnings are stored under _ir_consistency_warnings for T5 (LLM repair) to consume.
        data_models = schema.get("dataModels") or {}
        ir_warnings: list = []

        for ds in schema.get("dataSources", []) or []:
            if not isinstance(ds, dict):
                continue
            ds_type = (ds.get("type") or "").lower()
            if ds_type != "dynamic":
                continue
            ds_id = ds.get("id") or ""
            input_model_name = ds.get("inputModel")
            if not input_model_name:
                continue
            model_def = data_models.get(input_model_name)
            # Skip absent, empty, or non-dict models
            if not isinstance(model_def, dict):
                continue

            for field_name, field_spec in model_def.items():
                if not isinstance(field_spec, dict):
                    continue
                if not field_spec.get("required", False):
                    continue
                # Required field must be derivable from UI/state/workflow IO
                derivable = self._collect_derivable_field_names(schema)
                if field_name not in derivable:
                    ir_warnings.append({
                        "type": "uncovered_required_model_field",
                        "dataSource": ds_id,
                        "model": input_model_name,
                        "field": field_name,
                        "field_type": field_spec.get("type", "string"),
                        "message": (
                            f"Required field '{field_name}' in model '{input_model_name}' "
                            f"(inputModel of dataSource '{ds_id}') has no matching UI "
                            f"component, config.params, or inputParameter."
                        ),
                    })

        if ir_warnings:
            schema["_ir_consistency_warnings"] = ir_warnings
            print(f"⚠️ [T4] {len(ir_warnings)} IR consistency warning(s) detected:")
            for w in ir_warnings:
                print(f"  - {w['message']}")
        else:
            # Clear stale warnings if all required fields are now covered
            schema.pop("_ir_consistency_warnings", None)

        return schema

    async def _check_ir_internal_consistency(
        self,
        ir_schema: Dict[str, Any],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        T5: IR internal consistency LLM check.

        Consumes _ir_consistency_warnings set by T4 (_ensure_workflow_io_parameters).
        Uses a targeted LLM call to resolve inconsistencies where backend model required
        fields have no matching UI component, config.params, or inputParameter.

        Resolution paths the LLM may choose:
          Path A — Add hidden state / inputParameter to carry the missing field (e.g.
                   a file that was uploaded in a previous step and should be passed through).
          Path B — Mark the model field required:false if the screen genuinely does not
                   need it (e.g. an edit-only form that does not re-upload a file).

        Modifies only: dataModels, state, inputParameters, outputParameters.
        Cleans up _ir_consistency_warnings before returning.
        """
        warnings = ir_schema.get("_ir_consistency_warnings")
        if not warnings:
            ir_schema.pop("_ir_consistency_warnings", None)
            return ir_schema

        print(f"\n[T5] IR internal consistency check — {len(warnings)} warning(s) to resolve...")

        schema = json.loads(json.dumps(ir_schema))  # deep copy

        data_models = schema.get("dataModels", {})
        components = schema.get("components", [])
        state = schema.get("state", {})
        input_params = schema.get("inputParameters", [])
        output_params = schema.get("outputParameters", [])
        data_sources = schema.get("dataSources", [])

        warnings_text = "\n".join(f"- {w['message']}" for w in warnings)

        # Collect only the affected models to keep the prompt concise
        affected_models: Dict[str, Any] = {}
        for w in warnings:
            model_name = w.get("model")
            if model_name and model_name in data_models:
                affected_models[model_name] = data_models[model_name]

        # Compact components summary (avoid sending full prop trees)
        components_summary = [
            {
                "id": c.get("id"),
                "type": c.get("type"),
                "props_keys": list((c.get("props") or {}).keys()),
            }
            for c in components
            if isinstance(c, dict)
        ]

        consistency_prompt = (
            "You are an IR schema consistency expert. The IR schema has inconsistencies "
            "where backend model required fields have no matching UI component, config.params, "
            "or inputParameter. You must resolve every inconsistency listed below.\n\n"
            "INCONSISTENCIES DETECTED:\n"
            f"{warnings_text}\n\n"
            "AFFECTED DATA MODELS:\n"
            f"{json.dumps(affected_models, indent=2)}\n\n"
            "CURRENT DATA SOURCES:\n"
            f"{json.dumps(data_sources, indent=2)}\n\n"
            "CURRENT COMPONENTS (summary):\n"
            f"{json.dumps(components_summary, indent=2)}\n\n"
            "CURRENT STATE:\n"
            f"{json.dumps(state, indent=2)}\n\n"
            "CURRENT INPUT PARAMETERS:\n"
            f"{json.dumps(input_params, indent=2)}\n\n"
            "CURRENT OUTPUT PARAMETERS:\n"
            f"{json.dumps(output_params, indent=2)}\n\n"
            "TASK:\n"
            "For each inconsistency choose ONE resolution path:\n\n"
            "PATH A (preferred when the screen receives the missing data from a previous "
            "step or retains it across steps):\n"
            "  - Add the field to `inputParameters` (the data arrives from workflow context)\n"
            "  - OR add a hidden entry in `state.bindings` to carry the field value\n"
            "  Use this path for binary/file fields (base64) that were uploaded earlier.\n\n"
            "PATH B (use when the backend model is over-specified for this screen's purpose):\n"
            "  - Mark the field `required: false` in `dataModels`\n"
            "  Use this path when the screen only edits/reads data and truly does not supply "
            "that field at submit time.\n"
            "  NEVER use Path B on a field whose name appears in outputParameters — those "
            "are graph-contract outputs and must stay required: true on the response model.\n\n"
            "RULES:\n"
            "1. Only modify these sections: dataModels, state, inputParameters, outputParameters\n"
            "2. Do NOT modify components, dataSources, screen, theme, layout\n"
            "3. Use format-1 for dataModels fields: {\"type\": \"...\", \"required\": true|false}\n"
            "4. Keep all existing valid data unchanged\n"
            "5. Be surgical: only add/change what is necessary to resolve the inconsistencies\n\n"
            "Return ONLY a JSON object with the sections you changed, for example:\n"
            "{\n"
            "  \"dataModels\": { ...only the modified models... },\n"
            "  \"state\": { ...if changed... },\n"
            "  \"inputParameters\": [ ...if changed... ],\n"
            "  \"outputParameters\": [ ...if changed... ]\n"
            "}\n"
            "Do NOT return the full IR schema. Return ONLY the changed sections."
        )

        try:
            started_at = datetime.utcnow()
            response = await run_sync(
                sync_generate,
                "gemini-3-flash-preview",
                consistency_prompt,
                config=GeminiGenerateConfig(temperature=0.1),
            )
            finished_at = datetime.utcnow()

            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )

            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="ir-consistency-check",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

            # Extract text from response
            ai_response = ""
            try:
                ai_response = response.text or ""
            except Exception:
                if getattr(response, "candidates", None):
                    for c in response.candidates:
                        parts = getattr(c, "content", None) and getattr(c.content, "parts", None)
                        if parts:
                            ai_response = "\n".join(
                                getattr(p, "text", "") for p in parts if getattr(p, "text", "")
                            )
                            if ai_response:
                                break

            if ai_response:
                patch = self._parse_ai_response(ai_response)
                if patch and isinstance(patch, dict):
                    patched_sections = self._apply_ir_io_targeted_patch(
                        schema, patch, affected_models, log_prefix="T5"
                    )
                    if patched_sections:
                        print(f"  [T5] Patched sections: {', '.join(patched_sections)}")
                        print(f"✓ [T5] IR consistency resolved {len(warnings)} warning(s)")
                    else:
                        print("⚠️ [T5] LLM patch contained no applicable changes — keeping original IR")
                else:
                    print("⚠️ [T5] LLM returned unparseable response — keeping original IR")
            else:
                print("⚠️ [T5] LLM returned empty response — keeping original IR")

        except BillingError:
            raise
        except Exception as e:
            print(f"⚠️ [T5] IR consistency check failed: {e} — keeping original IR")

        # Always remove the private warnings key before returning
        schema.pop("_ir_consistency_warnings", None)
        return schema

    def _collect_derivable_field_names(self, ir_schema: Dict[str, Any]) -> set:
        """
        T7: Collect field names derivable at screen runtime for backend calls.

        Sources:
          - inputParameters (workflow inputs bound to this screen)
          - state.bindings (retained/hidden state)
          - dataSource config.params (static params)
          - component form fields and simple input-like components
        """
        derivable: set = set()

        for param in ir_schema.get("inputParameters", []) or []:
            if isinstance(param, dict) and param.get("name"):
                derivable.add(param["name"])

        state = ir_schema.get("state")
        if isinstance(state, dict):
            bindings = state.get("bindings")
            if isinstance(bindings, dict):
                derivable.update(bindings.keys())

        for ds in ir_schema.get("dataSources", []) or []:
            if not isinstance(ds, dict):
                continue
            params = (ds.get("config") or {}).get("params") or {}
            if isinstance(params, dict):
                derivable.update(params.keys())

        for comp in ir_schema.get("components", []) or []:
            if not isinstance(comp, dict):
                continue
            cid = comp.get("id")
            ctype = (comp.get("type") or "").lower()
            props = comp.get("props") or {}

            fields = props.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    fname = field.get("id") or field.get("name")
                    if fname:
                        derivable.add(fname)

            if ctype in ["input", "text", "textarea", "email", "password", "number", "date"]:
                if cid:
                    derivable.add(cid)

        return derivable

    def _validate_ir_callability(self, ir_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        T7: Programmatic callability check for dynamic dataSources.

        Every runtime-required field in each dynamic inputModel must be derivable
        from at least one source in _collect_derivable_field_names.
        """
        issues: List[Dict[str, Any]] = []
        derivable = self._collect_derivable_field_names(ir_schema)
        data_models = ir_schema.get("dataModels") or {}

        for ds in ir_schema.get("dataSources", []) or []:
            if not isinstance(ds, dict):
                continue
            if (ds.get("type") or "").lower() != "dynamic":
                continue

            ds_id = ds.get("id") or ""
            input_model_name = ds.get("inputModel")
            if not input_model_name:
                continue

            model_def = data_models.get(input_model_name)
            if not isinstance(model_def, dict):
                continue
            # List-root models have no named business fields to derive
            if model_def.get("type") == "list":
                continue

            for field_name, field_spec in model_def.items():
                if not isinstance(field_spec, dict):
                    continue
                if not field_spec.get("required", False):
                    continue
                if field_name in derivable:
                    continue
                issues.append({
                    "type": "uncallable_required_field",
                    "dataSource": ds_id,
                    "model": input_model_name,
                    "field": field_name,
                    "field_type": field_spec.get("type", "string"),
                    "message": (
                        f"Required field '{field_name}' in model '{input_model_name}' "
                        f"(inputModel of dataSource '{ds_id}') is not derivable from "
                        f"inputParameters, state.bindings, config.params, or UI components."
                    ),
                })

        return issues

    def _apply_ir_io_targeted_patch(
        self,
        schema: Dict[str, Any],
        patch: Dict[str, Any],
        affected_models: Dict[str, Any],
        log_prefix: str = "IR patch",
    ) -> List[str]:
        """
        Surgically merge an LLM patch into IR IO sections.

        Only modifies: dataModels (affected models only), state.bindings (additive),
        inputParameters (append new), outputParameters (append new).
        """
        patched_sections: List[str] = []

        if "dataModels" in patch and isinstance(patch["dataModels"], dict):
            existing_models = schema.get("dataModels") or {}
            for model_name, model_def in patch["dataModels"].items():
                if model_name not in affected_models:
                    print(f"  [{log_prefix}] Skipping unrequested model patch: {model_name}")
                    continue
                if not isinstance(model_def, dict):
                    print(f"  [{log_prefix}] Skipping model '{model_name}': non-dict definition")
                    continue
                valid_fields = {
                    k: v for k, v in model_def.items()
                    if isinstance(v, dict) and "type" in v
                }
                if not valid_fields and model_def:
                    print(f"  [{log_prefix}] Skipping model '{model_name}': no format-1 fields")
                    continue
                merged = dict(existing_models.get(model_name) or {})
                merged.update(valid_fields)
                existing_models[model_name] = merged
            schema["dataModels"] = normalize_data_models_to_format1(
                existing_models, legacy_required_default=False
            )
            patched_sections.append("dataModels")

        if "state" in patch and isinstance(patch["state"], dict):
            existing_state = schema.get("state") or {}
            existing_bindings = (existing_state.get("bindings") or {})
            new_bindings = (patch["state"].get("bindings") or {})
            for k, v in new_bindings.items():
                if k not in existing_bindings:
                    existing_bindings[k] = v
            existing_state["bindings"] = existing_bindings
            schema["state"] = existing_state
            patched_sections.append("state")

        if "inputParameters" in patch and isinstance(patch["inputParameters"], list):
            existing_ip = schema.get("inputParameters") or []
            existing_ip_names = {p.get("name") for p in existing_ip if isinstance(p, dict)}
            added = 0
            for param in patch["inputParameters"]:
                if (
                    isinstance(param, dict)
                    and param.get("name")
                    and param["name"] not in existing_ip_names
                ):
                    existing_ip.append(param)
                    existing_ip_names.add(param["name"])
                    added += 1
            schema["inputParameters"] = existing_ip
            if added:
                patched_sections.append(f"inputParameters (+{added})")

        if "outputParameters" in patch and isinstance(patch["outputParameters"], list):
            existing_op = schema.get("outputParameters") or []
            existing_op_names = {p.get("name") for p in existing_op if isinstance(p, dict)}
            added = 0
            for param in patch["outputParameters"]:
                if (
                    isinstance(param, dict)
                    and param.get("name")
                    and param["name"] not in existing_op_names
                ):
                    existing_op.append(param)
                    existing_op_names.add(param["name"])
                    added += 1
            schema["outputParameters"] = existing_op
            if added:
                patched_sections.append(f"outputParameters (+{added})")

        return patched_sections

    async def _repair_ir_callability(
        self,
        ir_schema: Dict[str, Any],
        callability_issues: List[Dict[str, Any]],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        T7: LLM repair when required backend params are still not derivable.

        Resolution paths:
          Path A — add inputParameter or state.bindings entry for workflow-passed data
          Path B — mark field required:false when screen does not supply it at submit
        """
        schema = json.loads(json.dumps(ir_schema))

        data_models = schema.get("dataModels", {})
        components = schema.get("components", [])
        state = schema.get("state", {})
        input_params = schema.get("inputParameters", [])
        output_params = schema.get("outputParameters", [])
        data_sources = schema.get("dataSources", [])

        issues_text = "\n".join(f"- {i['message']}" for i in callability_issues)
        failing_fields_summary = [
            {
                "field": i.get("field"),
                "model": i.get("model"),
                "dataSource": i.get("dataSource"),
                "field_type": i.get("field_type", "string"),
            }
            for i in callability_issues
        ]
        affected_models: Dict[str, Any] = {}
        for issue in callability_issues:
            model_name = issue.get("model")
            if model_name and model_name in data_models:
                affected_models[model_name] = data_models[model_name]

        derivable = sorted(self._collect_derivable_field_names(schema))
        existing_input_names = sorted({
            p.get("name") for p in input_params if isinstance(p, dict) and p.get("name")
        })
        existing_binding_keys = sorted(
            (state.get("bindings") or {}).keys()
            if isinstance(state, dict) and isinstance(state.get("bindings"), dict)
            else []
        )
        components_summary = [
            {
                "id": c.get("id"),
                "type": c.get("type"),
                "props_keys": list((c.get("props") or {}).keys()),
            }
            for c in components
            if isinstance(c, dict)
        ]

        callability_prompt = (
            "You are an IR callability repair specialist. Your ONLY job is to make the "
            "listed required backend fields derivable so Angular can submit them.\n\n"
            "CONTEXT: T5 consistency repair already ran. These fields are STILL not "
            "derivable from inputParameters, state.bindings, config.params, or UI "
            "components. Fix ONLY what is listed below.\n\n"
            "CALLABILITY FAILURES (fix every one):\n"
            f"{issues_text}\n\n"
            "FAILURES AS STRUCTURED LIST:\n"
            f"{json.dumps(failing_fields_summary, indent=2)}\n\n"
            "CURRENTLY DERIVABLE FIELD NAMES (do NOT re-add these):\n"
            f"{json.dumps(derivable, indent=2)}\n\n"
            "EXISTING inputParameter names (append only NEW names, never duplicate):\n"
            f"{json.dumps(existing_input_names, indent=2)}\n\n"
            "EXISTING state.bindings keys (append only NEW keys, never overwrite):\n"
            f"{json.dumps(existing_binding_keys, indent=2)}\n\n"
            "AFFECTED DATA MODELS (you may patch ONLY these models in dataModels):\n"
            f"{json.dumps(affected_models, indent=2)}\n\n"
            "CURRENT DATA SOURCES (read-only — do NOT modify):\n"
            f"{json.dumps(data_sources, indent=2)}\n\n"
            "CURRENT COMPONENTS summary (read-only — do NOT modify):\n"
            f"{json.dumps(components_summary, indent=2)}\n\n"
            "CURRENT STATE:\n"
            f"{json.dumps(state, indent=2)}\n\n"
            "CURRENT INPUT PARAMETERS:\n"
            f"{json.dumps(input_params, indent=2)}\n\n"
            "CURRENT OUTPUT PARAMETERS:\n"
            f"{json.dumps(output_params, indent=2)}\n\n"
            "DECISION RULE — for EACH failure, pick exactly ONE path:\n\n"
            "PATH A — use when the field value comes from a PREVIOUS workflow step or "
            "is retained across steps (file upload, prior step output, hidden carry-over):\n"
            "  • Prefer `inputParameters` when workflow orchestrator passes the value in.\n"
            "  • Use `state.bindings` only when the screen retains the value internally.\n"
            "  • The new entry name/key MUST EXACTLY equal the failing `field` name.\n\n"
            "PATH B — use ONLY when this screen is edit-only and truly does NOT submit "
            "that field at runtime (e.g. edit form that only sends edited_data):\n"
            "  • In `dataModels`, set ONLY that field's `required` to false.\n"
            "  • Keep the field `type` unchanged. Do NOT delete the field.\n\n"
            "EXACT PATCH SHAPES (follow precisely):\n\n"
            "PATH A — inputParameters (append ONE object per missing field):\n"
            "{\n"
            '  "inputParameters": [\n'
            "    {\n"
            '      "name": "<exact field name>",\n'
            '      "type": "<string|int|float|bool|datetime|dict|list|object|number>",\n'
            '      "description": "Workflow input for <field>",\n'
            '      "required": false,\n'
            '      "bindTo": []\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Map dataModel types: string→string, dict/object→dict, list→list, "
            "int/number→number, bool→bool, datetime→datetime.\n\n"
            "PATH A — state.bindings (append ONE key per missing field):\n"
            "{\n"
            '  "state": {\n'
            '    "bindings": {\n'
            '      "<exact field name>": "carried from prior step"\n'
            "    }\n"
            "  }\n"
            "}\n"
            "state.bindings values MUST be strings (not booleans/numbers/objects).\n\n"
            "PATH B — dataModels (change ONLY the required flag on the failing field):\n"
            "{\n"
            '  "dataModels": {\n'
            '    "<model name>": {\n'
            '      "<exact field name>": { "type": "<unchanged>", "required": false }\n'
            "    }\n"
            "  }\n"
            "}\n"
            "FORMAT 1 ONLY for dataModels fields: {\"type\": \"...\", \"required\": true|false}.\n"
            "NEVER use bare strings like \"field\": \"string\".\n\n"
            "CRITICAL RULES — violations cause the patch to be rejected:\n"
            "1. Return ONLY a JSON object with sections you changed. NO markdown, NO prose.\n"
            "2. Do NOT return the full IR schema — only the diff/patch object.\n"
            "3. Do NOT modify: components, dataSources, screen, theme, layout, actions.\n"
            "4. In dataModels, include ONLY models listed in AFFECTED DATA MODELS.\n"
            "5. In dataModels, include ONLY the specific failing fields you change — "
            "do NOT return the whole model with all fields.\n"
            "6. Do NOT change any field that is already in CURRENTLY DERIVABLE FIELD NAMES.\n"
            "7. Do NOT remove or replace existing inputParameters/outputParameters arrays — "
            "return only NEW entries to append.\n"
            "8. Do NOT overwrite existing state.bindings keys — only add missing keys.\n"
            "9. Do NOT add outputParameters unless strictly required for callability "
            "(almost never needed here).\n"
            "10. Each failing field must be fixed by exactly one path — do not fix the "
            "same field twice.\n"
            "11. NEVER mark required:false on a field whose name appears in "
            "outputParameters (graph-contract outputs stay required on response models).\n\n"
            "EXAMPLE — invoice edit screen missing file fields:\n"
            "Failure: invoice_file_base64, invoice_file_name required but only edited_data "
            "is in UI.\n"
            "Good Path A patch:\n"
            "{\n"
            '  "inputParameters": [\n'
            '    {"name": "invoice_file_base64", "type": "string", "description": "File from upload step", "required": false, "bindTo": []},\n'
            '    {"name": "invoice_file_name", "type": "string", "description": "Original filename", "required": false, "bindTo": []}\n'
            "  ]\n"
            "}\n"
            "Good Path B patch (if screen never resubmits files):\n"
            "{\n"
            '  "dataModels": {\n'
            '    "SubmitInvoiceRequest": {\n'
            '      "invoice_file_base64": {"type": "string", "required": false},\n'
            '      "invoice_file_name": {"type": "string", "required": false}\n'
            "    }\n"
            "  }\n"
            "}\n\n"
            "Return ONLY the JSON patch object now."
        )

        try:
            started_at = datetime.utcnow()
            response = await run_sync(
                sync_generate,
                "gemini-3-flash-preview",
                callability_prompt,
                config=GeminiGenerateConfig(temperature=0.1),
            )
            finished_at = datetime.utcnow()

            token_usage = self._extract_token_usage(response)
            self.token_metrics.add_usage(
                token_usage["prompt_tokens"],
                token_usage["candidates_tokens"],
            )

            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="ir-generation",
                sub_operation="ir-callability-repair",
                model="gemini-3-flash-preview",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

            ai_response = ""
            try:
                ai_response = response.text or ""
            except Exception:
                if getattr(response, "candidates", None):
                    for c in response.candidates:
                        parts = getattr(c, "content", None) and getattr(c.content, "parts", None)
                        if parts:
                            ai_response = "\n".join(
                                getattr(p, "text", "") for p in parts if getattr(p, "text", "")
                            )
                            if ai_response:
                                break

            if ai_response:
                patch = self._parse_ai_response(ai_response)
                if patch and isinstance(patch, dict):
                    patched = self._apply_ir_io_targeted_patch(
                        schema, patch, affected_models, log_prefix="T7"
                    )
                    if patched:
                        print(f"  [T7] Patched sections: {', '.join(patched)}")
                    else:
                        print("⚠️ [T7] LLM patch contained no applicable changes")
                else:
                    print("⚠️ [T7] LLM returned unparseable response — keeping IR")
            else:
                print("⚠️ [T7] LLM returned empty response — keeping IR")

        except BillingError:
            raise
        except Exception as e:
            print(f"⚠️ [T7] Callability repair failed: {e} — keeping IR")

        return schema

    async def _ensure_ir_callability(
        self,
        ir_schema: Dict[str, Any],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        T7 agentic gate: every runtime-required backend param must be derivable.

        Flow (never aborts IR generation for the whole screen):
          1. Programmatic validate via _validate_ir_callability.
          2. If failures → up to 2 LLM repair attempts via _repair_ir_callability.
          3. Re-validate after each repair.
          4. If still failing → log warning, return best-effort IR (T8 catches TS).
        """
        _MAX_CALLABILITY_REPAIR = 2
        schema = json.loads(json.dumps(ir_schema))

        issues = self._validate_ir_callability(schema)
        if not issues:
            schema.pop("_ir_callability_issues", None)
            return schema

        print(f"\n[T7] Callability check — {len(issues)} issue(s) detected:")
        for issue in issues:
            print(f"  - {issue['message']}")

        for attempt in range(1, _MAX_CALLABILITY_REPAIR + 1):
            print(f"[T7] Repair attempt {attempt}/{_MAX_CALLABILITY_REPAIR}...")
            schema = await self._repair_ir_callability(
                schema, issues,
                project_id, screen_id, version, org_id, subscriber_id, user_id,
            )
            issues = self._validate_ir_callability(schema)
            if not issues:
                print(f"✓ [T7] Callability resolved on attempt {attempt}")
                schema.pop("_ir_callability_issues", None)
                return schema

            print(f"⚠️ [T7] {len(issues)} callability issue(s) remain after attempt {attempt}")
            for issue in issues:
                print(f"  - {issue['message']}")

        print(
            f"⚠️ [T7] Callability repair exhausted ({_MAX_CALLABILITY_REPAIR} attempts). "
            f"Returning best-effort IR — {len(issues)} issue(s) remain."
        )
        schema.pop("_ir_callability_issues", None)
        return schema

    def _infer_field_type(self, ftype: Optional[str]) -> str:
        if not ftype:
            return "string"
        ft = ftype.lower()
        if ft in ["email", "text", "string"]:
            return "string"
        if ft in ["password"]:
            return "string"
        if ft in ["number", "int", "integer", "float", "double"]:
            return "number"
        if ft in ["date", "datetime"]:
            return "datetime"
        if ft in ["boolean", "bool", "checkbox", "switch"]:
            return "bool"
        return "string"

    def get_session_metrics(self) -> dict:
        """Get accumulated metrics for current session"""
        return self.token_metrics.get_cost()
    
    def reset_metrics(self):
        """Reset token metrics"""
        self.token_metrics.reset()

    async def process_prompt_async(self, prompt, screen_type=None, theme_preferences=None, workflow_context=None, **kwargs):
        """Async version of process_prompt"""
        # process_prompt is now native async, so we await it directly
        return await self.process_prompt( 
            prompt, 
            screen_type=screen_type, 
            theme_preferences=theme_preferences, 
            workflow_context=workflow_context,
            **kwargs
        )

    async def generate_ir_schema_async(self, prompt, screen_type=None, theme_preferences=None, workflow_context=None, **kwargs):
        """Async wrapper for generate_ir_schema"""
        # generate_ir_schema is now native async, so we await it directly
        return await self.generate_ir_schema(
            prompt,
            screen_type=screen_type,
            theme_preferences=theme_preferences,
            workflow_context=workflow_context,
            **kwargs
        )

    async def update_screen_ir_async(self, prompt, current_ir_schema, **kwargs):
        """Async version of update_screen_ir"""
        return await self.update_screen_ir(
            prompt,
            current_ir_schema,
            **kwargs
        )