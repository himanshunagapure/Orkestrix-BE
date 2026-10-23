"""
Surgical Code Editing Module for Workflow Regeneration

This module implements diff-based code editing instead of full regeneration.
It preserves existing functionality and only modifies what's necessary.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Awaitable, Set, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class EditType(str, Enum):
    """Type of code edit"""
    REPLACE = "replace"           # Replace a block of code
    INSERT = "insert"             # Insert new code at a location
    DELETE = "delete"             # Delete a block of code
    RENAME = "rename"             # Rename a symbol (function, variable, class)


@dataclass
class CodeEdit:
    """Represents a single code edit operation"""
    file_path: str
    edit_type: EditType
    target_symbol: Optional[str] = None      # Function/class name being modified
    search_block: str = ""                    # Code to find (for replace/delete)
    replace_block: str = ""                   # Code to replace with (for replace/insert)
    line_start: Optional[int] = None          # Line number for insert
    line_end: Optional[int] = None            # Line number for delete range
    description: str = ""                     # Human-readable description


@dataclass
class EditPlan:
    """Complete edit plan for a node update"""
    node_id: str
    node_name: str
    node_type: str
    edits: List[CodeEdit] = field(default_factory=list)
    affected_files: List[str] = field(default_factory=list)
    requires_backend_update: bool = False
    backend_edit_plan: Optional["EditPlan"] = None


class DiffGenerator:
    """
    Generates surgical diffs for code changes based on node updates.

    This implements the "Surgical Code Editing" approach from the team discussion:
    - Takes existing code as base
    - Generates minimal diffs for required changes
    - Preserves all unrelated functionality
    """

    def __init__(self):
        self._edit_generators: Dict[str, Callable] = {
            "ui_screen": self._generate_ui_screen_edits,
            "tool": self._generate_tool_edits,
            "agent:workflow": self._generate_agent_edits,
        }

    async def generate_edit_plan(
        self,
        node: Dict[str, Any],
        current_code: Dict[str, str],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        phase_graphs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> EditPlan:
        """
        Generate a surgical edit plan for a node update.

        Args:
            node: The node being updated
            current_code: Dict of file_path -> current code content
            proposed_changes: Proposed changes to the node (inputBindings, declaredOutputs, etc.)
            available_vars: Available variables in the workflow
            phase_graphs: All phase graphs for context
            llm_caller: Function to call LLM for complex edits

        Returns:
            EditPlan with specific edits to apply
        """
        node_id = node.get("nodeId", "")
        node_name = node.get("name", "Unknown")
        node_type = node.get("type", "tool")

        plan = EditPlan(
            node_id=node_id,
            node_name=node_name,
            node_type=node_type,
        )

        # Get the appropriate edit generator for this node type
        generator = self._edit_generators.get(node_type, self._generate_generic_edits)

        edits = await generator(
            node, current_code, proposed_changes, available_vars, phase_graphs, llm_caller
        )

        plan.edits = edits
        plan.affected_files = list(set(e.file_path for e in edits))

        return plan

    async def _generate_ui_screen_edits(
        self,
        node: Dict[str, Any],
        current_code: Dict[str, str],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        phase_graphs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> List[CodeEdit]:
        """Generate edits for UI screen nodes using Angular generator's analyze_update_scope"""
        edits = []

        # For UI screens, we delegate to the Angular generator's analysis
        # which already has sophisticated diff-based editing
        # Here we prepare the context for that analysis

        # Extract IR schema from node config
        config = node.get("config", {})
        generation = config.get("generation", {})
        node_name = node.get("name", "Unknown Screen")

        # If no generation spec in config, try to get it from capabilityRef
        if not generation:
            capability_ref = node.get("capabilityRef", {})
            capability_id = capability_ref.get("capabilityId", "") if isinstance(capability_ref, dict) else ""

            # Try to fetch generation spec from capability repository
            if capability_id and capability_id.startswith("ui:"):
                # The generation spec would be stored with the capability
                # For now, we'll try to build a minimal IR schema from the node's declared outputs
                # and config, which should be enough for the Angular generator to work with
                generation = self._build_generation_from_node(node)
                if generation:
                    logger.info(f"Built generation spec from node config for {node.get('nodeId')}")

        if not generation:
            # No generation spec, might need full regeneration
            return [CodeEdit(
                file_path="full_regeneration_required",
                edit_type=EditType.REPLACE,
                description="UI screen has no generation spec - full regeneration required"
            )]

        # The actual diff generation happens in angular_generator.py
        # via analyze_update_scope -> regenerate_files_for_update
        # This just marks that we need to use that pipeline
        edits.append(CodeEdit(
            file_path="angular_workspace",
            edit_type=EditType.REPLACE,
            target_symbol="ui_screen_update",
            description=f"UI screen '{node_name}' update - use Angular generator's selective regeneration",
            search_block="",
            replace_block=json.dumps({
                "node_id": node.get("nodeId"),
                "prompt": config.get("_update_request", ""),
                "updated_ir_schema": generation,
                "current_code": current_code
            })
        ))

        return edits

    def _build_generation_from_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a generation spec from node's config and declared outputs.
        This creates a minimal IR schema that the Angular generator can work with.
        """
        config = node.get("config", {})
        node_name = node.get("name", "Unknown Screen")
        node_id = node.get("nodeId", "")
        node_type = node.get("type", "ui_screen")

        # Get declared outputs from node config
        declared_outputs = config.get("declaredOutputs", [])
        input_bindings = config.get("inputBindings", {})

        # Get theme from config or use defaults
        theme = config.get("theme", {})

        # Build components from declared outputs
        components = []
        for output in declared_outputs:
            if isinstance(output, dict) and "name" in output:
                comp = {
                    "id": output["name"],
                    "type": self._map_output_type_to_component_type(output.get("dataType", "string")),
                    "props": {
                        "label": output.get("name", "").replace("_", " ").title(),
                        "type": output.get("dataType", "string"),
                        "required": output.get("required", True),
                        "description": output.get("description", "")
                    }
                }
                # Add binding reference
                if output["name"] in input_bindings:
                    comp["props"]["binding"] = input_bindings[output["name"]]
                components.append(comp)

        # Build screen config
        screen_config = {
            "type": "form",  # Default to form for input screens
            "title": node_name,
            "submitLabel": "Submit",
            "fields": [comp["props"] for comp in components]
        }

        # Build theme
        if not theme:
            theme = {
                "primary": "#3B82F6",
                "secondary": "#F3F4F6",
                "accent": "#10B981",
                "background": "#FFFFFF",
                "text": "#1F2937",
                "error": "#EF4444",
                "success": "#10B981",
                "font": "Inter",
                "radius": "8px"
            }

        return {
            "screen": screen_config,
            "theme": theme,
            "components": components
        }

    def _map_output_type_to_component_type(self, data_type: str) -> str:
        """Map data type to component type"""
        type_mapping = {
            "string": "text",
            "number": "number",
            "integer": "number",
            "boolean": "checkbox",
            "date": "date",
            "datetime": "datetime",
            "email": "email",
            "password": "password",
            "select": "select",
            "multiselect": "multiselect",
            "textarea": "textarea",
            "file": "file",
            "image": "image",
        }
        return type_mapping.get(data_type, "text")

    async def _generate_tool_edits(
        self,
        node: Dict[str, Any],
        current_code: Dict[str, str],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        phase_graphs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> List[CodeEdit]:
        """Generate edits for tool nodes (Python functions)"""
        edits = []
        config = node.get("config", {})
        generation = config.get("generation", {})
        node_id = node.get("nodeId", "")

        # For tools, we just mark them for regeneration through gap resolution
        # The actual code generation happens in the gap resolution phase
        # This avoids LLM calls during surgical editing which can timeout

        logger.info(f"[surgical_editing] Tool node {node_id} marked for regeneration via gap resolution")

        # Mark the node for regeneration by adding a marker
        edits.append(CodeEdit(
            file_path="tool_regeneration_marker",
            edit_type=EditType.REPLACE,
            target_symbol=f"tool_{node_id}",
            description=f"Tool '{node.get('name')}' marked for regeneration with updated schema",
            search_block="",
            replace_block=json.dumps({
                "node_id": node_id,
                "prompt": config.get("_update_request", ""),
                "updated_inputs": proposed_changes.get("inputBindings", {}),
                "updated_outputs": proposed_changes.get("declaredOutputs", []),
                "generation_description": proposed_changes.get("generationDescription", ""),
            })
        ))

        return edits

    async def _generate_agent_edits(
        self,
        node: Dict[str, Any],
        current_code: Dict[str, str],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        phase_graphs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> List[CodeEdit]:
        """Generate edits for sub-agent workflow nodes"""
        # Sub-agent workflows are separate workflows - changes might require
        # updating the referenced workflow
        edits = []

        config = node.get("config", {})
        capability_ref = node.get("capabilityRef", {})
        workflow_id = capability_ref.get("capabilityId", "").replace("agent:workflow:", "")

        if workflow_id:
            edits.append(CodeEdit(
                file_path=f"workflows/{workflow_id}.json",
                edit_type=EditType.REPLACE,
                target_symbol=workflow_id,
                description=f"Update referenced sub-workflow '{workflow_id}'",
                search_block="",
                replace_block=json.dumps({"workflow_id": workflow_id, "update": proposed_changes})
            ))

        return edits

    async def _generate_generic_edits(
        self,
        node: Dict[str, Any],
        current_code: Dict[str, str],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        phase_graphs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> List[CodeEdit]:
        """Generic edit generation for unknown node types"""
        return [CodeEdit(
            file_path="unknown_node_type",
            edit_type=EditType.REPLACE,
            description=f"Unknown node type '{node.get('type')}' - manual review required"
        )]

    async def _generate_tool_from_spec(
        self,
        node: Dict[str, Any],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> str:
        """Generate a new tool from specification"""
        config = node.get("config", {})
        generation = config.get("generation", {})

        prompt = f"""Generate a complete Python async function for a workflow tool node.

