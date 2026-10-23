# Implementation Tasklist — AI-Generated UI Quality

Companion checklist to `DESIGN_QUALITY_IMPROVEMENTS.md`. Suggested build order top to bottom.

## Phase 1 — Remove active anchors
- [x] Remove hardcoded `blue-600/gray-800/slate-700` palette instruction from `angular_generator.py::_create_system_prompt`
- [x] Remove worked-example theme (indigo/purple/pink + Poppins) from `_IR_GENERATION_SYSTEM_TEMPLATE`
- [x] Remove worked-example palette from `_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE` "Design principles" section
- [x] Fix `_heuristic_repair`'s `default_theme` — stop defaulting font to `"Inter"`
- [x] Stop `_extract_theme_from_prompt` silent `#3B82F6` default (superseded by Phase 3 removal)

## Phase 2 — IR schema expansion
- [x] Add `direction` (enum), `typography` (display + body), `effects`, `antiPatterns` to IR theme schema
- [x] Update JSON schema validator to match new theme shape
- [x] Update `_heuristic_repair`'s `default_theme` to match new shape
- [x] Update `_ir_schema_for_ui_update` UI-key slicing to include new fields
- [x] Normalize path: `_normalize_ir_before_validation` migrates legacy `font` → typography and hoists nested `antiPatterns`

## Phase 3 — Vendor and integrate ui-ux-pro-max design engine
- [x] Pin a specific release (verified: v2.11.0) — do not track `main`
  - Vendored at `backend/design/design_reference/` (`VENDORED_FROM.md`: tag v2.11.0 / commit 6142b07).
- [x] Vendor `data/*.csv` (incl. `charts.csv`, `ux-guidelines.csv`, `ui-reasoning.csv`), `data/stacks/angular.csv`, `core.py`, `design_system.py`, `LICENSE`
  - Assets already present under `backend/design/design_reference/`.
  - Integration API: `backend/services/design_reference/` (`resolve_theme_preferences`).
- [x] Wire in-process call to design engine from `ai_processor.py`, after `_enhance_user_prompt`
  - Uses `DesignSystemGenerator.generate()` (dict) — not the string-returning `generate_design_system()` wrapper.
  - Query = enhanced prompt text (no separate structured-extraction LLM call).
- [x] Remove `_extract_theme_from_prompt`
- [x] Add explicit-signal short-circuit (user-stated hex / "match our brand" skips lookup)
- [x] Test enhanced-prompt text as query directly — confirm no separate structured-extraction call is needed
  - Verified: banking/fintech enhanced-style text → `Banking/Traditional Finance` + real palette/typography/antiPatterns.
- [x] Test a batch of vague/unusual prompts — measure generic reasoning fallback; decide secondary fallback
  - Accurate detector: `_find_reasoning_rule(category)` empty ⇒ fallback.
  - Result on 15 mixed vague/domain prompts: **0/15 (0%)** hit the hardcoded generic reasoning path
    (even "General" matches a ui-reasoning row). BM25 can still pick odd product categories for nonsense
    prompts — that is engine ranking noise, not the generic-default path.
  - **Decision: no secondary fallback needed for v1.**

## Phase 4 — spartan/ui component library
- [ ] Confirm spartan/ui covers the component set needed for generated screens
- [ ] Vendor spartan/ui component reference/docs into FE codegen prompt context
- [ ] Decide whether `components[].type` gets constrained to spartan/ui primitive names

## Phase 5 — Split design from structure/logic
- [x] Generalize `update_screen_ir` UI-only-update pathway to run as step 2 of initial generation (not just later edits)
  - New `_run_design_pass` method in `ai_processor.py` wired as STEP 3.5 in `process_prompt`, after IR generation and before self-verify/validation.
  - Uses same machinery as `update_screen_ir(category="ui")`: `_create_update_system_prompt(category="ui")` + `_ir_schema_for_ui_update` + `_merge_update_response_into_current`.
  - Guard: skipped for Figma compiler mode and when `_has_design_tokens` returns False (no IR-visible tokens to apply).
  - Audit fixes: accept wrapper OR bare UI-schema response shapes (avoids silent no-op); re-raise `BillingError`; include `antiPatterns` in `_has_design_tokens`.
