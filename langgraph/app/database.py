import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from pymongo import MongoClient
from .schemas import InstanceDocument

class MongoDBClient:
    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self.uri = uri or os.getenv("MONGO_URI") or "mongodb://localhost:27017"
        self.db_name = db_name or os.getenv("MONGO_DB_NAME") or "aiqod-dev"
        
        self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
        
        try:
            self.client.admin.command('ping')
            print(f"[Database] Connected to MongoDB! (URI: {self.uri.split('@')[-1]}, Database: '{self.db_name}')")
        except Exception as e:
            print(f"[Database] CRITICAL: Failed to connect to MongoDB! Error: {e}")

        self.db = self.client[self.db_name]
        self.instances = self.db["instances"]
        self.process_configs = self.db["Process_Configs"]
        self.assignment_registry = self.db["assignment_registry"]

    def create_instance(self, workflow_id: str, initial_state: dict, created_by: Optional[str] = None, subscriber_id: Optional[str] = None, org_id: Optional[str] = None, jwt_token: Optional[str] = None) -> InstanceDocument:
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        thread_id = f"thread_{uuid.uuid4().hex[:8]}"
        
        doc = {
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "thread_id": thread_id,
            "status": "initialized",
            "current_node": None,
            "assigned_to": None,
            "created_by": created_by,
            "subscriber_id": subscriber_id,
            "org_id": org_id,
            "jwt_token": jwt_token,
            "initial_inputs": initial_state,
            "state": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        
        self.instances.insert_one(doc)
        print(f"[Database] Created instance {execution_id} in DB '{self.db_name}'")
        return InstanceDocument(**doc)

    def get_instance(self, execution_id: str, retries: int = 3, delay: float = 0.5) -> InstanceDocument:
        import time
        clean_id = str(execution_id).strip()
        
        for attempt in range(retries):
            doc = self.instances.find_one({"execution_id": clean_id})
            if doc:
                return InstanceDocument(**doc)
            
            print(f"[Database] Instance '{clean_id}' not found in DB '{self.db_name}' (Attempt {attempt + 1}/{retries}). Retrying in {delay}s...")
            time.sleep(delay)

        raise ValueError(f"Instance '{clean_id}' not found in database '{self.db_name}' after {retries} attempts.")

    def get_my_tasks(
        self,
        workflow_id: str,
        user_id: Optional[str] = None,
        role_id: Optional[str] = None,
        filter_type: str = "assigned",
    ) -> list[InstanceDocument]:
        """
        'assigned' -> instances of this workflow that are currently paused
        (status == 'waiting_human') AND assigned_to.users/roles matches this
        user/role. This is the live "My Task" bucket: a task only needs
        action while it's sitting at a human node.

        'created' -> all instances of this workflow started by this user,
        regardless of status (their own submissions / tracking view).
        """
        if filter_type == "created":
            query = {"workflow_id": workflow_id, "created_by": user_id}
        else:
            or_clauses = []
            if user_id:
                or_clauses.append({"assigned_to.users": user_id})
            if role_id:
                or_clauses.append({"assigned_to.roles": role_id})
            if not or_clauses:
                return []
            query = {
                "workflow_id": workflow_id,
                "status": "Pending Review",
                "$or": or_clauses,
            }

        docs = self.instances.find(query).sort("updated_at", -1)
        return [InstanceDocument(**d) for d in docs]

    def get_task_counts_by_workflow(
        self,
        workflow_ids: list,
        user_id: Optional[str] = None,
        role_id: Optional[str] = None,
        filter_type: str = "assigned",
    ) -> dict:
        """
        Returns {workflow_id: count} for only the workflows that actually have
        a matching task — used to filter the left-panel process list down to
        non-zero workflows server-side (the FE should never do this filtering).
        Same match criteria as get_my_tasks, just grouped across many workflow_ids
        in a single aggregation instead of N+1 queries.
        """
        if not workflow_ids:
            return {}

        if filter_type == "created":
            match = {"workflow_id": {"$in": workflow_ids}, "created_by": user_id}
        else:
            or_clauses = []
            if user_id:
                or_clauses.append({"assigned_to.users": user_id})
            if role_id:
                or_clauses.append({"assigned_to.roles": role_id})
            if not or_clauses:
                return {}
            match = {
                "workflow_id": {"$in": workflow_ids},
                "status": "Pending Review",
                "$or": or_clauses,
            }

        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$workflow_id", "count": {"$sum": 1}}},
        ]
        return {doc["_id"]: doc["count"] for doc in self.instances.aggregate(pipeline)}

    def get_pending_ui_request(self, execution_id: str) -> Optional[dict]:
        """
        Returns the ui_payload dict for an instance currently sitting at
        status == 'waiting_human' — the exact object executor.py published
        as the 'ui_request' SSE event, stamped directly onto the instance
        document by update_node_assignment() at pause time.

        NOTE: this deliberately does NOT read state.execution_history's
        'paused' entry — executor.py appends that entry to the in-memory
        history list but never calls update_state_snapshot() again after
        the pause branch, so that entry is never actually persisted to
        Mongo. pending_ui_request is written via a direct $set instead,
        so it doesn't depend on that gap.
        """
        instance = self.get_instance(execution_id)
        if instance.status != "Pending Review":
            return None
        return instance.pending_ui_request

    def get_workflow_config(self, workflow_id: str) -> dict:
        doc = self.process_configs.find_one({
            "$or": [
                {"agentic_process.workflow_id": workflow_id},
                {"workflow_id": workflow_id}
            ]
        })
        if not doc:
            raise ValueError(f"Workflow configuration for {workflow_id} not found in Process_Configs.")
        return doc

    def update_status(self, execution_id: str, status: str):
        self.instances.update_one(
            {"execution_id": execution_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}}
        )

    def update_state_snapshot(self, execution_id: str, state: dict):
        self.instances.update_one(
            {"execution_id": execution_id},
            {"$set": {"state": state, "updated_at": datetime.utcnow()}}
        )
    
    def update_node_assignment(self, execution_id: str, assigned_to: str, current_node: str, pending_ui_request: dict = None):
        self.instances.update_one(
            {"execution_id": execution_id},
            {"$set": {
                "assigned_to": assigned_to,
                "current_node": current_node,
                "pending_ui_request": pending_ui_request,
                "updated_at": datetime.utcnow(),
            }}
        )

    def clear_node_assignment(self, execution_id: str):
        self.instances.update_one(
            {"execution_id": execution_id},
            {"$set": {"assigned_to": None, "current_node": None, "pending_ui_request": None, "updated_at": datetime.utcnow()}}
        )

    def increment_chain_position(self, execution_id: str) -> int:
        """
        Increments chain_position for looping sequential nodes (used by
        rule-based/prompt-based assignment functions to resolve the next
        approver in an ordered list on each re-visit of the same node).
        Returns the new chain_position value.
        """
        doc = self.instances.find_one_and_update(
            {"execution_id": execution_id},
            {
                "$inc": {"chain_position": 1},
                "$set": {"updated_at": datetime.utcnow()},
            },
            return_document=True,
        )
        return (doc or {}).get("chain_position", 0)

    def reset_chain_position(self, execution_id: str):
        self.instances.update_one(
            {"execution_id": execution_id},
            {"$set": {"chain_position": 0, "updated_at": datetime.utcnow()}}
        )

    def get_node_config(self, workflow_id: str, node_id: str) -> dict:
        doc = self.process_configs.find_one(
            {
                "agentic_process.workflow_id": workflow_id,
                "agentic_process.phaseGraphs.graph.nodes.nodeId": node_id,
            },
            {"agentic_process.phaseGraphs": 1},
        )
        if not doc:
            print(f"[Database] get_node_config: No Process_Configs doc found for workflow {workflow_id}")
            return {}
        for phase in doc.get("agentic_process", {}).get("phaseGraphs", []):
            for node in phase.get("graph", {}).get("nodes", []):
                if node.get("nodeId") == node_id:
                    print(f"[Database] get_node_config: Successfully retrieved config for node {node_id}")
                    return node.get("config", {}) or {}
        print(f"[Database] get_node_config: Node {node_id} not matched in phaseGraphs")
        return {}

    def save_node_assignment_config(
        self,
        workflow_id: str,
        node_id: str,
        users: list,
        roles: list,
        assignment_ref: Optional[str],
    ) -> bool:
        """
        Persists {users, roles, assignmentRef} into the live node's
        config.assignment inside Process_Configs. Uses arrayFilters to target
        the exact phase + node without needing to know the phase index.
        Returns True if a node was matched and updated.
        """
        result = self.process_configs.update_one(
            {
                "agentic_process.workflow_id": workflow_id,
                "agentic_process.phaseGraphs.graph.nodes.nodeId": node_id,
            },
            {
                "$set": {
                    "agentic_process.phaseGraphs.$[phase].graph.nodes.$[node].config.assignment": {
                        "users": users or [],
                        "roles": roles or [],
                        "assignmentRef": assignment_ref,
                    },
                    "updated_at": datetime.utcnow(),
                }
            },
            array_filters=[
                {"phase.graph.nodes.nodeId": node_id},
                {"node.nodeId": node_id},
            ],
        )
        return result.matched_count > 0

    def get_assignment_rule(self, ref_id: str) -> Optional[dict]:
        return self.assignment_registry.find_one({"_id": ref_id})

    def save_assignment_rule(
        self,
        ref_id: str,
        node_id: str,
        function_code: str,
        project_id: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
    ) -> None:
        self.assignment_registry.update_one(
            {"_id": ref_id},
            {
                "$set": {
                    "nodeId": node_id,
                    "function_code": function_code,
                    "projectId": project_id,
                    "orgId": org_id,
                    "subscriberId": subscriber_id,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    def find_stale_instances(self, stale_after_minutes: int = 15) -> list[InstanceDocument]:
        """
        Return instances that were left in 'running' status and have not been
        updated for at least `stale_after_minutes` minutes.  These are orphaned
        by a hard process crash that acked the Valkey message but never finished.
        'waiting_human' instances are intentionally paused and must NOT be
        re-enqueued; only truly executing (running) ones are stale candidates.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=stale_after_minutes)
        docs = self.instances.find({
            "status": "running",
            "updated_at": {"$lt": cutoff},
        })
        return [InstanceDocument(**d) for d in docs]