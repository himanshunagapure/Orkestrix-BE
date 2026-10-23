# Workflow Regeneration Flow — Implementation Guide

This document describes the **current production implementation** of the three-phase regeneration pipeline, how it maps to the design in [`regenerationDesign.txt`](./regenerationDesign.txt), and exactly what each file/phase does.

It is written from a deep read of:

| Module | Role |
|--------|------|
| `backend/impact_analysis.py` | Phase 1 engine |
| `backend/surgical_editing.py` | Phase 2 engine |
| `backend/validation_repair.py` | Phase 3 engine |
| `backend/integrated_pipeline.py` | Orchestrator |
| `backend/regeneration.py` | Legacy schema/blast-radius utilities + fallback helpers |
| `backend/agent_new.py` | Production integration (`/plan/update` and related endpoints) |
| `backend/services/angular_generator.py` | Real surgical UI code edits |

---

## 1. What problem this solves

Before this pipeline, updating a workflow node often meant **full regeneration** of schemas and code. That was expensive, fragile, and could overwrite unrelated logic.

The regeneration flow instead:

1. **Analyzes** what a change will break (including retry/fallback cycles).
2. **Waits for approval** when impact is non-trivial (frontend gate; backend can auto-approve).
3. **Edits surgically** — only affected nodes/files.
4. **Validates and repairs** contracts, graph integrity, syntax, and cycle bindings.
5. **Cascades** only to nodes the impact report flagged.

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Client / UI                                                     │
│  - preview_only=true  → Phase 1 only, show ImpactReport          │
│  - approve            → /plan/update with same operations        │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  agent_new.py  —  POST /aiqod-agent/agent/agentic/plan/update    │
│                                                                  │
│  1. Normalize operations (DELETE → UPDATE → INSERT)              │
│  2. Mark nodes (_requires_regeneration, _update_request)         │
│  3. Build proposed_changes                                       │
│  4. Call integrated pipeline (or reuse cached preview)           │
│  5. Apply edit plans → Angular updates → downstream cascade      │
│  6. Gap resolution + tool reconciliation + LangGraph compile     │
└───────────────────────────────┬──────────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Phase 1          │  │ Phase 2          │  │ Phase 3          │
│ impact_analysis  │  │ surgical_editing │  │ validation_repair│
│                  │  │                  │  │                  │
│ Bidirectional    │  │ EditPlan markers │  │ 6(+cycle) checks │
│ blast radius     │  │ + DiffApplicator │  │ + RepairEngine   │
│ Tarjan cycles    │  │ (real UI work in │  │ max repair iters │
│ Severity class   │  │  Angular path)   │  │                  │
└──────────────────┘  └──────────────────┘  └──────────────────┘
          ▲                     ▲                     ▲
          └─────────────────────┴─────────────────────┘
                                │
                    integrated_pipeline.py
                    (gates, callbacks, iterations)
