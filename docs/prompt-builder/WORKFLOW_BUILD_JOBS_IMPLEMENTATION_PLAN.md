# Workflow Build Jobs — Implementation Plan

**Audience:** Backend and frontend engineers implementing async **preview** and **execute** for the workflow builder.

**Status:** Phase 1–4 done. Async preview/execute/resume via `POST /workflow/jobs`; worker calls unchanged sync `/scratch/preview`, `/plan/execute`, `/workflow/resume`. FE polls `/progress`.

**Related docs:**
- `PREVIEW_EXECUTE_FE_QUICK_GUIDE.md` — current sync preview/execute behavior
- `WORKFLOW_BUILD_EXECUTE_TIMEOUT_FRONTEND_GUIDE.md` — why sync HTTP fails at 20–100 min
- `WORKFLOW_BUILDER_CREDIT_FLOW_README.md` — credits, checkpoints, `/workflow/resume`
- `WORKFLOW_PROGRESS_FRONTEND_HANDOFF.md` — `/workflow/progress` fields
- `langgraph/main.py` + `langgraph/worker.py` — **runtime** job pattern (run built workflow only)

---

## 0. Implementation task list (tracking)

Use this checklist to track implementation. Update status as work lands: `[ ]` pending · `[~]` in progress · `[x]` done · `[-]` cancelled / deferred.

**Owner legend:** `BE` = backend/agent · `WK` = worker (PM2 104) · `FE` = frontend · `OPS` = deploy/env · `DOC` = docs

### Phase 1 — Foundation

| ID | Task | Owner | Status |
|----|------|-------|--------|
| T1.1 | Create Mongo collection `workflow_build_jobs` + indexes (`workflowId+subscriberId+jobType+status`, `status+createdAt`) | BE | [x] |
| T1.2 | Create package `backend/build_services/` with `__init__.py` | BE | [x] |
| T1.3 | Implement `build_services/job.py` — create job, idempotent active lookup, status updates | BE | [x] |
| T1.4 | Enqueue to Valkey via **new** `BUILD_STREAM_NAME` only (do **not** change `VALKEY_STREAM_NAME`) | BE | [x] |
| T1.5 | Add `POST /agentic/workflow/jobs` on `agent_new.py` → **202** `{ jobId, workflowId, jobType, status }` | BE | [x] |
| T1.6 | Implement `build_services/progress.py` — include `jobId`, `jobType`, `jobStatus`, `resultReady`, `jobError` | BE | [x] |
| T1.7 | Wire `POST /agentic/workflow/progress` to `build_services/progress.py` (primary FE poll — no `/jobs/{id}` poll) | BE | [x] |
| T1.8 | Extend `langgraph/worker.py`: setup build group; dual poll; `process_build_task` stub → mark job `COMPLETED` | WK | [x] |
| T1.9 | Cap build concurrency (`MAX_BUILD_WORKERS`); keep runtime `process_task` unchanged | WK | [x] |
| T1.10 | Worker job helpers under `langgraph/services/build_job.py` (no sibling `backend/` on exec deploy) | WK | [x] |
| T1.11 | Document `BUILD_STREAM_NAME`, `BUILD_GROUP_NAME`, `MAX_BUILD_WORKERS` in `guide-to-run-backend.txt` | DOC | [x] |
| T1.12 | Smoke: enqueue job → worker stub completes → `/progress` shows `COMPLETED` | BE/WK | [x] |

### Phase 2 — Execute async

| ID | Task | Owner | Status |
|----|------|-------|--------|
| T2.1 | Extract execute runner under `langgraph/` (callable from worker; not from sibling `backend/`) | WK | [x] |
| T2.2 | Worker `process_build_task` calls execute runner for `action=execute` | WK | [x] |
| T2.3 | Checkpoint writes during execute (phase start / gap node) unchanged in behavior | BE/WK | [x] |
| T2.4 | On success: job `COMPLETED`, `Process_Configs` → `draft`, store compact `result` | BE/WK | [x] |
| T2.5 | On `BillingError`: job `PAUSED_CREDIT` + existing checkpoint / creditPauseStatus path | BE/WK | [x] |
| T2.6 | On failure: job `FAILED` + `jobError`; poison-pill `xack` | WK | [x] |
| T2.7 | Keep sync `POST /plan/execute` working (no deprecation) | BE | [x] |
| T2.8 | FE handoff note: enqueue execute + poll **`POST /progress` only** | DOC/FE | [x] |
| T2.9 | E2E: disconnect FE mid-execute; poll recovers to `draft` / `COMPLETED` | BE/FE | [x] |

### Phase 3 — Preview async

