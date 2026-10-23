# Workflow Runtime AI Observability — Analysis & Plan

**Date:** 21 August 2026 (rev: nested tool/UI LLM capture)  
**Aim:** Observe **workflow runs** correctly for **FREEZE and DYNAMIC**, including LLM calls that happen **inside generated tools** and **UI-screen backend functions** (written at build time, executed at runtime).

**Sources reviewed:**  
- Technical Design v2 (`readme/AI_Observability_Technical_Design (4).docx`)  
- Runtime: `langgraph/app/executor.py`, `langgraph/worker.py`, `langgraph/main.py`  
- Build artifacts / codegen: `backend/scratch_graph_planner.py`, `backend/services/gap_tool_generator.py`, `backend/services/backend_generator.py`, `backend/agent_new.py` (`/dynamic/execute`)  
- Reference (patterns only): `aiobservabilitysrc/ai_observability/*`

---

## Part A — Analysis

### A.1 What “runtime” means here

```
main.py  POST /instances/start|execute|resume
    → Valkey stream agentic_tasks
    → worker.py process_task (start | resume)
    → WorkflowExecutionController
         _load_graph(workflow_id)     # disk .py + inject execute_dynamic_tool
         _run_and_log_stream(...)     # graph.stream + history + SSE + tokens
```

`/scratch/preview` and `/plan/execute` **build** graphs; they do not run tenant workflows.  
Build-time planner LLMs (`scratch_graph_planner`, gap-tool codegen, backend_generator) are **out of scope** for runtime observability.

---

### A.2 Three places LLM work can happen in a single workflow run

This is the critical correction to earlier plans.

```
┌──────────────────────────── Worker process (langgraph) ────────────────────────────┐
│ graph.stream                                                                       │
│   ├─ [DYNAMIC] LangChain ChatGoogleGenerativeAI via _get_llm().invoke              │
│   │            → visible to LangGraph/LangChain callbacks if wired                 │
│   ├─ tool node → execute_dynamic_tool → LocalToolRegistry.execute_tool → _run()    │
│   │            → raw google.generativeai / openai / anthropic / httpx              │
│   │            → NOT LangChain → callbacks on graph.stream DO NOT see these        │
│   └─ ui_screen node → interrupt()  [worker PAUSED]                                 │
└──────────────────────────────────────────┬─────────────────────────────────────────┘
                                           │ SSE ui_request
                                           ▼
┌──────────────────────────── Browser + Agent process ───────────────────────────────┐
│ Angular screen → POST /aiqod-agent/agent/dynamic/execute/{project_id}               │
│   → FunctionWorkerPool → SandboxFunctionExecutor (subprocess exec)                 │
│   → generated backend function may call Gemini/OpenAI via request_meta.credentials │
│   → DIFFERENT process from worker → graph callbacks NEVER see these                │
│ On submit → POST /instances/{execution_id}/resume → worker continues               │
└────────────────────────────────────────────────────────────────────────────────────┘
```

| Call site | FREEZE | DYNAMIC | Process | Seen by `graph.stream` callbacks? |
|-----------|--------|---------|---------|-----------------------------------|
| In-graph agent LLM (`_get_llm`) | No | Yes | Worker | **Yes** (if wired) |
| LLM inside **tool** `function_code` | Common | Common | Worker (same thread as tool) | **No** |
| LLM inside **UI backend** function | Common | Common | Agent sandbox subprocess | **No** |

If we only instrument the graph, we **under-count cost, latency, and risk** whenever tools or screens call models — which `gap_tool_generator` / `backend_generator` explicitly allow (Gemini / OpenAI / Anthropic via credentials).

---

### A.3 How tools run (and why nested LLMs are invisible today)

| Step | Location | Behavior |
|------|----------|----------|
| Codegen | `gap_tool_generator.py` | Emits JSON tool with `function_code` that may call `google.generativeai`, `openai`, `anthropic`, or REST |
| Load | `LocalToolRegistry.create_dynamic_tool_class` | `exec` → CrewAI `BaseTool._run(..., request_meta=None)` |
| Bind | `_load_graph` | Injects `execute_dynamic_tool` with `request_meta={project_id, credentials}` |
| Invoke | Graph node → `execute_tool` → `_run` | **Same worker process**; no LLM wrapper |

