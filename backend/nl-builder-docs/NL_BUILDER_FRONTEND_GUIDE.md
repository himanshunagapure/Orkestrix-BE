# NL Builder — Frontend Integration Guide (Prompt Mode)

How to wire the **natural-language → enterprise steps → clarify → plan** flow in the UI.

Architecture / backend details: [`NL_BUILDER_README.md`](./NL_BUILDER_README.md).

---

## Base URL

All paths below are under the agent API prefix (local default):

```text
http://127.0.0.1:5010/dev/aiqod-agent/agent/...
```

Use the same org / subscriber / user headers (or body fields) your app already sends for agentic APIs:

| Field | Typical source |
|-------|----------------|
| `projectId` | New or existing workflow project id |
| `orgId` | Tenant org |
| `subscriberId` | Billing subscriber |
| `userId` | Current user |

`Content-Type: application/json`

---

## Mental model

```text
User prompt
    │
    ▼
┌──────────────────────────────────────┐
│  Optional: /nl/expand/estimate       │  ← show cost of one expand call
│  POST /nl/expand                     │
│  Step editor (no graph)              │
│  Optional: /nl/revise/estimate       │  ← show cost of each revise call
│  POST /nl/steps/revise (loop)        │  ← FE holds latest steps; no sessionId
└──────────────────┬───────────────────┘
                   │ user confirms steps
                   ▼
┌──────────────────────────────────────┐
│  /plan/estimate {prompt,expandedSteps}│ ← Stage 0 + graph + execute hold
│  /workflow/register                   │
└──────────────────┬───────────────────┘
                   ▼
┌──────────────────────────────────────┐
│  Clarification Q&A                    │  ← /stage0/start + /stage0/respond*
│  sessionId required                   │     Pass confirmed steps as expandedSteps
└──────────────────┬───────────────────┘
                   ▼
┌──────────────────────────────────────┐
│  Graph preview                        │  ← /scratch/preview (no sessionId)
│  then execute                         │  ← /plan/execute
└──────────────────────────────────────┘
```

**Two different “revise” endpoints — do not mix them up:**

| Endpoint | When | What it edits |
|----------|------|----------------|
| `/agentic/nl/steps/revise` | Before Stage 0 | The **step list** the user is reviewing |
| `/agentic/nl/plan/revise` | After preview | The **LangGraph** (nodes/edges), needs stored plan |

**Three different “estimate” endpoints:**

| Endpoint | What it prices |
|----------|----------------|
| `/nl/expand/estimate` | One expand Gemini call |
| `/nl/revise/estimate` | One step-revise Gemini call |
| `/plan/estimate` | Remaining workflow: Stage 0 + scratch + execute |

---

## Recommended FE state

Keep this in component/store state (not the server) until Stage 0:

```ts
type LogicalStep = {
  stepId: string;           // "L1", "L2", ...
  name: string;
  description: string;
  preferredKind?: "ui_screen" | "tool" | "agent" | null;
  // Present on the type for compatibility; prompt-mode expand/revise
  // return empty arrays. Do not show or edit these in the step UI.
  inputs?: string[];
  outputs?: string[];
};

type PromptModeState = {
  prompt: string;                 // original NL text (immutable until restart)
  steps: LogicalStep[];           // latest list from expand / revise
  stepCount: number;
  warnings: string[];
  expandCallCost?: number;        // from /nl/expand/estimate
  reviseCallCost?: number;        // from latest /nl/revise/estimate
  // After /plan/estimate + register
  predictedCost?: number;         // Stage 0 + preview + execute hold
  // After stage0/start
  sessionId?: string;
  // After stage0 complete
  requirementSummary?: unknown;   // Stage0RequirementSummary
  // After preview
  phaseGraphs?: unknown[];
  estimatedExecuteCost?: number;  // refined remaining execute
};
```

UI phases to gate on:

1. `editing_steps` — show list + revise box + optional per-call LLM costs + “Looks good”
2. `estimating` / `registering` — full-pipeline `/plan/estimate` then register
3. `clarifying` — MCQ / free-text from Stage 0
4. `previewing` — long-running; show progress
5. `plan_ready` — show graph + refined execute cost + Confirm execute

---

## End-to-end call order

### 0. Estimate expand LLM cost

`POST /aiqod-agent/agent/agentic/nl/expand/estimate`

Same body as `/nl/expand`. No Gemini call. Show `predictedCost` before the user confirms expand.

```json
{
  "prompt": "Build a production-ready invoice processing and AP approval workflow",
  "projectId": "<projectId>",
  "subscriberId": "<subscriberId>"
}
```

