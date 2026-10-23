# `/workflow/progress` — Frontend handoff

**Full FE implementation (jobs + poll + screens):** `WORKFLOW_ASYNC_BUILD_FRONTEND_GUIDE.md`

**Endpoint:**
```http
POST {AGENT_ROOT}/agentic/workflow/progress
Content-Type: application/json

{ "workflowId": "…", "subscriberId": "…", "orgId": "…", "userId": "…" }
```

Poll every **3–5s** while `jobStatus` is `QUEUED` / `RUNNING` (async jobs) or while `workflowStatus === "building"`.

---

## FE UI → backend field map

### 1. Preview screen (spinner → show graph)

| UI | Bind to | Notes |
|----|---------|--------|
| Show spinner | `jobStatus` in `QUEUED` \| `RUNNING` **or** `workflowStatus` not yet `preview` | Prefer `jobType === "preview"` |
| Hide spinner / show graph | `jobStatus === "COMPLETED"` **and** `workflowStatus === "preview"` (or `resultReady === true`) | Then `GET /configdetails` (`workflow-id` header) for `agentic_process.phaseGraphs` |
| Optional status text | `stoppedAt`, `scratchProgress.subStageLabel` | Preview is coarse; spinner is enough |
| Fail / pause | `jobStatus === "FAILED"` → `jobError`; `PAUSED_CREDIT` → resume | |

**Do not** drive preview UI from `overallPercentComplete` — preview is spinner-only.

---

### 2. Execute screen (after “Create workflow”)

#### 2.1 Horizontal progress bar (0 → 100%)

| Prefer | Field |
|--------|--------|
| **Primary** | `gapProgress.percentComplete` |
| **Also OK** | `overallPercentComplete` (now = gap % during execute) |
| Alias | `stagePercentComplete` (same during execute) |

Avoid `pipelinePercentComplete` for this bar — that is the old coarse full-journey metric (~75→100).

```ts
const barPct =
  progress.gapProgress?.percentComplete ??
  progress.overallPercentComplete ??
  0;
```

Complete when: `jobStatus === "COMPLETED"` and `workflowStatus === "draft"` (then load `configdetails`).

#### 2.2 Live “what’s happening” text / log-like feed

| Source | Use |
|--------|-----|
| `stoppedAt` | Headline each poll (e.g. `In progress — generating 'extract_invoice_data'`) |
| `phaseProgress.currentNodeId` | Current node id |
| `phaseProgress.currentPhase.phaseName` | Current phase name |
| `scratchProgress` | Preview only |
| `billing.budgetSpentUsd` | Optional “credits used” line |

**There is no dedicated live log buffer API.** Append a line to a client-side log when `stoppedAt` or `currentNodeId` **changes** between polls:

```ts
if (prev.stoppedAt !== next.stoppedAt) {
  logs.push({ at: new Date().toISOString(), text: next.stoppedAt });
}
```

Optional SSE `GET /agentic/workflow/stream/{workflowId}` only pushes terminal events (`completed` / `paused_credit` / `error`) — **not** a full log stream. Do not rely on it for the live log panel.

#### 2.3 Task list (phases + substeps, tick as done)

| Bind to | How |
|---------|-----|
| Pending tasks | `gapProgress.remainingNodes[]` |
| Done tasks | `gapProgress.resolvedNodes[]` |
| Each item | `{ phaseId, phaseName, nodeId, name, type, capabilityId }` |
| Current highlight | `phaseProgress.currentNodeId` |
| Phase rollup | `phaseProgress.completedPhases` / `totalPhases` |

```ts
// Tick = node appears in resolvedNodes (capability no longer GENERATE:*)
const tasks = [
  ...progress.gapProgress.resolvedNodes.map(n => ({ ...n, done: true })),
  ...progress.gapProgress.remainingNodes.map(n => ({ ...n, done: false })),
];
// Group by phaseId / phaseName for the tree UI
```

Initial task list can also be seeded from preview `phaseGraphs` nodes (ui_screen / tool), then flipped to done via `resolvedNodes`.

---

## Percent fields (after fix)

| Field | Meaning |
|-------|---------|
| `overallPercentComplete` | **FE bar default.** During **execute** = gap resolution **0–100**. During **preview** = scratch sub-stage 0–100. `draft` = 100. |
| `stagePercentComplete` | Always 0–100 within current `pipelineStage` |
| `pipelinePercentComplete` | Coarse full pipeline (register→draft); optional journey UI only |
| `gapProgress.percentComplete` | Screens/tools resolved / total — **best for execute bar + task math** |

---

## Example mid-execute response (shape)

```json
{
  "success": true,
  "found": true,
  "workflowId": "test47_sb_…",
  "workflowStatus": "building",
  "pipelineStage": "execute",
  "pipelineStageLabel": "Gap resolution and code generation (execute)",
  "stoppedAt": "In progress — generating 'extract_invoice_data'",
  "overallPercentComplete": 33.3,
  "stagePercentComplete": 33.3,
  "pipelinePercentComplete": 83.3,
  "phaseProgress": {
    "currentIndex": 0,
    "totalPhases": 1,
    "completedPhases": 0,
    "currentPhase": { "index": 0, "phaseId": "phase_1", "phaseName": "Invoice Intake" },
    "currentNodeId": "extract_invoice_data"
  },
  "gapProgress": {
    "total": 3,
    "resolved": 1,
    "remaining": 2,
    "percentComplete": 33.3,
    "resolvedNodes": [{ "phaseId": "phase_1", "nodeId": "upload_invoice", "name": "Upload Invoice", "type": "ui_screen" }],
    "remainingNodes": [{ "phaseId": "phase_1", "nodeId": "extract_invoice_data", "capabilityId": "GENERATE:…" }]
  },
  "jobId": "job_…",
  "jobType": "execute",
  "jobStatus": "RUNNING",
  "resultReady": false,
  "billing": { "predictedCost": 0.5, "budgetSpentUsd": 0.12 }
}
```

---

## Async job fields

| Field | Use |
|-------|-----|
| `jobId` / `jobType` / `jobStatus` | Primary async lifecycle |
| `resultReady` | `true` when job `COMPLETED` |
| `jobError` | On `FAILED` / credit pause detail |

`jobStatus`: `QUEUED` → `RUNNING` → `COMPLETED` \| `FAILED` \| `PAUSED_CREDIT`

---

## What is **not** supported today

| FE wish | Status |
|---------|--------|
| Streaming token/log lines from LLM | Not available — poll + append on field change |
| Task list during **preview** at sub-step granularity | Only `scratchProgress.subStage*` (coarse); full node list appears after preview completes |
| Separate `/jobs/{id}` poll | Intentionally not added — use this endpoint |

---

## Quick FE checklist

- [ ] Preview: spinner on `jobStatus` QUEUED/RUNNING; graph on COMPLETED + `configdetails`
- [ ] Execute bar: `gapProgress.percentComplete` (or `overallPercentComplete`)
- [ ] Live log: append when `stoppedAt` / `currentNodeId` changes
- [ ] Task list: `resolvedNodes` + `remainingNodes`, group by `phaseId`
- [ ] Done: `workflowStatus === "draft"` + `jobStatus === "COMPLETED"`
- [ ] Pause: `jobStatus === "PAUSED_CREDIT"` or `isPaused` → resume job / `/workflow/resume`
