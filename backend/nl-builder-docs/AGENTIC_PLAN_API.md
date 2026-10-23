# Agentic plan API — request payloads

Base path (as registered in `agent_new.py`):

| Endpoint | Method | Path |
|----------|--------|------|
| Preview | `POST` | `/aiqod-agent/agent/agentic/plan/preview` |
| Execute | `POST` | `/aiqod-agent/agent/agentic/plan/execute` |

Tenant fields may also be sent via headers: `x-org-id`, `x-subscriber-id`, `x-user-id` (body values override when both are present).

---

## 1. `/aiqod-agent/agent/agentic/plan/preview`

**Request model:** `AgenticPlanRequest`

**Rule:** exactly one of `prompt` (non-empty string) **or** `steps` (non-empty array) must be set. The other must be omitted or `null`.

### 1a. Full field reference (all keys)

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `projectId` | string \| null | no | Used for capability DB context and stable ids |
| `orgId` | string \| null | no | Tenant |
| `subscriberId` | string \| null | **yes** for credits | Required for credit pre-check (50 minimum available) |
| `userId` | string \| null | no | Tenant |
| `prompt` | string \| null | one-of | Natural language → requirement analyzer LLM |
| `steps` | array \| null | one-of | Structured phases / sub-steps |
| `selectedName` | string \| null | no | Workflow / UI label; persisted |
| `selectedType` | string \| null | no | Persisted |
| `selectedCategory` | string \| null | no | Persisted |
| `token` | string \| null | no | Persisted on `Process_Configs` |

### 1b. Example — **steps** mode (every optional key present)

Use this when the user defines phases and sub-steps in the UI. `prompt` must be `null`.

```json
{
  "projectId": "507f1f77bcf86cd799439011",
  "orgId": "507f1f77bcf86cd799439012",
  "subscriberId": "507f1f77bcf86cd799439013",
  "userId": "507f1f77bcf86cd799439014",
  "prompt": null,
  "steps": [
    {
      "stepId": "phase_1",
      "name": "Onboarding",
      "description": "Collect user data and verify identity",
      "phaseType": "FREEZE",
      "subSteps": [
        {
          "stepId": "phase_1_step_1",
          "name": "Show welcome form",
          "description": "Display a form to capture name and email"
        },
        {
          "stepId": "phase_1_step_2",
          "name": "Verify documents",
          "description": "Upload and validate ID document"
        }
      ]
    },
    {
      "stepId": "phase_2",
      "name": "Automation",
      "description": "Run background checks with no sub-steps",
      "subSteps": null,
      "phaseType": "DYNAMIC"
    }
  ],
  "selectedName": "My workflow",
  "selectedType": null,
  "selectedCategory": null,
  "token": null
}
```

**Per-phase object (`AgenticPhaseStep`):**

| Field | Type | Required |
|-------|------|----------|
| `stepId` | string | yes |
| `name` | string | yes |
| `description` | string | yes |
| `subSteps` | array \| null | no; if null, the phase row becomes a single logical step |
| `phaseType` | `"FREEZE"` \| `"DYNAMIC"` | no; default `"FREEZE"` |

**Per sub-step (`AgenticSubStep`):**

| Field | Type | Required |
|-------|------|----------|
| `stepId` | string | yes |
| `name` | string | yes |
| `description` | string | yes |

### 1c. Example — **prompt** mode (every optional key present)

Use this when the user types a single natural-language brief. `steps` must be `null`.

```json
{
  "projectId": null,
  "orgId": null,
  "subscriberId": "507f1f77bcf86cd799439013",
  "userId": null,
  "prompt": "Build a workflow that collects KYC data, shows a review screen, then calls an API to submit the application.",
  "steps": null,
  "selectedName": "KYC workflow",
  "selectedType": null,
  "selectedCategory": null,
  "token": null
}
```

### 1d. Preview response (for chaining to execute)

The preview handler returns `AgenticPlanPreviewResponse`. Fields the client typically needs for execute:

- `workflowId` — pass as `workflowId` on execute (same value stored as `agentic_process.workflow_id`).
- `workflowName`, `mode`, `analysis_source` — echo back on execute for consistency.
- `phaseGraphs` — **pass back unchanged** (graphs still contain `GENERATE:*` nodes).
- `steps` — flattened logical steps; pass as `steps` on execute for persistence.
- `estimatedCredits` — recommended to pass on execute for credit preflight (else server reads from DB).

---

## 2. `/aiqod-agent/agent/agentic/plan/execute`

**Request model:** `AgenticPlanExecuteRequest`

**Rule:** `workflowId` and `phaseGraphs` are required. `phaseGraphs` should be the array returned by preview (same structure).