There is **no** existing hook around SDK calls inside tool bodies.  
(`record_llm_call` in gap_tool_generator is for **build-time** codegen tokens only.)

---

### A.4 How UI backend functions run (and why nested LLMs are invisible today)

| Step | Location | Behavior |
|------|----------|----------|
| Codegen | `backend_generator.py` | Async `def fn(*, params, request_meta=None)` may call LLMs using `request_meta["credentials"]` |
| Pause | Graph `interrupt({screen_id, ui_data, ...})` | Worker publishes SSE; status pending |
| Execute | Browser → `POST …/dynamic/execute/{project_id}` | `execute_dynamic_function_async` in `agent_new.py` → sandbox subprocess |
| Resume | `POST /instances/{id}/resume` | Worker advances with human payload |

While the screen is open, the **worker is paused**. Nested UI LLMs never pass through `executor._run_and_log_stream`.

Angular `request_meta` today is roughly `{ source: 'angular_app', … }` — **`execution_id` is not guaranteed** unless we add it. Correlation is a product requirement for joining UI LLM spans to the workflow trace.

---

### A.5 Can we observe nested tool/UI LLMs? — Answer: **Yes, with dual instrumentation**

LangGraph callbacks alone are **necessary but not sufficient**.

| Nested source | Capturable? | How (for this codebase) |
|---------------|-------------|-------------------------|
| Tool-body LLMs | **Yes** | Same process as worker: wrap `execute_dynamic_tool` / `execute_tool`, plus **SDK monkeypatch** (or shared helper) while `_run` executes |
| UI-backend LLMs | **Yes** | Instrument `/dynamic/execute` (sandbox entry) on the **agent**; correlate with `execution_id` (+ `workflow_id`, `screen_id`, `function_id`) in `request_meta` |
| In-graph DYNAMIC LLM | **Yes** | Callbacks on `graph.stream` + optional `_get_llm` wrap |

#### Capture options ranked

| Rank | Approach | Covers | Feasibility |
|------|----------|--------|-------------|
| **1** | Worker: wrap tools + monkeypatch common SDKs (`google.generativeai.GenerativeModel.generate_content`, `openai.chat.completions.create`, `anthropic` messages, optionally `httpx`/`requests` to known LLM hosts) for the duration of `_run` | Tool nested LLMs | High — single injection at `_load_graph` / `execute_tool` |
| **1** | Agent: instrument `execute_dynamic_function_async` / sandbox around `await func(...)` + light SDK patch in sandbox worker, or post-call usage extraction | UI nested LLMs | High — one API choke point |
| **2** | Build-time inject `observe_llm(...)` helper into `gap_tool_generator` + `backend_generator` prompts/templates | Both (after regen) | Medium — misses old tools/functions until rebuild; codegen drift |
| **3** | OpenTelemetry / global httpx hooks only | Partial (REST only) | Medium — misses native SDK paths unless every client is wrapped |

#### Examples — before → what we add → after (how it starts working)

---

##### Rank 1a — Worker: wrap tools + SDK monkeypatch

**Scenario:** FREEZE (or DYNAMIC) node calls `translate_text` tool; tool body calls Gemini via `google.generativeai`.

**Before (today)**

```python
# langgraph/app/executor.py — _load_graph
def execute_dynamic_tool_with_credentials(tool_name: str, params: dict) -> Any:
    return registry_ref.execute_tool(tool_name, params, request_meta=tool_request_meta)

module.execute_dynamic_tool = execute_dynamic_tool_with_credentials

# Generated tool function_code (unchanged, already on disk)
# def _run(..., request_meta=None):
#     import google.generativeai as genai
#     genai.configure(api_key=request_meta["credentials"]["gemini_api_key"])
#     model = genai.GenerativeModel("gemini-3.5-flash-lite")
#     return model.generate_content(prompt).text
```