```

**Design intent (from `regenerationDesign.txt`):** three phases + approval gate + iterative self-heal.

**Production reality:** same three engines exist, but the **real code mutation** for UI screens happens *after* the orchestrator returns success — inside `agent_new.py` via `update_angular_screen_async` / gap resolution. Phase 2’s `surgical_editing.py` mostly emits **markers / plans**; it does not rewrite Angular files by itself in the live `/plan/update` path.

---

## 3. End-to-end request lifecycle

### 3.1 Preview (Phase 1 only)

`WorkflowUpdatePayload.preview_only = true`

1. Load `Process_Configs.agentic_process.phaseGraphs`.
2. For each `UPDATE` operation:
   - Resolve UI step id → internal `nodeId`.
   - Set `config._requires_regeneration = True`.
   - Set `config._update_request = prompt`.
3. Build `proposed_changes` via `create_proposed_changes_from_node_update`.
4. Optionally dry-run LLM schemas via `regeneration.get_proposed_node_schemas` and merge into `proposed_changes`.
5. Load Angular codebase (`_load_current_codebase`) + screen backend metadata.
6. Run `run_impact_analysis(..., use_llm_analysis=True)`.
7. Cache under `agentic_process.pending_update_preview`:
   - `operations`, `proposed_changes`, `impact_report`, `updated_node_ids`, `created_at`
8. Return `impact_report` + `user_approval_required` — **no graph mutation persisted for apply**.

### 3.2 Apply (full pipeline)

Same endpoint with `preview_only=false` (default):

1. If cached preview ops signature matches current ops → reuse `proposed_changes` and skip fresh Phase 1 (`precomputed_impact_report`).
2. Apply structural ops first (DELETE rewires edges; INSERT may ask router clarification).
3. For UPDATEs: mark regeneration flags again.
4. Call `run_regeneration_with_auto_approval(...)` with **`max_iterations=1`** in production (prevents “add one more field” snowballing on retries).
5. On success (`pipeline_used = "integrated"`):
   - `_apply_edit_plans_to_phase_graphs` merges schemas into phase graphs.
   - Sync new outputs from impact report onto directly modified nodes.
   - For each updated `ui_screen`: `update_angular_screen_async` (selective Angular regen + new `screen_id` for rollback).
   - Regenerate `generationDescription` via `regenerate_gap_resolution`.
   - Cascade downstream nodes flagged by impact report (`ui_screen` / `tool`).
   - Queue remaining UI screens into `gap_generations`.
6. Continue with gap resolution, post-gap revalidation, downstream tool reconciliation, LangGraph compile, persist new version.

> **Note on fallback:** Design text mentions automatic fallback to legacy. Current `/plan/update` path **re-raises** integrated-pipeline failures (`NO LEGACY FALLBACK`). Legacy `run_update_regeneration_pipeline` is still used for **downstream tool regeneration** and as a utility library.

---

## 4. Phase 1 — Impact Analysis (“What will break?”)

**File:** `backend/impact_analysis.py`  
**Entry points:**

- `run_impact_analysis(...)` — main API
- `ImpactAnalyzer.analyze_impact(...)` — static graph analysis
- `ImpactAnalyzer.analyze_impact_llm(...)` — static + LLM enhancement for UI screens
- `format_impact_report_for_user(...)` — human-readable report

### 4.1 Approach

Two layers:

1. **Static graph analysis** (always runs)
   - Build forward/backward adjacency from edge `kind`
   - Detect cycles with **Tarjan’s SCC algorithm**
   - Compute **bidirectional blast radius**
   - Diff proposed vs current `declaredOutputs` / `inputBindings`
   - Classify severity

2. **LLM enhancement** (when `use_llm_analysis=True` and codebase available)
   - For directly modified `ui_screen` nodes (and some downstream screens with new upstream outputs)
   - Replicates Angular `analyze_update_scope` prompt logic
   - Returns affected files/components/backend functions
   - Converts that into `ImpactReport` change entries

### 4.2 Graph indexing (`ImpactAnalyzer._build_indices`)

Edges are classified by `kind`:

| Kind set | Examples | Adjacency treatment |
|----------|----------|---------------------|
| `CYCLE_EDGE_KINDS` | `retry`, `loop`, `human_loop`, `compensation` | Backward + all (cycle detection) |
| `ALTERNATIVE_PATH_KINDS` | `fallback` | Forward (error path consumer, not treated as cycle by kind alone) |
| `BACKWARD_DEPENDENCY_KINDS` | `retry`, `human_loop`, `compensation` | Backward dependency for impact |
| Default | `sequential` (and others) | Forward |

Data structures:

- `forward_adjacency` / `forward_reverse`
- `backward_adjacency` / `backward_reverse`
- `all_adjacency` — used by Tarjan
- `cycles: Set[str]` — nodes that participate in any SCC cycle

### 4.3 Cycle detection (Tarjan)

`_detect_cycles()`:

- Computes strongly connected components.
- Any SCC with size > 1 → all members are cycle nodes.
- Size-1 SCC with a self-loop → also a cycle node.

This matches the design’s “retry loop puts upstream A in blast radius when B changes.”

### 4.4 Blast radius (`_calculate_blast_radius`)

BFS from seed (directly updated) nodes:

1. Always expand **forward** neighbors.
2. If current node ∈ `cycles`, also expand **backward** neighbors.

So retry/human-loop participants on the other side of a changed node are included.

### 4.5 Severity classification

`ImpactSeverity`:

| Value | Meaning | Typical causes |
|-------|---------|----------------|
| `NONE` | No meaningful change | Unrelated / no diffs |
| `SAFE` | Compatible change | New output, input binding add/change, UI prompt regen without known removals, backend code change flagged from LLM |
| `BREAKING` | Downstream contracts break | Output removed/renamed/type-changed; component-add/remove from LLM file analysis |
| `STRUCTURAL` | Topology change | Node/edge add/remove (enum exists; structural ops often handled before impact in `/plan/update`) |

`ChangeType` covers bindings, outputs, edges, router rules, generation description, backend code, and `TOOL_INPUT_AVAILABLE_FROM_UPSTREAM` (non-breaking opportunity for downstream tool/UI to consume a new field).

### 4.6 Direct vs downstream analysis

**Directly modified node** (`_analyze_direct_changes`):

- Diff proposed vs current outputs/inputs.
- Special case: UI screen with `_update_request` but empty proposed outputs → “outputs unknown, LLM will regenerate” → `SAFE`, avoid false “all outputs removed”.
- UI prompt alone → `GENERATION_DESCRIPTION_CHANGED`.

**Downstream node** (`_analyze_downstream_impact`):

- Find modified upstream (forward parents + cycle-related).
- If binding references removed upstream output → `BREAKING`.
- If upstream has update request and outputs may change → `SAFE` warning.
- If upstream adds unbound new outputs and downstream is `tool` or `ui_screen` → `TOOL_INPUT_AVAILABLE_FROM_UPSTREAM` (`SAFE`) so cascade includes that node.

### 4.7 LLM path details

For UI screens, `_analyze_update_scope_with_llm`:

- Truncates codebase (max 80 files, 10k chars each).
- Asks for JSON: `affected_files`, `affected_components`, `affected_backend_functions`, `requires_backend_update`.
- Maps to impact changes; component-add/remove → `BREAKING`; backend-only → usually `SAFE`.

### 4.8 Approval gate

`ImpactReport.user_approval_required`:

- Static path: `True` if any affected nodes exist.
- LLM rebuild path: `True` if any `BREAKING` or `STRUCTURAL`.

In production `/plan/update`, backend uses **auto-approval**. The UI is expected to call preview first and only submit apply after the user confirms.

### 4.9 Important types

```text
NodeImpact
  node_id, node_name, node_type
  is_directly_modified
  severity
  changes[]
  affected_downstream[]
  requires_regeneration
  regeneration_reason

