# Live Build Issues — LinkedIn Recruiter Workflow (2026-09-18)

_Dev deploy of Issues 1–9. Prompt: same LinkedIn recruiter workflow. Result: workflow not created correctly._

**Source log:** `docs/architecture-review/live-logs.txt`  
**Project:** `proj564173d0ca324143_sb_5beaabd82ac6767c86dc311c`  
**Workflow name:** Linkedin Recr2  
**Phase:** `239909d6-4d5e-43d1-9c2f-21c1ec4e9fe8`  
**Nodes planned:** `jd_input_screen` → `profile_fetching_tool` → `display_profiles_screen` → `end`

---

## Verdict

Issues 1–9 (architecture-review fixes) did **not** cause this failure. The build failed for independent reasons:

1. **Critical:** `profile_fetching_tool` never registered (syntax errors on all 6 gap-tool attempts; now has heuristic + dedicated syntax fixer + flash→pro).
2. **High (now fixed):** Angular FE for `jd_input_screen` failed round 1 (RECITATION reused a 3.1-pro cache on 2.5-pro); fallback now drops CachedContent.
3. **High (now fixed):** IR `dataSources` missing `type` on every screen — previously burned budget via broken LLM repair; now injected in `_normalize_ir_before_validation`.

Final HTTP status after resume: `422 Unprocessable Entity` on `/agentic/workflow/resume`.

---

## Summary table

| # | Issue | Severity | Blocks workflow? | Log lines (approx) |
|---|---|---|---|---|
| 1 | KnowledgeSources arbiter `.target_store` attribute error | Low | No | 46–51 |
| 2 | Enterprise Graph schema not flowing into Stage 2a | Medium | No (degrades quality) | 121–122, 136–137 |
| 3 | IR `dataSources` missing `type` — every screen, every run | High | ✅ Fixed (normalize before repair) | 280–282, 457–459, 736–738, 941–943 |
| 4 | IR repair LLM hits 65K token cap; always returns broken JSON | High | ✅ Fixed (heuristic-first + fragment repair + 8K cap) | 285–291, 461–468, 742–747, 946–952 |
| 5 | `job_description` self-join persistence lineage unresolved | Low | ✅ Fixed (compile same-node writer+reader link) | 173–175 |
| 6 | Angular RECITATION → context-cache model mismatch kills FE gen | High | ✅ Fixed (drop cache on model fallback) | 526–616 |
| 7 | `profile_fetching_tool` syntax errors on all 6 attempts — tool never registered | **Critical** | ✅ Fixed (heuristic + dedicated syntax fixer + flash→pro) | 630–651, 1079–1106 |
| 8 | Budget exhausted due to wasted repair calls | Medium | ✅ Fixed (UI floor + gap-tool flash/pro rates + 1.55 buffer) | 340–347 |
| 9 | Design engine missing from dev server | Low | No | 207–215, 385–393, 664–672, 871–878 |
| 10 | Backend function returns empty required output (`user_action`) | Medium | No (advisory) | 783 |

---

## Issue 1 — KnowledgeSources LLM Arbiter crash (non-blocking)

**Severity:** Low  
**Blocks workflow:** No

### Evidence

```
LLM Arbiter failed, falling back: 'SchemaDecision' object has no attribute 'target_store'
LLM Arbiter (KG-only) failed, falling back to simple logic: 'SchemaKgOnlyDecision' object has no attribute 'target_store'
```

### What happened

The MERGE + KG arbiter LLM call returned a response parsed into `SchemaDecision` / `SchemaKgOnlyDecision`, but the code then accessed `.target_store`, which does not exist on those objects.

### Impact

Fell back to simple heuristic selection. Knowledge-source routing still returned 200 OK, but was not LLM-guided.

### Root cause

Attribute name mismatch between the Pydantic/response model and the code that reads the arbiter result. Unrelated to Issues 1–9.

---

## Issue 2 — Enterprise Graph schema lookup skipped in Stage 2a

**Severity:** Medium  
**Blocks workflow:** No (degrades schema quality)

### Evidence

```
Enterprise Graph schema lookup skipped: missing subscriber_id/org_id on state
```