```json
{
  "operation": "expand",
  "endpoint": "/aiqod-agent/agent/agentic/nl/expand",
  "model": "gemini-3.1-pro-preview",
  "predictedCost": 0.08,
  "estimatedCostUsd": 0.057,
  "inputTokens": 3517,
  "outputTokens": 4200,
  "totalTokens": 7717,
  "source": "default",
  "sampleCount": 0,
  "observedAvgCostUsd": null
}
```

`source` is `observed` when `llm_token_count` has ≥3 successful expand calls, `blended` with fewer samples, else `default`.

This cost is **not** part of `/workflow/register` — it is charged when `/nl/expand` actually runs.

---

### 1. Expand — first step list

`POST /aiqod-agent/agent/agentic/nl/expand`

```json
{
  "prompt": "Build a production-ready invoice processing and AP approval workflow",
  "projectId": "<projectId>",
  "orgId": "<orgId>",
  "subscriberId": "<subscriberId>",
  "userId": "<userId>"
}
```

**Response (`NLStepsResponse`):**

```json
{
  "prompt": "...",
  "steps": [
    {
      "stepId": "L1",
      "name": "Ingest Invoice Document",
      "description": "...",
      "preferredKind": null,
      "inputs": [],
      "outputs": []
    }
  ],
  "stepCount": 14,
  "source": "llm",
  "warnings": []
}
```

**FE actions:**

- Replace `state.steps` with `response.steps`.
- Render a readable list (`stepId`, `name`, `description`). Optionally badge `preferredKind` when non-null (`ui_screen` / `tool`).
- Do **not** show `inputs` / `outputs` — expander no longer invents them; Stage 2 of the planner owns I/O.
- Expect roughly **8–18** steps. Empty / failed expand → `422` with `enterprise_expand_empty` (or `503` if Gemini key missing). Surface `warnings`.

---

### 1b. Estimate revise LLM cost (each round)

`POST /aiqod-agent/agent/agentic/nl/revise/estimate`  
(alias: `POST /aiqod-agent/agent/agentic/nl/steps/revise/estimate`)

Same body as `/nl/steps/revise`. Call before each revise so the user sees that round’s LLM cost. Same response shape as expand estimate (`operation: "revise"`).

---

### 2. Revise steps — back-and-forth until satisfied

`POST /aiqod-agent/agent/agentic/nl/steps/revise`

**Stateless.** There is no `sessionId`. Always send:

- the **same original** `prompt`
- the **full latest** `steps` array (not a delta / not only changed rows)
- this round’s `feedback` string

```json
{
  "prompt": "Build a production-ready invoice processing and AP approval workflow",
  "feedback": "Add a 3-way match exception queue before department approval.",
  "steps": [ /* entire current state.steps */ ],
  "projectId": "<projectId>",
  "orgId": "<orgId>",
  "subscriberId": "<subscriberId>",
  "userId": "<userId>"
}
```

**Response:** same shape as expand, plus echoed `feedback`.

**FE loop:**

```text
show steps
  → (optional) POST /nl/revise/estimate
  → user types feedback
  → POST /nl/steps/revise
  → replace steps
  → repeat
```

Stop when the user clicks **Continue / Looks good**. That confirmed list is what you pass as `expandedSteps` later.

Timeouts: expand/revise are LLM calls (~20–60s). Disable the revise button while in flight; keep the previous list on screen until the new response arrives (optimistic replace only after 200).

---

### 3. Estimate cost (full remaining workflow)

`POST /aiqod-agent/agent/agentic/plan/estimate`

Send the **confirmed** expander steps. Same estimator as steps mode. **`expandedSteps` is required** in prompt mode (prompt-only → 400).

```json
{
  "prompt": "<same prompt>",
  "expandedSteps": [ /* confirmed state.steps */ ],
  "projectId": "<projectId>",
  "orgId": "<orgId>",
  "subscriberId": "<subscriberId>",
  "userId": "<userId>",
  "selectedName": "Invoice AP Approval Workflow"
}
```

**Important fields:**

| Field | Meaning |
|-------|---------|
| `predictedCost` | Hold for **Stage 0 + scratch graph + execute/gap codegen** (× safety buffer) |
| `stage0CostUsd` | Clarification turns |
| `previewCostUsd` | LangGraph definition (skeleton, descriptions, schemas, routers) |
| `executeCostUsd` | Gap codegen for GENERATE UI screens and tools (**> 0**) |
| `uiScreenCount` / `toolCount` | Topology inferred from `preferredKind` (`ui_screen`/`tool` lock 1 node; `null` may split) |

Show `predictedCost` before register. Expand/revise LLM costs are already spent and are **not** in this hold.