What is observed: nothing about the tool LLM. Maybe a node line in `execution_history` with tool output only.

**What we add**

```python
# 1) Context: current execution_id / parent node span (set in _run_and_log_stream)
# 2) Wrapper around execute_dynamic_tool
# 3) Temporary SDK patch only while _run runs

def execute_dynamic_tool_with_credentials(tool_name: str, params: dict) -> Any:
    with obs.tool_span(name=tool_name, parent=obs.current_node_span_id()) as tool_span:
        with obs.patch_llm_sdks(parent_span_id=tool_span.span_id, kind="llm_tool"):
            return registry_ref.execute_tool(
                tool_name, params, request_meta=tool_request_meta
            )
```

`patch_llm_sdks` temporarily replaces e.g. `GenerativeModel.generate_content` with:

```python
def wrapped_generate_content(self, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        result = original(self, *args, **kwargs)
        obs.emit_span(kind="llm_tool", model=..., prompt_digest=..., 
                      token_usage=extract(result), latency_ms=..., status="success")
        return result
    except Exception as e:
        obs.emit_span(..., status="failed", error=str(e))
        raise
```

**After (how it starts working)**

1. Worker starts workflow `exec_abc` → `ObservabilitySession(trace_id="exec_abc")`.
2. Graph hits tool node → opens `node` span.
3. Node calls `execute_dynamic_tool("translate_text", …)` → opens `tool` span → enables patch.
4. Tool’s existing `generate_content(...)` runs unchanged → patch records `llm_tool` child span → restores originals.
5. Mongo `workflow_obs_spans`:

```text
trace_id=exec_abc
  node:translate_text_node
    └─ tool:translate_text          latency=1200ms
         └─ llm_tool                model=gemini-3.5-flash-lite  tokens=…
```

No regen of tool JSON required; old tools start emitting as soon as the worker ships the wrapper.

---

##### Rank 1b — Agent: instrument `/dynamic/execute` + sandbox

**Scenario:** UI screen paused; Angular calls backend function `summarize_notes` which calls OpenAI inside the sandbox.

**Before (today)**

```text
Worker: interrupt({screen_id: "ReviewScreen"}) → paused
Browser: POST /aiqod-agent/agent/dynamic/execute/{project_id}
  body: { function_id: "summarize_notes", params: {...},
          request_meta: { source: "angular_app" } }   # no execution_id
Agent: sandbox exec → await summarize_notes(...) → returns JSON
# No span written; worker never saw the LLM
```

**What we add**

1. **FE / orchestrator** — pass correlation IDs:

```ts
request_meta: {
  source: "angular_app",
  execution_id,   // from paused instance
  workflow_id,
  screen_id,
}
```

2. **Agent** — around sandbox invoke in `execute_dynamic_function_async`:

```python
async def execute_dynamic_function_async(...):
    meta = request_meta or {}
    trace_id = meta.get("execution_id")
    with obs.ui_function_span(
        trace_id=trace_id,
        function_id=function_id,
        screen_id=meta.get("screen_id"),
        kind="ui_function",
    ) as parent:
        # Option A: patch SDKs inside sandbox worker for this call
        # Option B: at least record function latency; nest llm_ui if patch works
        with obs.patch_llm_sdks(parent_span_id=parent.span_id, kind="llm_ui"):
            result = await FunctionWorkerPool.execute(...)
        return result  # obs failures never change result
```

**After (how it starts working)**

1. Same `interrupt` as today; worker writes `ui` span (paused) under `exec_abc`.
2. Screen calls `/dynamic/execute` with `execution_id=exec_abc`.
3. Agent writes `ui_function` (+ nested `llm_ui` if OpenAI/Gemini was called) into **same** Mongo collection.
4. Trace tree (assembled by `trace_id`, even across processes):