ImpactReport
  workflow_id
  directly_modified_nodes[]
  all_affected_nodes[]
  summary{}
  breaking_changes / safe_changes
  user_approval_required
  to_dict() / from_dict()   # for preview cache
```

---

## 5. Phase 2 — Surgical Editing (“Fix only what’s broken”)

**File:** `backend/surgical_editing.py`  
**Entry points:**

- `generate_surgical_edit_plan(...)`
- `DiffGenerator.generate_edit_plan(...)`
- `DiffApplicator.apply_edit / apply_plan`
- `format_edit_plan_for_user(...)`

### 5.1 Design vs implementation

| Design claim | Implementation today |
|--------------|----------------------|
| Minimal search/replace diffs applied to code | Framework exists (`CodeEdit`, `DiffApplicator`) |
| Node-type generators produce real diffs | Partially — generators mostly emit **markers** |
| Preserve untouched code 100% | Achieved for UI via Angular selective regen *outside* this module |

### 5.2 Core data structures

```text
EditType: REPLACE | INSERT | DELETE | RENAME

CodeEdit
  file_path, edit_type, target_symbol
  search_block, replace_block
  line_start / line_end
  description

EditPlan
  node_id, node_name, node_type
  edits[]
  affected_files[]
  requires_backend_update
  backend_edit_plan?
