"""
llm_freeze_codegen.py
─────────────────────
LLM-powered generator for FREEZE-phase LangGraph code.

Drop-in companion to the existing `_generate_langgraph_code_for_phases` pipeline.
Instead of building the code through manual string-append logic, this module
serialises the full phase graph into a structured prompt and asks the LLM to emit
the correct Python — giving it the same "all context, all at once" advantage that
makes the DYNAMIC phase generation robust.

Public surface
──────────────
    generate_freeze_phase_code_via_llm(
        phase_results, workflow_name, global_caps=None
    ) -> str

    Accepts the same arguments as the existing `_generate_langgraph_code_for_phases`
    call site and returns the same string (valid Python source).  On failure it falls
    back to the original manual generator automatically so nothing breaks downstream.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response

logger = logging.getLogger(__name__)

# ── Canonical reference code injected verbatim into the LLM prompt ───────────
# The LLM is shown an exact, working example of the patterns it must follow so
# it can emit code that is structurally identical no matter how complex the graph.

_FREEZE_REFERENCE_CODE = textwrap.dedent(
    """
    # ── FREEZE PHASE REFERENCE EXAMPLE ──────────────────────────────────────────
    # This is the EXACT structure you must reproduce for every FREEZE phase.
    #
    # Key rules:
    #   1.  Every non-end node → a `def node_<phase>_<safe_id>(state)` function
    #       that calls `_execute_dynamic_node(node_def, state)` with repr(node_dict).
    #   2.  `builder.add_node('<safe_id>', node_<phase>_<safe_id>)` for every node.
    #   3.  `builder.add_edge(START, '<entry_safe_id>')` exactly once.
    #   4.  Simple edges (single target, no condition) →
    #           builder.add_edge('<src>', '<tgt>')   or   builder.add_edge('<src>', END)
    #   5.  Conditional/multi-target edges → emit a `route_<phase>_<src>(state)` func
    #       that calls `_evaluate_condition(cond_str, state)` and returns a path key,
    #       then: builder.add_conditional_edges('<src>', route_fn, {path_map})
    #   6.  node-id dashes must be replaced with underscores in ALL Python identifiers.
    #   7.  node_dict uses repr() (Python None/True/False), NOT json.dumps.
    #   8.  The entry-node id comes from graph.entryNodeId.
    #   9.  Nodes whose type == "end" must NOT get a node function — map them to END.
    #   10. Loop-back edges (target id == a previously defined node) are wired the
    #       same way; LangGraph handles cycles transparently.

    # --- single-phase standalone skeleton ---

    from typing import TypedDict, Annotated, Dict, Any
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    import re

    if 'execute_dynamic_tool' not in globals():
        def execute_dynamic_tool(tool_name: str, params: dict) -> Any:
            return {'error': 'execute_dynamic_tool not injected by executor'}

    def _merge_node_outputs(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(a) if a else {}
        if b:
            merged.update(b)
        return merged

    class AgenticState(TypedDict):
        global_inputs: Dict[str, Any]
        node_outputs: Annotated[Dict[str, Any], _merge_node_outputs]

    # [FREEZE_HELPERS are emitted here by the outer wrapper — do NOT re-emit them]

    GRAPH_TYPE = 'FREEZE'

    builder = StateGraph(AgenticState)

    # example node function
    def node_p0_step_a(state):
        node_def = {'nodeId': 'step-a', 'type': 'tool', 'name': 'Step A',
                    'capabilityRef': {'capabilityId': 'tool:my_tool', 'kind': 'tool'},
                    'config': {'inputBindings': {'param': '{{global.value}}'},
                               'declaredOutputs': []}}
        return _execute_dynamic_node(node_def, state)
    builder.add_node('step_a', node_p0_step_a)

    # example conditional router
    def route_p0_step_a(state):
        if _evaluate_condition("step_a.result == 'ok'", state):
            return 'path_0'
        return 'path_1'

    builder.add_edge(START, 'step_a')
    builder.add_conditional_edges('step_a', route_p0_step_a,
                                  {'path_0': 'next_node', 'path_1': END})
    # ── END REFERENCE ────────────────────────────────────────────────────────────
    """
)

_SYSTEM_PROMPT = textwrap.dedent(
    """
    You are an expert LangGraph code generator.  Your sole job is to emit valid,
    runnable Python source code for one or more FREEZE phases of a LangGraph workflow.

    STRICT OUTPUT CONTRACT
    ──────────────────────
    • Output ONLY raw Python source code — no markdown fences, no prose, no comments
      outside the generated code itself.
    • Do not re-emit the imports, state classes, or FREEZE helper functions unless you
      are generating a standalone single-phase file (the prompt will tell you which).
    • Follow the reference example EXACTLY for node functions, edge wiring, and
      router functions.
    • node-id dashes → underscores in ALL Python identifiers (node ids, fn names,
      builder.add_node keys, builder.add_edge arguments).
    • Use repr() for node_dict (Python None/True/False), never json.dumps.
    • Nodes with type == "end" must be wired to the LangGraph `END` sentinel, not
      given a Python function.
    • Emit one `route_<phase>_<src>(state)` function per source node that has
      multiple outgoing edges; call `_evaluate_condition(cond_str, state)` inside it.
    • Emit `builder.add_edge(START, '<entry_node_id>')` exactly once per mini-graph.
    • If a phase has loops (a target node that already exists as a source), wire them
      normally — LangGraph supports cycles.
    """
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_id(raw: str) -> str:
    """Replace dashes (and any non-word char) with underscores."""
    return re.sub(r"\W", "_", raw)


def _serialize_phase(pr: Any, phase_label: str) -> dict:
    """
    Converts a PhaseGraphResult into a plain dict that is safe to JSON-serialise
    and compact enough to fit inside a prompt without blowing the context window.
    """
    graph = pr.graph

    nodes_out = []
    for n in graph.nodes:
        nd = {
            "nodeId": n.nodeId,
            "type": n.type,
            "name": getattr(n, "name", ""),
        }
        if getattr(n, "capabilityRef", None):
            nd["capabilityRef"] = {
                "capabilityId": n.capabilityRef.capabilityId,
                "kind": getattr(n.capabilityRef, "kind", ""),
            }
        cfg = getattr(n, "config", None)
        if cfg:
            nd["config"] = {
                "inputBindings": getattr(cfg, "inputBindings", {}) or {},
                "declaredOutputs": [
                    {"name": o.name} for o in (getattr(cfg, "declaredOutputs", None) or [])
                ],
            }
        nodes_out.append(nd)

    edges_out = []
    for e in graph.edges:
        edges_out.append({
            "fromNode": e.fromNode,
            "to": e.to,
            "condition": getattr(e, "condition", None) or "",
        })

    return {
        "phaseLabel": phase_label,
        "phaseId": getattr(graph, "phaseId", phase_label),
        "entryNodeId": getattr(graph, "entryNodeId", None),
        "description": getattr(graph, "description", None) or getattr(graph, "goal", ""),
        "nodes": nodes_out,
        "edges": edges_out,
    }


def _build_prompt(
    phase_results: List[Any],
    workflow_name: str,
    is_standalone: bool,
    safe_phase_ids: List[str],
    builder_vars: List[str],
) -> str:
    """Assembles the full user prompt sent to the LLM."""

    serialised_phases = []
    for pr, label, safe_id, bvar in zip(
        phase_results, safe_phase_ids, safe_phase_ids, builder_vars
    ):
        data = _serialize_phase(pr, label)
        data["safe_phase_id"] = safe_id
        data["builder_var"] = bvar
        serialised_phases.append(data)

    phases_json = json.dumps(serialised_phases, indent=2, default=str)

    if is_standalone:
        context_block = textwrap.dedent(
            f"""
            TASK: Generate a STANDALONE single-phase FREEZE file for workflow "{workflow_name}".

            Include ALL of the following at the top of the file (in this order):
              1. Imports
              2. execute_dynamic_tool fallback
              3. _merge_node_outputs helper
              4. AgenticState TypedDict
              5. All _FREEZE_HELPERS functions (_resolve_bindings, _evaluate_condition,
                 _normalize_node_def, _execute_dynamic_node) — copy them EXACTLY as shown
                 in the reference without modification.
              6. GRAPH_TYPE = 'FREEZE'
              7. builder = StateGraph(AgenticState)
              8. All node functions + builder wiring for the single phase.

            builder_var  = "{builder_vars[0]}"    ← but for standalone use "builder" directly
            safe_phase_id = "{safe_phase_ids[0]}"
            """
        )
    else:
        context_block = textwrap.dedent(
            f"""
            TASK: Generate ONLY the per-phase node functions and builder wiring for
            each FREEZE phase listed below.  The file header (imports, state classes,
            FREEZE helpers) has already been emitted.  Do NOT re-emit them.

            For each phase emit, in order:
              1. A comment line:  # Phase: <phaseId>
              2. All node_<safe_phase_id>_<safe_node_id>(state) functions.
              3. All route_<safe_phase_id>_<src>(state) functions (if needed).
              4. All builder.add_node / add_edge / add_conditional_edges calls.

            Use the builder_var given for each phase (NOT "builder" unless specified).
            """
        )

    return textwrap.dedent(
        f"""
        {context_block}

        WORKFLOW NAME: {workflow_name}

        PHASE GRAPH DATA (JSON):
        {phases_json}

        REFERENCE CODE (follow this pattern exactly):
        {_FREEZE_REFERENCE_CODE}

        IMPORTANT REMINDERS:
        - Replace ALL dashes with underscores in Python identifiers.
        - Use repr(node_dict) — Python syntax, not JSON.
        - Nodes with type "end" → wire to END sentinel, no Python function.
        - Entry node → builder.add_edge(START, '<entry_safe_id>').
        - Conditional edges → route_<phase>_<src> function + add_conditional_edges.
        - Loop-back edges are fine — just wire them normally.

        Output ONLY the Python source code. No markdown, no explanation.
        """
    )


# ── LLM call ─────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    """
    Calls the LangGraph code-generation LLM.

    Uses google-generativeai (Gemini) by default since the rest of the codebase
    already depends on it.  If the environment variable OPENAI_API_KEY is present
    the caller can swap the backend by monkey-patching this function.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore
        import os

        llm = ChatGoogleGenerativeAI(
            model="gemini-3.6-flash",
            google_api_key=os.getenv("GEMINI_API_KEY", ""),
            temperature=0,
        )
        started_at = datetime.utcnow()
        response = llm.invoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        finished_at = datetime.utcnow()
        usage = extract_token_usage_from_response(response)
        record_llm_call(
            org_id=None,
            subscriber_id=None,
            operation="langgraph-codegen",
            sub_operation="freeze-codegen",
            model="gemini-3.6-flash",
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            metadata={},
        )
        raw = response.content if hasattr(response, "content") else str(response)
        # Strip accidental markdown fences the model might add despite the instruction
        raw = re.sub(r"^```(?:python)?\s*", "", raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)
        return raw.strip()

    except Exception as exc:
        try:
            record_llm_call(
                org_id=None,
                subscriber_id=None,
                operation="langgraph-codegen",
                sub_operation="freeze-codegen",
                model="gemini-3.6-flash",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                status="failed",
                metadata={"error": str(exc)},
            )
        except Exception:
            # Never let token tracking failures mask the original exception.
            pass
        logger.error("LLM call failed in _call_llm: %s", exc, exc_info=True)
        raise


# ── Validation pass ───────────────────────────────────────────────────────────

def _validate_generated_code(code: str, phase_results: List[Any]) -> List[str]:
    """
    Lightweight structural validation of LLM-generated code.
    Returns a list of warning strings (empty = all good).
    """
    warnings: List[str] = []

    # Must compile as valid Python
    try:
        compile(code, "<llm_generated>", "exec")
    except SyntaxError as e:
        warnings.append(f"SyntaxError in LLM output: {e}")
        return warnings  # further checks won't be meaningful

    # Every non-end node should have a corresponding add_node call
    for pr in phase_results:
        for node in pr.graph.nodes:
            if node.type == "end":
                continue
            safe_n = _safe_id(str(node.nodeId))
            if f"add_node('{safe_n}'" not in code:
                warnings.append(f"Missing add_node for node '{node.nodeId}' (safe: '{safe_n}')")

    # START edge must be present
    if "add_edge(START" not in code:
        warnings.append("No add_edge(START, ...) found — entry edge missing.")

    return warnings


# ── Repair pass ───────────────────────────────────────────────────────────────

def _repair_with_llm(code: str, warnings: List[str], prompt: str) -> str:
    """
    If validation found issues, send the broken code + warnings back to the LLM
    for a targeted fix — one retry only.
    """
    repair_prompt = textwrap.dedent(
        f"""
        The Python code below was generated for a LangGraph FREEZE phase but has
        the following structural problems:

        PROBLEMS:
        {chr(10).join(f'  - {w}' for w in warnings)}

        BROKEN CODE:
        ```python
        {code}
        ```

        Fix ONLY the listed problems.  Do not change anything else.
        Output ONLY the corrected Python source code — no markdown, no explanation.
        """
    )
    try:
        return _call_llm(repair_prompt)
    except Exception as exc:
        logger.warning("LLM repair call failed: %s", exc)
        return code  # return original; fallback will handle it


# ── Fallback to manual generator ──────────────────────────────────────────────

def _manual_fallback(phase_results: List[Any], workflow_name: str, global_caps: Optional[list]) -> str:
    """Delegates to the original string-based generator as a safety net."""
    try:
        from agent_new import _generate_langgraph_code_for_phases  # type: ignore
        logger.info("Using manual fallback generator for FREEZE phase(s).")
        return _generate_langgraph_code_for_phases(phase_results, workflow_name, global_caps=global_caps)
    except ImportError:
        # If relative import fails (e.g. running standalone), attempt absolute
        try:
            from agent_new import _generate_langgraph_code_for_phases  # type: ignore
            return _generate_langgraph_code_for_phases(phase_results, workflow_name, global_caps=global_caps)
        except Exception as inner_exc:
            logger.error("Manual fallback also failed: %s", inner_exc)
            return ""


# ── Public API ────────────────────────────────────────────────────────────────

def generate_freeze_phase_code_via_llm(
    phase_results: List[Any],
    workflow_name: str,
    global_caps: Optional[list] = None,
    *,
    validate: bool = True,
    repair: bool = True,
    fallback_on_error: bool = True,
) -> str:
    """
    LLM-powered replacement for the manual FREEZE phase code generator.

    Parameters
    ──────────
    phase_results       List[PhaseGraphResult]  — same as _generate_langgraph_code_for_phases
    workflow_name       str                     — human-readable workflow name
    global_caps         Optional[list]          — capability registry (unused in FREEZE but
                                                  kept for API parity)
    validate            bool (default True)     — run structural checks on LLM output
    repair              bool (default True)     — ask LLM to fix any validation warnings
    fallback_on_error   bool (default True)     — fall back to manual generator on failure

    Returns
    ───────
    str — valid Python source code for the FREEZE phase(s)
    """
    # Filter to only FREEZE phases (callers may mix types; we own only FREEZE)
    freeze_results = [
        pr for pr in phase_results
        if getattr(pr, "phaseType", "FREEZE") != "DYNAMIC"
    ]

    if not freeze_results:
        logger.debug("No FREEZE phases found; returning empty string.")
        return ""

    # ── Determine standalone vs integrated mode ───────────────────────────────
    # Standalone: exactly one FREEZE phase and no DYNAMIC phases in the batch.
    # Integrated: multiple phases or a mix — only emit per-phase wiring.
    all_freeze_only = all(
        getattr(pr, "phaseType", "FREEZE") != "DYNAMIC" for pr in phase_results
    )
    is_standalone = len(freeze_results) == 1 and all_freeze_only

    # ── Build safe ids and builder var names ──────────────────────────────────
    safe_phase_ids: List[str] = []
    builder_vars: List[str] = []

    for pr in freeze_results:
        raw_id = (
            getattr(pr.graph, "phaseId", None)
            or getattr(pr, "phaseId", None)
            or "phase"
        )
        sid = _safe_id(str(raw_id))
        safe_phase_ids.append(sid)
        builder_vars.append("builder" if is_standalone else f"_b_{sid}")

    # ── Build prompt and call LLM ─────────────────────────────────────────────
    prompt = _build_prompt(
        freeze_results,
        workflow_name,
        is_standalone=is_standalone,
        safe_phase_ids=safe_phase_ids,
        builder_vars=builder_vars,
    )

    logger.info(
        "Generating FREEZE code via LLM for phase(s): %s (standalone=%s)",
        safe_phase_ids,
        is_standalone,
    )

    try:
        llm_code = _call_llm(prompt)
    except Exception as exc:
        logger.error("LLM FREEZE code generation failed: %s", exc, exc_info=True)
        if fallback_on_error:
            return _manual_fallback(phase_results, workflow_name, global_caps)
        raise

    # ── Validation + optional repair ─────────────────────────────────────────
    if validate:
        warnings = _validate_generated_code(llm_code, freeze_results)
        if warnings:
            logger.warning(
                "LLM FREEZE code validation warnings: %s",
                warnings,
            )
            if repair:
                llm_code = _repair_with_llm(llm_code, warnings, prompt)
                # Re-validate after repair
                post_warnings = _validate_generated_code(llm_code, freeze_results)
                if post_warnings:
                    logger.warning(
                        "Repair did not fully resolve issues: %s",
                        post_warnings,
                    )
                    if fallback_on_error:
                        logger.info("Falling back to manual generator after failed repair.")
                        return _manual_fallback(phase_results, workflow_name, global_caps)
            elif fallback_on_error:
                return _manual_fallback(phase_results, workflow_name, global_caps)

    # ── Wrap standalone output with the shared header ─────────────────────────
    # For standalone single-phase files the LLM generates everything itself.
    # For integrated (multi-phase) files the header is already emitted by the
    # outer combined-file builder; we just splice in the per-phase wiring.
    if is_standalone:
        # The LLM already emits all boilerplate — return as-is.
        return llm_code

    # Integrated: return only the per-phase wiring block; the caller stitches it
    # into the combined file.
    return llm_code


# ── Convenience: full combined-file generator (drop-in replacement) ───────────

def generate_langgraph_code_for_phases_via_llm(
    phase_results: List[Any],
    workflow_name: str,
    global_caps: Optional[list] = None,
) -> str:
    """
    Full drop-in replacement for `_generate_langgraph_code_for_phases`.

    Strategy
    ────────
    • DYNAMIC phases are handled by the existing manual generator (unchanged).
    • FREEZE phases are generated by the LLM via `generate_freeze_phase_code_via_llm`.
    • The combined-file header and parent-graph wiring are always emitted by the
      trusted manual logic (same as today) so the only new surface is FREEZE wiring.

    This means:
      - If there are zero FREEZE phases, behaviour is identical to today.
      - If all phases are FREEZE, the LLM generates everything.
      - In a MIXED workflow, only the FREEZE per-phase blocks come from the LLM;
        the rest of the file is identical to the manual output.
    """
    import re as _re

    has_dynamic = any(getattr(p, "phaseType", "FREEZE") == "DYNAMIC" for p in phase_results)
    has_freeze  = any(getattr(p, "phaseType", "FREEZE") != "DYNAMIC" for p in phase_results)

    # ── Pure single-phase FREEZE: fully LLM-generated standalone ─────────────
    if len(phase_results) == 1 and has_freeze and not has_dynamic:
        return generate_freeze_phase_code_via_llm(
            phase_results,
            workflow_name,
            global_caps=global_caps,
        )

    # ── Multi-phase or MIXED: emit header manually, splice in FREEZE wiring ───
    graph_type = (
        "MIXED" if (has_dynamic and has_freeze)
        else ("DYNAMIC" if has_dynamic else "FREEZE")
    )

    code: List[str] = []

    # Header (identical to manual generator)
    code.append("from typing import TypedDict, Annotated, Sequence, Dict, Any, List, Optional")
    code.append("from langgraph.graph import StateGraph, START, END")
    code.append("from langgraph.types import interrupt")
    code.append("from langgraph.errors import GraphInterrupt")
    code.append("from langgraph.graph.message import add_messages")
    code.append("from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage, AIMessage")
    code.append("from langchain_core.tools import tool")
    code.append("from langchain_google_genai import ChatGoogleGenerativeAI")
    code.append("import os, json, re, traceback\n")

    code.append("if 'execute_dynamic_tool' not in globals():")
    code.append("    def execute_dynamic_tool(tool_name: str, params: dict) -> Any:")
    code.append("        return {'error': 'execute_dynamic_tool not injected by executor'}\n")

    code.append("def _merge_node_outputs(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:")
    code.append("    merged = dict(a) if a else {}")
    code.append("    if b:")
    code.append("        merged.update(b)")
    code.append("    return merged\n")

    code.append("MAX_ITERATIONS = 20\n")

    code.append("class AgenticState(TypedDict):")
    code.append("    global_inputs: Dict[str, Any]")
    code.append("    node_outputs: Annotated[Dict[str, Any], _merge_node_outputs]\n")

    code.append("class DynamicAgentState(TypedDict):")
    code.append("    messages: Annotated[Sequence[BaseMessage], add_messages]")
    code.append("    global_inputs: Dict[str, Any]")
    code.append("    node_outputs: Annotated[Dict[str, Any], _merge_node_outputs]")
    code.append("    iteration_count: int\n")

    code.append("class WorkflowState(TypedDict):")
    code.append("    global_inputs: Dict[str, Any]")
    code.append("    node_outputs: Annotated[Dict[str, Any], _merge_node_outputs]")
    code.append("    messages: Annotated[Sequence[BaseMessage], add_messages]")
    code.append("    iteration_count: int\n")

    code.append(f"GRAPH_TYPE = {repr(graph_type)}\n")

    if has_freeze:
        # Re-use the shared FREEZE helper list from the existing module
        try:
            from agent_new import _FREEZE_HELPERS  # type: ignore
        except ImportError:
            from agent_new import _FREEZE_HELPERS  # type: ignore
        code.extend(_FREEZE_HELPERS)

    # ── Per-phase blocks ──────────────────────────────────────────────────────
    phase_graph_vars: List[tuple] = []

    for pr in phase_results:
        raw_phase_id = (
            getattr(pr.graph, "phaseId", None)
            or getattr(pr, "phaseId", None)
            or "phase"
        )
        safe_phase_id = _safe_id(str(raw_phase_id))
        phase_type    = getattr(pr, "phaseType", "FREEZE")
        builder_var   = f"_b_{safe_phase_id}"
        graph_var     = f"_g_{safe_phase_id}"

        code.append(f"# {'─' * 60}")
        code.append(f"# Phase: {raw_phase_id}  ({phase_type})")
        code.append(f"# {'─' * 60}")

        if phase_type == "DYNAMIC":
            # Delegate to the existing manual dynamic block builder
            try:
                from agent_new import _build_dynamic_phase_block  # type: ignore
            except ImportError:
                from agent_new import _build_dynamic_phase_block  # type: ignore
            _build_dynamic_phase_block(pr, workflow_name, safe_phase_id, builder_var, global_caps, code)
        else:
            # FREEZE: ask the LLM to generate the per-phase wiring block
            freeze_wiring = generate_freeze_phase_code_via_llm(
                [pr],
                workflow_name,
                global_caps=global_caps,
            )
            # When called in integrated mode, generate_freeze_phase_code_via_llm
            # emits only the wiring (no header).  We need to prepend the builder
            # variable declaration the LLM skips.
            code.append(f"{builder_var} = StateGraph(AgenticState)")
            code.append(freeze_wiring)

        code.append(f"{graph_var} = {builder_var}.compile()\n")
        phase_graph_vars.append((safe_phase_id, graph_var, phase_type))

    # ── Parent graph (identical to manual generator) ───────────────────────────
    code.append("# Parent graph — chains all phases; executor compiles this with checkpointer")
    code.append("builder = StateGraph(WorkflowState)")

    for safe_id, gvar, _ in phase_graph_vars:
        code.append(f"builder.add_node('phase_{safe_id}', {gvar})")

    prev_type = None
    phase_node_sequence: List[str] = []

    for safe_id, _, phase_type in phase_graph_vars:
        if phase_type == "DYNAMIC" and prev_type == "DYNAMIC":
            reset_fn   = f"_reset_before_{safe_id}"
            reset_node = f"reset_{safe_id}"
            code.append(f"def {reset_fn}(state: WorkflowState) -> dict:")
            code.append(f"    return {{'iteration_count': 0}}")
            code.append(f"builder.add_node('{reset_node}', {reset_fn})")
            phase_node_sequence.append(reset_node)
        phase_node_sequence.append(f"phase_{safe_id}")
        prev_type = phase_type

    code.append(f"builder.add_edge(START, '{phase_node_sequence[0]}')")
    for i in range(len(phase_node_sequence) - 1):
        code.append(f"builder.add_edge('{phase_node_sequence[i]}', '{phase_node_sequence[i + 1]}')")
    code.append(f"builder.add_edge('{phase_node_sequence[-1]}', END)")

    return "\n".join(code)