"""
Plan generator module for Natural Language Workflow Builder.

Converts enterprise logical steps (from prompt-mode expansion + Stage 0) into a
validated LangGraph via Stage 1 skeleton / scratch pipeline, plus a preview
summary and remaining-execute cost estimate.
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from stage0 import format_stage0_planner_context
from scratch_graph_planner import (
    plan_phase_graph_scratch,
    build_prompt_mode_steps,
)
from services.plan_credit_estimator import (
    estimate_workflow_cost,
    estimate_execute_cost_from_topology,
)
from services.gemini_service import async_generate, GeminiGenerateConfig, extract_text, get_gemini_api_key
from services.llm_token_tracker import record_llm_call
from nl_builder.models import (
    LogicalStep,
    NLPlanPreview,
    NLPhaseSummary,
    NLNodeSummary,
    NLPlanResponse,
    NLCostEstimate,
    NLCostStageItem,
)

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_MODEL = "gemini-3.5-flash-lite"


class RequirementAnalysisResult(BaseModel):
    """
    Top-level LLM output schema for requirement analysis.
    Designed to be used as Gemini JSON schema.
    """
    steps: list[LogicalStep] = Field(
        default_factory=list,
        description="Ordered logical steps derived from the requirement prompt",
    )


class RequirementAnalysisResponse(BaseModel):
    """
    Response payload returned after analysis & normalization.
    """
    steps: list[LogicalStep]
    source: Literal["llm", "fallback"]
    warnings: list[str] = Field(default_factory=list)


def _normalize_logical_steps(
    raw_steps: list[LogicalStep],
    *,
    preserve_ids: bool = False,
) -> tuple[list[LogicalStep], list[str]]:
    """
    Apply deterministic normalization rules on logical steps:
    - Ensure contiguous stepIds: L1, L2, ...
    - Strip whitespace from all fields
    - Drop obviously invalid/empty steps
    - Normalize preferredKind values
    """
    normalized: list[LogicalStep] = []
    warnings: list[str] = []

    # Filter out any obviously broken entries
    for idx, step in enumerate(raw_steps):
        try:
            # Gemini parsed models (e.g. EnterpriseStepSpec) are not LogicalStep.
            if not isinstance(step, (LogicalStep, dict)) and hasattr(step, "model_dump"):
                step = step.model_dump(mode="json")
            # Re-validate to ensure all validators run
            step = LogicalStep.model_validate(step)
        except ValidationError as e:
            warnings.append(f"Discarded step at index {idx} due to validation error: {e.errors()}")
            continue

        # Clean inputs/outputs: strip blanks and de-duplicate while preserving order
        def _clean_list(values: list[str]) -> list[str]:
            seen = set()
            result: list[str] = []
            for item in values or []:
                s = (item or "").strip()
                if not s or s in seen:
                    continue
                seen.add(s)
                result.append(s)
            return result

        cleaned_inputs = _clean_list(step.inputs)
        cleaned_outputs = _clean_list(step.outputs)

        preferred = step.preferredKind
        if preferred:
            preferred_l = preferred.lower()
            # Map a few common synonyms into canonical values
            if preferred_l in {"ui", "screen", "ui-screen", "ui_screen"}:
                preferred = "ui_screen"
            elif preferred_l in {"service", "api", "tool"}:
                preferred = "tool"
            elif preferred_l in {"agent", "workflow", "ai_agent"}:
                preferred = "agent"
            elif preferred_l in {"hybrid", "free", "auto"}:
                preferred = None
            else:
                warnings.append(
                    f"Unknown preferredKind '{preferred}' for step '{step.stepId}', clearing it."
                )
                preferred = None

        normalized.append(
            LogicalStep(
                stepId=step.stepId,
                name=step.name,
                description=step.description,
                inputs=cleaned_inputs,
                outputs=cleaned_outputs,
                preferredKind=preferred,
            )
        )

    # Reassign deterministic stepIds: L1..Ln (unless caller wants to preserve IDs)
    final_steps: list[LogicalStep] = []
    for i, step in enumerate(normalized, start=1):
        new_id = step.stepId if preserve_ids else f"L{i}"
        final_steps.append(
            LogicalStep(
                stepId=new_id,
                name=step.name,
                description=step.description,
                inputs=step.inputs,
                outputs=step.outputs,
                preferredKind=step.preferredKind,
            )
        )

    if not final_steps:
        warnings.append("No valid logical steps were produced after normalization.")

    return final_steps, warnings


def _fallback_requirement_analysis(prompt: str) -> RequirementAnalysisResponse:
    """
    Heuristic fallback when LLM-based requirement analysis fails.
    Splits the prompt into coarse sentences and turns them into steps.
    """
    text = (prompt or "").strip()
    if not text:
        return RequirementAnalysisResponse(steps=[], source="fallback", warnings=["Empty prompt"])

    # Naive sentence splitting on ., ?, !
    parts = re.split(r"[\.!?]\s+", text)
    steps: list[LogicalStep] = []
    for idx, part in enumerate(parts, start=1):
        s = part.strip()
        if not s:
            continue
        # Use first few words as a name
        words = s.split()
        name = " ".join(words[:7])
        steps.append(
            LogicalStep(
                stepId=f"L{idx}",
                name=name,
                description=s,
                inputs=[],
                outputs=[],
                preferredKind=None,
            )
        )

    normalized, warnings = _normalize_logical_steps(steps)
    if not normalized:
        warnings.append("Fallback heuristic could not extract any steps.")
    return RequirementAnalysisResponse(steps=normalized, source="fallback", warnings=warnings)


async def analyze_requirements_with_llm(
    prompt: str,
    api_key: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> RequirementAnalysisResponse:
    """
    LLM-based Requirement Analyzer that converts a natural language requirement
    prompt into a validated, normalized list of logical steps.
    """
    if api_key is None:
        api_key = get_gemini_api_key()

    analysis_prompt = f"""
    You are a senior solution architect.
    Decompose the following requirement into an ordered list of logical steps.

    The requirement may be either:
    a) A single high-level paragraph describing the whole process, or
    b) Already broken into "STEP 1", "step 2", etc.

    You MUST:
    - Identify clear, implementation-ready logical steps.
    - Use SHORT, action-oriented names (verbs) for each step.
    - Provide concise but specific descriptions.
    - Fill inputs/outputs with key business entities or data artifacts when they are obvious.
    - Set preferredKind to:
        - "ui_screen" when the step is about user interaction or forms/screens.
        - "tool" when it is pure backend / API / computation / integration.
        - "agent" when it is clearly an autonomous multi-step agentic decision.
      If unclear, you may leave preferredKind null and focus on a good description.

    Requirement:
    \"\"\"{prompt}\"\"\"

    Return ONLY structured data according to the provided JSON schema.
    """

    try:
        started_at = datetime.utcnow()
        response = await async_generate(
            "gemini-3-flash-preview",
            analysis_prompt,
            config=GeminiGenerateConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=RequirementAnalysisResult,
            ),
            api_key=api_key,
        )
        finished_at = datetime.utcnow()

        usage = getattr(response, "usage_metadata", None)
        if usage:
            prompt_tokens = getattr(usage, "prompt_token_count", 0)
            completion_tokens = getattr(usage, "candidates_token_count", 0)
            total_tokens = getattr(usage, "total_token_count", 0) or (
                prompt_tokens + completion_tokens
            )
            try:
                from agent_new import record_agent_llm_usage
                record_agent_llm_usage(
                    "gemini-3-flash-preview",
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                )
            except Exception:
                pass

            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                project_id=None,
                screen_id=None,
                operation="requirement-analysis",
                sub_operation="llm-requirement-analyzer",
                model="gemini-3-flash-preview",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                status="success",
                started_at=started_at,
                finished_at=finished_at,
            )

        parsed: RequirementAnalysisResult = response.parsed
        normalized_steps, warnings = _normalize_logical_steps(parsed.steps)
        return RequirementAnalysisResponse(
            steps=normalized_steps,
            source="llm",
            warnings=warnings,
        )
    except Exception as e:
        logger.warning(f"Error during LLM requirement analysis: {e}")
        record_llm_call(
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=None,
            screen_id=None,
            operation="requirement-analysis",
            sub_operation="llm-requirement-analyzer",
            model="gemini-3-flash-preview",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            status="failed",
            metadata={"error": str(e)},
        )
        return _fallback_requirement_analysis(prompt)


class PlanSummaryResult(NLPlanPreview):
    """Internal model for structured JSON schema response from LLM summary call."""
    pass


async def generate_plan_summary(
    phase_graphs: List[Dict[str, Any]],
    original_prompt: str,
    api_key: Optional[str] = None,
) -> NLPlanPreview:
    """
    Generates a structured, human-readable preview summary for the UI from phase graphs.

    Attempts to call Gemini with a structured JSON schema. Falls back to a deterministic
    programmatic extraction if the LLM call fails.
    """
    total_nodes = 0
    total_edges = 0
    ui_screen_count = 0
    tool_count = 0
    router_count = 0

    topology_info = []

    for pg in phase_graphs:
        graph = pg.get("graph") or pg
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        phase_id = graph.get("phaseId") or graph.get("graphId") or "phase_1"
        phase_name = graph.get("phaseName") or graph.get("name") or "Phase 1"
        phase_desc = graph.get("description") or graph.get("goal") or ""

        total_nodes += len(nodes)
        total_edges += len(edges)

        node_details = []
        for n in nodes:
            ntype = n.get("type", "tool")
            nid = n.get("nodeId", "")
            nname = n.get("name", nid)
            ndesc = _node_description(n)
            if ntype == "ui_screen":
                ui_screen_count += 1
            elif ntype == "tool":
                tool_count += 1
            elif ntype == "router":
                router_count += 1

            node_details.append({
                "nodeId": nid,
                "name": nname,
                "type": ntype,
                "description": ndesc,
            })

        topology_info.append({
            "phaseId": phase_id,
            "phaseName": phase_name,
            "description": phase_desc,
            "nodes": node_details,
        })

    prompt_content = f"""
    You are a solution architect. Provide a concise, human-readable overview of this generated workflow.

    ORIGINAL USER PROMPT:
    \"\"\"{original_prompt}\"\"\"

    GENERATED WORKFLOW TOPOLOGY:
    {json.dumps(topology_info, indent=2)}

    Return ONLY a JSON object with this structure:
    {{
      "summary": "2-3 sentence overview of what the workflow does.",
      "phases": [
        {{
          "phaseId": "phase_1",
          "phaseName": "Phase Name",
          "description": "Phase summary...",
          "nodes": [
            {{
              "nodeId": "node_id",
              "name": "Node Label",
              "type": "ui_screen|tool|router|end",
              "description": "Node description..."
            }}
          ]
        }}
      ]
    }}
    """

    try:
        key = api_key or get_gemini_api_key()
        resp = await async_generate(
            DEFAULT_SUMMARY_MODEL,
            prompt_content,
            config=GeminiGenerateConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
            api_key=key,
        )
        raw_text = extract_text(resp)
        parsed_data = json.loads(raw_text)

        summary_text = parsed_data.get("summary") or f"Workflow generated with {total_nodes} nodes across {len(topology_info)} phase(s)."
        phases_out = []
        for p in parsed_data.get("phases", []):
            nodes_out = []
            for n in p.get("nodes", []):
                nodes_out.append(NLNodeSummary(
                    nodeId=n.get("nodeId", ""),
                    name=n.get("name", ""),
                    type=n.get("type", "tool"),
                    description=n.get("description", ""),
                ))
            phases_out.append(NLPhaseSummary(
                phaseId=p.get("phaseId", "phase_1"),
                phaseName=p.get("phaseName", "Phase 1"),
                description=p.get("description", ""),
                nodes=nodes_out,
            ))

        if phases_out:
            return NLPlanPreview(
                summary=summary_text,
                phases=phases_out,
                nodeCount=total_nodes,
                edgeCount=total_edges,
                uiScreenCount=ui_screen_count,
                toolCount=tool_count,
                routerCount=router_count,
            )
    except Exception as exc:
        logger.warning("LLM plan summary generation failed, using fallback summary: %s", exc)

    # Programmatic fallback
    fallback_phases = []
    for top in topology_info:
        fallback_nodes = [
            NLNodeSummary(
                nodeId=n["nodeId"],
                name=n["name"],
                type=n["type"],
                description=n["description"],
            )
            for n in top["nodes"]
        ]
        fallback_phases.append(NLPhaseSummary(
            phaseId=top["phaseId"],
            phaseName=top["phaseName"],
            description=top["description"],
            nodes=fallback_nodes,
        ))

    return NLPlanPreview(
        summary=f"Generated workflow containing {total_nodes} step(s) and {ui_screen_count} screen(s) based on your requirements.",
        phases=fallback_phases,
        nodeCount=total_nodes,
        edgeCount=total_edges,
        uiScreenCount=ui_screen_count,
        toolCount=tool_count,
        routerCount=router_count,
    )


def _extract_stage0_context(stage0_session: Any) -> str:
    stage0_state = getattr(stage0_session, "state", None)
    if stage0_state and getattr(stage0_state, "requirement_summary", None):
        return format_stage0_planner_context(stage0_state.requirement_summary) or ""
    return ""


def _attr_or_key(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return default


def _node_description(node: Any) -> str:
    """Scratch nodes store the spec on config.generationDescription, not top-level description."""
    if not node:
        return ""
    top = _attr_or_key(node, "description", default="") or ""
    if isinstance(top, str) and top.strip():
        return top.strip()
    cfg = _attr_or_key(node, "config", default=None)
    if not cfg:
        return ""
    gen_desc = _attr_or_key(cfg, "generationDescription", default="") or ""
    if isinstance(gen_desc, str) and gen_desc.strip():
        return gen_desc.strip()
    gen = _attr_or_key(cfg, "generation", default=None)
    nested = _attr_or_key(gen, "description", default="") or ""
    return nested.strip() if isinstance(nested, str) else ""


def _logical_steps_from_skeleton(skeleton: Any) -> List[Dict[str, Any]]:
    """Flatten skeleton nodes (except end) into LogicalStep-shaped dicts for API/cost."""
    nodes = _attr_or_key(skeleton, "nodes", default=[]) or []
    steps: List[Dict[str, Any]] = []
    idx = 1
    for n in nodes:
        ntype = _attr_or_key(n, "type", default="tool")
        if ntype == "end":
            continue
        kind = ntype if ntype in ("ui_screen", "tool") else None
        name = _attr_or_key(n, "name") or _attr_or_key(n, "nodeId", "node_id") or f"Step {idx}"
        steps.append({
            "stepId": _attr_or_key(n, "stepId", "step_id") or f"L{idx}",
            "name": name,
            "description": _node_description(n) or name,
            "preferredKind": kind,
        })
        idx += 1
    return steps


def _cost_steps_from_phase_graphs(phase_graphs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flat ui/tool nodes from assembled graphs (used to build estimator payload)."""
    steps: List[Dict[str, Any]] = []
    idx = 1
    for pg in phase_graphs or []:
        graph = pg.get("graph") or pg
        for n in graph.get("nodes") or []:
            ntype = n.get("type")
            if ntype not in ("ui_screen", "tool"):
                continue
            steps.append({
                "stepId": n.get("stepId") or n.get("nodeId") or f"L{idx}",
                "name": n.get("name") or n.get("nodeId") or f"Node {idx}",
                "description": _node_description(n),
                "preferredKind": ntype,
            })
            idx += 1
    return steps