| ID | Task | Owner | Status |
|----|------|-------|--------|
| T3.1 | Extract preview runner under `langgraph/` (callable from worker) | WK | [x] |
| T3.2 | Worker handles `action=preview` → `preview.run` | WK | [x] |
| T3.3 | On success: job `COMPLETED`, `Process_Configs` → `preview` | BE/WK | [x] |
| T3.4 | Keep sync `POST /scratch/preview` working (no deprecation) | BE | [x] |
| T3.5 | Update `PREVIEW_EXECUTE_FE_QUICK_GUIDE.md` (jobs + `/progress`; sync still documented) | DOC | [x] |
| T3.6 | E2E: preview job + poll until graph ready | BE/FE | [x] |

### Phase 4 — Resume async + hardening

| ID | Task | Owner | Status |
|----|------|-------|--------|
| T4.1 | Extract resume runner under `langgraph/` | WK | [x] |
| T4.2 | Async resume via `POST /workflow/jobs` `jobType=resume` → **202**; sync `/workflow/resume` unchanged | BE | [x] |
| T4.3 | Worker handles `action=resume` | WK | [x] |
| T4.4 | Idempotency: one `QUEUED`/`RUNNING` job per `(workflowId, subscriberId, jobType)` | BE | [x] |
| T4.5 | Stale `RUNNING` job recovery on worker startup | WK | [x] |
| T4.6 | Confirm sync preview/execute still available (explicitly **no** deprecate step) | BE | [x] |
| T4.7 | Credit pause → top-up → resume job → poll `/progress` E2E | BE/FE | [x] |

### Phase 5 — Optional polish (defer unless needed)

| ID | Task | Owner | Status |
|----|------|-------|--------|
| T5.1 | Optional SSE events on `/workflow/stream` — **not** required for FE v1 | BE | [ ] |
| T5.2 | Cancel job for `QUEUED` only | BE | [ ] |
| T5.3 | Metrics: duration, queue depth, failure rate | OPS | [ ] |
| T5.4 | Do **not** add `GET /workflow/jobs/{workflowId}` unless product later requires it | BE | [-] |

### Explicit non-tasks (do not do)

| ID | Item | Status |
|----|------|--------|
| N1 | Rename / replace existing `VALKEY_STREAM_NAME` | [-] |
| N2 | Put preview/execute actions on runtime `agentic_tasks` stream | [-] |
| N3 | New PM2 app (e.g. 105) for build worker | [-] |
| N4 | Dump new modules into `backend/services/` (use `backend/build_services/`) | [-] |
| N5 | Require FE to use `/workflow/stream` for status | [-] |
| N6 | Deprecate sync `/scratch/preview` or `/plan/execute` | [-] |

**Progress summary:** Phase 1 `12/12` · Phase 2 `9/9` · Phase 3 `6/6` · Phase 4 `7/7` · Phase 5 deferred

---

## 1. Problem statement

Today, `POST /agentic/plan/scratch/preview` and `POST /agentic/plan/execute` are **synchronous HTTP** calls that can run **8–100+ minutes**. The server holds the connection until work finishes.

| Failure mode | Cause |
|--------------|--------|
| FE spinner never ends | Client/proxy timeout; no JSON response |
| No `200 OK` in API logs | Client disconnected before response written |
| Build succeeded in Mongo | Work completes; HTTP layer already dead |
| `/workflow/progress` stuck at 0% | Progress tied to in-process checkpoints; no durable job boundary |

**Production requirement:** Work must **outlive** the browser tab, proxy, and API process. Frontend should **enqueue** and **poll** (or optional SSE), not wait on one long POST.

---

## 2. Goals and non-goals

### Goals

- **202 Accepted** for preview and execute enqueue; worker runs build in background.
- **Durable progress** in Mongo (job status + existing checkpoints).
- **Poll API for FE:** **`POST /agentic/workflow/progress` only** (extended with `jobStatus`).
- **Credit pause / resume** as jobs (not long sync `/workflow/resume`).
- Agent build API under **`backend/build_services/`** (PM2 76). Worker-side modules under **`langgraph/services/`** (PM2 104). No fourth PM2 app; no sibling `backend/` on exec.
- **Keep** existing sync `POST /scratch/preview` and `POST /plan/execute` available (no deprecation).

### Non-goals (this phase)

- Replacing LangGraph `instances/*` runtime (already async via `langgraph/worker.py`).
- Using in-memory `JobManager` in `agent_new.py` (Angular screen generation only — not durable).
- Putting build jobs on the same Valkey stream as `agentic_tasks` (runtime) — **two streams, one process**.
- Creating a new PM2 role (e.g. job 105) for build only — fit under existing **104**.
- Requiring FE to use `/workflow/stream` or a separate `/workflow/jobs/{id}` poll for status.
- Deprecating or removing sync preview/execute endpoints.

---

## 3. Architecture overview

