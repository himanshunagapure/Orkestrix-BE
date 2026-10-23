# Async Workflow Build — Frontend Implementation Guide

**Audience:** Frontend developers implementing workflow **preview → execute → resume** with the new async job system.

**Why this exists:** Long sync `POST /scratch/preview` and `POST /plan/execute` (20–100+ min) get killed by browsers, proxies, and FE timeouts. Builds now run on a **worker**; FE only **enqueues** (HTTP 202) and **polls** short `/progress` calls.

| Old (avoid for production) | New (preferred) |
|----------------------------|-----------------|
| Hold HTTP open on `/scratch/preview` or `/plan/execute` | `POST /workflow/jobs` → **202** |
| Wait for one giant JSON response | Poll `POST /workflow/progress` every 3–5s |
| Disconnect = lost visibility | Job keeps running; refresh and poll again |

Sync endpoints still work for debugging. **Do not** use them for the main create-workflow UI.

**Related docs**

| Doc | Use when |
|-----|----------|
| This guide | Implement FE for async jobs + UI |
| `WORKFLOW_PROGRESS_FRONTEND_HANDOFF.md` | Field-level progress binding |
| `WORKFLOW_EXECUTE_JOBS_FRONTEND_HANDOFF.md` | Short jobs API summary |
| `PREVIEW_EXECUTE_FE_QUICK_GUIDE.md` | Payload shapes / register / sync fallback |
| `WORKFLOW_BUILDER_CREDIT_FLOW_README.md` | Estimate, credits, pause |
| `WORKFLOW_CREATION_POSTMAN.md` | Copy-paste API bodies |

---

## 1. Stable IDs and headers

```ts
// projectId === workflowId for the whole build lifecycle
const workflowId = registeredProjectId;

const AGENT_ROOT = `${import.meta.env.VITE_AGENT_API_BASE}${import.meta.env.VITE_AGENT_API_PREFIX}`;
// e.g. http://localhost:5010/dev/aiqod-agent/agent
```

Every agent call:

| Header / body | Required |
|---------------|----------|
| `Content-Type: application/json` | Yes |
| `x-subscriber-id` or `subscriberId` | Yes |
| `x-org-id` / `x-user-id` | Optional |
| `Authorization` | If gateway requires it |

---

## 2. End-to-end call order

```text
estimate → register → preview job → poll → show graph
                 ↓ user clicks “Create workflow”
            execute job → poll (bar + logs + tasks) → draft → configdetails
                 ↓ if PAUSED_CREDIT
            top up → resume job → poll again
```

| Step | Endpoint | FE action |
|------|----------|-----------|
| 1 | `POST /agentic/plan/estimate` | Show cost; block if `sufficientBalance === false` |
| 2 | `POST /agentic/workflow/register` | Persist `projectId` / `workflowId` |
| 3 | `POST /agentic/workflow/jobs` `jobType: "preview"` | Expect **202**; start poll |
| 4 | `POST /agentic/workflow/progress` | Until preview done → load graph |
| 5 | `POST /agentic/workflow/jobs` `jobType: "execute"` | Expect **202**; start poll |
| 6 | `POST /agentic/workflow/progress` | Drive execute UI until draft |
| 7 | `GET /configdetails` | Header `workflow-id`; final graphs / screens |

---

## 3. Types (copy into FE)

```typescript
type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "PAUSED_CREDIT"
  | null;

type JobType = "preview" | "execute" | "resume" | null;

interface WorkflowBuildJobCreateResponse {
  success: boolean;
  jobId: string;
  workflowId: string;
  jobType: JobType;
  status: JobStatus; // usually QUEUED
  reused: boolean;
  progressEndpoint: string;
}

interface GapNode {
  phaseId?: string;
  phaseName?: string;
  nodeId?: string;
  name?: string;
  type?: string;
  capabilityId?: string | null;
}

interface WorkflowProgress {
  success: boolean;
  found: boolean;
  workflowId: string;
  workflowName?: string | null;
  workflowStatus?: string | null; // queued | building | preview | draft | paused_credit | …
  isPaused: boolean;
  canResume: boolean;
  resumeEndpoint?: string | null;
  stoppedAt?: string | null;
  pipelineStage?: "stage0" | "preview" | "execute" | null;
  pipelineStageLabel?: string | null;

  /** Prefer for execute progress bar (0–100 during execute) */
  overallPercentComplete: number;
  stagePercentComplete?: number;
  /** Coarse full-journey % — do NOT use for execute bar */
  pipelinePercentComplete?: number;

  phaseProgress: {
    currentIndex: number;
    totalPhases: number;
    completedPhases: number;
    currentPhase?: {
      index: number;
      phaseId?: string;
      phaseName?: string;
    } | null;
    currentNodeId?: string | null;
  };

  gapProgress: {
    total: number;
    resolved: number;
    remaining: number;
    percentComplete: number;
    resolvedNodes: GapNode[];
    remainingNodes: GapNode[];
  };

  scratchProgress?: {
    phaseId?: string;
    subStage?: string;
    subStageLabel?: string;
    subStagePercent?: number;
  } | null;

  billing?: {
    predictedCost: number;
    budgetSpentUsd: number;
    capRemainingUsd: number;
    reservationStatus?: string;
  };

  jobId?: string | null;
  jobType?: JobType;
  jobStatus?: JobStatus;
  resultReady: boolean;
  jobError?: Record<string, unknown> | null;
}
```

