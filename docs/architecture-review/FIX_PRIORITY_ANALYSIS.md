# Fix Priority Analysis — Workflow Generator
_Cross-referenced against forensic RCA, architecture review, and live code._  

**Implementation status (2026-09-18):** Issues **1**–**9** are **done**. Items A–C remain open.

Issue 5 **runtime** (interpret error dicts; pause; do not store failure as success) is closed. Two follow-ons are **outside Issue 5** and still open: (1) frontend `tool_error` / retry UI, (2) IR success/error union + explicit graph error edges.

---

## How to read this document

Each issue is evaluated on three axes:

| Symbol | Meaning |
|---|---|
| ✅ | User's proposed solution is correct as stated — implement it. |
| ⚠️ | User's proposed solution is on the right track but incomplete or misses the precise root cause — refined below. |
| ❌ | User's proposed solution would not fix the root cause or would introduce new problems — alternative given. |

Issues are ordered by leverage (how many distinct failure modes a single fix eliminates), not by the user's original numbering.

---

## Issue 1 — Intent / Prompt Degradation

**User severity rating:** Extremely important  
**Actual severity:** Critical — this is the root cause of entire screen classes being generated wrongly  
**Status:** ✅ **DONE** (2026-09-15)

### What the user said

> Make node descriptions good enough in Stage 1 / 1.2. Prompt enhancer should retain everything. IR schema generator should add a second LLM call to enrich `databaseLogic`.

### Assessment: ⚠️ Partially correct, but misses the decisive root cause

The user's solution targets Stage 1.2 (description enricher). That is a real problem, but it is **not where the Screen 3 failure actually happened**. The Screen 3 description was correctly written by Stage 1.2. It was later **deterministically overwritten** inside `/agentic/plan/execute`, in `_resolve_gap_generations` (`agent_new.py:8200–8227`). The code builds a string like:

```python
gen_desc = " ".join(desc_parts) + " to render a user interface."
node.config.generationDescription = gen_desc          # ← overwrites the rich description
node.config.generation.description = gen_desc         # ← overwrites it again
```

This is not LLM variance. It is a deterministic code path that runs on every downstream UI screen during gap resolution. No improvement to Stage 1.2 survives it.

### Two distinct sub-problems and their correct fixes

#### 1a — The execute-time overwrite (highest severity)

**Root cause:** `_resolve_gap_generations` conflates two separate concerns:
- updating I/O bindings when upstream outputs change (legitimate)
- rewriting the semantic/UX description of the screen (should never happen)

**Correct fix:**
Split `generation` into two immutable/mutable halves:
```python
generation.intentDescription  # immutable — set at scratch/preview, never touched again
generation.ioSummary          # mutable — recalculated by _resolve_gap_generations
```

`_resolve_gap_generations` must update only `ioSummary` and merge it into the generator prompt by appending, not replacing. The `AIProcessor` / `generate_ir_schema` should receive `intentDescription + ioSummary` concatenated, not just the I/O sentence.

Additionally, add an `acceptanceCriteriaIds` set at planning time — a list of feature IDs extracted from the user's requirements for that node. Validate before generation that every ID appears in the prompt. Fail and alert if any are missing.

#### 1b — Stage 1.2 description fidelity (important but secondary)

The user's point about the prompt enhancer and `databaseLogic` enrichment is valid. The enricher should:
- extract all explicit DB operations (collection name, operation type, schema) and make them literal in the description, not prose
- extract all external API calls (provider, method, endpoint, request/response format) as literal spec blocks
- never summarize or paraphrase exact constants, enums, algorithms, or required field names

A second LLM call to enrich `databaseLogic` is a reasonable mechanism for this. The prompt must include all collection schemas already known to the registry before this call runs (Stage 1.7 already does the architecture — Stage 1.2 should consume its output).

### Implementation completed (Issue 1)

#### 1a — Immutable intent + stop overwrite

| Change | Files |
|---|---|
| Added `GraphNodeConfig.intentDescription` (immutable original UX/domain intent) | `backend/agent_new.py` |
| Set `intentDescription` at Stage 4 assembly alongside `generationDescription` | `backend/scratch_graph_planner.py` |
| Whitelisted `intentDescription` for Mongo screen metadata | `backend/repositories/screen_repository.py` |
| Helpers: `_is_generic_io_summary_description`, `_best_intent_description`, `_ensure_intent_description`, `_frozen_intent_description` | `backend/agent_new.py` |
| `_resolve_gap_generations` no longer overwrites rich specs with I/O sentences; I/O is appended via typed contract in the generator prompt | `backend/agent_new.py` |
| `_resolve_one_gap_node` prefers frozen intent for UI **and** tool generation prompts | `backend/agent_new.py`, `backend/services/gap_tool_generator.py` |
| Persist intent on first UI generation and on update paths | `backend/agent_new.py` |
| `/plan/update`, regeneration, and keep-paths use `_frozen_intent_description` so long LLM rewrites cannot beat the original | `backend/agent_new.py`, `backend/regeneration.py` |

**Not done (deferred):** `acceptanceCriteriaIds` + pre-generation fidelity validation — separate hardening, not required to stop Screen 3 overwrite.

#### 1b — Description fidelity through generators

| Change | Files |
|---|---|
| Stage 1.2 FIDELITY RULES: preserve user-stated facts verbatim; missing-schema → runtime inspect note | `backend/scratch_graph_planner.py` |
| Prompt enhancer: VERBATIM PRESERVATION + DB/API operation rules; **deterministic prepend** of original node spec so LLM enhancement cannot drop facts | `backend/services/ai_processor.py` |
| IR Step 5.5 dataSource enricher: FIDELITY RULES so `databaseLogic` keeps API/DB field detail (existing enricher reinforced) | `backend/services/ai_processor.py` |

#### API / provider specs (Issue 1 extension + overlaps Issue 3)

User clarified: do **not** tell the LLM to “write a note that docs are needed.” Resolve specs for real:

1. User provides endpoint / request / response in the prompt → preserve verbatim in node descriptions.
2. If API is required but details missing → Stage 0 asks (especially private/internal APIs).
3. Last fallback at code-gen → Google Search grounding with guardrails.

| Change | Files |
|---|---|
| Extended `ClarifiedExternalAPI` (`endpoint_url`, `http_method`, `request_schema`, `response_schema`, `is_public`) | `backend/stage0.py` |
| Stage 0-B: ask for private API specs; public APIs auto-resolve for later grounding | `backend/stage0.py` |
| `_resolve_external_api_spec_with_grounding` when `external` DS has no concrete URL in `databaseLogic` | `backend/services/backend_generator.py` |

Removed earlier “add provider-docs note into the prompt” anti-pattern from Stage 1.2 / enhancer / DS enricher.

#### End-to-end chain now protected

```
user prompt
  → Stage 0 (API questions when needed)
  → Stage 1 / 1.2 (rich node descriptions + fidelity)
  → Stage 4 (intentDescription frozen)
  → gap resolve (intent + typed I/O; no overwrite)
  → prompt enhancer (original prepended)
  → IR + databaseLogic enricher
  → UI / tool / backend generators
  → grounding if external API still underspecified
```

---

## Issue 2 — Backend Generation: Generic CRUD Fallback

**User severity rating:** High  
**Actual severity:** Critical — directly caused Screen 1 to return empty data for the entire first run  
**Status:** ✅ **DONE** (2026-09-16) — implementation + unit tests + live LLM retest

### What the user said

> Remove semantic fallbacks. Prohibit generic CRUD. Try again / change model / change approach. Run generated function with fake adapters. Assert actual keys.