```

### 5.3 Per-node-type approach

`DiffGenerator` dispatches on `node.type`:

#### `ui_screen` → `_generate_ui_screen_edits`

- Builds/uses generation IR from node config (or minimal IR from declared outputs).
- Emits a single marker edit:
  - `file_path = "angular_workspace"`
  - `target_symbol = "ui_screen_update"`
  - `replace_block` = JSON `{node_id, prompt, updated_ir_schema, current_code}`
- `DiffApplicator` treats this as success without mutating files (“queued for Angular regeneration”).
- **Actual file surgery** happens later in `update_angular_screen_async` →
  `AngularAppGenerator.analyze_update_scope` → `regenerate_files_for_update`.

#### `tool` → `_generate_tool_edits`

- Does **not** call LLM during Phase 2 (timeout avoidance).
- Emits `file_path = "tool_regeneration_marker"` with updated inputs/outputs/description JSON.
- Real tool code regeneration happens via gap resolution / `run_update_regeneration_pipeline`.

#### `agent:workflow` → `_generate_agent_edits`

- Marker pointing at referenced sub-workflow id for update.

#### Unknown types → `_generate_generic_edits`

- Manual-review marker.

Helper methods `_generate_tool_diff` / `_generate_tool_from_spec` exist for LLM search/replace generation but are **not** on the main production Phase-2 path today.

### 5.4 How production applies Phase 2 results

After orchestrator success, `agent_new.py`:

1. **`_apply_edit_plans_to_phase_graphs`**
   - Merges `proposed_changes` into node `declaredOutputs` / `inputBindings` / `generationDescription`.
   - Merge is additive for outputs (protects against LLM replacing fields with generic `formOutput`).
   - Records `_applied_update_prompts` for idempotency on retries.

2. **Angular selective update** for directly modified UI screens
   - Builds a constrained `gen_prompt` (user request + previous description + I/O contract + no-generic-outputs rules).
   - Calls `update_angular_screen_async`.
   - Creates a **new screen_id / version row** for rollback safety.
   - Rewrites `capabilityRef` via `_finalize_ui_screen_update`.

3. **Downstream cascade** from impact report
   - Downstream `ui_screen` → `_regenerate_downstream_ui_screen`
   - Downstream `tool` → mark `_requires_regeneration` + run legacy `run_update_regeneration_pipeline`

So “surgical editing” in practice is a **hybrid**: Phase 2 plans + Angular selective file regen + schema merge helpers.

---

## 6. Phase 3 — Validation & Repair (“Did it work?”)

**File:** `backend/validation_repair.py`  
**Entry points:**

- `run_validation_and_repair(...)`
- `WorkflowValidator.validate_all(...)`
- `RepairEngine.repair(...)`
- `format_validation_report(...)`

### 6.1 Validation categories (design: 6 + cycle)

| Category | Enum | What it checks | Auto-fixable? |
|----------|------|----------------|-----------------|
| Graph Integrity | `GRAPH_INTEGRITY` | Uses `regeneration.validate_graph`; dangling edges; disconnected nodes | Mostly no |
| Contract Compliance | `CONTRACT_COMPLIANCE` | `{{node.output}}` bindings resolve to known sources/outputs | Yes (intended) |
| Type Safety | `TYPE_SAFETY` | Expected vs actual data types on bindings | No (stub expected types) |
| Syntax Validity | `SYNTAX_VALIDITY` | `compile()` on `.py` files in codebase dict | Yes (LLM patch) |
| Backend Compatibility | `BACKEND_COMPAT` | Scans FE HTTP calls (stubby; not fully wired to route registry) | No |
| Cycle Contracts | (reported under `CONTRACT_COMPLIANCE`) | For nodes in Tarjan cycles: bindings vs forward+backward sources; missing cycle outputs | Partial |
| Workflow Execution | `WORKFLOW_EXECUTION` | Defined but **not run** (`validate_all` leaves it commented) | — |

### 6.2 Cycle contract check (design addition)

`_validate_cycle_contracts`:

1. Build `ImpactAnalyzer` per phase → get `cycles`.
2. For each cycle node, gather data sources from:
   - forward upstream (`forward_reverse`)
   - backward adjacency (retry/fallback sources)
3. Validate each binding reference against those sources/types.
4. ERROR if referenced cycle output no longer exists (“retry will fail”).
5. WARNING when both ends are in a cycle (verify compatibility).

### 6.3 Repair loop

`RepairEngine.repair(report, max_iterations)`:

1. Collect ERROR issues with `auto_fixable=True`.
2. Apply category-specific fix:
   - Contract → rewrite binding (currently `_find_valid_reference` often returns `None` — limited in practice).
   - Syntax → LLM rewrites local context around the error line into `codebase`.
   - Type → no auto-fix.
3. Re-run `validate_all`.
4. Stop on pass or when no more auto-fixable errors / max iterations.

`run_validation_and_repair` default: **max 3 repair iterations**.  
Inside `integrated_pipeline`, Phase 3 is called with **`max_repair_iterations=2`**.

### 6.4 Orchestrator iteration interaction

`run_integrated_regeneration_pipeline`:

```
for iteration in 1..max_iterations:
  generate edit plans
  (optional) user approve edit plan
  apply edits to codebase dict
  run validation+repair
  if passed → COMPLETE
  else optional callback to continue
  update proposed_changes from contract fixes
