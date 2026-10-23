"""
Plan reviser module for Natural Language Workflow Builder.

Handles natural language text feedback from users to update an existing plan,
supporting dual revision paths (structural re-planning vs. detail description patching).
"""

import copy
import json
import logging
from typing import Any, Dict, List, Literal, Optional

from services.plan_credit_estimator import estimate_workflow_cost
from services.gemini_service import async_generate, GeminiGenerateConfig, extract_text, get_gemini_api_key
from nl_builder.models import (
    NLPlanPreview,
    NLPlanResponse,
)
from nl_builder.plan_generator import (
    generate_plan_summary,
    generate_nl_plan,
    _estimator_steps_from_phase_graphs,
    _logical_steps_from_skeleton,
    _node_description,
    _skeleton_from_graph,
    _cost_estimate_from_phase_graphs,
    _topology_counts_from_phase_graphs,
)

logger = logging.getLogger(__name__)

REVISION_CLASSIFICATION_MODEL = "gemini-3.5-flash-lite"


async def classify_feedback(
    feedback: str,
    current_plan_preview: Optional[NLPlanPreview] = None,
    api_key: Optional[str] = None,
) -> Literal["structural", "detail"]:
    """
    Classifies user feedback into 'structural' or 'detail'.

    - 'structural': User wants to add, delete, split, merge, or re-order steps/nodes/phases.
    - 'detail': User wants to change field labels, text wording, parameter names, or validation rules.
    """
    plan_desc = ""
    if current_plan_preview:
        plan_desc = f"Current Plan Summary: {current_plan_preview.summary}"

    prompt = f"""
    You are a classification assistant for workflow editing.

    {plan_desc}

    USER FEEDBACK:
    \"\"\"{feedback}\"\"\"

    Determine if this feedback requires:
    - "structural": Adding new steps/nodes, removing steps, changing execution order, or splitting screens/tools.
    - "detail": Modifying descriptions, form field labels, API parameters, or validation text within existing steps.

    Return ONLY a JSON object:
    {{
      "category": "structural" | "detail",
      "reasoning": "brief explanation"
    }}
    """

    try:
        key = api_key or get_gemini_api_key()
        resp = await async_generate(
            REVISION_CLASSIFICATION_MODEL,
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
            api_key=key,
        )
        raw_text = extract_text(resp)
        parsed = json.loads(raw_text)
        cat = str(parsed.get("category", "")).lower().strip()
        if cat in ("structural", "detail"):
            return cat  # type: ignore
    except Exception as exc:
        logger.warning("Feedback classification failed, defaulting to 'structural': %s", exc)

    # Heuristic fallback
    fb_lower = feedback.lower()
    structural_keywords = ["add ", "remove ", "delete ", "create new", "split ", "after ", "before ", "phase"]
    if any(kw in fb_lower for kw in structural_keywords):
        return "structural"
    return "detail"