- [x] Feed direction/tokens/antiPatterns from Phase 3 into this design pass
  - `_build_design_pass_user_prompt` formats all IR-visible tokens (direction, color roles, typography, effects, antiPatterns) as explicit instructions.
  - Response format explicitly requires `{updated_ir_schema, changes_summary}` wrapper (aligned with UI-update system prompt).
- [x] Shrink Angular codegen Design Principles section to mechanical token/primitive usage instruction
  - Removed: redundant "IR schema is the source of truth" duplicate line, vague creative instructions, redundant contrast checklist, and forced `transition-all duration-300` defaults in Design Principles / INTERACTIVITY.
  - What remains: mechanical token-consumption rules + contrast safety (one line).
  - Note: "exact spartan/ui primitives" wording from 2.6b waits on Phase 4 (spartan/ui not vendored yet).
- [x] Inject `data/stacks/angular.csv` guidance into Angular codegen prompt (separate from theme pipeline)
  - New `backend/services/design_reference/stack_guidelines.py` reads angular.csv once (lru_cache) and formats High-severity Do/Don't items.
  - Injected at end of `angular_generator.py::_create_system_prompt` as `## Angular Stack Conventions` block.

## Phase 6 — Persistence
- [x] Wire `generate_design_system(..., persist=True, output_dir=..., page=...)` for project-level Master + Overrides persistence
  - New `backend/services/design_reference/design_persistence.py`: path helpers, JSON cache r/w, and engine MASTER.md write (via `persist_design_system` from vendored `design_system.py`).
  - Storage layout: `{design_systems_root}/{project_slug}/master.json` + `MASTER.md`; page overrides under `pages/{screen_slug}.json` + `{screen_slug}.md`.
  - `save_master` / `save_page_override` write both JSON (primary cache) and the engine's MD format.
  - `_write_engine_md` failures are logged and suppressed — JSON cache is always the primary store.
- [x] Confirm `workflow_context` carries a reference to the stored design system across screens in the same project
  - Resolution order in `resolve_theme_preferences`: explicit-signal short-circuit → page override JSON → master JSON → engine call (generates + persists on miss).
  - `workflow_context["_design_override_requested"] = True` → `force_refresh=True` passed to engine (bypasses cache for that screen, writes page override).
  - `ai_processor.__init__` adds `self.design_systems_root` (config key `designSystemsRoot`; defaults to sibling of fileUploadPath parent).
  - Subsequent screens for the same `project_id` get `source="design_cache_master"` — engine is NOT re-run.
- [x] Confirm per-screen override path works when user requests a different look for one screen
  - `force_refresh=True` + `screen_id` → engine re-runs, result saved as `pages/{screen_slug}.json` (MASTER untouched).
  - Next call for same project+screen → returns page override (`source="design_cache_page"`), not master.
  - All three paths tested and verified (cache miss → master write, cache hit → master return, force_refresh → page override write + return).

## Deferred to v2 (not in this build)
- [ ] Screenshot-driven conditional design-gate second pass for Angular codegen (className/style/CSS-only edits, T7-gate-shaped)

## Open decisions to close out during implementation
- [x] Finalize `direction` enum value list — `DESIGN_DIRECTION_ENUM` from styles.csv v2.11.0
- [x] Finalize field names — `theme.typography.{display,body}`, `theme.effects`, top-level `antiPatterns`
- [x] Design-engine generic fallback secondary layer — **not needed for v1** (0% on probe batch)
- [ ] Confirm spartan/ui version compatibility with current Angular version in use

## Phase 3 implementation notes
- Files: `backend/services/design_reference/{__init__.py,theme_resolver.py,README.md}`, `backend/services/ai_processor.py`, `backend/design/design_reference/VENDORED_FROM.md`
- `theme_preferences` full shape: direction, color roles (primary/secondary/accent/background/text), typography, effects, antiPatterns, googleFontsUrl, source, usedGenericReasoningFallback, productCategory, pattern
- LLM-facing subset (IR-relevant only): direction, color roles, typography, effects, antiPatterns, googleFontsUrl — internal keys (source, productCategory, pattern, usedGenericReasoningFallback) are stripped in `_create_user_prompt` before the JSON dump
- Design engine skipped when `figma_json_str` is set (Figma compiler mode) — theme comes from the uploaded Figma, not BM25
- Persistence (`persist=True` / Master+Overrides) deferred to Phase 6 by design
- Next: Phase 4 (spartan/ui) or Phase 6 (persistence)