(Repeated for Stage 2a batch nodes.)

### What happened

Stage 2a Node Schema Builder tried to look up ArcadeDB collection schemas, but `subscriber_id` / `org_id` were not on the planner state at that point. Earlier in the same request, ArcadeDB had already connected and listed 21 collections.

### Impact

Nodes were schema-built without real collection field lists. Higher chance of invented field names and weak bindings.

### Root cause

Identity context from the request is not threaded into Stage 2a planner state, even though it was available earlier in the knowledge-sources / Stage 0 path.

---

## Issue 3 — IR `dataSources` missing required `type` (every screen, every run)

**Severity:** High  
**Blocks workflow:** No (heuristic repair eventually succeeds)  
**Status:** ✅ **DONE** (2026-09-18) — `_normalize_ir_before_validation` injects `dataSources[].type` before validation / LLM repair

### Evidence

```
✗ IR schema validation failed with 2 errors
  - At 'dataSources -> 0': 'type' is a required property
  - At 'dataSources -> 1': 'type' is a required property
```

Observed for:

- `jd_input_screen` (execute round 1 and resume round 1)
- `display_profiles_screen`
- `jd_input_screen` (gap retry round 2)

### What happened

Every IR generation attempt produced `dataSources` without the required `type` field. Validation failed, then triggered LLM repair (see Issue 4). Heuristic repair finally added the missing fields and validation passed.

### Root cause

IR generation prompt / schema example for `gemini-3.5-flash-lite` does not reliably enforce `dataSources[].type`. The field is recovered only after a failed LLM repair + heuristic pass. Pre-validation normalizer already fixed missing `config` but not missing `type`.

### Fix applied

1. **`AIProcessor._infer_datasource_type`** — returns `dynamic`|`external`; preserves valid type; infers `external` from `config.externalUrl` / `url` / `auth`; else `dynamic`.
2. **`_normalize_ir_before_validation`** — always sets `dataSources[].type` via that helper (same pass that already adds missing `config`). Runs **before** validation, so the expensive LLM repair path is never entered for type-only failures.
3. IR generation prompt — `type` listed as **REQUIRED** under every dataSource.
4. **IR generation model** — stopped using `gemini-3.5-flash-lite` (live logs showed it for every IR attempt). Now `gemini-3.5-flash` via `model_config.ir_schema_generation` and the `ai_processor` fallback (lite was too weak for first-draft IR fidelity).
5. Unit tests: `tests/test_issue3_datasource_type_normalize.py` (5 passing).

### Recommended fix (historical)

- Enforce `type` in the IR generation schema example / validator before repair.
- Prefer a deterministic heuristic inject of `type` **before** calling the expensive LLM repair.

---

## Issue 4 — IR repair LLM hits output token cap; never returns valid JSON

**Severity:** High  
**Blocks workflow:** No (heuristic fallback works) — but pure time/money waste  
**Status:** ✅ **DONE** (2026-09-18) — heuristic-first; fragment LLM repair; 8K output cap; abort on truncation

### Evidence

```
Recorded LLM call: ir-generation/repair-ir-schema - 69941 tokens (in=4409, out=65532)
No valid JSON object found in response (bracket matching failed)
LLM repair failed, applying heuristic repairs
✓ Schema repaired and validated on attempt 1
```

Same pattern on every screen repair in this run (~3–4 minutes and ~$0.20 each).

### What happened

The repair call consistently hit the **65,532** output-token ceiling. The truncated response failed bracket matching. Heuristics then fixed the schema.

### Root cause

Repair prompt dumped the **full IR** and asked the model to rewrite it. Output was unbounded, so Gemini hit max tokens, returned unclosed JSON, then the same heuristic that would have worked in milliseconds ran after a 3-minute wait.

### Fix applied

