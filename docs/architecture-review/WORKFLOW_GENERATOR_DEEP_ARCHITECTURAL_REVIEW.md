# Workflow Generator Deep Architectural Review

**Scope:** `POST /agentic/plan/scratch/preview`, `POST /agentic/plan/execute`, their Stage 0/NL inputs, generated UI/tool capabilities, LangGraph emission, and runtime execution.

**Method:** Read-only source trace plus forensic comparison with the LinkedIn recruitment artifacts under `linkedin-recruiter/`.

**Primary sources:** `backend/scratch_graph_planner.py`, `backend/stage0.py`, `backend/services/ai_processor.py`, `backend/services/backend_generator.py`, `backend/services/angular_generator.py`, `backend/services/gap_tool_generator.py`, `backend/agent_new.py`, and `langgraph/app/executor.py`.

---

## 1. Executive conclusion

The main problem is **not the LangGraph topology definition**. The dominant failure is:

> The pipeline validates that independently generated descriptions, schemas, metadata, and code are structurally consistent with one another, but it rarely proves that the generated capabilities implement the user's business behavior.

The current effective architecture is:

```text
LLM descriptions
  → LLM-planned graph contracts
  → independently generated IR/backend/Angular/tool artifacts
  → syntax, names, shape, and build validation
  → warnings or generic fallbacks when validation cannot repair
  → status=draft
```

This produces a large **self-consistency bubble**. A wrong IR can generate a wrong backend and a matching wrong frontend; all three agree and all validators pass. Conversely, a generator can claim the requested metadata while its executable body returns something entirely different.

The target architecture should be:

```text
Versioned canonical intent + typed contracts + acceptance scenarios
  → constrained graph/capability generation
  → static contract checks
  → generated implementation conformance tests
  → sandboxed integration probes or recorded fixtures
  → workflow-level dry run
  → evidence-driven repair
  → publish only when required gates pass
```

Absolute “perfect in one generation” is not achievable for arbitrary external APIs and open-ended requirements. High first-pass reliability is achievable if “success” means **demonstrated behavior**, rather than “generated and compiled.”

---

## 2. Direct answers to the suspected causes

### Is it a LangGraph definition issue?

**Usually not the primary issue.** The LinkedIn topology—Screen 1 → sourcing tool → Screen 3—was correct. Scratch planning has meaningful topology and path-aware binding checks.

LangGraph/runtime still contributes to silent failure:

- Tool error dictionaries are stored as normal node outputs and execution continues (`langgraph/app/executor.py:630–746`, `backend/agent_new.py:10394–10412`).
- Post-generation validation imports/builds the graph module but does not invoke a representative workflow (`backend/agent_new.py:9705–9752`).
- Cross-phase outputs are flattened by unqualified field name, allowing collisions (`backend/agent_new.py:10124–10134`).

### Is it a prompt or node-description issue?

**Yes, but not simply “write better prompts.”** A concrete execute-time bug destroys rich downstream UI descriptions:

- `_resolve_gap_generations` reconstructs every downstream UI description as only “receives inputs … produces outputs … to render a user interface” (`backend/agent_new.py:8152–8226`).
- `_resolve_one_gap_node` then sends that collapsed description to screen generation (`backend/agent_new.py:7863–7872`, `8086–8099`).

This exactly matches the LinkedIn Screen 3 persisted prompt and explains why detailed profile-card requirements became a generic summary/table.

### Are bindings missed?

**Sometimes, but graph bindings are only one class of binding.** The LinkedIn graph bindings between node outputs were mostly correct. The missed contract was the persistence join:

- The tool stored `jd_id`.
- Screen 3 queried `candidates_batch_id`.

The graph only checked that Screen 3 received a `candidates_batch_id`; it did not prove that persisted records were queryable by that value.

### Are the screen/tool generators at fault?

**Yes, materially.**

- Backend generation can replace failed domain logic with generic CRUD and still register it.
- Tool validation compiles code but does not call the provider or assert output behavior.
- Angular validation proves dependency/build and shallow FE↔BE name alignment, not user interactions or rendered data.

### Why do the advanced planner and validators not prevent this?

Because most validators prove **structure**, and many are explicitly **non-blocking**. They check:

