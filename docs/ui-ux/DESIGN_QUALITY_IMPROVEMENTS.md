# AI-Generated UI Quality — Finalized Implementation Plan

**Scope:** `ai_processor.py` (prompt enhancement → IR schema generation) and `angular_generator.py`
(IR schema → Angular code generation).

**Status:** Finalized. This document reflects the full review — root-cause analysis, design
options, feedback, and verification against the actual `ui-ux-pro-max-skill` source code. Nothing
below is a proposal; every item was reviewed and agreed. Use this as the implementation reference.
See `IMPLEMENTATION_TASKLIST.md` for a trackable checklist of the same items.

---

## 1. Background — why this work is happening

Generated screens currently converge toward a generic "AI-made" look (flat blue/purple palettes,
default fonts, hand-rolled Tailwind components) regardless of what the user asks for. Root-cause
review of both files found specific, fixable causes already in the pipeline:

- A hardcoded palette instruction in the Angular codegen prompt (`blue-600, gray-800, slate-700`)
  that overrides whatever theme the IR schema carries.
- Worked/example palettes embedded directly in the IR schema template and in the prompt-enhancement
  "Design principles" block, acting as attractors the model imitates instead of treating as purely
  structural examples.
- A regex-based theme extractor (`_extract_theme_from_prompt`) that only fires on a handful of
  literal color keywords and silently falls back to flat blue (`#3B82F6`) for everything else.
- A structural-repair fallback theme that defaults font to `"Inter"` — the most generic font in
  AI-generated UI.
- No enumerated design direction anywhere — vague "modern, clean, professional" language carries
  no real signal.
- No component library constraint — every button/card/modal is hand-rolled from raw Tailwind
  utilities on every generation, causing both a generic look and visual drift.
- No persistence of design tokens across screens in the same app/workflow.
- Single LLM calls are asked to guarantee a strict JSON contract (or compiling code) **and** make
  good aesthetic judgment calls at the same time — competing goals that hurt consistency.

---

## 2. Finalized action items

### 2.1 Remove the generic anchors (do first)

- Remove the hardcoded `"Use modern color palettes (blue-600, gray-800, slate-700, white)"`
  instruction from the **Design Principles** section of the Angular codegen system prompt
  (`angular_generator.py::_create_system_prompt`, ~line 1463).
