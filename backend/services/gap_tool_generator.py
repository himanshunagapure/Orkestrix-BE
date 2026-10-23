"""
Gap tool generator — LLM codegen for GENERATE:* tool nodes.

Extracted from agent_new.py for maintainability. Orchestration
(_resolve_one_gap_node / _resolve_gap_generations) stays in agent_new.

Design (Task 2):
  - Always emit real runnable function_code.
  - Missing credentials must NOT produce capability_gap stubs.
  - At runtime, read secrets from request_meta["credentials"] and return a clear
    error if a required credential is not configured (same pattern as backend_generator).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import textwrap
import uuid
from datetime import datetime
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from crewai.tools import BaseTool
from json_repair import repair_json

from services.capability_sync import normalize_input_parameters, normalize_output_parameters
from services.gap_tool_output_contract import (
    apply_gap_output_contract_fix,
    build_gap_tool_output_contract_fix_prompt as _build_gap_tool_output_contract_fix_prompt,
    classify_gap_output_contract as _classify_gap_output_contract,
    gap_output_param_names as _gap_output_param_names,
    pin_gap_tool_output_parameters as _pin_gap_tool_output_parameters_raw,
)
from services.gap_tool_syntax import (
    apply_gap_syntax_fix,
    build_gap_tool_syntax_fix_prompt,
    gap_tool_function_code_syntax_error,
    gap_tool_model_for_attempt,
    heuristic_fix_gap_tool_syntax,
    is_gap_tool_syntax_rejection,
)
from services.gemini_service import async_generate, GeminiGenerateConfig, google_search_tool
from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response
from services.provider_operation import (
    classify_provider_spec,
    description_implies_external_http_api,
    extract_provider_operations_from_text,
    format_pinned_provider_spec,
    function_code_matches_provider_fixture,
    node_provider_operations,
    operations_from_dicts,
    operations_to_dicts,
    resolve_rest_api_spec_with_grounding,
)
from services.persistence_link import (
    format_pinned_persistence_spec,
    function_code_matches_persistence_fixture,
    links_to_dicts,
    resolve_persistence_links,
)
from services.tool_param_utils import coerce_required_flag
from services.workflow_budget_service import BillingError
from repositories.capability_repository import CapabilityRepository, dynamic_tool_class_path

logger = logging.getLogger(__name__)

capability_repository = CapabilityRepository()


def _get_platform_tenant() -> Dict[str, Optional[str]]:
    """Platform-global tenant (lazy import to avoid circular deps with agent_new)."""
    try:
        from agent_new import _get_platform_tenant as _gpt  # type: ignore

        return _gpt()
    except Exception:
        return {"orgId": None, "subscriberId": None, "userId": None}


def _get_register_tool():
    """Lazy import — tool_registry is defined late in agent_new."""
    from agent_new import register_tool  # type: ignore

    return register_tool


def _get_tool_registry():
    from agent_new import tool_registry  # type: ignore

    return tool_registry



def _gap_unique_dynamic_tool_key(node: Any, subscriber_id: Optional[str] = None) -> str:
    """
    Registry key for DynamicToolRegistry (spaces stripped); must stay stable per generation call.

    For subscriber-owned tools the key is suffixed with ``_sb_<short_subscriber_id>`` so that:
      - The same logical tool name can be registered for different subscribers without collision.
      - The runtime registry can filter tools by subscriber at execution time.
    Platform-global tools have no ``_sb_`` suffix.
    """
    base = (node.nodeId or node.name or "gap_tool").strip()
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", base)
    slug = re.sub(r"_+", "_", slug).strip("_") or "gap_tool"
    if slug and slug[0].isdigit():
        slug = f"t_{slug}"
    slug = (slug or "gap_tool")[:40]
    unique_suffix = uuid.uuid4().hex[:8]
    if subscriber_id and str(subscriber_id).strip():
        # Use last 8 hex chars of subscriber id to keep the key readable but unique per subscriber.
        sub_short = re.sub(r"[^a-zA-Z0-9]", "", str(subscriber_id))[-8:]
        return f"{slug}_{unique_suffix}_sb_{sub_short}"
    return f"{slug}_{unique_suffix}"


def _gap_generation_io_contract_dict(gen: Any) -> Dict[str, Any]:
    def _rows(params: list[Any]) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for p in (params or []):
            row: Dict[str, Any] = {
                "name": p.name,
                "dataType": getattr(p, "dataType", None) or "Any",
                "required": bool(getattr(p, "required", False)),
                "description": getattr(p, "description", "") or "",
            }
            item_schema = getattr(p, "itemSchema", None)
            item_type = getattr(p, "itemType", None)
            scalar_dt = getattr(p, "scalarItemDatatype", None)
            enum_values = getattr(p, "enumValues", None)
            if item_type:
                row["itemType"] = item_type
            if scalar_dt:
                row["scalarItemDatatype"] = scalar_dt
            if enum_values:
                row["enumValues"] = list(enum_values)
            if item_schema:
                # Inline the itemSchema so the code-gen LLM knows the record shape
                # for list parameters — without this the generator sees only "list"
                # with no field details and cannot produce correct iteration code.
                def _field_val(f: Any, *attrs: str, default: str = "") -> str:
                    for attr in attrs:
                        v = f.get(attr) if isinstance(f, dict) else getattr(f, attr, None)
                        if v:
                            return str(v)
                    return default

                def _dump_item_field(f: Any) -> Dict[str, Any]:
                    field_row: Dict[str, Any] = {
                        "name": _field_val(f, "name"),
                        # NodeParam uses lowercase "datatype"; GenerationIOParam uses "dataType"
                        "dataType": _field_val(f, "datatype", "dataType", default="string"),
                        "description": _field_val(f, "description"),
                    }
                    nested_item_type = (
                        f.get("itemType") if isinstance(f, dict) else getattr(f, "itemType", None)
                    )
                    nested_scalar = (
                        f.get("scalarItemDatatype")
                        if isinstance(f, dict)
                        else getattr(f, "scalarItemDatatype", None)
                    )
                    nested_enum = (
                        f.get("enumValues") if isinstance(f, dict) else getattr(f, "enumValues", None)
                    )
                    nested_schema = (
                        f.get("itemSchema") if isinstance(f, dict) else getattr(f, "itemSchema", None)
                    )
                    if nested_item_type:
                        field_row["itemType"] = nested_item_type
                    if nested_scalar:
                        field_row["scalarItemDatatype"] = nested_scalar
                    if nested_enum:
                        field_row["enumValues"] = list(nested_enum)
                    if nested_schema:
                        field_row["itemSchema"] = [_dump_item_field(nf) for nf in nested_schema]
                    return field_row

                row["itemSchema"] = [_dump_item_field(f) for f in item_schema]
            rows.append(row)
        return rows

    return {
        "inputParameters": _rows(gen.inputs or []),
        "outputParameters": _rows(gen.outputs or []),
    }

def _strip_llm_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text

def _remove_standalone_pass_lines(function_code: str) -> str:
    """Drop lines that are only `pass` (common LLM stub); keeps real code."""
    kept: list[str] = []
    for line in function_code.splitlines():
        if re.match(r"^\s*pass\s*($|#)", line):
            continue
        kept.append(line)
    return "\n".join(kept)

def _parse_gap_tool_llm_response(raw: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Parse gap-tool LLM output into a dict. Tries direct JSON, repair_json, and brace slicing.
    """
    text = _strip_llm_json_fences(raw)
    if not text:
        return None, "empty LLM response"

    chunks: list[str] = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        sliced = text[start : end + 1]
        if sliced not in chunks:
            chunks.append(sliced)

    last_err: Optional[str] = None
    for chunk in chunks:
        for label, loader in (("json", json.loads), ("repair", repair_json)):
            try:
                obj = loader(chunk)
            except Exception as exc:
                last_err = f"{label} parse failed: {exc}"
                continue
            if isinstance(obj, dict):
                return obj, None
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        return item, None
            if isinstance(obj, str):
                nested, nested_err = _parse_gap_tool_llm_response(obj)
                if nested is not None:
                    return nested, None
                last_err = nested_err or last_err

    preview = text[:400].replace("\n", "\\n")
    return None, last_err or f"no JSON object found (preview={preview!r})"


# Gemini model for agentic gap-tool codegen (dynamic tool JSON + function_code).
GAP_TOOL_GENERATION_MODEL = "gemini-3.5-flash"
# Dedicated syntax fixer + generation retry after a syntax reject (live Issue 7).
GAP_TOOL_FIX_MODEL = "gemini-2.5-pro"

# Model IDs that must not appear in generated tool code (known deprecated families).
_GAP_TOOL_DEPRECATED_MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"gemini-2\.5-flash", "deprecated gemini-2.5-flash model id; use gemini-3.5-flash-lite"),
    (r"gemini-2\.0-flash[-_a-z0-9]*", "deprecated gemini-2.0-flash model id; use gemini-3.5-flash-lite"),
    (r"gemini-2\.0-pro[-_a-z0-9]*", "deprecated gemini-2.0-pro model id; use gemini-3-flash-preview"),
    (r"gemini-1\.5[-_a-z0-9]*", "deprecated Gemini 1.5 model id"),
    (r"gemini-1\.0[-_a-z0-9]*", "deprecated Gemini 1.0 model id"),
    (r"gemini-pro-vision", "deprecated gemini-pro-vision model id"),
    (r"['\"]gemini-pro['\"]", "deprecated gemini-pro model id"),
    (r"gpt-3\.5-turbo", "deprecated gpt-3.5-turbo model id"),
    (r"text-davinci", "deprecated OpenAI davinci model id"),
    (r"claude-2[\.\-]", "deprecated Claude 2.x model id"),
    (r"claude-instant", "deprecated Claude instant model id"),
]

# Hosted LLM / vision APIs need live model-id research; CRUD/email/DB tools do not.
_GAP_TOOL_LLM_RESEARCH_PATTERN = re.compile(
    r"(?:"
    r"gemini|gpt[-\s]?[34o]|openai|anthropic|claude|"
    r"\bllm\b|language\s+model|generative\s+ai|chatgpt|"
    r"vision\s+model|multimodal|"
    r"\bocr\b|\bvision\b"
    r")",
    re.IGNORECASE,
)
_GAP_TOOL_HOSTED_LLM_PATTERN = re.compile(
    r"gemini|gpt|claude|openai|anthropic|\bllm\b|chatgpt|generative",
    re.IGNORECASE,
)


def _gap_tool_description_implies_llm_api(
    description: str,
    io_contract: Dict[str, Any],
) -> bool:
    """
    True when gap-tool codegen likely needs Google Search model-id research.

  Pure CRUD, email, SMS, and MongoDB tools return False. Tesseract-only OCR
    returns False unless a hosted LLM provider is also mentioned.
    """
    parts: list[str] = [(description or "").strip()]
    try:
        parts.append(json.dumps(io_contract, ensure_ascii=False))
    except Exception:
        parts.append(str(io_contract))
    text = "\n".join(p for p in parts if p)
    if not text.strip():
        return False

    if not _GAP_TOOL_LLM_RESEARCH_PATTERN.search(text):
        return False

    # Tesseract-only OCR — no hosted model id lookup required.
    if re.search(r"\btesseract\b", text, re.IGNORECASE):
        if not _GAP_TOOL_HOSTED_LLM_PATTERN.search(text):
            return False

    return True