---

### 4. Register workflow (required before Stage 0)

`POST /aiqod-agent/agent/agentic/workflow/register`

```json
{
  "type": "workflow",
  "projectId": "<projectId>",
  "subscriberId": "<subscriberId>",
  "orgId": "<orgId>",
  "userId": "<userId>",
  "predictedCost": 4.09,
  "workflowName": "Invoice AP Approval Workflow",
  "selectedName": "Invoice AP Approval Workflow",
  "selectedType": "automation",
  "selectedCategory": "finance",
  "reserveSubscriberCredits": false
}
```

Use the `predictedCost` from `/plan/estimate` (full pipeline). If you skip register, `/stage0/start` returns `WORKFLOW_NOT_REGISTERED`.

---

### 5. Start clarification — pass the confirmed steps

`POST /aiqod-agent/agent/stage0/start`

```json
{
  "prompt": "<same prompt>",
  "mode": "prompt",
  "expandedSteps": [ /* confirmed state.steps — full list */ ],
  "projectId": "<projectId>",
  "orgId": "<orgId>",
  "subscriberId": "<subscriberId>",
  "userId": "<userId>",
  "selectedName": "Invoice AP Approval Workflow",
  "selectedType": "automation",
  "selectedCategory": "finance"
}
```

When `expandedSteps` is present, the backend **skips** re-expanding and clarifies **those** steps.

**Response (`Stage0Response`):**

- `sessionId` — **keep this**; only Stage 0 respond uses it
- `status`: `"needs_input"` | `"complete"`
- `questions[]` — show when `needs_input`
- `expandedSteps` — echo of enterprise steps
- `requirementSummary` — present when `complete`

**FE:** store `sessionId`. Render the first question (and options if MCQ).

---

### 6. Answer clarifications until complete

`POST /aiqod-agent/agent/stage0/respond`

```json
{
  "sessionId": "stage0_...",
  "answer": "Google Gemini",
  "skipRemaining": false
}
```

Loop while `status === "needs_input"`:

1. Take `questions[0]` (prompt / options).
2. Collect user answer → POST respond.
3. Replace UI with the new response.

When `status === "complete"`:

- Persist `requirementSummary` from the response (needed for preview).
- Prefer `response.requirementSummary` as the handoff payload — it includes `steps` plus clarifications (`planner_handoff`, secrets, DB schemas, etc.).

Optional: `skipRemaining: true` to force completion with defaults (use sparingly; product decision).

---

### 7. Plan graph — scratch preview

`POST /aiqod-agent/agent/agentic/plan/scratch/preview`

**Do not send `sessionId`.** Pass the Stage 0 handoff:

```json
{
  "prompt": "<same prompt>",
  "mode": "prompt",
  "projectId": "<projectId>",
  "orgId": "<orgId>",
  "subscriberId": "<subscriberId>",
  "userId": "<userId>",
  "selectedName": "Invoice AP Approval Workflow",
  "selectedType": "automation",
  "selectedCategory": "finance",
  "stage0Handoff": {
    "requirementSummary": { /* exact object from stage0 complete */ }
  }
}
```

This call is **long** (often 5–10+ minutes). Use a dedicated loading state; do not time out the HTTP client too aggressively (recommend ≥ 15 minutes).

**Response highlights:**

| Field | Use in UI |
|-------|-----------|
| `phaseGraphs` | Graph canvas / hand off to `/plan/execute` |
| `steps` | Logical steps used for planning |
| `estimatedCost` | **Remaining execute** cost (GENERATE ui/tool counts) |
| `costEstimate` | Breakdown (`estimateKind: topology_execute`, ui/tool counts) |
| `warnings` | Show non-fatally |

Display something like:

> Workflow hold was ~$X.XX (Stage 0 + plan + execute). After planning, remaining execute is about **$Y.YY**.

Product/billing may raise the reservation if `estimatedCost` exceeds the pre-plan execute slice.

---

### 8. Execute (existing)

`POST /aiqod-agent/agent/agentic/plan/execute`

Same as steps mode: send the preview `phaseGraphs` (and whatever your existing execute contract already requires). Prompt mode does not change this shape.

---

## UX checklist