- node/edge existence;
- binding reference existence;
- parameter names and broad types;
- Python/TypeScript syntax;
- Angular build;
- metadata-to-metadata agreement.

They do not reliably check:

- implementation postconditions;
- exact return values/shapes produced by executable code;
- correct external endpoint/method/auth/response mapping;
- persistence write/read key agreement;
- UI fidelity to original acceptance criteria;
- error-path behavior;
- a complete workflow run with representative fixtures.

---

## 3. Complete execution path and trust boundaries

### 3.1 User intent and Stage 0

1. Prompt mode can expand/revise enterprise steps through `backend/nl_builder/`.
2. `/stage0/start` and `/stage0/respond` discover credentials and produce `RequirementSummary`.
3. The handoff has two competing requirement channels:
   - enterprise `steps`;
   - Stage 0-B `requirements_analyzed` serialized into `planner_handoff`.

Architectural issues:

- Stage 0 uses a second, reduced `LogicalStep` model containing only `step_number` and `description` (`backend/stage0.py:364–386`).
- Stage 0-B is instructed to collapse requirements into 3–8 short steps while the enterprise expander says not to collapse them.
- Scratch planning strips the `Workflow steps:` section from Stage 0 context and treats the remainder as constraints.
- Full clarifications are attached to only the first enterprise step in prompt mode.
- `ClarifiedExternalAPI` contains only `provider` and `purpose`; no method, base URL, operation schema, auth placement, version, response example, or test fixture (`backend/stage0.py:217–246`).

Result: the pipeline can enter planning with a detailed human-readable requirement but without a single structured, versioned statement of business invariants and provider contracts.

### 3.2 Scratch preview

`/agentic/plan/scratch/preview` runs:

1. Stage 1 skeleton topology.
2. Stage 1.2 description enrichment.
3. Stage 1.5 prior-global selection.
4. Stage 1.7 persistence architecture.
5. Stage 2a per-node input/output schema and bindings.
6. Stage 2b path-aware validation.
7. Stage 2c state schema derivation.
8. Stage 3 router conditions and validation.
9. Stage 4 graph assembly.
10. Stage 4b graph validation/LLM binding repair.
11. Stage 4c persistence extraction/registry update.

The preview does **not select existing capabilities**. Every non-router/non-end node is assigned `GENERATE:*` (`backend/scratch_graph_planner.py:5032–5047`, `5224–5240`). Therefore, the current scratch path pays the variability cost of fresh generation even if a verified capability already exists.

Important gaps:

- The more comprehensive agent-level `_collect_graph_issues` validator is explicitly skipped in scratch preview (`backend/agent_new.py:13215–13224`).
- Stage 1.2, 1.7, and 4c can fail open with warnings.
- Stage 4b treats unreachable nodes, duplicate state writers, invalid state read/write references, and missing outgoing edges as warnings (`backend/scratch_graph_planner.py:5375–5498`).
- Structured logical-step `inputs` and `outputs` are discarded when converting into `LogicalStepLite` (`backend/scratch_graph_planner.py:363–380`, `6883–6905`), and steps-mode phase conversion also creates empty I/O lists (`backend/agent_new.py:12936–12953`).
- Prior-phase globals are deduplicated by bare output name, losing provenance when multiple nodes emit fields such as `status`, `result`, or `count` (`backend/scratch_graph_planner.py:6910–6935`).
- Stage 4c can write to ArcadeDB during a nominal preview (`backend/scratch_graph_planner.py:6626–6681`).
- Preview persistence failure is a warning; an HTTP success-shaped response can be returned without a durable preview (`backend/agent_new.py:13284–13328`).

What preview proves well:

- skeleton referential integrity and reachability at Stage 1;
- router minimum out-degree;
- required input binding presence;
- ancestor/path legality;
- broad scalar/list compatibility;
- list record field compatibility;
- router target and reference syntax.

What preview does not prove:

- descriptions faithfully preserve every user requirement;
- generated implementations can satisfy the planned contracts;
- external API contracts are real/current;
- persistence writers and readers use the same physical keys;
- runtime error semantics;
- UI acceptance criteria.

### 3.3 Execute and gap resolution

`/agentic/plan/execute` receives preview `phaseGraphs` and, per phase:

1. resolves every `GENERATE:*`;
2. revalidates generated capability names against graph metadata;
3. enriches tool runtime metadata;
4. checkpoints progress;
5. emits per-phase and combined LangGraph code;
6. persists `status: draft`.

#### UI gap resolution

`GenerationSpec.description` + JSON I/O marker are passed to:

```text
AIProcessor
  → IR
  → BackendGenerator
  → AngularAppGenerator
  → screen_generations
  → capability sync
```

Critical issues:

- Downstream UI descriptions are overwritten before generation (`backend/eation does not adopt real IR outputs back into the graph; only the update branch contains that reconciliation (`backend/agent_new.py:7895–8018` versus `8086–8108`).
- The screen generator is called without a structured `workflow_context`; the contract is serialized into text.
- Backend generation exceptions are downgraded to warnings and Angular proceeds with an empty function list and fallback base path (`backend/agent_new.py:26107–26135`).
- Screen persistence and capability sync failures are warnings, but `generate_angular_app_async` still returns `success: True` (`backend/agent_new.py:26238–26312`).

#### Tool gap resolution

The LLM produces JSON containing parameters, output metadata, imports, and function body. Validation:

- rejects mock/placeholder patterns;
- checks imports;
- parses Python syntax;
- compiles the generated dynamic class;
- registers and persists it.

It does **not** execute the function against fixtures or validate the actual returned dictionary against output parameters (`backenagent_new.py:8152–8226`).
- New UI crd/services/gap_tool_generator.py:857–904`).

Additional issues:

- LLM output metadata can override the workflow output contract.
- Retry/resume creates a new UUID-based tool key, so gap generation is not idempotent.
- External API research support is narrow; provider endpoints are generally generated from prompt/model knowledge rather than a verified OpenAPI/provider contract.
- Registration and capability persistence are not transactional, so an orphan JSON tool can remain if capability upsert fails.

#### Post-gap validation

`_revalidate_after_gap_gen` checks:

- every graph declared-output **name** exists in capability output metadata;
- every binding key exists in capability input metadata;
- every required capability input has a graph binding.

It does not check:

- parameter types or nested item schemas;
- executable return keys;
- persistence behavior;
- external API behavior;
- semantic equivalence.

More seriously:

- missing capability docs cause validation to be skipped (`backend/agent_new.py:8333–8383`);
- failed arbitration is skipped;
- `regenerate` is temporarily disabled (`backend/agent_new.py:8575–8582`);
- adapted nodes are not run through another deterministic validation pass before return.

### 3.4 IR, backend, Angular, and persistence

#### IR (`ai_processor.py`)

IR generation has enhancement, self-verification, JSON-schema validation, repair, callability checks, and data-source enrichment.

However:

- validation is primarily JSON shape/referential validation;
- after repair exhaustion, best-effort invalid IR is returned (`backend/services/ai_processor.py:5710–5807`);
- T5/T7 repair failures retain the original IR;
- self-verification can be skipped when parameter name/type signatures match, ignoring nested shape and semantics (`backend/services/ai_processor.py:1978–2022`, `2837–2850`);
- heuristic repair can inject a generic insert data source when none exists;
- repair is allowed to make required model fields optional to remove callability warnings.

This can convert a missing implementation into a structurally valid but semantically weaker contract.

#### Backend (`backend_generator.py`)

The backend generator validates and repairs syntax, model binding, Pydantic schemas, and light security properties.

But it explicitly optimizes for saving something:

- no generated source → generic external/dynamic template (`backend/services/backend_generator.py:3949–3963`);
- generic dynamic insert returns `{recordId, collectionName, status}` regardless of the declared domain response (`5910–6010`);
- no compiled function → persisted runtime stub (`3965–3984`);
- per-function failures are logged and omitted from the result (`1477–1596`);
- unresolved IO↔IR mismatch is saved anyway (`3200–3215`).

T6 compares model and exported schema metadata to IR. It does not inspect or execute the function to prove the body returns those fields or implements `databaseLogic`.

#### Angular (`angular_generator.py`)

Angular generation provides:

- dependency/import checks;
- TypeScript/template build recovery;
- request-side required-key audit;
- response TypeScript-interface field audit;
- deployment.

T8 is regex-based and intentionally non-blocking:

- dynamic/spread/computed params can be “undetermined” and skipped;
- `any`, `unknown`, `object`, or untyped responses skip response verification;
- it checks interface declarations, not whether the UI binds and renders fields;
- unresolved failures proceed with a warning (`backend/services/angular_generator.py:6220–6497`).

The Angular success criterion is therefore “builds and deploys,” not “performs the requested workflow behavior.”

### 3.5 Generated LangGraph and runtime

Combined code generation:

- creates FREEZE deterministic graphs or DYNAMIC ReAct subgraphs;
- checks AST and imports the module in a subprocess;
- repairs module-level errors.

It does not perform a representative invocation. Codegen failure becomes an empty string/file and warning (`backend/agent_new.py:16105–16170`).

At runtime:

- tool exceptions are converted to `{error, traceback}` dictionaries (`langgraph/app/executor.py:630–746`);
- FREEZE `_execute_dynamic_node` records that dictionary as a successful node output and continues (`backend/agent_new.py:10356–10427`);
- no output-schema assertion is performed before downstream binding;
- no node-level policy distinguishes retryable provider errors, business rejection, and success.

This explains how zero-result/error outcomes can propagate to a blank downstream screen without failing the workflow.

---

## 4. LinkedIn recruitment forensic chain

The LinkedIn workflow is not one isolated LLM miss. It exposed six independent contract gaps.

### 4.1 Generic Screen 1 backend

The requested Screen 1 behavior was:

- select or upload a JD;
- call Gemini for extraction;
- persist `job_description`;
- return `jd_id`, `candidate_count`, and flat `jd_*` fields.

The generated function was a generic insert template returning `{recordId, collectionName, status}`.

Why it passed:

- the template compiled;
- backend metadata still advertised the IR response model;
- T6 compared metadata/model fields, not executable returns;
- Angular compiled against the advertised metadata;
- workflow runtime received missing values and propagated defaults.

This is directly enabled by `_build_dynamic_template` and fail-open orchestration.

### 4.2 List-of-string versus list-of-object failure

The user requirement described arrays of strings. Scratch `NodeParam` requires every `datatype="list"` to have a non-empty `itemSchema` (`backend/scratch_graph_planner.py:264–287`). The planner therefore represented fields as arrays of records such as:

```text
required_skills: [{skill: string}]
```

The implementation returned:

```text
required_skills: ["Python"]
```

Pydantic correctly rejected it at runtime.

This is not just a bad prompt. The planner type system cannot cleanly represent `list<string>`; `list` means a structured record collection. That forces a semantic conversion the generated business code may not understand.

### 4.3 Wrong Unipile endpoint/configuration

The generated tool used approximate endpoints and initially did not normalize a host-only DSN with `https://`.

Why it passed:

- the tool body compiled;
- imports resolved;
- no provider contract was retrieved or pinned;
- no sandbox/fixture request checked method/path/auth;
- output metadata was accepted without running the tool.

### 4.4 Persistence join-key mismatch

The tool and Screen 3 disagreed on `jd_id` versus `candidates_batch_id`.

Why it passed:

- graph bindings described only the value passed to Screen 3;
- persistence registry extraction was LLM-driven and non-fatal;
- no write/read lineage validator proved both capabilities addressed the same collection/key/value;
- T8 only checked function call fields.

### 4.5 Candidate name/enrichment problems

Provider responses omitted or varied identity fields. Enrichment initially replaced rather than merged search data; later UI fallback treated a headline as a name.

Why it passed:

- response schemas modeled names as generic optional strings;
- no provider fixture covered missing names;
- no invariant stated that `candidate_name` must not derive from `headline`;
- no semantic sample assertion rendered realistic provider payloads.

### 4.6 Screen 3 UI fidelity loss

The original Step 3 required profile cards, photo, score badges/breakdown, matched skills, sorting/filtering, and counts.

The persisted generation prompt became:

> “This screen receives inputs candidates_batch_id, fetched_candidates_count, candidate_count and produces outputs user_acknowledged to render a user interface.”

That wording is generated by the execute-time overwrite at `backend/agent_new.py:8210–8226`. The screen generator never received the detailed UX requirement, so the generic table was a predictable result.

---

## 5. Architectural failure matrix

