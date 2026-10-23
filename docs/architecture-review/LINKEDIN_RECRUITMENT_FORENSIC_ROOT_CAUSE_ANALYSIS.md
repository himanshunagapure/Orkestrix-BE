# LinkedIn Recruitment Workflow — Forensic Root-Cause Analysis

## Scope and evidentiary standard

This report reconstructs the LinkedIn Recruitment workflow from the persisted requirements, graph, screen-generation records, generated source, execution history, issue chronology, and the generator/orchestrator code.

The analysis distinguishes:

1. **First incorrect decision** — the earliest pipeline stage where the intended behavior was changed, lost, or contradicted.
2. **Escape point** — the validator or publication gate that should have rejected it.
3. **Downstream symptom** — where the user finally observed the problem.

Nearby code is not classified as a root cause unless it is causally connected to the persisted artifact.

### Important forensic limitation

Several generated files were manually repaired **in place**:

- `linkedin-recruiter/ui_screens/screen_20260909102006_02d16c7d/backend-functions/process_job_search_config.py`
- `linkedin-recruiter/ui_screens/screen_20260909103118_146ac4d2/backend-functions/fetch_candidates_batch.py`
- `linkedin-recruiter/tools/row_toc1mxpz8_0c09b8f7_sb_86dc311c_20260909_160118_e3dfbc5f.json`
- `linkedin-recruiter/instances.txt`

The tool still says `version: 1` and retains its original `created_at` despite body edits. The current `instances.txt` is a later successful execution, not the first failing execution. Original behavior is therefore reconstructed from:

- the immutable-looking screen-generation records;
- the final Process Config;
- `docs/architecture-review/ISSUE_HISTORY.md`;
- the earlier audit record that captured the original generated functions before repair.

Where the exact original source no longer exists on disk, this report says so explicitly. This provenance loss is itself a finding.

---

## 1. Authoritative expected workflow

The persisted process is `mode: "steps"` with `prompt: null` (`linkedin-recruiter/Process_configs.txt:8–12`). Therefore, the authoritative original requirement is not a separate raw prompt; it is the three detailed sub-step descriptions persisted in `agentic_process.steps`.

### Step 1 — Job Description input

Expected:

- select an existing JD or enter a new JD;
- for a new JD, call Gemini and extract the named structured fields;
- save the JD to `job_description`;
- accept `candidate_count` between 1 and 30;
- return `jd_id`, `candidate_count`, and all flat `jd_*` fields;
- list-valued JD fields are described by the user as arrays of strings.

Evidence: `linkedin-recruiter/Process_configs.txt:20–23`.

### Step 2 — LinkedIn sourcing tool

Expected:

- Gemini generates `keywords`, `title`, and `industry_hint`;
- resolve Unipile location/industry IDs;
- POST paginated LinkedIn people search;
- enrich every profile with named LinkedIn sections;
- preserve/map identity and profile fields;
- calculate the exact five-part 100-point score;
- upsert by `linkedin_public_identifier` into `all_Candidates`;
- associate stored rows with the JD/batch;
- return `candidates_batch_id`, fetched count, and requested count.

Evidence: `linkedin-recruiter/Process_configs.txt:24–30`.

### Step 3 — Display profiles

Expected:

- profile cards, not a summary table;
- photo, full name, profile link, headline, location;
- score badge with thresholds 70/40;
- five-part score visualization;
- experience, Open to Work, matched skills, contact information, recent jobs;
- “Showing X of Y requested”;
- sort by score/experience/network distance;
- minimum-score filter.

Evidence: `linkedin-recruiter/Process_configs.txt:32–39`.

---

## 2. Reconstructed pipeline chronology

## 2.1 `/agentic/plan/estimate`

Function: `plan_estimate`  
File: `backend/agent_new.py:13412–13510`

Actual behavior:

- counts estimated phases/nodes/screens/tools;
- calculates and reports estimated cost;
- performs no LLM call and no Mongo write.

Forensic conclusion:

**Not causal.** It never interprets LinkedIn semantics, contracts, endpoints, bindings, or code. It had no opportunity to introduce the observed workflow defects.

## 2.2 `/agentic/workflow/register`

Function: `workflow_register`  
File: `backend/agent_new.py:13514–13561`

Actual behavior:

- creates the initial Process Config/billing record;
- stores cost and selected workflow metadata;
- optionally reserves credit.

Forensic conclusion:

**Not causal to generation correctness.** It does not plan or validate workflow behavior. It does, however, establish a lifecycle in which a registered workflow can later become `draft` without behavioral verification.

## 2.3 `/stage0/start` and `/stage0/respond`

Functions:

- `stage0_start`, `backend/agent_new.py:11616–11845+`
- `stage0_respond`, `backend/agent_new.py:12044–12205`

For steps mode, `stage0_start`:

- serializes supplied phase/sub-step descriptions into a clarification prompt;
- retains normalized caller-provided logical steps;
- checks whether named credentials exist.

The external API clarification schema contains only:

```text
provider
purpose
```

`backend/stage0.py:218–221`

It has no operation URL, HTTP method, auth placement, API version, request/response schema, or fixture.

Forensic conclusion:

- There is no surviving Stage 0 handoff/session artifact proving exactly what Stage 0 returned for this build.
- The detailed sub-step requirements survived into `Process_configs.txt`, so Stage 0 did **not** erase the entire workflow.
- Stage 0's shallow provider abstraction contributed to the absence of a verified Unipile contract, but it was **not the first point that changed the explicit Unipile paths**. The original steps still contained them when scratch planning began.
- The malformed host-only `UNIPILE_API_URL` was a user configuration value, but Stage 0 validated only credential presence, not URL format.

## 2.4 `/agentic/plan/scratch/preview`

Function: `agentic_plan_scratch_preview`  
File: `backend/agent_new.py:12725–13340`

Because this was steps mode:

- each sub-step became a `LogicalStep`;
- its `inputs` and `outputs` were forcibly set to empty (`backend/agent_new.py:12933–12953`);
- scratch Stage 2 had to invent all executable I/O contracts.

The scratch planner then produced:

```text
Job Description Input
  → Profile Fetching and Scoring
  → Display Profiles
  → End
```

This topology is persisted at `linkedin-recruiter/Process_configs.txt:500–520`.

Forensic conclusion:

- Topology was correct.
- Control-flow edges were correct.
- Graph-level Screen 1 → Tool bindings were correct (`Process_configs.txt:351–377`).
- Graph-level Tool → Screen 3 bindings were correct (`Process_configs.txt:452–458`).
- `validation_warnings` was empty and `binding_fixes_applied` was zero (`Process_configs.txt:524–531`).

The first planning defects did occur here:

1. scalar lists were converted into record lists;
2. the exact provider operation specification was compressed into prose;
3. capability reuse was not attempted because scratch assigns `GENERATE:*` to every actionable node.

## 2.5 Capability selection

Function: `_stage4_assemble_graph`  
File: `backend/scratch_graph_planner.py:5032–5251`

Actual behavior:

- every non-router/non-end scratch node is assigned `GENERATE:<name>`;
- no existing capability is searched, ranked, or selected in this scratch flow.

Persisted evidence:

