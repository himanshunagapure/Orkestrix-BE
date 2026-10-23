"""
Pydantic request and response models for the Natural Language Workflow Builder API.
"""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class LogicalStep(BaseModel):
    """
    Normalized representation of a logical requirement step.
    Canonical shape consumed by downstream planners (kept here so nl_builder
    does not import agent_new at module load — avoids circular imports).
    """
    stepId: str = Field(..., description="Stable identifier like 'L1', 'L2', ...")
    name: str = Field(..., description="Short human-readable step name")
    description: str = Field(..., description="Detailed step description")
    inputs: list[str] = Field(
        default_factory=list,
        description="Names of key inputs or preconditions for this step",
    )
    outputs: list[str] = Field(
        default_factory=list,
        description="Names of key outputs or postconditions for this step",
    )
    preferredKind: Optional[Literal["ui_screen", "tool", "agent"]] = Field(
        default=None,
        description="Preferred implementation kind, if clear from the requirement",
    )

    @field_validator("stepId")
    @classmethod
    def normalize_step_id(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("stepId cannot be empty")
        return v

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("value cannot be empty")
        return v


class NLExpandRequest(BaseModel):
    """POST /aiqod-agent/agent/agentic/nl/expand — prompt → enterprise steps."""
    prompt: str = Field(..., min_length=1, description="Original natural-language workflow request")
    projectId: Optional[str] = None
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None


class NLStepsReviseRequest(BaseModel):
    """POST /aiqod-agent/agent/agentic/nl/steps/revise — change the step list, not the graph."""
    prompt: str = Field(..., min_length=1, description="Original user prompt (same as /nl/expand, for context)")
    steps: List[LogicalStep] = Field(
        ...,
        min_length=1,
        description="Latest enterprise steps from /nl/expand or the previous /nl/steps/revise",
    )
    feedback: str = Field(..., min_length=1, description="This round's requested change to the step plan")
    projectId: Optional[str] = None
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None


class NLStepsResponse(BaseModel):
    """Shared response for /nl/expand and /nl/steps/revise."""
    prompt: str
    steps: List[LogicalStep] = Field(default_factory=list)
    stepCount: int = 0
    source: Literal["llm", "fallback"] = "llm"
    warnings: List[str] = Field(default_factory=list)
    feedback: Optional[str] = None


class NLLlmCallEstimateResponse(BaseModel):
    """Pre-flight LLM cost for one /nl/expand or /nl/steps/revise call."""
    operation: Literal["expand", "revise"]
    endpoint: str
    model: str
    predictedCost: float
    estimatedCostUsd: float
    inputTokens: int
    outputTokens: int
    totalTokens: int
    source: Literal["observed", "blended", "default"]
    sampleCount: int = 0
    observedAvgCostUsd: Optional[float] = None
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    subscriberAvailableUsd: Optional[float] = None
    sufficientBalance: Optional[bool] = None


class NLPlanRequest(BaseModel):
    """Request payload for POST /aiqod-agent/agent/agentic/nl/plan"""
    sessionId: str = Field(..., description="Stage 0 session ID from /stage0/start")
    projectId: str = Field(..., description="Project ID for persistent context and bindings")
    orgId: Optional[str] = Field(None, description="Organization ID")
    subscriberId: Optional[str] = Field(None, description="Subscriber ID for credit checks")
    userId: Optional[str] = Field(None, description="User ID")
    selectedName: Optional[str] = Field(None, description="Workflow display name")
    selectedType: Optional[str] = Field(None, description="Workflow type tag")
    selectedCategory: Optional[str] = Field(None, description="Workflow category tag")


class NLPlanReviseRequest(BaseModel):
    """Request payload for POST /aiqod-agent/agent/agentic/nl/plan/revise"""
    sessionId: str = Field(..., description="Stage 0 session ID containing active NL plan")
    feedback: str = Field(..., description="Natural language feedback describing requested plan changes")
    projectId: Optional[str] = Field(None, description="Project ID")
    orgId: Optional[str] = Field(None, description="Organization ID")
    subscriberId: Optional[str] = Field(None, description="Subscriber ID")
    userId: Optional[str] = Field(None, description="User ID")


class NLNodeSummary(BaseModel):
    """Human-readable node description for UI plan preview."""
    nodeId: str = Field(..., description="Unique node identifier in snake_case")
    name: str = Field(..., description="Human readable node label")
    type: str = Field(..., description="Node kind: tool | ui_screen | router | end")
    description: str = Field(..., description="Detailed description of node responsibilities")


class NLPhaseSummary(BaseModel):
    """Phase-level summary grouping nodes for UI preview."""
    phaseId: str = Field(..., description="Phase identifier e.g. phase_1")
    phaseName: str = Field(..., description="Phase title")
    description: str = Field(..., description="Phase goal")
    nodes: List[NLNodeSummary] = Field(default_factory=list, description="Nodes in this phase")


class NLPlanPreview(BaseModel):
    """Structured plan preview for UI rendering."""
    summary: str = Field(..., description="High-level natural language summary of the entire workflow")
    phases: List[NLPhaseSummary] = Field(default_factory=list, description="Per-phase node breakdown")
    nodeCount: int = Field(0, description="Total node count across all phases")
    edgeCount: int = Field(0, description="Total edge count across all phases")
    uiScreenCount: int = Field(0, description="Count of ui_screen nodes")
    toolCount: int = Field(0, description="Count of tool nodes")
    routerCount: int = Field(0, description="Count of router nodes")


class NLCostStageItem(BaseModel):
    """One line item in a topology-based cost breakdown."""
    kind: str
    count: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    costUsd: float = 0.0
    rationale: Optional[str] = None


class NLCostEstimate(BaseModel):
    """
    Cost shown after /nl/plan (and revise).

    `estimateKind=topology_execute` means Stage 0 + scratch are done; this is the
    remaining /plan/execute (gap codegen) cost from actual GENERATE node counts.
    """
    estimateKind: Literal["topology_execute"] = "topology_execute"
    predictedCost: float = 0.0
    executeCostUsd: float = 0.0
    stage0CostUsd: float = 0.0
    previewCostUsd: float = 0.0
    totalCostUsd: float = 0.0
    uiScreenCount: int = 0
    toolCount: int = 0
    nodeCount: int = 0
    edgeCount: int = 0
    routerCount: int = 0
    complexity: str = "medium"
    totalInputTokens: int = 0
    totalOutputTokens: int = 0
    stages: List[NLCostStageItem] = Field(default_factory=list)
    assumptions: Dict[str, Any] = Field(default_factory=dict)


class NLPlanResponse(BaseModel):
    """Response payload returned by both /nl/plan and /nl/plan/revise."""
    sessionId: str = Field(..., description="Active session ID")
    status: str = Field(..., description="Status flag: 'plan_ready' | 'plan_revised'")
    workflowId: str = Field(..., description="Generated or assigned workflow ID")
    workflowName: str = Field(..., description="Workflow display name")
    plan: NLPlanPreview = Field(..., description="Human readable plan preview for frontend")
    phaseGraphs: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of PhaseGraphResult dicts ready for /plan/execute"
    )
    steps: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Flattened logical steps list"
    )
    estimatedCost: float = Field(
        0.0,
        description=(
            "Remaining credit cost to run /plan/execute, from actual GENERATE "
            "ui_screen/tool node counts (not the rough pre-Stage-0 estimate)."
        ),
    )
    costEstimate: Optional[NLCostEstimate] = Field(
        None,
        description="Detailed remaining-execute cost breakdown for the UI confirm step",
    )
    subscriberAvailableUsd: Optional[float] = Field(None, description="Available credit balance in USD")
    sufficientBalance: Optional[bool] = Field(None, description="True if balance is sufficient")
    mode: str = Field("prompt", description="Pipeline execution mode")
    analysis_source: Optional[Literal["llm", "fallback"]] = Field(
        "llm",
        description="Must be llm|fallback for /plan/execute compatibility",
    )
    warnings: List[str] = Field(default_factory=list, description="Planning warnings if any")
    skeletonPlan: Optional[Dict[str, Any]] = Field(
        None,
        description="Stage 1 SkeletonPlan used for enrichment (nodes, edges, entry/end)",
    )

    @field_validator("analysis_source", mode="before")
    @classmethod
    def coerce_analysis_source(cls, v: Any) -> Optional[str]:
        if v in ("llm", "fallback", None):
            return v
        # Legacy NL responses stored "prompt" on stage0_sessions.
        return "llm"
