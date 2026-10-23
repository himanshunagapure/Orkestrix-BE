# Workflow Build `/execute` — Frontend Guide

**Audience:** Frontend engineers wiring workflow **build** (design-time), especially when the UI stays loading after the backend has already finished.

**Related docs:**
- `PREVIEW_EXECUTE_FE_QUICK_GUIDE.md` — timeouts, request shape
- `WORKFLOW_BUILDER_CREDIT_FLOW_README.md` — progress, 402, resume
- `WORKFLOW_BUILDER_BACKEND_FRONTEND_README.md` — full builder flow

**Not this doc:** LangGraph **runtime** (`/instances/*` SSE). That is a separate run loop after the workflow is built.

---

## 1. Symptom

User flow works through estimate → Stage 0 → preview (phase graph visible) → `/execute` starts.

- Backend logs show execute **completing** (phases done, Process_Configs saved, reservation released).
- Mongo `Process_Configs` has resolved `ui:*` / `tool:*` capability IDs and `agentic_process.status: "draft"`.
- Frontend **still shows loading**.
- Network tab: `/execute` POST has **no response body** / never shows **200**.
- Backend access log often has **no** `200 OK` for that long POST (client/proxy dropped the connection before the response was written or read).

### Real run evidence (`execute-issue.txt`)

| Signal | Meaning |
|--------|---------|
| `[execute] all phases processed … elapsed=627.65s` | Build took ~**10.5 minutes** — normal |
| `[execute] finalization … persisting Process_Configs` | Server finished successfully |
| `reservationStatus: "released"` + `status: "draft"` in Mongo | Build is done |
| `workflow_stream … type=completed subscriber_count=0` | Completion SSE fired; **FE was not subscribed** |
| Network: execute response “couldn't see” | Browser never got the JSON body |
| Mid-build `/workflow/progress` → `workflowStatus: "building"` | Expected **while** building; do not treat as final if `/execute` HTTP is gone |

**Verdict:** Backend succeeded. Frontend (or proxy) lost the long `/execute` HTTP response and never recovered via progress / configdetails / stream.

---

## 2. Mental model

`POST /agentic/plan/execute` is a **single synchronous HTTP request**. The server holds the connection until gap generation + codegen finish, then returns one JSON body (`AgenticPlanResponse`).

| Fact | Implication for FE |
|------|-------------------|
| Duration often **8–20+ minutes** (this run ~10.5 min; complex builds 30–60+) | Client timeout must be **≥ 60 minutes** |
| No streaming body on `/execute` itself | Spinner alone is not enough — use progress poll and/or workflow stream |
| HTTP 200 body is the primary success path | If the connection dies, **build may still have succeeded** |
| Execute response does **not** include `workflowId` | Keep `preview.workflowId` / `projectId` in app state |
| After success, checkpoint is deleted | `/progress` must be re-read; look for `workflowStatus: "draft"` |

Do **not** confuse:

| Endpoint | Role |
|----------|------|
| `POST …/agentic/plan/execute` | **Build** screens/tools (long POST) |
| `POST …/workflow/progress` | Read-only build status (poll) |
| `GET …/workflow/stream/{workflowId}` | Optional SSE: `completed` / `paused_credit` / `error` |
| `GET …/configdetails` | Load final `Process_Configs` after build |
| `POST …/instances/start` | **Run** the built workflow (different service) |

---

## 3. Correct FE pattern (required)

### 3.1 Call order

```text
POST /agentic/plan/estimate
POST /agentic/workflow/register          ← same projectId / workflowId
Stage 0 (start / respond) …
POST /agentic/plan/scratch/preview      ← keep full preview in state
User confirms graph
  ├─ open GET /agentic/workflow/stream/{workflowId}   (recommended)
  ├─ start polling POST /agentic/workflow/progress    (every 5–10s)
  └─ POST /agentic/plan/execute                       (timeout ≥ 60 min)
         │
         ├─ HTTP 200 → use response phaseGraphs → Screen 7 (build complete)
         ├─ HTTP 402 → credit pause → /workflow/resume
         └─ connection lost / timeout / cancelled
                → do NOT stay forever on spinner
                → recover via /progress + /configdetails (§5)
```

### 3.2 HTTP client timeout