- Remove or genericize the worked-example theme values in:
  - `ai_processor.py::_IR_GENERATION_SYSTEM_TEMPLATE` (currently indigo/purple/pink + `Poppins`).
  - `ai_processor.py::_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE` "Design principles" section (the "you
    can use this" fully worked blue/purple palette).
- Fix `_heuristic_repair`'s `default_theme` fallback — stop defaulting font to `"Inter"`. Prefer
  deriving from the IR's own theme/direction if present before falling back to any static default.

---

### 2.2 Replace theme detection with the vendored `ui-ux-pro-max` design engine

Replaces `ai_processor.py::_extract_theme_from_prompt` entirely. Verified directly against the
repo source (`nextlevelbuilder/ui-ux-pro-max-skill`, MIT licensed, pure Python stdlib — confirmed
via direct inspection of `core.py` and `design_system.py`, no external dependencies, no network
calls).

**What to vendor** (into e.g. `services/design_reference/`):
- `data/*.csv` — including corrected filenames `charts.csv` and `ux-guidelines.csv` (not
  `chart.csv`/`ux.csv`), plus `ui-reasoning.csv` (the reasoning-rules file `design_system.py`
  depends on).
- `data/stacks/angular.csv` (Angular is one of the 22 supported stacks — confirmed present).
- `scripts/core.py` and `scripts/design_system.py`.
- The repo's `LICENSE` file, kept alongside the vendored data (MIT — attribution required).

**What NOT to vendor:** `scripts/search.py` (CLI/argparse wrapper — call the underlying functions
directly instead), `templates/`, `.claude/`, `cli/`, `skill.json` (agent-activation layer for
Claude Code/Cursor/etc. — not relevant to a fixed API-call pipeline).

**Pin to a specific release** (e.g. `v2.11.0`) rather than tracking the repo's default branch, so
nothing changes under the pipeline without a deliberate, tested update.

**Integration:**
- Call `design_system.generate_design_system(query, project_name, ...)` **in-process**, directly
  from `ai_processor.py`, right after `_enhance_user_prompt` returns. No subprocess, no network.
- Feed the enhanced-prompt text in as the query — confirmed the engine runs its own BM25 search
  internally, so a separate structured-extraction LLM call is not required as a prerequisite.
  Test this against real prompts before assuming it's sufficient on its own.
- Keep an explicit-signal short-circuit: if the user's prompt states an exact hex or "match our
  brand," use that directly and skip the lookup.
- **Known risk to test before relying on this fully:** when the detected product category doesn't
  match any row in `ui-reasoning.csv`, `design_system.py` falls back to its own generic default
  (pattern "Hero + Features + CTA," style "Minimalism + Flat Design," mood "Professional/Clean").
  This is a smaller, rarer version of the same generic-default problem being fixed here. Before
  committing fully, test a batch of intentionally vague/unusual prompts to see how often this path
  fires, and decide whether a secondary fallback is needed on top of theirs.
- The engine also exposes optional `variance` / `motion` / `density` 1–10 dials that bias style,
  motion, and spacing selection. Not required for v1 — noted as a future lever, not an action item.

---

### 2.3 Expand the IR schema to represent design direction

**Confirmed necessary — not optional.** The current `theme` object (flat single-hex palette + one
font string) has nowhere to hold what the vendored engine now returns.

Minimum additions to the IR schema:
- `direction` — a single **enumerated** value (the matched style name/keywords), not free text.
- `typography` — **two roles** (display/heading font + body font), not one font string.
- `effects` — key transition/hover/motion notes from the matched style.
- `antiPatterns` — a short "avoid" list. **Kept as its own field, not merged into `theme`** — it's
  a constraint list for the codegen prompt, not a token set.

Keep this bounded and shallow, consistent with the rest of the IR philosophy — a handful of new
fields, not a nested design-token system.

**Coordinated changes required (must ship together):**
- The JSON schema validator's `theme` definition.
- `_heuristic_repair`'s `default_theme` (also fixed here per 2.1 — don't reintroduce `"Inter"`).
- `_ir_schema_for_ui_update` (the UI-key slicing used by the design-pass flow in 2.6a).

**Open decision for implementation:** whether `components[].type` gets constrained to a fixed set
of spartan/ui primitive names (ties into 2.4) or stays free-form as today.

---

### 2.4 Component library — adopt spartan/ui

- shadcn/ui itself cannot be used — it's React/Radix-based, incompatible with Angular.
- **spartan/ui** is the agreed replacement — the established Angular-ecosystem equivalent of the
  same philosophy (unstyled, accessible primitives + Tailwind + CSS variables, copy-paste
  ownership rather than an npm black box).
- Vendor its component reference/docs once into the FE codegen prompt context (its MCP server
  isn't directly usable here since this pipeline is a fixed sequence of API calls, not an
  interactive agent).
- **Verify before committing:** community-maintained, not backed at shadcn's scale — confirm it
  covers the component set actually needed, and how well it tracks current Angular versions.

---

### 2.5 Persist design tokens at the project level — via the engine's built-in Master + Overrides pattern

- Confirmed directly in `design_system.py`: `generate_design_system()` natively supports
  `persist=True`, `output_dir`, and an optional `page=` argument, implementing a Master +
  Overrides pattern (`MASTER.md` generated once per project; per-page files only for deviations).
  **This replaces the earlier plan to build our own persistence via `workflow_context`** — use
  the engine's built-in mechanism instead of building a parallel one.
- Generate the project's design system once (first screen, or an explicit "initialize project"
  step), persist it, and have every subsequent screen's IR generation read the stored result.
- Only re-run generation for a specific screen if the user explicitly asks for a different look
  for that screen — this becomes the page-level override case, natively supported by `page=`.

---

### 2.6 Split design decisions from structural/logic correctness

**a. IR schema — structural pass + design pass (v1, implement now)**
- Structural IR generation continues to run as it does today, producing the full contract
  (`dataModels`, `dataSources`, `state.bindings`, etc.) deterministically.
- A second, narrower call runs as **step 2 of initial generation** (not only for later
  user-requested edits) — generalizing the existing `update_screen_ir` **UI-only-update** pathway,
  which already scopes edits to `screen`, `theme`, `layout`, `components`, `state` (without
  bindings) and is already structurally forbidden from touching `dataModels`, `dataSources`,
  `actions`, or `state.bindings`.