```text
┌─────────────┐     POST /workflow/jobs      ┌──────────────────┐
│  Frontend   │ ───────────────────────────► │  agent_new.py    │
│             │ ◄── 202 { jobId, QUEUED } ── │  (PM2 ~76)       │
└──────┬──────┘                              └────────┬─────────┘
       │                                                │
       │ poll POST /workflow/progress                   │ xadd BUILD_STREAM_NAME (new only)
       │ (every 5–10s)                                  ▼
       │                                       ┌──────────────────┐
       │                                       │ Valkey           │
       │                                       │  VALKEY_STREAM_  │◄── langgraph/main.py (PM2 ~103)
       │                                       │  NAME (existing, │    unchanged — do not rename
       │                                       │  do not change)  │
       │                                       │  BUILD_STREAM_   │
       │                                       │  NAME (new only) │
       │                                       └────────┬─────────┘
       │                                                │
       │                                                │ xreadgroup BOTH streams
       │                                                ▼
       │                                       ┌──────────────────┐
       │                                       │ worker.py        │
       │                                       │ (PM2 ~104)       │
       │                                       │  ├─ runtime H    │
       │                                       │  └─ build H      │
       │                                       └────────┬─────────┘
       │                                                │
       │                                                │ read/write
       ▼                                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ MongoDB                                                           │
│  workflow_build_jobs          (QUEUED → RUNNING → terminal)       │
│  workflow_build_checkpoints   (fine-grained progress — existing)  │
│  Process_Configs              (preview / draft / building — existing)│
│  instances                    (runtime — unchanged)               │
└──────────────────────────────────────────────────────────────────┘
```

### Existing server roles (no new PM2 tier)

| PM2 / role (example) | Script | Responsibility after this plan |
|----------------------|--------|--------------------------------|
| **76** | `agent_new.py` | Build API: enqueue `POST /workflow/jobs`, poll `/progress` |
| **103** | `langgraph/main.py` | Runtime API: `/instances/*` enqueue (existing stream — unchanged) |
| **104** | `langgraph/worker.py` | **Both** consumers: runtime + build (**dual stream**, one process) |

### Streams vs process (important)

| Concern | Valkey stream | Handler in `worker.py` | Mongo |
|---------|---------------|------------------------|-------|
| **Build** (preview / execute / resume_build) | **New** `BUILD_STREAM_NAME` (e.g. `build_tasks`) | `process_build_task()` | `workflow_build_jobs`, checkpoints, `Process_Configs` |
| **Run** (start / resume instance) | **Existing** `VALKEY_STREAM_NAME` (default `agentic_tasks`) — **do not rename** | `process_task()` (today) | `instances` |

**Same PM2 app (104), same `worker.py` file, two streams, two handlers.**  
Do **not** change the existing runtime stream name, group, or payload shape.  
Do **not** put `action: preview` / `execute` on the existing runtime stream.

---

## 4. Data model

### 4.1 New collection: `workflow_build_jobs`

One **active** build job per `(workflowId, subscriberId, jobType)` at a time (enforce in API).

```json
{
  "jobId": "job_a1b2c3d4",
  "workflowId": "ixTGlTzT_sb_5beaabd82ac6767c86dc316c",
  "subscriberId": "5beaabd82ac6767c86dc316c",
  "orgId": "5c495dbfffa2a85b2c19a77f",
  "userId": "",
  "jobType": "preview",
  "status": "QUEUED",
  "pipelineStage": "preview",
  "request": {},
  "result": null,
  "error": null,
  "attempt": 1,
  "workerId": null,
  "valkeyMessageId": null,
  "createdAt": "2026-07-17T12:00:00.000Z",
  "startedAt": null,
  "finishedAt": null,
  "updatedAt": "2026-07-17T12:00:00.000Z"
}
```

**`jobType` values:**

| Value | Runs |
|-------|------|
| `preview` | `POST /scratch/preview` logic |
| `execute` | `POST /plan/execute` logic |
| `resume` | `POST /workflow/resume` logic (after `paused_credit`) |

**`status` values:**

| Status | Meaning |
|--------|---------|
| `QUEUED` | Accepted; waiting for worker |
| `RUNNING` | Worker claimed job |
| `COMPLETED` | Success; `result` or `Process_Configs` updated |
| `FAILED` | Terminal error; `error` populated |
| `PAUSED_CREDIT` | Billing pause; checkpoint + resume job |

**Indexes:**

- `{ workflowId: 1, subscriberId: 1, jobType: 1, status: 1 }` — idempotency / active job lookup
- `{ status: 1, createdAt: 1 }` — worker recovery (optional Mongo poll fallback)

### 4.2 Existing: `workflow_build_checkpoints`

Keep as **fine-grained progress** during RUNNING (phase graphs, `current_node_id`, `pipeline_stage`, `request_context`). Worker continues calling `write_checkpoint()` on the same schedule as today’s in-process execute.