async def _gap_tool_fetch_model_research_via_google(
    description: str,
    io_contract: Dict[str, Any],
    available_credentials: Optional[List[Dict[str, Any]]],
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    refresh_hint: Optional[str] = None,
) -> str:
    """
    Use Gemini + Google Search grounding to resolve current API model ids for this tool task.
    Result is injected into the gap-tool codegen prompt (not hardcoded in platform config).
    """
    cred_list = [
        {"name": c.get("name"), "type": c.get("type"), "service": c.get("service")}
        for c in (available_credentials or [])
        if c.get("name")
    ]
    refresh_block = ""
    if refresh_hint:
        refresh_block = f"\nPrevious codegen failed validation: [{refresh_hint}]. Find a newer model id.\n"

    research_prompt = f"""You are a technical researcher. Use Google Search to find CURRENT official API model id strings.

Task for the tool being built:
{description.strip()}

Workflow io_contract:
{json.dumps(io_contract, indent=2)}

Configured project credentials (infer which providers apply):
{json.dumps(cred_list, indent=2) if cred_list else "[]"}
{refresh_block}
Instructions:
1. Search for the latest up to date documentation for each provider this task needs (Gemini, OpenAI, Anthropic, etc.).
2. List the exact model id string to pass to that provider's SDK (e.g. GenerativeModel("..."), model= parameter).
3. Separate recommendations for vision/multimodal vs text-only if the task needs images/OCR.
4. List deprecated model ids that must NOT be used for this provider. For Gemini specifically, these are KNOWN DEPRECATED and must never appear in generated code:
   - gemini-2.0-flash (and all gemini-2.0-* variants) → use gemini-3.5-flash-lite
   - gemini-2.5-flash → use gemini-3.5-flash-lite
   - gemini-1.5-flash, gemini-1.5-pro, gemini-1.0-pro, gemini-pro, gemini-pro-vision
5. Only cover providers actually required by the task and credentials — do not invent providers.

Return plain text (not JSON), concise, with one section per provider."""

    started_at = datetime.utcnow()
    try:
        response = await async_generate(
            GAP_TOOL_GENERATION_MODEL,
            research_prompt,
            config=GeminiGenerateConfig(
                temperature=0.1,
                tools=google_search_tool(),
            ),
        )
        text = (response.text or "").strip()
        usage = extract_token_usage_from_response(response)
        prompt_tokens = usage.get("prompt_tokens") or len(research_prompt) // 4
        completion_tokens = usage.get("completion_tokens") or len(text) // 4
        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="graph-planning",
            sub_operation="gap-tool-model-research",
            model=GAP_TOOL_GENERATION_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            status="success",
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
        if text:
            return text[:8000]
    except BillingError:
        raise
    except Exception as exc:
        logger.warning("[gap-tool] Google Search model research failed: %s", exc)
        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="graph-planning",
            sub_operation="gap-tool-model-research",
            model=GAP_TOOL_GENERATION_MODEL,
            prompt_tokens=len(research_prompt) // 4,
            completion_tokens=0,
            total_tokens=len(research_prompt) // 4,
            status="error",
            started_at=started_at,
            finished_at=datetime.utcnow(),
            metadata={"error": str(exc)},
        )
    return ""


def _build_gap_tool_model_research_block(model_research: str) -> str:
    """Inject Google Search research into codegen prompt (replaces hardcoded model lists)."""
    body = (model_research or "").strip()
    if not body:
        body = (
            "(Research unavailable.) Pick model ids from each provider's current official API docs "
            "for the capabilities this task needs (vision, chat, etc.). Do not use ids known to be "
            "deprecated (gemini-2.5-*, gemini-pro-vision, gpt-3.5-turbo, claude-2*, etc.)."
        )
    return f"""
MODEL RESEARCH (Google Search — authoritative for model id strings in function_code):
{body}

Use the model id strings from this research in your generated function_code. Do not guess outdated defaults.

"""


async def async_gap_tool_llm_call(
    prompt: str,
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
) -> tuple[str, dict]:
    """Gemini call with JSON response mode for reliable gap-tool structured output."""
    chosen = model or GAP_TOOL_GENERATION_MODEL
    response = await async_generate(
        chosen,
        prompt,
        config=GeminiGenerateConfig(
            temperature=temperature,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        ),
    )
    usage = extract_token_usage_from_response(response)
    try:
        return (response.text or "").strip(), usage
    except Exception as exc:
        logger.warning("[gap-tool] LLM response.text failed: %s", exc)
        return "", usage


_MOCK_PATTERNS: list[tuple[str, str]] = [
    (r"#\s*(simulate|mock|placeholder|stub|demonstration|fake|dummy)", "simulation comment"),
    (r"\bSimulate\b", "Simulate keyword"),
    (r"\bmock\s+(data|result|response|value|output)\b", "mock data"),
    (r"for\s+(demonstration|illustration)\s+purposes", "demonstration disclaimer"),
    (r"# In a real scenario", "real-scenario comment"),
    (r"# Assume successful", "assume-success comment"),
    (r"# For (this |the )?example", "example comment"),
    (r"hardcoded", "hardcoded mention"),
    (r"raise NotImplementedError", "NotImplementedError"),
    (r"raise ValueError\(['\"]not implemented", "not-implemented ValueError"),
    (r"\bpass\b\s*$", "bare pass statement"),
    (r"# TODO", "TODO comment"),
    (r"\"INV-\d+\"", "hardcoded invoice literal"),
    (r"1234\.56", "hardcoded amount literal"),
    (r"sample[_\s]data", "sample data reference"),
    (r"dummy[_\s]data", "dummy data reference"),
    (r"test[_\s]data", "test data reference"),
    (r"# would involve", "would-involve comment"),
    (r"# would (call|use|connect|fetch)", "would-call comment"),
    # Dynamic tools implement BaseTool._run (sync). "await" will crash tool registration.
    (r"\bawait\b", "await keyword (tools must be synchronous)"),
    (r"\bget_sync_collection\b", "Database.get_sync_collection (use Database.upsert_one_sync / class methods)"),
    (r"\bget_collection\s*\(", "Database.get_collection (async handle; use Database.*_sync class methods)"),
    (
        r"\bDatabase\.(?:insert_one|update_one|upsert_one|find_one|delete_one)\s*\(",
        "async Database helper in sync _run (use insert_one_sync / update_one_sync / upsert_one_sync / find_one_sync / delete_one_sync)",
    ),
    (r"upsert\s*=\s*True", "raw upsert=True on a collection (use Database.upsert_one_sync)"),
    # Task 2: capability_gap stubs are forbidden — emit real code that errors at runtime if creds missing.
    (r'["\']capability_gap["\']', "capability_gap stub (forbidden — write real code)"),
    (r"\bcapability_gap\b", "capability_gap stub (forbidden — write real code)"),
]


def _detect_mock_code(function_code: str) -> Optional[str]:
    """
    Scans function_code for mock/simulation/placeholder patterns.
    Returns a human-readable description of the first offending pattern found,
    or None if the code appears production-ready.
    """
    for pattern, description in _MOCK_PATTERNS:
        if re.search(pattern, function_code, re.IGNORECASE | re.MULTILINE):
            return description
    return None


def _log_mock_code_context(function_code: str, reason: str, tool_key: str, attempt_no: int) -> None:
    """Log line numbers/snippets when mock/placeholder validation fails."""
    if "pass" not in reason.lower():
        return
    for line_no, line in enumerate(function_code.splitlines(), 1):
        if re.match(r"^\s*pass\s*($|#)", line):
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s pass stub at line %s: %r",
                tool_key,
                attempt_no,
                line_no,
                line.strip()[:120],
            )


def _function_code_defines_tool_shell(function_code: str) -> bool:
    """True when function_code wrongly defines _run or a class (helpers defs are OK)."""
    body = textwrap.dedent(function_code.replace("\r\n", "\n").replace("\t", "    ")).strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("class "):
            return True
        if re.match(r"^async\s+def\s+_run\s*\(", stripped):
            return True
        if re.match(r"^def\s+_run\s*\(", stripped):
            return True
    return False


def _gap_tool_rejection_extra_hint(reason: Optional[str]) -> str:
    """Generic retry hints appended after a validation rejection (no task/vendor hardcoding)."""
    if not reason:
        return ""
    hints: list[str] = []
    reason_lower = reason.lower()
    if "pass" in reason_lower:
        hints.append(
            "Remove EVERY bare `pass` line and every `except ...: pass` stub. "
            "Implement the full logic for the user's task and io_contract: real I/O, real parsing, "
            "and return a dict with ALL outputParameters keys. "
            "If a credential is missing at runtime, return "
            '{"error": "Missing credential \'NAME\'. Ask admin to configure it."} — never capability_gap.'
        )
    if "capability_gap" in reason_lower:
        hints.append(
            "Do NOT return capability_gap stubs. Write real implementation code. "
            "Read secrets from request_meta['credentials']; if missing at runtime, return a clear "
            "Missing credential 'NAME' error dict (include all outputParameters keys)."
        )
    if "syntax" in reason_lower or "compile" in reason_lower or "indented block" in reason_lower:
        hints.append(
            "Fix Python syntax. For f-strings that embed JSON or double quotes, use f'''...''' "
            "(triple single quotes) as the outer delimiter. "
            "In the JSON response, escape function_code newlines as \\n and quotes as \\\". "
            "Every try/except/finally/if/for/while/with/else must have an indented body — "
            "never leave `except:` or `try:` empty. On error `raise` or return a dict with "
            "all output keys plus 'error'. Do not use a bare `pass`."
        )
    if "import" in reason_lower:
        hints.append(
            "Missing third-party packages are auto-installed. If the import still fails after "
            "auto-install, the symbol/module may be wrong — fix the import path or use an "
            "installed alternative. For google.generativeai images use a dict payload, not Part "
            "from google.generativeai.types. Drop any import that is unused."
        )
    if "json" in reason_lower or "parse" in reason_lower:
        hints.append(
            "Return ONE raw JSON object with keys: name, description, parameters, outputParameters, "
            "function_code, imports. No markdown outside JSON. Escape function_code as a JSON string (\\n, \\\")."
        )
    if "deprecated" in reason_lower:
        hints.append(
            "The model id in function_code is deprecated. Use the MODEL RESEARCH section (Google Search) "
            "or search again mentally for the provider's current 2026 API model id — do not reuse gemini-2.5-*, "
            "gemini-pro-vision, gpt-3.5-turbo, or claude-2* families."
        )
    if "pinned provider" in reason_lower or "pinned path" in reason_lower or "pinned http" in reason_lower:
        hints.append(
            "Honor the PINNED PROVIDER OPERATION block exactly. Use that host, path, and HTTP method. "
            "Do not substitute a generic vendor default host (for example api.unipile.com instead of "
            "the pinned DSN/host)."
        )
    if "correlationkey" in reason_lower or "filterkey" in reason_lower or "pinned persistence" in reason_lower:
        hints.append(
            "Honor the PINNED PERSISTENCE LINK block exactly. Writer and reader MUST use the SAME "
            "Mongo field name (correlationKey/filterKey). Do not persist jd_id while the graph "
            "reads candidates_batch_id, or the reverse."
        )
    if "get_sync_collection" in reason_lower or "upsert_one_sync" in reason_lower or "upsert=true" in reason_lower or "async database helper" in reason_lower or "get_collection" in reason_lower:
        hints.append(
            "Do not call Database.get_sync_collection, Database.get_collection, or async "
            "Database.insert_one/update_one/upsert_one/find_one/delete_one. "
            "From this sync _run body, persist with Database.upsert_one_sync(collection, filters, "
            "plain_field_dict). Do not wrap updates in {\"$set\": ...} and do not use await."
        )
    if "mock" in reason_lower or "placeholder" in reason_lower or "simulation" in reason_lower:
        hints.append(
            "Replace mock/placeholder logic with real calls using request_meta credentials "
            "and installed libraries — never fabricated outputs or capability_gap stubs."
        )
    if not hints:
        return ""
    return "Additional fix guidance:\n" + "\n".join(f"  - {h}" for h in hints) + "\n\n"


