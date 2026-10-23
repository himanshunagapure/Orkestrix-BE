# Natural Language Workflow Builder — Implementation Tasks

- `[x]` **1. Directory Setup & Data Models**
  - `[x]` Create `backend/nl_builder/` module directory and `__init__.py`
  - `[x]` Create `backend/nl_builder/models.py` with `NLPlanRequest`, `NLPlanReviseRequest`, `NLPlanResponse`, `NLPlanPreview`, `NLPhaseSummary`, and `NLNodeSummary`

- `[x]` **2. Core Plan Generator (`backend/nl_builder/plan_generator.py`)**
  - `[x]` Implement Stage 0 session context extractor
  - `[x]` Connect requirement analyzer (`analyze_requirements_with_llm`) kept for `/requirements/analyze` only
  - `[x]` Expanded enterprise steps (from Stage 0 session) into `plan_phase_graph_scratch()` (full pipeline)
  - `[x]` Implement LLM-based `generate_plan_summary()` for UI preview rendering
  - `[x]` Wire cost estimation (`estimate_workflow_cost`) based on generated node counts
  - `[x]` Implement `generate_nl_plan()` orchestrator function

- `[x]` **3. Plan Reviser (`backend/nl_builder/plan_reviser.py`)**
  - `[x]` Implement `classify_feedback()` to categorize user prompt feedback into `structural` or `detail`
  - `[x]` Implement `revise_plan_structural()` (re-runs full scratch pipeline with `requirement_override`)
  - `[x]` Implement `revise_plan_detail()` (surgically patches `generationDescription` and node parameters in existing `phaseGraphs`)
  - `[x]` Implement `revise_nl_plan()` orchestrator function

- `[x]` **4. API Router & Integration (`backend/nl_builder/router.py`)**
  - `[x]` Create `nl_router` FastAPI APIRouter
  - `[x]` Add `POST /aiqod-agent/agent/agentic/nl/plan` endpoint
  - `[x]` Add `POST /aiqod-agent/agent/agentic/nl/plan/revise` endpoint
  - `[x]` Mount `nl_router` in `backend/agent_new.py`

- `[x]` **5. Testing & Verification**
  - `[x]` Create unit tests for data models (`test_models.py`)
  - `[x]` Create unit/integration tests for plan generator (`test_plan_generator.py`)
  - `[x]` Create unit/integration tests for plan reviser (`test_plan_reviser.py`)
  - `[x]` Test full end-to-end flow from Stage 0 clarification through plan generation, revision, and execution

- `[x]` **6. Prompt-mode: expand → Stage 0 clarify → Stage 1 skeleton**
  - `[x]` LLM expands NL into enterprise logical steps before Stage 0 (not the fewest steps)
  - `[x]` Stage 0 clarifies those steps (credentials, collections, review vs auto)
  - `[x]` `/scratch/preview` with `{prompt, stage0Handoff}` feeds expanded steps to Stage 1 (same endpoint as steps mode)
  - `[x]` `/nl/plan` kept as thin alias returning `NLPlanResponse`
  - `[x]` Structural revise re-expands via `requirement_override` then Stage 1
  - `[x]` Fallback: one FREE blob if expansion returns fewer than 6 steps

- `[x]` **7. Standalone expand + step revise APIs**
  - `[x]` `POST /nl/expand` — prompt → enterprise steps
  - `[x]` `POST /nl/steps/revise` — NL feedback on the step list (not `/nl/plan/revise`)
  - `[x]` `/stage0/start` accepts `expandedSteps` so expander is not run twice