1. **`validate_and_repair_ir`** — run `_heuristic_repair` **before** any LLM call. If the schema then validates, skip LLM entirely (this was the live LinkedIn path).
2. **`repair_ir_schema`** — send only the broken top-level keys (`_ir_repair_fragments`), merge with `_merge_ir_repair_fragments`. Never ask the model to echo the whole IR.
3. **Cap** `max_output_tokens=8192`. Detect `MAX_TOKENS` / ≥32k completion tokens and abort to heuristic immediately.
4. Unit tests: `tests/test_issue4_ir_repair_truncation.py` (8 passing).

### Recommended fix (historical)

- Send only the broken fragment(s) to repair, not the full IR.
- Or skip LLM repair when errors are only missing `dataSources[].type` (deterministic patch).
- Cap / detect max-output truncation and abort early instead of waiting ~3 minutes.

---

## Issue 5 — Persistence lineage unresolved for `job_description`

**Severity:** Low  
**Blocks workflow:** No  
**Status:** ✅ **DONE** (2026-09-18) — same-node writer+reader compiles a `persistenceLink` on `jd_id`

### Evidence

```
persistence lineage errors: ["collection 'job_description' has writers ['jd_input_screen'] and readers ['jd_input_screen'] but no compiled persistenceLink (join key unresolved)"]
```

### What happened

`jd_input_screen` is both writer and reader of `job_description`. Stage 4c could not compile a `persistenceLink` (join key unresolved). Links for `all_Candidates` on the tool and display screen did attach.

### Impact

Warning only. Persistence fixture enforcement cannot validate the JD read/write cycle for that node.

### Root cause

Same-node writer/reader (self-join) is not handled by the Stage 4c persistence compiler. There is no graph edge from the node to itself, and the fallback path skipped `writer_id == reader_id`.

### Fix applied

1. **`compile_persistence_links`** — after cross-node pairs, emit a `cardinality="self"` link when a node is in both `writers` and `readers`. Join key from declared outputs / filter_keys / `*_id` fields (`jd_id` on the live screen).
2. **Dual-role fixture** — `infer_persistence_role` uses the dataSource action when the node is both writer and reader (insert → writer, find → reader). List-all reads are allowed without filtering on the join key; writes must still persist it.
3. Pin text for that node is `writer+reader` with both rules.
4. Unit tests: `tests/test_issue5_self_join_persistence.py`.

---

## Issue 6 — Angular RECITATION then context-cache model mismatch (FE screen gen)

**Severity:** High  
**Blocks workflow:** Yes for round 1 `jd_input_screen`; recovered in round 2  
**Status:** ✅ **DONE** (2026-09-18) — RECITATION fallback drops CachedContent when the request model no longer matches the cache model

### Evidence

```
Finish reason: FinishReason.RECITATION
RECITATION detected with gemini-3.1-pro-preview — switching to fallback model gemini-2.5-pro
API call attempt 2/5 failed: 400 INVALID_ARGUMENT.
  Model used by GenerateContent request (models/gemini-2.5-pro)
  and CachedContent (models/gemini-3.1-pro-preview) has to be the same.
...
RuntimeError: Gemini API call failed after 5 attempts
[Gap resolution] Generation failed for node 'jd_input_screen' (round 1/2)
```

### What happened

1. Primary Angular model (`gemini-3.1-pro-preview`) aborted with `RECITATION`.
2. Fallback switched to `gemini-2.5-pro` but kept the context cache created for `gemini-3.1-pro-preview`.
3. Gemini rejected every remaining attempt (model ≠ cache model).
4. All 5 attempts failed; screen generation failed in round 1.

Round 2 later generated `jd_input_screen` successfully (`screen_20260918062657_4ee18fb9`).

### Root cause

RECITATION fallback in `angular_generator.py` changes the model without clearing or recreating the context cache for the new model.

### Fix applied

1. **`_adjust_angular_retry_for_model_cache`** — on RECITATION (enum, name, or integer `4`), switch to `fix_model_name` (`gemini-2.5-pro`) and drop CachedContent when it was created for a different model. Also drop cache if Gemini returns the CachedContent model-mismatch 400.
2. **`_invoke_model`** — retries use `active_cache`; after drop, the call sends the full prompt (`prompt_text`) with no cache instead of the cached user-only fragment. Integer finish_reason `4` is rewritten to `FinishReason.RECITATION` so fallback still fires.
3. Unit tests: `tests/test_issue6_angular_recitation_cache.py` (live recitation → 400 sequence, integer-4 recitation, `_invoke_model` wiring).

