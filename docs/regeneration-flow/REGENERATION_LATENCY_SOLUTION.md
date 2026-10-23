# Regeneration Latency — Solution Document

> **Goal:** Reduce wall-clock time for `/plan/update` (and the shared `update_angular_screen_async` stack used by `/plan/execute` gap resolution) from the current **15–20 min** typical range, **without compromising regeneration accuracy or rollback safety**.
>
> **Built on:** `README.md` (implementation guide), `REGENERATION_LATENCY_RESEARCH.md` (code-path analysis), `LATENCY_EVIDENCE_INTAKE.md` (measured evidence on `himanshu-dell`).
>
> **Codebase validation:** See **§8** for a review of whether each recommendation is a real issue, implementable in the current code, and sufficient to solve 15–20 min latency (includes caveats and a missing LLM-path item **R9**).
>
> **Confirmed context for this doc:**
> - Production/stage runs on the **same persistent-host topology** as the measured dev environment → caching and warm reuse are viable, not blocked by ephemeral containers.
> - The two "hard" code behaviors — new `screen_id` per update, and always running a **production** `ng build` — are **open to relaxing**.
> - Priority: **latency reduction**, but explicitly **not at the cost of regeneration accuracy**; this is a production system, so every fix below is annotated with its accuracy/safety impact.

---

## 1. Guiding principles (read before implementing anything below)

Because accuracy is a stated non-negotiable, every recommendation in this doc is filtered through three rules:

1. **Prefer "skip redundant work" over "skip validation work."** Caching, deduplication, and parallelization save time without changing *what* gets checked. Anything that reduces the surface area *validated* (thinner LLM context, weaker build config, fewer checks) is flagged **⚠️ accuracy-sensitive** and comes with a mitigation.
2. **Preserve ordering where correctness depends on it.** Nodes with real dependencies (a screen consuming another's new output) must still run in the correct order. Only *independent* work should be parallelized.
3. **Change defaults, not guarantees.** Rollback safety (versioned screens) and build integrity (a real compile check before serving) should still exist after these changes — just triggered more efficiently, not removed.

---

## 2. What the evidence actually says (recap)

On the measured host, for a 3-screen sequential gap-resolution phase (~20 min total):

| Component | Measured | Share of time |
|---|---|---|
| `npm install` (×3, sequential) | ~1.8–2.0 min each ≈ **5.6 min** | ~28% |
| `ng build` (×3, sequential) | ~0.7–1.5 min each ≈ **2.8 min** | ~14% |
| LLM/IR/scope/codegen work between builds | ~**11–14 min** | ~55–58% |
| Build-fix retries | No evidence they fired this run | negligible (this run) |

**Two things drive the total, roughly equally in weight:** (a) doing install+build **once per screen instead of once per install-worthy change**, and (b) large, partly-duplicated LLM calls happening **sequentially, once per screen**. Fixing only one of these caps your upside at ~50%.

---

## 3. Ranked recommendations

Each recommendation lists: what it fixes, the technique, expected impact (evidence-anchored where possible), effort, and accuracy/safety notes.

### R1 — Skip `npm install` when dependencies haven't changed
**Fixes:** ~2 min × N screens, unconditionally, on every single update — the largest *unconditional* tax in the whole pipeline.

**Technique:**
- Fingerprint `package.json` + lockfile (hash) for the base template used per project.
- Before running `npm install` in `_prepare_isolated_directory`, check whether a `node_modules` tree already exists (in a small pool of recent builds, or a dedicated "golden" install) with a matching fingerprint.
- If matched: **hardlink** `node_modules` into the new isolated build dir (`cp -al` / `rsync --link-dest`) instead of installing. Hardlinking is safe on the same NVMe filesystem — it's effectively free in I/O terms and each build dir still behaves as independent.
- If not matched (dependency actually changed): fall back to real `npm install`, then that install becomes the new "golden" reference for future hardlinks.
- Keep `~/.npm` cache in place regardless — it still helps the fallback path.

**Impact:** Since UI edits almost never touch `package.json`, this should eliminate `npm install` cost on the large majority of updates → **saves ~1.8–2 min per screen, most of the time.**

**Effort:** Medium (needs a small fingerprint+hardlink utility, and care around concurrent writers to the "golden" directory).

**Accuracy/safety:** ✅ Safe — hardlinked `node_modules` is byte-identical to a fresh install of the same lockfile. No behavior change to the app being built. Only regenerate when the fingerprint actually changes, so dependency-drift bugs aren't introduced.

---

### R2 — Reuse Angular's persistent build cache across versions
**Fixes:** Part of the `ng build` cost; also indirectly protects against the "cold" 0.7–1.5 min numbers becoming worse over time.

**Technique:**
- Angular CLI supports a persistent disk cache (`.angular/cache`). Currently each isolated build dir is fresh, so this cache never gets reused.
- Point `.angular/cache` at a **shared, persistent location** (outside the per-version isolated dir) so successive builds of the same project benefit from incremental compilation, the way the manual "warm rebench" in the evidence doc hit ~9.6s instead of ~1 min.

**Impact:** Evidence shows warm builds are **~6–9x faster** than cold ones on this host. This is close to "free" once wired up.

**Effort:** Low (config change + shared cache directory + cleanup policy so it doesn't grow unbounded).

**Accuracy/safety:** ✅ Safe — Angular's build cache doesn't change output correctness, only compilation speed. Still run a full type-check; the cache doesn't skip validation, it skips redundant recompilation of unchanged code.

---

### R3 — Deduplicate the "which files are affected" LLM analysis
**Fixes:** Phase 1's `_analyze_update_scope_with_llm` and Angular's `analyze_update_scope` are, per the research doc, "conceptually the same job," both dumping large codebase context (up to 80 files × 10k chars) into an LLM call — done **twice** per screen.

**Technique:**
- Thread Phase 1's scope-analysis result through `PipelineState` / the proposed_changes payload into `update_angular_screen_async`, and have it reuse that result instead of re-deriving from scratch — only re-run the LLM scope call if Phase 1 wasn't run, or if the codebase changed since Phase 1 ran (rare, since Phase 2 usually runs immediately after Phase 1 in the same request).
- Additionally, instead of a fixed 80-file/10k-char ceiling, scope the codebase sent to *either* call using the dependency graph Phase 1 already built (only send files actually reachable from the modified node), rather than a flat truncation.

**Impact:** Removes one full large-context LLM call per screen (evidence doc estimates ~20–120s for this call) — roughly halves the "which files change" cost, and reduces context size for the remaining call, which also speeds up generation.

**Effort:** Medium (requires passing state across a module boundary that currently doesn't share it, and re-validating that Angular's version still has enough context after graph-scoping instead of flat truncation).

**Accuracy/safety:** ⚠️ Accuracy-sensitive on the context-scoping half. Mitigation: scope by the **graph's actual reachability**, not an arbitrary smaller file count — this should *improve* precision (targeted files instead of "first 80 alphabetically or whatever the truncation order is") rather than reduce it. Add a fallback: if graph-scoped file count is unexpectedly small (e.g., <3 files) for a screen with real complexity, fall back to the wider scan once, and log it for review.

---

### R4 — Parallelize independent screen/gap regeneration (bounded)
**Fixes:** The single biggest measured cost — sequential per-screen full pipelines (LLM + install + build) × N screens, with zero overlap.

**Technique:**
- Use the existing (currently unused for this purpose) topological batching logic (`build_node_batch_plan` in `regeneration.py`) to identify which impacted nodes are **causally independent** of each other vs. which depend on one another's outputs.
- Process independent nodes in the same batch **concurrently** (`asyncio.gather` with a bounded semaphore), and only serialize nodes that genuinely depend on a prior node's new output.
- Bound concurrency conservatively given host headroom: evidence shows **8 CPUs / 14 GiB RAM with only ~250 MiB free / ~4 GiB available at sample time** — concurrent Angular builds are memory-hungry. Start with a semaphore of **2 concurrent screen builds**, measure memory pressure, and tune from there rather than assuming higher concurrency is free.

**Impact:** This is the highest-leverage single change. If 2 of 3 screens in the measured run had overlapped instead of running fully sequentially, the ~20 min phase could plausibly drop toward the low-to-mid teens even before any caching wins are applied — and stacks multiplicatively with R1/R2/R3.

**Effort:** Medium-high (topological batching exists but isn't wired into the gap-resolution loop; needs careful testing that dependent-node ordering is actually preserved, since this is where a parallelization bug could silently produce wrong bindings).

**Accuracy/safety:** ⚠️ Accuracy-sensitive — this is the one to test most carefully. Mitigation: only parallelize nodes the *existing* dependency graph says are independent (don't infer independence heuristically); add an explicit test/assertion that a node never starts before all of its upstream dependencies in the batch have completed; keep a feature flag to fall back to strictly sequential processing if anomalies show up in production.

---

### R5 — Narrow the cascade so SAFE additive changes skip full regeneration
**Fixes:** Not a raw speed technique — a **policy** change that reduces how much work R1–R4 even need to do.

**Technique:**
- The impact analyzer already distinguishes `SAFE` (e.g. `TOOL_INPUT_AVAILABLE_FROM_UPSTREAM`) from `BREAKING`/`STRUCTURAL`. Audit whether the existing `skipped=True` fast path (already present in code for binding-only changes) is being applied **consistently to downstream cascade nodes**, not just directly-modified ones.
- For a downstream `ui_screen` that merely *gains access* to a new upstream field but doesn't need to display it, apply a schema/binding merge only — no Angular regen, no npm/build at all.

**Impact:** Directly reduces the **N** in "N screens × full pipeline" for the common case of additive, non-breaking changes — which, per the design doc's own severity model, should be the majority of small edits.

**Effort:** Low-medium (mostly an audit + consistency fix on an already-existing fast path, rather than new mechanism).

**Accuracy/safety:** ✅ Safe by construction — this only skips regeneration for changes the system's own severity classifier already calls `SAFE`. If anything, this reduces unnecessary churn (fewer new `screen_id` versions minted for no-op UI changes).

---

### R6 — Cap the build-fix retry loop, but treat as low priority
**Fixes:** A theoretical worst case (`max_fix_iterations = 5`, each with a large LLM fix + rebuild) that the evidence doc could **not confirm actually fires** in current usage.

**Technique:**
- Lower `max_fix_iterations` from 5 to 2 for the update path specifically (leave generation-from-scratch paths as-is if they need more attempts).
- Shrink `use_full_context_for_fixes` from the 10-file/20k-char default to a targeted subset (the files the build error actually references) when fixing on the *update* path.

**Impact:** Bounds the worst-case tail; doesn't move the median, since evidence suggests retries aren't the common case today.

**Effort:** Low (config value + minor context-selection logic).

**Accuracy/safety:** ⚠️ Mild — capping retries at 2 instead of 5 means a genuinely hard-to-fix build error surfaces as a failure sooner instead of the system exhausting more attempts. This is an acceptable tradeoff for a bounded worst case, but pair it with a clear failure message so a human can intervene rather than the system silently giving up mid-fix.

---

### R7 — Relax "always full production build" for iterative work
**Fixes:** Running a fully optimized production Angular build (`--configuration production`) on every single edit, including intermediate/preview iterations.

**Technique:**
- Reserve the full production build for an explicit **publish/finalize** step.
- For intermediate updates (still being iterated on), use a lighter build configuration — still a real compile (so syntax/type errors are still caught), just without the heavier production optimizations (minification, tree-shaking passes, output hashing work) that don't matter until the screen is actually shipped.

**Impact:** Given warm builds already measured at ~10s and cold at ~1–1.5 min, a lighter intermediate build config compounds with R2's caching for the fastest possible inner loop.

**Effort:** Medium (needs a clear "draft vs. publish" distinction in the workflow, and confirming what checks the lighter build still performs vs. what only the production build catches).

**Accuracy/safety:** ⚠️ Accuracy-sensitive — this is the one place where "faster" could plausibly mean "catches fewer real bugs" if done carelessly. Mitigation: don't drop to `ng serve`/dev-server-only mode — use a real `ng build` with a **non-production configuration that still runs full type-checking**, just skips optimization passes. Run the full production build (with all optimizations) automatically at publish time regardless of how many draft builds preceded it, so nothing ships without the real check.

---

### R8 — Relax "new `screen_id` per update" for intermediate iterations
**Fixes:** Filesystem duplication tax — full `copytree` to a new versioned workspace, then another full `copytree` into an isolated build dir, then a `dist` copy to Liberty FS, for every single edit including micro-edits.

**Technique:**
- Keep an **in-place mutable "draft" workspace** for a user's active iteration session, rather than minting a brand-new versioned `screen_id` (and copying the full tree) on every keystroke-level change.
- Only create a new immutable, rollback-eligible version at meaningful checkpoints — e.g., when the user explicitly publishes/saves, or when a session ends.
- Use hardlinks (not full copies) even for the versions you do keep, per R1's technique, so retained versions don't each cost a full 495MB `node_modules` copy — this directly addresses the measured **60GB across 122 builds**.

**Impact:** Removes repeated full-tree copies for iterative work, and shrinks the storage/IO footprint of retention.

**Effort:** Medium-high (touches the rollback/versioning model, needs product agreement on what "checkpoint" means in the UI).

**Accuracy/safety:** ⚠️ Accuracy-sensitive on rollback granularity — mitigation: don't remove rollback entirely, just make its granularity match actual save points instead of every internal LLM pass. Keep a lightweight diff/history log during the draft phase so no work is silently lost even without a full versioned copy per step.

---

## 4. Rollout plan (sequenced by risk and dependency)

Because R4/R7/R8 touch correctness-sensitive paths, this is sequenced so cheap, safe wins land first and validate the instrumentation, before the riskier structural changes.

### Phase 0 — Instrumentation first (days, near-zero risk)
Before changing behavior, add timers so every subsequent change can be measured against real `/plan/update` numbers (today's hard numbers are `/plan/execute` proxies):

```text
regen.angular.npm_install
regen.angular.ng_build
regen.angular.build_attempt{n, success|fail}
regen.angular.llm.{classify,ir,scope,files,fix}
regen.gap_node{node_id, kind}
regen.total
```

Also pull 2–3 real `/plan/update` traces from `llm_token_count` (blocked in the evidence-gathering session due to DB access — worth resolving access before Phase 1, since it's cheap and de-risks every ranking above).

### Phase 1 — Low-risk, high-impact wins (1–2 weeks)
- **R1** — skip `npm install` via fingerprint + hardlink.
- **R2** — shared persistent Angular build cache.
- **R6** — cap build-fix retries to 2, shrink fix context.
- **R5** — audit/enforce the existing `skipped=True` fast path across downstream cascades.

These alone should meaningfully cut the *unconditional* tax on every update, with essentially no accuracy risk, and give you a clean before/after measurement using Phase 0's instrumentation.

### Phase 2 — Medium-risk, high-impact (2–4 weeks)
- **R3** — dedupe the scope-analysis LLM call, graph-scope the context.
- **R4** — bounded parallelization of independent screens/gaps via existing topological batching.

Ship behind a feature flag; validate on a sample of real updates that dependent-node ordering and merged outputs are unchanged before defaulting it on.

### Phase 3 — Structural, needs product sign-off (4+ weeks)
- **R7** — draft vs. publish build configuration split.
- **R8** — draft vs. checkpoint versioning model.

These change user-facing behavior around when a "real" build/version happens, so they need explicit product agreement on what a checkpoint/publish action looks like in the UI — not just a backend change.

---

## 5. Expected combined impact (estimate, to be confirmed by Phase 0 instrumentation)

| Stage | Baseline (measured, 3-screen case) | After Phase 1 | After Phase 2 | After Phase 3 |
|---|---|---|---|---|
| Total | ~20 min | ~13–15 min | ~7–10 min | ~4–7 min |

Treat these as directional, not commitments — they should be replaced with real numbers once Phase 0 instrumentation is in and Phase 1 ships. The single most load-bearing assumption behind the Phase 2/3 numbers is that most impacted nodes in a typical cascade are actually independent enough to parallelize (R4) and that most updates don't touch `package.json` (R1) — both worth confirming against real traffic before locking in a target.

**Revised expectations after codebase review:** see **§8.5**. In short, Phase 1 → ~12–15 min on a 3-screen case is plausible; under ~5 min for a typical update is **unlikely** without an LLM-path cut (**R9** in §8.4). Also treat R5 as a **policy change**, not merely enforcing an existing fast path (§8.3).

---

## 6. What NOT to do

- **Don't drop the production build entirely for anything that ships.** Lighter intermediate builds (R7) are fine; skipping real compilation before something is user-visible is not.
- **Don't naively parallelize the gap-resolution loop without the dependency graph.** Parallelizing by "just run all pending nodes at once" risks a screen consuming a binding before its upstream node has actually produced it — silent correctness bugs are worse than slow builds.
- **Don't remove rollback capability to save disk.** Hardlinking (R1/R8) gets you the storage savings without losing the ability to roll back — don't trade one for the other by deleting version history outright.
- **Don't blindly shrink LLM context to save tokens/time.** If context reduction isn't grounded in the dependency graph (R3), you risk the model missing a genuinely affected file, which is exactly the kind of accuracy regression this doc is trying to avoid while still cutting latency.

---

## 7. Open items still worth resolving

Carried over from the evidence intake doc, since they still affect how aggressively to tune concurrency and caching:

- Real `/plan/update` timing traces (Phase 0 should produce these directly).
- Actual production request volume/concurrency — determines the safe semaphore bound for R4 (2 was a conservative starting guess based on this host's memory headroom, not a measured production ceiling).
- Build-fix retry rate over a larger sample — confirms whether R6's cap of 2 is generous or still too tight.

---

## 8. Codebase validation (review against current implementation)

> Added after reviewing each recommendation against `agent_new.py`, `angular_generator.py`, `impact_analysis.py`, and the measured evidence. Use this section to decide what to implement first and what not to over-promise.

### 8.1 Verdict summary

Most diagnosed issues are **real**, and several fixes are **implementable** in the current codebase. The plan will **not fully solve** typical 15–20 min updates by itself: it underweights the LLM work that was ~55% of the measured phase, and a few recommendations are overstated or harder than they sound.

| Rec | Real issue? | Implementable now? | Needle movement |
|-----|-------------|--------------------|-----------------|
| **R1** skip npm via fingerprint/hardlink | **Yes** — every isolated dir does fresh `npm install` (~2 min × N) | **Yes** — `_prepare_isolated_directory` / `_install_dependencies` | **High** — best Phase 1 win |
| **R2** shared `.angular/cache` | **Yes** — isolated dir is `rmtree` + fresh copy every time; base `angular.json` has no shared cache path | **Yes** — low effort | **High** — cold ~1 min → warm ~10s class |
| **R3** dedupe scope LLM | **Partial** — Phase 1 + `analyze_update_scope` both run on `/plan/update` | **Yes, with plumbing** — impact stores `file_change`s but does **not** pass `affected_files` into `update_angular_screen_async` | **Modest** — removes ~one large call (~20–120s), not bulk LLM time |
| **R4** parallel independent screens | **Yes** — gap loop is strictly `for node in pending` | **Yes, carefully** — gap code updates downstream bindings from upstream **before** resolve; blind `gather` breaks that | **High if N≥2 independent screens**; memory tight on 14 GiB |
| **R5** SAFE cascade skip | **Yes problem; oversold as “existing fast path”** | **Partial** — `skipped=True` exists, but downstream path **intentionally** queues full Angular via `gap_generations` | **Only if policy changes** — not just an audit |
| **R6** cap build-fix retries | Real as **tail risk**, not median | **Yes** — trivial | **Low** for typical runs |
| **R7** draft vs prod build | Real optional win | **Medium** — base project only has `production` build config; need new config + draft/publish product path | Helps inner loop; does not fix LLM |
| **R8** draft `screen_id` / less copytree | Real versioning cost; **FS latency overstated** | **Hard** — versioning is wired for `/plan/restore` | Source `copytree` is small (`generated-apps` ~15 MB); **495 MB** tax is `node_modules` → mostly **R1**, not R8 |

### 8.2 What the recommendations get right

1. **R1 / R2 match code and measurements** — unconditional ~3 min/screen install+build is real; caching is viable on this persistent host.
2. **Sequential × N screens is a real amplifier** — measured 3 gaps → ~20 min phase.
3. **Guiding principles** (don’t drop compile for shipped UI; don’t parallelize without the graph) are correct.
4. **Phase 0 instrumentation first** is right — rankings still use `/plan/execute` proxies.

### 8.3 Caveats and corrections (per recommendation)

#### R1 — Hardlink / golden `node_modules`

- Hit rate should be high: UI edits almost never change `package.json`.
- **Not called out in §3:** concurrent builds mutating a shared hardlinked `node_modules` can corrupt installs; some packages write into `node_modules` after install.
- Prefer a **read-only golden tree** + exclusive lock, or copy-on-write / per-build overlay — not unrestricted shared mutable hardlinks.
- Implementation point: workspace trees usually have **no** `node_modules`; install happens only after `copytree` into the isolated dir — golden hardlink belongs **there**, after fingerprint match.

#### R2 — Shared Angular cache

- Angular 17 can use persistent disk cache under `.angular/cache`; wiping the isolated dir today always cold-starts it.
- Wire a shared path (e.g. `cli.cache` / env) **outside** the per-version isolated dir.
- Best hit rate for iterative updates of the **same** screen; weaker across unrelated projects/screens.
- Still run a real compile — cache skips redundant work, not validation.

#### R3 — Dedupe scope analysis

- Real on **`/plan/update`**: impact LLM (`_analyze_update_scope_with_llm`) and Angular `analyze_update_scope` both dump large codebase context.
- Impact converts analysis into `file_change` entries on `NodeImpact.changes` but **does not** feed `affected_files` into `update_angular_screen_async` today — needs explicit plumbing through `PipelineState` / payload.
- Prompts are similar, not identical (impact also carries backend-function context).
- **Does not help** `/plan/execute` first-time gap generation (no Phase 1).
- **Does not** remove classify / IR update / `regenerate_files_for_update` / backend / tool reconcile — those dominate the multi-minute gaps between builds.

#### R4 — Parallel gap / screen work

- Real: `_resolve_gap_generations` processes `pending` **sequentially**.
- **`build_node_batch_plan` is not a drop-in:** it exists for schema batching, not gap orchestration.
- Gap resolution currently refreshes downstream `inputBindings` from **already-resolved** upstream `declaredOutputs` before resolving the downstream node. Parallelism must be **dependency-aware**; “run all pending at once” can produce wrong bindings.
- Host sample: 8 CPUs / 14 GiB with low free memory — start semaphore at **2**, measure pressure (`node_modules` alone ~495 MB per build).

#### R5 — SAFE cascade / `skipped=True`

- **Oversold as “audit existing fast path.”** Downstream `_regenerate_downstream_ui_screen` updates bindings then **defers to gap resolution → `update_angular_screen_async`**. Comments explicitly say Angular will run.
- Impact `SAFE` still sets `requires_regeneration=True`, so cascade still pulls nodes into regen.
- `skipped=True` only fires when `analyze_update_scope` returns empty `affected_files`; binding-update prompts often still get a non-empty file list from the LLM.
- Treat R5 as a **real policy change**: for binding-only / SAFE additive downstream UI, skip Angular entirely (schema/binding merge only). That is correct and valuable — but it is **new behavior**, not a consistency fix.

#### R6 — Cap retries

- Leave as low priority; no measured evidence retries dominate median latency.
- Still fine as a worst-case bound for the update path (`max_fix_iterations` 5 → 2) plus narrower fix context.

#### R7 — Draft vs production build

- Base `angular.json` currently exposes essentially a **production** build configuration for the generator path.
- Needs a new non-prod (but still type-checking) configuration **and** a product draft/publish distinction.
- Do not ship user-visible screens without a full production build at publish time.

#### R8 — Draft `screen_id` / fewer copytrees

- Versioning (new `screen_id` / `screen_generations` row) is intentional for `/plan/restore` and helpers like `_bump_screen_version_for_workflow_update`.
- On this host, **workspace `copytree` is not the multi-minute killer** (`generated-apps` ~15 MB). The large cost is materializing **`node_modules` in isolated builds** — addressed primarily by **R1**.
- Defer R8 until product agrees what a checkpoint/publish means; don’t frame it as the main latency lever.

### 8.4 Critical gap in this solution doc: bulk LLM work

Evidence for the 3-screen phase: ~11–14 min was LLM/IR/scope/codegen between builds. As written, Phase 1–2 mainly attack install/build + one duplicate scope call + parallelism.

**Missing recommendation (proposed R9):** shrink or skip redundant IR / file-regen work for tiny updates, e.g.:

- Reuse IR / contract from Phase 1 when still valid.
- Skip `classify_update_category` when the update path already knows the category.
- Skip Angular entirely when bindings-only and HTML/TS do not need to change (pairs with hardened R5).
- Avoid re-sending full codebase dumps when a prior scope result already listed exact paths.

Without something in this class, **under ~5 min for a typical update is unlikely** even if R1–R4 land.

### 8.5 Realistic impact expectations (revised)

| Goal | With this plan as written in §§3–5 | After adding R9-class LLM cuts |
|------|--------------------------------------|--------------------------------|
| 3-screen ~20 min → ~12–15 min | **Plausible** after Phase 1 (R1+R2±R5) | Same or better |
| → ~7–10 min | **Possible** if R4 applies + R5 skips some screens + R3 | More likely |
| → ~4–7 min / under 5 for typical update | **Unlikely** (single-screen still LLM-bound) | Required |
| Accuracy preserved | **Yes for R1/R2/R6**; careful design for R4/R5/R7/R8 | Same |

Single-screen note: R1+R2 alone save ~2.5–3.5 min. If the rest is IR/file LLM, you remain multi-minute without R5/R9-style skips.

### 8.6 Revised implementation priority

1. **Phase 0** — instrumentation + real `/plan/update` traces (unchanged).
2. **Ship first:** **R1 → R2 → harden R5 as real policy** (not “audit only”).
3. **Phase 2:** **R3**, then **R4** behind a feature flag with dependency-aware batching (do not naively reuse `build_node_batch_plan` as the orchestrator).
4. **Add R9** (LLM path) before promising sub‑5‑minute targets.
5. **Defer R7 / R8** until product sign-off; treat R6 as optional tail bound.
6. For R1 hardlinks: design for concurrent safety (golden read-only + lock/overlay), not mutable shared `node_modules`.
