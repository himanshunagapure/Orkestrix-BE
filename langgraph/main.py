import os
import json
import time
import asyncio
import valkey
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Automatically load the .env file from the current directory
load_dotenv()

from app.database import MongoDBClient
from app.schemas import StartRequest, ResumeRequest, MyTasksRequest, MyTaskWorkflowsRequest

# --- Configuration from .env ---
API_PREFIX = os.getenv("API_PREFIX", "/dev")
APP_PORT = int(os.getenv("APP_PORT", 8000))
VALKEY_HOST = os.getenv("VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.getenv("VALKEY_PORT", 6379))
STREAM_NAME = os.getenv("VALKEY_STREAM_NAME", "agentic_tasks")
MAX_QUEUE_LENGTH = int(os.getenv("MAX_QUEUE_LENGTH", 10000))

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB_NAME", "aiqod-dev")

# --- Global Clients ---
valkey_client = valkey.Valkey(host=VALKEY_HOST, port=VALKEY_PORT, decode_responses=True)
db_client = MongoDBClient(uri=MONGO_URI, db_name=DB_NAME)

# --- FastAPI App ---
app = FastAPI(title="Agentic API Node")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

@app.post("/instances/start")
async def start_instance(req: StartRequest):
    instance = db_client.create_instance(
        req.workflow_id, 
        req.initial_state, 
        created_by=req.userId,
        subscriber_id=req.subscriberId,
        org_id=req.orgId,
        jwt_token=req.jwtToken
    )
    return {
        "message": "Instance Initialized", 
        "execution_id": instance.execution_id, 
        "status": instance.status
    }

@app.post("/instances/{execution_id}/execute")
async def execute_instance(execution_id: str):
    try:
        instance = db_client.get_instance(execution_id)
        if instance.status != "initialized":
            raise HTTPException(status_code=400, detail="Instance already executed or not initialized")
            
        db_client.update_status(execution_id, "running")
        
        payload = {
            "action": "start",
            "execution_id": instance.execution_id,
            "workflow_id": instance.workflow_id,
            "initial_state": json.dumps(instance.initial_inputs)
        }
        # Push to Valkey with strict memory limit
        valkey_client.xadd(STREAM_NAME, payload, maxlen=MAX_QUEUE_LENGTH)
        
        return {"message": "Execution started", "execution_id": execution_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/instances/{execution_id}/resume")
async def resume_instance(execution_id: str, req: ResumeRequest):
    try:
        instance = db_client.get_instance(execution_id)
        
        assigned_to = instance.assigned_to
        if assigned_to and isinstance(assigned_to, dict):
            assigned_users = assigned_to.get("users", [])
            assigned_roles = assigned_to.get("roles", [])
            
            if assigned_users or assigned_roles:
                if req.userid not in assigned_users and req.roleid not in assigned_roles:
                    raise HTTPException(status_code=403, detail="Unauthorized: You are not assigned to this task.")

        db_client.update_status(execution_id, "running")
        db_client.increment_chain_position(execution_id)

        payload = {
            "action": "resume",
            "execution_id": execution_id,
            "payload": json.dumps(req.payload),
            "userid": req.userid or "",
            "roleid": req.roleid or ""
        }
        valkey_client.xadd(STREAM_NAME, payload, maxlen=MAX_QUEUE_LENGTH)
        
        return {"message": "Resume job dispatched", "execution_id": execution_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
        
@app.get("/instances/{execution_id}/stream")
async def stream_execution_logs(execution_id: str):
    async def event_generator():
        pubsub = valkey_client.pubsub()
        channel_name = f"execution_stream:{execution_id}"
        try:
            pubsub.subscribe(channel_name)
        except Exception as e:
            print(f"[System] Failed to subscribe to Valkey channel {channel_name}: {e}")
            yield f"event: failed\ndata: {json.dumps({'error': 'Failed to subscribe to event stream'})}\n\n"
            return
        last_ping = time.time()
        
        try:
            while True:
                try:
                    message = pubsub.get_message(ignore_subscribe_messages=True)
                except Exception as pubsub_err:
                    print(f"[System] Valkey PubSub read error: {pubsub_err}")
                    await asyncio.sleep(0.5)
                    continue
                
                if message and message.get('data'):
                    try:
                        payload = message['data']
                        parsed = json.loads(payload)
                        event_type = parsed.get("type", "log")
                        event_data = parsed.get("data", {})
                        
                        yield f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
                        
                        if event_type in ["completed", "failed"]:
                            break
                    except json.JSONDecodeError as json_err:
                        print(f"[System] Failed to parse SSE payload: {json_err}")
                        
                    last_ping = time.time() 
                else:
                    if time.time() - last_ping > 15:
                        yield f"event: ping\ndata: {{}}\n\n"
                        last_ping = time.time()
                    await asyncio.sleep(0.2) 
        except asyncio.CancelledError:
            print(f"[System] SSE client disconnected for {execution_id}")
        except Exception as e:
            print(f"[System] Unexpected error in SSE stream {execution_id}: {e}")
        finally:
            try:
                pubsub.unsubscribe(channel_name)
                pubsub.close()
            except Exception:
                pass
            print(f"[System] Closed SSE connection for {execution_id}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream",
        }
    )

@app.post("/instances/{workflow_id}/nodes/{node_id}/assignment")
async def save_node_assignment(workflow_id: str, node_id: str, req: dict):
    """
    Persists a node's assignment config (users, roles, and/or a prompt-based
    assignmentRef) directly into the LIVE graph definition stored in
    Process_Configs, under agentic_process.phaseGraphs[].graph.nodes[].config.
    executor.py reads this live at every pause, so changes here take effect
    immediately without rebuilding the workflow.
    """
    try:
        assigned_users = req.get("assigned_users", []) or []
        assigned_roles = req.get("assigned_roles", []) or []
        assignment_ref = req.get("assignmentRef")

        matched = db_client.save_node_assignment_config(
            workflow_id=workflow_id,
            node_id=node_id,
            users=assigned_users,
            roles=assigned_roles,
            assignment_ref=assignment_ref,
        )
        if not matched:
            raise HTTPException(
                status_code=404,
                detail=f"Node '{node_id}' not found in workflow '{workflow_id}'",
            )

        return {
            "message": "Assignment saved",
            "status": 0,
            "data": {
                "assigned_users": assigned_users,
                "assigned_roles": assigned_roles,
                "assignmentRef": assignment_ref,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/instances/list-by-org-sub")
async def list_workflows(req: dict):
    try:
        from bson import ObjectId
        org_id = req.get("orgId")
        sub_id = req.get("subscriberId")

        if not org_id:
            raise HTTPException(status_code=400, detail="orgId is required")

        query = {
            "orgId": {"$in": [ObjectId(req.get("orgId")), req.get("orgId")]},
            "subscriberId": {"$in": [ObjectId(req.get("subscriberId")), req.get("subscriberId")]},
            "engineType": "agentic_graph"
        }
        # NOTE: ObjectId(None) does NOT raise/return empty — it silently
        # generates a random ObjectId, which made this query match nothing.
        # Only add subscriberId to the filter when it's actually present.
        if sub_id:
            query["subscriberId"] = {"$in": [ObjectId(req.get("subscriberId")), req.get("subscriberId")]}
        projection = {
            "agentic_process.workflow_id": 1,
            "agentic_process.projectId": 1,
            "selectedName": 1,
            "selectedCategory": 1,
            "agentic_process.dashboardUrl": 1,
            "agentic_process.linkedWorkflowId": 1,
            "agentic_process.kind": 1
        }
        
        cursor = list(db_client.client[DB_NAME]["Process_Configs"].find(query, projection).sort("selectedName", 1))
        dashboard_lookup = {}
        workflows = []
        for doc in cursor:
            ap = doc.get("agentic_process", {})

            if ap.get("kind") == "dashboard":
                dashboard_lookup[ap["linkedWorkflowId"]] = ap
            else:
                workflows.append(doc)
        for doc in workflows:
            workflow_id = doc["agentic_process"].get("workflow_id")
            if workflow_id in dashboard_lookup:
                doc["dashboarddata"] = dashboard_lookup[workflow_id]
        for doc in workflows:
            doc["_id"] = str(doc["_id"])
            
        return {"status": "success", "data": workflows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/instances/my-tasks/workflows")
async def get_my_task_workflows(req: MyTaskWorkflowsRequest):
    """
    Left-panel source for the 'My Task' screen: returns ONLY workflows that
    have at least one matching task for this user/role — the zero-task
    filtering happens here in the backend (not the FE), per requirement.
    """
    try:
        from bson import ObjectId
        query = {"orgId": ObjectId(req.orgId), "engineType": "agentic_graph"}
        if req.subscriberId:
            query["subscriberId"] = ObjectId(req.subscriberId)

        projection = {
            "agentic_process.workflow_id": 1,
            "selectedName": 1,
            "selectedCategory": 1,
        }
        cursor = db_client.client[DB_NAME]["Process_Configs"].find(query, projection)

        workflow_meta = {}  # workflow_id -> processName
        for wf in cursor:
            wid = (wf.get("agentic_process") or {}).get("workflow_id")
            if wid:
                workflow_meta[wid] = wf.get("selectedName") or wf.get("selectedCategory") or "Untitled Workflow"

        if not workflow_meta:
            return {"status": "success", "data": []}

        counts = db_client.get_task_counts_by_workflow(
            workflow_ids=list(workflow_meta.keys()),
            user_id=req.userId,
            role_id=req.roleId,
            filter_type=req.filterType,
        )

        data = [
            {"workflowId": wid, "processName": workflow_meta[wid], "count": counts[wid]}
            for wid in workflow_meta
            if counts.get(wid, 0) > 0
        ]
        return {"status": "success", "data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instances/{execution_id}/current-task")
async def get_current_task(execution_id: str):
    """
    Lets the executor screen open directly on an ALREADY-paused instance
    (e.g. clicked from 'My Task') instead of only supporting fresh starts.
    Returns the same ui_request shape the SSE 'ui_request' event carries,
    reconstructed from execution_history rather than a live pause event.
    """
    try:
        instance = db_client.get_instance(execution_id)
        ui_request = db_client.get_pending_ui_request(execution_id)
        return {
            "status": "success",
            "data": {
                "executionId": instance.execution_id,
                "workflowId": instance.workflow_id,
                "instanceStatus": instance.status,
                "uiRequest": ui_request,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/instances/my-tasks")
async def get_my_tasks(req: MyTasksRequest):
    """
    Aligned 'My Task' bucket: reads directly from the `instances` collection
    (the source of truth executor.py writes to), scoped to a single
    workflow_id, and filtered by the caller's userId/roleId — matching
    exactly how executor.py stamps assigned_to = {users, roles} on pause.
    'assigned' -> only currently waiting_human tasks assigned to this user/role.
    'created'  -> all instances of this workflow started by this user.
    """
    try:
        instances = db_client.get_my_tasks(
            workflow_id=req.workflow_id,
            user_id=req.userId,
            role_id=req.roleId,
            filter_type=req.filterType,
        )
        data = [
            {
                "executionId": inst.execution_id,
                "workflowId": inst.workflow_id,
                "status": inst.status,
                "currentNode": inst.current_node,
                "assignedTo": inst.assigned_to,
                "createdBy": inst.created_by,
                "createdAt": inst.created_at,
                "updatedAt": inst.updated_at,
            }
            for inst in instances
        ]
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Workflow observability (Phase 2 read API) ---
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from workflow_observability.metrics import WorkflowObsMetrics
except Exception:
    WorkflowObsMetrics = None  # type: ignore


@app.get("/obs/traces/{execution_id}")
async def get_obs_trace(execution_id: str):
    if WorkflowObsMetrics is None:
        raise HTTPException(status_code=503, detail="Workflow observability not available")
    metrics = WorkflowObsMetrics()
    spans = metrics.trace_timeline(execution_id)
    return {"status": "success", "data": {"execution_id": execution_id, "spans": spans}}


@app.get("/obs/metrics/summary")
async def get_obs_summary(
    workflow_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    if WorkflowObsMetrics is None:
        raise HTTPException(status_code=503, detail="Workflow observability not available")
    metrics = WorkflowObsMetrics()
    summary = metrics.summary(
        workflow_id=workflow_id,
        subscriber_id=subscriber_id,
        since=since,
        until=until,
    )
    return {"status": "success", "data": summary}

root_app = FastAPI() 
root_app.mount(API_PREFIX, app)

if __name__ == "__main__":
    print(f"[System] Starting API Server on port {APP_PORT} with prefix {API_PREFIX}...")
    uvicorn.run(root_app, host="0.0.0.0", port=APP_PORT)
