# Integrating ui-ux-pro-max Design Data — Implementation Plan (v2, corrected)

Based on: a full read of `ai_processor.py` / `angular_generator.py`, and a direct read of the
`nextlevelbuilder/ui-ux-pro-max-skill` repo (MIT licensed). This replaces the earlier draft — the
first version guessed at a generic "JSON + keyword search" shape; the actual repo is CSV data +
a BM25/regex hybrid ranking engine + a dedicated multi-domain reasoning module, all pure-stdlib
Python with no network calls. That's a better fit for your pipeline than what I originally described.

---

## 1. What's actually in the repo, and what to vendor

```
src/ui-ux-pro-max/
├── data/
│   ├── products.csv       # 192 product types, keyed to style/color/typography recs
│   ├── styles.csv         # 84 UI styles (glassmorphism, brutalism, bento grid, etc.)
│   ├── colors.csv         # 192 color palettes, 1:1 aligned with product types
│   ├── typography.csv     # 74 font pairings
│   ├── chart.csv          # chart-type recommendations for dashboards
│   ├── ux.csv             # 98 UX guidelines / anti-patterns
│   ├── motion.csv         # GSAP motion presets
│   └── stacks/            # per-framework guideline files — includes an Angular file
├── scripts/
│   ├── search.py          # CLI entry point (argparse wrapper — NOT what you vendor)
│   ├── core.py            # BM25 + regex hybrid search engine
│   └── design_system.py   # multi-domain reasoning engine — this is the real payload
└── templates/              # platform-specific SKILL.md templates — irrelevant to you
```

**Vendor:** `data/*.csv`, `data/stacks/` (at least the Angular file), `scripts/core.py`,
`scripts/design_system.py`.

**Don't vendor:** `scripts/search.py` (CLI/argparse wrapper for terminal use — you'll call the
underlying functions directly, not shell out to this), `templates/`, anything under `.claude/`,
`cli/`, or `skill.json` — that's the agent-activation layer for Claude Code/Cursor/etc., and
your pipeline is a fixed sequence of Gemini calls, not an agent deciding what to load.

Keep their LICENSE file alongside the vendored data (MIT — attribution required, no other
restriction). Suggested location: `services/design_reference/` in your repo, e.g.
`services/design_reference/data/*.csv` + `services/design_reference/engine/{core.py,design_system.py}`.

**Call it in-process, not as a subprocess.** `ai_processor.py` is already Python — import
`design_system.py`'s generation function directly rather than shelling out to `search.py`. Zero
network calls, zero subprocess overhead, and it's confirmed pure standard library (no pip
installs needed), so there's no dependency conflict risk with your existing environment.

---

## 2. What `design_system.py` actually returns

Calling it (via CLI, for illustration — `search.py "fintech banking" --design-system -p "MyApp" --json`)
runs 5 parallel domain searches — product type, style, color palette, layout/section pattern,
typography — and returns one combined structured result:

- a recommended **pattern** (e.g. hero-centric, conversion-optimized) with a section list
- a matched **style** (name, keywords, "best for", performance/accessibility notes)
- a 5-role **color palette** (primary, secondary, CTA, background, text) with hex values
- a **font pairing** (display + body, with Google Fonts links)
- **key effects** (transitions, hover behavior, motion)
- an **anti-patterns list** — explicit "avoid" items scoped to the matched product type (their
  own example: "AI purple/pink gradients" flagged as an anti-pattern for banking)
- a **pre-delivery checklist** (contrast ratios, focus states, `prefers-reduced-motion`, etc.)

The anti-patterns list is the one piece that doesn't map to anything in your current schema — it's
not a theme value, it's a "don't" list scoped per-generation, which is exactly the kind of named,
specific constraint that's more effective than a vague "modern/clean" instruction.

The query itself is free text, matched via BM25/regex — not a fixed set of keyword phrases like
your current `_extract_theme_from_prompt`. That likely means you don't need a separate
structured-extraction LLM call to feed it: the enhanced prompt text itself can probably go in as
the query directly. Worth testing this before building anything extra — if BM25 matches well
against your enhanced-prompt phrasing as-is, that's one fewer moving part than originally planned.

---

## 3. `_extract_theme_from_prompt` — replace with a direct call

Current state (`ai_processor.py`, line 2448–2475): regex against 8 literal phrases
(`blue\s*theme`, etc.), falling through to a hardcoded `#3B82F6` default for everything else.

Replacement: after `_enhance_user_prompt` (line 1677) returns, pass the enhanced prompt text into
the vendored `design_system.py` generation function. The dict it returns becomes your new
`theme_preferences` — richer than today's flat `{primary, secondary}` shape, which is why the IR
schema change in section 5 below is a prerequisite, not optional.

Keep an explicit-signal short-circuit: if the user's prompt states an exact hex or "match our
brand," use that directly and skip the lookup — same principle as today's regex path, just
triggered by a real signal instead of 8 fixed phrases.

