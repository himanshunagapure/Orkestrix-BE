import json
import re
from collections import defaultdict, deque
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Awaitable
from pydantic import BaseModel, Field
from json_repair import repair_json

# --- Models ---
class UpdateAction(str, Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

class OperationItem(BaseModel):
    action: str
    targetStepId: Optional[str] = None
    afterStepId: Optional[str] = None
    beforeStepId: Optional[str] = None
    prompt: Optional[str] = None
    name: Optional[str] = None
    toolType: Optional[str] = None
    phaseName: Optional[str] = None
    phaseType: Optional[str] = None
    nodes: Optional[List[dict]] = None
    clarificationAnswers: Optional[List[str]] = None

class WorkflowUpdatePayload(BaseModel):
    # Same value as projectId (prefer projectId in new clients).
    workflowId: str
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None
    operations: List[OperationItem]
    version: Optional[int] = None
    preview_only: bool = False  # If true, run impact analysis only (Phase 1) and cache for reuse
    useSubscriberCredits: bool = False  # If true, allow using subscriber credits to cover budget shortfall

class GlobalVar(BaseModel):
    name: str
    dataType: str
    source: str

class NodeOutputEntry(BaseModel):
    nodeId: str
    batchNumber: int
    outputs: list[Dict[str, Any]]

class AvailableVarsRegister(BaseModel):
    globals: list[GlobalVar] = Field(default_factory=list)
    nodeOutputs: list[NodeOutputEntry] = Field(default_factory=list)

class NodeBatch(BaseModel):
    batchNumber: int
    nodeIds: list[str]

SYSTEM_NODE_TYPES = {"router", "end", "entry"}

# --- Core Utilities ---
def resolve_internal_id(ui_id: str, phase_graphs: list) -> str:
    for phase in phase_graphs:
        for node in phase.get("graph", {}).get("nodes", []):
            if node.get("nodeId") == ui_id:
                return ui_id
            if node.get("stepId") == ui_id:
                return node.get("nodeId")
    return ui_id


def build_node_batch_plan(graph: dict, batch_size: int = 6) -> list[NodeBatch]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    real_node_ids = {n["nodeId"] for n in nodes if n.get("type") not in SYSTEM_NODE_TYPES}
    in_degree: Dict[str, int] = {nid: 0 for nid in real_node_ids}
    dependents: Dict[str, list] = defaultdict(list)
    for edge in edges:
        src, dst = edge.get("fromNode"), edge.get("to")
        if src in real_node_ids and dst in real_node_ids:
            in_degree[dst] += 1
            dependents[src].append(dst)
    queue = deque(nid for nid in real_node_ids if in_degree[nid] == 0)
    ordered: list[str] = []
    while queue:
        nid = queue.popleft()
        ordered.append(nid)
        for dep in dependents[nid]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)
    return [
        NodeBatch(batchNumber=i // batch_size + 1, nodeIds=ordered[i: i + batch_size])
        for i in range(0, len(ordered), batch_size)
    ]

def calculate_blast_radius_for_update(target_node_id: str, graph: dict, node_batch_plan: list[NodeBatch]) -> set[int]:
    node_to_batch = {nid: b.batchNumber for b in node_batch_plan for nid in b.nodeIds}
    if target_node_id not in node_to_batch:
        return set()
    flagged = {node_to_batch[target_node_id]}
    adjacency: Dict[str, list] = defaultdict(list)
    for edge in graph.get("edges", []):
        adjacency[edge.get("fromNode")].append(edge.get("to"))
    visited, queue = set(), deque([target_node_id])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency[current]:
            if neighbor not in visited:
                visited.add(neighbor)
                if neighbor in node_to_batch:
                    flagged.add(node_to_batch[neighbor])
                queue.append(neighbor)
    return flagged

def smart_diff_outputs(old_outputs: list[dict], new_outputs: list[dict]) -> str:
    old_names = {o.get("name") for o in old_outputs}
    new_names = {o.get("name") for o in new_outputs}
    return "destructive" if (old_names - new_names) else "safe"

def build_available_vars_register(graph: dict, node_batch_plan: list[NodeBatch], global_inputs: list[dict]) -> AvailableVarsRegister:
    nodes_dict = {n["nodeId"]: n for n in graph.get("nodes", [])}
    register = AvailableVarsRegister(
        globals=[GlobalVar(name=g["name"], dataType=g.get("dataType", "string"), source="global") for g in global_inputs]
    )
    for batch in sorted(node_batch_plan, key=lambda b: b.batchNumber):
        for nid in batch.nodeIds:
            node = nodes_dict.get(nid)
            if not node:
                continue
            outputs = node.get("config", {}).get("declaredOutputs", [])
            register.nodeOutputs.append(NodeOutputEntry(nodeId=nid, batchNumber=batch.batchNumber, outputs=outputs or []))
    return register

def apply_batch_results(regen_results: list[dict], graph: dict, available_vars: AvailableVarsRegister, batch_number: int) -> None:
    nodes_dict = {n["nodeId"]: n for n in graph.get("nodes", [])}
    for result in regen_results:
        nid = result.get("nodeId")
        node = nodes_dict.get(nid)
        if not node:
            continue
        config = node.setdefault("config", {})
        config["inputBindings"] = result.get("inputBindings", config.get("inputBindings", {}))
        config["declaredOutputs"] = result.get("declaredOutputs", config.get("declaredOutputs", []))
        config["stateReads"] = result.get("stateReads", config.get("stateReads", []))
        # Auto-generate stateWrites from declaredOutputs to ensure sync (LLM may miss new outputs)
        declared_outputs = config.get("declaredOutputs", [])
        node_id = node.get("nodeId", "")
        auto_state_writes = [f"{node_id}__{out.get('name')}" for out in declared_outputs if out.get("name")]
        llm_state_writes = result.get("stateWrites", config.get("stateWrites", []))
        # Merge: prefer LLM's explicit stateWrites, but add any missing from declaredOutputs
        merged_writes = list(llm_state_writes)
        for sw in auto_state_writes:
            if sw not in merged_writes:
                merged_writes.append(sw)
        config["stateWrites"] = merged_writes
        config.pop("_requires_regeneration", None)
        available_vars.nodeOutputs = [e for e in available_vars.nodeOutputs if e.nodeId != nid]
        if result.get("declaredOutputs"):
            available_vars.nodeOutputs.append(NodeOutputEntry(nodeId=nid, batchNumber=batch_number, outputs=result["declaredOutputs"]))

def validate_graph(graph: dict, available_vars: AvailableVarsRegister) -> list[str]:
    errors: list[str] = []
    known_node_ids: set[str] = {entry.nodeId for entry in available_vars.nodeOutputs}
    known_node_ids |= {f"global"}
    valid_sources: set[str] = {f"global.{g.name}" for g in available_vars.globals}
    for entry in available_vars.nodeOutputs:
        for output in entry.outputs:
            valid_sources.add(f"{entry.nodeId}.{output.get('name')}")
    binding_pattern = re.compile(r"\{\{(.+?)\}\}")
    for node in graph.get("nodes", []):
        if node.get("type") in SYSTEM_NODE_TYPES:
            continue
        config = node.get("config", {})
        for binding_key, binding_val in config.get("inputBindings", {}).items():
            match = binding_pattern.search(str(binding_val))
            if match:
                ref = match.group(1).strip()
                if ref in valid_sources:
                    continue
                # Handle pick: multi-source bindings (e.g., pick:nodeA.param,nodeB.param)
                if ref.startswith("pick:"):
                    pick_refs = [r.strip() for r in ref[5:].split(",") if r.strip()]
                    for pick_ref in pick_refs:
                        if pick_ref in valid_sources:
                            continue
                        pick_parts = pick_ref.split(".", 1)
                        if len(pick_parts) != 2:
                            errors.append(
                                f"Node '{node['nodeId']}': binding '{binding_key}' has malformed pick ref '{pick_ref}'"
                            )
                            continue
                        pick_node_id = pick_parts[0]
                        if pick_node_id not in known_node_ids:
                            errors.append(
                                f"Node '{node['nodeId']}': binding '{binding_key}' references unknown node '{pick_node_id}' in pick binding"
                            )
                    continue
                ref_parts = ref.split(".", 1)
                ref_node_id = ref_parts[0]
                if ref_node_id not in known_node_ids:
                    errors.append(
                        f"Node '{node['nodeId']}': binding '{binding_key}' references unknown var '{ref}'"
                    )
    edge_node_ids = {n["nodeId"] for n in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        if edge.get("fromNode") not in edge_node_ids:
            errors.append(f"Edge references missing fromNode '{edge.get('fromNode')}'")
        if edge.get("to") not in edge_node_ids:
            errors.append(f"Edge references missing toNode '{edge.get('to')}'")
    return errors

def get_router_branches_text(router_node: dict) -> str:
    router_config = router_node.get("config", {}).get("router", {})
    rules = router_config.get("routingRules", [])
    default_target = router_config.get("defaultTarget", "end_node")
    lines = [f"Target: {r.get('targetNodeId')} (Trigger: {r.get('condition')})" for r in rules]
    lines.append(f"Target: {default_target} (Trigger: default fallback)")
    return "\n".join(lines)


def _collect_transitive_dependents(graph: dict, seeded_nodes: set[str]) -> set[str]:
    adjacency: Dict[str, list] = defaultdict(list)
    for edge in graph.get("edges", []):
        adjacency[edge.get("fromNode")].append(edge.get("to"))

    affected: set[str] = set(seeded_nodes)
    stack = list(seeded_nodes)
    while stack:
        current = stack.pop()
        for neighbor in adjacency.get(current, []):
            if neighbor in affected:
                continue
            affected.add(neighbor)
            stack.append(neighbor)
    return affected


async def get_proposed_node_schemas(
    phase_graphs: list,
    updated_node_ids: set[str],
    llm_caller: Callable[[str, str, str], Awaitable[str]],
    proposed_changes: dict = None
) -> dict[str, dict]:
    """
    Dry-run LLM call to get proposed schemas for updated nodes.
    Returns dict of node_id -> {"inputBindings": {}, "declaredOutputs": [], ...}
    """
    # Collect all nodes across phases
    all_nodes = []
    nodes_dict = {}
    for phase_entry in phase_graphs:
        graph = phase_entry.get("graph", {})
        nodes = graph.get("nodes", [])
        for node in nodes:
            all_nodes.append(node)
            nodes_dict[node["nodeId"]] = node

    # Filter to only updated nodes
    to_regen = [nodes_dict[nid] for nid in updated_node_ids if nid in nodes_dict]
    if not to_regen:
        return {}

    # Build available vars (simplified - just use current node outputs)
    global_inputs = []
    for phase_entry in phase_graphs:
        graph = phase_entry.get("graph", {})
        for g in graph.get("requiredGlobalInputs", []):
            global_inputs.append({"name": g, "dataType": "string"})

    node_batch_plan = build_node_batch_plan(graph)
    available_vars = build_available_vars_register(graph, node_batch_plan, global_inputs)

    # Use first phase's context
    phase_entry = phase_graphs[0]
    graph = phase_entry.get("graph", {})
    phase_context = {"phaseId": graph.get("phaseId"), "phaseName": graph.get("phaseName"), "goal": graph.get("goal")}

    system_prompt = """You are a workflow node schema builder.
Rebuild schemas for flagged nodes only.
Rules:
- inputBindings reference only vars in availableVars. Format: "{{nodeId.outputName}}" or "{{global.varName}}".
- declaredOutputs must match what the node produces per its originalDescription and updateRequest.
- stateReads = ["nodeId__outputName", ...] for each binding consumed.
- stateWrites = ["thisNodeId__outputName", ...] for each declared output.
- If an "updateRequest" is provided, modify the inputs/outputs to accommodate the requested changes while preserving the node's core original intent.
- CRITICAL: You MUST preserve ALL existing declaredOutputs from currentDeclaredOutputs. The returned declaredOutputs array MUST include ALL current outputs PLUS any new ones requested. Do NOT remove or rename existing outputs unless explicitly requested.
- CRITICAL: For UI screen nodes, the declaredOutputs directly map to form fields. Adding a new field means ADDING to the array, not replacing it.
Respond ONLY with a JSON array — no markdown, no explanation.
Schema per node: {"nodeId": string, "inputBindings": object, "declaredOutputs": [{name, dataType, required, description}], "stateReads": [string], "stateWrites": [string]}"""

    locked = [nodes_dict[nid] for nid in nodes_dict if nid not in updated_node_ids]
    user_prompt = json.dumps({
        "phaseContext": phase_context,
        "availableVars": {
            "globals": [{"name": g.name, "dataType": g.dataType} for g in available_vars.globals],
            "nodeOutputs": [{"nodeId": e.nodeId, "outputs": e.outputs} for e in available_vars.nodeOutputs]
        },
        "lockedNodes": [{"nodeId": n["nodeId"], "name": n.get("name"), "type": n.get("type"), "declaredOutputs": n.get("config", {}).get("declaredOutputs", [])} for n in locked],
        "nodesToRegenerate": [
            {
                "nodeId": n["nodeId"],
                "name": n.get("name"),
                "type": n.get("type"),
                "originalDescription": n.get("config", {}).get("generationDescription", ""),
                "updateRequest": (proposed_changes or {}).get(n["nodeId"], {}).get("_update_request", n.get("config", {}).get("_update_request", "")),
                "currentInputBindings": n.get("config", {}).get("inputBindings", {}),
                "currentDeclaredOutputs": n.get("config", {}).get("declaredOutputs", [])
            }
            for n in to_regen
        ]
    }, indent=2)

    response_text = await llm_caller(system_prompt, user_prompt, "regeneration-schema")
    results = repair_json(response_text, return_objects=True)

    return {r["nodeId"]: r for r in results}, {"system_prompt": system_prompt, "user_prompt": user_prompt, "response": response_text}


# --- Async LLM Pipeline Stages ---
async def regenerate_batch(batch: NodeBatch, flagged_node_ids: set[str], all_nodes: list[dict], available_vars: AvailableVarsRegister, phase_context: dict, llm_caller: Callable[[str, str, str], Awaitable[str]]) -> list[dict]:
    nodes_dict = {n["nodeId"]: n for n in all_nodes}
    locked = [nodes_dict[nid] for nid in batch.nodeIds if nid not in flagged_node_ids and nid in nodes_dict]
    to_regen = [nodes_dict[nid] for nid in batch.nodeIds if nid in flagged_node_ids and nid in nodes_dict]
    if not to_regen:
        return []
    system_prompt = """You are a workflow node schema builder.
Rebuild schemas for flagged nodes only. Locked nodes are read-only context.
Rules:
- inputBindings reference only vars in availableVars. Format: "{{nodeId.outputName}}" or "{{global.varName}}".
- declaredOutputs must match what the node produces per its originalDescription and updateRequest.
- stateReads = ["nodeId__outputName", ...] for each binding consumed.
- stateWrites = ["thisNodeId__outputName", ...] for each declared output.
- If an "updateRequest" is provided, modify the inputs/outputs to accommodate the requested changes while preserving the node's core original intent.
- CRITICAL: You MUST preserve ALL existing declaredOutputs from currentDeclaredOutputs. The returned declaredOutputs array MUST include ALL current outputs PLUS any new ones requested. Do NOT remove or rename existing outputs unless explicitly requested.
- CRITICAL: For UI screen nodes, the declaredOutputs directly map to form fields. Adding a new field means ADDING to the array, not replacing it.
Respond ONLY with a JSON array — no markdown, no explanation.
Schema per node: {"nodeId": string, "inputBindings": object, "declaredOutputs": [{name, dataType, required, description}], "stateReads": [string], "stateWrites": [string]}"""
    user_prompt = json.dumps({
        "phaseContext": {"phaseId": phase_context.get("phaseId"), "phaseName": phase_context.get("phaseName"), "goal": phase_context.get("goal")},
        "availableVars": {
            "globals": [{"name": g.name, "dataType": g.dataType} for g in available_vars.globals],
            "nodeOutputs": [{"nodeId": e.nodeId, "outputs": e.outputs} for e in available_vars.nodeOutputs]
        },
        "lockedNodes": [{"nodeId": n["nodeId"], "name": n.get("name"), "type": n.get("type"), "declaredOutputs": n.get("config", {}).get("declaredOutputs", [])} for n in locked],
        "nodesToRegenerate": [
            {
                "nodeId": n["nodeId"], 
                "name": n.get("name"), 
                "type": n.get("type"), 
                "originalDescription": n.get("config", {}).get("generationDescription", ""),
                "updateRequest": n.get("config", {}).get("_update_request", ""),
                "currentInputBindings": n.get("config", {}).get("inputBindings", {}), 
                "currentDeclaredOutputs": n.get("config", {}).get("declaredOutputs", [])
            } 
            for n in to_regen
        ]
    }, indent=2)
    response_text = await llm_caller(system_prompt, user_prompt, "regeneration-schema")
    return repair_json(response_text, return_objects=True)

async def regenerate_affected_routers(graph: dict, phase_flagged: set[str], available_vars: AvailableVarsRegister, llm_caller: Callable[[str, str, str], Awaitable[str]]) -> None:
    nodes_dict = {n["nodeId"]: n for n in graph.get("nodes", [])}
    adjacency: Dict[str, list] = defaultdict(list)
    for edge in graph.get("edges", []):
        adjacency[edge.get("fromNode")].append(edge.get("to"))
    routers_to_update: set[str] = set()
    for nid in phase_flagged:
        for neighbor in adjacency.get(nid, []):
            neighbor_node = nodes_dict.get(neighbor)
            if neighbor_node and neighbor_node.get("type") == "router":
                routers_to_update.add(neighbor)
    if not routers_to_update:
        return
    system_prompt = """You are a workflow router condition updater.
Given a router's existing routing rules and the updated variable schemas from upstream nodes,
rewrite the condition expressions to use the correct updated variable names.
Respond ONLY with a JSON array of updated routing rules.
Schema per rule: {"condition": string, "targetNodeId": string}"""
    for router_id in routers_to_update:
        router_node = nodes_dict.get(router_id)
        if not router_node:
            continue
        router_config = router_node.get("config", {}).get("router", {})
        existing_rules = router_config.get("routingRules", [])
        upstream_outputs = [{"nodeId": e.nodeId, "outputs": e.outputs} for e in available_vars.nodeOutputs if e.nodeId in phase_flagged]
        user_prompt = json.dumps({"routerId": router_id, "existingRules": existing_rules, "updatedUpstreamOutputs": upstream_outputs}, indent=2)
        response_text = await llm_caller(system_prompt, user_prompt, "regeneration-router")
        updated_rules = repair_json(response_text, return_objects=True)
        router_node["config"]["router"]["routingRules"] = updated_rules

async def regenerate_gap_resolution(node: dict, available_vars: AvailableVarsRegister, llm_caller: Callable[[str, str, str], Awaitable[str]]) -> str:
    config = node.get("config", {})
    declared_outputs = config.get("declaredOutputs", [])
    output_names = [o.get("name", "") for o in declared_outputs]
    output_types = [o.get("dataType", "Any") for o in declared_outputs]
    returns_text = ", ".join([f"`{n}` ({t})" for n, t in zip(output_names, output_types)])

    system_prompt = f"""You are a workflow implementation description generator for a {node.get('type', 'tool')} node.
Rules:
- You will receive the node's originalDescription, a user's updateRequest, exactInputNames, and exactOutputSchema.
- Rewrite the original description into a single, cohesive paragraph that seamlessly incorporates the user's requested changes.
- DO NOT mention upstream nodes, data sources, or routing paths.
- State strictly: the exact input variables received (and their types), the core functionality, and the exact output variables produced (and their types).
- For tool nodes: Focus on the precise functionality to be executed using the provided inputs to generate the required outputs.
- For ui_screen nodes: Describe the exact UI elements (fields, labels, buttons) and the required submit behavior to produce the outputs.
- CRITICAL: The return statement MUST explicitly mention EVERY field in exactOutputSchema. Use this exact format: "Upon submission, the screen returns {returns_text}."
- No conversational filler, no code syntax. Natural language only.
Respond with ONLY the synthesized description string."""

    input_keys = list(config.get("inputBindings", {}).keys())

    user_prompt = json.dumps({
        "nodeName": node.get("name"),
        "nodeType": node.get("type"),
        "originalDescription": config.get("generationDescription", ""),
        "updateRequestedByUser": config.get("_update_request", ""),
        "exactInputNames": input_keys,
        "exactOutputSchema": config.get("declaredOutputs", [])
    }, indent=2)

    response_text = await llm_caller(system_prompt, user_prompt, "regeneration-gap")

    # NOTE: Do NOT pop _update_request here — it must persist in the node config
    # so that the preservation step in update_workflow_plan can copy it to
    # completed_phase_results for version history (user_update_prompt).
    # config.pop("_update_request", None)

    return response_text.strip()

# --- Pipeline Orchestrator ---
async def run_update_regeneration_pipeline(phase_graphs: list, updated_node_ids: set[str], llm_caller: Callable[[str, str, str], Awaitable[str]]) -> None:
    for phase_entry in phase_graphs:
        graph = phase_entry.get("graph", {})
        nodes = graph.get("nodes", [])
        phase_flagged = {n["nodeId"] for n in nodes if n["nodeId"] in updated_node_ids and n.get("config", {}).get("_requires_regeneration")}
        if not phase_flagged:
            continue
        phase_context = {"phaseId": graph.get("phaseId"), "phaseName": graph.get("phaseName"), "goal": graph.get("goal")}
        global_inputs = [{"name": g, "dataType": "string"} for g in graph.get("requiredGlobalInputs", [])]
        nodes_dict = {n["nodeId"]: n for n in nodes}
        node_batch_plan = build_node_batch_plan(graph)
        node_to_batch = {nid: b.batchNumber for b in node_batch_plan for nid in b.nodeIds}
        flagged_batches: set[int] = set()
        
        for nid in phase_flagged:
            flagged_batches |= calculate_blast_radius_for_update(nid, graph, node_batch_plan)
        target_node_batches = {node_to_batch[nid] for nid in phase_flagged if nid in node_to_batch}
        downstream_batches = flagged_batches - target_node_batches
        downstream_node_ids = _collect_transitive_dependents(graph, phase_flagged)
        available_vars = build_available_vars_register(graph, node_batch_plan, global_inputs)
        
        for batch in sorted(node_batch_plan, key=lambda b: b.batchNumber):
            if batch.batchNumber not in flagged_batches:
                continue
            if batch.batchNumber in downstream_batches:
                reads_flagged = any(read_key.split("__")[0] in phase_flagged for nid in batch.nodeIds for read_key in (nodes_dict.get(nid, {}).get("config", {}).get("stateReads", [])))
                if not reads_flagged:
                    continue
            # Only regenerate downstream nodes (nodes NOT in phase_flagged).
            # Directly modified nodes (phase_flagged) have already been updated via
            # the main update flow (surgical editing + angular update). Re-running
            # them here causes duplicate changes (e.g., "add one number" applied twice).
            # A batch may contain both target and downstream nodes; we only regenerate
            # the downstream ones in that batch.
            nodes_to_regen_in_batch = {nid for nid in batch.nodeIds if nid not in phase_flagged}
            old_outputs_map = {nid: list(nodes_dict[nid].get("config", {}).get("declaredOutputs", [])) for nid in nodes_to_regen_in_batch if nid in nodes_dict}
            regen_results = await regenerate_batch(batch, nodes_to_regen_in_batch, nodes, available_vars, phase_context, llm_caller)
            if batch.batchNumber in target_node_batches:
                for result in regen_results:
                    if smart_diff_outputs(old_outputs_map.get(result.get("nodeId"), []), result.get("declaredOutputs", [])) == "safe":
                        flagged_batches -= downstream_batches
                        downstream_batches.clear()
                        break
            apply_batch_results(regen_results, graph, available_vars, batch.batchNumber)
            
        await regenerate_affected_routers(graph, phase_flagged, available_vars, llm_caller)
        errors = validate_graph(graph, available_vars)
        if errors:
            raise ValueError(f"Graph validation failed for phase '{graph.get('phaseName')}': {errors}")

        for nid in phase_flagged:
            node = nodes_dict.get(nid)
            if not node:
                continue

            desc = await regenerate_gap_resolution(node, available_vars, llm_caller)
            cfg = node.setdefault("config", {})
            try:
                from agent_new import (
                    _frozen_intent_description,
                    _is_generic_io_summary_description,
                    _ensure_intent_description,
                )
                kept = _frozen_intent_description(cfg)
                _ensure_intent_description(cfg)
                if kept and not _is_generic_io_summary_description(kept):
                    desc = kept
                    if _is_generic_io_summary_description(cfg.get("generationDescription")):
                        cfg["generationDescription"] = kept
                else:
                    cfg["generationDescription"] = desc
            except Exception:
                cfg["generationDescription"] = desc

            node_name = node.get("name", "UnknownNode")
            pascal_name = "".join(word.capitalize() for word in re.split(r'[^a-zA-Z0-9]+', node_name) if word)

            if "capabilityRef" not in node or not isinstance(node["capabilityRef"], dict):
                node["capabilityRef"] = {}

            # FIX: Only set to GENERATE:* if it doesn't already have a real capability (ui:* or tool:*)
            # This preserves existing ui:* capabilities so _resolve_one_gap_node uses update_angular_screen_async
            existing_cap_id = node["capabilityRef"].get("capabilityId", "")
            if not existing_cap_id.startswith(("ui:", "tool:")):
                node["capabilityRef"]["capabilityId"] = f"GENERATE:{pascal_name}"

            if "capabilityRef" in node and node["capabilityRef"] == "None":
                node["capabilityRef"] = None
            for key in ["router", "generationDescription"]:
                if key in node["config"] and node["config"][key] == "None":
                    node["config"][key] = None

            cap_id = node.get("capabilityRef", {}).get("capabilityId", "") if isinstance(node.get("capabilityRef"), dict) else ""
            # Always update the generation spec with the latest inputs/outputs
            # This is needed for both GENERATE:* (new nodes) and ui:* (existing screens)
            existing_gen = node["config"].get("generation")
            if not isinstance(existing_gen, dict):
                existing_gen = {}
                node["config"]["generation"] = existing_gen
            existing_gen["description"] = cfg.get("generationDescription") or desc
            existing_gen["outputs"] = node["config"].get("declaredOutputs", [])
            existing_gen["kind"] = existing_gen.get("kind") or node.get("type", "tool")
            if not existing_gen.get("stepId"):
                existing_gen["stepId"] = node.get("nodeId", "")

            input_bindings = node["config"].get("inputBindings", {})
            if input_bindings:
                output_lookup: dict[str, dict] = {}
                for phase_entry in phase_graphs:
                    for n in phase_entry.get("graph", {}).get("nodes", []):
                        for declared in n.get("config", {}).get("declaredOutputs", []):
                            key = f"{n['nodeId']}.{declared['name']}"
                            output_lookup[key] = declared

                resolved_inputs = []
                for param_name, binding_expr in input_bindings.items():
                    data_type = "Any"
                    description = ""
                    required = False
                    if isinstance(binding_expr, str):
                        inner = binding_expr.strip().lstrip("{").rstrip("}")
                        if not inner.startswith("global."):
                            matched = output_lookup.get(inner)
                            if matched:
                                data_type = matched.get("dataType", "Any")
                                description = matched.get("description", "")
                                required = matched.get("required", False)
                    resolved_inputs.append({
                        "name": param_name,
                        "dataType": data_type,
                        "required": required,
                        "description": description,
                    })
                existing_gen["inputs"] = resolved_inputs
            else:
                existing_gen["inputs"] = []

        updated_cap_ids = [
            cap_id for nid in phase_flagged
            if isinstance(nodes_dict.get(nid, {}).get("capabilityRef"), dict)
            if (cap_id := nodes_dict[nid]["capabilityRef"].get("capabilityId")) and not cap_id.startswith("GENERATE:")
        ]
        existing = phase_entry.get("gap_generations", [])
        phase_entry["gap_generations"] = list(set(existing) | set(filter(None, updated_cap_ids)))

        