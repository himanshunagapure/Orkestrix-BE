# Run a Created Workflow — Frontend Integration Guide

**Audience:** Frontend developers integrating **runtime** execution of an already-built workflow (status `draft` in Process_Configs).

**This is NOT** preview / execute / build jobs. Those hit the **agent** API (`:5010`).  
**This guide** is LangGraph **run** APIs (`langgraph/main.py` + `worker.py` on **`:8011`**).

| Concern | Service | Base URL (local) |
|---------|---------|------------------|
| Build / preview / codegen | Agent `agent_new.py` | `http://localhost:5010/dev/aiqod-agent/agent` |
| **Run workflow instances** | LangGraph `main.py` | **`http://localhost:8011/dev`** |

If you call agent URLs for start/stream/resume, or close SSE after the first screen, the run will look “stuck”. Backend is usually fine — FE wiring is wrong.

**Code:** `langgraph/main.py`, `langgraph/worker.py`, `langgraph/app/executor.py`  
**Related:** `WORKFLOW_EXECUTION_RUNTIME_FRONTEND_README.md` (SSE pitfalls), `WORKFLOW_ASYNC_BUILD_FRONTEND_GUIDE.md` (build only)

---

## 0. Prerequisites (must be up)

| Process | How | Port / notes |
|---------|-----|----------------|
| MongoDB | local / remote | Workflow definition in `Process_Configs` |
| Valkey/Redis | `redis-server --port 6380` | Queue + SSE pub/sub |
| LangGraph API | `cd langgraph && python main.py` | **`APP_PORT=8011`**, prefix **`/dev`** |
| LangGraph worker | `cd langgraph && python worker.py` | Consumes `VALKEY_STREAM_NAME` (runtime) |

Check:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8011/dev/docs
# or hit start with a dummy body and expect 422/400, not connection refused
```

Worker must be running or `/execute` / `/resume` return 200 but **nothing happens** (no SSE events).

---

## 1. Mental model (read this twice)

```text
FE                         LangGraph API (:8011)              Worker
│  POST /instances/start  → create Mongo instance            │
│  ← execution_id                                            │
│  GET  …/stream (SSE)    → subscribe Valkey channel         │
│  POST …/execute         → XADD queue  ───────────────────► │ runs graph
│  ← SSE ui_request                                          │ pauses on UI
│  user fills screen                                         │
│  POST …/resume          → XADD queue  ───────────────────► │ continues
│  ← SSE ui_request (next) …                                 │
│  ← SSE completed                                           │
```

| Call | What it does | What it does **NOT** do |
|------|----------------|-------------------------|
| `POST /instances/start` | Creates run; returns `execution_id` | Start the graph |
| `GET …/stream` | **Live** channel for UI / logs / done | Replay missed events if you connect late |
| `POST …/execute` | Enqueues start; HTTP **200** immediate | Return screens or wait for finish |
| `POST …/resume` | Enqueues continue after a screen; **200** immediate | Return the **next** screen in the HTTP body |
| `GET …/current-task` | Reads current pause from Mongo | Replace keeping SSE open for live runs |
| Agent `POST …/dynamic/execute/…` | Screen/tool side effects | Advance the LangGraph instance |

**Rule:** `/execute` and `/resume` HTTP 200 only mean “job queued”. Next UI always comes from **SSE `ui_request`** (or recovery via `/current-task`).

---

## 2. Base URL and env

```typescript
// LangGraph runtime — NOT the agent
const LANGGRAPH =
  import.meta.env.VITE_LANGGRAPH_BASE ?? "http://localhost:8011/dev";

// Workflow id = same projectId used at register / build
const workflowId = "test47_sb_5beaabd82ac6767c86dc316c";
```

| Env (langgraph/.env) | Typical local |
|----------------------|---------------|
| `APP_PORT` | `8011` |
| `API_PREFIX` | `/dev` |
| Full base | `http://localhost:8011/dev` |

Gateway example (if used): `http://localhost:5010/…` is often **agent**, not LangGraph. Confirm which host serves `/instances/start`.

Headers: `Content-Type: application/json` for POSTs. SSE uses `EventSource` (no custom Authorization header unless you proxy differently).

---

## 3. Correct call order (always)

```text
1. POST /instances/start          → save execution_id
2. GET  /instances/{id}/stream    → open ONCE, keep open
3. POST /instances/{id}/execute   → after stream is open
4. On each SSE ui_request:
      show screen → user submits → POST /instances/{id}/resume
      (do NOT close EventSource)
5. On SSE completed | failed → close EventSource
```