- all three actionable nodes appear in `gap_generations` (`Process_configs.txt:525–532`).

Forensic conclusion:

This was not a wrong choice among candidates. There was **no capability selection decision**. It is a deliberate abstraction gap that forced three fresh generations and exposed the workflow to generator variance.

## 2.6 Scratch description and contract planning

Functions:

- `_stage1_2_enrich_node_descriptions`, `backend/scratch_graph_planner.py:1767+`
- `_stage4_assemble_graph`, `backend/scratch_graph_planner.py:5032–5251`

The description enricher is told that descriptions are passed verbatim to codegen and must preserve exact algorithms, DB mappings, retries, and edge cases (`backend/scratch_graph_planner.py:1389–1434`).

Actual persisted results:

- Screen 1 remained detailed (`Process_configs.txt:301–307`).
- Tool retained the general sourcing/scoring flow but lost exact endpoint/method/query details and several exact mapping/retry requirements (`Process_configs.txt:400–403`).
- The final Screen 3 description is generic, but that specific loss occurred later during execute, not necessarily in scratch preview.

## 2.7 `/agentic/plan/execute`

Function: `agentic_plan_execute`  
File: `backend/agent_new.py:15509–16373`

Execution order:

1. resolve each `GENERATE:*` node;
2. name-level post-gap revalidation;
3. emit LangGraph source;
4. save code;
5. persist Process Config as `status: "draft"`;
6. upsert workflow capability;
7. delete checkpoint and publish terminal `completed`.

The workflow's critical defects were generated or allowed in this phase:

- Screen 1's correct IR was paired with a generic fallback backend.
- The Unipile tool was generated from a degraded, unverified provider specification.
- Screen 3's rich requirement was deterministically overwritten before generation.
- post-gap checks compared metadata names, not executable behavior.

## 2.8 Generated LangGraph/runtime

The generated topology executes as intended, but runtime correctness checks are absent:

- UI resumes are stored without declared-output validation.
- tool error dictionaries are stored as ordinary node outputs.
- downstream edges continue normally.

Relevant source:

- generated `_execute_dynamic_node` template, `backend/agent_new.py:10356–10427`;
- tool exception handling, `langgraph/app/executor.py:630–746`.

Forensic conclusion:

LangGraph did not originate the LinkedIn defects. It amplified them by treating malformed or semantically empty capability results as successful node outputs.

---

## 3. Root-cause records

## RC-1 — Screen 1 backend was generic CRUD instead of JD processing

### Expected

`process_job_search_config` should:

- branch on `jd_source_mode`;
- fetch an existing JD or call Gemini for a new JD;
- persist the correct JD document;
- return `jd_id`, `candidate_count`, and every flat `jd_*` output.

The generated IR described this correctly:

- `process_job_search_config` is a custom dynamic source;
- it reads/writes `job_description`;
- its `databaseLogic` explicitly requires existing/new branching, Gemini extraction, persistence, and flat outputs.

Evidence: `linkedin-recruiter/ui_screens/screen_20260909102006_02d16c7d/screen-generations.txt:649–674`.

### Actually generated

The original generated file, before manual replacement, was a generic insert:

```text
action = "insert"
payload = params.get("payload") or params
result = await Database.insert_one(collection, payload)
return {
    "recordId": inserted_id,
    "collectionName": collection,
    "status": "success",
}
```

It never read `jd_source_mode`, never called Gemini, and never returned any graph-declared output.

Historical evidence:

- captured original source in the earlier audit;
- `docs/architecture-review/ISSUE_HISTORY.md:17–33`.

The current file is a manual replacement and must not be mistaken for original generator output.

### First incorrect decision

**Stage:** backend code generation during Screen 1 gap resolution  
**File:** `backend/services/backend_generator.py`  
**Functions:** `_create_function_entry` fallback path and `_build_dynamic_template`

Relevant logic:

- when generation produces no usable source, `_create_function_entry` selects `_build_dynamic_template` (`backend/services/backend_generator.py:3949–3963`);
- `_build_dynamic_template` derives only `operation.action`, defaulting to insert, and returns `recordId`, `collectionName`, and `status` (`backend/services/backend_generator.py:5910–5968`).

The original source-generation LLM may have failed before this point, but its exact failure trace is not preserved. The first provable durable incorrect decision is the deterministic decision to publish a generic insert as the implementation of a custom domain operation.

### Why it happened

The backend generator optimizes for “save a compiling function” rather than “satisfy this data source's postconditions.” Its fallback understands generic CRUD action metadata but not the richer `databaseLogic`.

### Why validation missed it

T6 validates:

- model names;
- compiled Pydantic field names/requiredness;
- exported input/response schema metadata.

It does **not** inspect or execute the function body (`backend/services/backend_generator.py:2790–2873`). The fallback callable remained attached to the correct IR response model, so metadata agreed even while the body returned different keys.

### Downstream consequences

1. Angular read `res.jd_id`, `res.candidate_count`, and `res.jd_*` from a response that only contained `recordId`, `collectionName`, and `status`.
2. `completeWorkflow` emitted `undefined` values (`app.component.ts:159–188`).
3. Runtime/default normalization persisted empty strings, zeros, and lists.
4. The tool received `jd_id=""`, `candidate_count=0`.
5. The search loop did no work.
6. Screen 3 received an empty batch ID and did not fetch.

### Classification

- **Primary:** J. Generator problem
- **Also:** B. Missing deterministic constraint; C. Missing validation; L. Multiple-layer systemic problem
- **Not primarily:** LangGraph, binding, or Stage 0

### Should an LLM make this decision?

- **LLM may write the domain function body:** YES, if constrained by exact contract and tested.
- **LLM or fallback may decide that generic insert is equivalent to the requested operation:** NO.

Deterministic mechanism:

- mark data sources as `operation.action=custom` and prohibit generic CRUD fallback;
- run the generated function with fake Gemini/DB adapters;
- assert actual keys and side effects against the canonical contract;
- fail generation if conformance is not demonstrated.

---

## RC-2 — Arrays of strings became arrays of record objects

### Expected

The original JD schema explicitly uses arrays of strings:

```text
required_skills: ["..."]
preferred_skills: ["..."]
tools_and_technologies: ["..."]
...
```

The persisted Screen 1 generation description still says “list of strings” (`Process_configs.txt:301–307`).

### Actually generated

Scratch graph outputs became:

```text
jd_required_skills: [{skill: string}]
jd_tools_and_technologies: [{tool: string}]
jd_certifications: [{certification: string}]
...
```

Evidence: `Process_configs.txt:162–299`.

The Screen 1 IR then generated Pydantic item models such as `SkillItem`, `ToolItem`, and `ResponsibilityItem` (`screen-generations.txt:513–616`).

### First incorrect decision

**Stage:** scratch Stage 2 schema design  
**File:** `backend/scratch_graph_planner.py`  
**Abstraction:** `NodeParam`

Relevant deterministic constraint:

```text
if datatype == "list" and not itemSchema:
    reject
```

`backend/scratch_graph_planner.py:264–287`

The type system has no representation for `array<string>`. Every list must be a list of records, so the LLM could not reproduce the user's scalar-array contract even if it reasoned correctly.

### Why it happened