---

## 4. Enqueue jobs (always expect 202)

### 4.1 Preview

```typescript
async function enqueuePreview(input: {
  workflowId: string;
  subscriberId: string;
  orgId?: string;
  userId?: string;
  // same body as old /scratch/preview — exactly one of prompt | steps
  payload: Record<string, unknown>;
}): Promise<WorkflowBuildJobCreateResponse> {
  const res = await fetch(`${AGENT_ROOT}/agentic/workflow/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-subscriber-id": input.subscriberId,
    },
    body: JSON.stringify({
      jobType: "preview",
      workflowId: input.workflowId,
      projectId: input.workflowId,
      subscriberId: input.subscriberId,
      orgId: input.orgId,
      userId: input.userId,
      payload: {
        ...input.payload,
        projectId: input.workflowId,
        workflowId: input.workflowId,
        subscriberId: input.subscriberId,
      },
    }),
  });
  if (res.status !== 202) {
    throw new Error(`preview job failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}
```

### 4.2 Execute

Pass **verbatim** `phaseGraphs` from preview / `configdetails` (do not rebuild).

```typescript
async function enqueueExecute(input: {
  workflowId: string;
  subscriberId: string;
  orgId?: string;
  userId?: string;
  phaseGraphs: unknown[];
}): Promise<WorkflowBuildJobCreateResponse> {
  const res = await fetch(`${AGENT_ROOT}/agentic/workflow/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-subscriber-id": input.subscriberId,
    },
    body: JSON.stringify({
      jobType: "execute",
      workflowId: input.workflowId,
      subscriberId: input.subscriberId,
      orgId: input.orgId,
      userId: input.userId,
      payload: {
        workflowId: input.workflowId,
        projectId: input.workflowId,
        subscriberId: input.subscriberId,
        orgId: input.orgId,
        userId: input.userId,
        phaseGraphs: input.phaseGraphs,
      },
    }),
  });
  if (res.status !== 202) {
    throw new Error(`execute job failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}
```

### 4.3 Resume (after credit pause)

```typescript
async function enqueueResume(input: {
  workflowId: string;
  subscriberId: string;
  orgId?: string;
  userId?: string;
}): Promise<WorkflowBuildJobCreateResponse> {
  const res = await fetch(`${AGENT_ROOT}/agentic/workflow/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-subscriber-id": input.subscriberId,
    },
    body: JSON.stringify({
      jobType: "resume",
      workflowId: input.workflowId,
      subscriberId: input.subscriberId,
      orgId: input.orgId,
      userId: input.userId,
      payload: {
        workflowId: input.workflowId,
        subscriberId: input.subscriberId,
        orgId: input.orgId,
        userId: input.userId,
      },
    }),
  });
  if (res.status !== 202) {
    throw new Error(`resume job failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}
```

**Idempotency:** Re-posting the same job type while `QUEUED`/`RUNNING` returns `reused: true` and the same `jobId`. Safe to retry enqueue on network blips.

---

## 5. Poll `/progress` (core pattern)

Polling is **not** the same as holding a long HTTP call:

- Each poll is a short request (&lt;1s).
- If the user closes the tab, the **job keeps running** on the worker.
- On reopen, poll again with the same `workflowId` — you catch up.

```typescript
const TERMINAL: JobStatus[] = ["COMPLETED", "FAILED", "PAUSED_CREDIT"];

async function fetchProgress(
  workflowId: string,
  subscriberId: string,
  orgId?: string,
  userId?: string,
): Promise<WorkflowProgress> {
  const res = await fetch(`${AGENT_ROOT}/agentic/workflow/progress`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-subscriber-id": subscriberId,
    },
    body: JSON.stringify({ workflowId, subscriberId, orgId, userId }),
  });
  if (!res.ok) throw new Error(`progress ${res.status}`);
  return res.json();
}

function startProgressPoll(opts: {
  workflowId: string;
  subscriberId: string;
  orgId?: string;
  userId?: string;
  intervalMs?: number;
  onTick: (p: WorkflowProgress) => void;
  onTerminal: (p: WorkflowProgress) => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const loop = async () => {
    if (stopped) return;
    try {
      const p = await fetchProgress(
        opts.workflowId,
        opts.subscriberId,
        opts.orgId,
        opts.userId,
      );
      opts.onTick(p);
      if (p.jobStatus && TERMINAL.includes(p.jobStatus)) {
        opts.onTerminal(p);
        return;
      }
    } catch (e) {
      // Network blip: keep polling; do not cancel the build
      console.warn("progress poll failed", e);
    }
    timer = setTimeout(loop, opts.intervalMs ?? 4000);
  };

  void loop();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}
```

**Also poll on:**

- Page mount / route enter (recover after refresh)
- Window `visibilitychange` when document becomes visible again
- After enqueue returns 202

**Stop polling when:** `jobStatus` is terminal **or** user leaves the flow (unmount cleanup). Leaving the page does **not** stop the backend job.

---

## 6. Wire your screens

### 6.1 Preview UI (spinner → graph)

| UI | Bind |
|----|------|
| Spinner | `jobStatus` ∈ `QUEUED` \| `RUNNING` |
| Status text (optional) | `stoppedAt` |
| Show graph | `jobStatus === "COMPLETED"` and `workflowStatus === "preview"` (or `resultReady`) |
| Load graphs | `GET {AGENT_ROOT}/configdetails` with header `workflow-id: {workflowId}` → `agentic_process.phaseGraphs` |
| Error | `jobStatus === "FAILED"` → show `jobError` |
| Credits | `jobStatus === "PAUSED_CREDIT"` → recharge UI |

Do **not** drive preview with a percent bar — spinner only.

```typescript
async function loadPhaseGraphs(workflowId: string) {
  const res = await fetch(`${AGENT_ROOT}/configdetails`, {
    headers: { "workflow-id": workflowId },
  });
  const doc = await res.json();
  return doc?.agentic_process?.phaseGraphs ?? [];
}
```

### 6.2 Execute UI (after “Create workflow”)

#### Progress bar (0 → 100%)

```typescript
function executeBarPercent(p: WorkflowProgress): number {
  return (
    p.gapProgress?.percentComplete ??
    p.overallPercentComplete ??
    0
  );
}
```

Do **not** use `pipelinePercentComplete` for this bar (coarse full-journey metric).

#### Live logs (client-side)

There is **no** server log stream for build steps. Append when fields change:

```typescript
type LogLine = { at: string; text: string };

function appendProgressLogs(
  prev: WorkflowProgress | null,
  next: WorkflowProgress,
  logs: LogLine[],
): LogLine[] {
  const out = [...logs];
  const push = (text: string) => {
    if (!text) return;
    if (out.length && out[out.length - 1].text === text) return;
    out.push({ at: new Date().toISOString(), text });
  };

  if (!prev || prev.stoppedAt !== next.stoppedAt) {
    push(next.stoppedAt ?? "");
  }
  if (next.phaseProgress?.currentNodeId &&
      prev?.phaseProgress?.currentNodeId !== next.phaseProgress.currentNodeId) {
    push(`Generating: ${next.phaseProgress.currentNodeId}`);
  }
  return out;
}
```

Optional SSE `GET /agentic/workflow/stream/{workflowId}` only signals terminal events — not a replacement for the log panel.

#### Task list (phases / nodes, tick when done)

```typescript
function buildTaskList(p: WorkflowProgress) {
  const done = (p.gapProgress?.resolvedNodes ?? []).map((n) => ({
    ...n,
    done: true as const,
  }));
  const pending = (p.gapProgress?.remainingNodes ?? []).map((n) => ({
    ...n,
    done: false as const,
  }));
  const tasks = [...done, ...pending];

  // Group by phase for tree UI
  const byPhase = new Map<string, typeof tasks>();
  for (const t of tasks) {
    const key = t.phaseId ?? "unknown";
    if (!byPhase.has(key)) byPhase.set(key, []);
    byPhase.get(key)!.push(t);
  }
  return { tasks, byPhase, currentNodeId: p.phaseProgress?.currentNodeId };
}
```

Highlight the row where `nodeId === phaseProgress.currentNodeId`.

#### Execute complete

```typescript
function isExecuteDone(p: WorkflowProgress): boolean {
  return (
    p.jobStatus === "COMPLETED" &&
    (p.workflowStatus === "draft" || p.resultReady === true)
  );
}
// Then reload configdetails for final graphs / screen URLs
```

---

## 7. Credit pause → resume

```typescript
function handleProgressTerminal(p: WorkflowProgress) {
  if (p.jobStatus === "FAILED") {
    showError(p.jobError);
    return;
  }
  if (p.jobStatus === "PAUSED_CREDIT" || p.isPaused) {
    showRechargeUI({
      onToppedUp: async () => {
        await enqueueResume({ workflowId, subscriberId, orgId, userId });
        startProgressPoll({ /* … */ });
      },
    });
    return;
  }
  if (p.jobType === "preview" && p.jobStatus === "COMPLETED") {
    void showPreviewGraph();
  }
  if (p.jobType === "execute" && isExecuteDone(p)) {
    void showDraftWorkflow();
  }
}
```

Sync `POST /workflow/resume` still exists (holds HTTP until done). Prefer **async resume job** for the same reasons as preview/execute.

---

## 8. Suggested React/Vue flow (pseudocode)

```typescript
// Preview screen
const stopPreviewPoll = startProgressPoll({
  workflowId,
  subscriberId,
  onTick: (p) => setProgress(p),
  onTerminal: async (p) => {
    if (p.jobStatus === "COMPLETED") {
      const graphs = await loadPhaseGraphs(workflowId);
      setPhaseGraphs(graphs);
      setPreviewReady(true);
    } else {
      handleProgressTerminal(p);
    }
  },
});

// User clicks Create workflow
await enqueueExecute({ workflowId, subscriberId, phaseGraphs });
const logs: LogLine[] = [];
let prev: WorkflowProgress | null = null;

const stopExecutePoll = startProgressPoll({
  workflowId,
  subscriberId,
  onTick: (p) => {
    setBar(executeBarPercent(p));
    setTasks(buildTaskList(p));
    setLogs(appendProgressLogs(prev, p, logs));
    prev = p;
  },
  onTerminal: handleProgressTerminal,
});

// onUnmount: stopPreviewPoll(); stopExecutePoll();
```

---

## 9. Migration checklist (from sync FE)

- [ ] Remove long-timeout `fetch` / axios for `/scratch/preview` and `/plan/execute` from the main path
- [ ] Enqueue via `/workflow/jobs`; treat **202** as success
- [ ] Poll `/progress` 3–5s; cleanup on unmount
- [ ] Recover on page load: if `jobStatus` is QUEUED/RUNNING, resume polling (do not re-enqueue unless needed)
- [ ] Preview: spinner until COMPLETED → `configdetails` for graphs
- [ ] Execute: bar = `gapProgress.percentComplete`; logs from `stoppedAt` diffs; tasks from `resolvedNodes` / `remainingNodes`
- [ ] Handle `PAUSED_CREDIT` → top-up → `jobType: "resume"`
- [ ] Do not AbortController-cancel thinking it stops the build — it only stops the poll
- [ ] Keep `workflowId === projectId` stable from register onward

---

## 10. Common mistakes

| Mistake | Fix |
|---------|-----|
| Waiting for job `result` to contain full `phaseGraphs` | Job result is compact; use `configdetails` |
| Using sync execute because “progress looks stuck at 0” | Poll; early execute can be 0% until first gap resolves |
| Re-enqueue execute on every poll failure | Keep polling; only enqueue once (idempotent reuse if still active) |
| Binding execute bar to `pipelinePercentComplete` | Use `gapProgress.percentComplete` |
| Expecting a live LLM log API | Client-side append on field change |
| New `workflowId` mid-flow | Breaks jobs, checkpoints, and config |

---

## 11. Acceptance criteria (FE QA)

1. Preview: enqueue returns in &lt;2s; spinner shows; graph appears without holding a long POST.
2. Refresh mid-preview: spinner/progress resumes from `/progress` (no duplicate broken state).
3. Execute: bar moves 0→100; task rows tick; headline/`stoppedAt` updates.
4. Refresh mid-execute: bar/tasks catch up; build still finishes.
5. Close tab mid-execute, reopen later: `jobStatus` eventually `COMPLETED` / `draft`.
6. `PAUSED_CREDIT`: recharge + resume job → poll continues to completion.