See `backend/services/build_checkpoint.py`.

### 4.3 Existing: `Process_Configs`

Coarse workflow state (unchanged semantics):

| `agentic_process.status` | After job |
|--------------------------|-----------|
| `building` | Execute running |
| `preview` | Preview job complete |
| `draft` | Execute job complete |
| `paused_credit` | Credit pause |

Billing fields (`predictedCost`, `budgetSpentUsd`, `reservationStatus`) remain on this document.

---

## 5. API design

### 5.1 Enqueue build job (new)

```http
POST /aiqod-agent/agent/agentic/workflow/jobs
Content-Type: application/json
x-subscriber-id: {subscriberId}
```

**Request:**

```json
{
  "jobType": "preview",
  "workflowId": "ixTGlTzT_sb_5beaabd82ac6767c86dc316c",
  "projectId": "ixTGlTzT_sb_5beaabd82ac6767c86dc316c",
  "subscriberId": "5beaabd82ac6767c86dc316c",
  "orgId": "5c495dbfffa2a85b2c19a77f",
  "userId": "",
  "payload": {}
}
```

- **`jobType: "preview"`** — `payload` = same body as `POST /agentic/plan/scratch/preview` (`AgenticPlanRequest`).
- **`jobType: "execute"`** — `payload` = same body as `POST /agentic/plan/execute` (`AgenticPlanExecuteRequest`).

**Response `202 Accepted`:**

```json
{
  "success": true,
  "jobId": "job_a1b2c3d4",
  "workflowId": "ixTGlTzT_sb_5beaabd82ac6767c86dc316c",
  "jobType": "preview",
  "status": "QUEUED",
  "progressEndpoint": "/aiqod-agent/agent/agentic/workflow/progress"
}
```

**Idempotency:** If a job for the same `(workflowId, subscriberId, jobType)` is already `QUEUED` or `RUNNING`, return **200/202** with the existing `jobId` (do not start a second build).

**Validation (same as today):**

- `POST /agentic/workflow/register` must have run (predicted cost reserved).
- Preview: exactly one of `prompt` or `steps` in payload.
- Execute: `phaseGraphs` from preview, unchanged.

### 5.2 Which status endpoint does FE use? (pick one — no confusion)

For async build status after enqueue, FE uses **exactly one** primary API:

| Endpoint | Role in this plan |
|----------|-------------------|
| **`POST /agentic/workflow/progress`** | **Primary — use this.** Poll every 5–10s until `jobStatus` is terminal. Extended with `jobId`, `jobType`, `jobStatus`, etc. |
| `GET /agentic/workflow/stream/{workflowId}` | **Not required for v1.** Optional later for push (`completed` / `paused_credit`). Never replaces progress poll for reconnect / percent / gaps. |
| `GET /agentic/workflow/jobs/{workflowId}?subscriberId=…` | **Not in v1.** Do not add a second poll API. Avoids two sources of truth. |

**FE contract (v1):**

```text
POST /agentic/workflow/jobs     → 202 { jobId, workflowId, status: QUEUED }
POST /agentic/workflow/progress → poll until jobStatus in COMPLETED | FAILED | PAUSED_CREDIT
```

Do **not** open `/workflow/stream` unless you later opt into optional SSE (phase 5).  
Do **not** poll a separate `/workflow/jobs/{id}` endpoint.

### 5.3 Poll progress (extend existing — primary)

```http
POST /aiqod-agent/agent/agentic/workflow/progress
{ "workflowId", "subscriberId", "orgId?", "userId?" }
```

**Add fields to response:**

| Field | Type | Description |
|-------|------|-------------|
| `jobId` | string? | Active or last job |
| `jobType` | `preview` \| `execute` \| `resume`? | |
| `jobStatus` | `QUEUED` \| `RUNNING` \| `COMPLETED` \| `FAILED` \| `PAUSED_CREDIT`? | Primary FE gate |
| `resultReady` | boolean | `true` when `COMPLETED` and result available |
| `jobError` | object? | When `FAILED` |

Existing fields (`gapProgress`, `pipelineStage`, `billing`, `stoppedAt`, `canResume`) stay; worker must keep checkpoints updated during `RUNNING`.

### 5.4 Resume (change to async)

Today `POST /workflow/resume` can run for a long time. After jobs:

1. Validate `paused_credit` + checkpoint (same as today).
2. Create `jobType: "resume"`, `status: QUEUED`.
3. Enqueue Valkey message `action: resume`.
4. Return **202** immediately.
5. Worker runs existing resume branches (`stage0` / `preview` / `execute`).
6. FE continues polling **`POST /workflow/progress`** (same as after enqueue).