| Failure class | Introduced at | Why current validation misses it | User symptom |
|---|---|---|---|
| Requirement collapse | Stage 0 / downstream description rewrite | No immutable intent manifest or fidelity diff | Missing UX/business behavior |
| Wrong topology | Skeleton LLM | Stage 1 catches many structural cases | Missing/incorrect step |
| Wrong binding source | Stage 2 / execute refresh | Name/type checks cannot prove semantic identity | Wrong upstream data |
| Primitive-list mismatch | Planner type system | `list` requires record `itemSchema` | Pydantic 500 |
| Generic backend fallback | Backend codegen | Compile and metadata pass; body not tested | Empty/wrong outputs |
| Missing backend function | Per-function generation | Exception logged, function omitted | UI call absent/fails |
| Wrong provider endpoint | Tool/backend codegen | No verified provider contract or probe | Fast zero results/API error |
| Wrong Mongo join key | Tool + screen generation | No physical data-lineage check | Empty table |
| FE request mismatch | Angular codegen | T8 catches only statically visible required keys | 4xx/500 |
| FE response/render mismatch | Angular codegen | Interface check is not render assertion | Blank/misleading UI |
| Tool returns error dict | Runtime | Error payload is ordinary node output | Workflow continues incorrectly |
| Capability persistence failure | Screen/tool persistence | Fetch failure causes 6p skip | Graph points to missing artifact |
| LangGraph emission failure | Execute | Warning + empty code allowed | Draft cannot run |
| Cross-phase key collision | State bridge | Bare-name dedupe/flatten | Wrong `status`/`result` used |
| Concurrent screen generation | Shared singletons/registry | No request-scoped generator state | Wrong metadata/functions |

---

## 6. Hidden coupling and unsafe assumptions

### 6.1 Multiple contract dialects

The same logical field crosses:

- `LogicalStep.inputs/outputs`;
- scratch `NodeParam.datatype/itemSchema`;
- `GenerationIOParam.dataType`;
- graph `declaredOutputs/inputBindings`;
- IR `inputParameters/outputParameters.type`;
- IR `dataModels`;
- Pydantic classes;
- backend exported schemas;
- Python return dictionaries;
- tool `param_type/outputParameters`;
- TypeScript interfaces;
- screen runtime `complete()` payload;
- Mongo physical field names.

Each translation is handwritten and partly lossy. There is no canonical schema ID/hash proving all artifacts were generated from the same contract version.

### 6.2 Self-reported authority

Generated capabilities publish their own metadata. Post-gap validation compares the graph to that metadata, not the metadata to executable behavior. A function can claim output `jd_id` and return `recordId`; validators see the claim.

### 6.3 “Repair” may change requirements

Several repair loops mutate contracts to match generated artifacts:

- post-gap `adapt` may rename graph inputs/outputs;
- IR T5 can mark required fields optional;
- backend rebinds schemas to IR without proving the function body;
- runtime strips unexpected parameters.

This optimizes internal compatibility and can silently move away from user intent.

### 6.4 Graph list order as execution-generation order

Gap resolution iterates `graph.nodes` rather than an explicit topological schedule (`backend/agent_new.py:8137–8254`). Scratch uses topological ordering for Stage 2 batches, but graph assembly preserves skeleton node order. A valid edge graph does not guarantee its serialized node array is topological.

### 6.5 State name collisions

Planner prior globals are deduplicated by parameter name; runtime bridges flatten nested node outputs by `dict.update`. Multiple `status`, `count`, or `result` fields can silently overwrite one another. Provenance exists in graph bindings but is lost in `global_inputs`.

### 6.6 Shared mutable process state

Module-level `ai_processor`, `backend_generator`, `angular_generator`, token tracker, and `FunctionRegistry` are shared. Generation resets metrics and `FunctionRegistry.clear_project(project_id)` while multi-screen generation can run concurrently. This risks:

- incorrect billing attribution;
- stale `last_enhanced_prompt`;
- functions from one screen being removed by another screen generation;
- inconsistent in-memory behavior across multiple server processes.

### 6.7 “Draft” conflates generated and verified

`status: draft` is written after gaps are syntactically resolved, even if warnings report:

- backend omission;
- T8 failure;
- capability sync failure;
- empty combined code;
- persistence failure.

