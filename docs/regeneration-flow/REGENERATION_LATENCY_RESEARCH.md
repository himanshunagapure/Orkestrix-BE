# Regeneration Latency Research

> **Goal:** Explain why a small change on a 5–7 node workflow can take **15–20 minutes**, where wall-clock time goes, and which stages dominate.  
> **Scope:** `/aiqod-agent/agent/agentic/plan/update` regeneration path (preview + apply), including integrated pipeline, Angular update, gap resolution, tool regen, and LangGraph postgen.  
> **Companion docs:** [`README.md`](./README.md) (how regeneration works), [`../WORKFLOW_BUILDER_LLM_COST_DEEP_DIVE.md`](../WORKFLOW_BUILDER_LLM_COST_DEEP_DIVE.md) (token cost, overlapping concerns).

---

## Executive verdict

For small UI/schema updates, time is **not** mostly spent in Phase 1 graph analysis or Phase 3 validation. Those are usually seconds to low minutes.

The wall clock is dominated by:

1. **Per-screen Angular update pipeline** — especially **`npm install` + production `ng build`**, often repeated on build-fix retries.
2. **Multiple large LLM calls per updated UI screen** — classify → IR update → scope analysis → file regen → optional fix loops.
3. **Sequential cascading** — downstream UI/tools/gap nodes are processed **one after another**, each potentially re-running the heavy Angular or tool path.
4. **Filesystem duplication** — every screen version copies the workspace and then copies again into an isolated build dir before install/build.

A realistic small update that touches **1 UI screen** can still land in the **8–15+ minute** range if install+build are cold and one build fix retry happens. Touching **2 screens** or cascading a tool nearly doubles that.

```
Typical 15–20 min budget (small change, 1–2 UI screens affected)
───────────────────────────────────────────────────────────────
Angular npm install + ng build (+ retries)     ████████████  40–70%
UI LLM chain (IR / scope / regen / fixes)      ████████      15–30%
Downstream tool regen + reconcile              ███           5–15%
Impact / schema dry-run / descriptions         ██            3–10%
Gap / postgen / Mongo / FS copy                ██            3–10%
Integrated Phase 2/3 markers + validate        █             <5%
```

---

## End-to-end timeline (what actually runs)

```
POST /plan/update  (or preview_only=true first)
│
├─ A. Load Process_Configs + credentials
├─ B. Normalize ops (DELETE/UPDATE/INSERT)
│
├─ [preview_only] ─────────────────────────────────────────────
│    B1. get_proposed_node_schemas          (1 LLM)
│    B2. _load_current_codebase             (disk I/O, all ui:* screens)
│    B3. run_impact_analysis LLM path       (1 LLM per modified ui_screen)
│    B4. cache pending_update_preview
│    RETURN  (no Angular build)
│
├─ C. Integrated pipeline (auto-approve)
│    C1. Phase 1 impact (or reuse cache)    (+ LLM if not cached)
│    C2. Phase 2 edit plans                 (mostly markers, little time)
│    C3. Phase 3 validate/repair            (static; usually fast)
│
├─ D. Apply success path in agent_new.py   ★ MAJOR WALL CLOCK ★
│    D1. _apply_edit_plans_to_phase_graphs
│    D2. For EACH directly updated ui_screen (sequential):
│         update_angular_screen_async(...)  ★★★
│         regenerate_gap_resolution(...)    (1 LLM)
│    D3. Downstream cascade from impact report:
│         ui_screen → another Angular-ish path / config update
│         tool → run_update_regeneration_pipeline (multi LLM)
│    D4. Queue remaining screens into gap_generations
│
├─ E. Gap resolution (_resolve_gap_generations)  ★ can re-enter Angular ★
│    For each pending GENERATE:*/flagged node sequentially:
│      tool → tool LLM codegen
│      ui_screen → update_angular_screen_async again (unless skipped)
│
├─ F. Post-gap revalidation (up to 2 rounds, LLM possible)
├─ G. Tool reconciliation (_reconcile_tool_inputs_from_upstream)
│      may force another tool LLM regen
├─ H. LangGraph code emit + postgen validate/repair (up to 2 rounds)
└─ I. Persist Process_Configs / artifacts
```

