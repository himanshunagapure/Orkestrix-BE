from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime

class InstanceDocument(BaseModel):
    execution_id: str = Field(..., description="Unique run identifier")
    workflow_id: str = Field(..., description="Which workflow template")
    thread_id: str = Field(..., description="LangGraph checkpoint key")
    status: str = Field(..., description="initialized | running | paused | Pending Review | completed | stopped | failed")
    current_node: Optional[str] = Field(None, description="Last active node")
    assigned_to: Optional[Any] = Field(None, description="Human assignee UUID, or {'users': [...], 'roles': [...]}")
    pending_ui_request: Optional[Dict[str, Any]] = Field(None, description="The exact ui_payload dict published as the 'ui_request' SSE event for the current pause — set alongside assigned_to so it survives independent of the state blob's execution_history")
    created_by: Optional[str] = Field(None, description="userId of the person who started this instance")
    subscriber_id: Optional[str] = Field(None, description="Subscriber ID from frontend session")
    org_id: Optional[str] = Field(None, description="Organization ID from frontend session")
    jwt_token: Optional[str] = Field(None, description="JWT authentication token")
    chain_position: int = Field(0, description="How many times a looping sequential node has been visited")
    initial_inputs: Dict[str, Any] = Field(default_factory=dict, description="Inputs provided during initialization")
    state: Dict[str, Any] = Field(default_factory=dict, description="Full graph state snapshot")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class StartRequest(BaseModel):
    workflow_id: str
    initial_state: Dict[str, Any]
    userId: Optional[str] = None
    subscriberId: Optional[str] = None
    orgId: Optional[str] = None
    jwtToken: Optional[str] = None


class MyTasksRequest(BaseModel):
    workflow_id: str
    userId: Optional[str] = None
    roleId: Optional[str] = None
    filterType: str = "assigned"  # "assigned" | "created"


class MyTaskWorkflowsRequest(BaseModel):
    orgId: str
    subscriberId: Optional[str] = None
    userId: Optional[str] = None
    roleId: Optional[str] = None
    filterType: str = "assigned"

class ResumeRequest(BaseModel):
    payload: Any
    userid: Optional[str] = None
    roleid: Optional[str] = None