### 2a. Full field reference (all keys)

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `projectId` | string \| null | no | Defaults to `workflowId` for some internal paths if omitted |
| `orgId` | string \| null | no | Tenant |
| `subscriberId` | string \| null | **yes** | Required |
| `userId` | string \| null | no | Tenant |
| `token` | string \| null | no | Persisted |
| `selectedName` | string \| null | no | Persisted |
| `selectedType` | string \| null | no | Persisted |
| `selectedCategory` | string \| null | no | Persisted |
| `workflowId` | string | **yes** | Must match preview’s `workflowId` / DB row |
| `workflowName` | string \| null | no | Display name |
| `mode` | `"prompt"` \| `"steps"` | no | Default `"steps"` |
| `analysis_source` | `"llm"` \| `"fallback"` \| null | no | Echo from preview when `mode` is `prompt` |
| `phaseGraphs` | array | **yes** | List of `PhaseGraphResult` (see below) |
| `steps` | array | no | Default `[]`; flat `LogicalStep` list for MongoDB persistence |
| `prompt` | string \| null | no | Original prompt if any; persisted |
| `estimatedCredits` | integer \| null | no | If set and &gt; 0, used for minimum balance check; else loaded from `Process_Configs.agentic_process.estimatedCredits`; if still missing, falls back to **50** |

Credit preflight: available credits (balance − reserved) must be ≥ resolved `estimatedCredits` (or DB / fallback as above). Per-screen gap generation still reserves credits per UI screen separately.

### 2b. Nested shapes (what `phaseGraphs` contains)

Each element is a **`PhaseGraphResult`**:

| Field | Type | Notes |
|-------|------|--------|
| `graph` | object | `GraphDefinition` — required |
| `validation_warnings` | string[] | default `[]` |
| `binding_fixes_applied` | int | default `0` |
| `gap_generations` | string[] | Usually `[]` at execute input (filled after gap resolution) |
| `langgraph_code` | string | Usually `""` from preview |
| `langgraph_code_manual` | string | default `""` |
| `langgraph_code_llm` | string | default `""` |
| `phaseType` | string | `"FREEZE"` or `"DYNAMIC"` |

**`GraphDefinition`:**

| Field | Type |
|-------|------|
| `graphId` | string |
| `phaseId` | string |
| `phaseName` | string |
| `name` | string |
| `description` | string |
| `goal` | string |
| `nodes` | `GraphNode[]` |
| `edges` | `GraphEdge[]` |
| `entryNodeId` | string |
| `endNodeId` | string |
| `requiredGlobalInputs` | string[] (default `[]`) |

**`GraphNode`:**

| Field | Type |
|-------|------|
| `nodeId` | string |
| `type` | string (`tool`, `ui_screen`, `agent`, `router`, `fallback_router`, `end`, …) |
| `name` | string |
| `stepId` | string \| null |
| `capabilityRef` | object \| null — `GraphCapabilityRef` |
| `config` | `GraphNodeConfig` |

**`GraphCapabilityRef`:**

| Field | Type |
|-------|------|
| `capabilityId` | string (e.g. `GENERATE:MyScreen`, `tool:…`, `ui:…`) |
| `kind` | string |
| `version` | string \| null |
| `source` | string \| null |
| `class_path` | string \| null |
| `wrapper_class` | string \| null |
| `wrapper_kwarg` | string \| null |

**`GraphNodeConfig`:**

| Field | Type |
|-------|------|
| `inputBindings` | object (map string → string) |
| `declaredOutputs` | `GenerationIOParam[]` |
| `router` | `RouterConfig` \| null |
| `generation` | `GenerationSpec` \| null |

**`RouterConfig`:**

| Field | Type |
|-------|------|
| `routingRules` | `{ "condition": string, "targetNodeId": string }[]` |
| `defaultTarget` | string \| null |
| `fallbackTarget` | string \| null |

**`GenerationSpec`:**

| Field | Type |
|-------|------|
| `kind` | string (default `"ui_screen"`) |
| `stepId` | string |
| `description` | string |
| `inputs` | `GenerationIOParam[]` |
| `outputs` | `GenerationIOParam[]` |

**`GenerationIOParam`:**

| Field | Type |
|-------|------|
| `name` | string |
| `dataType` | string (default `"Any"`) |
| `required` | boolean (default `false`) |
| `description` | string (default `""`) |

**`GraphEdge`:**

| Field | Type |
|-------|------|
| `fromNode` | string |
| `to` | string |
| `kind` | string |
| `condition` | string \| null |

**`LogicalStep` (for `steps` on execute):**

| Field | Type |
|-------|------|
| `stepId` | string |
| `name` | string |
| `description` | string |
| `inputs` | string[] |
| `outputs` | string[] |
| `preferredKind` | `"ui_screen"` \| `"tool"` \| `"agent"` \| null |

### 2c. Example — execute (all top-level keys; `phaseGraphs` minimal placeholder)