### Assessment: ✅ Correct — and the implementation is specific enough to execute

The code path that must be removed:

```python
# backend_generator.py:4044–4047
logger.warning("LLM generation failed for %s, using template fallback", func_id)
if ds_type == "external":
    return self._build_external_template(func_id, data_source)
return self._build_dynamic_template(func_id, data_source)   # ← this line
```

`_build_dynamic_template` derives only `operation.action` (defaulting to `"insert"`) and returns `recordId / collectionName / status`. It ignores `databaseLogic`, custom branching, external calls, and all declared outputs. Any data source that has a non-trivial `databaseLogic` description should never be allowed through this path.

**Refined implementation plan:**

1. **Gate on `databaseLogic` complexity.** If the data source has a non-trivial `databaseLogic` description or a custom operation type, raise a generation error instead of falling back. Log the exact LLM failure reason. Retry with a stronger model.

2. **Add an executable conformance gate.** After generation, run the function body against a fake adapter that validates:
   - every declared output key is present in the return value
   - required outputs are non-null/non-empty
   - any declared DB side effects (`writeCollections`) produced at least one document

3. **Fail loudly on no compiling source.** If the LLM never produces compiling code after retries, mark `generation_failed` and register a callable that raises — never publish the old CRUD template. *(Conformance is advisory: a compiling domain body is kept even if the fake-adapter gate fails; results are recorded on `conformance_ok` / `conformance_error`.)*

### Implementation completed (Issue 2)

| Change | Files |
|---|---|
| Removed generic CRUD / HTTP **template fallback** from `_generate_function_source` and `_create_function_entry`. Empty LLM output no longer publishes `recordId/collectionName/status`. | `backend/services/backend_generator.py` |
| Executable conformance gate (advisory): run compiled function against fake `Database` + `httpx`/`requests` adapters for diagnostics; record `conformance_ok` / `conformance_error`. **Does not discard** a compiling function. | `backend/services/backend_conformance.py`, `backend/services/backend_generator.py` |
| Model routing: **complex** data sources (LLM/HTTP/branching/`custom`/`databaseLogic` ≥ 200 chars via `is_nontrivial_domain_datasource`) start on `gemini-3.1-pro-preview` from attempt 1 (generate + fixer). Simple CRUD stays on `gemini-3.6-flash` until scratch retry escalates to pro. | `backend/services/backend_generator.py`, `backend/config/model_config.py` (`backend_generation_complex`) |
| After all retries with **no compiling LLM source**: persist last attempt with `generation_failed=True`; register a callable that raises. Compiling bodies are kept even if advisory conformance fails. | `backend/services/backend_generator.py` |
| Metadata `generation_failed` / `conformance_ok` / `conformance_error` stored on the function artifact. | `backend/services/backend_generator.py` |

**Tests:** `tests/test_backend_issue2_conformance.py` (CRUD prohibition, advisory conformance, complex→pro / simple→flash routing), `tests/test_backend_function_fixer.py`.

**Live verification (2026-09-16):** `backend/scripts/run_issue2_live_backend_gen.py`  
Artifacts: `backend/scripts/eval_output/issue2_live_backend_20260916_054625/`

| Case | First model | Compile | Generic CRUD | Conformance | Result |
|---|---|---|---|---|---|
| Screen 1 `process_job_search_config` (forensic IR) | `gemini-3.1-pro-preview` | yes | no | pass | PASS |
| Walkthrough `fetch_candidates` (thick `databaseLogic`) | `gemini-3.1-pro-preview` | yes | no | pass | PASS |

Domain code for Screen 1 branched on `jd_source_mode`, used `request_meta["credentials"]` for Gemini, and called `insert_one` — not `recordId`/`collectionName` template output.

---

## Issue 3 — Tool Generation: Provider Calls Not Grounded

**User severity rating:** High  
**Actual severity:** High — caused zero candidates for all of the first working Screen 1 runs  
**Status:** ✅ **DONE** (2026-09-16) — pin + fixture + three-way spec classification + Stage 0 no-invent gate; live gap-tool and Stage 0→backend evals passed (with D fixed in Stage 0).

### What the user said

> New API endpoint info should be in user prompt. If not then Stage 0 clarification should ask for request/response format. If still missing at backend_generator, use grounding.

### Assessment: ⚠️ The Stage 0 clarification and grounding ideas are right; the "search the web" path needs guardrails

The Unipile failure: user-stated DSN `api20.unipile.com:15002` + POST people-search was lost; tool codegen used default `https://api.unipile.com` GET `/people/search`. Compile-only validation could not catch it.

Grounding must **not** run for every API — the LLM already knows well-known public APIs (Stripe, OpenWeatherMap, …). Grounding is a **last resort** for truly unknown HTTP services.

### Layers (all now implemented)

0. User-stated URLs/methods/request/response are copied into Stage 0 `ClarifiedExternalAPI` even for public APIs, and **planner handoff includes endpoint/method/schemas** (previously only `provider — purpose`).
1. Stage 1.2 already has verbatim URL/method preservation (Issue 1). Stage 4 attaches structured `providerOperations` on GENERATE:tool nodes from description + Stage 0 handoff.
2. Gap tool prompt injects **PINNED PROVIDER OPERATION**. Generated `function_code` is checked against the fixture (host, method, distinctive path). Mismatch retries; last attempt still publishes (cannot block draft) with `provider_fixture_error` (and `provider_spec_unverified` only when the spec came from grounding).
3. **`classify_provider_spec()`** (pinned / model_known / unknown) decides grounding — replaces the old `_has_endpoint_url` `://` substring check (which misfired on host:port DSNs and on stray `https://` guesses).
4. Stage 0 **never keeps invented private hosts**: prompt forbids inventing endpoints when the user withholds docs; `strip_unattested_api_endpoints()` removes any `endpoint_url` not attested in the prompt or user answers.

### Spec classification (codegen gate)

| Class | Meaning | Grounding |
|---|---|---|
| `pinned` | User / Stage 0 gave a concrete host (URL **or** `host:port` DSN) | **Never** — inject pinned spec |
| `model_known` | Well-known public API name, no custom host | **Never** — LLM uses training knowledge |
| `unknown` | HTTP required; no host; not a known public provider | **Once** as last resort |

### Implementation completed (Issue 3)

| Change | Files |
|---|---|
| `ProviderOperation` extract; HTTP fixture matcher; REST grounding helper; `classify_provider_spec` + well-known public API set | `backend/services/provider_operation.py` |
| Stage 0 handoff emits endpoint/method/request/response; public APIs keep user-stated URLs; private APIs may complete with empty fields when user withholds; `strip_unattested_api_endpoints` on 0-B completion | `backend/stage0.py` |
| Tool nodes get `providerOperations` at Stage 4 graph assembly | `backend/scratch_graph_planner.py`, `GraphNodeConfig` in `backend/agent_new.py` |
| Gap tool: pin spec, classify before grounding, fixture retry, capability flags | `backend/services/gap_tool_generator.py` |
| Backend generator: same three-way classify; delete `_has_endpoint_url`; pin inject / model_known skip / unknown ground | `backend/services/backend_generator.py` |

**Unit tests:**
- `tests/test_issue3_provider_operation.py` — extract, fixture, classify (pinned DSN, model_known Stripe/OWM, unknown internal)
- `tests/test_stage0_no_invent_endpoints.py` — strip invented `api.internal-crm.com`; keep Unipile DSN and user-supplied CRM URL

**Live evals (real Gemini):**

