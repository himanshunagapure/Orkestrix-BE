# New Issues — LinkedIn Rec3 Workflow (`linkedin-recruiter2`)
_Forensic analysis of `proj31b91e3bf6094d19_sb_5beaabd82ac6767c86dc311c` ("Linkedin Rec3")_
_Date: 2026-09-21 | Run date: 2026-09-18_

**Implementation status (2026-09-23):** Issues **1–6**, **8–10**, **12–14** are **done**. Still open: **7**, **11**, **15** (reframe — see below), **16**.

**Evidence sources:**
- `linkedin-recruiter2/Process_config.txt` — graph, bindings, persistence, node descriptions
- `linkedin-recruiter2/screen1/screen_generations.txt` — Screen 1 IR + datasource databaseLogic
- `linkedin-recruiter2/screen1/BE/backend_functions.txt` — `fetch_jds`, `process_jd` generated source + error logs
- `linkedin-recruiter2/screen2/screen_generations.txt` — Screen 2 IR + datasource databaseLogic
- `linkedin-recruiter2/screen2/BE/backend_functions.txt` — `fetch_candidates` (v1+v2), `submit_action`
- `linkedin-recruiter2/tool1/profile_fetching_tool_22304660_sb_86dc311c_20260918_155151_6efb7f67.json` — tool source

---

## Summary table

