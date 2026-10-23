"""
Impact Analysis Module for Workflow Regeneration

This module provides comprehensive impact analysis before workflow regeneration,
showing users exactly what will change and allowing them to approve/reject changes.
"""

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ImpactSeverity(str, Enum):
    """Severity of impact on downstream nodes"""
    NONE = "none"           # No impact
    SAFE = "safe"           # Only input bindings change, outputs compatible
    BREAKING = "breaking"   # Output schema changes (removed/renamed fields, type changes)
    STRUCTURAL = "structural"  # Node added/removed, graph topology changes


class ChangeType(str, Enum):
    """Type of change detected"""
    INPUT_BINDING_ADDED = "input_binding_added"
    INPUT_BINDING_REMOVED = "input_binding_removed"
    INPUT_BINDING_CHANGED = "input_binding_changed"
    OUTPUT_ADDED = "output_added"
    OUTPUT_REMOVED = "output_removed"
    OUTPUT_RENAMED = "output_renamed"
    OUTPUT_TYPE_CHANGED = "output_type_changed"
    NODE_ADDED = "node_added"
    NODE_REMOVED = "node_removed"
    EDGE_ADDED = "edge_added"
    EDGE_REMOVED = "edge_removed"
    ROUTER_RULE_CHANGED = "router_rule_changed"
    GENERATION_DESCRIPTION_CHANGED = "generation_description_changed"
    # Backend code needs update (e.g., new component requires new data model/function)
    BACKEND_CODE_CHANGED = "backend_code_changed"
    # New: upstream node added a new declaredOutput that the downstream tool
    # may want to consume. Not breaking — just an opportunity to extend the
    # downstream tool's input parameters / function_code. Downstream tool
    # node is the canonical addr.
    TOOL_INPUT_AVAILABLE_FROM_UPSTREAM = "tool_input_available_from_upstream"


@dataclass
class NodeImpact:
    """Impact analysis for a single node"""
    node_id: str
    node_name: str
    node_type: str
    is_directly_modified: bool = False
    severity: ImpactSeverity = ImpactSeverity.NONE
    changes: List[Dict[str, Any]] = field(default_factory=list)
    affected_downstream: List[str] = field(default_factory=list)
    requires_regeneration: bool = False
    regeneration_reason: str = ""


@dataclass
class ImpactReport:
    """Complete impact analysis report for a workflow update"""
    workflow_id: str
    directly_modified_nodes: List[str]
    all_affected_nodes: List[NodeImpact]
    summary: Dict[str, int] = field(default_factory=dict)
    breaking_changes: List[Dict[str, Any]] = field(default_factory=list)
    safe_changes: List[Dict[str, Any]] = field(default_factory=list)
    user_approval_required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "workflow_id": self.workflow_id,
            "directly_modified_nodes": self.directly_modified_nodes,
            "all_affected_nodes": [
                {
                    "node_id": n.node_id,
                    "node_name": n.node_name,
                    "node_type": n.node_type,
                    "is_directly_modified": n.is_directly_modified,
                    "severity": n.severity.value,
                    "changes": n.changes,
                    "affected_downstream": n.affected_downstream,
                    "requires_regeneration": n.requires_regeneration,
                    "regeneration_reason": n.regeneration_reason,
                }
                for n in self.all_affected_nodes
            ],
            "summary": self.summary,
            "breaking_changes": self.breaking_changes,
            "safe_changes": self.safe_changes,
            "user_approval_required": self.user_approval_required,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImpactReport":
        """Reconstruct ImpactReport from dictionary (e.g., from cached preview)"""
        node_impacts = []
        for n in data.get("all_affected_nodes", []):
            # Convert severity string back to enum
            severity_str = n.get("severity", "none")
            if isinstance(severity_str, str):
                try:
                    severity = ImpactSeverity(severity_str.lower())
                except ValueError:
                    severity = ImpactSeverity.NONE
            else:
                severity = severity_str

            node_impacts.append(NodeImpact(
                node_id=n.get("node_id", ""),
                node_name=n.get("node_name", ""),
                node_type=n.get("node_type", ""),
                is_directly_modified=n.get("is_directly_modified", False),
                severity=severity,
                changes=n.get("changes", []),
                affected_downstream=n.get("affected_downstream", []),
                requires_regeneration=n.get("requires_regeneration", False),
                regeneration_reason=n.get("regeneration_reason", ""),
            ))

        return cls(
            workflow_id=data.get("workflow_id", "unknown"),
            directly_modified_nodes=data.get("directly_modified_nodes", []),
            all_affected_nodes=node_impacts,
            summary=data.get("summary", {}),
            breaking_changes=data.get("breaking_changes", []),
            safe_changes=data.get("safe_changes", []),
            user_approval_required=data.get("user_approval_required", False),
        )