This is a bad type abstraction, not merely an LLM prompt error. The planner's anti-blob rule conflates:

- scalar arrays;
- arrays of objects;
- large record collections.

### Why validation missed it

Validation enforced the incorrect abstraction. It verified that each list had an `itemSchema`, making the user-intent drift appear more valid.

IR self-verification compares top-level `(name, type, required)` signatures and ignores nested item-schema semantics (`backend/services/ai_processor.py:1980–2024`). The top-level type “array/list” therefore matched.

### Downstream consequences

- Any correct implementation returning `["Python"]` violates the generated Pydantic response contract.
- After Screen 1 was manually repaired to follow the textual requirement, Pydantic returned a 500 demanding dictionaries/`SkillItem`.
- A second manual repair had to wrap strings as `{skill: "Python"}`.
- The tool then needed custom unwrapping code.

Important chronology:

The observed Pydantic 500 occurred **after the original generic backend was manually replaced**. It was not the first original runtime failure, but it exposed a latent contract defect created by scratch planning.

### Classification

- **Primary:** D. Bad data contract
- **Also:** E. Bad abstraction; H. Graph planner problem; B. Missing deterministic constraint

### Should an LLM make this decision?

NO. Translating a declared JSON type into the canonical workflow type is deterministic.

Permanent mechanism:

```text
array:
  items:
    type: string | number | boolean | object | enum | union
```

The original step's scalar arrays should compile directly to JSON Schema/Pydantic/TypeScript scalar arrays.

---

## RC-3 — Exact Unipile operations were lost, then the tool hallucinated alternatives

### Expected

The original Step 2 included exact Unipile operation shapes:

- DSN `api20.unipile.com:15002`;
- search parameter endpoints;
- POST people search;
- profile endpoint;
- `linkedin_sections`;
- pagination and retry behavior.

Evidence: `Process_configs.txt:24–30`.

### Actually generated

The original tool used:

- default `https://api.unipile.com`;
- an incorrect people-search operation such as GET `/people/search`;
- API paths that did not match the supplied requirement;
- no reliable handling of the specified enrichment sections.

This produced zero candidates in roughly one second after Screen 1 was repaired.

Historical evidence:

- `docs/architecture-review/ISSUE_HISTORY.md:56–79`;
- earlier audit capture of the original tool before in-place repair.

### First incorrect decision

There are two causally distinct first points:

1. **First information-loss decision:** scratch Stage 1.2 description enrichment.
   - The persisted tool `generationDescription` retains the business outline but omits the exact endpoint strings, methods, query parameter names, and detailed throttled-section behavior (`Process_configs.txt:400–403`).
   - File/function: `backend/scratch_graph_planner.py`, `_stage1_2_enrich_node_descriptions`.

2. **First artifact containing a wrong endpoint:** gap tool LLM generation.
   - File/function: `backend/services/gap_tool_generator.py`, `_generate_gap_tool_and_upsert_capability`.
   - It receives `gen.description`, not the original step (`gap_tool_generator.py:1060–1072`, `1220–1322`).

The provider contract was therefore first weakened by planner summarization and then concretely falsified by tool codegen.

### Why it happened

- The description enricher was allowed to rewrite an already precise provider specification.
- Fidelity validation checks description coverage/length, not preservation of URLs, methods, constants, enums, or algorithms.
- Stage 0 models only provider/purpose.
- Tool codegen can use model knowledge or narrow model research, but it does not consume a pinned provider operation document.

### Why validation missed it

`_validate_gap_tool_executable` checks:

- no mocks/placeholders;
- imports;
- Python syntax;
- dynamic class compilation.

It never performs an HTTP contract check or fixture execution (`backend/services/gap_tool_generator.py:857–904`).

The prompt says return keys must match, but this is not deterministically enforced against function returns.

### Downstream consequences

- search failed fast;
- no rows were written;
- tool still returned a success-shaped count of zero;
- Screen 3 showed zero/empty records;
- operator initially investigated Screen 3 even though the data source was empty.

### Classification

- **Primary:** L. Multiple-layer systemic problem
- **Also:** F. Prompt/design problem; A. LLM reasoning failure; J. Generator problem; C. Missing validation

### Should an LLM make this decision?

- **Choosing endpoints/method/auth from an explicit provider contract:** NO. Compile them deterministically from a pinned operation spec.
- **Mapping workflow fields onto a verified provider schema:** YES, but only with exact request/response schemas and fixtures.
- **Inventing a provider operation from provider name/purpose:** NO.

---

## RC-4 — Host-only `UNIPILE_API_URL` passed credential setup but failed at runtime

### Expected

The HTTP client requires a URL with `http://` or `https://`.

### Actually configured/generated

The configured value was:

```text
api20.unipile.com:15002
```

The original tool concatenated paths without normalizing the scheme, causing:

```text
No connection adapters were found
```

Evidence: `docs/architecture-review/ISSUE_HISTORY.md:62–79`.

### First incorrect decision

The malformed value originated in user configuration. There is no surviving Stage 0 session/handoff proving that this build passed the value through `stage0_start`, so Stage 0 cannot be named as the proven first producer.

The first proven system-side incorrect assumption is in the generated tool body: it treated the configured value as an already-valid absolute URL. The earlier boundary that should have rejected it is credential ingestion/validation; if Stage 0 was used, its credential check only established that a named credential existed.

**Proven concrete stage:** tool body generation/runtime configuration handling  
**Producer:** `_generate_gap_tool_and_upsert_capability`, `backend/services/gap_tool_generator.py`  
**Missing guard:** deterministic typed URL validation/normalization before the generated request executes.

### Why validation missed it

- Stage 0 credential checks answer “does a credential with this name exist?”
- gap tool compilation cannot validate runtime credential values;
- no URL parser/normalizer was required by the capability contract;
- no sandbox call was performed.

### Downstream consequences

- all Unipile requests failed before reaching the provider;
- zero candidates looked like a valid search result;
- the actual configuration error was not surfaced as a failed workflow node.

### Classification

- **Primary:** B. Missing deterministic constraint
- **Also:** C. Missing validation; K. Runtime/execution problem
- **External contributor:** malformed user-supplied credential value

### Should an LLM make this decision?

NO. URL credentials must be typed and validated deterministically:

- parse as absolute HTTP(S) URL;
- normalize host-only DSNs if the credential type permits;
- reject unsupported schemes;
- perform an optional safe connectivity/auth check.

---

## RC-5 — Tool writes and Screen 3 reads used different batch keys

### Expected

Rows written by the sourcing tool must be queryable using the value passed as `candidates_batch_id`.

The tool declares:

```text
candidates_batch_id = jd_id
```

Therefore, every persisted candidate row must either:

- contain `candidates_batch_id: jd_id`; or
- Screen 3 must deterministically query `jd_id`.

### Actually generated

Original tool:

- stored `jd_id`;
- returned `candidates_batch_id = jd_id`.

Original Screen 3 backend:

- queried only `candidates_batch_id`/`batch_id`;
- did not query `jd_id`.

This was latent while Screen 1/tool were failing, but would still have produced an empty Screen 3 after candidate fetch succeeded.

Historical evidence:

- `docs/architecture-review/ISSUE_HISTORY.md:69–79`;
- current repaired `fetch_candidates_batch.py:30–48` shows the added `jd_id` fallback;
- current repaired tool stores both fields.

### First incorrect decision

**Stage:** independent capability generation with no shared persistence contract  
**Earliest concrete artifact:** original tool persistence body omitted the declared batch key.

Relevant producers:

- tool body: `_generate_gap_tool_and_upsert_capability`, `backend/services/gap_tool_generator.py`;
- Screen 3 IR/backend: `AIProcessor` + `BackendGenerator`.

The graph binding itself was correct:

```text
Screen3.candidates_batch_id ← Tool.candidates_batch_id
```

The missed binding was between a workflow value and a physical Mongo field.

### Why it happened

Persistence semantics lived in prose inside independently generated capabilities. There was no typed writer/reader lineage object shared by both generators.

### Why validation missed it

- scratch binding validation validates node-output references, not DB fields;
- post-gap 6p validates input/output **names** only (`backend/agent_new.py:8390–8428`);
- IR validation checks data-source structure, not cross-capability write/read equivalence;
- Angular T8 checks function-call fields, not Mongo queries.

### Downstream consequences

- tool could report N fetched and write N rows;
- Screen 3 could still query zero rows;
- counts and visible data could contradict each other.

### Classification

- **Primary:** D. Bad data contract
- **Also:** I. Binding problem; G. Capability metadata problem; C. Missing validation; L. Multiple-layer systemic problem

### Should an LLM make this decision?

NO. Persistence keys and lineage should be compiled from a deterministic contract:

```text
writer.collection
writer.fields
writer.correlationKey
reader.collection
reader.filterKey
valueSource
```

LLM may propose the initial storage design, but once accepted both writer and reader must be generated from the same object.

---

## RC-6 — Screen 3's detailed UI requirement was overwritten during execute

### Expected

Profile cards and all details listed in original Step 3.

### Actually generated

Persisted generation prompt:

> “This screen receives inputs candidates_batch_id, fetched_candidates_count, candidate_count and produces outputs user_acknowledged to render a user interface.”

Evidence:

- `Process_configs.txt:469`;
- `screen_20260909103118_146ac4d2/screen-generations.txt:409–412`.

Generated IR:

- “Candidate Batch Summary and Review”;
- summary metrics;
- generic four-column table;
- Finish button.

Evidence: Screen 3 `screen-generations.txt:5–330`.

Generated Angular:

- no profile cards;
- no photo/link/headline/location display;
- no score-breakdown bar;
- no matched-skill chips;
- no Open-to-Work badge;
- no recent experience;
- no requested sorting modes;
- no minimum-score filter.

Evidence: `src/app/app.component.html:1–174`.

### First incorrect decision

**Stage:** `/agentic/plan/execute`, immediately before resolving the downstream UI gap  
**File:** `backend/agent_new.py`  
**Function:** `_resolve_gap_generations`

Relevant logic:

1. recompute downstream UI inputs from upstream outputs;
2. build a generic sentence from input/output names;
3. assign it to both:
   - `node.config.generationDescription`;
   - `node.config.generation.description`.

`backend/agent_new.py:8152–8227`

`_resolve_one_gap_node` then builds the screen generation prompt from this overwritten `gen.description` (`backend/agent_new.py:7863–7872`, `8086–8099`).

This is deterministic and exactly reproduces the persisted sentence. It is not an uncertain inference and not an LLM reasoning failure.

### Why it happened

The orchestration code conflates two different concepts:

- semantic/UX requirement;
- derived I/O summary.

Refreshing bindings should update only the I/O contract. Instead, it replaces the semantic specification.

### Why validation missed it

- no intent-fidelity hash/diff exists;
- no required UI feature checklist survives planning;
- AIProcessor legitimately expands the only prompt it receives—a generic summary;
- Angular T8 checks request/response fields, not feature presence;
- Angular build success is treated as screen success.

### Downstream consequences

- AIProcessor invented a summary/table screen;
- the backend invented a generic batch query;
- Angular faithfully implemented the wrong IR;
- the screen built successfully and was marked stable;
- the final Process Config persisted the destroyed description, obscuring the original generation-time state.

### Classification

- **Primary:** J. Generator/orchestration problem
- **Also:** E. Bad abstraction; F. Prompt/design problem; B. Missing deterministic constraint; L. Multiple-layer systemic problem
- **Not:** A. LLM reasoning failure

### Should an LLM make this decision?

NO. Binding refresh must never rewrite intent.

Deterministic mechanism:

- immutable `intentDescription`;
- separate `ioSummary`;
- merge only by appending machine-readable contract;
- assert that required acceptance-criteria IDs remain present before generation.

---

## RC-7 — Screen 3 used `match_score` while storage/tool used `score`

### Expected

The user named the persisted/computed value `score`, and the display should show `total_score / 100`.

### Actually generated

Screen 3 IR defined:

```text
CandidateItem.match_score
```

and the table rendered `match_score`.

Evidence:

- `screen-generations.txt:253–273`;
- `screen-generations.txt:118–142`;
- Angular template `app.component.html:116–124`.

The tool/storage used `score`.

### First incorrect decision

**Stage:** Screen 3 IR generation after the description overwrite  
**File/function:** `backend/services/ai_processor.py`, `generate_ir_schema` → `process_prompt` (`3297+`, `3649–3668`)

The IR LLM received no original profile-card field contract. It invented a conventional `match_score` name. The current repaired backend explicitly maps `score` as a fallback to `match_score` (`fetch_candidates_batch.py:52–60`), proving the generated physical models were not aligned.

### Why validation missed it

- `match_score` was internally consistent across the Screen 3 IR, backend schema, TypeScript model, and Angular template;
- no canonical storage-to-view field mapping said `score → match_score`;
- validators compared generated layers to the same generated IR, creating self-consistency around a wrong field.

### Downstream consequences

- original Screen 3 could show zero/blank scores even when candidate documents had `score`;
- manual backend mapping was required;
- threshold semantics in the UI also drifted from the user requirement.

### Classification

- **Primary:** D. Bad data contract
- **Also:** A. LLM reasoning failure; G. Capability metadata problem; J. Generator problem; L. Multiple-layer systemic problem

### Should an LLM make this decision?

- Choosing a display label: YES.
- Renaming a canonical business/storage field without an explicit mapping: NO.

---

## RC-8 — Enrichment discarded candidate identity fields

### Expected

Each candidate should retain:

- first/last/full name;
- public identifier;
- profile URL/photo;
- contact fields when available;
- search metadata plus enriched profile sections.

### Actually generated

The original tool replaced the search-result object with the profile response. Unipile profile responses often omitted usable first/last names, so fields available in the search result were discarded. Mapping then read only a narrow set of fields.

Observed result:

- counts and scores were present;
- `first_name`, `last_name`, and email were empty;
- Screen 3 displayed `Unknown`.

Evidence:

- `docs/architecture-review/ISSUE_HISTORY.md:85–104`;
- candidate dump shows empty names but present `linkedin_public_identifier` and headline (`aiqod-dev...all_Candidates.json:1–56`);
- Angular fallback displays `Unknown` (`app.component.html:101`).

### First incorrect decision