| Script | What | Result |
|---|---|---|
| `backend/scripts/run_issue3_live_gap_tool_gen.py` | Gap-tool Unipile forensic case (`linkedin_sourcing_tool`) | **PASS** — attempt 1/3; `api20.unipile.com:15002`; fixture pass. Artifacts: `issue3_live_gap_tool_20260916_064350/` |
| `backend/scripts/run_issue3_live_stage0_backend_grounding.py` | Stage 0 QnA (scripted answers) → classify → backend codegen | A/B/C **PASS** (pinned Unipile, model_known OpenWeather, private CRM user-provided). D exposed Stage 0 inventing a host → fixed via strip gate. Artifacts + notes: `issue3_live_stage0_backend_20260916_081353/` |

### Known remaining limitations (out of Issue 3 scope / deferred)

- Fixture is substring-based (not live HTTP).
- Gap-tool last retry can still publish with `provider_fixture_error` (draft not blocked).
- When Stage 0 leaves a private API empty (`unknown`), codegen may still invent a host at generation time unless a separate missing-spec soft-fail path is added later — Issue 3 Stage 0 gate only ensures the **handoff** no longer carries invented endpoints.

---

## Issue 4 — Persistence Key Mismatch Between Writer and Reader

**User severity rating:** High  
**Actual severity:** High — latent, would have surfaced even after all other fixes  
**Status:** ✅ **DONE** (2026-09-16) — typed `persistenceLink` compiled at Stage 4c from graph binding + registry; pinned into gap-tool, IR, and backend generators; quoted-field fixture rejects `jd_id` vs `candidates_batch_id` mismatch; unit + live Gemini evals PASS.

### What the user said (Solution 1)

> Replace free-text persistence with explicit typed operations — writer.collection, writer.correlationKey, reader.collection, reader.filterKey, valueSource. Compile both from the same model.

### What the user said (Solution 2)

> Each node dealing with CRUD should be aware of its schema in LLM context.

### Assessment: Solution 1 ✅ is correct and complete. Solution 2 ⚠️ is also necessary but not sufficient on its own.

Solution 2 (schema awareness) is already partially in place via Stage 1.7 (Persistence Architect) and the `knownCollectionSchemas` injection. The problem is that even when the schema is in context, independently generated capabilities can produce inconsistent field names for the same logical join key.

Solution 1 (typed lineage object) is the correct architectural fix:

```yaml
persistenceLink:
  writer:
    nodeId: "sourcing_tool"
    collection: "all_Candidates"
    correlationKey: "candidates_batch_id"
    valueExpression: "{{jd_id}}"
  reader:
    nodeId: "screen_3"
    collection: "all_Candidates"
    filterKey: "candidates_batch_id"
    valueSource: "{{sourcing_tool.candidates_batch_id}}"
```

This object should be:
- authored by Stage 1.7 (Persistence Architect) at planning time, not generated independently
- validated for key-name equality before either capability is generated
- passed explicitly to both the gap tool generator and the screen IR generator
- referenced in the post-gap 6p validation by checking: does a reader's `filterKey` have a matching `correlationKey` from any writer in the same lineage chain?

The additional RC-14 concern (cross-JD candidate identity) applies here too: the lineage model should also encode cardinality — whether the join is one-to-one, one-to-many, or requires a compound key. This prevents the same public identifier from having JD-specific scores overwritten across runs.

### Implementation completed (Issue 4)

Stage 1.7 / 4c already recorded collections, fields, writers, readers, and `filter_keys` — but those were independent LLM extracts. The LinkedIn failure was: tool wrote Mongo `jd_id`, returned graph output `candidates_batch_id = jd_id`, Screen 3 queried Mongo `candidates_batch_id` → 0 rows. Graph bindings were correct; the missed binding was between a workflow value and a physical Mongo field.

The compiler prefers the **graph-declared** output/input name (`candidates_batch_id`) over registry `filter_keys` (`jd_id`). Both generators then persist/query that same physical key.

| Change | Files |
|---|---|
| `PersistenceLink` / `PersistenceEndpoint`; compile from graph `inputBindings` + registry; lineage validate; pin formatter; quoted-field fixture; stamp onto IR dataSources | `backend/services/persistence_link.py` |
| Stage 4c compiles links after registry merge, stamps `config.persistenceLinks` + pin on `generationDescription`, stores phase-level `persistenceLinks` | `backend/scratch_graph_planner.py` |
| Typed `GraphNodeConfig.persistenceLinks` + `GraphDefinition.persistenceLinks` so Pydantic does not drop the contract | `backend/agent_new.py` |
| Gap-tool: inject PINNED PERSISTENCE LINK; fixture retry; persist links on capability | `backend/services/gap_tool_generator.py` |
| UI gap prompt appends the pin (intentDescription stays immutable); stamp links onto IR dataSources before backend gen | `backend/agent_new.py` |
| Backend generator: inject pin; fixture retry (advisory on last attempt); `persistence_fixture_error` metadata | `backend/services/backend_generator.py` |
| IR DS enricher FIDELITY RULE 5: copy pinned collection + join key verbatim into `databaseLogic` | `backend/services/ai_processor.py` |

**Unit tests:** `tests/test_issue4_persistence_link.py` — **15/15 OK**
- LinkedIn compile prefers `candidates_batch_id` over registry `jd_id`
- one join key per writer→reader (ignores `fetched_count` bindings)
- writer key == reader key; identityKey stays `linkedin_public_identifier`
- lineage error when readers exist with no writers
- attach stamps node config + generationDescription
- writer fixture rejects persist-`jd_id` / return-`candidates_batch_id`; accepts persist-`candidates_batch_id`
- reader fixture rejects query-`jd_id`; accepts query-`candidates_batch_id`
- pin text round-trip + IR dataSource stamp

**Live verification (2026-09-16):** `backend/scripts/run_issue4_live_persistence_gen.py`  
Real Gemini on the same gap-tool / backend codegen entrypoints execute uses (not a full `/scratch/preview` → `/plan/execute` run).  
Artifacts: `backend/scripts/eval_output/issue4_live_persistence_20260916_150005/` (`overall_ok: true`)

| Scenario | Path | Temptation | Result |
|---|---|---|---|
| A `compile_linkedin_graph` | Stage 4c compiler on intent-trace `final_graph` (no LLM) | registry `filter_keys` include `jd_id`; graph binding is `batch_id` | **PASS** — correlation key = `batch_id`; pin stamped on writer + reader |
| B `writer_synthetic` | `gap_tool_generator` | description says persist Mongo `jd_id`, return `candidates_batch_id` | **PASS** — fixture retry; final code quotes `"candidates_batch_id"` |
| C `reader_linkedin_ir` | `BackendGenerator._create_function_entry` on `fetch_candidates` | IR `databaseLogic` tempts filter on `jd_id` | **PASS** — query uses pinned `batch_id` |
| D `writer_linkedin_tool` | `gap_tool_generator` on LinkedIn sourcing node (no Unipile; fake profiles) | registry / domain text tempts `jd_id` | **PASS** — documents persist `"batch_id"` |

**Bugs found in live testing and fixed before final PASS:**
- Compiler emitted dual links (`batch_id` + `fetched_count`) — now one join key per (collection, writer, reader), preferring batch/id-like names.
- Live harness missed tool JSON when registry lower-cased the filename — case-insensitive lookup.
- Stale `persistence_fixture_error` survived a later successful attempt — cleared in gap-tool + backend generators.
- Scenario D empty-`except` syntax from a mashed full Stage-4 description — harness prompt simplified (Issue 4 pin path unchanged).

### Known remaining limitations (out of Issue 4 scope / deferred)