| # | Issue | Layer | Severity | Blocks? | Status |
|---|---|---|---|---|---|
| 1 | IR / backend prompt teaches `Database.find` / `Database.find_one` — neither exists | IR prompt + backend_generator | **Critical** | Yes — Screen 1 submit crashes on first click | ✅ DONE |
| 2 | `process_jd` codegen ignores its own `databaseLogic` (wrong collection, wrong params, wrong model) | backend_generator | **Critical** | Yes — selecting an existing JD is impossible | ✅ DONE |
| 3 | `process_jd` reads Gemini key from `os.environ` / `params` instead of `request_meta["credentials"]` | backend_generator | **High** | Yes — new JD upload silently fails in sandbox | ✅ DONE |
| 4 | `process_jd` uses `Database.update_one` with no upsert — new JD not saved | backend_generator | **High** | Yes — new JDs never persist | ✅ DONE |
| 5 | Conformance gate is advisory: `fetch_candidates` (conformance_ok: false) still published | backend_conformance | **High** | Yes — Screen 2 load always fails | ✅ DONE |
| 6 | Screen 2 IR forces `user_action` onto a read-only datasource (`fetch_candidates`) | IR generator | **High** | Yes — fetch datasource structurally invalid | ✅ DONE |
| 7 | Screen 2 IR invents unnecessary `submit_action` datasource writing to `workflow_actions` | IR generator | **Medium** | Partial — adds complexity and a spurious write | ⬜ open |
| 8 | `fetch_candidates` sort probes use wrong alias names (`"exp"`, `"dist"`) | backend_generator | **Medium** | Yes — sort-by-experience/network-distance broken | ✅ DONE |
| 9 | `fetch_candidates` does not map `experience` / `education` nested arrays | backend_generator | **Medium** | Partial — "top 2–3 jobs" card section always empty | ✅ DONE |
| 10 | Tool uses `Database.get_sync_collection()` + sync `update_one($set)` — fails in async sandbox | gap_tool_generator | **High** | Yes — tool crashes after scoring, before write | ✅ DONE |
| 11 | Tool auth: `jwt_token` used as Unipile `X-API-KEY`; `account_id` from credentials never declared | gap_tool_generator + planner | **High** | Yes — every Unipile call fails | ⬜ open |
| 12 | Graph `capabilityRef.version: null`, `class_path: null` on tool node | gap_tool_generator / enrichment | **High** | Yes — LangGraph cannot dispatch the tool | ✅ DONE |
| 13 | Planner persistence self-join duplicated 4× in `databaseLogic` — crowds out algorithm | persistence_link + ai_processor | **Medium** | Indirect — confuses codegen, wastes context | ✅ DONE |
| 14 | Planner providerOperations have trailing `` ` `` in URLs, marked `verified: true` | provider_operation (+ planner pin) | **Medium** | Indirect — tool generator sees malformed spec | ✅ DONE |
| 15 | Screen 2 header `candidate_count` shows 0 | Stage 2 IO + IR/FE (not Stage 4c) | **Medium** | Partial — "Showing X of Y requested" is always 0 | ⬜ open (reframe) |
| 16 | Tool stores `actively_looking` as `"yes"`/`"no"`, Screen 2 badge expects `"Yes"`/`"No"` | gap_tool_generator | **Low** | Partial — "Open to Work" badge never renders | ⬜ open |

---

## Issue 1 — IR / backend prompt teaches non-existent `Database.find` / `Database.find_one`

**Layer:** `backend/services/ai_processor.py` (databaseLogic enricher) + `backend/services/backend_generator.py` (`_runtime_execute_rules`) + `backend/services/database.py`  
**Severity:** Critical  
**Blocks workflow:** Yes  
**Status:** ✅ **DONE** (2026-09-21)

### Evidence

`PLATFORM RULES` in both the databaseLogic enricher prompt (`ai_processor.py:1315`) and the code-generation runtime rules (`backend_generator.py:88`) say:

> "await insert_one/update_one/delete_one/**find**/**find_one**"

The real `Database` class (`backend/services/database.py`) exposes:
- `insert_one`, `update_one`, `delete_one` ✅
- `find_to_list`, `cursor_to_list`, `aggregate_to_list` ✅
- `get_collection` ✅
- `find` ❌ (does not exist as a class method)
- `find_one` ❌ (does not exist as a class method)

### What happened

`process_jd` faithfully followed the prompt and called `Database.find_one`, which does not exist. Live error from the function sandbox:

```
type object 'Database' has no attribute 'find_one'
```

Both retry attempts failed. `llm_fix_attempted: false` — automatic repair never ran.

### Why it slipped through

The conformance runner (`backend_conformance.py`) patches `Database` with `FakeDatabaseAdapter`, which **does** implement `find` and `find_to_list` (lines 249–266) but **not** `find_one`. So conformance would have failed too — but conformance is advisory (Issue 5 below).

The backend_generator live-rule comment at line 110 says "find_one / insert_one / … await them directly" — correctly listing `find_one` as safe to await. This directly contradicts reality.

### Fix applied

Prompt-only teaching of `collection.find_one` while `Database.insert_one` exists is unstable — models analogize `Database.find_one`. The durable fix is to **make that class method real** and stop teaching the one that still does not exist (`Database.find`).

1. **`database.py`** — added `Database.find_one(collection, filters, projection=None)`, same shape as `insert_one` / `update_one`, Motor + sandbox native-async.
2. **`backend_generator.py` `_runtime_execute_rules` + generation/fixer prompts** — teach `find_one` / `find_to_list`; explicit `NEVER Database.find(...)`. Removed the ambiguous “find_one / insert_one / … await them directly” line that did not say which object.
3. **`ai_processor.py` databaseLogic PLATFORM RULES** — same: `find_one` / `find_to_list`, never `Database.find`.
4. **`_runtime_contract_issue`** — rejects `Database.find(` so codegen retries instead of shipping the AttributeError.
5. **`backend_conformance.py`** — adapter now has `find_one` and patches `Database.find_one`. **Stopped** patching `Database.find`, which previously made conformance succeed for a method the sandbox does not have.

**Tests:** `tests/test_issue1_database_find_one.py`

The original write-up’s item 3 (“add `find_one` only on the fake”) would have hidden the production miss. Fake and real APIs now match.

---

## Issue 2 — `process_jd` codegen ignores `databaseLogic`: wrong collection, wrong params, wrong model

**Layer:** `backend/services/backend_generator.py` (complex domain generation) + `backend/services/backend_conformance.py`  
**Severity:** Critical  
**Blocks workflow:** Yes  
**Status:** ✅ **DONE** (2026-09-21)

### Evidence

`databaseLogic` for `process_jd` (screen1/screen_generations.txt:356–356) explicitly says:
- If `params.is_new` is false → `Database.find_one('job_description', {'jd_id': params.selected_jd_id})`
- Collection: `job_description`
- Gemini model: `gemini-3-flash-preview`
- Retrieve key from `request_meta['credentials']['GEMINI_API_KEY']`

Generated source (screen1/BE/backend_functions.txt) actually does:
```python
jd_id = safe_str(params.get("jd_id", ""))      # ← wrong param, "jd_id" not in request
if not jd_id:
    jd_id = str(uuid.uuid4())                   # ← mints new UUID even for select-existing
collection_name = "job_descriptions"            # ← wrong (plural)
existing_jd = await Database.find_one(...)      # ← doesn't exist
gemini_url = ".../gemini-1.5-flash:..."         # ← deprecated, not gemini-3-flash-preview
gemini_api_key = params.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") # ← wrong
```

### Root cause

`process_jd` is correctly classified as a complex datasource (custom action, long `databaseLogic`). It routes to `gemini-3.1-pro-preview` on attempt 1 (Architecture Issue 2). But the model is not following the `databaseLogic` spec — it invented its own logic. There is no automated check that `params.selected_jd_id` / `params.is_new` from the `ProcessJDRequest` model are actually accessed in the generated code.

The Pydantic model `ProcessJDRequest` has `selected_jd_id` and `is_new` as its top-level fields, but the function reads `params.get("jd_id")` — a field that does not exist in that model. The conformance runner never exercised the `is_new=False` (select-existing) branch.

### Fix applied

The LLM was already given `databaseLogic` and the inputModel field list; it still invented `params.get("jd_id")`, `job_descriptions`, and `gemini-1.5-flash`. Durable fix is an automated spec-fidelity gate that retries, plus a conformance fixture that actually runs the select-existing branch.

1. **`backend_generator.py` `_spec_fidelity_issue`** — after generation, require:
   - required inputModel fields and every `params.<field>` named in `databaseLogic` are read via `params.get` / `params[...]`;
   - Mongo collection string literals match `operation.collection` / read/writeCollections (catches `job_descriptions`);
   - LLM model ids from `databaseLogic` (e.g. `gemini-3-flash-preview`) appear verbatim in source.
   Wired into `_ensure_runtime_contract` so a miss triggers the existing LLM fixer / generate retry instead of shipping.
2. **Generation + fixer prompts** — explicit SPEC FIDELITY block with the exact field names, collection names, and model id for this dataSource.
3. **`backend_conformance.py`** — when the input model has an `is_new`/`create` boolean, run a second fixture `{selected_*: "existing-1", is_new: false, candidate_count: 5}` against a seeded document on the IR collection. Fail unless the identity response field equals `existing-1` (empty `jd_id` + Gemini-key error is no longer a pass). Also fail if `databaseLogic` names a Gemini model that is missing from source. Fake `find_one` / `find_to_list` now honor equality filters so a wrong collection or wrong id cannot return the seeded row.

**Tests:** `tests/test_issue2_spec_fidelity.py`

Credentials (`request_meta["credentials"]`) and `insert_one` vs `update_one` without upsert remain Issues 3 and 4.

---

## Issue 3 — `process_jd` reads Gemini key from env / params instead of `request_meta["credentials"]`

**Layer:** `backend/services/backend_generator.py` + `backend/services/backend_conformance.py`  
**Severity:** High  
**Blocks workflow:** Yes (new JD upload path)  
**Status:** ✅ **DONE** (2026-09-21)

### Evidence

Generated code:
```python
gemini_api_key = params.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
```

`databaseLogic` says:
> "Retrieve `api_key` from `request_meta['credentials']['GEMINI_API_KEY']`"

In the sandbox worker, `GEMINI_API_KEY` is **not** in the environment — it is injected into `request_meta["credentials"]` by the executor. `params` never contains it. Both fallbacks silently return `""`, then the function returns:

```
{"error": "Gemini API key is required", ...}
```

### Root cause

`backend_generator.py` `_runtime_execute_rules` says credentials should come from `request_meta["credentials"]`. The complex domain model (`gemini-3.1-pro-preview`) generated the correct instruction in `databaseLogic` but then wrote the opposite access pattern in code. This is a codegen drift — the LLM wrote env-lookup boilerplate it has memorized from training, overriding the spec. There was no automated reject for `os.environ.get("GEMINI_API_KEY")` / `params.get("gemini_api_key")`, and conformance injected fake keys but still treated a “key is required” short-circuit as an acceptable failure path.

### Fix applied

1. **`_runtime_execute_rules`** — explicit CREDENTIALS rule: always `request_meta["credentials"]`; never `os.environ` / `os.getenv` / `params.get` for secrets in the sandbox.
2. **`_fix_credential_access_patterns` (deterministic)** — rewrites `os.environ.get("GEMINI_API_KEY")` / `os.getenv(...)` / `params.get("gemini_api_key")` (and index forms) to `((request_meta or {}).get("credentials") or {}).get(...)` using declared names + case aliases. Runs inside `_ensure_runtime_contract` before reject/LLM retry — no full regenerate for this class of mistake.
3. **`_credential_access_issue`** — only rejects leftovers rewrite cannot fix (still no `request_meta['credentials']` read).
4. **Generation SPEC FIDELITY prompt** — lists exact credential names from the dataSource.
5. **`backend_conformance.py`** — injects declared names into `request_meta["credentials"]`. If the function still returns a missing-key error while those keys are present, conformance fails.

**Tests:** `tests/test_issue3_credentials.py`

`update_one` without upsert (new JDs not persisted) is Issue 4 (done).

---

## Issue 4 — `process_jd` uses `Database.update_one` without upsert — new JDs not saved

**Layer:** `backend/services/backend_generator.py` + `backend/services/backend_conformance.py` + `backend/services/database.py`  
**Severity:** High  
**Blocks workflow:** Yes (new JD path)  
**Status:** ✅ **DONE** (2026-09-21)

### Evidence

Generated code:
```python
await Database.update_one(
    collection_name,       # "job_descriptions" (also wrong, see Issue 2)
    {"jd_id": jd_id},
    doc
)
```

`Database.update_one` (`backend/services/database.py`) calls `motor.update_one(filters, update_doc)` with no `upsert=True`. The `job_description` collection is empty on a fresh subscriber — `matched_count = 0`, `modified_count = 0`, document silently not saved.

`databaseLogic` says: "Execute `await Database.insert_one('job_description', document)`."

### Root cause

The backend generator used `update_one` (the platform upsert style) instead of `insert_one` for a new document. This is a systematic pattern confusion: the generator treats all writes as update-or-create, but `Database.update_one` is not `upsert` — it only updates matched documents. Conformance hid the miss: `FakeDatabaseAdapter.update_one` appended a document when the store was empty and always returned `matched=1`.

Production `update_one` was **not** changed to `upsert=True` — that would alter every existing update. New documents must `insert_one`, matching `databaseLogic`.

### Fix applied

1. **`_runtime_execute_rules`** — `Database.update_one` is NOT upsert. New documents use `insert_one`; `update_one` only when the filter is guaranteed to match. Same warning on the EXISTING-collection probe branch (Rec3 writes to an already-named collection, so the old INSERT-ONLY stanza was skipped).
2. **`Database.update_one` docstring** — not upsert; zero matched = silent no-write; use `insert_one` for insert-if-new.
3. **Spec fidelity** — if `databaseLogic` / `action=insert|create` requires `insert_one` and generated source never calls it, reject and retry.
4. **`FakeDatabaseAdapter.update_one`** — matches filters like production: empty store → `matched=0`, no auto-insert. Conformance then fails write datasources that only `update_one` when insert is required.

**Tests:** `tests/test_issue4_update_one_not_upsert.py`

---

## Issue 5 — Conformance gate is advisory: `fetch_candidates` (conformance_ok: false) still published

**Layer:** `backend/services/backend_generator.py` + `backend_conformance.py`  
**Severity:** High  
**Blocks workflow:** Yes (Screen 2 load fails)  
**Status:** ✅ **DONE** (2026-09-21)

### Evidence

```json
"conformance_ok": false,
"conformance_error": "Function 'fetch_candidates' returned empty/null required outputs ['user_action'] with no declared error. Generic/empty success is not acceptable."
```

Both `fetch_candidates` v1 and v2 have this flag. Both are published. Screen 2 Angular was built successfully. At runtime, `user_action: ""` will not satisfy the workflow output contract.

This was already documented as "Issue 10" in `LIVE_BUILD_ISSUES_20260918.md` — but it is still unaddressed in Rec3.

### Root cause

Architecture Issue 2 (`FIX_PRIORITY_ANALYSIS.md:176`) explicitly says: *"Compiling bodies are kept even if advisory conformance fails."* This was a deliberate policy decision to avoid blocking valid functions that fail the stub fixture. However, it means structurally wrong functions always ship.

### Fix applied

1. **`run_conformance_gate` returns `failure_kind`** — e.g. `empty_required_output`, `missing_required_output`, `write_not_confirmed`, `missing_credential`, …
2. **`is_blocking_conformance_failure()`** — empty/missing required success outputs, wrong return type, generic CRUD, missing credentials, and select-existing misses are **blocking**. `write_not_confirmed` / `missing_insert` / fixture `raised` stay **advisory** (fake write fixtures and env quirks can miss for valid reasons).
3. **`_create_function_entry`** — on a blocking failure: retry with `CONFORMANCE BLOCKING (...)` as `previous_error`. On the last attempt: set `generation_failed=True`, publish a raising stub (do **not** register the empty-success body as a live function). Advisory failures still publish the compiling body and only record `conformance_ok` / `conformance_error`.

**Tests:** `tests/test_issue5_blocking_conformance.py`

Note: Issue 6 (IR putting `user_action` on a read datasource) is **DONE** — IR
prompt + deterministic strip in `restore_declared_output_requiredness` keep
find/list response models free of UI action fields; Issue 5 still blocks publish
if a broken contract slips through.

---

## Issue 6 — Screen 2 IR forces `user_action` onto read datasource `fetch_candidates`

**Layer:** `backend/services/ai_processor.py` (IR generator) + `backend/services/ir_response_requiredness.py`  
**Severity:** High  
**Blocks workflow:** Yes  
**Status:** DONE

### Evidence

Screen 2 `FetchCandidatesResponse` Pydantic model:
```python
class FetchCandidatesResponse(BaseModel):
    candidates: list[Candidate]
    fetched_count: float
    candidate_count: float
    error: Optional[str] = None
    user_action: str          # ← required field on a list-query response
```

`fetch_candidates` datasource `databaseLogic` also instructs the codegen to `return {..., "user_action": ""}`. This breaks conformance (Issue 5) and structurally misrepresents what a database query returns.

### Root cause

The IR generator interprets the "HARD CONSTRAINT: every interactive field must appear in outputParameters" as applying to `fetch_candidates` when `user_action` is declared in the screen's `outputParameters`. The IR then copies `user_action` into the `FetchCandidatesResponse` model because it sees the output parameter in the node contract. The generator conflates "screen output parameter" (UI action event) with "datasource response field" (database query result). Prompt text also said graph outputs MUST appear on "the dataSource response model," which the LLM applied to every response model including find.

The correct mapping is:
- `fetch_candidates` → returns `{candidates, fetched_count, candidate_count, error}`
- `submit_action` (separate button click datasource) → returns `{user_action}`

The screen has the right split in intent, but IR collapsed both into `FetchCandidatesResponse`.

### Fix (landed)

1. **IR generation prompt:** explicit rule — find/find_one/list/read/aggregate MUST NOT include UI action fields (`user_action`, `submitted`, `confirmed`, `proceed`) in responseModel; those belong on submit/action datasources. Clarified that graph-required outputs pin on the **appropriate** (emit) response model, not every find model. Prefer `user_action` over bare `action` for UI events.
2. **Deterministic repair:** `strip_ui_action_fields_from_read_datasources` + validator `read_datasource_ui_action_field_errors` in `ir_response_requiredness.py`. `restore_declared_output_requiredness` now:
   - treats POST+find as a read query (not a write target for UI actions)
   - honors `outputParameters[].extractFrom.dataSourceId` when adding missing fields
   - never adds/pins UI action fields onto find-only response models
   - strips polluted fields and scrubs empty placeholders (`user_action=''`) from find `databaseLogic` without touching `action: "find"`
3. **Longer term (deferred):** distinct IR datasource purposes (`query` vs `action`).

**Tests:** `tests/test_issue6_ui_action_on_find.py`

---

## Issue 7 — Screen 2 IR invents `submit_action` datasource writing to `workflow_actions`

**Layer:** `backend/services/ai_processor.py` (IR generator)  
**Severity:** Medium  
**Blocks workflow:** Partial (extra write, adds complexity)

### Evidence

Screen 2 IR contains a second datasource `submit_action` that:
- Writes to collection `workflow_actions`
- Records `{user_action, created_at}`
- Was **not described anywhere** in the user's Process_config step description, node `generationDescription`, or `databaseLogic`

The user's spec says: *"Primary action button 'Done' or 'Proceed' emitting `user_action` (string) to transition to the end node."* — i.e., this is a UI state output, not a database write.

### Root cause

IR generator hallucinated a persistence requirement for the "Proceed" button click. When a screen has a UI action output that is also a declared workflow output, the generator infers "this must be saved somewhere" and creates a write datasource. It should instead emit the value directly through the workflow state without a database round-trip.

### Fix

1. **IR generation prompt:** add rule — "A terminal UI action (e.g., 'Done', 'Proceed', 'Submit') that only emits a string value to transition the workflow does NOT need a write datasource. Do not create a datasource whose only purpose is to record the button action string."
2. **IR validator:** flag datasources whose `writeCollections` contains a collection not mentioned in `databaseLogic` / `generationDescription` and whose `outputFieldMappings` only contains `user_action` or similar.

---

## Issue 8 — `fetch_candidates` sort probes use wrong field alias names

**Layer:** `backend/services/backend_generator.py` + `backend/services/backend_conformance.py`  
**Severity:** Medium  
**Blocks workflow:** Yes for sort feature  
**Status:** DONE

### Evidence

`fetch_candidates` v2 generated code:
```python
exp_field = await resolve_field_name(collection_name, "exp")     # ← wrong
dist_field = await resolve_field_name(collection_name, "dist")   # ← wrong
```

`databaseLogic` says: sort by `total_years_of_experience` and `network_distance`.  
`all_Candidates` schema has fields: `total_years_of_experience`, `network_distance`.

`resolve_field_name(collection, "exp")` will probe for a field literally named `exp`, which does not exist, and fall back to the raw string `"exp"` — causing Mongo to sort on a non-existent field (no-op).

### Root cause

The codegen abbreviated the long field names to aliases `"exp"` and `"dist"` while calling `resolve_field_name`. The `resolve_field_name` utility requires the **full logical field name** as the second argument; it is not a fuzzy-match or abbreviation resolver. The databaseLogic spec listed the full names explicitly and they were not used.

### Fix (landed)

1. **Runtime / codegen prompts:** explicit note that `resolve_field_name(collection, logical_name)` requires the EXACT field name (e.g. `total_years_of_experience`, not `exp`). Spec-fidelity prompt section lists declared mapping/sort field names.
2. **Static reject + retry:** `_resolve_field_abbreviation_issue` in `backend_generator` — if a `resolve_field_name` literal is an abbreviation of a field named in `databaseLogic` / `outputFieldMappings`, generation is rejected and repaired. Prefers sort-clause fields when choosing the suggested exact name (so `exp` → `total_years_of_experience`, not `score_breakdown_experience`).
3. **Conformance fixture:** `_seed_find_query_document` seeds find-action fake docs with every field from mappings/sort clauses, using typed samples (numeric for score/years/distance) so mock structure matches the IR schema without breaking float coercion.

**Tests:** `tests/test_issue8_resolve_field_abbreviations.py`

---

## Issue 9 — `fetch_candidates` does not map `experience`/`education` nested arrays

**Layer:** `backend/services/backend_generator.py` + `backend/services/backend_conformance.py`  
**Severity:** Medium  
**Blocks workflow:** Partial (card section empty)  
**Status:** DONE

### Evidence

`fetch_candidates` v2 builds a `candidate` dict but handles `experience` and `education` as simple string fields via `optional_str_fields` list — not as nested arrays. The card spec says "render top 2–3 most recent entries from the `experience` array, showing company name, title, and duration."

`CandidateProfile` model (v2):
```python
education: Optional[list[EducationItem]] = None
experience: Optional[list[ExperienceItem]] = None
```

But in the mapping loop, neither `experience` nor `education` is mapped — the returned candidate dict has neither key, so Angular renders nothing for these card sections.

### Root cause

When the code loops over `optional_str_fields`, `experience` is not included (correctly). But the codegen did not add a separate mapping block for nested array types — it left them out entirely. The conformance runner did not verify that nested object-array model fields are populated.

### Fix (landed)

1. **Backend generator prompt:** explicit pattern for `list[ItemModel]` fields — must map into the return dict; do not omit non-scalars or coerce through string loops.
2. **Static reject + retry:** `_nested_object_array_mapping_issue` — if response/row models declare nested object arrays (`experience`, `education`, …) and source never references those field names, generation is rejected.
3. **Conformance:** find fixtures seed nested arrays onto mock docs; `missing_nested_array` (blocking) fails when mapped rows omit seeded `list[Model]` fields.

**Tests:** `tests/test_issue9_nested_object_arrays.py`

---

## Issue 10 — Tool uses `Database.get_sync_collection()` + sync `update_one($set)` in async sandbox

**Layer:** `backend/services/gap_tool_generator.py` + `backend/services/database.py` + `backend/services/backend_conformance.py`  
**Severity:** High  
**Blocks workflow:** Yes (tool crashes at write step)  
**Status:** DONE

### Evidence

Tool source (`tool1/...json`):
```python
coll = Database.get_sync_collection("all_Candidates")
...
coll.update_one(
    {"linkedin_public_identifier": public_identifier},
    {"$set": doc},
    upsert=True
)
```

Two problems in one:
1. `Database.get_sync_collection` (`database.py:296`) returns a **synchronous PyMongo collection** — it cannot be awaited or used in an async event loop without `asyncio.to_thread`, which is explicitly forbidden in the sandbox.
2. `{"$set": doc}` passed directly — `Database.update_one` helper auto-wraps with `$set`; calling `coll.update_one` on the raw pymongo collection with `$set` is correct raw PyMongo, but the mix of sync + upsert=True risks blocking the async event loop.

The async-sandbox rule (`_runtime_execute_rules`): "NEVER asyncio.to_thread / loop.run_in_executor for MongoDB."

### Root cause

The gap tool generator (`gap_tool_generator.py`) runtime rules prohibit `asyncio.to_thread`, but do not explicitly forbid `get_sync_collection` or sync raw pymongo calls. The LLM chose `get_sync_collection` because it needed `upsert=True`, which `Database.update_one` doesn't support. This exposed a missing feature: **the platform `Database` class has no upsert API**.

### Fix (landed)

Gap tools implement LangChain `BaseTool._run` (synchronous — `await` is a SyntaxError / validator reject). The prescribed `await Database.upsert_one` is correct for async UI/sandbox functions, not for `_run`. Dual API:

1. **`Database` class:** `async def upsert_one(collection, filters, updates)` for sandbox/UI (`await`, native-async collection). `def upsert_one_sync(...)` for gap-tool `_run` via a private sync client (`_ensure_sync_client` — sandbox `connect_db` previously skipped `sync_client`). Plain field dicts; helper wraps `$set`. Sync CRUD siblings: `insert_one_sync` / `find_one_sync` / `update_one_sync` / `delete_one_sync`. `get_sync_collection` kept but deprecated (warning).
2. **Gap tool generator:** prompt forbids `get_sync_collection`, `get_collection`, async `Database.upsert_one`/`insert_one`/`update_one`, and raw `coll.update_one(..., upsert=True)`; teaches `Database.upsert_one_sync`. Validator (`_MOCK_PATTERNS`) rejects those (so stripping `await` and calling async helpers cannot silently no-op). Example 3 rewritten to class methods.
3. **Conformance:** `FakeDatabaseAdapter.get_sync_collection` raises `AttributeError`. Fake `upsert_one` / `upsert_one_sync` insert-or-update. UI `_runtime_execute_rules` + `_runtime_contract_issue` reject `get_sync_collection` and teach `await Database.upsert_one`.

**Tests:** `tests/test_issue10_upsert_one.py`

---

## Issue 11 — Tool auth: `jwt_token` used as Unipile `X-API-KEY`; `account_id` undeclared

**Layer:** `backend/scratch_graph_planner.py` (planner) + `backend/services/gap_tool_generator.py`  
**Severity:** High  
**Blocks workflow:** Yes (every Unipile API call fails)

### Evidence

Process_config.txt node `profile_fetching_tool` input bindings:
```json
"jwt_token": "{{global.jwt_token}}"
```

Tool source uses `jwt_token` as the Unipile `X-API-KEY`:
```python
r = requests.get(loc_url, headers={"X-API-KEY": jwt_token})
```

Tool also reads `account_id` from credentials:
```python
account_id = creds.get("UNIPILE_ACCOUNT_ID")
```

`UNIPILE_ACCOUNT_ID` is **never declared** as a credential in the Process_config `requiredGlobalInputs` or `available_credentials`. `jwt_token` is the platform user's JWT, not the Unipile API key.

The user's step description says: `X-API-KEY: {key}` where `{key}` is the Unipile API key — a separate credential from the platform JWT.

### Root cause

Stage 0 / Stage 4 credential resolution saw `jwt_token` as the only available credential and mapped it to the Unipile auth header. The planner did not ask the user for `UNIPILE_API_KEY` / `UNIPILE_ACCOUNT_ID` separately. Both are Unipile-specific — without Stage 0 asking, the tool generator had to invent the credential name.

This is related to the known limitation in Architecture Issue 3: *"When Stage 0 leaves a private API empty (`unknown`), codegen may still invent a host at generation time."*

### Fix

1. **Stage 0:** when step description contains a named external API (here: `api20.unipile.com`) and includes URL path segments with `{account_id}`, Stage 0 must ask:
   - "What is your Unipile API Key?"
   - "What is your Unipile Account ID?"
   As separate credentials, not fold both into `jwt_token`.
2. **Planner:** `{account_id}` appearing in a providerOperation URL should be auto-detected as a required input — add it to `requiredGlobalInputs` or raise a Stage 0 clarification question.
3. **Gap tool generator:** if `jwt_token` appears in `inputBindings` but the description refers to a third-party API key (detected by provider name ≠ "platform"), warn and surface in `provider_spec_unverified`.

---

## Issue 12 — Graph `capabilityRef.version: null`, `class_path: null` on tool node

**Layer:** `backend/agent_new.py` (`_enrich_tool_nodes_with_source_and_class_path`)  
**Severity:** High  
**Blocks workflow:** Yes (LangGraph cannot dispatch the tool)  
**Status:** DONE

### Evidence

Process_config.txt `profile_fetching_tool` capabilityRef:
```json
"capabilityRef": {
    "capabilityId": "tool:profile_fetching_tool_22304660_sb_86dc311c",
    "kind": "tool",
    "version": null,
    "class_path": null
}
```

The tool JSON exists in `tool1/`. Capability was registered and saved (the `_resolve_gap_generations` returned `new_cap_id`). But the enrichment step that sets `class_path` and `version` from the capability registry did not populate the graph node.

The LangGraph emit code (`agent_new.py:18176`) calls `_enrich_tool_nodes_with_source_and_class_path` after gap resolution. That function should look up the registered capability by `capabilityId` and set `class_path`. It did not for this tool.

### Root cause

Likely: the capability was registered under the subscriber tenant, but the enrichment lookup used a different tenant (all-null platform) — exactly the "Issue A: tenant-inconsistent lookup" from `FIX_PRIORITY_ANALYSIS.md:820–830`. This is Architecture Issue A, still open.

### Fix (landed)

Architecture Issue A instance: gap tools are stored under the subscriber tenant, but enrichment looked them up with an all-null platform tenant (`get_latest_capability_by_id` then only matches platform/legacy-null rows). Lookup missed, and the function `continue`d with `class_path: null`. Callers also swallowed enrichment errors.

1. **Same-tenant lookup:** `_enrich_tool_nodes_with_source_and_class_path` uses the request `tenant` (subscriber + platform `$or`, same as 6p / registration).
2. **Registration:** gap/user-authored capability docs write `class_path: dynamic:<toolKey>` and `version`. Generator returns `{capabilityId, version, class_path, source}`; `_apply_gap_resolution_success` pins those on the graph node immediately (not a hardcoded `"1"`).
3. **No silent null / no clobber:** on lookup miss, synthesize `dynamic:<toolKey>` only when `class_path` is empty — never overwrite an existing langchain import path. If `class_path` is still empty after merge, enrichment raises. Plan/execute no longer swallow that error.

**Tests:** `tests/test_issue12_capability_class_path.py`

---

## Issue 13 — Persistence self-join link duplicated 4× in `databaseLogic` — crowds codegen context

**Layer:** `backend/services/persistence_link.py` + `backend/services/ai_processor.py`  
**Severity:** Medium  
**Blocks workflow:** Indirect (degrades codegen quality)  
**Status:** ✅ **DONE** (2026-09-22)

### Evidence

Screen 1 `process_jd` databaseLogic contains 4 identical PINNED PERSISTENCE LINK blocks, each repeating:
```
collection: job_description
writer.nodeId: jd_input_screen / correlationKey: jd_id / ...
reader.nodeId: jd_input_screen / filterKey: jd_id / ...
RULE: every document written...
RULE: when fetching...
```

Graph-level `persistenceLinks` also has the same link duplicated twice (cardinality: `one_to_many` instead of `self`).

### Root cause

Issue 5 (`LIVE_BUILD_ISSUES_20260918.md`) fixed the self-join so it compiles a `persistenceLink`. But `attach_persistence_links_to_graph` appended the same self-join once for the writer and once for the reader (same node) — so `persistenceLinks` and the pin text had Link 1 + Link 2 identical. Re-stamp / LLM copy then stacked full PINNED headers (up to 4×) into `databaseLogic`. Extract also defaulted cardinality to `one_to_many` instead of `self`.

### Fix (landed)

1. **`dedupe_persistence_links`:** keep one link per `(collection, writer.nodeId, writer.key, reader.nodeId, reader.key)`; normalize same-node links to `cardinality="self"`.
2. **`compile` / `extract` / `format_pinned` / `links_from_dicts`:** all go through dedupe + self cardinality.
3. **`attach_persistence_links_to_graph`:** do not double-append self-joins; strip prior PINNED blocks before re-pinning `generationDescription`.
4. **`stamp_persistence_links_on_datasources`:** strip prior PINNED blocks then stamp exactly once (no stacking).
5. **`ai_processor` enricher prompts:** copy PINNED block at most once.

**Tests:** `tests/test_issue13_persistence_dedupe.py`

---

## Issue 14 — Planner providerOperations have trailing backticks in URLs, marked `verified: true`

**Layer:** `backend/services/provider_operation.py` (`extract_provider_operations_from_text`) + planner pin path  
**Severity:** Medium  
**Blocks workflow:** Indirect (misleads tool generator)  
**Status:** ✅ **DONE** (2026-09-22)

### Evidence

Process_config.txt tool `providerOperations`:
```json
{
    "endpoint_url": "https://api20.unipile.com:15002/v2/{account_id}/linkedin/search/people?limit=10&cursor={next_cursor}`",
    "http_method": "POST",
    "verified": true
}
```

The backtick at the end of the URL is a Markdown code-fence artifact from the original step description (where the URL was wrapped in triple-backticks). The extractor pulled the URL including the closing backtick. These operations are marked `verified: true` with empty `provider` and `auth_header`.

### Root cause

`extract_provider_operations_from_text` used a regex that included trailing `` ` `` as part of the URL, then set `verified: true` for description-parsed ops. Empty `provider` + `verified: true` told the tool generator the malformed URL was confirmed.

### Fix (landed)

1. **`sanitize_endpoint_url`:** strip leading/trailing backticks, quotes, slashes, and punctuation; preserve `{template}` braces and trailing `)` in paths. URL regex no longer matches backticks.
2. **Description-parsed ops:** always `verified: false` (`source="description"`).
3. **`finalize_provider_operation`:** `verified: true` only for Stage 0 (always when provider set) or user (explicit flag) **with** a non-empty `provider`; empty provider / grounding / description force `verified: false`. Applied on extract, Stage 0, grounding, and `operations_from_dicts` (repairs persisted bad rows; missing `source` + provider + verified infers `stage0`).

**Tests:** `tests/test_issue14_provider_url_verified.py`

---

## Issue 15 — Screen 2 header `candidate_count` shows 0 ("Showing X of Y requested")

**Layer:** Stage 2 IO schema (scratch planner) + IR / screen FE-BE (not Stage 4c)  
**Severity:** Medium  
**Blocks workflow:** Partial — "Showing X of Y requested" in Screen 2 header always shows 0  
**Status:** ⬜ **open** — original diagnosis **reframed** (2026-09-22 recheck); not implemented yet

### Evidence (symptom is real)

Tool `declaredOutputs`:
```json
{"name": "candidates_batch_id"}, {"name": "fetched_count"}
```

Screen 2 graph `inputBindings` only bind those tool outputs:
```json
"candidates_batch_id": "{{profile_fetching_tool.candidates_batch_id}}",
"fetched_count": "{{profile_fetching_tool.fetched_count}}"
```

`candidate_count` is absent from the graph. Screen 2 IR still declares it as a `required` input (`workflowContext.candidate_count`). Header defaults to 0.

### Recheck (2026-09-22) — original root cause was wrong

1. **Not Stage 4c.** Stage 4c is persistence registry. Graph wiring is Stage 2 (`inputParams` + `boundTo`) → Stage 4 copies into `inputBindings` → Stage 4b repairs empty **declared** required inputs.
2. **Skip-level binding already works.** Screen 1 → tool already has `candidate_count: {{jd_input_screen.candidate_count}}`. Another LinkedIn run correctly bound Screen 2 `candidate_count` → `jd_input_screen.candidate_count`. Rec3 Stage 2 simply never declared that Screen 2 input — so the validator had nothing to flag.
3. **Tool echo workaround is wrong.** Correct graph bind is `{{jd_input_screen.candidate_count}}`, not echoing through the tool.
4. **AI processor / FE amplify it:** IR invents required `candidate_count`; `fetch_candidates` redefines it as `count_documents(batch)` and FE overwrites `onInput` with that response — so a planner-only bind would still be clobbered after fetch.

### Fix (revised — not landed)

1. **Stage 2:** when Screen 2 description needs "requested" count, declare `candidate_count` input bound to `jd_input_screen.candidate_count`.
2. **IR / FE-BE:** do not invent required inputs the graph did not wire; `fetch_candidates` must not redefine `candidate_count` as batch document count if the header means "requested."
3. Do **not** add a Stage 4c tracer for this; do **not** force the tool to echo `candidate_count` as the primary fix.

---

## Issue 16 — Tool stores `actively_looking: "yes"/"no"`, Screen 2 badge expects `"Yes"/"No"`

**Layer:** `backend/services/gap_tool_generator.py`  
**Severity:** Low  
**Blocks workflow:** Partial — "Open to Work" badge never renders

### Evidence

Tool source:
```python
"actively_looking": "yes" if is_open_to_work else "no"
```

Screen 2 card spec (generationDescription + IR actions):
```
conditionally rendered if `actively_looking` == 'Yes'
```

Angular template will check `candidate.actively_looking === 'Yes'` — lowercase `"yes"` never matches.

### Root cause

The spec says `"Yes"` / `"No"` (capitalized). The tool generator produced lowercase. There is no cross-artifact schema validation at write time that enforces the same enum casing between the writer (tool) and reader (screen IR).

### Fix

1. **Gap tool generator prompt:** when `databaseLogic` or the description specifies exact string values (e.g., `"Yes"`, `"linkedin_sourced"`), include a reminder: "String constants must match EXACTLY — case-sensitive. Use `"Yes"` not `"yes"`, `"No"` not `"no"`."
2. **Schema cross-check:** the persistence lineage object already tracks `identityKey`. Extend it to also track `enumValues` for key status fields — then the 6p validator can compare tool output enum values against screen IR expected enum values.

---

## Systemic root causes (cross-issue patterns)

Three structural problems underlie most of the above:

### S1 — `Database` API surface is undocumented and inconsistent with the codegen prompt

The prompt says `find`/`find_one` work; they don't. The prompt says `update_one` is for updates but doesn't mention it is not upsert. The `Database` class has no `upsert_one`. This gap forces codegen to use raw collection access or non-existent methods. **Fix: make the Database API surface the prompt is allowed to teach exactly match the real class.**

### S2 — Conformance gate is advisory — structurally wrong functions always ship

`conformance_ok: false` functions are currently published without retry. For structurally invalid returns (empty required output, non-existent method calls), conformance must be blocking. For advisory failures (live HTTP timeout in fake adapter), it can remain advisory. The two failure modes are distinguishable. **Fix: classify conformance errors as blocking vs advisory; block on structural failures.**

### S3 — No cross-artifact schema validation between tool writer and screen reader

The tool stores field values (`"yes"/"no"`, missing `candidate_count`, truncated experience array). The screen reads those values expecting different shapes. Neither IR validation nor 6p checks the actual data contract between what the tool writes into MongoDB and what the screen's datasource expects to read. **Fix: the persistence lineage object should carry not just key names but also field shapes and enum values, with a 6p check that writer and reader agree.**

---

## Priority order for fixes

| Priority | Issues | Estimated impact |
|---|---|---|
| P0 — Blocking Screen 1 | 1, 2, 3, 4 | Screen 1 submit works for both existing-JD select and new-JD upload |
| P0 — Blocking Screen 2 | 5, 6 | Screen 2 actually loads candidates |
| P1 — Blocking tool execution | 10, 11, 12 | Tool can call Unipile and write candidates |
| P2 — Graph correctness | 7, 13, 14, 15 | Bindings complete; no spurious write |
| P3 — Feature correctness | 8, 9, 16 | Sort, experience cards, Open to Work badge work |
| Systemic | S1, S2, S3 | Prevents same class of errors on future workflows |