- This design pass is fed the direction/tokens/anti-patterns from 2.2/2.3 and applies them within
  the UI-scoped fields only.
- Depends on 2.3 shipping first.

**b. Angular codegen — lighter version only (v1, implement now)**
- Keep a **single** codegen call — no second Angular-generation call yet.
- Shrink the Design Principles section from a creative, judgment-based instruction to a mechanical
  one: use the exact tokens and exact spartan/ui primitives provided; do not invent a palette; do
  not hand-roll components that already exist in the library; treat `antiPatterns` as hard
  constraints.
- This also closes the earlier "leak" where IR theme values were present in context but not
  reliably reaching the actual Tailwind/CSS output.
- Separately (independent of the theme pipeline — don't conflate the two), inject the vendored
  `data/stacks/angular.csv` guidance into the same prompt as supplementary stack-level convention.

---

## 3. Explicitly deferred to v2

- **Angular codegen — true second pass (screenshot-driven, conditional).** A separate,
  narrowly-scoped call after the first codegen call and build succeed, allowed to edit only
  className/style/CSS (never `.ts` logic, template control-flow, or non-component-library
  imports). Modeled on the existing T7 callability-gate shape (cheap check → only invoke repair
  call if something's off → re-validate) rather than an unconditional stage on every generation.
  Revisit after v1 items are shipped and measured.

---

## 4. Implementation dependency order

1. **2.1** — Remove generic anchors (no dependencies, do first).
2. **2.3** — Expand IR schema for design direction (needed before 2.2's output has anywhere useful
   to go, and before 2.6a can do anything).
3. **2.2** — Vendor and integrate the `ui-ux-pro-max` design engine (replaces
   `_extract_theme_from_prompt`).
4. **2.4** — spartan/ui integration (component reference vendored into codegen prompt; coverage
   check first).
5. **2.6a** — IR structural pass + design pass split (depends on 2.2 + 2.3).
6. **2.6b** — Angular codegen design-principles simplification + stack-file injection (depends on
   2.3 + 2.4).
7. **2.5** — Project-level persistence via the engine's Master + Overrides support (can be built
   in parallel; needed for multi-screen consistency once single-screen quality is fixed).

---

## 5. Files/functions touched (traceability)

| Item | File | Function(s) |
|---|---|---|
| 2.1 | `angular_generator.py` | `_create_system_prompt` (Design Principles section) |
| 2.1 | `ai_processor.py` | `_IR_GENERATION_SYSTEM_TEMPLATE`, `_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE`, `_heuristic_repair` |
| 2.2 | `ai_processor.py` | `_enhance_user_prompt` (feed output as query), `_extract_theme_from_prompt` (removed), new call site for vendored `design_system.generate_design_system` |
| 2.2 | new | `services/design_reference/` — vendored `data/*.csv`, `data/stacks/angular.csv`, `core.py`, `design_system.py`, `LICENSE` |
| 2.3 | `ai_processor.py` | IR JSON schema (theme: `direction`, `typography`, `effects`, `antiPatterns`), `_heuristic_repair`, JSON schema validator |
| 2.4 | `angular_generator.py` | `_create_system_prompt`, component-reference context assembly |
| 2.5 | `ai_processor.py` / orchestration | Call site invoking `generate_design_system(..., persist=True, output_dir=..., page=...)`; `workflow_context` passes the stored result reference |
| 2.6a | `ai_processor.py` | `process_prompt` (add design-pass step), `update_screen_ir`, `_create_update_system_prompt`, `_ir_schema_for_ui_update` |
| 2.6b | `angular_generator.py` | `_create_system_prompt` (Design Principles section + stack-file injection) |

---

## 6. Open items to resolve during implementation

- Final list of `direction` enum values, informed by testing the vendored engine's actual output
  range across our real domain mix.
- Rate at which the reasoning-engine's generic fallback (2.2) fires on real prompts, and whether a
  secondary fallback is needed on top of it.
- spartan/ui component coverage check against actual generated-screen needs (2.4).
- Exact new IR field names for `typography`/`effects`/`antiPatterns` (2.3).
- Which vendored `ui-ux-pro-max` release to pin (latest verified: v2.11.0).