class ImpactAnalyzer:
    """
    Analyzes the impact of workflow node changes before regeneration.

    This implements the "Impact Analysis" phase from the team's discussion:
    - Identifies all downstream AND upstream (via cycles) dependencies
    - Determines what changes are breaking vs safe
    - Generates a report for user approval
    """

    SYSTEM_NODE_TYPES = {"router", "end", "entry", "fallback_router"}

    # Edge kinds that create cycles (retry loops, human loops, compensation rollbacks)
    CYCLE_EDGE_KINDS = {
        "retry",           # B fails → back to A
        "loop",            # Explicit loop edge
        "human_loop",      # User returns to previous screen
        "compensation",    # Saga rollback → previous step
    }

    # Edge kinds that create alternative forward paths (fallback handlers)
    # These don't create cycles but are alternative downstream consumers
    ALTERNATIVE_PATH_KINDS = {
        "fallback",        # Error → fallback node
    }

    # Edge kinds that create backward dependencies (for impact analysis)
    BACKWARD_DEPENDENCY_KINDS = {
        "retry",           # A receives B's output on retry
        "human_loop",      # A receives B's output on user return
        "compensation",    # A receives B's output on rollback
    }

    def __init__(self, phase_graphs: List[Dict[str, Any]]):
        self.phase_graphs = phase_graphs
        self.nodes_by_id: Dict[str, Dict[str, Any]] = {}
        self.edges_by_phase: Dict[str, List[Dict[str, Any]]] = {}

        # Forward adjacency (normal flow)
        self.forward_adjacency: Dict[str, List[str]] = defaultdict(list)
        self.forward_reverse: Dict[str, List[str]] = defaultdict(list)

        # Backward adjacency (retry/fallback/loop edges)
        self.backward_adjacency: Dict[str, List[str]] = defaultdict(list)
        self.backward_reverse: Dict[str, List[str]] = defaultdict(list)

        # All edges combined for cycle detection
        self.all_adjacency: Dict[str, List[str]] = defaultdict(list)

        self.cycles: Set[str] = set()  # Nodes participating in cycles
        self._build_indices()

    def _build_indices(self) -> None:
        """Build lookup indices for fast traversal"""
        for phase_entry in self.phase_graphs:
            graph = phase_entry.get("graph", {})
            phase_id = graph.get("phaseId")
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])

            for node in nodes:
                self.nodes_by_id[node["nodeId"]] = node

            if phase_id:
                self.edges_by_phase[phase_id] = edges

            for edge in edges:
                src = edge.get("fromNode")
                dst = edge.get("to")
                kind = edge.get("kind", "sequential")
                if src and dst:
                    if kind in self.CYCLE_EDGE_KINDS:
                        # Creates a cycle: add to backward adjacency for blast radius
                        # AND to all_adjacency for cycle detection
                        self.backward_adjacency[src].append(dst)
                        self.backward_reverse[dst].append(src)
                        self.all_adjacency[src].append(dst)
                    elif kind in self.ALTERNATIVE_PATH_KINDS:
                        # Alternative path (fallback): normal forward edge for blast radius
                        # but NOT a cycle
                        self.forward_adjacency[src].append(dst)
                        self.forward_reverse[dst].append(src)
                        self.all_adjacency[src].append(dst)
                    elif kind in self.BACKWARD_DEPENDENCY_KINDS:
                        # Backward dependency without cycle (compensation rollback)
                        self.backward_adjacency[src].append(dst)
                        self.backward_reverse[dst].append(src)
                        # Note: compensation may or may not create cycle depending on workflow
                        # We'll let cycle detection handle it
                        self.all_adjacency[src].append(dst)
                    else:
                        # Normal forward edge (sequential)
                        self.forward_adjacency[src].append(dst)
                        self.forward_reverse[dst].append(src)
                        self.all_adjacency[src].append(dst)

        # Detect cycles in the combined graph
        self.cycles = self._detect_cycles()

    async def analyze_impact_llm(
        self,
        updated_node_ids: Set[str],
        proposed_changes: Dict[str, Dict[str, Any]] = None,
        llm_caller: Callable[[str, str, str], Awaitable[str]] = None,
        current_codebase: Dict[str, str] = None,
        screen_metadata: Dict[str, Dict[str, Any]] = None,
        project_id: str = None,
        org_id: str = None,
        subscriber_id: str = None,
        user_id: str = None,
        workflow_id: str = None
    ) -> ImpactReport:
        """
        LLM-based impact analysis - more precise by analyzing actual code changes.

        Args:
            updated_node_ids: Set of node IDs being directly modified
            proposed_changes: Proposed changes per node
            llm_caller: Async function to call LLM (for Angular generator's analyze_update_scope)
            current_codebase: Current Angular code files (for UI screens)
            screen_metadata: Screen metadata including backend functions
            project_id, org_id, subscriber_id, user_id: Context for Angular generator
            workflow_id: Optional workflow ID to override the one from phase_graphs

        Returns:
            ImpactReport with LLM-analyzed precise changes
        """
        # First run static analysis as baseline
        static_report = self.analyze_impact(updated_node_ids, proposed_changes)

        if not llm_caller:
            return static_report

        # Enhance with LLM analysis for UI screens
        enhanced_impacts = []
        for impact in static_report.all_affected_nodes:
            should_run_llm = False
            user_prompt = ""
            node_proposed = {}

            if impact.node_type == "ui_screen" and impact.is_directly_modified:
                # Directly modified UI screen - use its update request
                should_run_llm = True
                node_proposed = proposed_changes.get(impact.node_id, {}) if proposed_changes else {}
                user_prompt = node_proposed.get("_update_request", "")

            elif impact.node_type == "ui_screen" and not impact.is_directly_modified:
                # Downstream UI screen - check if it has new upstream outputs available
                has_new_upstream_outputs = False
                new_output_details = []
                for change in impact.changes:
                    if change.get("type") == ChangeType.TOOL_INPUT_AVAILABLE_FROM_UPSTREAM.value:
                        has_new_upstream_outputs = True
                        upstream_id = change.get("upstream_id")
                        param_names = change.get("param_names", [])
                        new_output_details.append(f"Upstream '{upstream_id}' exposes new outputs: {', '.join(param_names)}")

                if has_new_upstream_outputs:
                    should_run_llm = True
                    # Build a prompt describing the new available inputs
                    user_prompt = (
                        f"Downstream screen receives new upstream outputs. "
                        f"{'; '.join(new_output_details)}. "
                        f"Analyze how these new inputs should be integrated into the screen "
                        f"(e.g., display new data, add form fields, update components)."
                    )
                    # For downstream nodes, proposed_changes may be empty, but we need to build
                    # an updated IR that includes the new available inputs
                    node_proposed = {
                        "_update_request": user_prompt,
                        "declaredOutputs": [],  # Will be built from current + new inputs
                    }

            if should_run_llm:
                # Get the screen's current code and IR schema
                node = self.nodes_by_id.get(impact.node_id, {})
                config = node.get("config", {})
                cap_ref = config.get("capabilityRef", {})
                screen_id = None

                # First try: get screen_id from capabilityRef
                if isinstance(cap_ref, dict) and cap_ref.get("capabilityId", "").startswith("ui:"):
                    screen_id = cap_ref["capabilityId"][3:]

                # Second try: reverse-lookup from screen_metadata (maps screen_id -> node_id)
                if not screen_id and screen_metadata:
                    for sid, meta in screen_metadata.items():
                        if meta.get("node_id") == impact.node_id:
                            screen_id = sid
                            break

                # Third try: use node_id as fallback (codebase keys are prefixed with screen_id)
                if not screen_id:
                    screen_id = impact.node_id

                if screen_id and current_codebase:
                    # Get updated IR schema from proposed_changes
                    # For downstream nodes, we need to include the new upstream outputs in the IR
                    updated_ir = self._build_ir_from_proposed(node, node_proposed)

                    # For downstream nodes with new upstream outputs, enhance the IR
                    if not impact.is_directly_modified:
                        updated_ir = self._enhance_ir_with_upstream_outputs(
                            updated_ir, impact, proposed_changes
                        )

                    # Filter current_codebase to only include files for this screen
                    # Codebase keys are prefixed with screen_id/ (e.g., "screen_20260818040753_e8f64426/src/app/...")
                    screen_codebase = {
                        path: content
                        for path, content in current_codebase.items()
                        if path.startswith(f"{screen_id}/")
                    }

                    # If no files found with screen_id prefix, try node_id as fallback
                    if not screen_codebase and screen_id != impact.node_id:
                        screen_codebase = {
                            path: content
                            for path, content in current_codebase.items()
                            if path.startswith(f"{impact.node_id}/")
                        }

                    # If still no files, pass all codebase (will be truncated by LLM anyway)
                    if not screen_codebase:
                        screen_codebase = current_codebase
                        logger.warning(f"[impact_analysis] No code files found for screen_id={screen_id} or node_id={impact.node_id}, passing full codebase")

                    # Get backend functions from screen_metadata
                    backend_functions = []
                    if screen_metadata and screen_id in screen_metadata:
                        backend_functions = screen_metadata[screen_id].get("backend_functions", [])

                    # Build the analysis prompt (same logic as AngularAppGenerator.analyze_update_scope)
                    # but use the passed llm_caller instead of creating a new generator
                    analysis = await self._analyze_update_scope_with_llm(
                        llm_caller=llm_caller,
                        user_prompt=user_prompt,
                        ir_schema=updated_ir,
                        current_code=screen_codebase,
                        screen_id=screen_id,
                        backend_functions=backend_functions,
                    )
                    if analysis:
                        logger.info(f"[impact_analysis] LLM analysis result for {impact.node_id}: {analysis}")
                        # Convert analysis to impact changes
                        llm_changes = self._convert_analysis_to_changes(analysis, impact)
                        if llm_changes:
                            impact.changes = llm_changes
                            impact.severity = self._determine_severity_from_changes(llm_changes)
                            logger.info(f"[impact_analysis] Updated impact for {impact.node_id}: severity={impact.severity.value}, changes={len(llm_changes)}")
                        else:
                            logger.warning(f"[impact_analysis] No LLM changes extracted for {impact.node_id}")
                    else:
                        logger.warning(f"[impact_analysis] LLM analysis returned empty for {impact.node_id}")

            enhanced_impacts.append(impact)

        # Rebuild report with enhanced impacts, using provided workflow_id if available
        report = self._build_report_from_impacts(enhanced_impacts, updated_node_ids, static_report)
        if workflow_id:
            report.workflow_id = workflow_id
        return report

    async def _analyze_update_scope_with_llm(
        self,
        llm_caller: Callable[[str, str, str], Awaitable[str]],
        user_prompt: str,
        ir_schema: Dict[str, Any],
        current_code: Dict[str, str],
        screen_id: str,
        backend_functions: List[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze update scope using the passed LLM caller (instead of AngularAppGenerator's internal LLM).
        Replicates the prompt building logic from AngularAppGenerator.analyze_update_scope.
        """
        import json

        # Build IR summary for context
        ir_summary = json.dumps(
            {
                "screen": ir_schema.get("screen", {}),
                "components": [
                    {"id": comp.get("id"), "type": comp.get("type")}
                    for comp in ir_schema.get("components", [])
                ],
                "theme": ir_schema.get("theme", {}),
                "antiPatterns": ir_schema.get("antiPatterns", []),
            },
            indent=2,
        )

        # Include the current Angular project codebase (truncated per file)
        file_blocks: List[str] = []
        max_files = 80
        max_chars_per_file = 10000

        for idx, (path, content) in enumerate(current_code.items()):
            if idx >= max_files:
                break
            truncated = content[:max_chars_per_file]
            file_blocks.append(f"<file path=\"{path}\">\n{truncated}\n</file>")

        current_code_block = "\n".join(file_blocks)

        # Build backend functions context
        backend_context = ""
        if backend_functions:
            backend_context = f"""
Associated Backend Functions:
{json.dumps(backend_functions, indent=2)}

If the update adds new fields/outputs that are submitted to backend functions,
the affected backend functions MUST be listed in "affected_backend_functions"
with their function names and what parameters need updating.
"""
        else:
            backend_context = """
Associated Backend Functions: None provided
"""

        analysis_prompt = f"""You are analyzing an existing Angular application update request to determine
which files need to be regenerated.

Current Angular Project (selected files):
<current_codebase>
{current_code_block}
</current_codebase>

{backend_context}

User Request:
\"\"\"{user_prompt}\"\"\"

Updated IR Schema summary (relevant parts):
{ir_summary}

Task: Analyze the user request and determine:
1. Which exact files are affected by this change
2. For each file, what type of change is needed (style, text, layout, component-add, component-remove, theme)
3. Which IR component IDs (if any) are affected
4. Whether this change requires backend updates (true if component-add/component-remove is detected)
5. Which backend functions (if any) need parameter updates

IMPORTANT: Adding or removing components (change_type: "component-add" or "component-remove") may require
backend model and function updates because:
- New components might need new data models (dataModels in IR schema)
- New components might need new data sources (dataSources in IR schema)
- New components might require new backend API functions
- Removing components might require removing associated backend functions

Respond with ONLY a JSON object in this exact format:
{{
  "affected_files": [
    {{"path": "src/app/app.component.ts", "reason": "Button color change affects root shell", "change_type": "style"}},
    {{"path": "src/styles.css", "reason": "Global theme variables need new colors", "change_type": "theme"}}
  ],
  "affected_components": [
    {{"id": "button_1", "reason": "Color change requested"}}
  ],
  "affected_backend_functions": [
    {{"function_name": "submit_numbers", "reason": "New field 'number_3' added to input parameters", "parameters_to_add": ["number_3"]}}
  ],
  "requires_backend_update": false
}}

Rules:
- Use the actual file paths from <current_codebase>.
- Change types must be one of: "style", "text", "layout", "component-add", "component-remove", "theme".
- Set "requires_backend_update" to true if ANY affected file has change_type "component-add" or "component-remove".
- If backend functions are provided and new fields are added that would be sent to them, include them in "affected_backend_functions" with function_name, reason, and parameters_to_add.
- Be conservative: only include files that are clearly affected by the request.
- If the request is purely theme-related, prefer global style files (e.g. src/styles.css) and any component-level styles that reference theme colors.
- If the user request explicitly asks to ADD A NEW COMPONENT (e.g., "add a new chart", "add a new card component", "add a new table", "create a new section"), classify as "component-add" and set requires_backend_update to true. These require adding new IR components and potentially backend updates.
- If the user request explicitly asks to REMOVE A COMPONENT (e.g., "remove the chart", "delete the card", "remove the table"), classify as "component-remove" and set requires_backend_update to true.
- For requests about MODIFYING EXISTING COMPONENTS (e.g., "add a button to the popup", "add one more number input", "add another field to the form", "change the button text", "update the label"), classify based on the specific change type (style, text, layout) - do NOT use "component-add"/"component-remove" for modifications to existing components.
- **NAVIGATION CHANGES**: If the request involves adding navigation (e.g., "add back button", "add next button", "navigate to screen", "go to screen"), the following files are typically affected:
  - `src/app/app.component.ts` - to add/update navigation state management (currentScreen variable, onNavigate handler)
  - `src/app/app.component.html` - to add/update conditional screen rendering (*ngIf)
  - The target screen component (e.g., `src/app/screen-two/screen-two.component.html`) - to add the navigation button
  - Classify new buttons as "component-add", navigation state changes as "layout"
- Be conservative: only include files that are clearly affected by the request. Prefer fewer files over more.
- If the request is purely theme-related, prefer global style files (e.g. src/styles.css) and any component-level styles that reference theme colors.
"""

        try:
            response_text = await llm_caller(analysis_prompt, "", "analyze-angular-update")

            # Extract JSON from response
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                logger.warning(f"[impact_analysis] No JSON found in LLM response for {screen_id}")
                return None
        except Exception as e:
            logger.error(f"[impact_analysis] _analyze_update_scope_with_llm failed for {screen_id}: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[impact_analysis] Traceback: {traceback.format_exc()}")
            return None

    def _build_ir_from_proposed(self, node: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Any]:
        """Build IR schema from node config and proposed changes"""
        config = node.get("config", {})
        generation = config.get("generation", {})

        # Merge proposed declaredOutputs into generation
        declared_outputs = proposed.get("declaredOutputs", [])
        input_bindings = proposed.get("inputBindings", config.get("inputBindings", {}))

        if not generation:
            generation = {}

        return {
            "screen": generation.get("screen", {"type": "form", "title": node.get("name"), "fields": []}),
            "theme": generation.get("theme", {}),
            "components": [
                {
                    "id": o.get("name"),
                    "type": self._map_output_type_to_component_type(o.get("dataType", "string")),
                    "props": {
                        "label": o.get("name", "").replace("_", " ").title(),
                        "type": o.get("dataType", "string"),
                        "required": o.get("required", True),
                        "description": o.get("description", ""),
                        "binding": input_bindings.get(o.get("name"), "")
                    }
                }
                for o in declared_outputs if isinstance(o, dict) and o.get("name")
            ]
        }

    def _map_output_type_to_component_type(self, data_type: str) -> str:
        """Map data type to Angular component type"""
        type_map = {
            "string": "input",
            "number": "input",
            "boolean": "checkbox",
            "date": "date-picker",
            "enum": "select",
            "array": "table",
            "object": "form-group",
        }
        return type_map.get(data_type, "input")

    def _enhance_ir_with_upstream_outputs(
        self,
        ir_schema: Dict[str, Any],
        impact: NodeImpact,
        proposed_changes: Dict[str, Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Enhance IR schema for downstream ui_screen nodes by adding new upstream outputs
        as available input components.
        """
        if not proposed_changes:
            return ir_schema

        # Find TOOL_INPUT_AVAILABLE_FROM_UPSTREAM changes
        new_upstream_outputs = []
        for change in impact.changes:
            if change.get("type") == ChangeType.TOOL_INPUT_AVAILABLE_FROM_UPSTREAM.value:
                upstream_id = change.get("upstream_id")
                param_names = change.get("param_names", [])
                for param_name in param_names:
                    new_upstream_outputs.append({
                        "upstream_id": upstream_id,
                        "param_name": param_name,
                        "binding": f"{{{{{upstream_id}.{param_name}}}}}"
                    })

        if not new_upstream_outputs:
            return ir_schema

        # Add new components for the new upstream outputs
        existing_components = ir_schema.get("components", [])
        existing_ids = {c.get("id") for c in existing_components}

        for output in new_upstream_outputs:
            param_name = output["param_name"]
            if param_name not in existing_ids:
                # Determine component type based on param name heuristic
                component_type = "input"
                if "amount" in param_name.lower() or "total" in param_name.lower() or "sum" in param_name.lower():
                    component_type = "input"
                elif "list" in param_name.lower() or "items" in param_name.lower() or "data" in param_name.lower():
                    component_type = "table"
                elif "date" in param_name.lower():
                    component_type = "date-picker"
                elif "select" in param_name.lower() or "status" in param_name.lower() or "type" in param_name.lower():
                    component_type = "select"

                new_component = {
                    "id": param_name,
                    "type": component_type,
                    "props": {
                        "label": param_name.replace("_", " ").title(),
                        "type": "string",  # default
                        "required": False,
                        "description": f"New upstream output from {output['upstream_id']}",
                        "binding": output["binding"]
                    }
                }
                existing_components.append(new_component)
                logger.info(f"[impact_analysis] Added new upstream output component '{param_name}' to IR for downstream screen {impact.node_id}")

        ir_schema["components"] = existing_components
        return ir_schema

    def _convert_analysis_to_changes(self, analysis: Dict[str, Any], impact: NodeImpact) -> List[Dict[str, Any]]:
        """Convert Angular generator's analyze_update_scope output to impact changes"""
        changes = []

        # File-level changes
        for af in analysis.get("affected_files", []):
            changes.append({
                "type": "file_change",
                "path": af.get("path"),
                "reason": af.get("reason"),
                "change_type": af.get("change_type"),
                "description": f"File {af.get('path')} will be {af.get('change_type', 'modified')}: {af.get('reason')}"
            })

        # Component-level changes
        for ac in analysis.get("affected_components", []):
            changes.append({
                "type": "component_change",
                "component_id": ac.get("id"),
                "reason": ac.get("reason"),
                "description": f"Component {ac.get('id')} will be modified: {ac.get('reason')}"
            })

        # Backend update required
        requires_backend = analysis.get("requires_backend_update", False)

        # Also check for model/interface file changes (these indicate backend contract changes)
        model_file_patterns = (".model.ts", ".interface.ts", "api.contract", "api-model", "dto.ts", "schema.ts", "contract.ts")
        for af in analysis.get("affected_files", []):
            path = af.get("path", "").lower()
            if any(pattern in path for pattern in model_file_patterns):
                requires_backend = True
                logger.info(f"[impact_analysis] Model file detected in affected files: {af.get('path')} - marking requires_backend_update=true")
                break

        # Check for new components in affected_components (these are component-add)
        # The LLM returns affected_components with id and reason but not change_type
        # If the reason mentions "new" or "added", treat as component-add
        for ac in analysis.get("affected_components", []):
            reason = ac.get("reason", "").lower()
            if any(word in reason for word in ["new", "added", "adding"]):
                requires_backend = True
                # Also add a file_change entry with component-add type for severity detection
                changes.append({
                    "type": "file_change",
                    "path": f"component:{ac.get('id')}",
                    "reason": ac.get("reason"),
                    "change_type": "component-add",
                    "description": f"New component {ac.get('id')} added: {ac.get('reason')}"
                })
                logger.info(f"[impact_analysis] New component detected: {ac.get('id')} - marking as component-add")
                break

        # Handle affected backend functions from LLM analysis
        affected_backend = analysis.get("affected_backend_functions", [])
        if affected_backend:
            requires_backend = True
            for bf in affected_backend:
                changes.append({
                    "type": "backend_function_change",
                    "function_name": bf.get("function_name"),
                    "reason": bf.get("reason"),
                    "parameters_to_add": bf.get("parameters_to_add", []),
                    "description": f"Backend function {bf.get('function_name')} needs update: {bf.get('reason')}"
                })
            logger.info(f"[impact_analysis] Affected backend functions: {[bf.get('function_name') for bf in affected_backend]}")

        if requires_backend:
            changes.append({
                "type": ChangeType.BACKEND_CODE_CHANGED.value,
                "description": "Backend update required due to component add/remove or model file changes",
                "source": "llm_analysis"
            })

        return changes

    def _determine_severity_from_changes(self, changes: List[Dict[str, Any]]) -> ImpactSeverity:
        """Determine severity from LLM-analyzed changes"""
        for change in changes:
            change_type = change.get("change_type", "")
            if change_type in ("component-add", "component-remove", "component_add", "component_remove"):
                return ImpactSeverity.BREAKING
            if change.get("type") == ChangeType.BACKEND_CODE_CHANGED.value:
                return ImpactSeverity.SAFE
        return ImpactSeverity.SAFE

    def _build_report_from_impacts(
        self,
        impacts: List[NodeImpact],
        updated_node_ids: Set[str],
        static_report: ImpactReport
    ) -> ImpactReport:
        """Build ImpactReport from enhanced impacts"""
        directly_modified = [n for n in impacts if n.is_directly_modified]
        breaking = [n for n in impacts if n.severity == ImpactSeverity.BREAKING]
        safe = [n for n in impacts if n.severity == ImpactSeverity.SAFE]
        structural = [n for n in impacts if n.severity == ImpactSeverity.STRUCTURAL]

        return ImpactReport(
            workflow_id=static_report.workflow_id,
            directly_modified_nodes=list(updated_node_ids),
            all_affected_nodes=impacts,
            summary={
                "total_affected": len(impacts),
                "directly_modified": len(directly_modified),
                "breaking_changes": len(breaking),
                "safe_changes": len(safe),
                "structural_changes": len(structural),
                "downstream_only": len([n for n in impacts if not n.is_directly_modified]),
            },
            breaking_changes=[n.changes for n in breaking],
            safe_changes=[n.changes for n in safe],
            user_approval_required=len(breaking) > 0 or len(structural) > 0,
        )

    def analyze_impact(
        self,
        updated_node_ids: Set[str],
        proposed_changes: Dict[str, Dict[str, Any]] = None
    ) -> ImpactReport:
        """
        Perform complete impact analysis for the given node updates.

        Args:
            updated_node_ids: Set of node IDs that are being directly modified
            proposed_changes: Optional dict of node_id -> proposed changes (inputBindings, declaredOutputs, etc.)

        Returns:
            ImpactReport with detailed analysis
        """
        # Step 1: Find all transitively affected nodes (blast radius)
        all_affected = self._calculate_blast_radius(updated_node_ids)

        # Step 2: Analyze each affected node
        node_impacts = []
        for node_id in all_affected:
            impact = self._analyze_node_impact(
                node_id,
                updated_node_ids,
                proposed_changes  # Pass full proposed_changes for downstream analysis
            )
            node_impacts.append(impact)

        # Step 3: Build downstream relationships
        for impact in node_impacts:
            impact.affected_downstream = self._get_direct_downstream(impact.node_id)

        # Step 4: Classify changes and build summary
        directly_modified = [n for n in node_impacts if n.is_directly_modified]
        breaking = [n for n in node_impacts if n.severity == ImpactSeverity.BREAKING]
        safe = [n for n in node_impacts if n.severity == ImpactSeverity.SAFE]
        structural = [n for n in node_impacts if n.severity == ImpactSeverity.STRUCTURAL]

        # Always require user approval when there are any affected nodes
        has_changes = len(node_impacts) > 0

        report = ImpactReport(
            workflow_id=self.phase_graphs[0].get("graph", {}).get("workflowId", "unknown"),
            directly_modified_nodes=list(updated_node_ids),
            all_affected_nodes=node_impacts,
            summary={
                "total_affected": len(node_impacts),
                "directly_modified": len(directly_modified),
                "breaking_changes": len(breaking),
                "safe_changes": len(safe),
                "structural_changes": len(structural),
                "downstream_only": len([n for n in node_impacts if not n.is_directly_modified]),
            },
            breaking_changes=[n.changes for n in breaking],
            safe_changes=[n.changes for n in safe],
            user_approval_required=has_changes,
        )

        return report

    def _detect_cycles(self) -> Set[str]:
        """
        Detect all nodes participating in cycles using Tarjan's algorithm.
        Returns set of node_ids that are part of at least one cycle.
        """
        index = 0
        stack = []
        on_stack = set()
        indices = {}
        lowlinks = {}
        cycles = set()

        def strongconnect(node: str):
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for neighbor in self.all_adjacency.get(node, []):
                if neighbor not in indices:
                    strongconnect(neighbor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
                elif neighbor in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[neighbor])

            # If node is a root node, pop the stack and generate an SCC
            if lowlinks[node] == indices[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    scc.append(w)
                    if w == node:
                        break
                # If SCC has more than 1 node, or a self-loop, it's a cycle
                if len(scc) > 1:
                    cycles.update(scc)
                elif len(scc) == 1 and node in self.all_adjacency.get(node, []):
                    cycles.add(node)

        for node in self.nodes_by_id:
            if node not in indices:
                strongconnect(node)

        return cycles

    def _calculate_blast_radius(self, seed_nodes: Set[str]) -> Set[str]:
        """
        Calculate all nodes transitively affected by seed nodes.

        For nodes in cycles, traversal is BIDIRECTIONAL:
        - Forward edges (normal flow) → downstream
        - Backward edges (retry/fallback/loop) → upstream becomes downstream too
        """
        visited = set(seed_nodes)
        queue = deque(seed_nodes)

        while queue:
            current = queue.popleft()

            # Always follow forward edges (normal downstream)
            for neighbor in self.forward_adjacency.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

            # If current node is in a cycle, also follow backward edges
            # This means upstream nodes in the cycle become affected too
            if current in self.cycles:
                for neighbor in self.backward_adjacency.get(current, []):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

        return visited

    def _get_direct_downstream(self, node_id: str) -> List[str]:
        """Get immediate downstream nodes (forward only)"""
        return self.forward_adjacency.get(node_id, [])

    def _get_direct_upstream(self, node_id: str) -> List[str]:
        """Get immediate upstream nodes (forward reverse)"""
        return self.forward_reverse.get(node_id, [])

    def _get_cycle_related_nodes(self, node_id: str) -> List[str]:
        """Get nodes connected via backward edges (retry/fallback/loop)"""
        related = []
        # Nodes that can flow back to this one (backward adjacency)
        related.extend(self.backward_adjacency.get(node_id, []))
        # Nodes this one can flow back to (backward reverse)
        related.extend(self.backward_reverse.get(node_id, []))
        return related

    def _analyze_node_impact(
        self,
        node_id: str,
        directly_modified: Set[str],
        full_proposed_changes: Optional[Dict[str, Dict[str, Any]]]
    ) -> NodeImpact:
        """Analyze impact on a single node"""
        node = self.nodes_by_id.get(node_id, {})
        config = node.get("config", {})

        is_direct = node_id in directly_modified
        current_outputs = config.get("declaredOutputs", [])
        current_inputs = config.get("inputBindings", {})

        impact = NodeImpact(
            node_id=node_id,
            node_name=node.get("name", "Unknown"),
            node_type=node.get("type", "unknown"),
            is_directly_modified=is_direct,
        )

        if is_direct and full_proposed_changes:
            # Analyze proposed changes to this node
            node_proposed = full_proposed_changes.get(node_id, {})
            impact = self._analyze_direct_changes(impact, config, node_proposed)
        elif not is_direct:
            # Analyze impact from upstream changes - pass FULL proposed_changes
            impact = self._analyze_downstream_impact(impact, config, directly_modified, full_proposed_changes)

        # Determine if regeneration is needed
        impact.requires_regeneration = impact.severity in (
            ImpactSeverity.SAFE,
            ImpactSeverity.BREAKING,
            ImpactSeverity.STRUCTURAL
        )

        if impact.requires_regeneration:
            impact.regeneration_reason = self._get_regeneration_reason(impact)

        return impact

    def _analyze_direct_changes(
        self,
        impact: NodeImpact,
        current_config: Dict[str, Any],
        proposed_changes: Dict[str, Any]
    ) -> NodeImpact:
        """Analyze changes directly proposed for a node"""
        current_outputs = {o.get("name"): o for o in current_config.get("declaredOutputs", [])}
        current_inputs = current_config.get("inputBindings", {})

        proposed_outputs_list = proposed_changes.get("declaredOutputs", [])
        proposed_outputs = {o.get("name"): o for o in proposed_outputs_list}
        proposed_inputs = proposed_changes.get("inputBindings", {})

        # Check if this is a UI screen with an update request
        # UI screens can have code changes (UI elements, buttons, etc.) without schema changes
        is_ui_screen = impact.node_type == "ui_screen"
        has_update_request = bool(proposed_changes.get("_update_request"))

        # SPECIAL CASE: If there's an update request but no proposed outputs,
        # it means "LLM will regenerate outputs" - don't assume all removed
        outputs_unknown = has_update_request and len(proposed_outputs_list) == 0

        changes = []

        if not outputs_unknown:
            # Normal case: compare proposed vs current outputs
            # Check output changes
            for name, output in proposed_outputs.items():
                if name not in current_outputs:
                    changes.append({
                        "type": ChangeType.OUTPUT_ADDED.value,
                        "output_name": name,
                        "output_type": output.get("dataType"),
                        "description": f"New output '{name}' added"
                    })
                    impact.severity = ImpactSeverity.SAFE
                else:
                    # Check for type changes
                    if output.get("dataType") != current_outputs[name].get("dataType"):
                        changes.append({
                            "type": ChangeType.OUTPUT_TYPE_CHANGED.value,
                            "output_name": name,
                            "old_type": current_outputs[name].get("dataType"),
                            "new_type": output.get("dataType"),
                            "description": f"Output '{name}' type changed from {current_outputs[name].get('dataType')} to {output.get('dataType')}"
                        })
                        impact.severity = ImpactSeverity.BREAKING

            # Check for removed outputs
            for name in current_outputs:
                if name not in proposed_outputs:
                    changes.append({
                        "type": ChangeType.OUTPUT_REMOVED.value,
                        "output_name": name,
                        "old_type": current_outputs[name].get("dataType"),
                        "description": f"Output '{name}' removed - THIS WILL BREAK DOWNSTREAM NODES"
                    })
                    impact.severity = ImpactSeverity.BREAKING

            # For UI screens, if there's an update request, always flag as changed
            # (UI changes like adding buttons don't change declaredOutputs schema)
            if is_ui_screen and has_update_request:
                changes.append({
                    "type": ChangeType.GENERATION_DESCRIPTION_CHANGED.value,
                    "description": "UI screen content will be regenerated based on prompt (e.g., new buttons, layout changes)"
                })
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE
        else:
            # Outputs unknown - LLM will regenerate
            # Don't flag specific outputs as removed, but note regeneration needed
            changes.append({
                "type": ChangeType.GENERATION_DESCRIPTION_CHANGED.value,
                "description": "Outputs will be regenerated based on prompt - downstream bindings may need update"
            })
            impact.severity = ImpactSeverity.SAFE

        # Check input binding changes (always applicable)
        for name, binding in proposed_inputs.items():
            if name not in current_inputs:
                changes.append({
                    "type": ChangeType.INPUT_BINDING_ADDED.value,
                    "input_name": name,
                    "binding": binding,
                    "description": f"New input binding '{name}' added"
                })
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE
            elif current_inputs[name] != binding:
                changes.append({
                    "type": ChangeType.INPUT_BINDING_CHANGED.value,
                    "input_name": name,
                    "old_binding": current_inputs[name],
                    "new_binding": binding,
                    "description": f"Input binding '{name}' changed"
                })
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE

        for name in current_inputs:
            if name not in proposed_inputs:
                changes.append({
                    "type": ChangeType.INPUT_BINDING_REMOVED.value,
                    "input_name": name,
                    "old_binding": current_inputs[name],
                    "description": f"Input binding '{name}' removed"
                })
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE

        # Check generation description changes
        if proposed_changes.get("generationDescription") != current_config.get("generationDescription"):
            if not outputs_unknown:  # Already added above
                changes.append({
                    "type": ChangeType.GENERATION_DESCRIPTION_CHANGED.value,
                    "description": "Generation description updated - code will be regenerated"
                })
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE

        impact.changes = changes
        return impact

    def _analyze_downstream_impact(
        self,
        impact: NodeImpact,
        current_config: Dict[str, Any],
        directly_modified: Set[str],
        proposed_changes: Dict[str, Dict[str, Any]] = None
    ) -> NodeImpact:
        """Analyze impact on downstream nodes from upstream changes"""
        # Check if any upstream dependencies have breaking changes
        upstream_nodes = self.forward_reverse.get(impact.node_id, [])
        modified_upstream = [u for u in upstream_nodes if u in directly_modified]

        # Also check cycle-related upstream nodes (via backward edges)
        # If this node is in a cycle, upstream nodes in the cycle become affected too
        cycle_related = self._get_cycle_related_nodes(impact.node_id)
        for c in cycle_related:
            if c in directly_modified and c not in modified_upstream:
                modified_upstream.append(c)

        if not modified_upstream:
            # Check transitive upstream (forward only)
            for u in upstream_nodes:
                if u in directly_modified or self._has_modified_ancestor(u, directly_modified):
                    modified_upstream.append(u)

            # Also check transitive cycle-related
            for c in cycle_related:
                if self._has_modified_ancestor(c, directly_modified) and c not in modified_upstream:
                    modified_upstream.append(c)

        if modified_upstream:
            current_inputs = current_config.get("inputBindings", {})
            changes = []

            # For downstream nodes, we mainly need to update input bindings
            # to reference the correct upstream outputs
            for upstream_id in modified_upstream:
                upstream_node = self.nodes_by_id.get(upstream_id, {})

                # Get proposed outputs for this upstream node (if available)
                proposed_outputs = []
                upstream_has_update_request = False
                if proposed_changes and upstream_id in proposed_changes:
                    proposed_outputs = proposed_changes[upstream_id].get("declaredOutputs", [])
                    # Check if upstream has an update request that might change outputs
                    upstream_has_update_request = bool(proposed_changes[upstream_id].get("_update_request"))
                    # If proposed_outputs is empty, it means "outputs unknown, will be regenerated"
                    outputs_unknown = upstream_has_update_request and len(proposed_outputs) == 0

                # Fall back to current outputs if no proposed changes
                current_outputs = upstream_node.get("config", {}).get("declaredOutputs", [])
                upstream_outputs = proposed_outputs if proposed_outputs else current_outputs

                # ── Additive detection ────────────────────────────────────────
                # If the upstream ADDED a new declaredOutput that the downstream
                # doesn't already consume via inputBindings, surface it as a
                # non-breaking opportunity. Severity bumps from NONE → SAFE so
                # the downstream tool/ui_screen node gets pulled into regeneration
                # rather than being silently skipped.
                downstream_node_type = self.nodes_by_id.get(impact.node_id, {}).get("type", "")
                if (
                    downstream_node_type in ("tool", "ui_screen")
                    and upstream_has_update_request
                    and upstream_outputs
                ):
                    current_names = {
                        (o.get("name") or "").strip()
                        for o in current_outputs
                        if isinstance(o, dict)
                    }
                    proposed_names = {
                        (o.get("name") or "").strip()
                        for o in upstream_outputs
                        if isinstance(o, dict)
                    }
                    new_outputs = sorted(proposed_names - current_names)
                    bound_via_binding = {
                        name
                        for name in current_names
                        if any(
                            isinstance(b, str) and f"{upstream_id}.{name}" in b
                            for b in current_inputs.values()
                        )
                    }
                    unbound_new_outputs = [
                        n for n in new_outputs
                        if n in proposed_names and n not in bound_via_binding
                    ]
                    if unbound_new_outputs:
                        changes.append({
                            "type": ChangeType.TOOL_INPUT_AVAILABLE_FROM_UPSTREAM.value,
                            "upstream_id": upstream_id,
                            "param_names": unbound_new_outputs,
                            "description": (
                                f"Upstream '{upstream_id}' exposes new output(s) "
                                f"{unbound_new_outputs!r} that downstream {downstream_node_type} "
                                f"'{impact.node_id}' could consume. Re-evaluating "
                                f"downstream node."
                            ),
                        })
                        if impact.severity == ImpactSeverity.NONE:
                            impact.severity = ImpactSeverity.SAFE

                for inp_name, binding_expr in current_inputs.items():
                    if isinstance(binding_expr, str) and upstream_id in binding_expr:
                        # This binding references the modified upstream node
                        # Check if the referenced output still exists
                        output_ref = self._extract_output_ref(binding_expr)
                        if output_ref:
                            output_name = output_ref.split(".")[-1] if "." in output_ref else output_ref
                            output_exists = any(o.get("name") == output_name for o in upstream_outputs)

                            if not output_exists:
                                changes.append({
                                    "type": ChangeType.INPUT_BINDING_CHANGED.value,
                                    "input_name": inp_name,
                                    "reason": f"Upstream output '{output_name}' from '{upstream_id}' was removed",
                                    "old_binding": binding_expr,
                                    "description": f"Input '{inp_name}' references removed upstream output - MUST UPDATE"
                                })
                                impact.severity = ImpactSeverity.BREAKING
                            elif upstream_has_update_request and (proposed_outputs == current_outputs or outputs_unknown):
                                # Upstream has update request - downstream binding may need update
                                severity_reason = "may need update if upstream output is renamed"
                                if outputs_unknown:
                                    severity_reason = "upstream outputs will be regenerated - verify binding still valid"
                                changes.append({
                                    "type": ChangeType.INPUT_BINDING_CHANGED.value,
                                    "input_name": inp_name,
                                    "reason": f"Upstream node '{upstream_id}' has pending changes that may affect output '{output_name}'",
                                    "old_binding": binding_expr,
                                    "description": f"Input '{inp_name}' {severity_reason}"
                                })
                                if impact.severity == ImpactSeverity.NONE:
                                    impact.severity = ImpactSeverity.SAFE

            if changes:
                impact.changes = changes
                if impact.severity == ImpactSeverity.NONE:
                    impact.severity = ImpactSeverity.SAFE

        return impact

    def _has_modified_ancestor(self, node_id: str, modified: Set[str]) -> bool:
        """Check if node has any modified ancestor (forward reverse traversal)"""
        visited = set()
        queue = deque([node_id])

        while queue:
            current = queue.popleft()
            if current in modified:
                return True
            if current in visited:
                continue
            visited.add(current)
            for parent in self.forward_reverse.get(current, []):
                queue.append(parent)

        return False

    def _extract_output_ref(self, binding_expr: str) -> Optional[str]:
        """Extract nodeId.outputName from {{nodeId.outputName}} binding"""
        import re
        match = re.search(r'\{\{(.+?)\}\}', binding_expr)
        return match.group(1).strip() if match else None

    def _get_regeneration_reason(self, impact: NodeImpact) -> str:
        """Generate human-readable reason for regeneration"""
        reasons = []
        for change in impact.changes:
            if change.get("type") == ChangeType.OUTPUT_REMOVED.value:
                reasons.append(f"Output '{change['output_name']}' removed")
            elif change.get("type") == ChangeType.OUTPUT_TYPE_CHANGED.value:
                reasons.append(f"Output '{change['output_name']}' type changed")
            elif change.get("type") == ChangeType.OUTPUT_ADDED.value:
                reasons.append(f"New output '{change['output_name']}' added")
            elif "INPUT_BINDING" in change.get("type", ""):
                reasons.append(f"Input binding '{change.get('input_name')}' modified")
            elif change.get("type") == ChangeType.GENERATION_DESCRIPTION_CHANGED.value:
                reasons.append("Generation description updated")

        return "; ".join(reasons) if reasons else "Downstream dependency changed"


async def run_impact_analysis(
    phase_graphs: List[Dict[str, Any]],
    updated_node_ids: Set[str],
    proposed_changes: Dict[str, Dict[str, Any]] = None,
    *,
    use_llm_analysis: bool = False,
    llm_caller: Optional[Callable[[str, str, str], Awaitable[str]]] = None,
    current_codebase: Optional[Dict[str, str]] = None,
    screen_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> ImpactReport:
    """
    Main entry point for impact analysis.

    Args:
        phase_graphs: List of phase graph objects
        updated_node_ids: Set of node IDs being directly modified
        proposed_changes: Optional dict of proposed changes per node
        use_llm_analysis: Whether to use LLM-based analysis for UI screens
        llm_caller: Async function to call LLM (for Angular generator's analyze_update_scope)
        current_codebase: Current Angular code files (for UI screens)
        screen_metadata: Screen metadata including backend functions
        project_id, org_id, subscriber_id, user_id: Context for Angular generator
        workflow_id: Optional workflow ID to override the one from phase_graphs

    Returns:
        ImpactReport with full analysis
    """
    analyzer = ImpactAnalyzer(phase_graphs)

    if use_llm_analysis and llm_caller:
        return await analyzer.analyze_impact_llm(
            updated_node_ids=updated_node_ids,
            proposed_changes=proposed_changes,
            llm_caller=llm_caller,
            current_codebase=current_codebase or {},
            screen_metadata=screen_metadata,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            workflow_id=workflow_id,
        )
    else:
        report = analyzer.analyze_impact(updated_node_ids, proposed_changes)
        # Override workflow_id if provided
        if workflow_id:
            report.workflow_id = workflow_id
        return report


def format_impact_report_for_user(report: ImpactReport) -> str:
    """Format impact report for user display"""
    lines = [
        "=" * 60,
        "WORKFLOW IMPACT ANALYSIS REPORT",
        "=" * 60,
        f"Workflow ID: {report.workflow_id}",
        f"Directly Modified Nodes: {', '.join(report.directly_modified_nodes) or 'None'}",
        "",
        "SUMMARY:",
        f"  Total Affected Nodes: {report.summary['total_affected']}",
        f"  Directly Modified: {report.summary['directly_modified']}",
        f"  Downstream Only: {report.summary['downstream_only']}",
        f"  Breaking Changes: {report.summary['breaking_changes']}",
        f"  Safe Changes: {report.summary['safe_changes']}",
        f"  Structural Changes: {report.summary['structural_changes']}",
        "",
    ]

    if report.breaking_changes:
        lines.append("⚠️  BREAKING CHANGES (Require Approval):")
        for change in report.breaking_changes:
            for c in change:
                lines.append(f"  - {c.get('description', str(c))}")
        lines.append("")

    if report.safe_changes:
        lines.append("✅ SAFE CHANGES (Auto-approved):")
        for change in report.safe_changes:
            for c in change:
                lines.append(f"  - {c.get('description', str(c))}")
        lines.append("")

    lines.append("DETAILED NODE IMPACTS:")
    for impact in report.all_affected_nodes:
        status = "🔴 DIRECT" if impact.is_directly_modified else "🟡 DOWNSTREAM"
        severity_icon = {
            ImpactSeverity.NONE: "⚪",
            ImpactSeverity.SAFE: "🟢",
            ImpactSeverity.BREAKING: "🔴",
            ImpactSeverity.STRUCTURAL: "🟠",
        }.get(impact.severity, "⚪")

        lines.append(f"  {status} {severity_icon} {impact.node_name} ({impact.node_id})")
        lines.append(f"    Type: {impact.node_type} | Severity: {impact.severity.value}")
        if impact.changes:
            for change in impact.changes:
                lines.append(f"    - {change.get('description', str(change))}")
        if impact.regeneration_reason:
            lines.append(f"    → Regeneration: {impact.regeneration_reason}")
        lines.append("")

    lines.append("=" * 60)
    if report.user_approval_required:
        lines.append("⚠️  USER APPROVAL REQUIRED - Breaking changes detected")
    else:
        lines.append("✅ No breaking changes - Safe to proceed automatically")
    lines.append("=" * 60)

    return "\n".join(lines)