- RC-14 compound keys (JD + public identifier upsert identity) — `identityKey` is recorded but generators are not yet required to upsert on `(identityKey, correlationKey)`.
- Post-gap 6p still validates I/O **names** only; lineage equality is enforced at Stage 4c compile + generator fixtures, not by regenerating via 6p arbitration.
- Last codegen retry can still publish with `persistence_fixture_error` (same draft policy as Issue 3 provider fixture).
- Cardinality defaults to `one_to_many`; not yet used as a hard upsert constraint.
- Full end-to-end builder (Stage 0 → scratch preview → plan execute → runtime Mongo) not re-run as part of this Issue 4 live harness.

---

## Issue 5 — Runtime: Error Dictionaries Travel Normal Success Edges

**User severity rating:** Medium  
**Actual severity:** High — this affects every workflow, not just LinkedIn  
**Status:** ✅ **DONE** (2026-09-17) — interpret error dicts at runtime; pause + `tool_error` SSE; do not store failure as `node_outputs` success

**Progress recap.** Runtime now classifies tool dicts (`ok` / nonempty `error` / transport `status`) in `langgraph/app/result_envelope.py`. Failures interrupt instead of writing `node_outputs` and walking the success edge. Old graphs get the same pause from the injected `execute_dynamic_tool` wrapper; new FREEZE/DYNAMIC graphs also `_await_successful_tool_result`. Dual SSE (`tool_error` + `ui_request` with `type: tool_error`) is published; resume `retry` re-runs the tool, `abort` fails the run. Unit 22 + live A–H (8/8, including Gemini codegen) + residual I–N (6/6) all PASS. Frontend banner and IR error-edge routing were **not** in this issue — see remaining below.

### What the user said

> Show errors on the frontend. User should know what happened — missing credentials, configuration needed, etc.

### Assessment: ⚠️ Correct user-facing goal, but the architectural fix must go deeper

The user's proposal is the right outcome. The architectural problem is that the system has no typed distinction between:

```python
{"error": "Missing credential", "candidates_batch_id": ...}  # tool error
{"candidates_batch_id": "abc", "fetched_candidates_count": 10}  # tool success
```

Both are plain dicts. The executor stores either under `node_outputs` and continues on the same edge. The downstream screen receives `candidates_batch_id` from either and never knows whether the tool succeeded.

**Two-layer fix:**

**Layer 1 — Tool result envelope (deterministic).**  
Every tool must return a typed envelope:
```python
{"ok": True,  "data": {...declared_outputs...}}
{"ok": False, "error": "...", "retryable": True/False, "code": "MISSING_CREDENTIAL"}
```
Generated tools already have the error-return pattern; it just needs to be a contract the executor checks, not prose.

**Layer 2 — Runtime execution protocol.**  
After a tool returns, the executor must check `result.ok`. If `False`:
- do NOT store `result.data` into `node_outputs`
- look for an explicit error edge on the graph node; if found, follow it
- if no error edge exists, pause the workflow and emit a `tool_error` event to the frontend with `result.error` and `result.code`
- record the node as `status: "failed"` in execution history

The frontend already has a streaming channel (`valkey.publish`). The `tool_error` event type should trigger a user-visible notification with the error message and a "Retry" or "Fix credentials" action.

### Implementation completed (Issue 5)

Did **not** wrap every tool return in `{ok, data}` — that would break `node_outputs[nodeId][field]` bindings. Did **not** invent IR success/error unions (Issue 7 leftover). Detection cannot use “missing required fields” because Issue 7 error returns still include empty declared outputs plus `error`.

Runtime interprets the existing dict:

1. explicit boolean `ok`
2. non-empty `error` string (or `{message|error}` dict)
3. `status` in error/failed/failure **only when** there is no substantive payload (so ERP `{status: "failed", invoices: [...]}` stays business data)
4. otherwise success — store the original dict (or `data` if the tool used an explicit envelope)

On failure the graph **pauses** via `interrupt({type: tool_error, ...})` instead of following the success edge. Resume `{action: "retry"}` (or empty ack) re-runs the tool; `{action: "abort"}` fails the workflow.

| Change | Files |
|---|---|
| Canonical `interpret_tool_result` + `run_tool_until_success` (interrupt until retry/abort) | `langgraph/app/result_envelope.py` |
| Injected `execute_dynamic_tool` wrapper interprets + interrupts — **old graphs pause without regen** | `langgraph/app/executor.py` |
| SSE `tool_error` (does not close the stream) + `ui_request` with `type: tool_error`; history `failed`; instance stays `Pending Review`; no fake `screen_id`; do not flatten error dicts into the next screen | `langgraph/app/executor.py` |
| Resume of a tool-error pause does **not** write `{action: retry}` into `node_outputs` | `langgraph/app/executor.py` |
| Generated FREEZE `_execute_dynamic_node` + DYNAMIC `@tool` wrappers call `_await_successful_tool_result` | `backend/agent_new.py` |
| Gap-tool / backend generators: non-empty `error` is a hard failure; keep top-level output keys (Issue 7) | `backend/services/gap_tool_generator.py`, `backend/services/backend_generator.py` |

**Frontend contract**

| SSE `event:` | Meaning | Resume |
|---|---|---|
| `tool_error` | Tool failed; graph paused. `data.error`, `data.code` (`MISSING_CREDENTIAL`, `CONFIGURATION_NEEDED`, …), `data.retryable` | `POST /resume` `{ "payload": { "action": "retry" } }` or `{ "action": "abort" }` |
| `ui_request` with `data.type === "tool_error"` | Same pause, for FE that only listens to `ui_request`. **Do not** mount a screen bundle. | same |
| `failed` | Uncaught exception **or** operator abort. Stream closes. | n/a |

`GET /instances/{id}/current-task` returns the same payload on `pending_ui_request` (includes `type: tool_error`).

**Unit tests:** `tests/test_issue5_tool_error_envelope.py`
- LinkedIn-style success dict vs missing-credential error
- Issue 7 `{jd_id:"", error:"Gemini failed"}` is failure; empty fields alone are not
- explicit `ok: true/false`; business `{status: "failed", invoices: [...]}` stays success
- retry loop then unwraps `{ok: true, data: ...}`; abort raises `ToolNodeFailed`
- generated `_await_successful_tool_result` interrupt fallback
- executor publishes `tool_error` + `ui_request` without inventing `screen_id`

### Live verification (2026-09-17)

Script: `backend/scripts/run_issue5_live_tool_errors.py`  
Artifacts: `backend/scripts/eval_output/issue5_live_20260917_082226/`  
**Overall: PASS (8/8)**

| Scenario | LLM? | Result | What it proved |
|---|---|---|---|
| A `gap_tool_missing_cred` | ✅ Gemini codegen | PASS | Live-generated tool with empty credentials returns Missing credential; interpret → `MISSING_CREDENTIAL`; `tool_error` interrupt + abort |
| B `issue7_empty_plus_error` | no | PASS | `{jd_id:"", error:"Gemini failed"}` fails; empty fields alone succeed |
| C `backend_error_path` | ✅ Gemini codegen | PASS | Live-generated process_job_search error returns include required keys + `error`; interpret fails; retry resumes to success data |
| D `gemini_runtime_fail` | ✅ real Gemini HTTP | PASS | Invalid API key call becomes error dict → pause + abort (not success edge) |
| E `business_vs_transport` | no | PASS | `{status:"failed", invoices:[...]}` stays success; bare transport status fails |
| F `sse_and_assignee` | no | PASS | SSE payload has no `screen_id`; prior assignee preserved |
| G `freeze_helper` | no | PASS | Generated `_await_successful_tool_result` interrupts on error |
| H `retry_then_success` | no | PASS | Missing credential → interrupt → retry → plain success dict (no `ok` wrapper) |