No-match fallback: `design_system.py` likely always returns *something* (it's a ranked match, not
an exact-match lookup), so this may not need a separate fallback path the way the regex version
did — confirm this by testing an intentionally vague/generic prompt against it before removing
the old default entirely.

---

## 4. IR schema changes (prerequisite — confirmed necessary)

`_IR_GENERATION_SYSTEM_TEMPLATE`'s `theme` object (line 137–151) only has room for flat hex values
and one font string. What `design_system.py` returns needs more surface area:

- `direction` — the matched style name/keywords (single enumerated value downstream steps can react to)
- `typography` — two roles (display + body), not one font string
- `effects` — key transition/hover/motion notes
- `antiPatterns` — a short list, injected as its own labeled block in the FE codegen prompt (not
  merged into `theme` — it's a constraint list, not a token set)

Also remove the worked example in the same schema block (indigo `#6366F1` / purple `#8B5CF6` /
pink `#EC4899`, font `Poppins`) — same reasoning as before: worked examples become de facto
defaults regardless of what the retrieval step selects.

This is a coordinated change, not isolated: `_heuristic_repair`'s `default_theme` (line 3693,
which also reintroduces `"font": "Inter"` — worth dropping that specifically) and wherever the
JSON schema validator defines `theme`'s expected keys need to move together with this schema
change, or generated screens that hit the repair path will collapse back to the old flat shape.

---

## 5. Persistence: adopt their Master + Overrides pattern instead of inventing one

This directly solves the "design tokens don't persist across screens in the same app" problem
(your point 7 from earlier) — and it's already a solved design in the repo, not something to
build from scratch:

```
design-system/
├── MASTER.md           # generated once per project — global source of truth
└── pages/
    └── <page-name>.md  # page-specific overrides — only deviations from Master
```

Retrieval logic: when generating a given screen, check for a page-specific override file first;
if none exists, fall back to the project's MASTER file exclusively.

Mapped onto your architecture: generate the design system once per project (first screen, or an
explicit "initialize project" step), store it — `workflow_context` is the natural carrier, since
it already flows step-to-step — and every subsequent screen's IR generation reads that stored
result instead of re-running the lookup fresh. Only re-run the lookup for a screen if the user
explicitly asks for a different look for that specific screen, which becomes the page-level
override case.

---

## 6. angular_generator.py — two separate changes, not one

**a) Remove the actively-competing instruction.** `_create_system_prompt` (line 1248), Design
Principles section (line 1458), line 1463: *"Use modern color palettes (blue-600, gray-800,
slate-700, white)."* This has to go regardless of anything else here — it's a concrete, repeated
instruction that will keep overriding whatever the IR schema's theme carries. Replace with:
consume the IR schema's theme object as literal source of truth, do not substitute your own.

**b) Separately, pull in their Angular stack file.** `data/stacks/` includes Angular-specific
guidance — independent of the theme/palette lookup, this is stack-level convention (component
patterns, idioms) worth injecting into the FE codegen prompt as a supplement. This is a second,
independent vendoring target from the same repo, not part of the theme pipeline — don't conflate
the two when implementing.

---

## 7. What this doesn't cover

Still separate, later work: the constrained component-library layer (spartan/ui), the
render/screenshot/critique loop, and the v2 skill-selection mechanism. None of them are
prerequisites for this integration — this is useful standalone.

---

## Summary of touch points

| File | Location | Change |
|---|---|---|
| `ai_processor.py` | `_PROMPT_ENHANCEMENT_SYSTEM_TEMPLATE` (~596–627) | Remove worked color example |
| `ai_processor.py` | `_extract_theme_from_prompt` (2448) | Replace with direct in-process call to vendored `design_system.py` |
| `ai_processor.py` | `_IR_GENERATION_SYSTEM_TEMPLATE` theme schema (137–151) | Add `direction`, `typography` (2 roles), `effects`, `antiPatterns`; remove worked example |
| `ai_processor.py` | `_create_user_prompt` (2784, injection at 2854) | Keep injection point; add a separate labeled block for anti-patterns |
| `ai_processor.py` | `_heuristic_repair` `default_theme` (3693) | Match new schema shape; drop `"font": "Inter"` |
| `ai_processor.py` / orchestration | `workflow_context` | Carry the project-level design system once generated (Master + Overrides pattern) |
| `angular_generator.py` | `_create_system_prompt` Design Principles (1458), line 1463 | Remove hardcoded palette instruction; consume IR theme tokens as-is |
| `angular_generator.py` | `_create_system_prompt` | Separately inject Angular stack-guideline content from `data/stacks/` |
| new | `services/design_reference/` | Vendored `data/*.csv` + `scripts/{core.py,design_system.py}` + LICENSE |

The IR schema / repair / validator row still needs to move together — a partial change there
leaves the pipeline in a worse, inconsistent state rather than just an incomplete one.