---

## Issue 7 — `profile_fetching_tool` fails all gap-tool attempts (Critical)

**Severity:** Critical  
**Blocks workflow:** **Yes** — this is why the workflow was not created correctly  
**Status:** ✅ **DONE** (2026-09-18) — heuristic empty try/except fill + dedicated syntax-fixer LLM + flash→pro on the next generate

### Evidence (round 1)

```
attempt=1/3 REJECTED: expected an indented block after 'except' statement on line 75
attempt=2/3 REJECTED: expected 'except' or 'finally' block on line 80
attempt=3/3 REJECTED: expected an indented block after 'except' statement on line 45
FAILED after 3 attempts
[Gap resolution] Generation failed for node 'profile_fetching_tool' (round 1/2)
```

### Evidence (round 2 — same failure pattern)

```
attempt=1/3 REJECTED: expected an indented block after 'except' statement on line 73
attempt=2/3 REJECTED: expected 'except' or 'finally' block on line 79
attempt=3/3 REJECTED: expected an indented block after 'except' statement on line 65
FAILED after 3 attempts
[Gap resolution] Generation failed for node 'profile_fetching_tool' (round 2/2)
```

Final resume response: `422 Unprocessable Entity`.

### What happened

Gap-tool codegen for the Unipile LinkedIn `profile_fetching_tool` (model `gemini-3.5-flash`) repeatedly produced invalid `try`/`except` bodies. All 6 attempts across 2 rounds failed AST validation. The tool was never registered. Downstream `display_profiles_screen` still built, but the core sourcing node has no working capability.

### Root cause

Large, multi-API gap tools exceed reliable one-shot generation quality for `gemini-3.5-flash`. Backend function generation has a **dedicated syntax fixer**; gap tools only full-retry generation and have no equivalent targeted syntax fix pass.

### Fix applied

1. **Heuristic** (`gap_tool_syntax.py`) — empty `except` / bare `try` get an indented `raise` (not `pass`, which mock detection rejects). Covers the live error class without an LLM call.
2. **Dedicated syntax-fixer LLM** — if the body still fails `ast.parse`, a patch-only call on `gemini-2.5-pro` rewrites `function_code` (does not regenerate the tool).
3. **Model escalation** — the next full generate after a syntax reject uses `gemini-2.5-pro` instead of `gemini-3.5-flash`.
4. Unit tests: `tests/test_issue7_gap_tool_syntax.py`.

### Relation to Issue 9 (architecture)

Issue 9 (output contract pinning) never ran here — the tool never got past syntax validation. This failure is **upstream** of contract enforcement.

---

## Issue 8 — Workflow budget exhausted mid-build

**Severity:** Medium  
**Blocks workflow:** Yes until manual resume  
**Status:** ✅ **DONE** (2026-09-18) — execute estimate now floors UI screens and prices gap tools at 3.5-flash + 2.5-pro fixer rates

### Evidence

```
BillingError code=WORKFLOW_BUDGET_EXHAUSTED phase_id=... node_id=jd_input_screen elapsed=433.97s
status=paused_credit
POST /agentic/plan/execute → 402 Payment Required
```

On resume:

```
cap exhausted — wallet-only mode
spent=0.932386 predicted=0.900000 available=43.9899
```

### What happened

Screen 1 alone (prompt enhance → IR → broken repair → backend codegen) ran ~7+ minutes and exhausted the workflow budget cap (~$0.90 predicted) before the tool node. User resumed; build continued in wallet-only mode.

### Root cause

1. Wasted IR repair calls (Issues 3 + 4) consumed budget and wall clock before expensive gap-tool codegen.
2. `/plan/estimate` under-counted execute: the learned UI intercept (~$0.08–0.17/screen) was from an older flash-only path, and gap tools were priced as 210-token flash-lite snippets (~$0.003). Live Angular is `gemini-3.1-pro-preview`; gap tools are `gemini-3.5-flash` plus a 2.5-pro syntax fixer. The $0.90 cap was below one screen.

