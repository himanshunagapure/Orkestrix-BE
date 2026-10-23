"""
Validation & Repair Loop for Workflow Regeneration

This module implements the validation and repair loop that runs after code changes
to ensure workflow integrity, contract compliance, and downstream compatibility.
"""

import asyncio
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from regeneration import (
    validate_graph,
    build_available_vars_register,
    build_node_batch_plan,
    calculate_blast_radius_for_update,
    AvailableVarsRegister,
    NodeBatch,
    SYSTEM_NODE_TYPES,
)

# Import impact analysis for cycle detection
from impact_analysis import ImpactAnalyzer


class ValidationSeverity(str, Enum):
    """Severity of validation issues"""
    ERROR = "error"       # Must fix - workflow will fail
    WARNING = "warning"   # Should fix - potential issues
    INFO = "info"         # Informational only


class ValidationCategory(str, Enum):
    """Category of validation check"""
    GRAPH_INTEGRITY = "graph_integrity"           # Nodes/edges exist, no dangling refs
    CONTRACT_COMPLIANCE = "contract_compliance"   # Input/output contracts match
    TYPE_SAFETY = "type_safety"                   # Type compatibility between nodes
    SYNTAX_VALIDITY = "syntax_validity"           # Code compiles/runs
    BACKEND_COMPAT = "backend_compatibility"      # Frontend/backend schema match
    WORKFLOW_EXECUTION = "workflow_execution"     # End-to-end execution test


@dataclass
class ValidationIssue:
    """A single validation issue found"""
    category: ValidationCategory
    severity: ValidationSeverity
    message: str
    node_id: Optional[str] = None
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    suggested_fix: Optional[str] = None
    auto_fixable: bool = False


@dataclass
class ValidationReport:
    """Complete validation report"""
    issues: List[ValidationIssue] = field(default_factory=list)
    passed: bool = True

    def add_issue(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        if issue.severity == ValidationSeverity.ERROR:
            self.passed = False

    def get_errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == ValidationSeverity.ERROR]

    def get_warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == ValidationSeverity.WARNING]

    def get_by_category(self, category: ValidationCategory) -> List[ValidationIssue]:
        return [i for i in self.issues if i.category == category]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "total_issues": len(self.issues),
            "errors": len(self.get_errors()),
            "warnings": len(self.get_warnings()),
            "issues": [
                {
                    "category": i.category.value,
                    "severity": i.severity.value,
                    "message": i.message,
                    "node_id": i.node_id,
                    "file_path": i.file_path,
                    "line_number": i.line_number,
                    "suggested_fix": i.suggested_fix,
                    "auto_fixable": i.auto_fixable,
                }
                for i in self.issues
            ]
        }