def _estimator_steps_from_phase_graphs(phase_graphs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Cost estimator treats each top-level item as a *phase*. Passing a flat node
    list inflates cost (each node becomes its own phase). Wrap ui/tool nodes as
    subSteps of a single phase so preferredKind locks are honored.
    """
    substeps = _cost_steps_from_phase_graphs(phase_graphs)
    if not substeps:
        return []
    graph = (phase_graphs[0].get("graph") if phase_graphs else None) or (phase_graphs[0] if phase_graphs else {}) or {}
    return [{
        "stepId": graph.get("phaseId") or graph.get("graphId") or "phase_1",
        "name": graph.get("phaseName") or graph.get("name") or "Phase 1",
        "description": graph.get("description") or graph.get("goal") or "",
        "subSteps": substeps,
    }]


def _topology_counts_from_phase_graphs(phase_graphs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count assembled-graph nodes. ui/tool counts are GENERATE:* only (execute cost)."""
    nodes = 0
    edges = 0
    ui = 0
    tool = 0
    router = 0
    for pg in phase_graphs or []:
        graph = pg.get("graph") or pg
        ns = graph.get("nodes") or []
        es = graph.get("edges") or []
        nodes += len(ns)
        edges += len(es)
        for n in ns:
            ntype = n.get("type")
            if ntype == "router":
                router += 1
                continue
            if ntype not in ("ui_screen", "tool"):
                continue
            cap = n.get("capabilityRef") if isinstance(n.get("capabilityRef"), dict) else {}
            cref = str((cap or {}).get("capabilityId") or "")
            # Execute only generates GENERATE:* gaps. Reused ui:/tool: caps are free.
            if cref and not cref.startswith("GENERATE:"):
                continue
            if ntype == "ui_screen":
                ui += 1
            else:
                tool += 1
    return {
        "nodeCount": nodes,
        "edgeCount": edges,
        "uiScreenCount": ui,
        "toolCount": tool,
        "routerCount": router,
    }


def _cost_estimate_from_phase_graphs(
    phase_graphs: List[Dict[str, Any]],
    *,
    base_text: str = "",
) -> Tuple[float, NLCostEstimate]:
    """
    Remaining /plan/execute cost from actual GENERATE node counts.

    Prefer this after /nl/plan — Stage 0 + scratch are already done.
    """
    counts = _topology_counts_from_phase_graphs(phase_graphs)
    est = estimate_execute_cost_from_topology(
        ui_screen_count=counts["uiScreenCount"],
        tool_count=counts["toolCount"],
        node_count=counts["nodeCount"],
        edge_count=counts["edgeCount"],
        router_count=counts["routerCount"],
        base_text=base_text or "",
    )
    detail = NLCostEstimate(
        estimateKind="topology_execute",
        predictedCost=est.predicted_cost,
        executeCostUsd=est.execute_cost_usd,
        stage0CostUsd=0.0,
        previewCostUsd=0.0,
        totalCostUsd=est.total_cost_usd,
        uiScreenCount=est.ui_screen_count,
        toolCount=est.tool_count,
        nodeCount=counts["nodeCount"],
        edgeCount=counts["edgeCount"],
        routerCount=counts["routerCount"],
        complexity=est.complexity,
        totalInputTokens=est.total_input_tokens,
        totalOutputTokens=est.total_output_tokens,
        stages=[
            NLCostStageItem(
                kind=i.kind,
                count=i.count,
                inputTokens=i.input_tokens,
                outputTokens=i.output_tokens,
                costUsd=i.cost_usd,
                rationale=i.rationale,
            )
            for i in (est.items or [])
        ],
        assumptions=dict(est.assumptions or {}),
    )
    return float(est.predicted_cost), detail


def _skeleton_from_graph(graph_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compact topology snapshot from an assembled graph (for API `skeletonPlan`)."""
    if not graph_dict:
        return None
    nodes = graph_dict.get("nodes") or []
    edges = graph_dict.get("edges") or []
    if not nodes:
        return None
    entry = graph_dict.get("entryNodeId") or graph_dict.get("entry_node_id")
    if not entry:
        entry = nodes[0].get("nodeId")
    end_id = "end"
    for n in nodes:
        if n.get("type") == "end":
            end_id = n.get("nodeId") or "end"
            break
    return {
        "entryNodeId": entry,
        "endNodeId": end_id,
        "nodes": [
            {
                "nodeId": n.get("nodeId"),
                "type": n.get("type"),
                "name": n.get("name"),
                "description": _node_description(n),
                "stepId": n.get("stepId"),
            }
            for n in nodes
        ],
        "edges": [
            {
                "fromNode": e.get("fromNode") or e.get("from"),
                "to": e.get("to") or e.get("toNode"),
                "kind": e.get("kind") or "sequential",
            }
            for e in edges
        ],
    }


async def generate_nl_plan(
    session_id: str,
    project_id: str,
    stage0_session: Any,
    *,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    selected_name: Optional[str] = None,
    selected_type: Optional[str] = None,
    selected_category: Optional[str] = None,
    api_key: Optional[str] = None,
    requirement_override: Optional[str] = None,
) -> NLPlanResponse:
    """
    POST /nl/plan: enterprise steps (from Stage 0 session or re-expansion) → Stage 1 skeleton.
    """
    from agent_new import get_subscriber_available_credits
    from nl_builder.enterprise_expander import (
        attach_stage0_clarifications,
        expand_prompt_to_enterprise_steps,
        logical_steps_to_scratch_dicts,
    )

    original_prompt = (
        getattr(stage0_session, "original_user_prompt", None)
        or getattr(stage0_session, "raw_prompt", None)
        or "Create a standard workflow"
    )
    raw_prompt = requirement_override or original_prompt
    stage0_context = _extract_stage0_context(stage0_session)
    planning_warnings: List[str] = []
    planning_warnings.extend(list(getattr(stage0_session, "expander_warnings", None) or []))

    scratch_steps: List[Dict[str, Any]] = []
    used_legacy_blob = False
    from_override_expand = False
    if requirement_override:
        expanded = await expand_prompt_to_enterprise_steps(
            requirement_override,
            api_key=api_key,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=project_id,
        )
        planning_warnings.extend(expanded.warnings or [])
        if expanded.steps:
            scratch_steps = logical_steps_to_scratch_dicts(expanded.steps)
            from_override_expand = True
    if not scratch_steps:
        provided = list(getattr(stage0_session, "provided_steps", None) or [])
        if provided:
            scratch_steps = logical_steps_to_scratch_dicts(provided)
    if not scratch_steps:
        # Fallback: one FREE blob (legacy prompt-mode)
        scratch_steps = [
            {
                "stepId": s.stepId,
                "name": s.name,
                "description": s.description,
                "preferredKind": s.preferredKind,
            }
            for s in build_prompt_mode_steps(raw_prompt, stage0_context)
        ]
        used_legacy_blob = True
        planning_warnings.append(
            "No enterprise steps on session; Stage 1 received a single FREE prompt blob."
        )
    else:
        extra_ctx = stage0_context
        if requirement_override and not from_override_expand:
            extra_ctx = "\n\n".join(
                p for p in (stage0_context, requirement_override) if (p or "").strip()
            )
        scratch_steps = attach_stage0_clarifications(scratch_steps, extra_ctx)

    session_src = getattr(stage0_session, "analysis_source", None)
    if used_legacy_blob:
        analysis_source: Literal["llm", "fallback"] = "fallback"
    elif session_src in ("llm", "fallback") and not requirement_override:
        analysis_source = session_src
    else:
        analysis_source = "llm"

    session_mode = getattr(stage0_session, "mode", None)
    mode: Literal["prompt", "steps"] = session_mode if session_mode in ("prompt", "steps") else "prompt"

    phase_id = "phase_1"
    phase_name = selected_name or getattr(stage0_session, "workflow_name", None) or "Workflow Phase 1"
    phase_desc = original_prompt

    logger.info(
        "NL Plan Generator starting (enterprise steps=%d → scratch pipeline) "
        "session_id=%s project_id=%s",
        len(scratch_steps),
        session_id,
        project_id,
    )

    graph_dict = await plan_phase_graph_scratch(
        phase_id=phase_id,
        phase_name=phase_name,
        phase_description=phase_desc,
        steps=scratch_steps,
        stage0_context=stage0_context,
        api_key=api_key,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        run_gap_resolution=False,
        run_code_emission=False,
    )

    planning_warnings.extend(list(graph_dict.get("_planningWarnings") or []))
    graph_public = {k: v for k, v in graph_dict.items() if not str(k).startswith("_")}
    skeleton_dict = _skeleton_from_graph(graph_public)
    formatted_steps = _logical_steps_from_skeleton(skeleton_dict) or scratch_steps

    phase_result = {
        "graph": graph_public,
        "validation_warnings": planning_warnings,
        "binding_fixes_applied": graph_dict.get("_binding_fixes_applied", 0),
        "gap_generations": [],
        "langgraph_code": "",
        "langgraph_code_manual": "",
        "langgraph_code_llm": "",
        "phaseType": "FREEZE",
    }
    phase_graphs = [phase_result]

    plan_preview = await generate_plan_summary(
        phase_graphs=phase_graphs,
        original_prompt=raw_prompt,
        api_key=api_key,
    )

    cost_steps = _estimator_steps_from_phase_graphs(phase_graphs)
    if phase_graphs and _topology_counts_from_phase_graphs(phase_graphs)["nodeCount"] > 0:
        predicted_cost, cost_detail = _cost_estimate_from_phase_graphs(
            phase_graphs,
            base_text=raw_prompt,
        )
    elif cost_steps:
        # Fallback: full-pipeline heuristic from locked steps (should be rare)
        est = estimate_workflow_cost(steps=cost_steps)
        predicted_cost = est.predicted_cost
        cost_detail = None
    else:
        est = estimate_workflow_cost(prompt=raw_prompt)
        predicted_cost = est.predicted_cost
        cost_detail = None

    subscriber_avail = None
    sufficient = None
    if subscriber_id:
        try:
            subscriber_avail = get_subscriber_available_credits(subscriber_id)
            if subscriber_avail is not None:
                sufficient = subscriber_avail >= predicted_cost
        except Exception as exc:
            logger.warning("Failed to fetch subscriber available credits: %s", exc)

    workflow_id = getattr(stage0_session, "workflow_id", None) or f"agentic_{int(time.time()*1000)}"
    workflow_name = selected_name or getattr(stage0_session, "workflow_name", None) or "NL Workflow"

    return NLPlanResponse(
        sessionId=session_id,
        status="plan_ready",
        workflowId=workflow_id,
        workflowName=workflow_name,
        plan=plan_preview,
        phaseGraphs=phase_graphs,
        steps=formatted_steps,
        estimatedCost=predicted_cost,
        costEstimate=cost_detail,
        subscriberAvailableUsd=subscriber_avail,
        sufficientBalance=sufficient,
        mode=mode,
        analysis_source=analysis_source,
        warnings=planning_warnings,
        skeletonPlan=skeleton_dict,
    )