**Stage:** tool body generation  
**File/function:** `backend/services/gap_tool_generator.py`, `_generate_gap_tool_and_upsert_capability`  
**Generated logic:** enrichment replacement rather than a merge preserving search identity.

### Why it happened

The LLM was asked to implement provider-specific merging without:

- recorded search/profile response fixtures;
- required-field provenance;
- a merge policy;
- invariants for identity preservation.

### Why validation missed it

- code compiled;
- candidate fields were optional;
- no fixture represented “search has name, profile does not”;
- no postcondition required `candidate_name` or a deterministic fallback.

### Downstream consequences

- valid search results became anonymous;
- contact info appeared absent;
- users saw “Unknown” despite usable public identifiers/headlines.

### Classification

- **Primary:** J. Generator problem
- **Also:** A. LLM reasoning failure; C. Missing validation; D. Bad data contract

### Should an LLM make this decision?

The LLM may propose provider mappings, but merge precedence should be deterministic once schemas are known.

Required contract:

- field provenance by endpoint;
- non-destructive merge policy;
- required identity postcondition;
- fixture with missing/null profile identity fields.

---

## RC-9 — Exact scoring/mapping requirements degraded into approximate implementation

### Expected

Examples of exact requirements:

- source = `"linkedin"`;
- status = `"linkedin_sourced"`;
- current_base = location;
- direct `is_open_to_work` mapping;
- exact education gap table: 20/14/7/0;
- diploma and associate tier = 2;
- retry once when `throttled_sections[]` reports a throttled section;
- random delay between batches;
- map recent work experiences into the specified shape.

### Actually generated/current artifact

Even after endpoint repairs, the tool body still demonstrates semantic drift:

- source = `"LinkedIn Search"`;
- status = `"Sourced"`;
- current_base = empty;
- Open to Work inferred from text keywords;
- education uses proportional scoring rather than the specified gap table;
- diploma can be treated as tier 1;
- no deterministic `throttled_sections[]` retry behavior;
- enrichment fields are often empty/raw rather than normalized.

Evidence:

- current tool JSON `function_code`, around the scoring and `candidate_doc` sections;
- candidate dump `source`, `status`, empty experience/skills and score components (`all_Candidates.json:1–56`);
- issue chronology notes these remaining spec gaps (`ISSUE_HISTORY.md:139–149`).

### First incorrect decision

**Stage:** scratch description enrichment first omitted several exact invariants; tool LLM then generated approximate alternatives.

Files/functions:

- `_stage1_2_enrich_node_descriptions`, `backend/scratch_graph_planner.py`;
- `_generate_gap_tool_and_upsert_capability`, `backend/services/gap_tool_generator.py`.

### Why validation missed it

All deviations are semantically legal Python and return the advertised count fields. No validator executes golden scoring cases or asserts fixed enum/mapping values.

### Downstream consequences

- scores are not the scores the user specified;
- UI status styling does not recognize `"Sourced"` because it was generated for active/matched/pending;
- experience/open-to-work/education/contact breakdowns can be systematically wrong;
- persisted records violate the requested collection semantics.

### Classification

- **Primary:** L. Multiple-layer systemic problem
- **Also:** F. Prompt/design problem; A. LLM reasoning failure; J. Generator problem; C. Missing validation

### Should an LLM make this decision?

- **Generate a search query from a JD:** YES, constrained by output schema and examples.
- **Implement exact scoring arithmetic, constants, enums, retry counts, and field copies:** NO. Compile deterministic code or at least deterministic tests from the requirement.
- **Infer provider-field mappings:** YES, only against pinned schemas/fixtures and explicit null/merge policy.

---

## RC-10 — Failure and zero-result states were treated as successful execution

### Expected

A provider/configuration/tool error should:

- fail the node or follow an error edge;
- preserve diagnostics;
- not masquerade as a successful empty search.

### Actually generated/executed

- original tool returned zero counts without making workflow status fail;
- runtime marked the tool step `success`;
- Screen 3 paused normally with empty data.

Runtime source:

- `LocalToolRegistry.execute_tool` returns `{"error": ..., "traceback": ...}` on exceptions (`langgraph/app/executor.py:704–746`);
- generated `_execute_dynamic_node` stores any result under `node_outputs` and returns normally (`backend/agent_new.py:10404–10412`);
- no declared-output or `error` check occurs.

### First incorrect decision

There are two:

1. tool implementation chose “zero candidates” instead of an explicit failed result;
2. runtime has no typed failure protocol and treats error dictionaries as data.

The first visible LinkedIn run hit the first; the second is the architectural escape that also affects thrown tool exceptions.

### Why validation missed it

- success is defined as callable return, not postcondition satisfaction;
- `error` is not part of a standard node-result envelope;
- graph has no explicit error edge for this tool;
- no invariant says “requested count > 0 and provider error implies failed node.”

### Downstream consequences

- root cause was hidden;
- empty Screen 3 looked like a UI/binding defect;
- execution history recorded success for a failed business operation.

### Classification

- **Primary:** K. Runtime/execution problem
- **Also:** B. Missing deterministic constraint; C. Missing validation; L. Multiple-layer systemic problem

### Should an LLM make this decision?

NO. Runtime success/failure semantics must be deterministic.

---

## RC-11 — The workflow was persisted as a clean draft despite no behavioral proof

### Expected

A draft may be editable, but its status/warnings should reveal whether it is generated, verified, or known-broken.

### Actually persisted

`Process_configs.txt` records:

- `status: "draft"`;
- `warnings: []`;
- `validation_warnings: []`;
- all gaps resolved.

`backend/agent_new.py:15658–15670` always writes `status: "draft"` after execute.

The execute pipeline:

- allows LangGraph generation failure to become `combined_code=""` plus warning (`backend/agent_new.py:16105–16118`);
- allows code persistence/postgen problems as warnings (`16130–16170`);
- allows Process Config and workflow capability writes to fail without failing the response (`16210–16243`);
- then deletes the checkpoint and publishes terminal `completed` (`16330–16349`).

### First incorrect decision

**Stage:** execute finalization/publication  
**File/function:** `backend/agent_new.py`, `agentic_plan_execute` and `_save_process_to_process_configs`

It equates “no remaining `GENERATE:*` references” with a publishable draft.

### Why validation missed it

There is no final behavioral acceptance gate. Each subsystem reports local structural success, and warnings are not status-bearing.

### Downstream consequences

- a wrong but compiling workflow appears normally generated;
- operators discover faults only through manual execution;
- warning-free status falsely increases confidence.

### Classification

- **Primary:** C. Missing validation
- **Also:** E. Bad abstraction; L. Multiple-layer systemic problem

### Should an LLM make this decision?

NO. Publication status must be derived from deterministic gate evidence.

---

## RC-12 — Generated artifacts were repaired in place without immutable provenance

### Expected

Every generated or repaired artifact should have:

- immutable source version;
- generation prompt/contract hash;
- repair parent;
- timestamp;
- verification evidence;
- workflow capability pin.

### Actually stored

- current tool body is repaired but still `version: 1`;
- tool capability is not version-pinned in Process Config;
- current backend function files no longer contain original generated code;
- current `instances.txt` replaced the first failing run;
- the original generation retry/error trace is unavailable.

### First incorrect decision

