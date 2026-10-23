"""
Integrated Regeneration Pipeline - Three-Phase Approach

This module implements the complete three-phase workflow regeneration pipeline
as discussed by the team:

Phase 1: Impact Analysis - Show user what will change
Phase 2: Surgical Editing - Generate minimal diffs
Phase 3: Validation & Repair - Ensure integrity and fix issues

This replaces the old "full regeneration" approach with a controlled,
user-approved, surgical editing process.
"""

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Dict, List, Optional, Set, Tuple

from impact_analysis import (
    ImpactAnalyzer,
    ImpactReport,
    ImpactSeverity,
    run_impact_analysis,
    format_impact_report_for_user,
)

from surgical_editing import (
    DiffGenerator,
    DiffApplicator,
    EditPlan,
    CodeEdit,
    EditType,
    generate_surgical_edit_plan,
    format_edit_plan_for_user,
)

from validation_repair import (
    WorkflowValidator,
    RepairEngine,
    ValidationReport,
    ValidationSeverity,
    run_validation_and_repair,
    format_validation_report,
)

from regeneration import (
    run_update_regeneration_pipeline,
    build_available_vars_register,
    build_node_batch_plan,
    AvailableVarsRegister,
)


class PipelinePhase(str, Enum):
    """Pipeline execution phases"""
    IMPACT_ANALYSIS = "impact_analysis"
    USER_APPROVAL = "user_approval"
    SURGICAL_EDITING = "surgical_editing"
    VALIDATION_REPAIR = "validation_repair"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class PipelineState:
    """State of the regeneration pipeline"""
    phase: PipelinePhase = PipelinePhase.IMPACT_ANALYSIS
    workflow_id: str = ""
    updated_node_ids: Set[str] = field(default_factory=set)
    proposed_changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Phase 1 results
    impact_report: Optional[ImpactReport] = None

    # Phase 2 results
    edit_plans: List[EditPlan] = field(default_factory=list)
    applied_edits: Dict[str, Any] = field(default_factory=dict)

    # Phase 3 results
    validation_report: Optional[ValidationReport] = None
    applied_fixes: List[Dict[str, Any]] = field(default_factory=list)

    # Overall
    success: bool = False
    error_message: str = ""
    iteration: int = 0
    max_iterations: int = 3


@dataclass
class PipelineCallbacks:
    """Callbacks for user interaction and progress"""
    on_impact_report: Optional[Callable[[ImpactReport], Awaitable[bool]]] = None  # Returns user approval
    on_edit_plan: Optional[Callable[[List[EditPlan]], Awaitable[bool]]] = None     # Returns user approval
    on_validation_report: Optional[Callable[[ValidationReport], Awaitable[bool]]] = None  # Returns continue/retry
    on_progress: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None