```

Production `/plan/update` sets **`max_iterations=1`**, so outer self-heal is effectively one pass; failures raise rather than looping.

---

## 7. Orchestrator — `integrated_pipeline.py`

**Purpose:** Sequence Phase 1 → approval → Phase 2 → Phase 3 with shared state and callbacks.

### 7.1 Key types

```text
PipelinePhase
  impact_analysis | user_approval | surgical_editing
  | validation_repair | complete | failed

PipelineState
  phase, workflow_id, updated_node_ids, proposed_changes
  impact_report, edit_plans, applied_edits
  validation_report, applied_fixes
  success, error_message, iteration, max_iterations

PipelineCallbacks
  on_impact_report(ImpactReport) -> bool
  on_edit_plan([EditPlan]) -> bool
  on_validation_report(ValidationReport) -> bool
  on_progress(message, data)
```

### 7.2 Public runners

| Function | Behavior |
|----------|----------|
| `run_integrated_regeneration_pipeline` | Full control + callbacks |
| `run_regeneration_with_auto_approval` | No callbacks → auto-approve (used by `/plan/update`) |
| `run_regeneration_with_user_callbacks` | Explicit UI callbacks |
| `create_proposed_changes_from_node_update` | Builds proposed_changes stub from node + prompt; idempotent via `_applied_update_prompts` |

### 7.3 `proposed_changes` shape

Per node id:

```json
{
  "inputBindings": { "param": "{{upstream.field}}" },
  "declaredOutputs": [ { "name": "...", "dataType": "...", ... } ],
  "generationDescription": "...",
  "_update_request": "user natural language prompt"
}
```

Important: `create_proposed_changes_from_node_update` **does not** heuristic-parse “add one more number” into new outputs anymore. It keeps current outputs and lets Angular/IR LLM discover additions. Preview path may enrich via `get_proposed_node_schemas`.

---

## 8. Legacy utilities — `regeneration.py`

Still foundational; used by Phase 3 and downstream cascades.

| Symbol | Role |
|--------|------|
| `WorkflowUpdatePayload` | API model; includes `preview_only` |
| `build_node_batch_plan` | Topological batches of real nodes |
| `calculate_blast_radius_for_update` | **Forward-only** batch blast radius (legacy) |
| `build_available_vars_register` | Globals + node outputs for binding validation |
| `validate_graph` | Binding/edge integrity |
| `get_proposed_node_schemas` | Dry-run LLM schema for preview |
| `regenerate_batch` | LLM schema rebuild for flagged nodes |
| `regenerate_affected_routers` | Rewrite router conditions after upstream schema change |
| `regenerate_gap_resolution` | Rewrite generationDescription from update request |
| `run_update_regeneration_pipeline` | Legacy full regen for flagged nodes (`_requires_regeneration`) |
| `smart_diff_outputs` | `"destructive"` if any old output name missing |

Phase 1’s bidirectional blast radius **supersedes** the legacy forward-only blast for impact reporting; legacy blast still drives batch selection inside `run_update_regeneration_pipeline`.

---

## 9. Production integration map (`agent_new.py`)

### 9.1 Helpers (regeneration-specific)

| Helper | What it does |
|--------|----------------|
| `_load_current_codebase` | Reads Angular workspaces for all `ui:*` screens; keys = `{screen_id}/{relative_path}` |
| `_extract_global_inputs` | Collects `requiredGlobalInputs` across phases |
| `_apply_edit_plans_to_phase_graphs` | Merges proposed schemas into graphs; idempotency markers |
| `_trigger_angular_updates_for_ui_screens` | Adds affected `ui:*` caps to `gap_generations` |
| `_finalize_ui_screen_update` | Points node at new screen_id; refreshes declaredOutputs from IR |
| `_regenerate_downstream_ui_screen` | Cascades UI updates for impact-flagged downstream screens |
| `_reconcile_tool_inputs_from_upstream` | After gap resolve, bind tools to newly available upstream outputs |
| Force-include / generic-output filters | Keep declaredOutputs grounded in user-named fields |

### 9.2 HTTP endpoints

| Endpoint | Phase / behavior |
|----------|------------------|
| `POST .../plan/update` | Full flow; `preview_only` for Phase 1-only |
| `POST .../plan/update/impact-analysis` | Standalone Phase 1 (+ optional LLM schema dry-run) |
| `POST .../plan/update/edit-plans` | Standalone Phase 2 plan generation (no apply) |
| `POST .../plan/update/apply-edits` | Delegates to full `update_workflow_plan` |
| `POST .../plan/update/validate` | Standalone Phase 3 |
| `POST .../plan/update/pipeline` | Stub placeholder |
| `GET  .../plan/update/stream/{workflowId}` | SSE progress stub (simulated phases) |

### 9.3 Response signals

`/plan/update` returns:

- `pipelineUsed`: `"integrated"` | `"legacy_fallback"` | `"none"`
- `hadFallback`: boolean
- Also persists `agentic_process.last_update_pipeline`

Today, failure path tends to raise before a soft `"legacy_fallback"` completion; the field still exists for older dual-path behavior and telemetry.

---

## 10. Real UI surgery (where Phase 2 actually edits code)

**File:** `backend/services/angular_generator.py`  
Called from `update_angular_screen_async` in `agent_new.py`.

1. **`analyze_update_scope`**
   - LLM lists affected files + change types (`style|text|layout|component-add|component-remove|theme`).
2. **IR update** (screen intermediate representation / I/O contract).
3. **`regenerate_files_for_update`**
   - Regenerates **only** listed files in the existing workspace.
4. Persist new `screen_generations` version / possibly new `screen_id`.

This is the production realization of “minimal diffs, preserve untouched code” for Angular screens.

---

## 11. Algorithms & approaches cheat sheet

| Concern | Approach | Location |
|---------|----------|----------|
| Cycle detection | Tarjan SCC | `impact_analysis._detect_cycles` |
| Blast radius | BFS forward + conditional backward | `impact_analysis._calculate_blast_radius` |
| Severity | Rule-based schema/file diffs | `ImpactAnalyzer` + LLM file analysis |
| UI file selection | LLM scope analysis | `impact_analysis` + `angular_generator` |
| Tool schema regen | Batched LLM JSON schemas + gap descriptions | `regeneration.py` |
| Code apply (generic) | search/replace / insert / delete | `surgical_editing.DiffApplicator` |
| Code apply (UI) | Selective Angular file regen | `angular_generator` |
| Validation | Multi-category static checks | `validation_repair.WorkflowValidator` |
| Repair | Iterative auto-fix + revalidate | `RepairEngine` |
| Approval | Preview cache + frontend gate; backend auto-approve | `agent_new` + `integrated_pipeline` |
| Idempotency | `_applied_update_prompts` | `create_proposed_changes_from_node_update` / `_apply_edit_plans_to_phase_graphs` |
| Rollback safety | New screen_id per UI update | `update_angular_screen_async` + capabilityRef rewrite |

---

## 12. Design document vs current code (honest delta)

| Design (`regenerationDesign.txt`) | Current implementation |
|-----------------------------------|------------------------|
| Bidirectional blast + Tarjan | ✅ Implemented in Phase 1 |
| Severity SAFE / BREAKING / STRUCTURAL | ✅ Implemented (`NONE` also exists) |
| User approval before edits | ✅ Preview endpoint + frontend responsibility; backend auto-approves |
| Phase 2 minimal diffs | ⚠️ Marker plans in `surgical_editing`; real UI diffs in Angular generator |
| Phase 3 six checks + cycle | ✅ Categories present; backend-compat & execution weak/stubbed |
| Auto-repair loop max 3× | ✅ Engine supports it; production outer loop capped at 1 |
| Automatic fallback to legacy | ⚠️ Design says yes; `/plan/update` currently re-raises on integrated failure |
| Integration point `agent_new.py` | ✅ Primary |

---

## 13. Typical example walkthrough

**User:** Update Screen B: “add a field called `selected_operation`”.

1. **Preview**
   - Phase 1 finds Screen B direct; tools/screens that bind to B may be SAFE/BREAKING.
   - If retry edge Tool C → Screen B exists and both in a cycle, C/B appear in blast radius.
   - Report cached in `pending_update_preview`.

2. **Approve / apply**
   - Reuse cached impact (ops match).
   - Phase 2 emits UI marker edit plan.
   - Phase 3 validates graph/contracts (often passes lightly at this stage).
   - `_apply_edit_plans_to_phase_graphs` merges schemas.
   - `update_angular_screen_async` surgically regenerates only affected Angular files; new screen version.
   - Downstream tool marked and regenerated / reconciled to consume `selected_operation`.
   - Gap resolution + LangGraph compile + version bump.

---

## 14. File index (quick reference)

```text
docs/regeneration-flow/
  regenerationDesign.txt     # Product/design intent
  README.md                  # This implementation guide