**Stage:** artifact persistence/versioning  
**Components:** file-backed tool registry, generated backend workspace, manual deployment/update path, Process Config capability references.

### Why validation missed it

Versioning is treated as metadata/pointer management, not content-addressed provenance.

### Downstream consequences

- exact original LLM output and retry failure cannot be reproduced;
- runtime artifact can drift from the screen-generation/Process Config record;
- regression comparisons are unreliable;
- forensic diagnosis depends on chat/audit history.

### Classification

- **Primary:** E. Bad abstraction
- **Also:** G. Capability metadata problem; C. Missing validation; L. Multiple-layer systemic problem

### Should an LLM make this decision?

NO. Artifact identity and versioning must be deterministic and content-addressed.

---

## RC-13 — Required graph outputs were weakened into optional response fields

### Expected

Screen 1's outputs are mandatory inputs to the sourcing tool. The persisted graph marks `jd_id`, `candidate_count`, and every `jd_*` field required (`Process_configs.txt:71–294`).

### Actually generated

`ProcessJobSearchResponse` marked all those fields optional (`screen_20260909102006_02d16c7d/screen-generations.txt:460–579`). This was not LLM variance: the IR repair prompt deterministically instructs that every non-error response field be changed to `required: false` so partial error responses remain model-compatible (`backend/services/ai_processor.py:5091–5097`).

### First incorrect decision

**Stage:** Screen 1 IR repair  
**File/function:** `backend/services/ai_processor.py`, repair path in `generate_ir_schema`

The repair policy conflates two incompatible states in one schema:

- successful response, where graph outputs are mandatory;
- failed response, where only an error may exist.

### Why validation missed it

- the repaired IR is valid by its own weakened schema;
- backend T6 checks model/schema alignment against that same IR;
- post-gap 6p checks output **names**, not output types or requiredness (`backend/agent_new.py:8391–8400`);
- runtime does not assert required declared outputs before scheduling the next node.

### Downstream consequences

The generic Screen 1 backend could omit every graph-required value while remaining response-model compatible. This did not first create the bad backend body (RC-1), but it removed the deterministic containment boundary that should have stopped it.

### Classification

- **Primary:** D. Bad data contract
- **Also:** B. Missing deterministic constraint; C. Missing validation; K. Runtime/execution problem

### Should an LLM make this decision?

NO. Use a discriminated result union:

```text
Success { ok: true, data: Required<DeclaredOutputs> }
Failure { ok: false, error: TypedError }
```

Requiredness must be compiled from graph contracts, never relaxed by an IR-repair prompt.

---

## RC-14 — Candidate identity and job-specific match data use one conflicting persistence key

### Expected

A candidate may match more than one JD. Person identity should deduplicate globally, while score, score components, sourcing status, and batch membership remain specific to a JD/run.

### Actually specified/generated

The original requirement explicitly requested upsert by `linkedin_public_identifier`, and the current tool does so while also storing JD-specific fields in that same row. Sourcing the same person for another JD can therefore replace the earlier row's `jd_id`, batch key, and score. The earlier Screen 3 can then lose that candidate.

This is a requirement-level inconsistency that the generator implemented rather than challenged. Because the tool was repaired in place (RC-12), the exact historical body cannot prove which repair changed individual fields; the conflicting key design is nevertheless present in both the requirement and current artifact.

### First incorrect decision

**Stage:** original persistence design accepted during planning  
**First source:** user Step 2 persistence requirement  
**Allowed by:** `backend/scratch_graph_planner.py`, description enrichment and graph contract design  
**Implemented by:** `backend/services/gap_tool_generator.py`, generated tool body

### Why validation missed it

No schema distinguishes:

- stable candidate identity;
- JD-specific candidate match;
- execution/batch membership.

Writer/reader validation checks neither key cardinality nor two-run behavior.

### Downstream consequences

- later workflows can silently mutate earlier workflow results;
- scores become “latest JD score,” not a stable property;
- counts and historical Screen 3 views become time-dependent;
- concurrent sourcing runs can overwrite one another.

### Classification

- **Primary:** D. Bad data contract
- **Also:** E. Bad abstraction; C. Missing validation; L. Multiple-layer systemic problem

### Should an LLM make this decision?

The LLM may propose a domain model, but NO LLM should finalize key cardinality without deterministic invariant checks. Use either:

- `candidates` keyed by public identifier plus `candidate_matches` keyed by `(jd_id, public_identifier)`; or
- a composite upsert key if separate identity storage is unnecessary.

Regression must source the same public identifier for two JDs and verify both match records remain independently queryable.

---

## 3.1 Proven containment risks that were not the first cause of this run

The following are concrete defects, but available evidence does **not** show that either one first caused an observed LinkedIn symptom. They are recorded separately to avoid misclassifying nearby architecture as forensic root cause.

### Capability tenant and version resolution

- gap-tool generation stores a generated capability under the subscriber tenant when `subscriber_id` exists (`backend/services/gap_tool_generator.py:1453–1462`);
- graph enrichment fetches tool capability metadata under an all-null tenant because it assumes tools are global (`backend/agent_new.py:8742–8751`);
- the LinkedIn Process Config leaves the tool capability version `null` (`Process_configs.txt:344`);
- runtime loads JSON files by bare tool name, iterates unsorted directory entries, and overwrites duplicate names in one dictionary (`langgraph/app/executor.py:435–523`, `630–653`).

This can make planning metadata disappear and can execute a different tool body than the one validated. Deterministic fix: resolve by `(tenant, capabilityId, immutable version/hash)` and fail on duplicate runtime names or missing pins.

### Over-broad paused-screen hydration and credential exposure

The executor flattens all prior `node_outputs`, then almost every `global_input`, into `ui_data` (`langgraph/app/executor.py:1380–1400`). The LinkedIn instance proves that a JWT reached persisted/published screen data (`instances.txt:164–170`, `457–585`). The credential is intentionally not reproduced here and should be rotated.

This is deterministic runtime behavior, not an LLM decision. It did not cause the observed candidate-generation defects, but it creates hidden coupling, payload growth, name collisions, and a security boundary violation across all workflows. Fix with an allowlist compiled strictly from the paused node's declared input bindings; auth context must stay server-side.

---

## 4. Manual-repair regression that was not an original generator defect

After names were initially `Unknown`, a manual repair added a fallback order that included `headline`. Because Unipile often supplied no first/last name but did supply a headline, the “Candidate Name” column displayed job headlines.

This was **not introduced by the original workflow generator**. It was introduced by the first manual fix to `fetch_candidates_batch.py`.

The later repair:

- rejects headline-like strings;
- derives a display name from `linkedin_public_identifier`.

Evidence:

- `ISSUE_HISTORY.md:106–136`;
- current `fetch_candidates_batch.py:64–101`.

Classification:

- J. Generator/manual repair problem
- C. Missing regression validation

Required regression case:

```text
first_name=""
last_name=""
headline="Python Developer | FastAPI • Django"
linkedin_public_identifier="tushar-patil-727125229"
```

Expected display name: `Tushar Patil`, never the headline.

---

## 5. Failure summary