async def run_integrated_regeneration_pipeline(
    phase_graphs: List[Dict[str, Any]],
    updated_node_ids: Set[str],
    proposed_changes: Dict[str, Dict[str, Any]],
    current_codebase: Dict[str, str],
    global_inputs: List[Dict[str, Any]],
    llm_caller: Callable[[str, str, str], Awaitable[str]],
    callbacks: Optional[PipelineCallbacks] = None,
    max_iterations: int = 3,
    precomputed_impact_report: Optional[ImpactReport] = None,
    # New parameters for LLM-based impact analysis
    screen_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> PipelineState:
    """
    Run the complete three-phase regeneration pipeline.

    This implements the team's recommended approach:
    1. IMPACT ANALYSIS - Analyze and show user what will change
    2. USER APPROVAL - Get user consent for breaking changes
    3. SURGICAL EDITING - Generate minimal diffs, apply changes
    4. VALIDATION & REPAIR - Verify integrity, auto-fix issues
    5. Repeat 3-4 if needed (up to max_iterations)

    Args:
        phase_graphs: Workflow phase graphs
        updated_node_ids: Nodes being directly updated
        proposed_changes: Proposed changes per node
        current_codebase: Current code files
        global_inputs: Global workflow inputs
        llm_caller: LLM calling function
        callbacks: Optional user interaction callbacks
        max_iterations: Maximum validation/repair iterations
        precomputed_impact_report: Optional pre-computed impact report to skip Phase 1
        screen_metadata: Screen metadata for LLM-based impact analysis
        project_id, org_id, subscriber_id, user_id: Context for Angular generator

    Returns:
        PipelineState with results
    """
    state = PipelineState(
        workflow_id=phase_graphs[0].get("graph", {}).get("workflowId", "unknown"),
        updated_node_ids=updated_node_ids,
        proposed_changes=proposed_changes,
        max_iterations=max_iterations,
    )

    if callbacks is None:
        callbacks = PipelineCallbacks()

    try:
        # ============================================================
        # PHASE 1: IMPACT ANALYSIS
        # ============================================================
        if precomputed_impact_report is not None:
            # Use pre-computed impact report (from preview cache)
            state.phase = PipelinePhase.IMPACT_ANALYSIS
            state.impact_report = precomputed_impact_report
            await _report_progress(
                callbacks,
                "Using cached impact analysis",
                {
                    "phase": "impact_analysis",
                    "report": state.impact_report.to_dict(),
                    "formatted": format_impact_report_for_user(state.impact_report),
                    "cached": True,
                }
            )
        else:
            # Run fresh impact analysis
            state.phase = PipelinePhase.IMPACT_ANALYSIS
            await _report_progress(callbacks, "Starting impact analysis...", {"phase": "impact_analysis"})

            # Extract workflow_id from phase_graphs
            wf_id = phase_graphs[0].get("graph", {}).get("workflowId") if phase_graphs else None

            # Use LLM-based impact analysis for UI screens if LLM caller and codebase available
            state.impact_report = await run_impact_analysis(
                phase_graphs,
                updated_node_ids,
                proposed_changes,
                use_llm_analysis=True,
                llm_caller=llm_caller,
                current_codebase=current_codebase,
                screen_metadata=screen_metadata,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                workflow_id=wf_id,
            )

            await _report_progress(
                callbacks,
                "Impact analysis complete",
                {
                    "phase": "impact_analysis",
                    "report": state.impact_report.to_dict(),
                    "formatted": format_impact_report_for_user(state.impact_report),
                }
            )

        # Check if user approval needed
        if state.impact_report.user_approval_required:
            state.phase = PipelinePhase.USER_APPROVAL

            if callbacks.on_impact_report:
                approved = await callbacks.on_impact_report(state.impact_report)
            else:
                # Auto-approve if no callback (for testing)
                approved = True

            if not approved:
                state.phase = PipelinePhase.FAILED
                state.error_message = "User rejected changes due to breaking changes"
                return state

            await _report_progress(callbacks, "User approved changes", {"phase": "user_approval"})

        # ============================================================
        # PHASE 2: SURGICAL EDITING (with validation loop)
        # ============================================================
        state.phase = PipelinePhase.SURGICAL_EDITING

        for iteration in range(max_iterations):
            state.iteration = iteration + 1
            await _report_progress(
                callbacks,
                f"Surgical editing iteration {state.iteration}/{max_iterations}",
                {"phase": "surgical_editing", "iteration": state.iteration}
            )

            # Generate edit plans
            state.edit_plans = await generate_surgical_edit_plan(
                phase_graphs,
                updated_node_ids,
                proposed_changes,
                current_codebase,
                await _get_available_vars(phase_graphs, global_inputs),
                llm_caller
            )

            await _report_progress(
                callbacks,
                "Edit plan generated",
                {
                    "phase": "surgical_editing",
                    "plans": [p.node_id for p in state.edit_plans],
                    "formatted": format_edit_plan_for_user(state.edit_plans),
                }
            )

            # User approval for edit plan (optional)
            if callbacks.on_edit_plan:
                approved = await callbacks.on_edit_plan(state.edit_plans)
                if not approved:
                    state.phase = PipelinePhase.FAILED
                    state.error_message = "User rejected edit plan"
                    return state

            # Apply edits
            apply_success, apply_messages = _apply_all_edits(state.edit_plans, current_codebase)
            state.applied_edits = {
                "success": apply_success,
                "messages": apply_messages,
            }

            if not apply_success:
                state.error_message = f"Failed to apply edits: {apply_messages}"
                continue  # Try next iteration

            # ============================================================
            # PHASE 3: VALIDATION & REPAIR
            # ============================================================
            state.phase = PipelinePhase.VALIDATION_REPAIR
            await _report_progress(callbacks, "Running validation...", {"phase": "validation_repair"})

            state.validation_report, state.applied_fixes = await run_validation_and_repair(
                phase_graphs,
                current_codebase,
                global_inputs,
                llm_caller,
                max_repair_iterations=2,
            )

            await _report_progress(
                callbacks,
                "Validation complete",
                {
                    "phase": "validation_repair",
                    "report": state.validation_report.to_dict(),
                    "formatted": format_validation_report(state.validation_report),
                    "fixes_applied": len(state.applied_fixes),
                }
            )

            if state.validation_report.passed:
                state.success = True
                state.phase = PipelinePhase.COMPLETE
                await _report_progress(callbacks, "Pipeline completed successfully!", {"phase": "complete"})
                break

            # Check if user wants to continue
            if callbacks.on_validation_report:
                continue_retry = await callbacks.on_validation_report(state.validation_report)
                if not continue_retry:
                    state.phase = PipelinePhase.FAILED
                    state.error_message = "User chose not to retry after validation failures"
                    break

            # Update proposed_changes based on fixes for next iteration
            _update_proposed_changes_from_fixes(state, state.applied_fixes)

        else:
            # Max iterations reached
            state.phase = PipelinePhase.FAILED
            state.error_message = f"Max iterations ({max_iterations}) reached without passing validation"

    except Exception as e:
        state.phase = PipelinePhase.FAILED
        state.error_message = f"Pipeline error: {str(e)}"
        await _report_progress(callbacks, f"Pipeline failed: {e}", {"phase": "failed", "error": str(e)})

    return state


async def _get_available_vars(
    phase_graphs: List[Dict[str, Any]],
    global_inputs: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Get combined available variables from all phases"""
    all_vars = {"globals": [], "nodeOutputs": []}

    for phase_entry in phase_graphs:
        graph = phase_entry.get("graph", {})
        batch_plan = build_node_batch_plan(graph)
        available = build_available_vars_register(graph, batch_plan, global_inputs)

        all_vars["globals"] = [
            {"name": g.name, "dataType": g.dataType} for g in available.globals
        ]
        all_vars["nodeOutputs"].extend([
            {"nodeId": e.nodeId, "outputs": e.outputs} for e in available.nodeOutputs
        ])

    return all_vars


def _apply_all_edits(plans: List[EditPlan], codebase: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Apply all edits from all plans"""
    all_messages = []
    all_success = True

    for plan in plans:
        success, messages = DiffApplicator.apply_plan(plan, codebase)
        all_messages.extend([f"{plan.node_id}: {m}" for m in messages])
        if not success:
            all_success = False

    return all_success, all_messages


def _update_proposed_changes_from_fixes(state: PipelineState, fixes: List[Dict[str, Any]]) -> None:
    """Update proposed changes based on applied fixes for next iteration"""
    for fix in fixes:
        if fix.get("type") == "contract_fix" and fix.get("success"):
            node_id = fix.get("node_id")
            param = fix.get("parameter")
            new_binding = fix.get("new_binding")

            if node_id and param and new_binding:
                if node_id not in state.proposed_changes:
                    state.proposed_changes[node_id] = {}
                if "inputBindings" not in state.proposed_changes[node_id]:
                    state.proposed_changes[node_id]["inputBindings"] = {}
                state.proposed_changes[node_id]["inputBindings"][param] = new_binding

        elif fix.get("type") == "syntax_fix" and fix.get("success"):
            # Syntax fixes are already applied to codebase
            pass


async def _report_progress(
    callbacks: PipelineCallbacks,
    message: str,
    data: Dict[str, Any]
) -> None:
    """Report progress via callback if available"""
    if callbacks.on_progress:
        await callbacks.on_progress(message, data)


# ============================================================
# CONVENIENCE FUNCTIONS FOR COMMON USE CASES
# ============================================================

async def run_regeneration_with_auto_approval(
    phase_graphs: List[Dict[str, Any]],
    updated_node_ids: Set[str],
    proposed_changes: Dict[str, Dict[str, Any]],
    current_codebase: Dict[str, str],
    global_inputs: List[Dict[str, Any]],
    llm_caller: Callable[[str, str, str], Awaitable[str]],
    max_iterations: int = 3,
    precomputed_impact_report: Optional[ImpactReport] = None,
    # New parameters for LLM-based impact analysis
    screen_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> PipelineState:
    """
    Run regeneration pipeline with automatic approval (for automated workflows).
    """
    return await run_integrated_regeneration_pipeline(
        phase_graphs,
        updated_node_ids,
        proposed_changes,
        current_codebase,
        global_inputs,
        llm_caller,
        callbacks=None,  # No callbacks = auto-approve
        max_iterations=max_iterations,
        precomputed_impact_report=precomputed_impact_report,
        screen_metadata=screen_metadata,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        workflow_id=workflow_id,
    )


async def run_regeneration_with_user_callbacks(
    phase_graphs: List[Dict[str, Any]],
    updated_node_ids: Set[str],
    proposed_changes: Dict[str, Dict[str, Any]],
    current_codebase: Dict[str, str],
    global_inputs: List[Dict[str, Any]],
    llm_caller: Callable[[str, str, str], Awaitable[str]],
    on_impact_report: Callable[[ImpactReport], Awaitable[bool]],
    on_edit_plan: Callable[[List[EditPlan]], Awaitable[bool]],
    on_validation_report: Callable[[ValidationReport], Awaitable[bool]],
    on_progress: Callable[[str, Dict[str, Any]], Awaitable[None]],
    max_iterations: int = 3,
    precomputed_impact_report: Optional[ImpactReport] = None,
    # New parameters for LLM-based impact analysis
    screen_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> PipelineState:
    """
    Run regeneration pipeline with full user interaction callbacks.
    """
    callbacks = PipelineCallbacks(
        on_impact_report=on_impact_report,
        on_edit_plan=on_edit_plan,
        on_validation_report=on_validation_report,
        on_progress=on_progress,
    )

    return await run_integrated_regeneration_pipeline(
        phase_graphs,
        updated_node_ids,
        proposed_changes,
        current_codebase,
        global_inputs,
        llm_caller,
        callbacks=callbacks,
        max_iterations=max_iterations,
        precomputed_impact_report=precomputed_impact_report,
        screen_metadata=screen_metadata,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        workflow_id=workflow_id,
    )


def create_proposed_changes_from_node_update(
    node: Dict[str, Any],
    update_request: str,
) -> Dict[str, Any]:
    """
    Helper to create proposed_changes dict from a node update request.
    This mimics what the LLM would generate in regenerate_batch.

    Idempotency: If the same ``update_request`` was already applied in a
    previous iteration (e.g. during a validation retry), this returns empty
    ``proposed_changes`` so the LLM doesn't re-apply the same change again.
    The first iteration marks the prompt in ``_applied_update_prompts`` via
    ``_apply_edit_plans_to_phase_graphs``; subsequent retries see the marker
    and skip re-generation.
    """
    config = node.get("config", {})
    applied_prompts = config.get("_applied_update_prompts") or []
    if isinstance(applied_prompts, list) and update_request in applied_prompts:
        # Already applied — return minimal stub so orchestrator can still iterate
        # (Phase 2/3) without re-applying the user-requested change.
        nid = node.get("nodeId")
        return {nid: {}} if nid else {}

    current_outputs = config.get("declaredOutputs", [])

    # Do NOT auto-add outputs based on prompt parsing.
    # Let the LLM-driven pipeline (analyze_update_scope + update_screen_ir + regenerate_files_for_update)
    # handle output additions naturally based on full context (IR schema, current code, user prompt).
    # This avoids false positives from hardcoded pattern matching.
    updated_outputs = list(current_outputs)

    return {
        node.get("nodeId"): {
            "inputBindings": config.get("inputBindings", {}),
            "declaredOutputs": updated_outputs,
            "generationDescription": config.get("generationDescription", ""),
            "_update_request": update_request,
        }
    }