backend/
  impact_analysis.py         # Phase 1
  surgical_editing.py        # Phase 2 plan/diff framework
  validation_repair.py       # Phase 3
  integrated_pipeline.py     # Orchestrator
  regeneration.py            # Legacy + shared graph utilities
  agent_new.py               # HTTP + post-pipeline apply/cascade
  services/angular_generator.py  # Real selective UI code edits
```

---

## 15. How to extend safely

1. **New edge kinds that form loops** → add to `CYCLE_EDGE_KINDS` / `BACKWARD_DEPENDENCY_KINDS` in `ImpactAnalyzer`.
2. **New node types** → register generator in `DiffGenerator._edit_generators` and a cascade handler in `/plan/update` success path.
3. **Stronger Phase 2 diffs for tools** → wire `_generate_tool_diff` into `_generate_tool_edits` carefully (watch timeouts; production currently avoids LLM in Phase 2 for tools).
4. **Stronger Phase 3 backend-compat** → connect HTTP URL scan to real route registry.
5. **True iterative heal in prod** → raise `max_iterations` only with idempotency guarantees (`_applied_update_prompts`) already in place.

---

## 16. Glossary

| Term | Meaning |
|------|---------|
| Blast radius | Set of nodes transitively affected by a change |
| Bidirectional | Includes forward downstream *and* cycle/backward participants |
| Proposed changes | Desired schema/prompt delta before apply |
| Edit plan | List of `CodeEdit`s (often markers) for a node |
| Gap resolution | Turn `GENERATE:*` / flagged caps into real tool/UI capabilities |
| Screen bump | New `screen_id`/version so restore can roll back UI independently |
| Preview cache | `pending_update_preview` on `agentic_process` |

---

*Last reviewed against the codebase modules listed in §1. If `regenerationDesign.txt` and this README diverge, treat this README as the source of truth for **how the system behaves now**, and the design file as the product intent.*