The status model lacks a mandatory `generated_unverified`, `verification_failed`, and `verified` distinction.

---

## 7. Recommended architecture

### P0 — Stop known-bad workflows from being published

1. **Remove the downstream UI description overwrite.**
   - Preserve immutable `intentSpec`.
   - Store derived I/O summaries separately.
   - Never assign a summary back to `generation.description`.

2. **Make execute fail closed on critical artifacts.**
   - Backend function missing/uncompiled.
   - IR still invalid.
   - T8 unresolved.
   - Capability not persisted/fetchable.
   - Post-gap contract mismatch.
   - Empty or non-importable combined LangGraph code.
   - Process configuration persistence failure.

3. **Reject generic fallbacks for domain behavior.**
   - Generic CRUD may be offered as an explicit user-visible fallback, never saved as the implementation of a richer `databaseLogic`.
   - Add `generation_mode: exact | fallback` and prevent `verified` status for fallback.

4. **Treat tool error results as failures.**
   - Define a standard node result envelope:

   ```text
   {ok, data, error, retryable, provider_status, diagnostics}
   ```

   - Route or stop on `ok=false`; never bind error payloads as normal declared outputs.

5. **Fix the type system.**
   - Represent `array<T>` where `T` may be scalar, enum, record, union, or nullable.
   - Remove the assumption that every list requires a record `itemSchema`.

### P1 — Establish one canonical contract

Create a versioned `WorkflowContractManifest` as the source of truth:

- immutable user intent and per-node acceptance criteria;
- node topology and control-flow conditions;
- canonical JSON Schema for every input/output;
- persistence operations with collection, key, and lineage;
- provider operations with method/path/auth/version/request/response schemas;
- error and retry policy;
- UI interaction/render assertions;
- contract version/hash.

Generate scratch graph, IR, backend models, tool schemas, Angular interfaces, and runtime checks from this manifest. Generators should fill implementations, not redefine contracts.

### P1 — Add executable conformance gates

For each generated backend/tool:

1. compile/import;
2. run with generated fixtures;
3. validate actual return against canonical JSON Schema;
4. assert required side effects through a fake DB/HTTP adapter;
5. assert error-path behavior;
6. produce machine-readable evidence.

For each screen:

1. build;
2. mount with representative workflow inputs;
3. intercept backend calls and validate request schemas;
4. feed fixture responses;
5. assert required labels/components/rows/cards;
6. trigger submit/complete and validate output schema.

For the complete workflow:

1. compile graph;
2. run a deterministic dry-run with UI resumes and mocked tools;
3. verify each node's preconditions/postconditions;
4. verify every branch at least once where feasible;
5. verify persisted writer→reader lineage.

### P1 — Ground external integrations

- Require a pinned OpenAPI/provider operation or explicitly approved documentation snapshot.
- Stage 0 should collect structured provider operation information, not only provider name.
- Generate HTTP calls from a typed operation model.
- Validate against recorded response fixtures.
- Run read-only/sandbox probes when credentials and provider support allow.
- Block live side-effecting tests unless explicitly safe.

### P1 — Deterministic persistence compiler

Replace free-text persistence extraction with explicit operations:

```text
write:
  collection: all_Candidates
  key: candidates_batch_id
  valueFrom: sourcing.candidates_batch_id

read:
  collection: all_Candidates
  filter:
    candidates_batch_id: display.candidates_batch_id
```

Compile both tool and screen data access from this model. Fail if a reader has no matching writer or if key types differ.

### P2 — Repair from evidence, not prose

The repair loop should receive:

- failing fixture;
- actual versus expected output;
- stack trace/provider response;
- violated manifest clause;
- changed files;
- allowed repair scope.

After repair, rerun the same test. Do not let the LLM “adapt” the graph to a broken implementation unless the user intent manifest permits that change.

### P2 — Reliability and concurrency

- Make tool keys deterministic per `(workflow, phase, node, contractVersion)`.
- Generate nodes in topological order.
- Use request-scoped generator/tracker objects.
- Remove `FunctionRegistry.clear_project` from per-screen generation or namespace functions by screen/version.
- Add workflow build leases/idempotency keys to direct execute as well as queue jobs.
- Make artifact + capability + Process_Configs publication transactional or use a staged commit.
- Persist checkpoints before external side effects and retain them until final commit.