```text
trace_id=exec_abc
  node:review_screen
    └─ ui:ReviewScreen              status=paused → success (on resume)
         └─ ui_function:summarize_notes   (written by agent)
              └─ llm_ui                   model=gpt-… tokens=…
  human:resume                      reviewed=true
```

Starts working once agent deploy + FE passes `execution_id`; no need to regenerate every screen function for basic function-level spans (SDK patch gives nested LLM detail).

---

##### Rank 2 — Build-time inject `observe_llm(...)`

**Scenario:** New tools/functions generated after we change codegen prompts/templates.

**Before (generated tool body)**

```python
import google.generativeai as genai
genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-3.5-flash-lite")
resp = model.generate_content(user_text)
return {"translation": resp.text}
```

**What we add (in `gap_tool_generator` / `backend_generator`)**

- Ship a small runtime helper (worker tools env + sandbox builtins), e.g. `observe_llm(provider, model, prompt, call_fn)`.
- Prompt/template rule: “Do not call provider SDKs directly; wrap every model call with `observe_llm`.”

**After (new generated code)**

```python
from aiqod_obs import observe_llm  # injected helper available at runtime

def _call():
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.5-flash-lite")
    return model.generate_content(user_text)

resp = observe_llm(
    provider="google",
    model="gemini-3.5-flash-lite",
    prompt=user_text,
    call=_call,  # helper times call, extracts usage, emits llm_tool/llm_ui span
)
return {"translation": resp.text}
```

**How it starts working**

- Only after **re-running** gap-tool / backend generation (or execute rebuild) for that tool/function.
- Old artifacts without `observe_llm` still need Rank 1 monkeypatch.
- Best as hardening on top of Rank 1, not a replacement for Phase 1.

---

##### Rank 3 — OpenTelemetry / global `httpx`/`requests` hooks only

**Scenario:** Some tools call Gemini via REST (`https://generativelanguage.googleapis.com/...`) with `httpx`.

**Before**

```python
import httpx
r = httpx.post(
    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    headers={"x-goog-api-key": api_key},
    json={"contents": [{"parts": [{"text": prompt}]}]},
)
return r.json()
```

No span; native `genai.GenerativeModel.generate_content` also invisible to HTTP hooks.

**What we add**

```python
# At worker (and/or agent sandbox) process start:
instrument_httpx(known_llm_host_suffixes=[
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
])
# On each matching request/response: emit llm_* span (URL→provider, latency, status)
```

**After (how it starts working)**

- REST-based LLM calls automatically appear as spans.
- **Still misses** `google.generativeai` / official `openai` / `anthropic` SDK paths that use their own transports or gRPC-like stacks unless those are also wrapped → hence Rank 3 alone is **partial**.
- Useful as a safety net under Rank 1 patches, not as the primary strategy.

---

##### Side-by-side: same user action, what each rank sees

| What ran | Rank 1a worker | Rank 1b agent | Rank 2 codegen | Rank 3 HTTP-only |
|----------|----------------|---------------|----------------|------------------|
| Tool uses `genai.GenerativeModel` | `tool` + `llm_tool` | — | `llm_tool` if regenerated | miss |
| Tool uses Gemini REST via `httpx` | `tool` + `llm_tool` (patch or HTTP) | — | if wrapped in `observe_llm` | `llm_*` from HTTP |
| UI function uses OpenAI SDK in sandbox | — | `ui_function` + `llm_ui` | if regenerated | miss (unless HTTP) |
| DYNAMIC `_get_llm().invoke` | separate graph callbacks (not this table) | — | — | miss |

**Recommended hybrid:** Rank-1 for both processes (worker tools + agent `/dynamic/execute`), with Rank-2 as a later hardening so new codegen prefers an explicit helper. Do **not** rely on regenerating every historical tool/UI function for Phase 1. Rank-3 optional as HTTP safety net.

#### Correlation model

