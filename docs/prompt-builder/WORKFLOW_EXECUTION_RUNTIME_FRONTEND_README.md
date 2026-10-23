# Workflow Execution Runtime — Frontend Guide

> **Full run integration guide (ports, requests, responses, timings, mistakes):**  
> **`WORKFLOW_RUN_FRONTEND_GUIDE.md`** — start here for FE.

**Audience:** Frontend engineers wiring multi-screen workflow runs (LangGraph instances + SSE).

**Related docs:**
- `WORKFLOW_RUN_FRONTEND_GUIDE.md` — complete run API + FE checklist
- `docs/WORKFLOW_EXECUTION_FRONTEND_GUIDE.md` — older full runtime API reference
- `docs/prompt-builder/WORKFLOW_BUILDER_BACKEND_FRONTEND_README.md` §10 — builder + run overview

---

## 1. Symptom this guide covers

Workflow starts correctly. Screen 1 appears. User submits. Backend advances to screen 2 (`waiting_human` + `pending_ui_request` in Mongo), but the **frontend stays loading and never shows screen 2**.

### Root cause (most common)

The frontend **closed the SSE stream too early**. Screen 2 is delivered only via SSE event `ui_request` (or via recovery poll). `POST /resume` does **not** return the next screen.

### Evidence from a real stuck run

| Source | What it showed |
|--------|----------------|
| Worker | Paused on `collect_service_info` with next `screen_id` |
| Mongo `instances` | `status: waiting_human`, `pending_ui_request` populated for screen 2 |
| LangGraph API log | `[System] Closed SSE connection for exec_…` **before** resume |
| Network tab | `/resume` 200 OK, but stream already gone — no second `ui_request` listener |

**Backend was fine.** Frontend missed the second `ui_request`.

---

## 2. Mental model

| Call | Role |
|------|------|
| `POST /instances/start` | Create run → `execution_id` |
| `GET /instances/{id}/stream` | **Only live channel** for next UI / logs / completion |
| `POST /instances/{id}/execute` | Enqueue start (returns immediately) |
| `POST /instances/{id}/resume` | Enqueue continue after a UI screen (returns immediately) |
| `GET /instances/{id}/current-task` | Recovery: read current pause / `uiRequest` from Mongo |
| `POST …/dynamic/execute/{project}` | Screen/tool side-effect only (e.g. DB insert). Not the workflow driver. |

`/execute` and `/resume` HTTP 200 only mean **job dispatched**. All live updates come from **SSE** (or `/current-task` recovery).

Local base URL:

```text
http://localhost:8011/dev
```

---

## 3. Correct call order (multi-screen)

Use **one** SSE connection for the entire run:

```text
POST /instances/start
GET  /instances/{execution_id}/stream     ← open ONCE, keep open
POST /instances/{execution_id}/execute

  SSE ui_request (screen 1) → mount screen → user submits
  POST /instances/{id}/resume              ← do NOT close EventSource

  SSE log
  SSE ui_request (screen 2) → mount screen → user submits
  POST /instances/{id}/resume

  SSE completed | failed → THEN close EventSource
```

### Expected Network tab

One long-lived request:

```text
GET …/instances/exec_xxx/stream
```

stays open until the workflow finishes.

### Expected SSE sequence (2 UI screens)

```text
[SSE] connected
[SSE] ui_request   screen_…   (node: extract_user_info)
(resume screen 1)
[SSE] log
[SSE] ui_request   screen_…   (node: collect_service_info)
(resume screen 2)
[SSE] log
[SSE] completed
[SSE] close stream
```

If API logs show `Closed SSE connection` **before** `completed` / `failed`, the frontend closed the stream early.

---

## 4. Frontend checklist (fix these)

1. **Do not close `EventSource` on `/resume`.** Close only on SSE `completed` or `failed`.
2. **Do not close on first `ui_request` or on `log`.**
3. **One stream per run** — not one stream per screen.
4. **Keep the run shell mounted** while swapping screen iframes. Unmounting the stream owner kills SSE.
5. After `/resume`, show loading / “running” and **keep listening** for the next `ui_request`. Do not treat resume 200 as “done”.
6. Ignore `log` events whose `outputs` contain `__interrupt__` — that is internal pause metadata, not completion.