| Failure | First introduced at | Root cause | Why validation missed it | Permanent fix | Regression test |
|---|---|---|---|---|---|
| Screen 1 emitted empty JD data | Execute → Screen 1 backend generation | Generic CRUD fallback published for custom domain logic | T6 compared IR/model/schema metadata, not actual returns or side effects | Prohibit CRUD fallback for custom operations; executable conformance gate | Fake Gemini + fake DB; assert `jd_id`, every `jd_*`, and insert/read behavior |
| Scalar arrays became item objects | Scratch Stage 2 schema design | `NodeParam(list)` requires record `itemSchema` | Validator enforced the wrong abstraction; top-level list still matched | Add scalar/record/enum array item types | `required_skills=["Python"]` passes end-to-end without adapters |
| Wrong Unipile endpoint/method | Scratch description enrichment, concretized in gap tool generation | Exact provider operations were summarized away; LLM invented replacements | Syntax/import/class checks only; no provider contract probe | Pinned provider operation spec compiled into request code | Fixture asserts method/path/query/auth and response parsing |
| Host-only DSN failed | Credential configuration accepted by Stage 0 | Credential presence checked, value shape unchecked | No typed URL validation or connectivity check | Typed URL credential with deterministic normalization/validation | `api20.unipile.com:15002` normalizes or is rejected before execute |
| Tool writes `jd_id`, screen reads batch field | Independent tool/screen generation | No shared typed persistence lineage | Graph binding checks do not inspect Mongo write/read keys | Compile writer and reader from one persistence contract | Write fixture by correlation key, then read through Screen 3 function |
| Screen 3 became generic table | Execute `_resolve_gap_generations` | Rich description overwritten with generic I/O sentence | No intent-fidelity/feature validation; UI still built | Keep immutable intent separate from derived I/O summary | Assert original Step 3 acceptance IDs survive into AIProcessor prompt |
| `score` became `match_score` | Screen 3 IR generation | IR invented field because canonical profile/storage schema was absent | IR/backend/TS/UI were mutually consistent with the invented name | Canonical field IDs plus explicit view mapping | Candidate with `score=72` renders 72 without handwritten fallback |
| Candidate identity was lost | Tool body generation | Enriched profile replaced search result; narrow field mapping | Optional fields + no missing-name fixture | Deterministic merge precedence and identity postcondition | Search has name, profile omits name; final candidate retains name |
| Scoring/status/source drift | Scratch description compression + tool body generation | Exact constants/algorithm left to LLM | No golden semantic tests | Compile exact rules or generate mandatory golden cases | Table-driven score tests for skills/experience/education/open/contact |
| Tool error/zero looked successful | Tool implementation and runtime | No typed failure envelope or output postcondition | Any dict is accepted as successful node output | Standard `ok/data/error/retryable` result and explicit error policy | Provider 401/timeout/zero-error must fail or route to error edge |
| Broken workflow persisted warning-free | Execute finalization | “gaps resolved” treated as generation success | No workflow-level dry run or verified status | Separate generated/verification-failed/verified states | Deliberately wrong function cannot reach verified draft |
| Original evidence overwritten | Artifact persistence/update paths | Mutable version-1 files and unpinned tool capability | No content hash/version invariant | Immutable content-addressed artifacts and run IDs | Repair creates v2 with parent hash; v1 remains retrievable |
| Required graph outputs became optional | Screen 1 IR repair | Success and failure responses collapsed into one all-optional model | T6 follows weakened IR; 6p checks names only; runtime checks no postconditions | Discriminated success/failure result; compile success requiredness from graph | Missing any `jd_*` field must fail Screen 1 before the tool runs |
| Same candidate can overwrite another JD match | Persistence design accepted during planning | Person identity and JD-specific match stored under one public-ID upsert key | No cardinality, lineage, concurrency, or two-run test | Separate candidate identity from `(jd_id, public_id)` match, or use composite key | Source one public ID for two JDs; both scores/batches remain queryable |

---

## 6. Which layers were and were not responsible

### Not the first cause

- **Estimate:** cost only.
- **Workflow registration:** billing/lifecycle only.
- **Graph topology:** correct sequence.
- **Graph edge generation:** correct.
- **Graph node-output bindings:** correct for Screen 1 → Tool → Screen 3.
- **LangGraph topology definition:** represented the persisted graph correctly.

### Direct first causes

- scratch type abstraction;
- scratch semantic-description fidelity;
- backend fallback publication;
- gap tool provider/body generation;
- execute-time UI description overwrite;
- missing shared persistence contract;
- runtime failure semantics;
- final publication status.

### Failure-to-contain layers

- scratch validation;
- IR self-verification;
- backend T6;
- tool executable validation;
- post-gap 6p;
- Angular T8;
- post-generation LangGraph validation;
- workflow finalization.

---

## 7. LLM decision-ownership audit

## Decisions that should move to deterministic code

1. Preservation of original intent and acceptance criteria.
2. JSON/array type translation.
3. Binding reference selection when there is a unique typed producer.
4. Persistence collection/key/value lineage after design approval.
5. External endpoint/method/auth/version when present in a provider contract.
6. Exact score arithmetic, thresholds, fixed enums, retry counts, and caps.
7. Credential type/format validation.
8. Generated function return-schema checking.
9. Node success/failure interpretation.
10. Publication/verification status.
11. Artifact IDs, versions, hashes, and parentage.
12. Preservation of upstream identity fields during provider-response merges once precedence is defined.

## Decisions that may remain LLM-driven

1. Decomposing ambiguous natural-language goals into candidate business steps.
2. Naming screens and proposing layout.
3. Generating a LinkedIn search query from a structured JD.
4. Proposing provider-field mappings when a verified schema is supplied.
5. Writing implementation code inside a fixed contract.
6. Proposing repair patches from concrete failing evidence.
7. Choosing a UI presentation among layouts that all satisfy explicit acceptance criteria.

Required constraints for LLM-owned decisions:

- immutable input intent;
- canonical typed I/O schemas;
- verified provider operation schemas/fixtures;
- allowed collections and field mappings;
- explicit error policy;
- acceptance scenarios;
- bounded change scope;
- deterministic tests after generation.

---

## 8. Answers to the architectural questions

## 8.1 Why did the architecture fail despite IR, planning, validation, and LangGraph?

Because each layer validates a different **description** of behavior:

- graph validates references;
- IR validates schema structure;
- backend validates model metadata;
- tool validator validates compilation;
- Angular validates static call/interface shape and build;
- LangGraph validates source/importability.

No gate proved that:

- the Screen 1 body returned the advertised fields;
- provider calls matched the supplied Unipile contract;
- Mongo writer and reader used the same correlation key;
- Screen 3 implemented the original UI acceptance criteria;
- a real representative workflow path succeeded.

The pipeline achieved local self-consistency, not end-to-end behavioral correctness.

## 8.2 Which layer has too much responsibility?

The overloaded abstraction is **natural-language `generationDescription`**.

It currently acts as:

- intent;
- UI specification;
- backend specification;
- provider contract;
- persistence design;
- code-generation prompt;
- capability metadata.

It is rewritten by multiple LLM/deterministic stages and has no immutable semantics. The gap-resolution orchestrator also has too much authority because it can mutate bindings, descriptions, capability references, and generation prompts in one pass.