def _gap_tool_imported_symbol_names(import_stmt: str) -> list[str]:
    """Names bound in function_code scope by a single import statement."""
    stmt = import_stmt.strip()
    if not stmt:
        return []
    m = re.match(r"^from\s+\S+\s+import\s+(.+)$", stmt)
    if m:
        names: list[str] = []
        for piece in m.group(1).replace("(", "").replace(")", "").split(","):
            piece = piece.strip()
            if not piece:
                continue
            if " as " in piece:
                names.append(piece.rsplit(" as ", 1)[-1].strip())
            else:
                names.append(piece.split(".")[0].strip())
        return names
    m = re.match(r"^import\s+(.+)$", stmt)
    if m:
        names = []
        for piece in m.group(1).split(","):
            piece = piece.strip()
            if not piece:
                continue
            if " as " in piece:
                names.append(piece.rsplit(" as ", 1)[-1].strip())
            else:
                names.append(piece.split(".")[0].strip())
        return names
    return []


def _ensure_gap_tool_packages(
    imports: Any,
    function_code: str = "",
) -> dict:
    """
    Auto-install missing third-party packages for gap tools (same as UI sandbox functions).

    Uses services.package_installer — skips stdlib / pre-installed libs, pip-installs the rest.
    """
    try:
        from services.package_installer import get_package_installer

        installer = get_package_installer()
        stmts = imports if isinstance(imports, list) else []
        result = installer.ensure_packages_for_imports(stmts, extra_source=function_code or "")
        if result.get("installed"):
            logger.info("[gap-tool] auto-installed packages: %s", result["installed"])
        if result.get("failed"):
            logger.warning("[gap-tool] auto-install failed for: %s", result["failed"])
        return result
    except Exception as exc:
        logger.warning("[gap-tool] package auto-install error: %s", exc)
        return {"installed": [], "failed": [], "error": str(exc)}


def _validate_gap_tool_imports(function_code: str, imports: Any) -> Optional[str]:
    """
    Reject when an import fails and function_code references a symbol from that import.

    Missing packages are auto-installed first (same behavior as UI-screen sandbox functions).
    Only reject after install attempt if the import still fails.
    """
    if not isinstance(imports, list):
        return None

    _ensure_gap_tool_packages(imports, function_code)

    for import_stmt in imports:
        stmt = str(import_stmt).strip()
        if not stmt:
            continue
        bound_names = _gap_tool_imported_symbol_names(stmt)
        if not bound_names:
            continue
        exec_globals: dict = {}
        try:
            exec(stmt, exec_globals)
        except Exception as exc:
            # One more install attempt targeted at this statement, then retry once.
            _ensure_gap_tool_packages([stmt], function_code)
            try:
                exec(stmt, exec_globals)
            except Exception as retry_exc:
                for name in bound_names:
                    if re.search(rf"\b{re.escape(name)}\b", function_code):
                        return (
                            f"import failed for '{stmt}' after auto-install: {retry_exc}; "
                            f"function_code references '{name}' — use an alternative API or remove the import"
                        )
    return None