### Fix applied

1. **UI floor** — `UI_MIN_COST_USD_PER_SCREEN = $0.48` after the pipeline multiplier so two GENERATE screens cannot register below a live screen-1 spend.
2. **Gap-tool rates** — template/output tokens match Unipile-class codegen; billed at gemini-3.5-flash plus an expected 2.5-pro syntax-fixer pass.
3. **Buffers** — execute repair 20%→25%, global safety 1.40→1.55.
4. Unit tests: `tests/test_issue8_workflow_budget_estimate.py` (LinkedIn 2 UI + 1 tool predicted cost beats the live $0.90 cap).

---

## Issue 9 — Design engine missing on dev server (non-blocking)

**Severity:** Low  
**Blocks workflow:** No

### Evidence

```
FileNotFoundError: Vendored design data not found at
  /data/backend-script/aiqod-dev-jobs/aiqod-dev-genai/design/design_reference/data
Design engine failed; continuing without theme lookup
```

Fired on every screen generation in the log.

### Impact

Design pass skipped (`[STEP 3.5] Design pass skipped (no design tokens available)`). Screens generate without design-system tokens. Logs are very noisy.

### Recommended fix

Mount vendored design data on the dev path, or downgrade this to DEBUG when the path is known-missing in that environment.

---

## Issue 10 — Advisory conformance: empty required output `user_action`

**Severity:** Medium  
**Blocks workflow:** No (advisory / non-blocking)

### Evidence

```
Function 'fetch_candidates_batch' advisory conformance failed (non-blocking):
Function 'fetch_candidates_batch' returned empty/null required outputs ['user_action']
with no declared error. Generic/empty success is not acceptable.
```

Function was still registered and Angular for `display_profiles_screen` succeeded.

### Impact

Matches the broader empty-success / missing declared output failure mode from the architecture review. Downstream consumers may see empty `user_action` at runtime.

### Root cause

Generated backend function does not populate a required IR output on the success path, and does not return a declared error either.

---

## Timeline (compressed)

| Time (UTC-ish from log) | Event |
|---|---|
| 11:19 | Stage 0 start; knowledge sources; scratch preview planning begins |
| 11:21–11:23 | Stages 1→4c plan OK; gap/code emission skipped (preview); preview 200 |
| 11:23 | Execute starts; `jd_input_screen` IR + backend |
| 11:23–11:30 | IR missing `type`; repair wastes ~3 min; backend gen; **budget exhausted** → 402 |
| 11:37 | User resumes (wallet-only) |
| 11:37–11:43 | `jd_input_screen` IR/backend again; Angular starts |
| 11:44–11:44 | Angular **RECITATION** + cache mismatch → screen fail round 1 |
| 11:44–11:48 | `profile_fetching_tool` gap-tool **3/3 syntax fail** |
| 11:48–11:56 | `display_profiles_screen` succeeds → `ui:screen_20260918061842_91a64fa1` |
| 11:56 | Gap retry round 2/2 for 2 unresolved nodes |
| 11:56–12:08 | `jd_input_screen` succeeds on retry → `ui:screen_20260918062657_4ee18fb9` |
| 12:08–12:12 | `profile_fetching_tool` gap-tool **3/3 syntax fail again** |
| 12:12 | Resume returns **422**; reservation released |

---

## What succeeded despite the failures

- Scratch preview graph planning (Stages 1–4b) completed with valid bindings.
- `display_profiles_screen` UI generated and built.
- `jd_input_screen` UI generated on gap retry round 2.
- Backend functions for screen dataSources were eventually registered (after syntax fixer).
- Issue 9 contract logic was **not exercised** — gap tool never reached contract classification.

---

## Priority for follow-up fixes

1. **Medium — Issue 2:** Pass `subscriber_id`/`org_id` into Stage 2a state for Enterprise Graph lookups.
2. **Medium — Issue 10:** Tighten backend success-path required outputs (related to architecture empty-success theme).
3. **Low — Issues 1, 9:** Arbiter attribute fix; design-data deploy path.