**Verdict:** Issue 5 is sorted for the original defect (error dicts traveling success edges). Live LLM-generated tools and real Gemini failures are classified and paused; success data is not written until retry succeeds.

### Residual coverage (same day, after live A–H)

Script: `backend/scripts/run_issue5_residual_coverage.py` — **6/6 PASS**

| Gap previously untested | Result |
|---|---|
| I Executor `_publish_tool_error_pause` (Valkey `tool_error` + `ui_request`, history `failed`, assignee kept) | PASS |
| J Resume must not stamp `{action:retry}` into `node_outputs` | PASS |
| K Old-graph path: injected `execute_dynamic_tool` wrapper alone pauses + retries | PASS |
| L DYNAMIC `tool_node` re-raises `RuntimeError` on abort (not swallowed as ToolMessage) | PASS |
| M Double-await idempotent when wrapper already returned clean success | PASS |
| N After retry, stored `node_outputs` is success data (no leftover `error`) | PASS |

**Still not tested (and not required to close Issue 5):** full worker + Valkey + Mongo end-to-end instance (live A–H + residual I–N cover the interpreter, pause, SSE payload, resume, freeze helper, and injected wrapper). Regenerating already-built graphs is also not required — residual K confirms the injected `execute_dynamic_tool` wrapper pauses old graphs without regen.

---

### Remaining outside Issue 5 (open follow-on)

Issue 5 closed the original defect: error dicts no longer travel the success edge into `node_outputs`. What follows is **not** unfinished Issue 5 work. It is product/architecture that the two-layer design deliberately left for later.

#### 1. Frontend must listen for `tool_error` / `ui_request.type === "tool_error"` and show retry / fix-credentials

**SSE contract is ready. UI is not built here.**

Runtime already dual-publishes so both new and old clients can branch:

| Event | What the backend sends | What FE must **not** do |
|---|---|---|
| `event: tool_error` | `{ error, code, retryable, node_name, execution_id, … }` — **does not close the stream** | Treat it like `failed` and close `EventSource` |
| `event: ui_request` with `data.type === "tool_error"` | Same pause, for clients that only listen to `ui_request` | Mount a screen bundle / iframe (`screen_id` is absent on purpose) |

Resume (same pause either event):

```http
POST /instances/{execution_id}/resume
{ "payload": { "action": "retry" } }   // or empty ack — re-runs the tool
{ "payload": { "action": "abort" } }   // fails the workflow; then `failed` closes SSE
```

Suggested UX:

- Banner / toast with `data.error` (missing credentials, configuration needed, provider failure, …).
- **Retry** when `retryable !== false`.
- **Fix credentials** when `code === "MISSING_CREDENTIAL"` (or equivalent) — then retry after the operator has filled secrets.
- Keep the SSE connection open. Recover the same payload from `GET /instances/{id}/current-task` if the event was missed (`pending_ui_request.type === "tool_error"`).

`docs/prompt-builder/WORKFLOW_RUN_FRONTEND_GUIDE.md` still documents `ui_request` as “mount the screen” and only closes on `completed` / `failed`. It does **not** yet branch on `tool_error`. That guide + the actual run UI are the remaining frontend work.

#### 2. No IR success/error union and no explicit graph error edges — pause is the default

Issue 5 Layer 2 originally allowed “follow an explicit error edge if present, else pause.” **Only the else branch shipped.** That is enough to stop silent continuation.

| Not built | Current behavior |
|---|---|
| Discriminated IR success/error union (`ok` / `data` vs `error` as separate response types) | Tools still return a **plain dict**. Issue 7 requires error returns to include empty declared output keys **plus** `error`. Runtime `interpret_tool_result` treats a non-empty `error` (or `ok: false`) as failure. |
| Planner / IR `error` edges (onError → compensation / fallback node) | Graph still has only the success successor. On interpret-failure the node **interrupts**; it does not compile or walk an error edge. |
| Executor “if error edge exists, follow it” | `run_tool_until_success` always `interrupt({type: tool_error})` until retry or abort. |

Do **not** invent IR unions or error edges as a drive-by: that is an end-to-end IR + planner + 6p + codegen + executor change. Until then, **pause is the default error policy**, and Issue 7 leftovers (6p `dataModels`, runtime assert of required outputs before the next node) stay in Issue 7’s deferred list — they are not required for the success/error split.

---

## Issue 6 — Arrays: Scalar Lists Forced into Record Arrays

**User severity rating:** High  
**Actual severity:** High — causes Pydantic 500s, forces manual downstream unwrapping everywhere  
**Status:** ✅ **DONE** (2026-09-17) — type-system change + review patches + unit tests + live LLM eval (planner / IR / gap-tool)

### What the user said

> Model array<T> where T can be scalar, enum, record, union, or nullable. If not possible, translate to list only, but correctly.

### Assessment: ✅ Correct — the fix is a type system change in `NodeParam`, not a prompt fix

The current constraint in `scratch_graph_planner.py:264–287`:
```python
if datatype == "list" and not itemSchema:
    reject
```
forces every list to declare a record `itemSchema`. There is no way to represent `list<string>`.

**Concrete fix:**

Extend `NodeParam` to accept:
```python
class NodeParam(BaseModel):
    name: str
    datatype: Literal["string", "int", "float", "bool", "datetime", "dict", "list"]
    itemType: Optional[Literal["scalar", "record", "enum"]] = None
    scalarItemDatatype: Optional[Literal["string", "int", "float", "bool"]] = None
    itemSchema: Optional[Dict[str, Any]] = None   # only for record items
    enumValues: Optional[List[str]] = None        # only for enum items
```

Validation rule: if `datatype == "list"`, then exactly one of `itemType` must be set. The LLM is instructed with three canonical examples:
- `required_skills` → `itemType: "scalar"`, `scalarItemDatatype: "string"`
- `jd_required_skills` → `itemType: "record"`, `itemSchema: {skill: string}`
- `status_filter` → `itemType: "enum"`, `enumValues: ["active", "pending"]`

This type then propagates consistently through IR generation (Pydantic scalar lists), backend generation (no adapter needed), TypeScript models (string[]), and Angular templates (plain array iteration). The wrapping/unwrapping adapters that were manually added to the LinkedIn tool and Screen 3 backend disappear.

### Implementation completed (Issue 6)

`NodeParam` now models three list item kinds. Planner datatypes stay `string|boolean|number` (mapped to IR `string|bool|number`). Legacy dumps with only `itemSchema` still infer `itemType=record`.

| Change | Files |
|---|---|
| `NodeParam.itemType` / `scalarItemDatatype` / `enumValues`; no longer require itemSchema on every list | `backend/scratch_graph_planner.py` |
| Check 6/7/8: scalar↔scalar, enum↔enum, record subset; indexed `.field` is record-only | `backend/scratch_graph_planner.py` |
| `_format_ref_type_of` emits `list<string>` / `list<enum[...]>` / `list<field:type>` | `backend/scratch_graph_planner.py` |
| Stage 6 `_to_py_type`: scalar → `List[str]` etc. | `backend/scratch_graph_planner.py` |
| Stage 1 / 1.2 / 2a prompts: three canonical list examples; forbid wrapping string arrays | `backend/scratch_graph_planner.py` |
| Carry annotation through GlobalVar, StateField, gen-spec, declaredOutputs | `backend/scratch_graph_planner.py`, `backend/agent_new.py` |
| Gap-tool IO contract + IR workflow-contract normalize preserve `itemType` | `backend/services/gap_tool_generator.py`, `backend/services/ai_processor.py` |
| IR + Angular prompts: `itemType: scalar` → `items.type` / `string[]`, never `SkillItem` | `backend/services/ai_processor.py`, `backend/services/angular_generator.py` |