**Key structural fact:** even when Phase 2 is “surgical,” production still runs the **full screen update stack** (`update_angular_screen_async` → IR → optional backend → selective file regen → **fresh npm install + ng build**).

---

## Ranked bottlenecks

### P0 — Angular `npm install` on every update (often 2–8+ min)

**Where:** `angular_generator._install_dependencies` ← called from `_update_app_sync` and full `_generate_app_sync`.

**What happens:**

1. Create new versioned workspace.
2. `shutil.copytree(old_workspace → new_workspace)`.
3. `_prepare_isolated_directory` does **another full `copytree`** into `isolated-angular-apps/...`.
4. Always runs:

```text
npm install
# on ERESOLVE → npm install --legacy-peer-deps
```

Workspace base copy **excludes** `node_modules` when seeding from `angular-base-project`, so isolated builds typically install from scratch (or nearly). Command timeout budget is enormous (`_COMMAND_TIMEOUT_SECONDS = 7200`).

**Why it hurts small changes:** a one-field UI tweak still pays a **cold dependency install** tax every version.

**Amplifiers:**

- Multiple screens updated/cascaded → install × N (sequential).
- Build recovery does **not** remove install, but rebuilds after LLM fixes (install usually once per update attempt; rebuilds repeat).

---

### P0 — Production `ng build` (+ recovery loop) (often 1–6+ min, worse with retries)

**Where:** `angular_generator._build_angular_app` + `_build_with_recovery`.

**Command:**

```text
npx ng build --configuration production --output-hashing=none --base-href ./
```

**Recovery loop:**

1. Optional static analysis (`tsc`-style) + LLM fix attempt.
2. Build attempt 1..`max_fix_iterations + 1` with **`max_fix_iterations = 5`**.
3. Each failure → `_run_fix_iteration` (large LLM prompt; `use_full_context_for_fixes = True` can load up to **10 files × 20k chars**) → merge → **rebuild**.

So one flaky compile can become:

```text
(install) + build_fail + LLM_fix + build_fail + LLM_fix + build_ok
```

That alone can explain **10–20 minutes** for a “tiny” edit.

---

### P0 — Full `update_angular_screen_async` stack per UI screen (multi-minute even before npm)

**Where:** `agent_new.update_angular_screen_async`.

Per screen, roughly:

| Step | Work | Typical time drivers |
|------|------|----------------------|
| Load screen state / chat window | Mongo | small |
| `classify_update_category` | 1 LLM | ~5–30s |
| `update_screen_ir_async` | 1 LLM (full/partial IR JSON) | ~15–90s |
| Backend diff + maybe `generate_backend` | 0..N LLM + compile/fix loops | 0–several minutes if DS/models change |
| `analyze_update_scope` | 1 LLM with up to **80 files × 10k chars** | ~20–120s |
| `regenerate_files_for_update` | 1 LLM rewriting affected files | ~20–180s |
| Workspace copy + isolated copy | disk | tens of seconds on large trees |
| npm install + ng build (+ fixes) | shell | **dominant** |
| Upload to Liberty FS + persist screen doc | disk/network/Mongo | small–medium |

**Called from regeneration in multiple places:**

- Direct UPDATE path after integrated pipeline success.
- Gap resolution for `ui:*` / `GENERATE` screens (`_resolve_one_gap_node`).
- Downstream UI cascade helpers (config/bindings; Angular may still be queued via `gap_generations`).

If impact analysis marks **2 UI screens**, you can pay this stack **twice**, sequentially.

---

### P1 — Duplicate / overlapping LLM analysis for the same change

Same user prompt is analyzed more than once across layers:

| Call | Purpose | Context size |
|------|---------|--------------|
| Preview `get_proposed_node_schemas` | Proposed I/O schema dry-run | medium |
| Impact `analyze_impact_llm` / `_analyze_update_scope_with_llm` | Blast + affected files | **huge** (codebase dump) |
| `classify_update_category` | ui vs behavior | small |
| `update_screen_ir` | IR mutation | large (current IR JSON) |
| `analyze_update_scope` (again inside Angular update) | Affected files | **huge** again |
| `regenerate_files_for_update` | File content rewrite | medium–large |
| `regenerate_gap_resolution` | Rewrite generationDescription | medium |
| Downstream tool `regenerate_batch` / gap tool / reconcile | Schema + Python tool | medium–large |
| Build fix iterations | Compiler repair | can be **very large** |

Impact-scope and Angular-scope are conceptually the same job (which files change) and both send truncated workspaces into the model.

---

### P1 — Sequential cascades multiply cost linearly with “affected” nodes

Impact analysis intentionally pulls downstream nodes into the blast radius (including cycle participants). Production then regenerates them:

- Downstream **ui_screen** → binding/config updates + potentially Angular/gap path.
- Downstream **tool** → `run_update_regeneration_pipeline` (batch schema LLM + router LLM + gap description LLM) and/or `_reconcile_tool_inputs_from_upstream` (another tool codegen).

Gap resolution itself loops **sequentially**:

```text
for round in max_rounds:
  for node in pending:
    await _resolve_one_gap_node(...)   # no parallelization
```

So a 5–7 node workflow where impact marks 3 nodes can feel like 3 nearly-independent generations, not one cheap surgical edit.

---

### P1 — Filesystem copy tax (often underestimated)

On each selective update:

1. Copy previous generated workspace → new versioned workspace (`copytree`).
2. Copy that workspace → isolated build directory (`copytree` **without** ignoring heavy dirs if present).
3. After build, copy `dist` → Liberty FS versioned deploy dir.
4. Retention cleanup of old versions.

On spinning disks / large trees / antivirus, this is not free—especially when repeated per screen and per failed recovery rebuild mirrors files again.

---

### P2 — Integrated Phase 1/2/3 themselves (usually not the 15–20 min)

| Stage | Cost profile | Notes |
|-------|--------------|-------|
| Phase 1 static graph | ms–seconds | Tarjan + BFS |
| Phase 1 LLM | 1 call / ui_screen | Can be large prompt; minutes if slow model |
| Phase 2 `surgical_editing` | seconds | Emits markers; **does not** do npm/build |
| Phase 3 validation | seconds | Mostly static; syntax compile of provided codebase dict (often incomplete for Angular) |
| Repair loop | low in practice | Outer `/plan/update` caps integrated iterations at **1** |

**Implication:** optimizing only `impact_analysis.py` / `validation_repair.py` will not fix 15–20 minute updates.

---

### P2 — Tool regeneration & reconciliation

When tools are flagged:

- Legacy `run_update_regeneration_pipeline`: schema batch LLM(s), router rewrite LLM, gap-description LLM per flagged node.
- `_reconcile_tool_inputs_from_upstream`: may regenerate tool `function_code` again if new upstream outputs appear or orphan cap refs are detected.

Tools are cheaper than Angular **unless** they also trigger UI gap updates, but they still add serial minutes.

---

### P2 — LangGraph postgen validate/repair

After graphs are updated:

- Deterministic code emit (`_generate_langgraph_code_for_phases`) — usually fast.
- Persist `.py` artifact.
- Postgen validation/repair up to **2 rounds** with LLM fixes if compile/runtime checks fail.

Usually secondary compared to Angular, but can add 1–3 minutes on failure.

---

### P3 — Smaller but real costs

