# Regeneration Latency — Evidence Intake (measured + open questions)

> **Purpose:** Answer the pre-solution questionnaire with **measured evidence where available**, and explicitly flag **unknowns / assumptions** so a fix plan is not built on guesswork.  
> **Date gathered:** 2026-09-03 (local machine `himanshu-dell`)  
> **Companions:** [`REGENERATION_LATENCY_RESEARCH.md`](./REGENERATION_LATENCY_RESEARCH.md) (code-path analysis — now partly superseded by measurements below), [`README.md`](./README.md)

---

## Status legend

| Tag | Meaning |
|-----|---------|
| **MEASURED** | Observed on this host from logs / filesystem / timed command |
| **INFERRED** | Strongly supported by timestamps/code, but not a direct timer |
| **UNKNOWN** | Could not determine in this session — needs your input or better instrumentation |
| **CODE-FACT** | True from code/config regardless of runtime |

---

## Questionnaire coverage matrix

Maps every ask from the pre-solution questionnaire → status in this doc.

| # | Original ask | Status | Where |
|---|--------------|--------|-------|
| 1a | Actual timings (npm install, ng build) | **Answered (MEASURED)** — proxy from `/plan/execute` gaps, not `/plan/update` | §1.1–1.2 |
| 1b | Build-fix loop frequency | **Open** — no reliable rate | §1.3 |
| 1c | `llm_token_count` sample (2–3 updates, per-stage timestamps) | **Open** — DB not queryable this session | §1.4 |
| 1d | Per-stage split of the non-build gaps (~4 min / ~10 min) | **Missing** — only total gaps; not IR vs scope vs file-regen vs tool | §1.1 note |
| 2a | Ephemeral container vs persistent build host | **Answered (local)**; prod/stage still **open** | §2.1 |
| 2b | Dependency/artifact caching present or absent | **Answered** (npm user cache yes; shared `node_modules` no) | §2.2 |
| 2c | Disk type for workspace/isolated dirs | **Answered** — NVMe SSD | §2.3 |
| 2d | Measured `copytree` cost | **Weak** — asserted unlikely multi-minute; not timed | §2.3 |
| 3a | New `screen_id`/versioned workspace hard requirement? | **Code yes / product open** | §3 |
| 3b | Full prod `ng build` every update vs preview/dev OK? | **Code always prod / product open** | §3 |
| 3c | Compliance/security blocking cache/reuse/parallel LLM? | **Open** | §3 |
| 4a | Target time (5 min / 2 min / out of 15–20) | **Open** (suggested default under ~5 min) | §4 |
| 4b | Latency vs LLM/infra cost vs eng effort | **Open** (suggested default: latency first) | §4 |
| 4c | Urgent user pain vs scoping ahead | **Weak** — assumed urgent from your 15–20 min report; not confirmed | §4 |
| 5a | Updates per day/hour + concurrency | **Open** | §5 |
| 5b | Typical cascade size (1 screen vs 2–3) | **Partial** — inferred 2–3 from one execute + your report; not usage stats | §5 |

**Highest-leverage holes for a solution doc:** 1b, 1c, 1d, 2a (prod), 3a–3b, 4a–4b, 5a.

---

## 1. Confirm the diagnosis with real numbers

### 1.1 Do we have actual timings?

**Yes — for a real multi-screen generation run today (not a `/plan/update`, but the same Angular install+build path).**

Source: live `agent_new.py` terminal + `isolated-angular-apps/` mtimes for workflow `05Rf3iwD_sb_5beaabd82ac6767c86dc311c`.

| Event | Evidence | Value |
|-------|----------|-------|
| Full `/agentic/plan/execute` | log `elapsed=1570.07s` | **26.2 min** |
| Phase 2 alone (`gaps_resolved=3`) | log `duration=1197.44s` | **20.0 min** for **3** gap nodes |
| LangGraph codegen | log `duration=0.00s` | negligible |
| Postgen | “Round 1: generated code is valid” ~15s after codegen | ~15s, no repair loop |

Three isolated Angular builds were created during that phase (one per UI gap), with filesystem timestamps:

| Screen build dir (short) | `package.json` | `node_modules` ready | `dist` ready | **npm install ≈** | **ng build ≈** | **install+build ≈** |
|--------------------------|----------------|----------------------|--------------|-------------------|----------------|---------------------|
| `…64544…` | 12:18:45 | 12:20:40 | 12:21:39 | **~1 min 55s** | **~59s** | **~2 min 54s** |
| `…65141…` | 12:25:39 | 12:27:25 | 12:28:51 | **~1 min 46s** | **~1 min 26s** | **~3 min 12s** |
| `…65924…` | 12:39:03 | 12:40:54 | 12:41:36 | **~1 min 51s** | **~42s** | **~2 min 33s** |

**Sum of install+build across 3 screens ≈ 8.7 minutes** out of the 20-minute phase.

**Gaps between builds (mostly LLM / IR / backend / codegen — not npm):**

| Interval | Wall clock |
|----------|------------|
| After screen1 `dist` → before screen2 `package.json` | **~4.0 min** |
| After screen2 `dist` → before screen3 `package.json` | **~10.2 min** |

So on this machine, for a 3-UI-screen sequential gap phase:

```
~20 min phase_2
  ≈ 8.7 min  npm install + ng build  (×3 sequential)
  ≈ 14 min   everything else between/around builds (LLM-heavy; overlaps with pre-build work inside each gap)
```

(The “everything else” and install/build are interleaved per screen, so they are not additive to 20; the table above is the cleanest split we can make from mtimes.)

### 1.2 Recalibration vs earlier theoretical ranges

Earlier latency doc used wide theoretical bands. **On this host today:**

| Claim in research doc | Measured here | Takeaway |
|-----------------------|---------------|----------|
| `npm install` 2–8+ min | **~1.8–2.0 min** per screen (with existing `~/.npm` cache present) | Still expensive ×N, but not “8 min” here |
| `ng build` 1–6+ min | **~0.7–1.5 min** during pipeline; **~9.6s** warm rebench with existing `node_modules` | Build itself is **not** the multi-minute monster when deps are warm |
| Build-fix loop as top risk | **Insufficient sample** (see §1.3) | Do **not** rank “cap retries” as #1 without more data |

**Benchmarks run in this session:**

- Warm `npx ng build --configuration production …` on latest `05Rf` isolated build: **`elapsed_sec=9.57`** (Angular reported compile ~5.4s).
- Controlled `npm install` rebench into `/tmp` **failed** (sandbox/npm exit-handler error after ~73s) — treat production mtimes as authoritative for install, not the failed bench.

### 1.3 How often does the build-fix loop trigger?

**UNKNOWN (no reliable rate yet).**

What we have:

- Code allows up to **`max_fix_iterations = 5`** build retries + static-analysis fix (**CODE-FACT**).
- Scrape of residual Cursor terminal text found only: `build_attempt=4`, `build_failed=1`, `build_succeeded=1` — sample too small / logs truncated; **not usable as a rate**.
- Today’s `05Rf` execute: **no evidence of multi-attempt builds** from mtimes (single install→dist span per screen looks like first-try success). Dist spans are seconds-long, not multi-cycle.

**Implication for fix ranking:** until we know retry frequency, prefer fixes that help **every** run (reuse `node_modules`, skip redundant LLM, reduce sequential screens) over “cap retries.”

### 1.4 `llm_token_count` samples

**UNKNOWN this session — collection could not be queried.**

| Attempt | Result |
|---------|--------|
| `pymongo` via system Python | ImportError (`bson.SON` conflict) |
| `mongosh mongodb://localhost:27017` | `MongoServerSelectionError: connection … closed` |
| App itself | **Is** talking to Mongo (screen/credits/execute logs succeed) |

Config shows both:

- `devDbLink=mongodb://localhost:27017`
- `devDbLink2=mongodb://…@172.168.1.19:27017/…` (remote)

So the live agent may be on a URI we could not open from this tooling session. **Need either:** working DB access, or export of 2–3 recent `workflow-update` / `angular-generation` docs with `started_at`/`finished_at`/`operation`/`sub_operation`.

Relevant `sub_operation` names to pull when DB works (**CODE-FACT**):

- `update-scope-analysis`, `update-angular-files`, `angular-codegen`, `fix-errors`, `fe-be-contract-patch`
- `workflow-update` / regeneration sub-ops via `_tracked_llm_caller`
- `ir-generation` / `update-screen-ir`

### 1.5 `/plan/update` specifically