**Unit tests:** `tests/test_issue6_scalar_list_params.py` (18 cases — scalar/record/enum NodeParam, Check 6/7/8, global scalar↔record reject, nested list kinds, dump-only-on-list, IR normalize preserve nested `itemType`).

### Follow-up patches after review (same day)

Post-implementation correctness pass found and fixed real holes (not prompt-only):

| Hole | Fix |
|---|---|
| Global `global.*` list bindings skipped Check 7/7b | Stage 2b runs `_check_list_binding_compat` on globals |
| Cross-phase prior-phase handoff dropped `itemType` | `_declared_outputs_as_prior_phase_entries` / `_build_prior_phase_section` merge list annotations |
| List annotation keys attached to every param dump | `_dump_list_item_annotation` only when `datatype==list` |
| Nested list fields: Check 6 incomplete; Check 7 compared only outer `list` | `_collect_list_kind_errors` recursive; nested + indexed `[0].field` list extract use Check 7 |
| IR normalize dropped nested `itemType` / ignored planner `datatype` | `_normalize_workflow_contract_param` recursive + reads `datatype` |
| Gap-tool / plan-update `_io_params` dropped list annotations | Preserve via `_list_item_io_fields` / nested itemSchema dump |

### Live LLM verification (2026-09-17)

Script: `backend/scripts/eval_issue6_scalar_lists_live.py`  
Prompts: LinkedIn JD → sourcing (from `LINKEDIN_PIPELINE_STAGE_WALKTHROUGH.md`). Artifacts under `backend/scripts/eval_output/issue6_scalar_live_*`.

| Leg | Result | Observation |
|---|---|---|
| Scratch planner Stage 1.2→4 | ✅ PASS | `required_skills` / `preferred_skills` / `tools_and_technologies` / etc. → `itemType=scalar`, `scalarItemDatatype=string`, `itemSchema=null` (walkthrough historically wrapped as `[{skill:string}]`). LangGraph: `List[str]`. |
| AIProcessor IR | ✅ PASS | With scalar IO contract → `items: { "type": "string" }`; no `SkillItem` wrapper models. |
| gap_tool_generator | ✅ PASS | IO contract carries `itemType`; codegen iterates skills as strings (`isinstance(s, str)`), not `['skill']` dicts. (Initial flash call truncated mid-JSON — harness noise; production `async_gap_tool_llm_call` OK.) |

**Verdict:** Issue 6 closed for the original defect (scalar lists forced into record arrays). No further required patches.

**Not in this change (deferred / optional hardening):**
- union / nullable item types
- live full LinkedIn rebuild of existing `SkillItem` graphs (regen when convenient)
- `global.foo[0].bar` indexed binding syntax
- capability-registry `_normalize_param` Text/Number/Array catalog can still drop planner `list` annotations on sync
- Backend `_resolve_list_field_py_type` already compiled `items.type` → `List[str]` from Task 1.1

---

## Issue 7 — Required Graph Outputs Made Optional by IR Repair

**User severity rating:** Medium (listed as forensic only)  
**Actual severity:** High — removes the containment gate that should catch wrong backends  
**Status:** ✅ **DONE** (2026-09-17) — stop IR repair from weakening graph outputs; pin declared outputs `required: true` on response models; 6p flags UI capabilities that mark them optional; unit + live Gemini evals PASS.

### What the user said

> _(no proposed solution)_

### Cause

Screen 1 graph outputs (`jd_id`, `candidate_count`, `jd_*`) were required. IR repair then made **every** `ProcessJobSearchResponse` field `required: false` so `{ "error": "..." }` would still match the schema. Backend compiled those as `Optional` and could omit every graph-required value while staying "valid".

This was not LLM variance. Three places did it on purpose:

1. IR generation prompt: "make ALL fields optional"
2. LLM repair rule 12: same instruction
3. Heuristic repair: **deterministically** set every response-model field to `required: false`

6p only checked output **names**, not requiredness. Workflow-contract normalize also dropped `required` on output params.

Discriminated success/error unions are still out of scope (IR has no union type). The practical fix is the recommended minimum: `error` stays optional; graph-declared outputs stay required; on error, return empty/default values for those fields plus `error`.

### Implementation completed (Issue 7)

| Change | Files |
|---|---|
| Deterministic pin: optional `error`; declared outputs `required: true` on matching response fields; stamp `outputParameters[].required` | `backend/services/ir_response_requiredness.py` |
| Generation + LLM repair prompts no longer weaken non-error fields; T5/T7 Path B must not optionalize `outputParameters` names | `backend/services/ai_processor.py` |
| Heuristic repair calls the pin instead of bulk-`required: false` | `backend/services/ai_processor.py` |
| Finalize IR (after T5/T7) re-pins so later repairs cannot weaken outputs | `backend/services/ai_processor.py` |
| Workflow IO normalize preserves output `required` (listed outputs default required) | `backend/services/ai_processor.py` |
| 6p 3d: UI capability `outputParameters.required=false` vs graph `declaredOutputs.required=true` | `backend/agent_new.py` |

**Unit tests:** `tests/test_issue7_response_requiredness.py`
- LinkedIn Screen 1: `jd_id` / `candidate_count` / `jd_job_title` restored; `error` and convenience `debug_trace` stay optional
- listed output with no `required` flag treated as required
- explicitly optional outputs not forced
- omitted graph output is added on POST/insert response, not GET/find
- planner IO contract names survive dropped `outputParameters`
- T4-inferred optional outputs are not pinned
- heuristic repair no longer weakens graph outputs
- 6p flags / passes capability requiredness

### Follow-up patches after review (same day)

| Hole | Fix |
|---|---|
| Pin skipped fields the LLM omitted from the response model | Add missing required outputs onto write (POST/insert) response models |
| T4 inferred UI outputs had no `required` flag → treated as graph contract | T4 `add_output` sets `required: false` |
| Restore only looked at IR `outputParameters` | Merge `_detected_io_params` / planner IO contract names |
| Heuristic pin ran before default dataSources existed | Pin at end of heuristic repair |

### Live verification (2026-09-17)

Script: `backend/scripts/run_issue7_live_required_outputs.py`  
Real Gemini on IR gen, validate/repair, and backend codegen (not a full `/plan/execute`).  
Artifacts: `backend/scripts/eval_output/issue7_live_required_20260917_061815/` (`overall_ok: true`)

| Scenario | Path | Temptation / stress | Result |
|---|---|---|---|
| A `ir_linkedin_screen1` | Full `AIProcessor` IR gen with WORKFLOW IO CONTRACT | LLM free to invent all-optional response models | **PASS** — `jd_id` / `candidate_count` / `jd_job_title` required on `ProcessJDResponse`; `error` optional; `jd_location` stays optional |
| B `llm_repair_weakened` | `validate_and_repair_ir` on invalid IR with **all** response fields `required:false` | Classic forensic defect + bad selector/theme | **PASS** — repair/finalize restored required; started with all three optional |
| C `backend_error_path` | `BackendGenerator._create_function_entry` on pinned ProcessJobSearch | databaseLogic tempts error-only `{error}` returns | **PASS** — Pydantic fields required; **3** except returns include all graph keys + error; **0** error-only returns |
| D `temptation_repair` | Repair/finalize with optionalized response + invalid selector | databaseLogic / historic "make all optional" temptation | **PASS** — required restored; optional `jd_location` not forced |