| Item | Detail |
|------|--------|
| `_load_current_codebase` | Reads all `ui:*` workspaces into memory for impact LLM |
| Mongo screen/doc reads | Multiple per screen (`get_screen_state`, `get_latest_screen_doc`, credentials) |
| Preview then apply | If preview was not reused (ops signature mismatch), Phase 1 LLM + schema dry-run run again |
| Billing / token tracking | Small overhead per LLM call |
| FE↔BE contract audit | Runs in full generate path before npm; can add LLM patch loops (`_MAX_FE_BE_AUDIT_ATTEMPTS = 2`) |
| Backend generator T6 repair | Programmatic + optional regenerate loops when dataSources change |

---

## Worked example: “add one more field” on Screen A (5–7 node workflow)

Assume Screen A → Tool B → Screen C.

### Best case (preview reused, only Screen A files change, build succeeds first try)

| Stage | Est. time |
|-------|-----------|
| Integrated Phase 1–3 (cached impact) | 10–40s |
| classify + IR update + scope + file regen | 1–3 min |
| workspace/isolated copies | 10–40s |
| npm install | 2–6 min |
| ng build (prod) | 1–3 min |
| gap description + LangGraph persist | 20–60s |
| **Total** | **~6–14 min** |

### Common bad case (impact marks A + B; build fails once; Tool reconcile regenerates)

| Stage | Est. time |
|-------|-----------|
| Impact LLM + schema dry-run (if not cached) | 1–3 min |
| Screen A full Angular update + 1 build retry | 8–15 min |
| Tool B schema + codegen / reconcile | 1–4 min |
| Screen C cascade/gap (bindings; maybe Angular) | 0–10+ min |
| Postgen | 0–2 min |
| **Total** | **~12–25+ min** |

This matches the reported **15–20 minutes** for “small” changes.

---

## Why “surgical” still feels like full regen

Design intent: minimal diffs, preserve untouched code.

Production reality for UI:

1. Phase 2 only **queues** an Angular update marker.
2. `update_angular_screen_async` still:
   - regenerates IR via LLM,
   - optionally regenerates backend functions,
   - asks LLM which files to rewrite,
   - **creates a new versioned workspace**,
   - **reinstalls npm deps**,
   - **runs a full production Angular build**,
   - deploys a new Liberty FS folder / screen_id.

So the *source edit* may be surgical, but the *delivery pipeline* is closer to a **full screen rebuild**.

---

## Inventory of time sinks by file

| Location | Sink type | Severity |
|----------|-----------|----------|
| `services/angular_generator.py` `_install_dependencies` | shell npm | **P0** |
| `services/angular_generator.py` `_build_angular_app` / `_build_with_recovery` | shell + LLM retries | **P0** |
| `services/angular_generator.py` `_prepare_isolated_directory` + workspace `copytree` | disk | **P1** |
| `services/angular_generator.py` `analyze_update_scope` / `regenerate_files_for_update` / `_run_fix_iteration` | large LLM | **P0/P1** |
| `agent_new.py` `update_angular_screen_async` | orchestration of above × screens | **P0** |
| `agent_new.py` `/plan/update` direct UI loop + downstream cascade | sequential multiply | **P0/P1** |
| `agent_new.py` `_resolve_gap_generations` | sequential UI/tool regen | **P1** |
| `agent_new.py` `_reconcile_tool_inputs_from_upstream` | extra tool LLM | **P1** |
| `impact_analysis.py` LLM scope with codebase dump | large LLM | **P1** |
| `regeneration.py` `get_proposed_node_schemas` / `run_update_regeneration_pipeline` | multi LLM | **P1/P2** |
| `services/ai_processor.py` `update_screen_ir` | LLM | **P1** |
| `services/backend_generator.py` `generate_backend` + T6 | LLM + compile | **P1** (when DS changes) |
| `integrated_pipeline.py` Phase 2/3 | mostly light | **P3** |
| LangGraph postgen repair | LLM occasional | **P2** |

Models commonly involved on regen path:

- `REGENERATION_MODEL = gemini-3.6-flash` (`agent_new.py`) for tracked pipeline LLM.
- `gemini-3-flash-preview` for IR update / some preview helpers.
- Angular codegen/fix models inside `AngularAppGenerator` (historically heavier / flash-lite mix; see cost deep-dive).