```
trace_id == execution_id

span kinds:
  node | llm_graph | tool | llm_tool | ui | llm_ui | human

parentage examples:
  node(tool:X) → tool(X) → llm_tool(...)
  node(ui:Y)   → ui(Y)   → llm_ui(function_id=...)   # written by agent, same trace_id
  node(llm)    → llm_graph(...)                      # DYNAMIC agent
```

UI spans are written by the **agent process** into the **same Mongo** span collection, keyed by `execution_id` from `request_meta`.

---

### A.6 What the runtime already captures (unchanged gaps)

| Signal | Mechanism | Gap |
|--------|-----------|-----|
| Node I/O | `execution_history` | No latency / tree / nested LLMs |
| Live feed | Valkey SSE | Not analytics |
| DYNAMIC tokens | `llm_token_count` from message scrape | Tools/UI LLMs ignored; FREEZE ignored |
| Human pause | interrupt / resume | Weak `human_review` |

---

### A.7 What’s useful from `aiobservabilitysrc`

**Use (reimplement):** span/trace model, AsyncWriter, callback pattern for **graph** LLMs, upsert indexes, “obs never fails the run”, agent metric formulas.  

**Don’t use as dependency / default:** invoice field-accuracy eval pack, HF models on hot path, assuming one process sees all LLM calls.

---

## Part B — Target architecture (FREEZE + DYNAMIC + nested LLMs)

### B.1 Components

```
langgraph/app/observability/     # worker-side
  session.py                     # ObservabilitySession(trace_id=execution_id)
  callbacks.py                   # LangGraph handler → node / llm_graph spans
  tool_wrap.py                   # execute_dynamic_tool wrapper → tool spans
  sdk_patch.py                   # temporary monkeypatch during tool _run → llm_tool spans
  store.py / writer.py           # Mongo AsyncWriter

backend/.../observability/       # agent-side (or shared library copied/packaged)
  dynamic_execute_hooks.py       # wrap sandbox function execution → llm_ui spans
  sdk_patch.py                   # same idea inside sandbox worker if needed
  store.py                       # SAME Mongo DB + workflow_obs_spans collection
```

Shared contract: same span schema + same `trace_id=execution_id`.

### B.2 Executor / worker wiring

1. `_run_and_log_stream`: start session; add callbacks to `graph.stream` config; set contextvars (`execution_id`, current node).  
2. `_load_graph`: accept obs context **or** read contextvars; wrap `execute_dynamic_tool` to:
   - open `tool` span  
   - enable SDK patch bound to that parent  
   - call registry  
   - close span (latency, error)  
3. Pause / resume / assignment re-pause: `ui` + `human` spans.  
4. DYNAMIC: graph LLM via callbacks; enrich with existing usage scrape as backup.

### B.3 Agent `/dynamic/execute` wiring

1. Require / inject into `request_meta`: `execution_id`, `workflow_id`, `screen_id` (from Angular / orchestrator).  
2. Around function execution: open `llm_ui` or generic `function` span; if SDK patch available in sandbox, nest LLM children; else record function latency + best-effort token fields if return value / logs expose them.  
3. Write to `workflow_obs_spans` with `trace_id=execution_id`.  
4. Never fail the HTTP response because obs failed.

### B.4 Storage

Same Mongo as runtime (`MONGO_URI` / `MONGO_DB_NAME`):

- `workflow_obs_spans` — canonical  
- Indexes: `span_id` unique; `trace_id`; `subscriber_id+timestamp`; `kind`; `workflow_id`  
- Dual-write `llm_token_count` during transition (include `kind=llm_tool|llm_ui|llm_graph` in metadata)

Payload policy: digest by default; full prompt/completion behind flag (tools/UI often carry customer data).

---

## Part C — Implementation plan

### Phase 0 — Lock decisions

1. Flag `WORKFLOW_OBS_ENABLED`.  
2. Payload policy `digest|full|off`.  
3. Same Mongo for worker + agent writers.  
4. Angular/orchestrator must pass `execution_id` (and ideally `workflow_id`, `screen_id`) on every `/dynamic/execute` call.  

### Phase 1 — Capture that covers FREEZE + DYNAMIC end-to-end

