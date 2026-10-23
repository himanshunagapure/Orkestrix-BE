# Preview & Execute — Frontend Quick Guide

**Audience:** Frontend engineers wiring preview and execute.

**Use this doc for:** timeouts, request shape, and “Network tab shows nothing / request cancelled” issues.

**Preferred for long builds:** enqueue via `POST /agentic/workflow/jobs` and poll `POST /agentic/workflow/progress`.  
**Implement here:** `WORKFLOW_ASYNC_BUILD_FRONTEND_GUIDE.md` · short summary: `WORKFLOW_EXECUTE_JOBS_FRONTEND_HANDOFF.md`  
**Sync paths below** remain fully supported for short runs / debugging.

**Full integration (canvas, Stage 0, runtime):** `WORKFLOW_BUILDER_BACKEND_FRONTEND_README.md`  
**Credit / register / pause-resume:** `WORKFLOW_BUILDER_CREDIT_FLOW_README.md`

---

## 1. TL;DR — call order (do not skip)

| Step | When | Preferred (async) | Sync (still OK) |
|------|------|-------------------|-----------------|
| 1 | After Stage 0 + estimate | `POST /agentic/workflow/register` | same |
| 2 | Generate graph | `POST /workflow/jobs` `{ jobType: "preview", payload }` → poll `/progress` | `POST /plan/scratch/preview` |
| 3 | Generate assets | `POST /workflow/jobs` `{ jobType: "execute", payload }` → poll `/progress` | `POST /plan/execute` |

Async jobs return **202** immediately. Poll until `jobStatus === "COMPLETED"` (then `workflowStatus` is `preview` or `draft`). Full graphs live in Process_Configs / configdetails — job `result` is compact only.

**Credit pause:** poll shows `jobStatus=PAUSED_CREDIT` → top up → `POST /workflow/jobs` `{ jobType: "resume" }` (or sync `POST /workflow/resume`) → poll again.

**Stable ID:** `projectId` === `workflowId` (same MongoDB `Process_Configs` document). Use the **same** `projectId` from register through preview and execute.

Sync HTTP (sections 4–5) holds the connection until LLM work finishes. Most FE timeout issues apply to **sync** only; prefer jobs + progress for production.

---

## 2. Base URL and headers

```typescript
const AGENT_ROOT = `${import.meta.env.VITE_AGENT_API_BASE}${import.meta.env.VITE_AGENT_API_PREFIX}`;
// Example: http://localhost:5010/dev/aiqod-agent/agent
```

Every request:

| Header / field | Value |
|----------------|--------|
| `Content-Type` | `application/json` |
| `x-subscriber-id` | Required (or `subscriberId` in body) |
| `x-org-id`, `x-user-id` | Optional (body overrides headers) |
| `Authorization` | Bearer token if your gateway requires it |

**Inspect the POST in DevTools**, not only the OPTIONS preflight. A successful preview/execute is a **POST** that stays open until the server returns JSON.

---

## 3. Prerequisites — register (required)

If you call preview without register, you get **400** with code `WORKFLOW_NOT_REGISTERED`:

```json
{
  "detail": "Workflow is not registered in Process_Configs. Call POST /agentic/workflow/register after queue admission."
}
```

Register **once** after `/agentic/plan/estimate` and **before** Stage 0 / preview:

```typescript
await agentPost("/agentic/workflow/register", {
  type: "workflow",
  projectId: tenant.projectId,
  subscriberId: tenant.subscriberId,
  orgId: tenant.orgId,
  userId: tenant.userId,
  predictedCost: estimate.predictedCost,
  reserveSubscriberCredits: true,
  workflowName: planInput.selectedName,
});
```

Keep `projectId` in app state for all later calls.

---

## 4. `POST /agentic/plan/scratch/preview`

**Full path:** `{AGENT_ROOT}/agentic/plan/scratch/preview`

**Typical duration:** ~**40–90 seconds** (can be longer for complex prompts).  
**Client timeout:** set **≥ 3 minutes** (180_000 ms). Do **not** use default axios/fetch timeouts (often 30–60s).

### Request body

| Field | Required | Notes |
|-------|----------|-------|
| `projectId` | Yes | Same as registered workflow |
| `subscriberId` | Yes | |
| `prompt` **or** `steps` | Exactly one | XOR — not both, not neither |
| `stage0Handoff` | Yes if Stage 0 ran | `{ requirementSummary: … }` from final `/stage0/respond` |
| `selectedName`, `orgId`, `userId` | Recommended | |
| ~~`minimumCredits`~~ | No | Deprecated — billing uses registered `predictedCost` |