### P2 — Better status and observability

Track separate states:

```text
planned
generated_unverified
verifying
verification_failed
verified_draft
published
```

Persist every gate:

- input contract hash;
- artifact contract hash;
- compile result;
- conformance result;
- integration-fixture result;
- workflow dry-run result;
- warnings classified as blocking/non-blocking.

---

## 8. Practical implementation sequence

### Phase 1: immediate correctness fixes

1. Preserve downstream UI descriptions.
2. Synchronize new-screen IR contracts back into graph nodes.
3. Revalidate adapted nodes; re-enable bounded regenerate or fail.
4. Block draft on missing capability, backend function, T8 failure, empty code, or persistence failure.
5. Remove automatic generic CRUD publication.
6. Treat runtime `error` dictionaries as failed node results.
7. Add primitive array support.

### Phase 2: contract consolidation

1. Define canonical JSON Schema-based node contracts.
2. Make graph, IR, backend, tool, Angular, and runtime reference contract IDs/hashes.
3. Model persistence lineage and external API operations structurally.
4. Eliminate duplicate `LogicalStep`/parameter dialects through adapters generated from the canonical schema.

### Phase 3: executable verification

1. Generate fixtures and acceptance scenarios during planning.
2. Add backend/tool conformance harness.
3. Add Angular interaction tests with mocked backend.
4. Add full workflow dry-run.
5. Add evidence-driven, scoped repair.

### Phase 4: quality measurement

Build a regression corpus from real workflows, including LinkedIn recruitment:

- nested/primitive arrays;
- external providers;
- multi-screen persistence;
- missing provider fields;
- pagination and throttling;
- routers and retry branches;
- duplicate output names across phases;
- concurrent multi-screen generation.

Measure:

- first-pass contract pass rate;
- first-pass workflow dry-run pass rate;
- semantic acceptance pass rate;
- repair success by failure class;
- fallback usage;
- escaped runtime defects.

Do not optimize primarily for “generation completed” or Angular build pass rate.

---

## 9. Areas deserving deeper investigation

1. **Production failure telemetry:** classify recent failed workflows by contract, provider, persistence, UI, and runtime error category.
2. **Exact preview-versus-execute description drift:** persist hashes and diffs at each stage.
3. **IR heuristic fallback frequency:** determine how often generic data sources/templates are used.
4. **Backend body conformance:** scan generated functions for declared-versus-actual return-key mismatches.
5. **Provider integration accuracy:** audit endpoints/methods against pinned docs for top providers.
6. **Mongo lineage:** find reader filters with no matching writer field across generated screens/tools.
7. **Runtime error propagation:** quantify tool error dictionaries followed by successful downstream execution.
8. **Cross-phase output collisions:** detect duplicate bare names and compare planner source selection with runtime flatten order.
9. **Concurrency:** reproduce simultaneous screen generation under one project and inspect `FunctionRegistry`, token metrics, and prompt metadata.
10. **Resume/idempotency:** simulate credit pause after tool registration but before graph checkpoint.
11. **Capability reuse:** evaluate whether verified generated capabilities can be safely retrieved instead of regenerating every scratch node.
12. **Security boundary:** generated code currently imports/executes dynamically; a separate security audit should review sandboxing, credential exposure, SSRF, package installation, and DB access.
13. **Test coverage:** there are no focused unit/integration tests for Stage 0→scratch handoff, preview validators, post-gap reconciliation, or end-to-end generation contracts.

---

## 10. Bottom line

The system is advanced in the number of stages, but stage count does not equal correctness. The stages mostly generate and reconcile **descriptions of behavior**. When those descriptions are wrong, collapsed, or contradicted by executable code, later stages often validate the same wrong metadata and proceed.

For LinkedIn recruitment:

- topology was mostly correct;
- graph bindings were mostly correct;
- the downstream UI prompt was concretely destroyed during execute;
- the backend fell back to generic CRUD;
- the planner type system forced string arrays into record arrays;
- provider behavior was never contract-tested;
- persistence lineage was never validated;
- runtime errors/results were allowed to continue.

The highest-leverage change is not another general LLM review prompt. It is to make the pipeline **contract-first, executable-test-driven, and fail-closed at publication boundaries**.