### 5.5 Sync preview / execute — keep (no deprecation)

| Endpoint | Status |
|----------|--------|
| `POST /agentic/plan/scratch/preview` | **Keep** — still available |
| `POST /agentic/plan/execute` | **Keep** — still available |

Preferred production path for FE: **`POST /workflow/jobs` + poll `/progress`**.  
Sync endpoints remain for curl, debugging, or callers that still use the long HTTP wait. No feature-flag removal in this plan.

### 5.6 Optional SSE (phase 5 only — not FE v1)

`GET /agentic/workflow/stream/{workflowId}` may later publish:

- `type: "progress"` — on checkpoint write
- `type: "completed"` / `paused_credit` / `error`

**Still optional.** Source of truth for UI status remains **`POST /workflow/progress`**.

---

## 6. Worker design (extend existing `langgraph/worker.py` on PM2 104)

**Preferred deployment:** do **not** add a fourth PM2 app. Put build consumption **inside** the same `worker.py` that already runs as `aiqod-dev-worker` (PM2 id ~104).

### 6.0 What “inside worker.py” means

| Approach | Status |
|----------|--------|
| Same **file** (`worker.py`), same **PM2 104**, **two Valkey streams**, two handlers | **Preferred** |
| Same stream (`agentic_tasks`) with `action: preview` / `execute` | **Avoid** — wrong payload / Mongo model |
| Separate `build_worker.py` + new PM2 process | Optional only if ops wants isolation later |
| In-memory `JobManager` on API (76) | **Avoid** — not durable |

```text
PM2 104 → worker.py (one OS process, one PM2 describe)
            │
            ├─ Stream A: VALKEY_STREAM_NAME (existing, unchanged) → process_task()
            └─ Stream B: BUILD_STREAM_NAME (new)                  → process_build_task()
```

Agent enqueue/progress stays in `backend/build_services/`. Worker claim/run modules stay under `langgraph/` (e.g. `services/build_job.py`; later execute/preview runners likewise).  
`worker.py` only **dispatches** (same pattern as today’s call to `WorkflowExecutionController`).

### 6.1 Configuration

**Do not change** existing runtime Valkey settings. Keep whatever is already on PM2 104 / `.env` today:

| Env var (existing — leave as-is) | Code default | Notes |
|----------------------------------|--------------|-------|
| `VALKEY_HOST` / `VALKEY_PORT` | existing | Unchanged |
| `VALKEY_STREAM_NAME` | `agentic_tasks` | **Do not rename** — use current server value |
| `VALKEY_GROUP_NAME` | `agentic_workers` | **Do not rename** — use current server value |
| `CONSUMER_NAME` | `worker_node_1` | Unchanged |
| `MONGO_URI` / `MONGO_DB_NAME` | existing | Unchanged |
| `CLAIM_TIMEOUT_MS` | `600000` | Unchanged |

**Only add** these new vars for the build queue:

| Env var (new) | Suggested default | Notes |
|---------------|-------------------|-------|
| `BUILD_STREAM_NAME` | `build_tasks` | New stream only — pick any unused name; does not replace runtime stream |
| `BUILD_GROUP_NAME` | `build_workers` | New consumer group |
| `MAX_BUILD_WORKERS` | `1` | Cap long builds so runtime stays responsive |

### 6.2 Valkey message payload (build stream only)

```json
{
  "action": "preview",
  "job_id": "job_a1b2c3d4",
  "workflow_id": "ixTGlTzT_sb_…",
  "subscriber_id": "5beaabd82ac6767c86dc316c"
}
```

`action` on **build** stream: `preview` | `execute` | `resume`  
`action` on **runtime** stream: `start` | `resume` (unchanged; `resume` here = instance resume)

### 6.3 Dual-stream loop (extend `worker_loop`)

1. `xgroup_create` on **both** `STREAM_NAME` and `BUILD_STREAM_NAME` (idempotent).
2. Single loop: `xreadgroup` with **both** streams:

```python
streams = {
    STREAM_NAME: ">",
    BUILD_STREAM_NAME: ">",
}
messages = valkey_client.xreadgroup(
    groupname=…,  # note: xreadgroup is per-group — see implementation note below
    consumername=CONSUMER_NAME,
    streams=streams,
    count=1,
    block=2000,
)
# Route by stream name → process_task vs process_build_task
```

**Implementation note:** Valkey `xreadgroup` takes one `groupname`. Use either:

- **(A)** Two sequential `xreadgroup` calls (runtime group + build group) in the same loop with short `block`, or  
- **(B)** One group per stream with matching names and alternate polls.

Claim / pending recovery runs for **both** streams.

3. Shared `ThreadPoolExecutor`; prefer a **small dedicated pool** for build (`MAX_BUILD_WORKERS`) so a 60‑min execute does not starve runtime `start`/`resume`.