### Minimal example (prompt mode)

```typescript
const PREVIEW_TIMEOUT_MS = 180_000; // 3 min — increase if needed

async function scratchPreview(
  tenant: TenantContext,
  prompt: string,
  stage0Handoff: { requirementSummary: unknown },
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PREVIEW_TIMEOUT_MS);

  try {
    const res = await fetch(`${AGENT_ROOT}/agentic/plan/scratch/preview`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-subscriber-id": tenant.subscriberId,
        "x-org-id": tenant.orgId,
        "x-user-id": tenant.userId,
      },
      body: JSON.stringify({
        projectId: tenant.projectId,
        subscriberId: tenant.subscriberId,
        orgId: tenant.orgId,
        userId: tenant.userId,
        prompt,
        stage0Handoff,
        selectedName: "My Workflow",
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail ?? `Preview failed: ${res.status}`);
    }

    return (await res.json()) as AgenticPlanPreviewResponse;
  } finally {
    clearTimeout(timer);
  }
}
```

### Axios

```typescript
import axios from "axios";

const client = axios.create({
  baseURL: AGENT_ROOT,
  timeout: 180_000, // preview — use 3_600_000 for execute
  headers: { "Content-Type": "application/json" },
});

const { data } = await client.post("/agentic/plan/scratch/preview", body, {
  headers: { "x-subscriber-id": tenant.subscriberId },
});
```

### Response — what to keep for execute

```typescript
interface AgenticPlanPreviewResponse {
  workflowId: string;           // same as projectId
  workflowName?: string;
  mode: "prompt" | "steps";
  steps: LogicalStep[];
  phaseGraphs: PhaseGraphResult[];  // pass verbatim to execute
  warnings: string[];
  stepCapabilities: unknown[];      // often [] for scratch
  estimatedCredits: number;         // informational; cap is in Process_Configs
}
```

**Persist `preview` in state** — especially `workflowId` and `phaseGraphs`. The execute response does **not** return `workflowId`.

---

## 5. `POST /agentic/plan/execute`

**Full path:** `{AGENT_ROOT}/agentic/plan/execute`

**Typical duration:** **8–20+ minutes** (many screens/tools → 30–60+ min possible).  
**Client timeout:** set **≥ 60 minutes** (`3_600_000` ms).

This is a **single long POST**. There is no streaming body on this endpoint; show a non-dismissible progress UI and disable double-submit.

### Request body

Pass preview output **verbatim** (do not strip `GENERATE:*` nodes):

| Field | Required | Notes |
|-------|----------|-------|
| `workflowId` | Yes | From `preview.workflowId` |
| `projectId` | Yes | Same tenant project |
| `subscriberId` | Yes | |
| `phaseGraphs` | Yes | Unmodified from preview |
| `steps` | Recommended | From preview |
| `mode` | Yes | From preview |
| `prompt` | If prompt mode | Original prompt |
| `estimatedCredits` | Optional | Legacy; cap comes from DB |

```typescript
const EXECUTE_TIMEOUT_MS = 3_600_000; // 60 min

async function executePlan(preview: AgenticPlanPreviewResponse, tenant: TenantContext) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), EXECUTE_TIMEOUT_MS);

  try {
    const res = await fetch(`${AGENT_ROOT}/agentic/plan/execute`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-subscriber-id": tenant.subscriberId,
      },
      body: JSON.stringify({
        workflowId: preview.workflowId,
        projectId: tenant.projectId,
        subscriberId: tenant.subscriberId,
        orgId: tenant.orgId,
        userId: tenant.userId,
        mode: preview.mode,
        steps: preview.steps,
        phaseGraphs: preview.phaseGraphs,
        workflowName: preview.workflowName,
      }),
      signal: controller.signal,
    });

    if (res.status === 402) {
      const detail = await res.json();
      // Show recharge UI → POST /agentic/workflow/resume (see CREDIT_FLOW readme)
      throw Object.assign(new Error("Paused — credits"), { status: 402, detail });
    }

    if (!res.ok) throw new Error(`Execute failed: ${res.status}`);
    return await res.json(); // AgenticPlanResponse — phaseGraphs now resolved
  } finally {
    clearTimeout(timer);
  }
}
```

### Optional progress while waiting

Poll (does not replace the long POST):

```typescript
POST /agentic/workflow/progress
{ workflowId, subscriberId }
```