**Verdict:** Issue 7 closed for the original defect (IR repair weakening graph-required outputs). Live LLM paths that used to erase the containment gate now keep required fields through gen, repair, and error-path backend codegen.

### Known remaining limitations (out of Issue 7 scope / deferred)

- IR still has no discriminated success/error union — error returns should include empty/default required fields plus `error`. Issue 5 treats a non-empty `error` as a hard runtime failure (pause + `tool_error`) and does **not** require an IR union. See Issue 5 “Remaining outside Issue 5” item 2.
- 6p 3d is UI-only (tools use catalog `outputParameters` default `required=false`).
- 6p still does not load full IR `dataModels`; response-model requiredness is enforced at IR finalize. Runtime still does not assert required outputs before scheduling the next node.

---

## Issue 8 — Prior-Phase Globals Deduplicated by Bare Name

**User severity rating:** _(listed as code reference only)_  
**Actual severity:** Medium-High — causes cross-phase binding failures whenever two upstream nodes produce identically named outputs  
**Status:** ✅ **DONE** (2026-09-17) — namespaced `global.<nodeId>.<field>`; colliding prior-phase fields both survive; runtime looks up the producing node

### What the user said

> _(no proposed solution)_

### What the code does

`scratch_graph_planner.py` used to compact prior-phase outputs like this:
```python
seen_global_names: set = set()
for node_id, param_names in prior_phase_outputs.items():
    for item in param_names:
        pname = item.get("name")
        if pname in seen_global_names:
            continue          # ← silently drops the second one
        seen_global_names.add(pname)
        global_vars.append(GlobalVar(name=pname, ..., source=node_id))
```

If Phase 1 has a `status` output from `screen_upload` and another `status` from `tool_validate`, only the first one survived into Phase 2's context. The binder couldn't reference the second one and either failed or picked the wrong one.

The runtime twin: the phase-bridge flattened `node_outputs` into `global_inputs` with last-writer-wins, and `_resolve_bindings` parsed only `{{global.field}}` (two tokens).

### Recommended solution

Use namespaced global references throughout. The `GlobalVar.name` should become `source_nodeId.fieldName`. The LLM binder's reference syntax `global.<name>` becomes `global.<nodeId>.<fieldName>`.

This change is cascading through the prompt templates for Stage 1.5, Stage 2a, and Stage 4b. The simplest migration path:
1. Keep backward-compatible: if no dot in the name, treat it as `unknown_node.name`.
2. Change deduplication key from `pname` to `f"{node_id}.{pname}"`.
3. Update the global register text builder to emit `global.nodeId.fieldName` references.
4. Update the LangGraph state-read path that consumes these refs.

This also removes the silent "first one wins" behavior which is currently a hidden source of non-deterministic binding outcomes across workflow types.

### Implementation completed (Issue 8)

Did **not** invent a second binding namespace. Prior-phase `GlobalVar.name` is `sourceNode.field`. Binders copy the register key: `{"boundTo": "global.screen_upload.status"}`. System/config globals stay bare (`global.user_id`). Unique field names still validate as `global.<field>` (alias); colliding fields require the namespaced form.

Runtime does **not** fall back from `global.screen_upload.status` to flattened `global_inputs["status"]` (that would reintroduce last-wins). The phase bridge still writes the flatten for old `{{global.status}}` graphs **and** writes namespaced `nodeId.field` keys.

| Change | Files |
|---|---|
| Canonical name / parse / lookup helpers | `backend/services/global_bindings.py` |
| Dedup by `sourceNode.field`; snapshot migrate; unique bare alias; Stage 1.5 / 2a / 4b prompts | `backend/scratch_graph_planner.py` |
| `_BINDING_REF` parse of `{{global.node.field}}`; 6b repair allows namespaced globals; `requiredGlobalInputs` excludes dotted prior-phase keys | `backend/agent_new.py` |
| FREEZE `_lookup_global_key`; phase-bridge stores namespaced keys; DYNAMIC kickoff resolves namespaced globals | `backend/agent_new.py` |

**Unit tests:** `tests/test_issue8_prior_phase_globals.py` (17 cases) — colliding `status` both survive; unique alias accepted; colliding bare `global.status` rejected; namespaced lookup reads the producing node; flatten fallback is not used for namespaced keys.

**Not in this change:** regenerating already-built graphs (old `{{global.status}}` still uses last-wins flatten); capability planner already bound prior-phase as `{{nodeId.param}}`.

---

## Issue 9 — LLM Output Metadata Overrides Workflow Contract

**User severity rating:** _(listed as code reference only)_  
**Actual severity:** Medium — silently replaces authoritative declared outputs with LLM-hallucinated ones  
**Status:** ✅ **DONE** (2026-09-18) — `io_contract` wins; extras → targeted LLM rewrite; missing contract names entirely → generation retry; unit + live Gemini PASS

### What the user said

> _(no proposed solution)_

### What the code did (before)

`gap_tool_generator.py` used to prefer the LLM list whenever it was non-empty:
```python
llm_out_params = normalize_output_parameters(parsed.get("outputParameters") or [])
contract_out_params = normalize_output_parameters(io_contract.get("outputParameters") or [])
parsed["outputParameters"] = llm_out_params if llm_out_params else contract_out_params
```

The LLM's `outputParameters` won when present. If the LLM hallucinated a field name (e.g., `total_count` instead of the graph-declared `fetched_candidates_count`), the capability document stored in the DB used that name. The post-gap 6p validator then checked the graph's binding against the capability's `outputParameters` — and since the LLM renamed the field, the validator saw a match against a wrong field name.

### Recommended solution