### 6.4 `process_build_task` steps

1. Load job from Mongo by `job_id`; if not `QUEUED`, `xack` and skip.
2. `findOneAndUpdate` job → `RUNNING`, set `startedAt`, `workerId`.
3. Initialize billing context (same as API / `_init_agentic_workflow_billing`).
4. Run async services from sync worker:

```python
def process_build_task(message_id, message_data):
    import asyncio
    try:
        asyncio.run(run_build_job(message_data))  # thin wrapper → services
        valkey_client.xack(BUILD_STREAM_NAME, BUILD_GROUP_NAME, message_id)
    except Exception:
        # mark job FAILED in Mongo, then xack (poison-pill)
        ...
```

5. Dispatch by `action`:
   - `preview` → `WorkflowBuildPreviewService.run(request)`
   - `execute` → `WorkflowBuildExecuteService.run(request)`
   - `resume` → `WorkflowBuildResumeService.run(workflow_id, subscriber_id)`
6. On success:
   - Job → `COMPLETED`, store compact `result`.
   - Update `Process_Configs` (`preview` / `draft`).
   - Delete checkpoint on full execute success (same as today).
   - Publish `workflow_stream` `completed` when possible.
7. On `BillingError`:
   - Job → `PAUSED_CREDIT`; checkpoint + `creditPauseStatus`.
   - Publish `paused_credit` SSE when possible.
8. On other exception → job `FAILED`, `xack` after mark.

Prefix logs clearly: `[Build]` vs `[Runtime]` / existing `[CONSUMER_NAME]`.

### 6.5 Stale job recovery

On worker startup (alongside `recover_stale_instances`):

- Find `workflow_build_jobs` with `status: RUNNING` and `updatedAt` older than ~30 minutes.
- Re-queue to `QUEUED` + `xadd` (or fail after N `attempt`s).

### 6.6 Deploy artifact for PM2 104

Today 104 cwd is typically `…/aiqod-dev-exec` (langgraph only — **no** sibling `backend/`).

- Agent enqueue/progress: `backend/build_services/` (PM2 76).
- Worker claim/complete/fail (+ later execute/preview runners): **`langgraph/services/`** (and related modules under langgraph only). Ship those files with the exec deploy.

Do **not** import `backend/` from `worker.py` / `main.py`. Do **not** copy-paste thousands of lines of `agent_new.py` into `worker.py` — extract build runners into langgraph modules as phases land.

### 6.7 Tradeoffs of merging into `worker.py`

| Pros | Cons |
|------|------|
| No new PM2 id / role | 104 process RAM grows (loads build stack) |
| One Valkey/Mongo env | Long build can contend with runtime — mitigate with `MAX_BUILD_WORKERS` |
| One ops story (`pm2 logs aiqod-dev-worker`) | Exec deploy must include build service modules |
| Fits current 76 / 103 / 104 topology | Logs mix unless prefixed |

---

## 7. Code refactor

### 7.1 Packages: agent vs worker

**Agent (PM2 76)** — `backend/build_services/` (enqueue + progress; sync paths stay in `agent_new.py` until extracted):

```text
backend/build_services/
  __init__.py
  job.py          # create / enqueue / status (workflow_build_jobs)
  progress.py     # job-aware progress for POST /workflow/progress
  # Phase 2+: thin wrappers or shared contracts only if needed by the API
```

**Worker (PM2 104)** — everything under `langgraph/` (exec deploy has no sibling `backend/`):

```text
langgraph/services/build_job.py      # claim / complete / fail / pause_credit
langgraph/services/build_execute.py  # Phase 2: HTTP → agent /plan/execute
langgraph/services/build_preview.py  # Phase 3: HTTP → agent /scratch/preview
langgraph/services/build_consumer.py # claim + dispatch
# Phase 3+: preview runner; Phase 4: resume runner
```

| Side | File | Responsibility |
|------|------|----------------|
| Agent | `build_services/job.py` | Create job, enqueue Valkey `BUILD_STREAM_NAME`, idempotency |
| Agent | `build_services/progress.py` | Progress response including `jobStatus` |
| Worker | `services/build_job.py` | Claim / complete / fail / pause_credit |
| Worker | `services/build_execute.py` | Phase 2: long HTTP → agent sync `/plan/execute` |
| Worker | `services/build_consumer.py` | Build stream poll + dispatch |

Shared Mongo helpers on the agent that already exist stay in `backend/services/` and are imported by agent code only:

| Existing (leave in place) | Used for |
|---------------------------|----------|
| `services/build_checkpoint.py` | Checkpoint read/write |
| `services/workflow_budget_service.py` | Billing / Process_Configs |
| `services/workflow_stream.py` | Optional SSE publish |