Use `pipelineStage`, `overallPercentComplete`, `gapProgress` for UI hints. See `WORKFLOW_BUILDER_CREDIT_FLOW_README.md` §4.4.

---

## 6. Timeouts — layer checklist

| Layer | Preview | Execute |
|-------|---------|---------|
| **fetch / axios** | ≥ 180s | ≥ 3600s (60 min) |
| **UI** | Loading spinner; block navigation away | Non-dismissible progress; disable re-submit |
| **React** | Do not abort on unmount unless user cancels | Same — `useEffect` cleanup often kills in-flight requests |
| **API gateway / nginx** | `proxy_read_timeout` ≥ 180s | ≥ 3600s on agent routes |
| **DevTools** | Watch **POST** status “pending” then **200** + JSON | Same — pending for many minutes is normal |

**If the client times out but the server finished:** call `POST /agentic/workflow/progress` or list workflows — the build may have succeeded server-side.

---

## 7. Common FE mistakes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| 400 `WORKFLOW_NOT_REGISTERED` | Skipped `/workflow/register` | Register after estimate, before preview |
| Request **cancelled** in Network tab | `AbortController` on route change / strict mode double-mount | Keep request alive until user leaves flow; or resume via `/workflow/progress` |
| **Failed / timeout** after ~30–60s | Default HTTP client timeout | Set explicit timeout (§6) |
| No response but backend log shows **200** | Proxy timeout shorter than server | Raise nginx/gateway read timeout |
| CORS error on POST only | Wrong `AGENT_ROOT` or missing gateway CORS | Match working curl host; preflight alone is not enough |
| 400 on preview | Both `prompt` and `steps` sent, or neither | Exactly one |
| Execute starts from stale graph | Rebuilt `phaseGraphs` from UI edits | Send **exact** preview JSON; edit graph only after execute if needed |
| Lost `workflowId` after execute | Execute response omits it | Keep `preview.workflowId` in state |

---

## 8. Verify backend independently (curl)

Use this to confirm the API before debugging FE:

```bash
BASE="http://localhost:5010/dev/aiqod-agent/agent"
SUB="YOUR_SUBSCRIBER_ID"
PID="YOUR_PROJECT_ID"

# 1) Register (after estimate)
curl -sS -X POST "$BASE/agentic/workflow/register" \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"workflow\",\"projectId\":\"$PID\",\"subscriberId\":\"$SUB\",\"predictedCost\":1.5,\"reserveSubscriberCredits\":true}"

# 2) Preview (allow 2–3 min)
curl -sS --max-time 300 -X POST "$BASE/agentic/plan/scratch/preview" \
  -H "Content-Type: application/json" \
  -H "x-subscriber-id: $SUB" \
  -d @preview-body.json

# 3) Execute (allow 60+ min)
curl -sS --max-time 7200 -X POST "$BASE/agentic/plan/execute" \
  -H "Content-Type: application/json" \
  -H "x-subscriber-id: $SUB" \
  -d @execute-body.json
```

If curl returns **200** with JSON but the browser does not, the problem is **not** the Python handler — fix client timeout, proxy, or abort logic.

---

## 9. HTTP status quick reference

| Status | Endpoint | Meaning |
|--------|----------|---------|
| **200** | preview / execute | Success — parse JSON body |
| **400** | preview | Invalid body, or `WORKFLOW_NOT_REGISTERED` |
| **400** | execute | Missing `subscriberId` or invalid payload |
| **402** | any billed step | Paused for credits → `/workflow/resume` after top-up |
| **5xx** | any | Server error — check backend logs |

---

## 10. Frontend checklist

- [ ] `POST /agentic/workflow/register` with same `projectId` before preview
- [ ] Prefer async: `POST /workflow/jobs` + poll `/progress` (see jobs handoff doc)
- [ ] Preview payload: exactly one of `prompt` or `steps`; include `stage0Handoff` when Stage 0 ran
- [ ] After async preview `COMPLETED`, load `phaseGraphs` from configdetails / Process_Configs (not from job.result)
- [ ] Execute payload: pass those `phaseGraphs` unchanged
- [ ] Sync-only: HTTP client timeout ≥ **180s** (preview) / **3600s** (execute)
- [ ] Handle **402** / `jobStatus=PAUSED_CREDIT` with pause UI + resume flow
- [ ] Confirm nginx/gateway timeouts on agent routes (sync path) / Valkey+worker healthy (async path)

For canvas binding, screen URLs, and runtime (`langgraph/`), continue with `WORKFLOW_BUILDER_BACKEND_FRONTEND_README.md`.