**Do not copy this graph literally** — replace `phaseGraphs` with the full JSON array from the preview response. The snippet below only shows the **required envelope** and field names.

```json
{
  "projectId": "507f1f77bcf86cd799439011",
  "orgId": "507f1f77bcf86cd799439012",
  "subscriberId": "507f1f77bcf86cd799439013",
  "userId": "507f1f77bcf86cd799439014",
  "token": null,
  "selectedName": "My workflow",
  "selectedType": null,
  "selectedCategory": null,
  "workflowId": "agentic_1730000000000",
  "workflowName": "My workflow",
  "mode": "steps",
  "analysis_source": null,
  "phaseGraphs": [
    {
      "graph": {
        "graphId": "phase_1_graph",
        "phaseId": "phase_1",
        "phaseName": "Onboarding",
        "name": "Onboarding Graph",
        "description": "Collect user data and verify identity",
        "goal": "Collect user data and verify identity",
        "nodes": [
          {
            "nodeId": "start_node",
            "type": "tool",
            "name": "Start",
            "stepId": null,
            "capabilityRef": {
              "capabilityId": "tool:builtin_start",
              "kind": "tool",
              "version": null,
              "source": null,
              "class_path": null,
              "wrapper_class": null,
              "wrapper_kwarg": null
            },
            "config": {
              "inputBindings": {},
              "declaredOutputs": [],
              "router": null,
              "generation": null
            }
          },
          {
            "nodeId": "gen_welcome_ui",
            "type": "ui_screen",
            "name": "Welcome form",
            "stepId": "phase_1_step_1",
            "capabilityRef": {
              "capabilityId": "GENERATE:WelcomeForm",
              "kind": "ui_screen",
              "version": null,
              "source": null,
              "class_path": null,
              "wrapper_class": null,
              "wrapper_kwarg": null
            },
            "config": {
              "inputBindings": {},
              "declaredOutputs": [
                {
                  "name": "form_data",
                  "dataType": "object",
                  "required": true,
                  "description": "Captured form fields"
                }
              ],
              "router": null,
              "generation": {
                "kind": "ui_screen",
                "stepId": "",
                "description": "Angular screen for name and email capture.",
                "inputs": [],
                "outputs": [
                  {
                    "name": "form_data",
                    "dataType": "object",
                    "required": true,
                    "description": ""
                  }
                ]
              }
            }
          },
          {
            "nodeId": "end_node",
            "type": "end",
            "name": "End",
            "stepId": null,
            "capabilityRef": null,
            "config": {
              "inputBindings": {},
              "declaredOutputs": [],
              "router": null,
              "generation": null
            }
          }
        ],
        "edges": [
          {
            "fromNode": "start_node",
            "to": "gen_welcome_ui",
            "kind": "sequential",
            "condition": null
          },
          {
            "fromNode": "gen_welcome_ui",
            "to": "end_node",
            "kind": "sequential",
            "condition": null
          }
        ],
        "entryNodeId": "start_node",
        "endNodeId": "end_node",
        "requiredGlobalInputs": []
      },
      "validation_warnings": [],
      "binding_fixes_applied": 0,
      "gap_generations": [],
      "langgraph_code": "",
      "langgraph_code_manual": "",
      "langgraph_code_llm": "",
      "phaseType": "FREEZE"
    }
  ],
  "steps": [
    {
      "stepId": "phase_1_step_1",
      "name": "Show welcome form",
      "description": "Display a form to capture name and email",
      "inputs": [],
      "outputs": [],
      "preferredKind": null
    },
    {
      "stepId": "phase_1_step_2",
      "name": "Verify documents",
      "description": "Upload and validate ID document",
      "inputs": [],
      "outputs": [],
      "preferredKind": null
    }
  ],
  "prompt": null,
  "estimatedCredits": 1250
}
```

### 2d. Recommended client flow

1. `POST …/plan/preview` with either `prompt` or `steps`.
2. Read `workflowId`, `estimatedCredits`, `phaseGraphs`, `steps`, `mode`, `analysis_source`, `workflowName` from the response.
3. `POST …/plan/execute` with the same `phaseGraphs` (verbatim), same `workflowId`, and `estimatedCredits` set to the preview value (optional but recommended). If `estimatedCredits` is omitted, the server loads it from `Process_Configs` for that `workflowId` and tenant.

---

## Source of truth

Models and routes are defined in `backend/agent_new.py`:

- `AgenticPlanRequest`, `AgenticPhaseStep`, `AgenticSubStep` — preview body
- `AgenticPlanExecuteRequest`, `PhaseGraphResult`, `GraphDefinition`, … — execute body
- `@app.post("/aiqod-agent/agent/agentic/plan/preview", …)`
- `@app.post("/aiqod-agent/agent/agentic/plan/execute", …)`