**Timings**

| Step | Typical wait |
|------|----------------|
| `/start` | &lt; 1s |
| `/execute` HTTP | &lt; 1s (enqueue only) |
| First `ui_request` after execute | seconds (worker + first nodes) |
| `/resume` HTTP | &lt; 1s |
| Next `ui_request` after resume | seconds |
| Full multi-screen run | minutes; SSE stays open the whole time |

Do **not** set a short timeout that aborts `/stream`. It is long-lived by design.

---

## 4. Endpoints — request / response

All paths below are under `{LANGGRAPH}`.

---

### 4.1 Start a run — `POST /instances/start`

**When:** User clicks **Run** on a **draft** workflow.

**Request:**

```json
{
  "workflow_id": "test47_sb_5beaabd82ac6767c86dc316c",
  "initial_state": {},
  "userId": "5beaabd82ac6767c86dc313e"
}
```

| Field | Required | Notes |
|-------|----------|--------|
| `workflow_id` | Yes | `Process_Configs.agentic_process.workflow_id` (same as build `projectId`) |
| `initial_state` | Yes (can be `{}`) | Merged into graph `global_inputs` |
| `userId` | Recommended | Stored as `created_by` (My Tasks “created”) |

**Response (200):**

```json
{
  "message": "Instance Initialized",
  "execution_id": "exec_a3261e37",
  "status": "initialized"
}
```

Store `execution_id`. Status is `initialized` until `/execute`.

**Mistakes**

- Using agent base URL or `/aiqod-agent/...`
- Passing preview-only ids that were never executed to `draft`
- Calling `/execute` before `/start`

---

### 4.2 Open SSE — `GET /instances/{execution_id}/stream`

**When:** Immediately after `/start`, **before** `/execute`.

```typescript
const es = new EventSource(
  `${LANGGRAPH}/instances/${executionId}/stream`,
);
```

**Media type:** `text/event-stream`  
**Keep-alive:** server sends `event: ping` about every **15s** when idle — ignore.

| SSE `event` | Data (JSON) | FE action |
|-------------|-------------|-----------|
| `ui_request` | Screen interrupt | Mount UI; on submit → `/resume` |
| `log` | Node progress | Optional timeline; **ignore** if `outputs.__interrupt__` |
| `completed` | Done | Close `EventSource`; success UI |
| `failed` | Error payload | Close `EventSource`; error UI |
| `ping` | `{}` | Ignore |

#### Example `ui_request` data

```json
{
  "screen_id": "screen_20260705100731_17a0a43d",
  "ui_data": {
    "invoice_file_url": "https://…",
    "extracted_data_json": "{…}"
  },
  "node_name": "review_extraction",
  "execution_id": "exec_a3261e37",
  "assigned_to": "human_user"
}
```

| Field | Use |
|-------|-----|
| `screen_id` | Which Angular screen / iframe to load |
| `ui_data` | Inputs for the screen (`setInput`) |
| `node_name` | Graph node id (debug / labels) |
| `execution_id` | Same run id |
| `assigned_to` | Assignment hint |

**Critical:** One `EventSource` per `execution_id` for the **entire** run. Close only on `completed` / `failed`.

---

### 4.3 Enqueue start — `POST /instances/{execution_id}/execute`

**When:** SSE is open.

```http
POST http://localhost:8011/dev/instances/exec_a3261e37/execute
```

No body required.

**Response (200):**

```json
{
  "message": "Execution started",
  "execution_id": "exec_a3261e37"
}
```

**Errors**

| HTTP | Meaning |
|------|---------|
| 400 | Instance not `initialized` (already executed) |
| 404 | Unknown `execution_id` |

Then wait for SSE — do not poll `/execute`.

---

### 4.4 Continue after UI — `POST /instances/{execution_id}/resume`

**When:** User finished the screen from the latest `ui_request`.

**Request:**

```json
{
  "payload": {
    "approved": true,
    "notes": "looks good"
  },
  "userid": "5beaabd82ac6767c86dc313e",
  "roleid": "optional-role-id"
}
```

| Field | Required | Notes |
|-------|----------|--------|
| `payload` | Yes | Screen **outputs** (keys must match declared outputs) |
| `userid` / `roleid` | If node assignment set | Else **403** Unauthorized |

**Response (200):**

```json
{
  "message": "Resume job dispatched",
  "execution_id": "exec_a3261e37"
}
```