Node Name: {node.get('name')}
Node ID: {node.get('nodeId')}
Update Request: {config.get('_update_request', '')}

Inputs: {json.dumps(generation.get('inputs', []), indent=2)}
Outputs: {json.dumps(generation.get('outputs', []), indent=2)}
Description: {generation.get('description', '')}

Requirements:
1. Function signature: async def {generation.get('stepId', 'tool_function')}(*, params, db_wrapper=None, secure_http=None, request_meta=None)
2. All imports INSIDE the function body
3. Comprehensive error handling with logging
4. Return JSON-serializable dict matching output schema
5. Use only stdlib and pre-installed packages (httpx, pymongo via Database class)
6. No placeholders - production-ready code

Return ONLY the function code, no markdown, no explanations."""

        response = await llm_caller(
            "You are an expert Python backend developer generating production-ready async functions.",
            prompt,
            "tool-generation"
        )

        return response.strip()

    async def _generate_tool_diff(
        self,
        current_code: str,
        node: Dict[str, Any],
        proposed_changes: Dict[str, Any],
        available_vars: Dict[str, Any],
        llm_caller: Callable[[str, str, str], Awaitable[str]]
    ) -> Optional[Dict[str, str]]:
        """Generate a minimal diff for tool code changes"""
        config = node.get("config", {})

        prompt = f"""You are an expert Python developer making surgical edits to an existing tool function.