**Worker**

1. Native `langgraph/app/observability/`.  
2. Graph callbacks → node / `llm_graph` spans + latency.  
3. Tool wrap + SDK monkeypatch → `tool` / `llm_tool` spans.  
4. UI pause/resume → `ui` / `human` spans.  
5. Contextvars so tools attach to the right parent node even though `_load_graph` historically lacked `execution_id`.  

**Agent**

6. Instrument `/dynamic/execute` → spans with `kind=llm_ui` (or `ui_function` + nested `llm_ui`).  
7. Correlate via `request_meta.execution_id`.  
8. FE change: pass `execution_id` from the paused instance into screen `request_meta`.  

**Exit criteria**

- FREEZE run with an LLM-tool: spans show `node → tool → llm_tool`.  
- FREEZE/DYNAMIC run with UI that calls an LLM backend: spans show `ui → llm_ui` under same `execution_id` even while worker was paused.  
- DYNAMIC agent turn: `llm_graph` tokens present.  
- Obs failure never breaks tool/UI/workflow.

### Phase 2 — Metrics API

Volume, latency (by kind), tokens/cost (sum **graph + tool + ui** LLMs), human override/interference, step utility, per-model, status breakdown. Filter by tenant/workflow/time.

### Phase 3 — Contracts / eval (opt-in)

Only where Process_Configs declares contracts; not default field-accuracy for all workflows.

### Phase 4 — Hardening

Build-time `observe_llm` helper in gap_tool + backend_generator; stronger sandbox patches; retention/redaction.

### Phase 5 — Product UI

Trace timeline (including nested LLM children) + KPI dashboard.

---

## Part D — Non-goals

- Treating LangGraph callbacks as sufficient for all LLM cost  
- Observing build-time planner/codegen LLMs as “workflow runtime”  
- Requiring regen of all historical tools/screens before Phase 1 works  
- Vendoring `aiobservabilitysrc` as the production library  
- HF safety models on the hot path  

---

## Part E — Bottom line

| Question | Answer |
|----------|--------|
| Observe FREEZE + DYNAMIC? | **Yes** — different signals, one trace model |
| Observe LLMs inside tools? | **Yes** — worker-side tool wrap + SDK patch (not graph callbacks) |
| Observe LLMs inside UI backends? | **Yes** — agent `/dynamic/execute` instrumentation + `execution_id` correlation (worker is paused) |
| One process enough? | **No** — need **worker + agent** writers to the same span store |
| First build step | Phase 1 dual capture + FE `execution_id` on dynamic execute |

Without nested tool/UI capture, runtime observability will look “correct” on the graph while **missing a large share of real LLM work** that this platform generates into tools and screens by design.

---

## Part F — Will this slow execution or screen loading?

**Short answer:** If we stick to Phase 1 capture (async writes, digests, no HF eval on the hot path), users should **not** notice meaningful slowdown. Observability adds small fixed overhead (microseconds–low milliseconds) next to LLM/tool work that already takes hundreds of ms to seconds. Screen *loading* (mounting Angular + first paint) is mostly unaffected; what can add time is each `/dynamic/execute` call the screen makes — and even there, the backend/LLM dominates unless we do something heavy on the hot path.

### F.1 Where time can be spent

| Moment | What user experiences | Obs work on the critical path | Typical cost if designed correctly |
|--------|----------------------|-------------------------------|------------------------------------|
| Graph node / tool run (worker) | Workflow step waiting | Open/close span + SDK wrap call | **≪ 1–5 ms** per call |
| Nested tool LLM | Same wait as today for Gemini/OpenAI | Extra wrapper around existing SDK call | Negligible vs model latency (often **0.5–5+ s**) |
| UI screen first open | Screen appears after `interrupt` + SSE | Worker writes a `ui` span (async) | **Not on browser paint path** |
| Screen calls backend function | Spinner on that action | Agent wrap around existing sandbox+LLM | **≪ 1–5 ms** + async Mongo enqueue |
| Resume workflow | Continues after submit | `human` span write (async) | Negligible |
| Metrics dashboard / trace UI | Separate admin views | Read path only | **Zero** impact on workflow users |
| HF toxicity/PII/HHEM eval | — | If run **inline** after every LLM | **Bad** — can add hundreds of ms–seconds; **must stay async/off hot path** |