class WorkflowValidator:
    """
    Comprehensive workflow validator that checks multiple aspects of the workflow
    after regeneration to ensure everything is consistent and executable.
    """

    def __init__(
        self,
        phase_graphs: List[Dict[str, Any]],
        codebase: Dict[str, str],
        global_inputs: List[Dict[str, Any]],
        llm_caller: Callable[[str, str, str], Awaitable[str]],
    ):
        self.phase_graphs = phase_graphs
        self.codebase = codebase
        self.global_inputs = global_inputs
        self.llm_caller = llm_caller
        self.nodes_by_id: Dict[str, Dict[str, Any]] = {}
        self._build_node_index()

    def _build_node_index(self) -> None:
        """Build index of all nodes by ID"""
        for phase in self.phase_graphs:
            graph = phase.get("graph", {})
            for node in graph.get("nodes", []):
                self.nodes_by_id[node["nodeId"]] = node

    async def validate_all(self) -> ValidationReport:
        """Run all validation checks"""
        report = ValidationReport()

        # Phase 1: Graph Integrity
        await self._validate_graph_integrity(report)

        # Phase 2: Contract Compliance
        await self._validate_contract_compliance(report)

        # Phase 3: Type Safety
        await self._validate_type_safety(report)

        # Phase 4: Syntax Validity (code compilation)
        await self._validate_syntax(report)

        # Phase 5: Backend Compatibility
        await self._validate_backend_compatibility(report)

        # Phase 6: Cycle Contract Compliance (bidirectional impact)
        await self._validate_cycle_contracts(report)

        # Phase 7: Workflow Execution (if requested)
        # await self._validate_workflow_execution(report)

        return report

    async def _validate_graph_integrity(self, report: ValidationReport) -> None:
        """Validate graph structure - nodes, edges, no dangling references"""
        for phase_entry in self.phase_graphs:
            graph = phase_entry.get("graph", {})
            phase_name = graph.get("phaseName", "unknown")

            # Use existing validation
            available_vars = build_available_vars_register(
                graph,
                build_node_batch_plan(graph),
                self.global_inputs
            )
            errors = validate_graph(graph, available_vars)

            for error in errors:
                report.add_issue(ValidationIssue(
                    category=ValidationCategory.GRAPH_INTEGRITY,
                    severity=ValidationSeverity.ERROR,
                    message=error,
                    suggested_fix="Check node IDs and edge references",
                    auto_fixable=False
                ))

            # Check for orphaned nodes (no incoming/outgoing edges except entry/exit)
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])
            node_ids = {n["nodeId"] for n in nodes}
            edge_from = {e.get("fromNode") for e in edges}
            edge_to = {e.get("to") for e in edges}

            for node in nodes:
                nid = node["nodeId"]
                ntype = node.get("type")

                if ntype in SYSTEM_NODE_TYPES:
                    continue

                if nid not in edge_from and nid not in edge_to:
                    # Check if it's the entry point (has no incoming, but is first in batch)
                    report.add_issue(ValidationIssue(
                        category=ValidationCategory.GRAPH_INTEGRITY,
                        severity=ValidationSeverity.WARNING,
                        message=f"Node '{nid}' ({node.get('name')}) is disconnected - no incoming or outgoing edges",
                        node_id=nid,
                        suggested_fix="Connect node to workflow or remove if unused",
                        auto_fixable=False
                    ))

    async def _validate_contract_compliance(self, report: ValidationReport) -> None:
        """Validate that input/output contracts are satisfied"""
        for phase_entry in self.phase_graphs:
            graph = phase_entry.get("graph", {})
            available_vars = build_available_vars_register(
                graph,
                build_node_batch_plan(graph),
                self.global_inputs
            )

            # Build valid sources
            valid_sources: Set[str] = set()
            for g in available_vars.globals:
                valid_sources.add(f"global.{g.name}")
            for entry in available_vars.nodeOutputs:
                for output in entry.outputs:
                    valid_sources.add(f"{entry.nodeId}.{output.get('name')}")

            # Check each node's input bindings
            for node in graph.get("nodes", []):
                if node.get("type") in SYSTEM_NODE_TYPES:
                    continue

                config = node.get("config", {})
                input_bindings = config.get("inputBindings", {})

                for param_name, binding_expr in input_bindings.items():
                    if not isinstance(binding_expr, str):
                        continue

                    # Extract reference from {{...}}
                    match = re.search(r'\{\{(.+?)\}\}', binding_expr)
                    if not match:
                        continue

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
                                report.add_issue(ValidationIssue(
                                    category=ValidationCategory.CONTRACT_COMPLIANCE,
                                    severity=ValidationSeverity.ERROR,
                                    message=f"Node '{node['nodeId']}': pick input '{param_name}' has malformed ref '{pick_ref}'",
                                    node_id=node["nodeId"],
                                    suggested_fix=f"Use format pick:nodeA.param,nodeB.param",
                                    auto_fixable=True
                                ))
                                continue
                            pick_node_id = pick_parts[0]
                            if pick_node_id not in self.nodes_by_id:
                                report.add_issue(ValidationIssue(
                                    category=ValidationCategory.CONTRACT_COMPLIANCE,
                                    severity=ValidationSeverity.ERROR,
                                    message=f"Node '{node['nodeId']}': pick input '{param_name}' references unknown node '{pick_node_id}'",
                                    node_id=node["nodeId"],
                                    suggested_fix=f"Update pick binding to reference a valid upstream node",
                                    auto_fixable=True
                                ))
                        continue

                    # Try to find what it might be referencing
                    ref_parts = ref.split(".", 1)
                    ref_node = ref_parts[0]

                    # Check if node exists
                    if ref_node not in self.nodes_by_id:
                        report.add_issue(ValidationIssue(
                            category=ValidationCategory.CONTRACT_COMPLIANCE,
                            severity=ValidationSeverity.ERROR,
                            message=f"Node '{node['nodeId']}': input '{param_name}' references unknown node '{ref_node}'",
                            node_id=node["nodeId"],
                            suggested_fix=f"Update binding to reference a valid upstream node",
                            auto_fixable=True
                        ))
                        continue

                    # Node exists - check if the output exists
                    if len(ref_parts) == 2:
                        ref_output = ref_parts[1]
                        upstream_node = self.nodes_by_id[ref_node]
                        upstream_outputs = upstream_node.get("config", {}).get("declaredOutputs", [])
                        output_exists = any(o.get("name") == ref_output for o in upstream_outputs)
                        if not output_exists:
                            report.add_issue(ValidationIssue(
                                category=ValidationCategory.CONTRACT_COMPLIANCE,
                                severity=ValidationSeverity.ERROR,
                                message=f"Node '{node['nodeId']}': input '{param_name}' references unknown output '{ref_output}' from '{ref_node}'",
                                node_id=node["nodeId"],
                                suggested_fix=f"Update binding to reference a valid output from '{ref_node}'",
                                auto_fixable=True
                            ))

    async def _validate_type_safety(self, report: ValidationReport) -> None:
        """Validate type compatibility between connected nodes"""
        for phase_entry in self.phase_graphs:
            graph = phase_entry.get("graph", {})
            available_vars = build_available_vars_register(
                graph,
                build_node_batch_plan(graph),
                self.global_inputs
            )

            # Build output type map
            output_types: Dict[str, Dict[str, str]] = {}
            for entry in available_vars.nodeOutputs:
                output_types[entry.nodeId] = {}
                for output in entry.outputs:
                    output_types[entry.nodeId][output.get("name")] = output.get("dataType", "Any")

            for g in available_vars.globals:
                if "global" not in output_types:
                    output_types["global"] = {}
                output_types["global"][g.name] = g.dataType

            # Check each node's inputs against upstream output types
            for node in graph.get("nodes", []):
                if node.get("type") in SYSTEM_NODE_TYPES:
                    continue

                config = node.get("config", {})
                input_bindings = config.get("inputBindings", {})

                # Get expected input types from node's capability
                expected_types = {}
                capability_ref = node.get("capabilityRef", {})
                if isinstance(capability_ref, dict):
                    cap_id = capability_ref.get("capabilityId", "")
                    # Could fetch from capability repository here

                for param_name, binding_expr in input_bindings.items():
                    if not isinstance(binding_expr, str):
                        continue

                    match = re.search(r'\{\{(.+?)\}\}', binding_expr)
                    if not match:
                        continue

                    ref = match.group(1).strip()
                    ref_parts = ref.split(".", 1)

                    if len(ref_parts) == 2:
                        src_node, src_output = ref_parts
                        if src_node in output_types and src_output in output_types[src_node]:
                            actual_type = output_types[src_node][src_output]
                            expected_type = expected_types.get(param_name)

                            if expected_type and not self._types_compatible(expected_type, actual_type):
                                report.add_issue(ValidationIssue(
                                    category=ValidationCategory.TYPE_SAFETY,
                                    severity=ValidationSeverity.WARNING,
                                    message=f"Node '{node['nodeId']}': input '{param_name}' expects {expected_type} but receives {actual_type} from {src_node}.{src_output}",
                                    node_id=node["nodeId"],
                                    suggested_fix="Add type conversion or update upstream output type",
                                    auto_fixable=False
                                ))

    def _types_compatible(self, expected: str, actual: str) -> bool:
        """Check if two types are compatible"""
        # Simple compatibility - can be extended
        if expected == actual:
            return True
        if expected == "Any" or actual == "Any":
            return True
        # Numeric compatibility
        numeric = {"number", "int", "float", "integer"}
        if expected in numeric and actual in numeric:
            return True
        return False

    async def _validate_syntax(self, report: ValidationReport) -> None:
        """Validate Python syntax of generated code files"""
        for file_path, content in self.codebase.items():
            if not file_path.endswith('.py'):
                continue

            try:
                compile(content, file_path, 'exec')
            except SyntaxError as e:
                report.add_issue(ValidationIssue(
                    category=ValidationCategory.SYNTAX_VALIDITY,
                    severity=ValidationSeverity.ERROR,
                    message=f"Syntax error in {file_path}: {e.msg}",
                    file_path=file_path,
                    line_number=e.lineno,
                    suggested_fix=f"Fix syntax error at line {e.lineno}",
                    auto_fixable=True
                ))
            except Exception as e:
                report.add_issue(ValidationIssue(
                    category=ValidationCategory.SYNTAX_VALIDITY,
                    severity=ValidationSeverity.ERROR,
                    message=f"Compilation error in {file_path}: {str(e)}",
                    file_path=file_path,
                    suggested_fix="Fix code compilation error",
                    auto_fixable=True
                ))

    async def _validate_backend_compatibility(self, report: ValidationReport) -> None:
        """Validate frontend/backend API compatibility"""
        # Check Angular components have matching backend endpoints
        for file_path, content in self.codebase.items():
            if file_path.endswith('.ts') or file_path.endswith('.html'):
                # Look for HTTP calls to backend
                http_calls = re.findall(r'(http|this\.http)\.(get|post|put|delete|patch)\(["\']([^"\']+)["\']', content)

                for call in http_calls:
                    url = call[2]
                    # Check if there's a matching backend endpoint
                    # This would need integration with backend route registry

        # Check for data model mismatches between frontend components and backend responses
        # This is a simplified check - full validation would require schema comparison

    async def _validate_workflow_execution(self, report: ValidationReport) -> None:
        """Run a dry-run execution of the workflow"""
        # This would execute the workflow in a test mode
        # For now, just validate that the graph is executable
        pass

    async def _validate_cycle_contracts(self, report: ValidationReport) -> None:
        """
        Validate that nodes in cycles (retry loops, fallbacks, etc.) have compatible contracts.

        When Node A → Node B → Node A (retry), Node A receives Node B's outputs on retry.
        If Node B's outputs changed, Node A's input bindings may break on retry.
        """
        for phase_entry in self.phase_graphs:
            graph = phase_entry.get("graph", {})
            phase_name = graph.get("phaseName", "unknown")

            # Build impact analyzer for this phase to detect cycles
            analyzer = ImpactAnalyzer([phase_entry])
            cycles = analyzer.cycles

            if not cycles:
                continue

            # Build output type map for this phase
            available_vars = build_available_vars_register(
                graph,
                build_node_batch_plan(graph),
                self.global_inputs
            )

            output_types: Dict[str, Dict[str, str]] = {}
            for entry in available_vars.nodeOutputs:
                output_types[entry.nodeId] = {}
                for output in entry.outputs:
                    output_types[entry.nodeId][output.get("name")] = output.get("dataType", "Any")

            for g in available_vars.globals:
                if "global" not in output_types:
                    output_types["global"] = {}
                output_types["global"][g.name] = g.dataType

            # For each node in a cycle, check its inputs against ALL nodes it can receive data from
            # (both forward upstream and backward/loop edges)
            for node in graph.get("nodes", []):
                if node.get("type") in SYSTEM_NODE_TYPES:
                    continue

                node_id = node["nodeId"]
                if node_id not in cycles:
                    # Only check nodes that are actually in cycles
                    continue

                config = node.get("config", {})
                input_bindings = config.get("inputBindings", {})

                # Get ALL nodes that can flow data to this node (including backward edges)
                # This includes: forward upstream + backward adjacency (retry/fallback sources)
                upstream_sources = set()

                # Forward upstream (normal flow)
                upstream_sources.update(analyzer.forward_reverse.get(node_id, []))

                # Backward sources (retry/fallback/loop edges)
                upstream_sources.update(analyzer.backward_adjacency.get(node_id, []))

                # Check each input binding against ALL possible upstream sources
                for param_name, binding_expr in input_bindings.items():
                    if not isinstance(binding_expr, str):
                        continue

                    match = re.search(r'\{\{(.+?)\}\}', binding_expr)
                    if not match:
                        continue

                    ref = match.group(1).strip()
                    ref_parts = ref.split(".", 1)

                    if len(ref_parts) == 2:
                        src_node, src_output = ref_parts

                        # If the referenced source is in the cycle's upstream sources, validate
                        if src_node in upstream_sources:
                            if src_node in output_types and src_output in output_types[src_node]:
                                actual_type = output_types[src_node][src_output]
                                # Check type compatibility if we have expected type info
                                capability_ref = node.get("capabilityRef", {})
                                if isinstance(capability_ref, dict):
                                    cap_id = capability_ref.get("capabilityId", "")
                                    # Could fetch expected types from capability repo

                                # Also check if the source node is in the cycle
                                if src_node in cycles:
                                    report.add_issue(ValidationIssue(
                                        category=ValidationCategory.CONTRACT_COMPLIANCE,
                                        severity=ValidationSeverity.WARNING,
                                        message=f"Cycle contract check: Node '{node_id}' (in cycle) receives '{src_output}' from '{src_node}' (also in cycle). Type: {actual_type}. Verify compatibility on retry/loop.",
                                        node_id=node_id,
                                        suggested_fix="Ensure input binding handles all possible output types from cycle sources",
                                        auto_fixable=False
                                    ))

                                # Check if the referenced output exists
                                if src_node in output_types and src_output not in output_types[src_node]:
                                    report.add_issue(ValidationIssue(
                                        category=ValidationCategory.CONTRACT_COMPLIANCE,
                                        severity=ValidationSeverity.ERROR,
                                        message=f"Cycle contract violation: Node '{node_id}' references '{src_node}.{src_output}' but that output no longer exists. Retry/loop will fail.",
                                        node_id=node_id,
                                        suggested_fix="Update binding to reference an existing output from the cycle source",
                                        auto_fixable=True
                                    ))