CURRENT CODE:
```python
{current_code}
```

NODE INFO:
- Node ID: {node.get('nodeId')}
- Node Name: {node.get('name')}
- Update Request: {config.get('_update_request', '')}

PROPOSED CHANGES:
- Input Bindings: {json.dumps(proposed_changes.get('inputBindings', {}), indent=2)}
- Declared Outputs: {json.dumps(proposed_changes.get('declaredOutputs', []), indent=2)}
- Generation Description: {proposed_changes.get('generationDescription', '')}

AVAILABLE UPSTREAM VARIABLES:
{json.dumps(available_vars, indent=2)}

TASK: Generate a MINIMAL diff to update the function for the proposed changes.
- Only change what's necessary for the update request
- Preserve ALL existing functionality, error handling, logging, imports
- Do NOT reformat, reorganize, or "improve" unrelated code
- Keep the same function signature
- Update parameter handling, return values, and logic only as needed

Return a JSON object with:
{{
  "search": "exact code block to find (10-50 lines)",
  "replace": "exact replacement code block",
  "description": "what this change does"
}}

If no changes needed, return {{"search": "", "replace": "", "description": "no changes needed"}}"""

        response = await llm_caller(
            "You are an expert Python developer generating minimal surgical diffs for code updates.",
            prompt,
            "tool-diff-generation"
        )

        try:
            result = json.loads(response.strip())
            if result.get("search") and result.get("replace"):
                return result
        except:
            pass

        return None


class DiffApplicator:
    """
    Applies generated diffs to actual code files.
    """

    @staticmethod
    def apply_edit(edit: CodeEdit, codebase: Dict[str, str]) -> Tuple[bool, str]:
        """
        Apply a single edit to the codebase.

        Returns:
            (success, message)
        """
        # Special case: UI screen updates via Angular generator
        # These don't modify the codebase directly - they trigger Angular generation later
        if edit.file_path == "angular_workspace" and edit.target_symbol == "ui_screen_update":
            return True, f"UI screen update queued for Angular regeneration: {edit.description}"

        # Special case: Tool regeneration marker - signals gap resolution to regenerate this tool
        # This doesn't modify the codebase directly - the actual code is generated in gap resolution phase
        if edit.file_path == "tool_regeneration_marker":
            return True, f"Tool regeneration queued for gap resolution: {edit.description}"

        if edit.file_path not in codebase:
            return False, f"File not found: {edit.file_path}"

        current_content = codebase[edit.file_path]

        if edit.edit_type == EditType.REPLACE:
            if edit.search_block not in current_content:
                return False, f"Search block not found in {edit.file_path}"
            new_content = current_content.replace(edit.search_block, edit.replace_block, 1)
            codebase[edit.file_path] = new_content
            return True, f"Applied replace to {edit.file_path}"

        elif edit.edit_type == EditType.INSERT:
            lines = current_content.split('\n')
            insert_line = edit.line_start or len(lines)
            lines.insert(insert_line, edit.replace_block)
            codebase[edit.file_path] = '\n'.join(lines)
            return True, f"Applied insert to {edit.file_path} at line {insert_line}"

        elif edit.edit_type == EditType.DELETE:
            lines = current_content.split('\n')
            start = edit.line_start or 0
            end = edit.line_end or len(lines)
            del lines[start:end]
            codebase[edit.file_path] = '\n'.join(lines)
            return True, f"Applied delete to {edit.file_path} lines {start}-{end}"

        return False, f"Unknown edit type: {edit.edit_type}"

    @staticmethod
    def apply_plan(plan: EditPlan, codebase: Dict[str, str]) -> Tuple[bool, List[str]]:
        """
        Apply all edits in a plan.

        Returns:
            (all_successful, messages)
        """
        messages = []
        all_success = True

        for edit in plan.edits:
            success, msg = DiffApplicator.apply_edit(edit, codebase)
            messages.append(msg)
            if not success:
                all_success = False

        return all_success, messages


async def generate_surgical_edit_plan(
    phase_graphs: List[Dict[str, Any]],
    updated_node_ids: Set[str],
    proposed_changes: Dict[str, Dict[str, Any]],
    current_codebase: Dict[str, str],
    available_vars: Dict[str, Any],
    llm_caller: Callable[[str, str, str], Awaitable[str]]
) -> List[EditPlan]:
    """
    Main entry point for generating surgical edit plans.

    Args:
        phase_graphs: All phase graphs
        updated_node_ids: Nodes being directly updated
        proposed_changes: Proposed changes per node
        current_codebase: Current code files (file_path -> content)
        available_vars: Available workflow variables
        llm_caller: LLM calling function

    Returns:
        List of EditPlan objects
    """
    logger.info(f"[surgical_editing] Starting generate_surgical_edit_plan for {len(updated_node_ids)} nodes")
    diff_generator = DiffGenerator()
    plans = []

    # Build node lookup
    nodes_by_id = {}
    for phase in phase_graphs:
        graph = phase.get("graph", {})
        for node in graph.get("nodes", []):
            nodes_by_id[node["nodeId"]] = node

    for node_id in updated_node_ids:
        node = nodes_by_id.get(node_id)
        if not node:
            continue

        changes = proposed_changes.get(node_id, {})

        logger.info(f"[surgical_editing] Generating edit plan for node: {node_id}")
        plan = await diff_generator.generate_edit_plan(
            node, current_codebase, changes, available_vars, phase_graphs, llm_caller
        )
        logger.info(f"[surgical_editing] Generated plan for {node_id} with {len(plan.edits)} edits")

        plans.append(plan)

    logger.info(f"[surgical_editing] Completed generating {len(plans)} edit plans")
    return plans


def format_edit_plan_for_user(plans: List[EditPlan]) -> str:
    """Format edit plans for user display"""
    lines = [
        "=" * 60,
        "SURGICAL EDIT PLAN",
        "=" * 60,
    ]

    for plan in plans:
        lines.append(f"\nNode: {plan.node_name} ({plan.node_id})")
        lines.append(f"Type: {plan.node_type}")
        lines.append(f"Files to modify: {', '.join(plan.affected_files) or 'None'}")
        lines.append(f"Requires backend update: {plan.requires_backend_update}")

        for edit in plan.edits:
            lines.append(f"  - {edit.file_path}: {edit.edit_type.value}")
            lines.append(f"    Target: {edit.target_symbol or 'N/A'}")
            lines.append(f"    Description: {edit.description}")
            if edit.search_block:
                preview = edit.search_block[:100].replace('\n', ' ')
                lines.append(f"    Search: {preview}...")
            if edit.replace_block:
                preview = edit.replace_block[:100].replace('\n', ' ')
                lines.append(f"    Replace: {preview}...")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)