```typescript
const AGENT_ROOT = "http://localhost:5010/dev/aiqod-agent/agent";
const EXECUTE_TIMEOUT_MS = 3_600_000; // 60 minutes — required

async function executePlan(
  preview: { workflowId: string; mode: string; steps: unknown; phaseGraphs: unknown[]; workflowName?: string },
  tenant: { projectId: string; subscriberId: string; orgId: string; userId: string },
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), EXECUTE_TIMEOUT_MS);

  try {
    const res = await fetch(`${AGENT_ROOT}/agentic/plan/execute`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-subscriber-id": tenant.subscriberId,
        "x-org-id": tenant.orgId,
        "x-user-id": tenant.userId,
      },
      body: JSON.stringify({
        workflowId: preview.workflowId,
        projectId: tenant.projectId,
        subscriberId: tenant.subscriberId,
        orgId: tenant.orgId,
        userId: tenant.userId,
        mode: preview.mode,
        steps: preview.steps,
        phaseGraphs: preview.phaseGraphs, // verbatim from preview
        workflowName: preview.workflowName,
      }),
      signal: controller.signal,
    });

    if (res.status === 402) {
      const detail = await res.json();
      // Show recharge → POST /agentic/workflow/resume
      throw Object.assign(new Error("Paused — credits"), { status: 402, detail });
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw Object.assign(new Error(`Execute failed: ${res.status}`), { status: res.status, detail: err });
    }

    return await res.json(); // AgenticPlanResponse
  } catch (e) {
    // AbortError / network error → try recovery (§5) before showing hard failure
    throw e;
  } finally {
    clearTimeout(timer);
  }
}
```

**Axios:** `timeout: 3_600_000` on execute (not the default 30–60s).

### 3.3 Do not abort the request accidentally

| Mistake | Symptom |
|---------|---------|
| `useEffect` cleanup aborts fetch on remount / Strict Mode | Network: **cancelled**; backend may still finish |
| Navigate away / unmount build page mid-execute | Same |
| Default axios/fetch timeout (~30–60s) | Fails early while server still building |
| Proxy `proxy_read_timeout` too low | Server finishes; browser never sees 200 |

Keep the build page mounted. Only abort if the user explicitly cancels.

### 3.4 Progress polling (while `/execute` is in flight)

```typescript
async function pollProgress(workflowId: string, subscriberId: string, orgId?: string) {
  const res = await fetch(`${AGENT_ROOT}/agentic/workflow/progress`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workflowId, subscriberId, orgId }),
  });
  return res.json();
}

// Every 5–10s while UI phase === "generating"
const p = await pollProgress(workflowId, subscriberId, orgId);
// Prefer: gapProgress, phaseProgress.currentNodeId, billing, stoppedAt, workflowStatus
```

**Progress reliability:**
- Live gap % is driven by `workflow_build_checkpoints`, not by `Process_Configs.phaseGraphs` (those stay on `GENERATE:*` until execute finishes).
- If `checkpointUpdatedAt` stays `null` and `gapProgress.resolved` stays `0` while `billing.budgetSpentUsd` rises, checkpoints are not writing — use billing + final `draft` as fallback.
- Prefer `gapProgress.percentComplete` / `phaseProgress.currentNodeId` for fine-grained execute UI; `overallPercentComplete` is coarse (stage buckets).

**Completion signal from progress:**

| `workflowStatus` | Meaning | FE action |
|------------------|---------|-----------|
| `building` | Execute still running | Keep spinner + progress bar |
| `paused_credit` | Out of credits | Show recharge + `/resume` |
| `draft` | **Build complete** | Leave loading → build-complete screen |
| `preview` | Graph ready, execute not done | Should not happen mid-execute |

When `workflowStatus === "draft"`:

- Treat as success even if `/execute` HTTP never returned.
- Load final graphs via `GET /configdetails` (header `workflow-id: {workflowId}`).
- Keep `preview.workflowId` for later **Run**.

### 3.5 Optional: workflow status SSE (recommended)

Open **before** or **as** `/execute` starts:

```http
GET {AGENT_ROOT}/agentic/workflow/stream/{workflowId}
Header: x-subscriber-id: …
```

Events (JSON in `data:`):

| `type` | Meaning |
|--------|---------|
| `paused_credit` | Same as HTTP 402 path |
| `completed` | Build finished — leave loading; load configdetails / use execute response if it also arrives |
| `error` | Build failed |

Backend log from the stuck run:

```text
workflow_stream publish … type=completed subscriber_count=0 (no live subscribers)
```

That means FE never opened this stream. Opening it is the cleanest way to leave the spinner when the long POST is dropped.

```typescript
function watchBuildStream(workflowId: string, subscriberId: string, onDone: () => void) {
  // EventSource cannot set custom headers in all browsers — if gateway requires
  // x-subscriber-id, use fetch+ReadableStream or pass subscriberId as query if supported.
  const url = `${AGENT_ROOT}/agentic/workflow/stream/${workflowId}?subscriberId=${encodeURIComponent(subscriberId)}`;
  const es = new EventSource(url);

  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "completed") {
      es.close();
      onDone();
    }
    if (msg.type === "paused_credit" || msg.type === "error") {
      es.close();
      // handle pause / error UI
    }
  };

  return () => es.close();
}
```

---

## 4. After successful build — what to show

Prefer the `/execute` **200** body when available:

- `phaseGraphs` — resolved (no `GENERATE:*`)
- `warnings`, optional `combinedLanggraphCode`

Always keep from preview:

- `workflowId` / `projectId` (same value)

If HTTP body was lost, recover:

```typescript
async function loadBuiltWorkflow(workflowId: string) {
  const res = await fetch(`${AGENT_ROOT}/configdetails`, {
    headers: { "workflow-id": workflowId },
  });
  const json = await res.json();
  return json.data?.agentic_process?.phaseGraphs ?? [];
}
```

Success checks in Mongo / UI:

- Capability IDs like `ui:screen_…` and `tool:…` (not `GENERATE:…`)
- `reservationStatus: "released"`
- `agentic_process.status: "draft"`

Then enable **Run workflow** (LangGraph instances) — separate from this build call.

---

## 5. Recovery when `/execute` response is missing

Use this whenever:

- Network shows cancelled / failed / (pending forever) / no response
- User refreshed mid-build
- Spinner still up but you suspect backend finished

```typescript
async function recoverAfterLostExecute(workflowId: string, subscriberId: string, orgId?: string) {
  const progress = await pollProgress(workflowId, subscriberId, orgId);

  if (progress.workflowStatus === "draft") {
    const phaseGraphs = await loadBuiltWorkflow(workflowId);
    return { status: "complete" as const, phaseGraphs, workflowId };
  }

  if (progress.workflowStatus === "paused_credit" || progress.canResume) {
    return { status: "paused_credit" as const, progress };
  }

  if (progress.workflowStatus === "building") {
    return { status: "still_building" as const, progress }; // keep polling
  }

  return { status: "unknown" as const, progress };
}
```

**Rules:**

1. **Never** re-call `/execute` with the old preview `GENERATE:*` graphs after a successful or mid-execute pause — use `/workflow/resume` for credit pause; for lost response after success, load `configdetails`.
2. Do not treat a single mid-build progress snapshot as failure.
3. After client abort/timeout, **always** call recovery before showing “Build failed”.

---

## 6. Debugging checklist (FE)

1. **Network → POST `/execute`**
   - Status **pending for many minutes** = normal.
   - Ends as **cancelled** / **failed** / no response while Mongo is `draft` = timeout/abort/proxy — recover (§5).
2. **Client timeout** ≥ 3600s on execute.
3. **No AbortController** tied to screen transitions.
4. **Poll `/progress`** — when status flips to `draft`, leave loading.
5. **Subscribe to `/workflow/stream/{workflowId}`** — listen for `completed`.
6. **Backend logs** — `elapsed=…s`, `persisting Process_Configs`, `reservation released` means success even without FE 200.
7. **Proxy / nginx** — `proxy_read_timeout` ≥ 3600s on agent routes (local: often no nginx; still check browser/extension/devtools limits).

### How to confirm backend finished without FE

```bash
# Progress should show draft after completion
curl -sS -X POST "http://localhost:5010/dev/aiqod-agent/agent/agentic/workflow/progress" \
  -H "Content-Type: application/json" \
  -d '{"workflowId":"JFrIBcvy_sb_…","subscriberId":"5beaabd82ac6767c86dc316c"}' | jq .workflowStatus

# Or configdetails
curl -sS "http://localhost:5010/dev/aiqod-agent/agent/configdetails" \
  -H "workflow-id: JFrIBcvy_sb_…" | jq '.data.agentic_process.status'
```

---

## 7. Common mistakes

| Mistake | Result |
|---------|--------|
| Wait only on `/execute` HTTP with default 30–60s timeout | Spinner forever or early “failed”; build may succeed server-side |
| Abort fetch on React remount | Cancelled in Network; `subscriber_count=0` on stream |
| Ignore `/progress` after abort | Never discover `workflowStatus: "draft"` |
| Re-POST `/execute` after success | Wasteful / may error; use configdetails |
| Expect execute JSON to include `workflowId` | Lost ID → keep preview `workflowId` |
| Treat `/progress` `building` forever without recovery path | Stuck UI when HTTP dropped after completion |
| Confuse build `/execute` with runtime `/instances/execute` | Wrong service / wrong loading state |

---

## 8. Acceptance criteria

- [ ] Execute HTTP timeout ≥ **60 minutes**
- [ ] Build page stays mounted; no accidental abort
- [ ] Progress polled every 5–10s during generate
- [ ] On `workflowStatus === "draft"` → build-complete UI (even without execute HTTP body)
- [ ] Optional: SSE `/workflow/stream/{id}` handles `completed` / `paused_credit`
- [ ] On abort/timeout → `recoverAfterLostExecute` before error toast
- [ ] `workflowId` retained from preview; final graphs from execute body **or** `configdetails`
- [ ] Double-submit disabled while building

---

## 9. Quick reference — endpoints

| Method | Path | When |
|--------|------|------|
| POST | `/agentic/plan/execute` | Start build (long) |
| POST | `/agentic/workflow/progress` | Poll status / detect `draft` |
| GET | `/agentic/workflow/stream/{workflowId}` | Live `completed` / pause |
| GET | `/configdetails` | Load final graphs after lost HTTP |
| POST | `/agentic/workflow/resume` | After 402 credit pause only |