## 8.3 Which decisions should move from LLM to deterministic code?

The twelve items in the previous section, especially:

- schema translation;
- exact API operations;
- persistence lineage;
- scoring constants;
- runtime result validation;
- publication state.

## 8.4 Which decisions should remain LLM-driven?

Natural-language interpretation, proposed decomposition, search-query generation, code synthesis, and UX composition—provided their outputs are constrained and executable tests determine acceptance.

## 8.5 Which contracts are too weak?

1. **Stage 0 provider contract:** provider/purpose only.
2. **Workflow I/O:** many parallel dialects; scalar arrays unsupported in scratch.
3. **Capability metadata:** self-reported and not tied to actual executable behavior.
4. **Persistence contract:** collection prose with no cross-capability lineage.
5. **UI contract:** field I/O only; no durable acceptance criteria.
6. **Runtime result contract:** ordinary dictionaries combine success, error, and business-empty states.
7. **Artifact contract:** mutable paths/versions without content identity.
8. **Success/failure schema contract:** graph-required success fields are made optional to accommodate errors.
9. **Entity/relationship contract:** stable person identity is conflated with JD-specific match state.
10. **Capability resolution contract:** tenant scope and immutable runtime version are not carried end to end.
11. **UI hydration contract:** runtime sends global state rather than only declared screen inputs.

## 8.6 Which validation rules are missing?

1. Original-intent fidelity across every description rewrite.
2. Actual function return versus declared output JSON Schema.
3. Required side effects versus fake DB/HTTP adapter.
4. Provider method/path/auth/request/response fixture conformance.
5. Writer→reader collection and correlation-key lineage.
6. UI feature/interaction acceptance checks.
7. Exact enum/constant/algorithm golden tests.
8. Required-output validation on UI resume.
9. Error-result routing and retry policy.
10. Full workflow dry-run before verified status.
11. Artifact hash/version consistency.
12. Success-response requiredness must equal graph declared-output requiredness.
13. Multi-run persistence cardinality and concurrency invariants.
14. Tenant/version/hash equality from capability selection through runtime dispatch.
15. UI payload allowlist and explicit prohibition of server-side credentials.

## 8.7 Which failures generalize across many workflow types?

Highly general:

- generic backend fallback;
- scalar-array/object-list coercion;
- description overwrite or requirement loss;
- generated metadata disagreeing with function body;
- independently generated DB writer/readers;
- external API hallucination;
- optional-field/data-normalization failures;
- error dictionaries treated as success;
- fail-open publication;
- mutable artifact provenance.
- all-optional success schemas;
- entity identity mixed with workflow-specific relationship data;
- unpinned/tenant-inconsistent capability lookup;
- global-state leakage into UI payloads.

LinkedIn-specific only:

- exact Unipile paths and response fields;
- LinkedIn privacy-related missing email/name;
- public-identifier name fallback.

## 8.8 What changes prevent whole classes of failures?

The repository does not require a full rewrite. The smallest high-leverage set is:

1. **Preserve immutable intent and acceptance criteria.**
   - Delete the execute-time description replacement behavior.
   - Keep I/O summaries separate.

2. **Fix canonical type representation.**
   - Add scalar-array support and one JSON-Schema-compatible representation across planner, IR, backend, tool, UI, and runtime.

3. **Make generated bodies prove their contracts.**
   - Execute backend/tools against fixtures/fakes.
   - Validate actual return values and side effects.
   - Prohibit semantic fallback publication.

4. **Introduce typed integration and persistence contracts.**
   - Pin provider operations.
   - Compile DB writer/readers from the same lineage object.

5. **Make runtime and publication fail closed.**
   - typed result envelope;
   - declared-output assertion;
   - explicit error edges/policy;
   - verified status only after capability and workflow dry runs.

6. **Make artifacts immutable and content-addressed.**
   - repairs create a new version;
   - Process Config pins exact capability/artifact hashes.

7. **Separate identity, relationship, and execution state.**
   - keep stable candidate identity independent from JD-specific scoring/batch records;
   - enforce key/cardinality invariants with multi-run tests.

8. **Compile runtime boundaries from graph contracts.**
   - resolve capabilities by tenant plus immutable hash;
   - hydrate UIs only with bound inputs;
   - never put auth credentials in UI state.

These changes retain the current staged planner/generator architecture while changing what counts as success.

---

## 9. Minimal regression suite for LinkedIn Recruitment

1. **Screen 1 new JD**
   - fake Gemini returns scalar string arrays;
   - assert DB insert;
   - assert every flat output and exact types.

2. **Screen 1 existing JD**
   - DB record with string/ObjectId variants;
   - assert no Gemini call;
   - assert same output contract as new mode.

3. **Unipile operation contract**
   - assert exact method/path/query/auth for filter, search, and profile calls;
   - assert pagination and count trimming.

4. **Profile merge**
   - search response has name/public ID;
   - profile response omits names;
   - final row preserves search identity.

5. **Throttled sections**
   - fixture reports `throttled_sections`;
   - assert exactly one delayed retry.

6. **Score golden cases**
   - fixed candidates for each education gap, experience threshold, skills ratio, Open-to-Work, and contact combination;
   - assert exact five components and total.

7. **Persistence lineage**
   - source N candidates under one correlation value;
   - Screen 3 backend must retrieve exactly those N using workflow `candidates_batch_id`.

8. **Screen 3 acceptance**
   - assert cards, photo, link, headline/location, score thresholds, five components, skills, experience, counts, sort modes, and minimum-score filter.

9. **Error behavior**
   - missing credential, malformed DSN, provider 401/429/500, timeout, malformed JSON;
   - node cannot be recorded as successful normal data.

10. **Workflow dry run**
    - Screen 1 resume → tool → Screen 3 interrupt;
    - assert all declared outputs at every boundary;
    - assert visible candidate count and representative card content.

11. **Provenance**
    - repair one artifact;
    - assert old content remains addressable and workflow version pins do not silently move.

12. **Required success outputs**
    - return a nominal success missing one `jd_*` field;
    - assert Screen 1 fails before the sourcing tool is scheduled.

13. **Cross-JD identity**
    - source one public identifier for two JDs with different scores;
    - assert both match records remain queryable and the identity record remains stable.

14. **Capability pinning**
    - publish same tool name in two tenants/versions;
    - assert the workflow executes only its pinned tenant/hash.

15. **UI hydration boundary**
    - put unrelated prior outputs and auth credentials in runtime state;
    - assert neither appears in paused-screen `ui_data`.

---

## Final determination

The LinkedIn Recruitment workflow did not fail because LangGraph chose the wrong graph. It failed because correct user intent was progressively converted into weaker prose and self-reported metadata, while executable implementations were allowed to diverge.

The first decisive defects were:

1. scratch could not represent scalar arrays;
2. exact tool semantics were not preserved as machine constraints;
3. backend generation published generic CRUD for a custom operation;
4. execute overwrote Screen 3's intent;
5. independently generated persistence operations had no shared key contract;
6. no executable or runtime gate treated those divergences as failure.

The most effective correction is a narrow architectural hardening—not a rewrite: preserve intent, strengthen canonical contracts, test actual generated behavior, and fail closed at capability/runtime/publication boundaries.