When Phase 2 extracts execute/preview, put the **worker-callable** runners under `langgraph/` (not imported from `backend/`).

### 7.2 Changes to `agent_new.py` (PM2 ~76)

- Add route `POST /agentic/workflow/jobs` → `build_services.job.enqueue(...)` → `xadd` **build** stream only.
- Keep sync `POST /scratch/preview` and `POST /plan/execute` (call into `build_services` or keep current paths).
- `workflow_resume` → enqueue resume **build** job (after phase 2–3); still return 202.
- `POST /workflow/progress` → `build_services.progress.build_workflow_progress(...)`.
- Billing middleware stays on API; worker sets billing context when claiming a job.

### 7.3 Changes to `langgraph/worker.py` (PM2 ~104)

- Add build stream env + group setup.
- Dual poll / route to `process_build_task`.
- Call into `backend.build_services` via `asyncio.run(...)`.
- Keep `process_task` for `start` / `resume` **unchanged** in behavior.

### 7.4 No change to `langgraph/main.py` (PM2 ~103)

Runtime API and existing `VALKEY_STREAM_NAME` enqueue stay as today (unchanged).

---

## 8. Frontend contract (summary)

### 8.1 Preview flow

```text
POST /workflow/register
Stage 0 (sync — short)
POST /workflow/jobs { jobType: "preview", payload: { … } }
  → 202, jobId

poll POST /workflow/progress every 5–10s until:
  jobStatus === "COMPLETED" && workflowStatus === "preview"
  OR jobStatus === "PAUSED_CREDIT" → recharge → POST /workflow/resume (202)

Show phaseGraphs from progress result or GET /configdetails
```

### 8.2 Execute flow

```text
User confirms graph
POST /workflow/jobs { jobType: "execute", payload: { phaseGraphs, … } }
  → 202

poll until jobStatus === "COMPLETED" && workflowStatus === "draft"
GET /configdetails → build-complete UI
Enable Run workflow → langgraph /instances/* (unchanged)
```

### 8.3 UI bindings

| Element | Field |
|---------|--------|
| Queue / running badge | `jobStatus` |
| Headline | `stoppedAt` |
| Current node | `phaseProgress.currentNodeId` |
| Screens/tools | `gapProgress.resolved` / `gapProgress.total` |
| Budget | `billing.budgetSpentUsd` / `predictedCost` |
| Done | `jobStatus === "COMPLETED"` |
| Error | `jobStatus === "FAILED"` → `jobError` |

Do **not** wait on HTTP response body from enqueue. Prefer jobs + **`POST /workflow/progress`**. Sync `/execute` remains available but is not the recommended FE path.

---

## 9. Credit and billing

Unchanged business rules; only **execution context** moves to worker:

- Reservation on register (`predictedCost`).
- `workflow_reservation_release_scope` around execute in worker.
- `402` / `BillingError` → `PAUSED_CREDIT` job + checkpoint.
- Resume after top-up → **resume job**, not sync HTTP.

See `WORKFLOW_BUILDER_CREDIT_FLOW_README.md`.

---

## 10. Implementation phases

Detail checklists live in **§0 Implementation task list (tracking)** (`T1.*` … `T5.*`). Summary:

### Phase 1 — Foundation
T1.1–T1.12 — jobs collection, `build_services/`, enqueue API, `/progress` + `jobStatus`, dual-stream worker stub.

### Phase 2 — Execute async
T2.1–T2.9 — `build_services/execute.py`, worker runs execute, draft on success, FE polls `/progress`.

### Phase 3 — Preview async
T3.1–T3.6 — `build_services/preview.py`, worker runs preview, keep sync preview.

### Phase 4 — Resume async + hardening
T4.1–T4.7 — `build_services/resume.py`, 202 resume enqueue, idempotency, stale recovery; **no** sync deprecation.

### Phase 5 — Optional polish
T5.1–T5.4 — SSE / cancel / metrics optional; no `/jobs/{id}` poll unless requested.

---

## 11. Testing plan

| Test | Method |
|------|--------|
| Enqueue returns 202 in < 1s | API integration |
| Worker picks up and completes simple 1-node workflow | E2E local |
| FE disconnect during build; poll shows COMPLETED | Manual |
| Credit pause mid-execute → PAUSED_CREDIT → resume job | Billing test |
| Duplicate enqueue → same jobId | Idempotency |
| Kill worker mid-RUNNING → recovery re-queues | Chaos |
| Runtime unchanged | LangGraph smoke test |

**curl smoke (after phase 2):**