async def _apply_detail_patches(
    phase_graphs: List[Dict[str, Any]],
    feedback: str,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    LLM call to identify node description updates and apply them to phase_graphs in place.
    """
    updated_graphs = copy.deepcopy(phase_graphs)

    nodes_summary = []
    for pg in updated_graphs:
        graph = pg.get("graph") or pg
        for node in graph.get("nodes", []):
            nodes_summary.append({
                "nodeId": node.get("nodeId"),
                "name": node.get("name"),
                "type": node.get("type"),
                "description": _node_description(node),
            })

    prompt = f"""
    You are a node editor assistant. Apply the user's feedback by updating descriptions for affected nodes.

    CURRENT NODES:
    {json.dumps(nodes_summary, indent=2)}

    USER FEEDBACK:
    \"\"\"{feedback}\"\"\"

    Return ONLY a JSON object listing updated descriptions for modified nodes:
    {{
      "patches": [
        {{
          "nodeId": "node_id_here",
          "updatedDescription": "New description reflecting user request..."
        }}
      ]
    }}
    """

    try:
        key = api_key or get_gemini_api_key()
        resp = await async_generate(
            REVISION_CLASSIFICATION_MODEL,
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
            api_key=key,
        )
        raw_text = extract_text(resp)
        parsed = json.loads(raw_text)
        patches = parsed.get("patches", [])

        patch_map = {p.get("nodeId"): p.get("updatedDescription") for p in patches if p.get("nodeId")}

        for pg in updated_graphs:
            graph = pg.get("graph") or pg
            for node in graph.get("nodes", []):
                nid = node.get("nodeId")
                if nid in patch_map and patch_map[nid]:
                    new_desc = patch_map[nid]
                    node["description"] = new_desc
                    cfg = node.get("config")
                    if not isinstance(cfg, dict):
                        cfg = {}
                        node["config"] = cfg
                    cfg["generationDescription"] = new_desc
                    gen = cfg.get("generation")
                    if isinstance(gen, dict):
                        gen["description"] = new_desc
                    elif hasattr(gen, "description"):
                        setattr(gen, "description", new_desc)

        logger.info("Applied detail patches to %d nodes", len(patch_map))
    except Exception as exc:
        logger.warning("Failed to apply detail patches: %s", exc)

    return updated_graphs


async def revise_nl_plan(
    session_id: str,
    feedback: str,
    stage0_session: Any,
    current_plan_response: Optional[NLPlanResponse] = None,
    *,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    api_key: Optional[str] = None,
) -> NLPlanResponse:
    """
    Main orchestrator for POST /aiqod-agent/agent/agentic/nl/plan/revise.

    Classifies feedback into structural or detail revision and executes the corresponding update.
    """
    from agent_new import get_subscriber_available_credits

    pid = project_id or getattr(stage0_session, "nl_plan_project_id", None) or "default_project"
    # Prefer the original NL — session.raw_prompt is the composed expander blob after Stage 0.
    raw_prompt = (
        getattr(stage0_session, "original_user_prompt", None)
        or getattr(stage0_session, "raw_prompt", None)
        or ""
    )
    current_preview = current_plan_response.plan if current_plan_response else None

    # 1. Classify feedback
    category = await classify_feedback(
        feedback=feedback,
        current_plan_preview=current_preview,
        api_key=api_key,
    )
    logger.info("Plan revision category for session_id=%s: %s", session_id, category)

    if category == "structural" or not current_plan_response:
        # Re-expand to enterprise steps from original prompt + feedback, then Stage 1.
        revised_parts = [raw_prompt.strip()] if raw_prompt.strip() else []
        if current_preview and current_preview.summary:
            revised_parts.append(f"CURRENT PLAN SUMMARY:\n{current_preview.summary}")
        revised_parts.append(f"REVISION REQUEST: {feedback}")
        revised_prompt = "\n\n".join(revised_parts)

        resp = await generate_nl_plan(
            session_id=session_id,
            project_id=pid,
            stage0_session=stage0_session,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            selected_name=getattr(stage0_session, "workflow_name", None),
            api_key=api_key,
            requirement_override=revised_prompt,
        )
        resp.status = "plan_revised"
        return resp

    # Detail path: patch existing phase graphs without re-building topology
    phase_graphs = current_plan_response.phaseGraphs
    updated_phase_graphs = await _apply_detail_patches(
        phase_graphs=phase_graphs,
        feedback=feedback,
        api_key=api_key,
    )

    # Regenerate plan summary preview
    updated_preview = await generate_plan_summary(
        phase_graphs=updated_phase_graphs,
        original_prompt=raw_prompt,
        api_key=api_key,
    )

    cost_steps = _estimator_steps_from_phase_graphs(updated_phase_graphs)
    if updated_phase_graphs and _topology_counts_from_phase_graphs(updated_phase_graphs)["nodeCount"] > 0:
        predicted_cost, cost_detail = _cost_estimate_from_phase_graphs(
            updated_phase_graphs,
            base_text=raw_prompt,
        )
    elif cost_steps:
        est = estimate_workflow_cost(steps=cost_steps)
        predicted_cost = est.predicted_cost
        cost_detail = None
    else:
        est = estimate_workflow_cost(prompt=raw_prompt or "Create a workflow")
        predicted_cost = est.predicted_cost
        cost_detail = None

    subscriber_avail = None
    sufficient = None
    if subscriber_id:
        try:
            subscriber_avail = get_subscriber_available_credits(subscriber_id)
            if subscriber_avail is not None:
                sufficient = subscriber_avail >= predicted_cost
        except Exception:
            pass

    graph = (updated_phase_graphs[0].get("graph") if updated_phase_graphs else None) or {}
    skeleton = _skeleton_from_graph(graph) if graph else current_plan_response.skeletonPlan
    steps = _logical_steps_from_skeleton(skeleton) or current_plan_response.steps

    return NLPlanResponse(
        sessionId=session_id,
        status="plan_revised",
        workflowId=current_plan_response.workflowId,
        workflowName=current_plan_response.workflowName,
        plan=updated_preview,
        phaseGraphs=updated_phase_graphs,
        steps=steps,
        estimatedCost=predicted_cost,
        costEstimate=cost_detail,
        subscriberAvailableUsd=subscriber_avail,
        sufficientBalance=sufficient,
        mode="prompt",
        # Execute API only accepts llm|fallback — never echo invalid "prompt".
        analysis_source=(
            current_plan_response.analysis_source
            if current_plan_response.analysis_source in ("llm", "fallback")
            else "llm"
        ),
        warnings=current_plan_response.warnings,
        skeletonPlan=skeleton,
    )