**No `[plan/update]` lines** in the retained terminal buffer for this session.  
Today’s hard numbers are from **`/plan/execute` gap resolution**, which calls the **same** `update_angular_screen_async` → npm → `ng build` stack that `/plan/update` uses for UI screens.

**Assumption (flagged):** per-screen install+build costs on `/plan/update` ≈ same as measured here; cascade/LLM mix may differ.

---

## 2. Infra constraints

### 2.1 Where does the build run?

**MEASURED / CODE-FACT: persistent local process on a developer/server host — not an ephemeral per-request container.**

Evidence:

- `python agent_new.py` long-lived on host `himanshu-dell`
- Builds write under repo-local:
  - `generated-apps/` (workspaces, **~15MB** total — no `node_modules`)
  - `isolated-angular-apps/` (**~60GB**, **122** `*-build` dirs, **121** with `node_modules`)
- Docker binary exists; **no evidence** that Angular npm/ng runs inside a fresh container per request
- Host: **8 CPUs**, **14 GiB RAM** (at sample time: ~250Mi free / 4Gi available — memory pressure possible under concurrent builds)

**Implication:** `node_modules` caching, warm shared installs, and a build-agent pool are **viable** on this topology. Ephemeral-container-only designs are **not** required by what we see here.

> **UNKNOWN:** Whether production/stage deploy (Yotta/`yotta_files/`) differs (containerized). Local evidence ≠ prod topology.

### 2.2 Artifact / dependency caching today?

| Mechanism | Status |
|-----------|--------|
| Per-isolated-build `node_modules` (~**495MB** each) | **Present** — full copy per build |
| Shared/reused `node_modules` across versions | **Absent** — code always `_prepare_isolated_directory` → `npm install` |
| User-level npm cache `~/.npm` (~**12GB**) | **Present** — helps download/extract, still materializes full tree each time |
| Docker layer cache / BuildKit cache mounts | **Not used** by Angular generator path |
| Private npm registry mirror | **UNKNOWN** |
| `npm ci` | **Not used** — code runs `npm install` (+ optional `--legacy-peer-deps`) |

**CODE-FACT:** workspace seeded from `angular-base-project` **ignores** `node_modules`; isolated dir is a fresh tree then installed.

**CODE-FACT:** `version_retention_count=10` in `backend/config.json` — cleanup exists, but **60GB / 122 builds** shows retention still leaves a large footprint (many projects/screens).

### 2.3 Disk / storage backend

**MEASURED:** local **NVMe SSD** (`/dev/nvme0n1p2`, ext4, relatime).

- `copytree` cost exists but is unlikely the multi-minute driver on this disk.
- Dominant FS issue is **capacity + duplicated `node_modules`**, not NFS latency.

Liberty FS deploy root (**CODE-FACT** from config): `/home/user/publicfolder/liberty_fs` (also local path).

---

## 3. Non-negotiable requirements (from code vs product)

These need your confirmation. Code intent is noted.

| Requirement | Code behavior today | Soft vs hard? |
|-------------|---------------------|---------------|
| New `screen_id` / versioned workspace per UI update | **Yes** — `update_angular_screen_async` mints `screen_{timestamp}_{uuid}`; comments cite rollback safety | **UNKNOWN product hardness** — treated as hard in code |
| Version retention | Keep last **10** (`version_retention_count`) | Configurable |
| Full production `ng build` every update with file changes | **Yes** — `--configuration production` always in `_build_angular_app` | **UNKNOWN** if preview/dev build OK |
| Skip build when `affected_files=[]` | **Yes** — `skipped=True` path exists | Already a fast path for binding-only |
| Parallel gap/UI generation | **No** — sequential `for node in pending` | Engineering choice, not compliance |
| Compliance blocking cache/reuse/parallel LLM | **UNKNOWN** — nothing in code forbids npm cache reuse |

---

## 4. Priorities and success bar

**UNKNOWN — needs your answers.** Suggested defaults if you say “use best guess”:

| Question | Working default (assumption) |
|----------|------------------------------|
| Target time | Get typical single-screen update **under ~5 min**; stretch **under 2 min** only with structural build changes |
| Optimize for | **Latency first**, then LLM cost, then eng effort |
| Urgency | User-facing pain now (you reported 15–20 min) |

Will **not** lock a solution ranking until you confirm or accept these defaults.

---

## 5. Scale and usage pattern

**UNKNOWN** for production request rate / concurrency.

Local observations only:

- Long-lived single agent process handling interactive use
- Memory already tight (14GB) — **concurrent** full Angular builds on this host would contend hard
- Typical cascade size: today’s execute resolved **3 UI gaps** sequentially; your report of 5–7 node workflows with 15–20 min updates is consistent with **2–3 screens** in the blast radius, not 1

---

## Revised bottleneck ranking (evidence-weighted for THIS host)

Given measured install~2min and build~1min, with large inter-screen LLM gaps:

| Rank | Bottleneck | Evidence weight |
|------|------------|-----------------|
| **1** | **Sequential per-screen full pipeline** (LLM + install + build) × N screens | **MEASURED** — 3 screens → 20 min phase |
| **2** | **LLM/codegen between builds** (IR, scope, file regen, tools, etc.) — especially the **~10 min** gap before screen 3 | **INFERRED** from mtimes + execute duration |
| **3** | **`npm install` every version** (~2 min × N) despite npm cache | **MEASURED** |
| **4** | **`ng build` every version** (~0.7–1.5 min × N; ~10s if warm reuse) | **MEASURED** |
| **5** | Build-fix retry loops | **UNKNOWN frequency** — deprioritize until measured |
| **6** | Phase 1–3 orchestrator / LangGraph emit | **MEASURED small** on this run |

**Biggest correction to the prior research doc:** on this NVMe persistent host, **do not assume ng build is a 5-minute problem**. The larger story is **paying ~3 minutes of install+build per screen, sequentially, plus multi-minute LLM work per screen**.

---

## What would unlock a confident solution doc next

Minimum asks (highest leverage first):

1. **Confirm infra for the environment users hit** — same as this laptop/server, or containerized stage/prod?
2. **Hard constraints:** Is new `screen_id` every update mandatory? Is production `ng build` mandatory on every edit?
3. **Target:** under 5 min / under 2 min / just “out of 15–20”? Optimize for latency, cost, or eng effort? Urgent now or scoping ahead?
4. **Export or grant DB access** for `llm_token_count` on 2–3 recent `/plan/update` runs (or add stage timers — see below).
5. **Rough volume:** updates/day and concurrent builds; typical cascade = 1 screen or often 2–3?
6. **Build-fix rate (optional but useful):** of last ~20 Angular updates, how many needed attempt 2+?

### Still missing even after answers above (needs instrumentation)

- Per-stage breakdown of the multi-minute gaps (classify / IR / scope / file regen / backend / tool).
- A timed `/plan/update` run (current numbers are `/plan/execute` proxies).
- Timed `copytree` (minor on NVMe; still unmeasured).

### Cheap instrumentation to add (if you want measured `/plan/update` next)

Log monotonic durations around:

```text
regen.angular.npm_install
regen.angular.ng_build
regen.angular.build_attempt{n, success|fail}
regen.angular.llm.{classify,ir,scope,files,fix}
regen.gap_node{node_id, kind}
regen.total
```

Even one instrumented `/plan/update` would let us rank caching vs LLM dedupe vs cascade narrowing with confidence.

---

## Appendix A — Host snapshot (2026-09-03)

```
Host: himanshu-dell, Linux 7.0.0-30-generic
CPU: 8
RAM: 14Gi (sample: ~250Mi free, ~4Gi available)
Disk: NVMe /dev/nvme0n1p2 ext4
generated-apps: ~15M
isolated-angular-apps: ~60G (122 builds, ~495M node_modules each)
~/.npm: ~12G
version_retention_count: 10
Liberty FS: /home/user/publicfolder/liberty_fs
```

## Appendix B — Execute log excerpts (real)

```
[execute] phase complete … phase_id=phase_2 … gaps_resolved=3 duration=1197.44s elapsed=1554.72s
[execute] LangGraph code generation done … duration=0.00s
[postgen] Round 1: generated code is valid — no errors detected.
[execute] building AgenticPlanResponse … elapsed=1570.07s
```

## Appendix C — Explicit assumptions (do not treat as facts)

1. `/plan/update` UI path has similar per-screen install/build costs as `/plan/execute` gaps.  
2. Inter-build gaps are mostly LLM-bound (not hidden disk waits).  
3. This host’s topology matches the environment where users feel 15–20 min pain.  
4. Build-fix loops are uncommon on recent successful runs here (weak evidence).  
5. Default success bar under ~5 min if unspecified.