Invert the priority. The `io_contract` (the workflow's graph-declared outputs) is authoritative:

```python
# io_contract fields must all be present — they are the contract
contract_out_params = normalize_output_parameters(io_contract.get("outputParameters") or [])

# LLM may add supplementary description or type detail, but cannot add/remove params
# Merge: use contract as base, apply LLM descriptions where name matches
for p in contract_out_params:
    llm_match = next((x for x in llm_out_params if x["name"] == p["name"]), None)
    if llm_match:
        p["description"] = llm_match.get("description") or p.get("description", "")
        # type stays as contract declares, not LLM guess

parsed["outputParameters"] = contract_out_params
```

If the LLM generates output parameters that are NOT in the contract, that is a signal the LLM misunderstood the spec — use a targeted LLM fix with the real contract only. If the LLM is missing contract params entirely, that is a generation failure — retry.

### Implementation completed (Issue 9)

Did **not** only pin metadata. Classification compares LLM `outputParameters` **and** visible top-level `return {…}` keys (nested helpers ignored). Transport keys (`error` / `code` / `retryable` / `ok` / `message` / `status`) are ignored as extras **unless** the contract itself declares them (e.g. a graph output named `status`).

| LLM result vs contract | Action |
|---|---|
| No overlap in metadata **or** visible `return {…}` keys (empty outputs or a full rename) | Generation **retry** |
| Overlap, but extra names and/or missing contract names in metadata or return dicts | Targeted LLM rewrite of `function_code` using **only** the real contract (`gap-tool-output-contract-fix`), then pin metadata |
| Names match | Pin metadata; no extra LLM call |

Pin always: contract **names and types** win; LLM may only fill **descriptions** on matching names. Invented names are dropped from stored capability metadata. A metadata-only rewrite that still returns invented keys is rejected.

| Change | Files |
|---|---|
| Classify / pin / apply targeted-fix response; return-dict key scan | `backend/services/gap_tool_output_contract.py` |
| Generation loop: retry on missing-entirely; targeted LLM rewrite on extras | `backend/services/gap_tool_generator.py` |
| Prompt + checklist: output names must be exact contract keys | `backend/services/gap_tool_generator.py` |

**Unit tests:** `tests/test_issue9_gap_tool_output_contract.py` — **19/19 OK** (classify, pin, transport-vs-contract `status`, code/metadata mismatch, nested helper ignore, targeted-fix apply/reject).

### Live verification (2026-09-18)

Script: `backend/scripts/run_issue9_live_output_contract.py`  
Real Gemini (`gemini-3.5-flash`) on targeted fix + full gap-tool generation (Mongo upserts patched; no full `/plan/execute`).  
Artifacts: `backend/scripts/eval_output/issue9_live_output_contract_20260918_050331/` (`overall_ok: true`)

| Scenario | Path | Temptation / stress | Result |
|---|---|---|---|
| A `targeted_fix` | Live `_targeted_fix_gap_tool_output_contract` | Tool returns `total_count` + missing `fetched_candidates_count` / `candidate_count` | **PASS** — pinned + return keys = contract; `total_count` removed |
| B `full_generation` | `_generate_gap_tool_and_upsert_capability` (LinkedIn-style contract) | Prompt tempts `total_count` / `batch_id` rename | **PASS** — capability `outputParameters` exactly `candidates_batch_id`, `fetched_candidates_count`, `candidate_count` |
| C `retry_gate` | Classify only | Full rename / empty + wrong return | **PASS** — `retry_missing_contract` |

**Verdict:** Issue 9 closed for the original defect (LLM-invented output names overriding the graph contract in stored capability metadata). Extras trigger a real-contract rewrite; zero overlap retries generation; pin is always applied before publish.

---

## Additional High-Leverage Issues Not in the User's List

These are not hypothetical risks — they have documented evidence from the forensic RCA and should be considered alongside the user's nine issues.

---

### A — Capability Resolution Is Tenant-Inconsistent and Version-Unpinned

**Severity:** High  
**Evidence:** `gap_tool_generator.py:1453–1462`, `agent_new.py:8742–8751`, `Process_configs.txt:344`

Gap-generated tools are stored under the subscriber tenant. During graph enrichment, they are looked up under an all-null/global tenant. The Process Config stores tool capability version as `null`. The local tool registry resolves by bare name, iterating an unsorted directory — if two JSON files have the same tool name, one silently overwrites the other in memory.

**Fix:** Resolve capabilities by `(subscriberId or platform, capabilityId, exact version hash)` at every stage — graph enrichment, 6p validation, runtime dispatch. Never allow a null version reference to resolve a tool. The local registry should fail on duplicate names at load time, not silently overwrite.

---

### B — UI Hydration Leaks Global State and Auth Credentials

**Severity:** High (security + correctness)  
**Evidence:** `executor.py:1380–1400`, `linkedin-recruiter/instances.txt:164–170, 457–585`

When a workflow pauses at a UI screen, the executor merges all `node_outputs` plus almost all `global_inputs` into `ui_data` before publishing to the frontend. This exposes:
- JWT tokens (confirmed present in the LinkedIn instance snapshot — **these should be rotated**)
- outputs from prior nodes that are not inputs to the paused screen
- name collisions between prior-node outputs and the screen's own fields

**Fix:** Compile the UI payload from only the declared `inputBindings` of the paused node. Auth context (`jwt_token`, `subscriber_id`, `org_id`) must never appear in the published `ui_data`; it is already available server-side at resume time. The executor should allowlist by `node.config.inputBindings.keys()` and nothing else.

---

### C — Workflow Persisted as Draft Without Any Behavioral Evidence

**Severity:** Medium  
**Evidence:** RC-11, `LINKEDIN_RECRUITMENT_FORENSIC_ROOT_CAUSE_ANALYSIS.md:1049–1098`

A workflow currently reaches "draft" status after all gaps are resolved without generation errors. A generation error means "the code didn't compile" — it does not mean "the code does what it says." The LinkedIn workflow was persisted as a clean draft despite Screen 1 returning empty data and the sourcing tool calling the wrong endpoint.

**Fix:** Introduce a `generation_verified` status that is separate from `draft`. A workflow reaches `draft` (browsable, editable) after gap resolution. It can only reach `generation_verified` (runnable without a warning) after:
1. Each generated backend function passes its executable conformance gate (Issue 2).
2. Each generated tool passes its provider fixture test (Issue 3).
3. The persistence lineage check passes (Issue 4).

Users can still trigger a draft run with an explicit "run unverified" acknowledgment — but the distinction is visible.

---

## Summary Priority Table

| # | Issue | Severity | User solution | Verdict | Implementation complexity | Status |
|---|---|---|---|---|---|---|
| 1a | Execute-time intent overwrite | Critical | Not targeted | Must add immutable `intentDescription` field and fix `_resolve_gap_generations` | Low-medium (targeted code change) | ✅ Done |
| 2 | Generic CRUD fallback published | Critical | ✅ Correct | Prohibit fallback; advisory conformance gate; complex→pro model routing | Medium | ✅ Done (2026-09-16 live retest) |
| 1b | Stage 1.2 description fidelity | High | ⚠️ Correct goal; add DB/API spec extraction | Extract literal spec blocks, enrich `databaseLogic` | Medium | ✅ Done |
| 6 | Scalar arrays forced to record arrays | High | ✅ Correct | Extend `NodeParam` type system | Low-medium | ✅ Done (2026-09-17 live LLM) |
| 7 | Required outputs made optional | High | Not proposed | Pin declared outputs required on response models; stop IR repair weakening | Low | ✅ Done (2026-09-17 live) |
| 5 | Errors travel success edges | High | ⚠️ Correct goal; needs typed envelope | Interpret `error`/`ok` at runtime; pause + `tool_error` SSE; do not store failure as success | Medium | ✅ Done (2026-09-17). FE UI + IR error edges **outside** Issue 5 |
| 4 | Writer/reader key mismatch | High | ✅ Correct (Solution 1) | Typed `persistenceLink` compiled at Stage 4c; pin+fixture in tool/IR/backend generators | Medium | ✅ Done (2026-09-16) |
| 3 | Provider calls not grounded | High | ⚠️ Web grounding useful but needs guardrails | Stage 0 capture + pin/fixture + classify (pinned/model_known/unknown) + no-invent strip | Medium-high | ✅ Done (2026-09-16 live + Stage 0 D fix) |
| A | Capability tenant/version unsafe | High | Not in list | Pin version+tenant end-to-end | Medium | Open |
| B | JWT leaked into UI payload | High (security) | Not in list | Allowlist `ui_data` to bound inputs only | Low | Open |
| 9 | LLM output metadata overrides contract | Medium | Not proposed | Contract wins; extras → targeted LLM fix; missing entirely → retry | Low | ✅ Done (2026-09-18 unit + live) |
| 8 | Prior-phase globals dedup by bare name | Medium | Not proposed | Namespace as `nodeId.fieldName` | Medium | ✅ Done (2026-09-17) |
| C | Draft published without behavioral proof | Medium | Not in list | Separate `draft` from `generation_verified` states | Low-medium | Open |

---

## Recommended next work

With Issues 1–9 closed:

1. **Issue 5 follow-on (not Issue 5 itself):** frontend must listen for `tool_error` / `ui_request.type === "tool_error"` and show retry / fix-credentials. SSE contract is ready; `WORKFLOW_RUN_FRONTEND_GUIDE.md` and the run UI do not yet branch on it.
2. **Issue 5 follow-on (optional hardening):** IR success/error union and explicit graph error edges. Pause is the default until that is designed end-to-end.
3. Highest remaining leverage: items **A–C**.