### React `useEffect` pitfall

```typescript
// BAD — cleanup runs when parent re-renders on screen change
useEffect(() => {
  const es = new EventSource(url);
  return () => es.close(); // closes stream when UI step changes
}, [screenId]);

// GOOD — tie lifecycle to execution_id only; keep parent mounted
useEffect(() => {
  const es = new EventSource(`${LANGGRAPH}/instances/${executionId}/stream`);
  // ... listeners
  return () => es.close();
}, [executionId]);
```

---

## 5. Reference handler

```typescript
const LANGGRAPH = "http://localhost:8011/dev"; // or VITE_EXEC_API_BASE

type UiRequest = {
  screen_id: string;
  ui_data?: Record<string, unknown>;
  node_name?: string;
  execution_id: string;
  assigned_to?: string;
};

async function runWorkflow(
  workflowId: string,
  projectId: string,
  initialState: Record<string, unknown>,
  mountScreenAndWait: (
    screenId: string,
    uiData: Record<string, unknown>,
    projectId: string,
  ) => Promise<Record<string, unknown>>,
) {
  const start = await fetch(`${LANGGRAPH}/instances/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workflow_id: workflowId, initial_state: initialState }),
  }).then((r) => r.json());

  const executionId: string = start.execution_id;
  let runStatus: "running" | "waiting_human" | "completed" | "failed" = "running";

  // ONE connection for the whole run
  const es = new EventSource(`${LANGGRAPH}/instances/${executionId}/stream`);

  es.addEventListener("ui_request", async (ev) => {
    const ui = JSON.parse((ev as MessageEvent).data) as UiRequest;
    runStatus = "waiting_human";

    const output = await mountScreenAndWait(
      ui.screen_id,
      ui.ui_data ?? {},
      projectId,
    );

    // Do NOT es.close() here
    await fetch(`${LANGGRAPH}/instances/${executionId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: output }),
    });

    runStatus = "running"; // wait for next ui_request | completed | failed
  });

  es.addEventListener("log", (ev) => {
    const data = JSON.parse((ev as MessageEvent).data);
    if (data?.outputs?.__interrupt__) return; // ignore internal pause metadata
    // append to timeline UI
  });

  es.addEventListener("completed", () => {
    runStatus = "completed";
    es.close();
  });

  es.addEventListener("failed", (ev) => {
    runStatus = "failed";
    es.close();
    console.error(JSON.parse((ev as MessageEvent).data));
  });

  es.addEventListener("ping", () => {
    /* keep-alive — ignore */
  });

  es.onerror = () => {
    if (runStatus !== "completed" && runStatus !== "failed") {
      // Optional: recover via GET /current-task (see §6)
      console.error("[SSE] connection error — consider /current-task recovery");
    }
  };

  // Enqueue after SSE is open
  await fetch(`${LANGGRAPH}/instances/${executionId}/execute`, { method: "POST" });

  return { executionId, getStatus: () => runStatus, close: () => es.close() };
}
```

### Screen iframe contract

```typescript
await window[`bootstrap_${screenId.replace(/-/g, "_")}`]?.();
window.__AIQOD_SCREEN__.setInput(uiData);
const output = await window.__AIQOD_SCREEN__.waitForCompletion();
// parent sends { payload: output } to POST /resume
```

`payload` keys must match the screen’s declared output parameters.

---

## 6. Recovery when SSE was already dropped

SSE is Valkey pub/sub — **missed events are not replayed**. Reconnecting the stream alone may not show screen 2 if `ui_request` already fired.

### Endpoint

```http
GET /instances/{execution_id}/current-task
```

Example:

```http
GET http://localhost:8011/dev/instances/exec_20c176d0/current-task
```

### Response shape

```json
{
  "status": "success",
  "data": {
    "executionId": "exec_20c176d0",
    "workflowId": "WerUpJG3_sb_5beaabd82ac6767c86dc316c",
    "instanceStatus": "waiting_human",
    "uiRequest": {
      "screen_id": "screen_20260705100731_17a0a43d",
      "assigned_to": "human_user",
      "ui_data": {
        "name": "qer",
        "phone_number": "9090909090",
        "address": "rhndehhedb"
      },
      "execution_id": "exec_20c176d0",
      "node_name": "collect_service_info"
    }
  }
}
```

`uiRequest` matches the SSE `ui_request` payload (and Mongo `pending_ui_request`).

### Recommended recovery pattern

After every `/resume`, and on SSE `onerror`:

1. Keep or reopen SSE.
2. Call `GET …/current-task`.
3. Branch on `instanceStatus`:
   - `waiting_human` + non-null `uiRequest` → mount that screen (same as SSE `ui_request`)
   - `running` → keep spinner
   - `completed` / `failed` → stop run UI

```typescript
async function syncCurrentTask(executionId: string) {
  const res = await fetch(`${LANGGRAPH}/instances/${executionId}/current-task`);
  const json = await res.json();
  const { instanceStatus, uiRequest } = json.data ?? {};

  if (instanceStatus === "waiting_human" && uiRequest?.screen_id) {
    // same path as SSE ui_request handler
    return { kind: "ui_request" as const, uiRequest };
  }
  if (instanceStatus === "running") return { kind: "running" as const };
  if (instanceStatus === "completed") return { kind: "completed" as const };
  if (instanceStatus === "failed") return { kind: "failed" as const };
  return { kind: "unknown" as const, instanceStatus };
}
```

---

## 7. Do not confuse these APIs

| Call | What it does | What it does **not** do |
|------|----------------|-------------------------|
| `POST …/dynamic/execute/{project}` (`submit_user_info`, etc.) | Runs screen/tool function (e.g. insert row) | Advance LangGraph / return next screen |
| `POST …/instances/{id}/resume` | Enqueues graph continue | Return next `screen_id` / `ui_data` |
| SSE `ui_request` | Delivers next UI interrupt | — |
| `GET …/current-task` | Reads current pause from Mongo | Replace keeping SSE open for live progress |

---

## 8. Debugging checklist

1. **Browser Network** — one long-lived `…/stream` until `completed`.
2. **Console** — log every SSE `event:` type before any `es.close()`.
3. **LangGraph API logs** — `Closed SSE connection` before workflow end ⇒ FE closed early.
4. **Worker logs** — `Paused. Waiting for UI: screen_…` means backend is waiting on FE.
5. **Mongo `instances`** — if `status: waiting_human` and `pending_ui_request` is set, backend already has screen 2; FE must render it from SSE or `/current-task`.

### Instance statuses

| Status | Frontend action |
|--------|-----------------|
| `initialized` | Call `/execute` (SSE already open) |
| `running` | Spinner; listen to SSE |
| `waiting_human` | Show screen from `ui_request` or `/current-task` |
| `completed` | Close stream; success UI |
| `failed` | Close stream; error UI |

---

## 9. Acceptance criteria

- [ ] One `EventSource` per `execution_id`, closed only on `completed` / `failed`
- [ ] Screen changes do not remount/unmount the SSE owner
- [ ] After each `/resume`, FE waits for next `ui_request` (and/or polls `/current-task`)
- [ ] 2+ screen runs show screen N with `ui_data` from prior node outputs
- [ ] Network tab: stream stays open across all UI screens
- [ ] Stuck `waiting_human` recoverable via `GET /current-task` without restarting the run

---

## 10. Quick curl smoke test

```bash
LG="http://localhost:8011/dev"

EXEC=$(curl -s -X POST "$LG/instances/start" \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"YOUR_WORKFLOW_ID","initial_state":{}}' | jq -r .execution_id)
echo "execution_id=$EXEC"

# Terminal A — keep open
curl -N "$LG/instances/$EXEC/stream"

# Terminal B
curl -s -X POST "$LG/instances/$EXEC/execute"

# After ui_request in Terminal A:
curl -s -X POST "$LG/instances/$EXEC/resume" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"name":"Test","phone_number":"123","address":"x"}}'

# If stream was lost:
curl -s "$LG/instances/$EXEC/current-task" | jq .
```

Prerequisites: `langgraph/main.py`, `langgraph/worker.py`, Valkey/Redis, MongoDB.