def _build_gap_tool_checklist_block(io_contract: Dict[str, Any]) -> str:
    """Task-specific checklist from io_contract (not vendor-specific)."""
    inp = [
        str(p.get("name"))
        for p in (io_contract.get("inputParameters") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    out = [
        str(p.get("name"))
        for p in (io_contract.get("outputParameters") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    lines = [
        "PRE-SUBMISSION CHECKLIST (complete ALL before you respond — avoids automatic retry):",
        "1. function_code is ONLY the _run body (no `def _run`, no class, no import lines in function_code).",
        "2. Every import in the imports array must be valid Python. Missing third-party packages "
        "are auto-installed at validation/runtime (same as UI sandbox functions) — prefer real "
        "libraries (e.g. reportlab, Pillow) over inventing alternate APIs just to avoid imports.",
        "3. No bare `pass`, TODO, NotImplementedError, mock data, or except: pass stubs.",
        "4. return {{...}} must be a dict whose keys are EXACTLY the outputParameters names for this task.",
    ]
    if inp:
        lines.append(f"5. parameters must include these input names: {', '.join(inp)}.")
    if out:
        lines.append(
            f"6. outputParameters AND every return dict must use ONLY these contract keys "
            f"(plus optional error/code/retryable on failure): {', '.join(out)}."
        )
    lines.extend(
        [
            "7. Secrets only from request_meta['credentials'] using exact configured credential names.",
            "8. If function_code embeds JSON in an f-string, outer delimiter must be f'''...'''.",
            "9. Mentally compile-check: the snippet must be valid Python when indented under def _run.",
        ]
    )
    return "\n".join(lines) + "\n\n"


def _build_gap_tool_common_pitfalls_block() -> str:
    """Runtime-tested pitfalls that commonly cause retry (provider-neutral where possible)."""
    return """
COMMON PITFALLS THAT CAUSE AUTOMATIC REJECTION (avoid these):
- Putting import statements inside function_code — imports belong ONLY in the imports array.
- Broken imports: if `from some.module import Symbol` fails AFTER auto-install, do not use Symbol.
  Missing packages (reportlab, Pillow, etc.) are installed automatically — do not abandon a library
  solely because it was not pre-installed in the environment.
- google.generativeai multimodal: use image dicts {"mime_type": "image/jpeg", "data": image_bytes}
  in generate_content([image_dict, prompt_text]). Do NOT import Part from google.generativeai.types.
- Invalid JSON in your outer response: function_code must be one escaped string with \\n for newlines.
- Using pass/TODO/placeholder instead of real logic — write the full implementation in one shot.
- Returning output keys not listed in this task's outputParameters.
- Using await (tools are synchronous).
- Calling Database.get_sync_collection or raw coll.update_one(..., upsert=True) — use Database.upsert_one_sync.
- Deprecated LLM model ids (gemini-2.5-*, gemini-pro-vision, gpt-3.5-turbo, claude-2*, etc.).

"""


def _validate_gap_tool_deprecated_models(function_code: str) -> Optional[str]:
    """Reject function_code that references known deprecated API model ids."""
    for pattern, description in _GAP_TOOL_DEPRECATED_MODEL_PATTERNS:
        if re.search(pattern, function_code, re.IGNORECASE):
            return description
    return None


def _build_gap_tool_implementation_strategy_block(
    available_credentials: Optional[List[Dict[str, Any]]],
) -> str:
    """
    Provider-agnostic guidance: LLM picks stack from task + configured credentials.
    """
    lines = [
        "IMPLEMENTATION STRATEGY (derive from task + io_contract + credentials — do NOT hardcode vendors):",
        "- Implement exactly what the task description and io_contract require; no unrelated logic.",
        "- Secrets/API keys: ONLY via request_meta['credentials'] using exact names from AVAILABLE PROJECT CREDENTIALS.",
        "- Hosted LLM / vision / chat: pick the SDK or REST client that matches a configured credential",
        "  (Gemini → google.generativeai, OpenAI → openai, Claude → anthropic, etc.).",
        "  Use model id strings from the MODEL RESEARCH section (from Google Search) — never guess legacy defaults.",
        "- External HTTP microservices: requests or httpx with base URL/auth from credentials or explicit io_contract fields.",
        "- OCR / image text extraction when no suitable LLM/vision credential exists: fall back to pytesseract + PIL (installed).",
        "- File/binary inputs: decode or read real bytes (e.g. base64.b64decode); never pretend to process opaque IDs without a resolver.",
        "- NEVER return capability_gap / stub code because a credential is missing from AVAILABLE PROJECT CREDENTIALS.",
        "  Always write the real implementation. At runtime, if creds.get('NAME') is missing, return",
        "  {\"error\": \"Missing credential 'NAME'. Ask admin to configure it.\"} (and fill all outputParameters keys).",
        "- If the task has OPTIONAL integration paths (e.g. PDF upload OR email OR shared folder), implement ONLY",
        "  the path(s) described in the task / io_contract — do not require credentials for unused options.",
        "- Always return a dict whose keys match outputParameters; use clear error or status fields on failure paths.",
    ]
    if available_credentials:
        cred_names = sorted(
            {str(c["name"]) for c in available_credentials if c.get("name")}
        )
        if cred_names:
            lines.append(
                "- Configured credential names (use these exact keys only): "
                + ", ".join(cred_names)
            )
    else:
        lines.append(
            "- No project credentials are listed yet: still write real code that reads "
            "request_meta['credentials'] by the exact secret name the task needs "
            "(e.g. GEMINI_API_KEY). If absent at runtime, return Missing credential error — "
            "do NOT emit capability_gap stubs."
        )
    return "\n".join(lines) + "\n\n"


def _pin_gap_tool_output_parameters(
    parsed: Dict[str, Any],
    io_contract: Dict[str, Any],
) -> None:
    """Issue 9: io_contract output names/types win. LLM may only fill descriptions."""
    _pin_gap_tool_output_parameters_raw(
        parsed, io_contract, normalize=normalize_output_parameters
    )


async def _targeted_fix_gap_tool_output_contract(
    candidate: Dict[str, Any],
    io_contract: Dict[str, Any],
    *,
    extra_names: List[str],
    missing_names: List[str],
    tool_key: str,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    node_id: Optional[str] = None,
    attempt: int = 1,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """LLM rewrite of function_code to the real io_contract outputs only."""
    prompt = _build_gap_tool_output_contract_fix_prompt(
        tool_key=tool_key,
        io_contract=io_contract,
        function_code=str(candidate.get("function_code") or ""),
        imports=list(candidate.get("imports") or []),
        extra_names=extra_names,
        missing_names=missing_names,
        normalize=normalize_output_parameters,
    )
    started_at = datetime.utcnow()
    try:
        response_text, usage = await async_gap_tool_llm_call(prompt)
    except Exception as exc:
        return None, f"output-contract targeted fix LLM failed: {exc}"
    finished_at = datetime.utcnow()
    prompt_tokens = usage.get("prompt_tokens") or (len(prompt) // 4)
    completion_tokens = usage.get("completion_tokens") or (len(response_text) // 4)
    record_llm_call(
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        operation="graph-planning",
        sub_operation="gap-tool-output-contract-fix",
        model=GAP_TOOL_GENERATION_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        status="success",
        started_at=started_at,
        finished_at=finished_at,
        metadata={
            "node_id": node_id,
            "tool_key": tool_key,
            "attempt": attempt,
            "extra_names": extra_names,
            "missing_names": missing_names,
        },
    )
    fixed, parse_err = _parse_gap_tool_llm_response(response_text)
    if fixed is None:
        return None, parse_err or "output-contract targeted fix returned no JSON"
    updated, apply_err = apply_gap_output_contract_fix(
        candidate,
        io_contract,
        fixed,
        normalize=normalize_output_parameters,
    )
    if updated is None:
        return None, apply_err
    _, extra_after, _ = _classify_gap_output_contract(
        normalize_output_parameters(fixed.get("outputParameters") or []),
        normalize_output_parameters(io_contract.get("outputParameters") or []),
    )
    if extra_after:
        logger.warning(
            "[gap-tool] tool_key=%s targeted fix still had extra outputs %s — pinned to contract",
            tool_key,
            extra_after,
        )
    return updated, None


async def _targeted_fix_gap_tool_syntax(
    candidate: Dict[str, Any],
    syntax_error: str,
    *,
    tool_key: str,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    node_id: Optional[str] = None,
    attempt: int = 1,
    model: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """LLM patch of function_code for a known syntax error (does not regenerate)."""
    fixer_model = model or GAP_TOOL_FIX_MODEL
    prompt = build_gap_tool_syntax_fix_prompt(
        tool_key=tool_key,
        function_code=str(candidate.get("function_code") or ""),
        imports=list(candidate.get("imports") or []),
        syntax_error=syntax_error,
    )
    started_at = datetime.utcnow()
    try:
        response_text, usage = await async_gap_tool_llm_call(
            prompt,
            model=fixer_model,
            temperature=0.1,
            max_output_tokens=8192,
        )
    except BillingError:
        raise
    except Exception as exc:
        return None, f"syntax targeted fix LLM failed: {exc}"
    finished_at = datetime.utcnow()
    prompt_tokens = usage.get("prompt_tokens") or (len(prompt) // 4)
    completion_tokens = usage.get("completion_tokens") or (len(response_text) // 4)
    record_llm_call(
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        operation="graph-planning",
        sub_operation="gap-tool-syntax-fix",
        model=fixer_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        status="success",
        started_at=started_at,
        finished_at=finished_at,
        metadata={
            "node_id": node_id,
            "tool_key": tool_key,
            "attempt": attempt,
            "syntax_error": (syntax_error or "")[:500],
        },
    )
    fixed, parse_err = _parse_gap_tool_llm_response(response_text)
    if fixed is None:
        return None, parse_err or "syntax targeted fix returned no JSON"
    updated, apply_err = apply_gap_syntax_fix(candidate, fixed)
    if updated is None:
        return None, apply_err
    still = gap_tool_function_code_syntax_error(str(updated.get("function_code") or ""))
    if still:
        return None, still
    return updated, None


async def _repair_gap_tool_syntax_candidate(
    validation_candidate: Dict[str, Any],
    compile_reason: Optional[str],
    *,
    tool_key: str,
    attempt_no: int,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Heuristic empty-suite fill, then dedicated syntax-fixer LLM. Full regen is last resort."""
    if compile_reason is None or not is_gap_tool_syntax_rejection(compile_reason):
        return validation_candidate, compile_reason

    fc = str(validation_candidate.get("function_code") or "")
    heur = heuristic_fix_gap_tool_syntax(fc)
    if heur:
        validation_candidate = copy.deepcopy(validation_candidate)
        validation_candidate["function_code"] = heur
        logger.info(
            "[gap-tool] tool_key=%s attempt=%s heuristic syntax repair applied",
            tool_key,
            attempt_no,
        )
        compile_reason = _validate_gap_tool_executable(validation_candidate)
        if compile_reason is None or not is_gap_tool_syntax_rejection(compile_reason):
            return validation_candidate, compile_reason

    logger.warning(
        "[gap-tool] tool_key=%s attempt=%s syntax error — applying dedicated fixer model=%s: %s",
        tool_key,
        attempt_no,
        GAP_TOOL_FIX_MODEL,
        (compile_reason or "")[:300],
    )
    fixed, fix_err = await _targeted_fix_gap_tool_syntax(
        validation_candidate,
        compile_reason or "Python syntax error in function_code",
        tool_key=tool_key,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        node_id=node_id,
        attempt=attempt_no,
        model=GAP_TOOL_FIX_MODEL,
    )
    if fixed is None:
        logger.warning(
            "[gap-tool] tool_key=%s attempt=%s dedicated syntax fixer failed: %s",
            tool_key,
            attempt_no,
            fix_err,
        )
        return validation_candidate, compile_reason
    validation_candidate = fixed
    compile_reason = _validate_gap_tool_executable(validation_candidate)
    return validation_candidate, compile_reason


def _normalize_gap_tool_parsed(parsed: Dict[str, Any], io_contract: Dict[str, Any]) -> None:
    """Normalize imports, outputParameters, and contract parameters on a gap-tool dict in place."""
    raw_function_code = str(parsed.get("function_code") or "")
    raw_imports = parsed.get("imports") or []
    if not isinstance(raw_imports, list):
        raw_imports = []

    lines = raw_function_code.splitlines()
    extracted: list[str] = []
    kept: list[str] = []
    seen_non_import = False
    for line in lines:
        stripped = line.strip()
        if not stripped and not seen_non_import:
            kept.append(line)
            continue
        is_import = bool(re.match(r"^(import\s+\S+|from\s+\S+\s+import\s+.+)\s*$", stripped))
        if not seen_non_import and is_import:
            extracted.append(stripped)
            continue
        seen_non_import = True
        kept.append(line)
    cleaned_function_code = "\n".join(kept).strip("\n")
    if extracted:
        merged = [str(x).strip() for x in (raw_imports + extracted) if str(x).strip()]
        seen: set[str] = set()
        deduped: list[str] = []
        for imp in merged:
            if imp in seen:
                continue
            seen.add(imp)
            deduped.append(imp)
        parsed["imports"] = deduped
        parsed["function_code"] = cleaned_function_code

    _pin_gap_tool_output_parameters(parsed, io_contract)

    type_map = {
        "text": "string",
        "string": "string",
        "number": "float",
        "integer": "integer",
        "float": "float",
        "boolean": "boolean",
        "bool": "boolean",
        "object": "dict",
        "dict": "dict",
        "array": "list",
        "list": "list",
        "any": "any",
    }
    contract_inputs = io_contract.get("inputParameters")
    if isinstance(contract_inputs, list):
        contract_tool_params: list[dict] = []
        for p in contract_inputs:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            dt = str(p.get("dataType") or "Any").strip().lower()
            contract_tool_params.append(
                {
                    "name": str(p["name"]),
                    "param_type": type_map.get(dt, "any"),
                    "description": str(p.get("description") or ""),
                    "required": coerce_required_flag(p.get("required"), default=False),
                    "default_value": None,
                }
            )
        if contract_tool_params:
            parsed["parameters"] = contract_tool_params


def _compile_gap_tool_like_executor(api_input: Dict[str, Any]) -> None:
    """
    Compile-check gap tool code the same way langgraph/app/executor.py LocalToolRegistry does.
    Raises on syntax/compile errors.
    """
    tool_name = str(api_input.get("name") or "gap_tool")
    description = str(api_input.get("description") or "")
    parameters = api_input.get("parameters") or []
    imports = api_input.get("imports") or []
    function_code = str(api_input.get("function_code") or "")

    exec_globals: Dict[str, Any] = {
        "BaseTool": BaseTool,
        "ClassVar": ClassVar,
        "List": List,
        "Any": Any,
        "__builtins__": __builtins__,
    }
    if imports:
        _ensure_gap_tool_packages(imports, function_code)
        for import_stmt in imports:
            stmt = str(import_stmt).strip()
            if not stmt:
                continue
            try:
                exec(stmt, exec_globals)
            except ImportError as e:
                _ensure_gap_tool_packages([stmt], function_code)
                try:
                    exec(stmt, exec_globals)
                except ImportError as retry_e:
                    print(f"Warning: Could not import {stmt} after auto-install: {retry_e}")

    class_name = re.sub(r"\W|^(?=\d)", "_", tool_name)
    safe_name = json.dumps(tool_name)
    safe_desc = json.dumps(description)

    class_code = f"""
class {class_name}(BaseTool):
    name: str = {safe_name}
    description: str = {safe_desc}
"""
    keywords = api_input.get("keywords")
    if keywords:
        class_code += f"    keywords: ClassVar[List[str]] = {keywords}\n"

    class_code += "\n    def _run(self"
    from services.tool_param_utils import format_tool_run_params

    class_code += format_tool_run_params(parameters)
    class_code += ", request_meta: Any = None) -> Any:\n"
    class_code += "        if request_meta is None:\n"
    class_code += "            request_meta = {}\n"

    clean_code = function_code.replace("\r\n", "\n").replace("\t", "    ")
    clean_code = textwrap.dedent(clean_code).strip()
    raw_lines = clean_code.split("\n")
    healed_lines: list[str] = []
    double_quote = chr(34)
    single_quote = chr(39)
    for line in raw_lines:
        stripped = line.strip()
        is_dangling = stripped in (
            double_quote,
            single_quote,
            double_quote + ",",
            single_quote + ",",
        )
        if is_dangling and healed_lines:
            healed_lines[-1] += stripped
        else:
            healed_lines.append(line)
    for line in healed_lines:
        class_code += f"        {line}\n"

    exec(textwrap.dedent(class_code), exec_globals)


async def _write_back_graph_schema(
    function_code: str,
    collection_store_map: Optional[Dict[str, str]],
    *,
    org_id: Optional[str],
    subscriber_id: Optional[str],
) -> None:
    """
    Best-effort Phase-2 write-back for tools: if function_code used one of the tenant's
    known arcadedb collection names as a Cypher vertex label, persist whatever properties
    it referenced so the NEXT tool/function touching that collection gets them as a hint.
    """
    if not org_id or not subscriber_id or not collection_store_map or not function_code:
        return
    try:
        from repositories.arcadedb_repository import EnterpriseGraphRepository
        for coll, store in collection_store_map.items():
            if store != "arcadedb":
                continue
            if not re.search(rf":\s*{re.escape(coll)}\b", function_code, re.IGNORECASE):
                continue
            props: set = set()
            for m in re.finditer(r"\(\s*\w+\s*:\s*\w+\s*\{([^}]*)\}", function_code):
                for prop_match in re.finditer(r"(\w+)\s*:\s*\$", m.group(1)):
                    props.add(prop_match.group(1))
            for m in re.finditer(r"\.(\w+)\s*=\s*\$", function_code):
                props.add(m.group(1))
            if props:
                fields = [{"name": p, "type": "string"} for p in sorted(props)]
                await EnterpriseGraphRepository().upsert_schema(
                    subscriber_id, org_id, coll, fields, target_store="arcadedb"
                )
    except Exception as exc:
        logger.warning("[gap-tool] graph schema write-back failed: %s", exc, exc_info=True)


def _find_graph_topology_json_blob_violation(function_code: str) -> Optional[str]:
    """
    Deterministic backstop: reject function_code that serializes a node/edge list into a
    JSON string property instead of creating real ArcadeDB vertices/relationships — an LLM
    instruction alone isn't reliable enough (observed in production: json.dumps(formatted_nodes)
    written straight into a MERGE property).
    """
    if not function_code or "run_arcadedb_command" not in function_code:
        return None
    match = re.search(r"json\.dumps\(\s*([a-zA-Z_][a-zA-Z0-9_]*)", function_code)
    if match and re.search(r"node|edge", match.group(1), re.IGNORECASE):
        return (
            f"function_code calls json.dumps({match.group(1)}) and writes it into an ArcadeDB "
            f"property. Serializing a node/edge list into a JSON string property defeats the "
            f"purpose of a graph database. You MUST loop over the list and create one real "
            f"vertex per node (MERGE) and one real relationship per edge (MATCH + MERGE) instead."
        )
    return None


def _find_collection_store_violation(
    function_code: str, collection_store_map: Optional[Dict[str, str]]
) -> Optional[str]:
    """
    Deterministic backstop for the COLLECTION -> DATA STORE table in the prompt:
    an LLM instruction is advisory, not enforced, so this rejects (and forces a
    retry of) any function_code that calls Database.get_sync_collection(...) on
    a collection name authoritatively classified as "arcadedb".
    """
    if not collection_store_map or not function_code:
        return None
    arcadedb_collections = {
        name for name, store in collection_store_map.items() if store == "arcadedb"
    }
    if not arcadedb_collections:
        return None
    for match in re.finditer(r"get_sync_collection\(\s*[\"']([^\"']+)[\"']", function_code):
        coll = match.group(1)
        if coll in arcadedb_collections:
            return (
                f"'{coll}' is classified as arcadedb but function_code called "
                f"Database.get_sync_collection('{coll}'). You MUST use "
                f"Database.run_arcadedb_command_sync(...) for '{coll}' instead — "
                f"see the COLLECTION -> DATA STORE table."
            )
    return None


def _validate_gap_tool_executable(api_input: Dict[str, Any]) -> Optional[str]:
    """
    Quick pre-save validation so gap tools are ready for LangGraph executor registration.
    Returns a human-readable rejection reason, or None when compile checks pass.
    """
    function_code = str(api_input.get("function_code") or "").strip()
    if not function_code:
        return "empty function_code"

    if _function_code_defines_tool_shell(function_code):
        return "function_code must not define _run or a class (helper def functions inside the body are OK)"

    mock_reason = _detect_mock_code(function_code)
    if mock_reason:
        return mock_reason

    import_reason = _validate_gap_tool_imports(function_code, api_input.get("imports"))
    if import_reason:
        return import_reason

    deprecated_reason = _validate_gap_tool_deprecated_models(function_code)
    if deprecated_reason:
        return deprecated_reason

    clean_code = textwrap.dedent(
        function_code.replace("\r\n", "\n").replace("\t", "    ")
    ).strip()
    syntax_err = gap_tool_function_code_syntax_error(clean_code)
    if syntax_err:
        return syntax_err

    try:
        _compile_gap_tool_like_executor(api_input)
    except Exception as exc:
        return f"LangGraph executor compile check failed: {exc}"

    try:
        tool_registry = _get_tool_registry()
        tool_def = tool_registry._parse_api_input(api_input)
        tool_registry.create_dynamic_tool_class(tool_def)
    except Exception as exc:
        return f"DynamicToolRegistry compile check failed: {exc}"

    return None


def _build_gap_tool_prompt(
    node: Any,
    gen: Any,
    tool_key: str,
    io_contract: Dict[str, Any],
    attempt: int,
    previous_rejection_reason: Optional[str] = None,
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    model_research_context: Optional[str] = None,
    existing_function_code: Optional[str] = None,
    collection_store_map: Optional[Dict[str, str]] = None,
    collection_field_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    provider_operations: Optional[List[Any]] = None,
    persistence_links: Optional[List[Any]] = None,
) -> str:
    sample_out = {
        "name": "ToolRegistryKey",
        "description": "What the tool does in one sentence.",
        "parameters": [
            {
                "name": "example_arg",
                "param_type": "Any",
                "description": "meaning of the input arg",
                "required": True,
            }
        ],
        "outputParameters": [
            {
                "name": "result_value",
                "dataType": "Any",
                "description": "what the tool returns",
            }
        ],
        "keywords": ["gap_generated"],
        "function_code": "return {\"result_value\": example_arg}",
        "imports": ["from typing import Any"],
    }

    rejection_block = ""
    if attempt > 0 and previous_rejection_reason:
        extra_hint = _gap_tool_rejection_extra_hint(previous_rejection_reason)
        rejection_block = f"""
⛔ ATTEMPT {attempt} WAS REJECTED — READ THIS BEFORE WRITING CODE ⛔

Your previous function_code was rejected because it contained: [{previous_rejection_reason}].

You MUST fix this now. Rewrite function_code with ZERO simulation, mock, or placeholder logic.

{extra_hint}Concrete guidance for this specific task:
- If the task involves processing a file by ID: require the caller to pass raw bytes or a base64 string instead of an opaque ID, and use a real parsing library (pypdf, pdfplumber, python-docx, Pillow, etc.) on the actual bytes.
- If the task requires an external API: call it for real using `requests` or the SDK. Read API keys from request_meta["credentials"], NEVER from os.environ or hardcoded strings.
- NEVER emit capability_gap stubs. If a credential is missing at runtime, return Missing credential 'NAME' error (with all output keys). Implement optional paths only when selected in the task.

"""

    # Build credential context block (names/types only — never include secret values)
    credential_context_block = ""
    if available_credentials:
        cred_list = [
            {"name": c.get("name"), "type": c.get("type"), "service": c.get("service")}
            for c in available_credentials
            if c.get("name")
        ]
        credential_context_block = f"""
AVAILABLE PROJECT CREDENTIALS (use the exact 'name' when reading from request_meta):
{json.dumps(cred_list, indent=2)}

To use a credential in function_code:
  creds = (request_meta or {{}}).get("credentials") or {{}}
  api_key = creds.get("exact_credential_name_from_list_above")
  if not api_key:
      return {{"error": "Missing credential 'exact_credential_name'. Ask admin to configure it."}}

"""
    else:
        credential_context_block = """
NO PROJECT CREDENTIALS are currently configured. If this tool requires an external API key or
secret, read it from request_meta["credentials"] using the exact credential name configured for
this project (do not invent os.environ reads). Example:
  creds = (request_meta or {}).get("credentials") or {}
  api_key = creds.get("<credential_name_required_by_your_implementation>")
  if not api_key:
      return {"error": "Missing credential '<credential_name_required_by_your_implementation>'. Ask admin to configure it."}

"""

    # MongoDB context: this backend already provides a configured Mongo client via services.database.Database.
    #
    # Why we include this explicitly:
    # - The gap-tool generator is prohibited from reading secrets from os.environ.
    # - Our Mongo connection is configured outside tool code (env/config.json) and is accessed via Database.
    # Without this guidance, the LLM often returns a "capability_gap" claiming missing Mongo URI/DB details.
    mongodb_context_block = """
MONGODB IS ALREADY AVAILABLE IN THIS BACKEND (do NOT return capability_gap for missing URI/DB):
- Dynamic tools implement LangChain `BaseTool._run`, which is SYNCHRONOUS.
  Therefore: DO NOT use `await` anywhere in function_code.
- ❌ NEVER call `Database.get_sync_collection(...)`. That returns a synchronous pymongo
  handle and fails in the async sandbox / event loop (Issue 10).
- ❌ NEVER call raw `coll.update_one(..., {"$set": doc}, upsert=True)` on a collection handle.
- Put `from services.database import Database` in the JSON "imports" array (NOT inside function_code).
- Database connection details (URI + DB name) are configured by the platform (env/config.json) and are NOT
  supposed to be read directly inside tool code.
- IMPORTANT: You MAY import and use Database even though it reads env internally; that is the supported pattern.
- Database.update_one_sync is NOT upsert (zero matched = silent no-write).
- For insert-or-update from this sync `_run` body, call:
    res = Database.upsert_one_sync("<collection>", filters, updates)
  Pass a PLAIN field dict for `updates` — the helper adds $set. Do NOT wrap with {"$set": ...}.
  Do NOT call Database.upsert_one / insert_one / update_one / find_one / delete_one (those are
  async and return a coroutine here). Do NOT mention or copy `await` from UI/sandbox examples.
- Other sync helpers for this `_run` body (never await them): insert_one_sync, find_one_sync,
  update_one_sync, delete_one_sync.

MongoDB example patterns (adapt to the IO contract; do not invent fields):
1) Insert:
  res = Database.insert_one_sync("users", {"username": username, "email": email, "password_hash": password_hash})
  return {"inserted_id": res["inserted_id"]}

2) Upsert:
  filters = {"user_id": user_id}
  updates = {"aadhaar_last4": aadhaar_last4, "kyc_payload": kyc_payload, "verified": True}
  res = Database.upsert_one_sync("ekyc_details", filters, updates)
  return {"matched": int(res["matched"]), "modified": int(res["modified"]), "upserted_id": res.get("upserted_id")}
"""

    arcadedb_context_block = """
ARCADEDB IS ALSO ALREADY AVAILABLE (see COLLECTION → DATA STORE table below for which named
collections need it):
- Same SYNCHRONOUS rule applies: DO NOT use `await` or any asyncio API.
- Use `Database.run_arcadedb_command_sync(command, params, language="cypher")` — an httpx-based call
  against ArcadeDB's HTTP REST API (POST /api/v1/command/{database}), the SAME protocol this platform
  already uses to save graph metadata — never write raw psycopg/Postgres-wire-protocol code.
- Put `from services.database import Database` in the JSON "imports" array (same import as MongoDB).
- NEVER pass database= to run_arcadedb_command_sync — subscriber routing is automatic.
- Use MERGE for idempotent create, MATCH for reads, SET for updates, DETACH DELETE for removals.
- A single tool MAY use BOTH Database.get_sync_collection AND Database.run_arcadedb_command_sync in the
  same function_code when the task needs both plain document lookups and graph traversal — this is
  expected, not a mistake.
- CRITICAL — NEVER serialize a list of nodes/edges into a JSON string property on a single vertex
  (e.g. NEVER do a MERGE that sets a property like nodes_json = json.dumps(nodes)). That stores a
  document inside a graph database and defeats the entire purpose of using ArcadeDB. If the task
  involves a whole topology (a list of node records plus a list of edge records), create one real
  vertex per node record and one real relationship per edge record, looping over each list.
- ArcadeDB's Cypher does not support a dynamic/parameterized relationship type in MERGE — use one
  fixed relationship type (e.g. :CONNECTS_TO) and store the specific edge id/type as a property on
  that relationship, rather than binding the type itself to a variable.

ArcadeDB example pattern (single entity):
  command = "MERGE (n:Employee {employee_id: $employee_id}) SET n.name = $name RETURN n"
  rows = Database.run_arcadedb_command_sync(command, {"employee_id": employee_id, "name": name}, language="cypher")
  return {"result": rows}

ArcadeDB example pattern (a whole topology — list of nodes + list of edges — one real vertex per
node, one real relationship per edge, NOT a JSON blob):
  for node in graph_nodes:
      Database.run_arcadedb_command_sync(
          "MERGE (n:GraphNode {node_id: $node_id}) SET n.node_label = $node_label",
          {"node_id": node.get("node_id"), "node_label": node.get("node_label", "")},
          language="cypher",
      )
  for edge in graph_edges:
      Database.run_arcadedb_command_sync(
          "MATCH (a:GraphNode {node_id: $source_node_id}), (b:GraphNode {node_id: $target_node_id}) "
          "MERGE (a)-[r:CONNECTS_TO {edge_id: $edge_id}]->(b)",
          {"edge_id": edge.get("edge_id"), "source_node_id": edge.get("source_node_id"),
           "target_node_id": edge.get("target_node_id")},
          language="cypher",
      )
  return {"result": "ok"}
"""

    if collection_store_map:
        rows = "\n".join(
            f'  - "{coll}" -> {store} '
            f'({"Database.run_arcadedb_command_sync (Cypher)" if store == "arcadedb" else "Database.get_sync_collection"})'
            + (
                f" — ALREADY HAS FIELDS, reuse these EXACT names: "
                + ", ".join(f['name'] for f in (collection_field_map or {}).get(coll, []))
                if store == "arcadedb" and (collection_field_map or {}).get(coll)
                else " — NEW graph collection, choose one clear vertex label and property set"
                if store == "arcadedb"
                else ""
            )
            for coll, store in sorted(collection_store_map.items())
        )
        collection_store_block = f"""
COLLECTION -> DATA STORE (AUTHORITATIVE — already decided, do NOT re-classify these):
{rows}
For any collection named above, you MUST use the listed access method for that collection specifically.
For any collection NOT listed above, default to "mongodb" / Database.get_sync_collection unless the task
is explicitly an N-hop graph traversal (who's connected across a hierarchy/network), in which case use
Database.run_arcadedb_command_sync for that collection.
"""
    else:
        collection_store_block = """
COLLECTION -> DATA STORE:
No authoritative classification available for this tenant yet. Default every collection to "mongodb" /
Database.get_sync_collection, UNLESS the task is explicitly an N-hop graph traversal (who's connected
across a hierarchy/network), in which case use Database.run_arcadedb_command_sync for that collection.
"""

    implementation_strategy_block = _build_gap_tool_implementation_strategy_block(
        available_credentials
    )
    model_research_block = _build_gap_tool_model_research_block(model_research_context or "")
    pinned_provider_block = ""
    pinned_text = format_pinned_provider_spec(
        operations_from_dicts(provider_operations) if provider_operations else []
    )
    if pinned_text:
        pinned_provider_block = f"""
{pinned_text}

CRITICAL: function_code MUST call the pinned endpoint host/path and HTTP method above.
Do NOT replace a pinned host with a generic vendor default.
"""
    pinned_persistence_block = ""
    persist_pin = format_pinned_persistence_spec(
        persistence_links or [],
        node_id=getattr(node, "nodeId", "") or "",
    )
    if persist_pin:
        pinned_persistence_block = f"""
{persist_pin}

CRITICAL: every Mongo document this tool writes MUST include the pinned correlationKey
field. Do NOT persist a different batch key (for example jd_id instead of candidates_batch_id).
"""

    library_info_section = """
IMPORTANT - Library Usage Strategy (pick what the TASK needs — examples are not prescriptions):
    PREFER stdlib when sufficient: json, re, hashlib, datetime, base64, difflib, etc.
    COMMON INSTALLED PACKAGES (use only if required by your chosen approach):
    - HTTP: requests, httpx
    - Images/OCR fallback: PIL (Pillow), pytesseract
    - LLM SDKs (match configured credential): google.generativeai, openai, anthropic
    - Documents: PyPDF2, pdfplumber, python-docx
    - Text similarity: fuzzywuzzy (or stdlib difflib)
    - Payments/other: stripe, pandas, numpy — only when the task truly needs them

    BEFORE adding an import: confirm it is required for this specific task and credential set.
    """
    checklist_block = _build_gap_tool_checklist_block(io_contract)
    pitfalls_block = _build_gap_tool_common_pitfalls_block()

    existing_function_code_block = ""
    if existing_function_code:
        existing_function_code_block = f"""
EXISTING TOOL FUNCTION_CODE (for context — you are extending this tool to handle NEW parameters):
{existing_function_code}

IMPORTANT: The IO contract above includes BOTH existing and new parameters. Your function_code MUST:
- Keep all existing logic intact
- Accept and properly use the new parameters listed in the IO contract
- Return all outputParameters as before
- Do NOT change the tool's fundamental behavior for existing parameters
"""

    prompt = f"""{rejection_block}Create a Python LangChain-compatible CUSTOM TOOL definition as JSON.
The tool's registry name (no spaces) MUST be EXACTLY:
  {tool_key}
Use that exact string for the JSON "name" field.

Task / node description:
{(gen.description or node.name or '').strip()}
{pinned_provider_block}
{pinned_persistence_block}
WORKFLOW IO CONTRACT — your tool MUST satisfy these parameter contracts exactly:
{json.dumps(io_contract, indent=2)}
{existing_function_code_block}
{credential_context_block}
{mongodb_context_block}
{collection_store_block}
{arcadedb_context_block}
{implementation_strategy_block}
{model_research_block}
{library_info_section}
{pitfalls_block}
{checklist_block}
Return JSON in the SAME shape as this example (replace content; do not add extra top-level keys):
{json.dumps(sample_out, indent=2)}

Example 1 — primality check (no credentials needed):
{{
  "name": "check_prime",
  "description": "Checks whether the given integer is a prime number.",
  "parameters": [
    {{"name": "n", "param_type": "Any", "description": "integer to test for primality", "required": true, "default_value": null}}
  ],
  "outputParameters": [
    {{"name": "is_prime", "dataType": "Boolean", "description": "True if n is prime, False otherwise"}}
  ],
  "keywords": ["prime", "mathematics"],
  "function_code": "if n <= 1:\\n    return {{\\\"is_prime\\\": False}}\\nfor i in range(2, int(math.sqrt(n)) + 1):\\n    if n % i == 0:\\n        return {{\\\"is_prime\\\": False}}\\nreturn {{\\\"is_prime\\\": True}}",
  "imports": ["import math", "from typing import Any"],
  "version": 1
}}

Example 2 — hosted LLM call (illustrative shape only — YOU must pick provider/SDK from AVAILABLE PROJECT CREDENTIALS):
{{
  "name": "translate_text",
  "description": "Translates text using whichever LLM credential the project provides.",
  "parameters": [
    {{"name": "text", "param_type": "str", "description": "text to translate", "required": true, "default_value": null}},
    {{"name": "target_language", "param_type": "str", "description": "target language", "required": true, "default_value": null}}
  ],
  "outputParameters": [
    {{"name": "translated_text", "dataType": "Text", "description": "translated string"}}
  ],
  "keywords": ["translation", "language"],
  "function_code": "creds = (request_meta or {{}}).get(\\"credentials\\") or {{}}\\napi_key = creds.get(\\"<use_exact_credential_name_from_list>\\")\\nif not api_key:\\n    return {{\\\"error\\\": \\"Missing credential '<use_exact_credential_name_from_list>'.\\"}}\\n# Call the provider SDK or REST API that matches that credential (Gemini/OpenAI/Claude/etc.)\\n# ... production call here ...\\nreturn {{\\\"translated_text\\\": <result_text>}}",
  "imports": ["from typing import Any"],
  "version": 1
}}

Example 3 — MongoDB CRUD (imports are separate; function_code has NO imports):
{{
  "name": "mongo_users_crud_example",
  "description": "Example CRUD patterns for a MongoDB 'users' collection using the platform Database helper.",
  "parameters": [
    {{"name": "op", "param_type": "str", "description": "one of: create|get|update|delete", "required": true, "default_value": null}},
    {{"name": "user_id", "param_type": "str", "description": "logical user id (used as filter key in this example)", "required": false, "default_value": null}},
    {{"name": "payload", "param_type": "dict", "description": "document fields for create/update", "required": false, "default_value": null}}
  ],
  "outputParameters": [
    {{"name": "result", "dataType": "Any", "description": "operation-specific result payload"}}
  ],
  "keywords": ["mongodb", "crud", "example"],
  "function_code": "if op == \\"create\\":\\n    doc = payload or {{}}\\n    res = Database.insert_one_sync(\\"users\\", doc)\\n    return {{\\"result\\": {{\\"inserted_id\\": res.get(\\"inserted_id\\")}}}}\\nif op == \\"get\\":\\n    if not user_id:\\n        return {{\\"result\\": {{\\"error\\": \\"user_id is required for get\\"}}}}\\n    doc = Database.find_one_sync(\\"users\\", {{\\"user_id\\": user_id}})\\n    if not doc:\\n        return {{\\"result\\": None}}\\n    doc[\\"_id\\"] = str(doc.get(\\"_id\\"))\\n    return {{\\"result\\": doc}}\\nif op == \\"update\\":\\n    if not user_id:\\n        return {{\\"result\\": {{\\"error\\": \\"user_id is required for update\\"}}}}\\n    updates = payload or {{}}\\n    res = Database.upsert_one_sync(\\"users\\", {{\\"user_id\\": user_id}}, updates)\\n    return {{\\"result\\": {{\\"matched\\": int(res[\\"matched\\"]), \\"modified\\": int(res[\\"modified\\"]), \\"upserted_id\\": res.get(\\"upserted_id\\")}}}}\\nif op == \\"delete\\":\\n    if not user_id:\\n        return {{\\"result\\": {{\\"error\\": \\"user_id is required for delete\\"}}}}\\n    res = Database.delete_one_sync(\\"users\\", {{\\"user_id\\": user_id}})\\n    return {{\\"result\\": {{\\"deleted\\": int(res[\\"deleted\\"])}}}}\\nreturn {{\\"result\\": {{\\"error\\": \\"unknown op\\", \\"allowed\\": [\\"create\\", \\"get\\", \\"update\\", \\"delete\\"]}}}}",
  "imports": ["from typing import Any", "from services.database import Database"],
  "version": 1
}}

Example 4 — ArcadeDB graph traversal (only when the task needs N-hop relationships):
{{
  "name": "find_reports_n_levels",
  "description": "Finds all employees reporting up to N levels below a given manager.",
  "parameters": [
    {{"name": "manager_id", "param_type": "str", "description": "employee_id of the manager to start from", "required": true, "default_value": null}},
    {{"name": "levels", "param_type": "int", "description": "how many levels down to traverse", "required": true, "default_value": null}}
  ],
  "outputParameters": [
    {{"name": "result", "dataType": "Any", "description": "list of employee records found within the given levels"}}
  ],
  "keywords": ["arcadedb", "graph", "hierarchy"],
  "function_code": "command = \\"MATCH (m:Employee {{employee_id: $manager_id}})-[:REPORTS_TO*1..$levels]-(e:Employee) RETURN e\\"\\nrows = Database.run_arcadedb_command_sync(command, {{\\"manager_id\\": manager_id, \\"levels\\": levels}}, language=\\"cypher\\")\\nreturn {{\\"result\\": rows}}",
  "imports": ["from typing import Any", "from services.database import Database"],
  "version": 1
}}

RULES — all are mandatory, violations cause rejection and retry:

1. "function_code" is ONLY the body of _run (do NOT define `def _run` or a `class`). Helper `def` functions nested inside the body are allowed.
   Write every line at column 0 — the registry indents automatically.
   It MUST NOT contain any import statements (no "import x" and no "from x import y"). All imports belong in the "imports" array only.
2. The function MUST return a plain dict whose keys exactly match the names in "outputParameters". This is how downstream nodes bind to the tool's outputs.
3. Every parameter in the IO contract's "inputParameters" MUST appear in "parameters" with the exact same "name".
   Do NOT add extra parameters that are not in the IO contract (no invented user_id, org_id, etc.).
   Read identity/audit fields from request_meta if needed — they are injected at runtime, not bound by the graph.
4. "outputParameters" MUST be EXACTLY the io_contract output names — same names, no rename, no drop, no invented fields. Optional extra keys on failure only: error, code, retryable.
5. Use only safe standard-library logic unless a common import is truly necessary; keep code short and deterministic.
6. Include 'from typing import Any' in imports when using Any.
7. Respond with a single JSON object only (no markdown prose before/after). Required keys: name, description, parameters, outputParameters, function_code, imports.

8. ══ ABSOLUTE PROHIBITION ON MOCK / SIMULATION CODE ══
   The following are BANNED without exception — their presence causes automatic rejection:
   • Hardcoded fake values (invoice numbers, amounts, names, UUIDs, dates, coordinates, etc.)
   • Comments containing: "simulate", "mock", "placeholder", "demonstration", "in a real scenario",
     "assume successful", "for this example", "would involve", "would call"
   • Returning fabricated data structures that pretend to be real tool outputs
   • `raise NotImplementedError`, bare `pass` (any line that is only pass), TODO stubs
   • NEVER write `pass` — not even one line. Implement the full body immediately.
   • Any code whose correctness depends on inputs that are not in the io_contract
COMPLETE IMPLEMENTATION REQUIREMENTS:
    - For file upload: Actually save the file to provided path or return error if not possible    
    - For API calls: Actually make the HTTP request or return error about missing config or invalid request
    - For database: Actually query/insert/update or return error about missing collection or invalid query
    - For OCR/images/PDF: Actually process inputs (vision API if credentialed, else pytesseract/PIL, etc.)
    - NO "example" data, NO "mock" responses, NO "simulated" logic or placeholder code
    - Do NOT copy example provider names, models, or field names unless they appear in THIS task's io_contract

9. ══ MISSING CREDENTIALS → REAL CODE + RUNTIME ERROR (NEVER STUB) ══
   Do NOT return capability_gap, empty stubs, or "credentials not available" placeholders.
   Those are REJECTED automatically.

   Always write the real implementation. At runtime, if a required secret is absent:
        creds = (request_meta or {{}}).get("credentials") or {{}}
        api_key = creds.get("EXACT_CREDENTIAL_NAME")
        if not api_key:
            return {{
                "error": "Missing credential 'EXACT_CREDENTIAL_NAME'. Ask admin to configure it.",
                # also include every outputParameters key with safe empty defaults
            }}

   A non-empty string on the "error" key is a HARD runtime failure: the executor pauses
   the workflow and shows that message to the user (retry / fix-credentials). Do not put
   warnings or partial-success notes in "error". You may also set "code" (e.g.
   MISSING_CREDENTIAL) and "retryable": true. Do NOT wrap the payload in {{"ok": false, "data": ...}}
   — keep declared output keys at the top level plus "error".

   Optional integrations: if the task says the user chose PDF upload (and email/Drive are
   alternatives they did not select), implement ONLY PDF upload — do not require IMAP/Drive
   credentials for unused options.

10. For file/document processing: NEVER accept an opaque ID as the only input unless you can
    resolve it to bytes using a real service whose credentials are in the io_contract.
    Prefer accepting raw bytes (bytes param_type) or a base64 string, then use a real parser
    (pypdf, pdfplumber, python-docx, Pillow, etc.).

11. ══ CREDENTIAL & SECRET ACCESS ══
    • NEVER hardcode API keys, tokens, passwords, or any secret values in function_code.
    • NEVER read secrets from os.environ in function_code.
    • The _run method automatically receives a `request_meta` dict injected by the platform.
    • Read ALL secrets like this (use the exact credential name from the AVAILABLE PROJECT CREDENTIALS list above):
        creds = (request_meta or {{}}).get("credentials") or {{}}
        api_key = creds.get("exact_credential_name")
        if not api_key:
            return {{"error": "Missing credential 'exact_credential_name'. Ask admin to configure it."}}
    • `request_meta` is injected at runtime — do NOT add it to the "parameters" list in your JSON output.
      It is always available inside function_code as a local variable.

12. ══ F-STRINGS WITH EMBEDDED JSON ══
    When function_code uses an f-string whose literal portion contains JSON examples or other double-quoted text,
    ALWAYS delimit the outer f-string with triple single quotes: f'''...''' (never f\"\"\"...\"\"\" or f\"...\").
    This prevents quote collisions and SyntaxError when JSON keys/values use double quotes.
    Example (correct):
      prompt = f'''Analyze this payload and return JSON:
      {{"status": "ok", "items": {{items}}}}
      Input: {{user_text}}'''
    Example (wrong — will break parsing):
      prompt = f"Return {{\\"status\\": \\"ok\\"}} for {{user_text}}"

13. ══ NO HARDCODED VENDORS OR OUTPUT SHAPES ══
    Examples in this prompt are structural templates only. Your code MUST follow the user's task,
    io_contract field names, and AVAILABLE PROJECT CREDENTIALS — never blindly copy example SDKs,
    model IDs, credential names, or business logic from unrelated examples.

14. ══ FIRST-ATTEMPT QUALITY ══
    Your response is compile-checked immediately. Produce complete, runnable code on the first try.
    Do not submit a skeleton to be "filled in later". If unsure about a library symbol, use a pattern
    known to work (stdlib, requests, dict-based multimodal payloads) rather than experimental imports.

15. ══ MODEL IDS FROM RESEARCH ══
    Any GenerativeModel("..."), model=, or API model parameter in function_code MUST use a current model id
    from the MODEL RESEARCH section (Google Search). Known deprecated families are auto-rejected.

Remember: 100% production-ready code only. Missing credentials → runtime Missing credential error, NEVER a capability_gap stub.
"""
    return prompt


async def _generate_gap_tool_and_upsert_capability(
    node: Any,
    gen: Any,
    *,
    tool_key: str,
    io_contract: Dict[str, Any],
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    project_id: str,
    max_attempts: int = 3,
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    existing_function_code: Optional[str] = None,
) -> Dict[str, str]:
    """
    LLM-writes a dynamic tool, registers it in the in-process registry + JSON backing,
    and upserts a global tool:* capability document for search / codegen enrichment.

    Retries up to `max_attempts` times if function_code contains mock/placeholder patterns
    or fails pre-save compile validation (LangGraph executor + DynamicToolRegistry).
    Raises RuntimeError if all attempts fail.

    Returns dict with capabilityId, version, class_path, source (Issue 12 — pin on graph node).
    """
    parsed: Optional[Dict[str, Any]] = None
    last_rejection_reason: Optional[str] = None
    last_fixture_error: Optional[str] = None
    tool_description = (gen.description or node.name or "").strip()
    cfg = getattr(node, "config", None)
    try:
        from agent_new import _best_intent_description, _ensure_intent_description
        if cfg is not None:
            _ensure_intent_description(cfg)
            intent = _best_intent_description(
                getattr(cfg, "intentDescription", None) if not isinstance(cfg, dict) else cfg.get("intentDescription"),
                getattr(cfg, "generationDescription", None) if not isinstance(cfg, dict) else cfg.get("generationDescription"),
                getattr(gen, "description", None),
            )
            if intent:
                tool_description = intent
                if hasattr(gen, "description") and (gen.description or "").strip() != intent:
                    try:
                        gen.description = intent
                    except Exception:
                        pass
    except Exception:
        pass

    collection_store_map: Dict[str, str] = {}
    collection_field_map: Dict[str, List[Dict[str, Any]]] = {}
    if org_id and subscriber_id:
        try:
            from repositories.arcadedb_repository import EnterpriseGraphRepository
            repo = EnterpriseGraphRepository()
            known_collections = await repo.list_all_collections(subscriber_id, org_id)
            if known_collections:
                collection_store_map = await repo.get_target_stores_for_collections(
                    subscriber_id, org_id, known_collections
                )
                arcadedb_collections = [
                    c for c, store in collection_store_map.items() if store == "arcadedb"
                ]
                if arcadedb_collections:
                    collection_field_map = await repo.get_schemas_for_collections(
                        subscriber_id, org_id, arcadedb_collections
                    )
        except Exception as exc:
            logger.warning(
                "[gap-tool] tool_key=%s collection target_store lookup failed: %s",
                tool_key, exc, exc_info=True,
            )

    provider_ops = node_provider_operations(node)
    extra_context = ""
    try:
        if isinstance(cfg, dict):
            extra_context = (cfg.get("generationDescription") or "") + "\n" + (
                cfg.get("intentDescription") or ""
            )
        elif cfg is not None:
            extra_context = (
                (getattr(cfg, "generationDescription", None) or "")
                + "\n"
                + (getattr(cfg, "intentDescription", None) or "")
            )
    except Exception:
        extra_context = ""
    if not provider_ops:
        provider_ops = extract_provider_operations_from_text(
            tool_description,
            extra_context,
        )
    provider_spec_unverified = False
    spec_class = classify_provider_spec(provider_ops, tool_description)
    logger.info("[gap-tool] tool_key=%s spec_class=%s", tool_key, spec_class)

    if spec_class == "unknown":
        # Last resort: grounding only when we have no host and no public provider.
        logger.info("[gap-tool] tool_key=%s fetching REST spec via Google Search grounding", tool_key)
        grounded = await resolve_rest_api_spec_with_grounding(
            tool_description,
            available_credentials=available_credentials,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        if grounded:
            provider_ops = [grounded]
            provider_spec_unverified = True
            logger.info(
                "[gap-tool] tool_key=%s grounded REST spec %s %s (unverified)",
                tool_key,
                grounded.http_method,
                grounded.endpoint_url,
            )
    # pinned → provider_ops already set; model_known → LLM writes from training knowledge
    if cfg is not None and provider_ops:
        dumped = operations_to_dicts(provider_ops)
        try:
            if isinstance(cfg, dict):
                cfg["providerOperations"] = dumped
                if provider_spec_unverified:
                    cfg["provider_spec_unverified"] = True
            else:
                cfg.providerOperations = dumped
                if provider_spec_unverified:
                    cfg.provider_spec_unverified = True
        except Exception:
            pass

    persist_links = resolve_persistence_links(
        node=node,
        texts=(tool_description, extra_context),
    )
    if cfg is not None and persist_links:
        dumped_links = links_to_dicts(persist_links)
        try:
            if isinstance(cfg, dict):
                cfg["persistenceLinks"] = dumped_links
            else:
                cfg.persistenceLinks = dumped_links
        except Exception:
            pass

    model_research = ""
    if _gap_tool_description_implies_llm_api(tool_description, io_contract):
        logger.info("[gap-tool] tool_key=%s fetching model ids via Google Search", tool_key)
        model_research = (
            await _gap_tool_fetch_model_research_via_google(
                tool_description,
                io_contract,
                available_credentials,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            or ""
        )
        if model_research:
            logger.info(
                "[gap-tool] tool_key=%s model research ready (%s chars)",
                tool_key,
                len(model_research),
            )
    else:
        logger.info(
            "[gap-tool] tool_key=%s skipping model research (non-LLM / tesseract-only tool)",
            tool_key,
        )

    syntax_escalated = False
    for attempt in range(max_attempts):
        attempt_no = attempt + 1
        generation_model = gap_tool_model_for_attempt(
            attempt,
            last_rejection_reason,
            base_model=GAP_TOOL_GENERATION_MODEL,
            fix_model=GAP_TOOL_FIX_MODEL,
            already_escalated=syntax_escalated,
        )
        if generation_model == GAP_TOOL_FIX_MODEL:
            syntax_escalated = True
        logger.info(
            "[gap-tool] tool_key=%s node_id=%s attempt=%s/%s starting LLM codegen model=%s",
            tool_key,
            node.nodeId,
            attempt_no,
            max_attempts,
            generation_model,
        )
        if last_rejection_reason:
            logger.info(
                "[gap-tool] tool_key=%s attempt=%s/%s previous_rejection=%s",
                tool_key,
                attempt_no,
                max_attempts,
                last_rejection_reason,
            )
            if "deprecated" in (last_rejection_reason or "").lower():
                logger.info(
                    "[gap-tool] tool_key=%s fetching model research after deprecated rejection",
                    tool_key,
                )
                model_research = await _gap_tool_fetch_model_research_via_google(
                    tool_description,
                    io_contract,
                    available_credentials,
                    project_id=project_id,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    refresh_hint=last_rejection_reason,
                )
                logger.info(
                    "[gap-tool] tool_key=%s refreshed model research after deprecated rejection",
                    tool_key,
                )

        prompt = _build_gap_tool_prompt(
            node=node,
            gen=gen,
            tool_key=tool_key,
            io_contract=io_contract,
            attempt=attempt,
            previous_rejection_reason=last_rejection_reason,
            available_credentials=available_credentials,
            model_research_context=model_research,
            existing_function_code=existing_function_code,
            collection_store_map=collection_store_map,
            collection_field_map=collection_field_map,
            provider_operations=provider_ops,
            persistence_links=persist_links,
        )

        started_at = datetime.utcnow()
        response_text, usage = await async_gap_tool_llm_call(
            prompt, model=generation_model
        )
        response = response_text.strip()
        finished_at = datetime.utcnow()
        prompt_tokens = usage.get("prompt_tokens") or (len(prompt) // 4)
        completion_tokens = usage.get("completion_tokens") or (len(response) // 4)

        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="graph-planning",
            sub_operation="gap-tool-generation",
            model=generation_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            metadata={
                "node_id": node.nodeId,
                "tool_key": tool_key,
                "attempt": attempt_no,
                "rejection_reason": last_rejection_reason,
            },
        )

        candidate, parse_err = _parse_gap_tool_llm_response(response)
        if candidate is None:
            last_rejection_reason = parse_err or "LLM output is not a JSON object"
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s (response_len=%s)",
                tool_key,
                attempt_no,
                max_attempts,
                last_rejection_reason,
                len(response),
            )
            continue

        # Force the tool name to match exactly what was requested.
        candidate["name"] = tool_key

        validation_candidate = copy.deepcopy(candidate)
        if not validation_candidate.get("description"):
            validation_candidate["description"] = (
                gen.description or node.name or ""
            ).strip()

        llm_out_params = normalize_output_parameters(
            validation_candidate.get("outputParameters") or []
        )
        contract_out_params = normalize_output_parameters(
            io_contract.get("outputParameters") or []
        )
        action, extra_names, missing_names = _classify_gap_output_contract(
            llm_out_params,
            contract_out_params,
            function_code=str(validation_candidate.get("function_code") or ""),
        )
        if action == "retry_missing_contract":
            last_rejection_reason = (
                "outputParameters missing workflow contract keys entirely "
                f"(contract={missing_names}, llm={_gap_output_param_names(llm_out_params)}). Retry."
            )
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s",
                tool_key,
                attempt_no,
                max_attempts,
                last_rejection_reason,
            )
            continue
        if action == "targeted_fix":
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s output contract mismatch extra=%s missing=%s — targeted fix",
                tool_key,
                attempt_no,
                extra_names,
                missing_names,
            )
            fixed, fix_err = await _targeted_fix_gap_tool_output_contract(
                validation_candidate,
                io_contract,
                extra_names=extra_names,
                missing_names=missing_names,
                tool_key=tool_key,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                node_id=getattr(node, "nodeId", None),
                attempt=attempt_no,
            )
            if fixed is None:
                last_rejection_reason = (
                    fix_err or "output-contract targeted fix failed"
                )
                logger.warning(
                    "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s",
                    tool_key,
                    attempt_no,
                    max_attempts,
                    last_rejection_reason,
                )
                continue
            validation_candidate = fixed

        _normalize_gap_tool_parsed(validation_candidate, io_contract)

        fc = str(validation_candidate.get("function_code") or "")
        if _detect_mock_code(fc) == "bare pass statement":
            cleaned_fc = _remove_standalone_pass_lines(fc)
            if cleaned_fc != fc:
                validation_candidate["function_code"] = cleaned_fc
                logger.info(
                    "[gap-tool] tool_key=%s attempt=%s auto-removed standalone pass lines",
                    tool_key,
                    attempt_no,
                )

        compile_reason = _validate_gap_tool_executable(validation_candidate)
        if compile_reason is not None and "pass" in compile_reason.lower():
            _log_mock_code_context(
                str(validation_candidate.get("function_code") or ""),
                compile_reason,
                tool_key,
                attempt_no,
            )
        if compile_reason is not None and is_gap_tool_syntax_rejection(compile_reason):
            validation_candidate, compile_reason = await _repair_gap_tool_syntax_candidate(
                validation_candidate,
                compile_reason,
                tool_key=tool_key,
                attempt_no=attempt_no,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                node_id=getattr(node, "nodeId", None),
            )
        if compile_reason is not None:
            last_rejection_reason = compile_reason
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s",
                tool_key,
                attempt_no,
                max_attempts,
                compile_reason,
            )
            continue

        store_violation = _find_collection_store_violation(fc, collection_store_map)
        if store_violation is not None:
            last_rejection_reason = store_violation
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s",
                tool_key,
                attempt_no,
                max_attempts,
                store_violation,
            )
            continue

        topology_violation = _find_graph_topology_json_blob_violation(fc)
        if topology_violation is not None:
            last_rejection_reason = topology_violation
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s REJECTED: %s",
                tool_key,
                attempt_no,
                max_attempts,
                topology_violation,
            )
            continue

        fixture_reason = function_code_matches_provider_fixture(
            str(validation_candidate.get("function_code") or ""),
            provider_ops,
        )
        if fixture_reason:
            last_fixture_error = fixture_reason
            last_rejection_reason = fixture_reason
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s provider fixture failed: %s",
                tool_key,
                attempt_no,
                max_attempts,
                fixture_reason,
            )
            if attempt < max_attempts - 1:
                continue
            logger.warning(
                "[gap-tool] tool_key=%s publishing despite fixture mismatch (advisory)",
                tool_key,
            )

        persist_reason = function_code_matches_persistence_fixture(
            str(validation_candidate.get("function_code") or ""),
            persist_links,
            node_id=getattr(node, "nodeId", "") or "",
        )
        if persist_reason:
            last_fixture_error = persist_reason
            last_rejection_reason = persist_reason
            logger.warning(
                "[gap-tool] tool_key=%s attempt=%s/%s persistence fixture failed: %s",
                tool_key,
                attempt_no,
                max_attempts,
                persist_reason,
            )
            if attempt < max_attempts - 1:
                continue
            logger.warning(
                "[gap-tool] tool_key=%s publishing despite persistence fixture mismatch (advisory)",
                tool_key,
            )
        else:
            # Successful persistence fixture — drop stale persistence errors from
            # earlier attempts (provider fixture errors are unrelated).
            if last_fixture_error and any(
                tok in last_fixture_error
                for tok in ("correlationKey", "filterKey", "persist pinned", "query pinned")
            ):
                last_fixture_error = None

        parsed = validation_candidate
        logger.info(
            "[gap-tool] tool_key=%s attempt=%s/%s ACCEPTED validation, registering tool",
            tool_key,
            attempt_no,
            max_attempts,
        )
        await _write_back_graph_schema(
            fc, collection_store_map, org_id=org_id, subscriber_id=subscriber_id
        )
        break

    if parsed is None:
        logger.error(
            "[gap-tool] tool_key=%s FAILED after %s attempts last_rejection=%s",
            tool_key,
            max_attempts,
            last_rejection_reason,
        )
        raise RuntimeError(
            f"gap tool codegen for '{tool_key}' failed validation "
            f"on all {max_attempts} attempt(s). "
            f"Last rejection reason: [{last_rejection_reason}]. "
            f"Consider refining the io_contract to provide concrete input types "
            f"(e.g. raw bytes instead of an opaque ID) so the LLM can write real code."
        )

    _normalize_gap_tool_parsed(parsed, io_contract)
    final_reason = _validate_gap_tool_executable(parsed)
    if final_reason is not None:
        logger.error(
            "[gap-tool] tool_key=%s post-normalization validation failed: %s",
            tool_key,
            final_reason,
        )
        raise RuntimeError(
            f"gap tool codegen for '{tool_key}' failed post-normalization validation: "
            f"[{final_reason}]"
        )

    out_params = parsed.get("outputParameters") or []

    if not _get_register_tool()(parsed, save=True):
        logger.error("[gap-tool] tool_key=%s register_tool failed after validation passed", tool_key)
        raise RuntimeError(f"register_tool failed for gap-generated tool '{tool_key}'")

    logger.info("[gap-tool] tool_key=%s registered and saved successfully", tool_key)

    if cfg is not None:
        try:
            if isinstance(cfg, dict):
                cfg["provider_spec_unverified"] = bool(provider_spec_unverified)
                cfg["provider_fixture_error"] = last_fixture_error
            else:
                cfg.provider_spec_unverified = bool(provider_spec_unverified)
                cfg.provider_fixture_error = last_fixture_error
        except Exception:
            pass

    inp_params = normalize_input_parameters(parsed.get("parameters"))
    if not inp_params:
        # Fallback: use io_contract inputs if LLM returned no parameters.
        inp_params = normalize_input_parameters(io_contract.get("inputParameters") or [])

    # ── Upsert capability document under the correct tenant ──────────────────
    # If a real subscriber_id was provided this tool is subscriber-scoped.
    # Otherwise fall back to the platform tenant (never all-null).
    if subscriber_id and str(subscriber_id).strip():
        tenant_for_tool: Dict[str, Optional[str]] = {
            "orgId": org_id or None,
            "subscriberId": subscriber_id,
            "userId": None,
        }
    else:
        tenant_for_tool = _get_platform_tenant()

    now = datetime.utcnow()
    cap_id = f"tool:{tool_key}"
    capability_doc: Dict[str, Any] = {
        "capabilityId": cap_id,
        "kind": "tool",
        "name": tool_key,
        "description": (
            parsed.get("description") or gen.description or node.name or ""
        ).strip(),
        "inputParameters": inp_params,
        "outputParameters": out_params,
        "version": str(parsed.get("version") or "1"),
        "isLatest": True,
        "status": "active",
        "source": {
            "sourceType": "gap_tool_llm",
            "sourceRef": {
                "projectId": project_id,
                "nodeId": node.nodeId,
                "toolKey": tool_key,
                "class_path": dynamic_tool_class_path(tool_key),
            },
            "sourceUpdatedAt": now,
            "syncedAt": now,
        },
        "runtime": {"tool": {"toolName": tool_key}},
        "class_path": dynamic_tool_class_path(tool_key),
        # Richer searchText: tool_key + LLM-parsed description + original
        # step name/nodeId + generationDescription.  All of these are indexed
        # by MongoDB's $text search so the capability surfaces on future runs
        # that use the same or similar prompt wording.
        "searchText": " ".join(filter(None, [
            tool_key,
            parsed.get("description") or "",
            node.name or "",
            node.nodeId or "",
            gen.description or "",
        ])).strip(),
        "tags": list(parsed.get("keywords") or []) + ["gap_generated", "agentic"],
        "usageCount": 0,
        "providerOperations": operations_to_dicts(provider_ops),
        "provider_spec_unverified": bool(provider_spec_unverified),
        "provider_fixture_error": last_fixture_error,
        "persistenceLinks": links_to_dicts(persist_links),
    }

    await capability_repository.upsert_capability(tenant_for_tool, capability_doc)
    await capability_repository.set_is_latest_for_capability(tenant_for_tool, cap_id)

    return {
        "capabilityId": cap_id,
        "version": str(capability_doc.get("version") or "1"),
        "class_path": str(capability_doc.get("class_path") or dynamic_tool_class_path(tool_key)),
        "source": "gap_tool_llm",
    }