class RepairEngine:
    """
    Automatically repairs common validation issues by generating targeted fixes.
    """

    def __init__(
        self,
        validator: WorkflowValidator,
        llm_caller: Callable[[str, str, str], Awaitable[str]],
    ):
        self.validator = validator
        self.llm_caller = llm_caller

    async def repair(self, report: ValidationReport, max_iterations: int = 3) -> Tuple[ValidationReport, List[Dict[str, Any]]]:
        """
        Attempt to automatically repair validation issues.

        Returns:
            (updated_report, applied_fixes)
        """
        applied_fixes = []
        current_report = report

        for iteration in range(max_iterations):
            auto_fixable = [i for i in current_report.issues if i.auto_fixable and i.severity == ValidationSeverity.ERROR]

            if not auto_fixable:
                break

            for issue in auto_fixable:
                fix_result = await self._apply_fix(issue)
                if fix_result:
                    applied_fixes.append(fix_result)

            # Re-validate after fixes
            current_report = await self.validator.validate_all()

            if current_report.passed:
                break

        return current_report, applied_fixes

    async def _apply_fix(self, issue: ValidationIssue) -> Optional[Dict[str, Any]]:
        """Apply a specific fix based on issue type"""
        if issue.category == ValidationCategory.CONTRACT_COMPLIANCE:
            return await self._fix_contract_issue(issue)
        elif issue.category == ValidationCategory.SYNTAX_VALIDITY:
            return await self._fix_syntax_issue(issue)
        elif issue.category == ValidationCategory.TYPE_SAFETY:
            return await self._fix_type_issue(issue)
        return None

    async def _fix_contract_issue(self, issue: ValidationIssue) -> Optional[Dict[str, Any]]:
        """Fix binding reference issues"""
        if not issue.node_id:
            return None

        node = self.validator.nodes_by_id.get(issue.node_id)
        if not node:
            return None

        config = node.get("config", {})
        input_bindings = config.get("inputBindings", {})

        # Find the problematic binding
        for param_name, binding_expr in input_bindings.items():
            if isinstance(binding_expr, str) and issue.message and param_name in issue.message:
                # Try to find correct reference
                match = re.search(r"references unknown source '(.+?)'", issue.message)
                if match:
                    bad_ref = match.group(1)
                    # Find valid alternative
                    valid_ref = self._find_valid_reference(node, bad_ref)
                    if valid_ref:
                        input_bindings[param_name] = f"{{{{{valid_ref}}}}}"
                        return {
                            "type": "contract_fix",
                            "node_id": issue.node_id,
                            "parameter": param_name,
                            "old_binding": binding_expr,
                            "new_binding": f"{{{{{valid_ref}}}}}",
                            "success": True
                        }

        return None

    def _find_valid_reference(self, node: Dict, bad_ref: str) -> Optional[str]:
        """Find a valid reference for a bad binding"""
        # This would need access to available_vars
        # Simplified - just return None for now
        return None

    async def _fix_syntax_issue(self, issue: ValidationIssue) -> Optional[Dict[str, Any]]:
        """Fix syntax errors using LLM"""
        if not issue.file_path or issue.file_path not in self.validator.codebase:
            return None

        content = self.validator.codebase[issue.file_path]
        lines = content.split('\n')

        # Get context around error
        start = max(0, (issue.line_number or 1) - 5)
        end = min(len(lines), (issue.line_number or 1) + 5)
        context = '\n'.join(lines[start:end])

        prompt = f"""Fix the syntax error in this Python code:

FILE: {issue.file_path}
ERROR: {issue.message}
LINE: {issue.line_number}

CODE CONTEXT:
```python
{context}
```

Return ONLY the fixed code block (the same lines with the fix applied), no explanations."""

        try:
            response = await self.llm_caller(
                "You are a Python syntax fixer. Return only corrected code.",
                prompt,
                "syntax-fix"
            )

            fixed_code = response.strip()
            # Apply fix
            new_lines = lines[:start] + fixed_code.split('\n') + lines[end:]
            self.validator.codebase[issue.file_path] = '\n'.join(new_lines)

            return {
                "type": "syntax_fix",
                "file_path": issue.file_path,
                "line": issue.line_number,
                "original": context,
                "fixed": fixed_code,
                "success": True
            }
        except Exception as e:
            return {
                "type": "syntax_fix",
                "file_path": issue.file_path,
                "success": False,
                "error": str(e)
            }

    async def _fix_type_issue(self, issue: ValidationIssue) -> Optional[Dict[str, Any]]:
        """Fix type compatibility issues"""
        # Type fixes usually require manual intervention
        # Could suggest adding type conversion
        return None