- [ ] Optional: show `/nl/expand/estimate` cost before expand
- [ ] Step editor screen before full-pipeline cost / clarification UI
- [ ] Optional: show `/nl/revise/estimate` before each revise
- [ ] Revise is a chat/feedback box over the **current list**, not a graph editor
- [ ] Always POST full `steps` on revise
- [ ] “Looks good” is an explicit user action before `/plan/estimate`
- [ ] `/plan/estimate` sends `{prompt, expandedSteps}` (not prompt alone)
- [ ] Register before Stage 0 using full-pipeline `predictedCost`
- [ ] Pass `expandedSteps` into `/stage0/start`
- [ ] Keep `sessionId` only for `/stage0/respond`
- [ ] Preview uses `stage0Handoff.requirementSummary`, not `sessionId`
- [ ] Hide / ignore step `inputs`/`outputs` in the step editor
- [ ] Distinguish **step revise** vs **plan revise**, and **call-cost estimate** vs **workflow estimate**
- [ ] Long timeouts + cancel UX for expand, revise, Stage 0 turns, and especially preview

---

## Error handling (prompt mode)

| HTTP | Where | FE handling |
|------|-------|-------------|
| `422` `enterprise_expand_empty` | `/nl/expand` | Ask user to rephrase prompt; do not continue |
| `503` Gemini key missing | expand / revise | Config / ops error banner |
| `400` missing `expandedSteps` | `/plan/estimate` | Send confirmed steps from expand/revise |
| `WORKFLOW_NOT_REGISTERED` | `/stage0/start` | Call `/plan/estimate` + register first |
| `503` `scratch_graph_planner_unavailable` | preview | Retryable planner failure; keep Stage 0 summary and offer “Retry plan” |
| `4xx` validation | any | Show `detail` / `message` |

On revise failure, keep the previous `state.steps` visible; do not clear the list.

---

## Minimal TypeScript sequence (sketch)

```ts
async function runPromptMode(prompt: string, tenant: Tenant, meta: WorkflowMeta) {
  // 0) Optional: expand call cost
  const expandEst = await post("/aiqod-agent/agent/agentic/nl/expand/estimate", {
    prompt, ...tenant,
  });
  // show expandEst.predictedCost

  // 1) Expand
  let { steps } = await post("/aiqod-agent/agent/agentic/nl/expand", {
    prompt, ...tenant,
  });

  // 2) FE loop: show steps; on feedback:
  // const revEst = await post(".../nl/revise/estimate", { prompt, steps, feedback, ...tenant });
  // steps = (await post(".../nl/steps/revise", { prompt, steps, feedback, ...tenant })).steps;
  // until user confirms → confirmedSteps = steps

  // 3) Full remaining workflow estimate (requires expandedSteps)
  const est = await post("/aiqod-agent/agent/agentic/plan/estimate", {
    prompt,
    expandedSteps: confirmedSteps,
    selectedName: meta.selectedName,
    ...tenant,
  });

  // 4) Register
  await post("/aiqod-agent/agent/agentic/workflow/register", {
    type: "workflow",
    predictedCost: est.predictedCost,
    workflowName: meta.selectedName,
    ...meta, ...tenant,
    reserveSubscriberCredits: false,
  });

  // 5) Stage 0
  let s0 = await post("/aiqod-agent/agent/stage0/start", {
    prompt,
    mode: "prompt",
    expandedSteps: confirmedSteps,
    ...meta, ...tenant,
  });

  while (s0.status === "needs_input") {
    const answer = await askUser(s0.questions?.[0]);
    s0 = await post("/aiqod-agent/agent/stage0/respond", {
      sessionId: s0.sessionId,
      answer,
      skipRemaining: false,
    });
  }

  // 6) Preview (no sessionId)
  const preview = await post(
    "/aiqod-agent/agent/agentic/plan/scratch/preview",
    {
      prompt,
      mode: "prompt",
      stage0Handoff: { requirementSummary: s0.requirementSummary },
      ...meta, ...tenant,
    },
    { timeoutMs: 900_000 },
  );

  // 7) Show preview.phaseGraphs + preview.estimatedCost → execute
  return preview;
}
```

---

## What not to do

1. **Do not** call `/nl/plan/revise` to change the step list before Stage 0.
2. **Do not** send only changed steps on `/nl/steps/revise`.
3. **Do not** omit `expandedSteps` on `/stage0/start` after the user edited steps (server would re-expand from the raw prompt and discard edits).
4. **Do not** put `sessionId` on `/scratch/preview`.
5. **Do not** call `/plan/estimate` with only `{prompt}` — send `expandedSteps` (required; otherwise 400).
6. **Do not** treat `/nl/expand/estimate` or `/nl/revise/estimate` as the workflow hold — those are single-call LLM costs only.
7. **Do not** build a step I/O editor for prompt mode — I/O bindings are planned in scratch Stage 2.

---

## Related docs

- Backend architecture: [`NL_BUILDER_README.md`](./NL_BUILDER_README.md)
- Live request/response sample: `backend/nl_builder/e2e_results/*/ALL_APIS_request_response.json`