### F.2 By capture approach

| Approach | Extra latency on user path? | Notes |
|----------|----------------------------|--------|
| **Rank 1a** tool wrap + SDK monkeypatch | Very low | One Python wrapper + `perf_counter`; patch install/restore per tool call is cheap if scoped to `_run` only |
| **Rank 1b** `/dynamic/execute` wrap | Very low on function calls; **no** effect on initial screen HTML/JS load | Screen “feels slow” today mainly from network + LLM inside the function, not from mounting the route |
| **Rank 2** `observe_llm` helper | Same as Rank 1 (explicit call instead of patch) | No extra vs monkeypatch if helper only times + enqueues |
| **Rank 3** HTTP hooks | Very low per REST call | Slight cost on all matched HTTP; keep host allowlist tight |
| **AsyncWriter → Mongo** | Near-zero on hot path | Enqueue to in-memory queue; background thread writes. Do **not** `insert_one` synchronously inside the tool/UI call |
| **Full prompt/completion capture** | Can hurt | Large JSON `json.dumps` / copy of huge prompts on the hot thread. Prefer **digests + sizes** by default; sample full payloads |
| **Sync eval / HF models** | **Yes — user-visible** | Never on Phase 1 hot path |

### F.3 Screen loading specifically

```
User opens UI node
  → worker already paused (interrupt)
  → browser loads Angular screen (assets/UI)     ← obs does not sit here
  → screen may call /dynamic/execute N times       ← obs wraps each call lightly
  → user submits → resume                          ← obs records human span
```

- **Initial screen load / blank-to-interactive:** should stay essentially the same (plus tiny `execution_id` in `request_meta`).  
- **Actions on the screen that call LLM backends:** same order of magnitude as today; obs should not turn a 2s LLM into a 2.5s wait if writes are async and payloads are digests.  
- **If Mongo is down / slow:** queue backs up or drops with logging — **must not block** returning the function result to the UI (same rule as today’s `_save_llm_token_count`).

### F.4 What would make users feel slowness (avoid these)

1. Synchronous Mongo (or any network I/O) inside tool `_run` or `/dynamic/execute` before returning.  
2. Serializing full prompts/completions/state blobs on every span on the request thread.  
3. Running HF evaluators or LLM-as-judge inline before returning to the graph/UI.  
4. Patching SDKs globally for the whole worker process lifetime in a way that wraps *non-LLM* traffic broadly (keep patches scoped + allowlisted).  
5. Emitting high-cardinality SSE “obs” events that flood the browser.

### F.5 Design rules so impact stays “not much issue”

1. **Hot path = record + enqueue only**; flush/write on a background thread (`AsyncWriter` pattern).  
2. **Default payload policy = digest**; full bodies opt-in / sampled.  
3. **Feature flag** `WORKFLOW_OBS_ENABLED` for soak tests; compare p50/p95 tool and `/dynamic/execute` latency with flag on/off.  
4. **Budgets (targets):** obs overhead **&lt; 5 ms** p95 per instrumented call excluding the underlying LLM/tool work; if exceeded, digests/async first, not more features.  
5. Evaluation / dashboards only on **cold path**.

### F.6 Bottom line for stakeholders

| Question | Answer |
|----------|--------|
| Will workflow steps get slower? | **Barely**, if async + digests; noise vs existing LLM/tool time |
| Will screens load slower? | **Initial load: essentially no.** Per-action backend calls: negligible vs LLM |
| When would users notice? | Only if we do sync I/O, huge payload copy, or inline model eval — all explicitly out of Phase 1 |
| How do we prove it? | Flag on/off latency comparison on tool nodes and `/dynamic/execute` in staging |