**Does not include** next `screen_id`. Keep SSE open and wait for the next `ui_request` (or `completed`).

After resume, show a short “running…” state until the next event.

---

### 4.5 Recover current pause — `GET /instances/{execution_id}/current-task`

**When:**

- SSE dropped / reconnect
- Opening a run from **My Tasks** that is already `waiting_human`
- After `/resume`, as a safety sync (optional)

```http
GET http://localhost:8011/dev/instances/exec_a3261e37/current-task
```

**Response (200):**

```json
{
  "status": "success",
  "data": {
    "executionId": "exec_a3261e37",
    "workflowId": "test47_sb_…",
    "instanceStatus": "waiting_human",
    "uiRequest": {
      "screen_id": "screen_…",
      "ui_data": {},
      "execution_id": "exec_a3261e37",
      "node_name": "review_extraction",
      "assigned_to": "…"
    }
  }
}
```

| `instanceStatus` | FE action |
|------------------|-----------|
| `initialized` | Call `/execute` (stream already open) |
| `running` | Spinner; listen SSE |
| `waiting_human` | If `uiRequest.screen_id` → same as SSE `ui_request` |
| `completed` / `failed` | End UI |

**SSE does not replay.** If you missed `ui_request`, `/current-task` is how you recover the pending screen.

---

### 4.6 List runnable workflows — `POST /instances/list-by-org-sub`

**When:** “My Workflows” / pick a draft to run.

```json
{
  "orgId": "5c495dbfffa2a85b2c19a77f",
  "subscriberId": "5beaabd82ac6767c86dc311c"
}
```

Returns Process_Configs rows (`agentic_process.workflow_id`, names, etc.). Prefer workflows with status **draft** for run.

---

### 4.7 My Tasks — `POST /instances/my-tasks/workflows` + `POST /instances/my-tasks`

**Workflows with tasks for this user:**

```json
{
  "orgId": "…",
  "subscriberId": "…",
  "userId": "…",
  "roleId": "…",
  "filterType": "assigned"
}
```

`filterType`: `"assigned"` | `"created"`.

**Tasks for one workflow:**

```json
{
  "workflow_id": "test47_sb_…",
  "userId": "…",
  "roleId": "…",
  "filterType": "assigned"
}
```

Then open `executionId` → `GET …/current-task` → mount screen → `/resume` (and open SSE for further events).

---

## 5. Reference FE flow (TypeScript)