async def run_validation_and_repair(
    phase_graphs: List[Dict[str, Any]],
    codebase: Dict[str, str],
    global_inputs: List[Dict[str, Any]],
    llm_caller: Callable[[str, str, str], Awaitable[str]],
    max_repair_iterations: int = 3,
) -> Tuple[ValidationReport, List[Dict[str, Any]]]:
    """
    Main entry point for validation and repair loop.

    Args:
        phase_graphs: All phase graphs
        codebase: Current code files (path -> content)
        global_inputs: Global input definitions
        llm_caller: LLM calling function
        max_repair_iterations: Maximum repair attempts

    Returns:
        (final_report, applied_fixes)
    """
    validator = WorkflowValidator(phase_graphs, codebase, global_inputs, llm_caller)
    repair_engine = RepairEngine(validator, llm_caller)

    # Initial validation
    report = await validator.validate_all()

    if not report.passed:
        # Attempt repair
        report, fixes = await repair_engine.repair(report, max_repair_iterations)
    else:
        fixes = []

    return report, fixes


def format_validation_report(report: ValidationReport) -> str:
    """Format validation report for user display"""
    lines = [
        "=" * 60,
        "WORKFLOW VALIDATION REPORT",
        "=" * 60,
        f"Status: {'✅ PASSED' if report.passed else '❌ FAILED'}",
        f"Total Issues: {len(report.issues)}",
        f"  Errors: {len(report.get_errors())}",
        f"  Warnings: {len(report.get_warnings())}",
        "",
    ]

    for category in ValidationCategory:
        issues = report.get_by_category(category)
        if not issues:
            continue

        lines.append(f"\n{category.value.upper()}:")
        for issue in issues:
            icon = "🔴" if issue.severity == ValidationSeverity.ERROR else "🟡" if issue.severity == ValidationSeverity.WARNING else "🔵"
            lines.append(f"  {icon} {issue.message}")
            if issue.node_id:
                lines.append(f"     Node: {issue.node_id}")
            if issue.file_path:
                lines.append(f"     File: {issue.file_path}:{issue.line_number or '?'}")
            if issue.suggested_fix:
                lines.append(f"     Fix: {issue.suggested_fix}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)