---

## How to measure on a live run (recommended)

There is no single end-to-end timer spanning all stages today. Practical instrumentation:

1. **Backend logs** — search chronological markers:
   - `[plan/update] Starting integrated pipeline`
   - `Starting selective Angular update`
   - `Installing dependencies...`
   - `Building Angular application...`
   - `Angular build attempt N/`
   - `[Gap resolution]`
   - `[Tool reconciliation]`
2. **`llm_token_count` collection** — group by `operation` / `sub_operation` / `started_at`–`finished_at` for the `workflowId` / `project_id`.
3. **Shell timing** — wrap or log duration around `_run_command` for `npm install` and `ng build`.
4. **Disk** — time `copytree` of workspace and isolated dirs; check whether `node_modules` is being recopied.

Suggested stage timers to add (if instrumenting):

```text
regen.impact_llm
regen.angular.classify
regen.angular.ir_update
regen.angular.scope
regen.angular.file_regen
regen.angular.copy_workspace
regen.angular.copy_isolated
regen.angular.npm_install
regen.angular.ng_build
regen.angular.fix_iteration
regen.gap_node
regen.tool_reconcile
regen.postgen
```

---

## Highest-ROI speed levers (research conclusions only)

Not implementing here—ranked by expected wall-clock impact for small updates:

1. **Reuse `node_modules` across versions** (symlink/cache/npm ci from warm cache; skip install when `package-lock` unchanged).
2. **Skip or defer production build** for tiny surgical edits when possible (or use faster config / incremental build / shared isolated env).
3. **Avoid second scope LLM** — reuse Phase 1 / preview analysis inside `update_angular_screen_async` instead of re-sending 80-file prompts.
4. **Short-circuit no-op Angular path** more aggressively when only bindings/contracts change (`skipped=True` already exists; ensure cascades use it).
5. **Cap build recovery** for update path (e.g. 1–2 fixes, not 5) and shrink fix context (`use_full_context_for_fixes=False`).
6. **Parallelize independent gap/UI work** carefully (today strictly serial).
7. **Narrow impact cascade** so SAFE additive changes don’t force full downstream Angular rebuilds.
8. **Don’t re-copy entire trees** into isolated dirs; mutate in place or hardlink.

---

## What is *not* usually the main cost

- Tarjan cycle detection / blast radius BFS.
- Phase 2 marker generation in `surgical_editing.py`.
- Phase 3 static validation (unless feeding huge Python codebases into syntax checks).
- Pure LangGraph template emission when postgen passes first time.
- Node count alone (5–7 nodes) — **affected UI screens and whether each triggers npm+build** matter far more than total node count.

---

## Bottom line

**15–20 minutes for a small change is expected with the current delivery path**, because regeneration still performs a **versioned Angular rebuild pipeline** (install + production build + multi-LLM prep), often **more than once** when impact/gap/cascade touch additional screens or tools.

Treat Phase 1–3 as the *decision* layer (relatively cheap) and `update_angular_screen_async` + npm/ng + sequential cascades as the *execution* layer (where nearly all time lives).

---

## Related code map

```text
backend/agent_new.py
  REGENERATION_MODEL
  update_workflow_plan (/plan/update)
  update_angular_screen_async
  _resolve_gap_generations / _resolve_one_gap_node
  _reconcile_tool_inputs_from_upstream

backend/integrated_pipeline.py          # Phase 1–3 orchestration
backend/impact_analysis.py              # blast + LLM scope
backend/surgical_editing.py             # edit markers (not npm)
backend/validation_repair.py            # cheap relative to builds
backend/regeneration.py                 # schema/tool legacy regen

backend/services/angular_generator.py   # npm/ng/copy/LLM fix loops ★
backend/services/ai_processor.py        # IR update LLM
backend/services/backend_generator.py   # backend function regen
```