```typescript
const LANGGRAPH = "http://localhost:8011/dev";

type UiRequest = {
  screen_id: string;
  ui_data?: Record<string, unknown>;
  node_name?: string;
  execution_id: string;
  assigned_to?: string;
};

async function runWorkflow(
  workflowId: string,
  initialState: Record<string, unknown>,
  userId: string,
  showScreen: (ui: UiRequest) => Promise<Record<string, unknown>>,
) {
  // 1) Start
  const start = await fetch(`${LANGGRAPH}/instances/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workflow_id: workflowId,
      initial_state: initialState,
      userId,
    }),
  }).then((r) => {
    if (!r.ok) throw new Error(`start ${r.status}`);
    return r.json();
  });

  const executionId: string = start.execution_id;
  let terminal = false;

  // 2) SSE — ONE connection for the whole run
  const es = new EventSource(`${LANGGRAPH}/instances/${executionId}/stream`);

  const handleUi = async (ui: UiRequest) => {
    const output = await showScreen(ui);
    // 4) Resume — do NOT es.close()
    const res = await fetch(`${LANGGRAPH}/instances/${executionId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: output, userid: userId }),
    });
    if (!res.ok) throw new Error(`resume ${res.status} ${await res.text()}`);
    // wait for next ui_request | completed | failed
  };

  es.addEventListener("ui_request", (ev) => {
    const ui = JSON.parse((ev as MessageEvent).data) as UiRequest;
    void handleUi(ui).catch(console.error);
  });

  es.addEventListener("log", (ev) => {
    const data = JSON.parse((ev as MessageEvent).data);
    if (data?.outputs?.__interrupt__) return;
    // optional: append timeline
  });

  es.addEventListener("completed", () => {
    terminal = true;
    es.close();
  });

  es.addEventListener("failed", (ev) => {
    terminal = true;
    es.close();
    console.error(JSON.parse((ev as MessageEvent).data));
  });

  es.addEventListener("ping", () => {});

  es.onerror = () => {
    if (terminal) return;
    // Recover pending UI if event was missed
    void fetch(`${LANGGRAPH}/instances/${executionId}/current-task`)
      .then((r) => r.json())
      .then((json) => {
        const { instanceStatus, uiRequest } = json.data ?? {};
        if (instanceStatus === "waiting_human" && uiRequest?.screen_id) {
          void handleUi(uiRequest);
        }
      });
  };

  // 3) Execute AFTER stream is listening
  await fetch(`${LANGGRAPH}/instances/${executionId}/execute`, {
    method: "POST",
  }).then((r) => {
    if (!r.ok) throw new Error(`execute ${r.status}`);
  });

  return {
    executionId,
    close: () => es.close(),
  };
}
```

### Screen host contract (typical)

```typescript
await window[`bootstrap_${screenId.replace(/-/g, "_")}`]?.();
window.__AIQOD_SCREEN__.setInput(ui.ui_data ?? {});
const output = await window.__AIQOD_SCREEN__.waitForCompletion();
// POST /resume { payload: output }
```

---

## 6. React lifecycle pitfalls (very common)

```typescript
// BAD — closes SSE when screenId changes
useEffect(() => {
  const es = new EventSource(url);
  return () => es.close();
}, [screenId]);

// GOOD — bind to executionId only; keep parent shell mounted
useEffect(() => {
  const es = new EventSource(`${LANGGRAPH}/instances/${executionId}/stream`);
  // …listeners
  return () => es.close();
}, [executionId]);
```

| Mistake | Result |
|---------|--------|
| Close SSE on first `ui_request` | Screen 2 never arrives |
| Close SSE after `/resume` 200 | Same |
| Unmount stream owner when swapping iframes | Same |
| Treat `/resume` 200 as “run finished” | Infinite loading |
| Open a new EventSource per screen | Missed events / races |
| Call agent `/workflow/jobs` thinking it “runs” the workflow | Wrong API (that’s **build**) |

---

## 7. Build vs run — do not mix

| Goal | API | Port |
|------|-----|------|
| Generate graph / codegen | Agent `/workflow/jobs` + `/progress` | **5010** |
| **Run** an existing draft | LangGraph `/instances/*` | **8011** |

`workflow_id` for run = same id as build `projectId` / `workflowId`.

---

## 8. Curl smoke test

```bash
LG="http://localhost:8011/dev"
WF="YOUR_DRAFT_WORKFLOW_ID"

EXEC=$(curl -s -X POST "$LG/instances/start" \
  -H "Content-Type: application/json" \
  -d "{\"workflow_id\":\"$WF\",\"initial_state\":{},\"userId\":\"u1\"}" \
  | jq -r .execution_id)
echo "execution_id=$EXEC"

# Terminal A — leave open
curl -N "$LG/instances/$EXEC/stream"

# Terminal B
curl -s -X POST "$LG/instances/$EXEC/execute"

# After ui_request appears in A:
curl -s -X POST "$LG/instances/$EXEC/resume" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"field":"value"},"userid":"u1"}'

# If stuck:
curl -s "$LG/instances/$EXEC/current-task" | jq .
```

---

## 9. Debugging checklist

1. Network: **one** long `GET …/stream` until `completed`.
2. LangGraph log: `Closed SSE connection` before end ⇒ FE closed early.
3. Worker log: `Paused. Waiting for UI: screen_…` ⇒ backend waiting on FE.
4. Mongo `instances`: `waiting_human` + `pending_ui_request` set ⇒ use SSE or `/current-task`.
5. Worker not running ⇒ execute/resume 200 but no SSE events.

---

## 10. FE acceptance checklist

- [ ] Uses `http://localhost:8011/dev` (or env equivalent), not agent `:5010` for run
- [ ] Order: start → stream → execute → (ui_request → resume)* → completed
- [ ] Single EventSource per run; closed only on completed/failed
- [ ] `/resume` body is `{ payload, userid?, roleid? }`; next UI from SSE
- [ ] Multi-screen: screen 2 appears without restarting the run
- [ ] SSE drop recoverable via `/current-task`
- [ ] My Tasks path: list → current-task → resume + stream for remainder

---

## 11. Quick reference

```text
Base:  http://localhost:8011/dev

POST /instances/start
POST /instances/{execution_id}/execute
GET  /instances/{execution_id}/stream          ← SSE, keep open
POST /instances/{execution_id}/resume          ← after each UI screen
GET  /instances/{execution_id}/current-task    ← recovery / My Tasks
POST /instances/list-by-org-sub
POST /instances/my-tasks/workflows
POST /instances/my-tasks
```