```bash
AGENT="http://localhost:5010/dev/aiqod-agent/agent"
SUB="…"
WF="…"

# Enqueue execute
curl -sS -X POST "$AGENT/agentic/workflow/jobs" \
  -H "Content-Type: application/json" \
  -H "x-subscriber-id: $SUB" \
  -d "{\"jobType\":\"execute\",\"workflowId\":\"$WF\",\"subscriberId\":\"$SUB\",\"payload\":{…}}"

# Poll
curl -sS -X POST "$AGENT/agentic/workflow/progress" \
  -H "Content-Type: application/json" \
  -d "{\"workflowId\":\"$WF\",\"subscriberId\":\"$SUB\"}" | jq .jobStatus
```

---

## 12. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Two workers run same job | `findOneAndUpdate` claim; unique active job per workflow+type |
| Checkpoint write fails (Mongo blip) | Non-fatal logs; job `updatedAt` heartbeat; billing still updates |
| Valkey down | API returns 503; do not mark QUEUED without xadd (or use transactional outbox later) |
| Large `result` on job doc | Store pointer only; full graphs in `Process_Configs` |
| Dashboard execute path | Mirror job pattern for `/agentic/dashboard/plan/execute` in phase 3+ |
| Build starves runtime on 104 | Separate thread pool / `MAX_BUILD_WORKERS`; RAM guard already in worker |
| Schema drift agent vs worker | Keep `workflow_build_jobs` field/status names aligned in `backend/build_services/job.py` and `langgraph/services/build_job.py` |

---

## 13. Decision log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Queue technology | Valkey stream | Same ops as langgraph; fast dispatch |
| Runtime stream | **Leave `VALKEY_STREAM_NAME` unchanged** | Do not rename existing queue |
| Build stream | **New** `BUILD_STREAM_NAME` only | Separate from runtime; do not mix actions |
| Worker hosting | **Extend PM2 104 `worker.py`** (dual stream) | No new PM2 role; fits 76 / 103 / 104 topology |
| Separate `build_worker.py` process | Not required for v1 | Optional later if isolation needed |
| Build logic location | Agent: `backend/build_services/`; Worker: `langgraph/services/` | Exec deploy has no sibling `backend/` |
| Job key | `workflowId` + `jobType` | Already stable after register |
| FE status API | **`POST /workflow/progress` only** | One poll source; no `/jobs/{id}` alias in v1 |
| `/workflow/stream` | Optional later | Push nicety; not required for progress UI |
| Sync preview/execute | **Keep** | No deprecation in this plan |
| In-memory JobManager | Not used | Not durable for 20–100 min builds |

---

## 14. PM2 deployment notes (dev example)

Current worker (illustrative):

```text
pm2 id 104  name=aiqod-dev-worker
script = …/aiqod-dev-exec/worker.py
```

**After change:** same PM2 id / name / script path; **restart** after deploying dual-stream `worker.py` + build service imports. **Only append** new env vars — do **not** edit `VALKEY_STREAM_NAME` / `VALKEY_GROUP_NAME`:

```bash
# on worker host — ADD only (leave existing VALKEY_* as they are today)
BUILD_STREAM_NAME=build_tasks
BUILD_GROUP_NAME=build_workers
MAX_BUILD_WORKERS=1
```

```bash
sudo pm2 restart 104
sudo pm2 logs aiqod-dev-worker --lines 100
# expect: runtime group + build group created; [Build] / [Runtime] lines
```

**Do not** create `pm2 start build_worker.py` unless ops later chooses isolation.

---

## 15. References (code)

| Path | Role |
|------|------|
| `backend/agent_new.py` | Current sync preview/execute/resume; enqueue jobs |
| `backend/build_services/` | Agent: job enqueue + progress fields |
| `backend/services/build_checkpoint.py` | Checkpoint persistence (agent) |
| `backend/services/workflow_stream.py` | In-process SSE pub/sub (optional) |
| `backend/services/workflow_budget_service.py` | Billing / Process_Configs |
| `langgraph/worker.py` | Dual-stream consumer (runtime + build) |
| `langgraph/services/build_job.py` | Worker claim/complete/fail for build jobs |
| `langgraph/main.py` | Runtime enqueue + SSE (unchanged) |

---

## 16. Open questions

1. **Single `POST /workflow/jobs` vs separate `/jobs/preview` and `/jobs/execute`?**  
   Plan: one endpoint with `jobType` (simpler FE client).

2. **Store full preview/execute response on job doc vs only `configdetails`?**  
   Plan: preview stores compact `result`; large graphs always in `Process_Configs`.

3. **How to ship build code to the exec deploy tree?**  
   Keep worker modules under `langgraph/` only (e.g. `services/build_job.py`). Do not require sibling `backend/` on PM2 104.

4. **Mongo-only queue for v1 without Valkey?**  
   Not preferred — Valkey already used by 104; add a **new** build stream via `BUILD_STREAM_NAME` without changing the existing runtime stream.

---

*Last updated: 2026-07-18 — added §0 implementation task list (T1–T5)*
