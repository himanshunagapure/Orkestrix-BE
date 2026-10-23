"""
Prompt-mode enterprise step expander.

Turns a vague NL prompt into production-ready logical steps BEFORE Stage 0.
Stage 0 then clarifies those steps; Stage 1 skeleton implements them.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from services.gemini_service import async_generate, GeminiGenerateConfig, get_gemini_api_key
from services.llm_token_tracker import record_llm_call
from services.workflow_budget_service import BillingError
from nl_builder.models import LogicalStep

logger = logging.getLogger(__name__)

ENTERPRISE_EXPAND_MODEL = "gemini-3.1-pro-preview"
MIN_ENTERPRISE_STEPS = 6
MAX_ENTERPRISE_STEPS = 24


class EnterpriseExpandResult(BaseModel):
    steps: List[LogicalStep] = Field(default_factory=list)
    source: str = "llm"
    warnings: List[str] = Field(default_factory=list)


class EnterpriseStepSpec(BaseModel):
    """
    Expander/revise Gemini schema — capability list only.
    No inputs/outputs: Stage 1 builds topology; Stage 2 owns I/O bindings.
    """
    stepId: str = Field(..., description="Stable identifier like 'L1', 'L2', ...")
    name: str = Field(..., description="Short human-readable step name")
    description: str = Field(..., description="What capability and which outcomes/states exist")
    preferredKind: Optional[Literal["ui_screen", "tool", "agent"]] = Field(
        default=None,
        description="ui_screen / tool only when obvious; otherwise null",
    )

    @field_validator("stepId", "name", "description")
    @classmethod
    def normalize_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("value cannot be empty")
        return v


class EnterpriseStepsPayload(BaseModel):
    """Gemini JSON schema for the expander — must not say 'decompose'."""
    steps: List[EnterpriseStepSpec] = Field(
        default_factory=list,
        description=(
            "8-18 production-ready enterprise workflow capabilities for this domain. "
            "Expand the user request; do not reduce it to the fewest happy-path steps. "
            "Emit only stepId, name, description, preferredKind — never inputs/outputs. "
            "preferredKind should be null unless the step is clearly a human screen "
            "or a backend-only tool."
        ),
    )


def _without_io(step: Any) -> LogicalStep:
    """Expander steps never carry I/O; Stage 2 invents bindings."""
    if not isinstance(step, LogicalStep):
        if hasattr(step, "model_dump"):
            step = step.model_dump(mode="json")
        step = LogicalStep.model_validate(step)
    return LogicalStep(
        stepId=step.stepId,
        name=step.name,
        description=step.description,
        inputs=[],
        outputs=[],
        preferredKind=step.preferredKind,
    )


def compose_expanded_steps_prompt(user_prompt: str, steps: List[LogicalStep]) -> str:
    """
    Stage 0-B input: original request + enterprise steps to clarify
    (credentials, collections, review vs auto, providers) — not a minimal rewrite.
    """
    lines = [
        "ORIGINAL USER REQUEST:",
        (user_prompt or "").strip() or "Create a workflow",
        "",
        "ENTERPRISE WORKFLOW STEPS TO CLARIFY AND IMPLEMENT:",
        "Clarify credentials, collection/table names, human review vs auto-approve,",
        "external providers, and notification channels for THESE steps.",
        "Do not collapse this back into a minimal happy-path. Do not drop steps.",
        "",
    ]
    for s in steps:
        if hasattr(s, "model_dump"):
            kind = getattr(s, "preferredKind", None) or "FREE"
            sid = getattr(s, "stepId", "")
            name = getattr(s, "name", "")
            desc = getattr(s, "description", "")
        elif isinstance(s, dict):
            kind = s.get("preferredKind") or "FREE"
            sid = s.get("stepId") or ""
            name = s.get("name") or ""
            desc = s.get("description") or ""
        else:
            continue
        lines.append(f"{sid}. {name} [{kind}]")
        lines.append(f"   {desc}")
    return "\n".join(lines)


def attach_stage0_clarifications(
    steps: List[dict],
    stage0_context: Optional[str],
) -> List[dict]:
    """
    Stage 1 treats Stage 0 context as constraints-only and will not add nodes
    from it. Clarifications that name collections, review vs auto, or providers
    must live inside LOGICAL STEPS or they are ignored.
    """
    ctx = (stage0_context or "").strip()
    if not ctx or not steps:
        return steps
    out = [dict(s) for s in steps]
    first = dict(out[0])
    desc = (first.get("description") or "").rstrip()
    first["description"] = (
        f"{desc}\n\n"
        "AUTHORITATIVE CLARIFICATIONS (apply across all steps; do not ignore):\n"
        f"{ctx}"
    )
    out[0] = first
    return out


def logical_steps_to_scratch_dicts(steps: List[Any]) -> List[dict]:
    """Scratch LogicalStepLite only locks ui_screen/tool; leave others FREE."""
    out: List[dict] = []
    for s in steps or []:
        if hasattr(s, "model_dump"):
            raw = s.model_dump(mode="json")
        elif isinstance(s, dict):
            raw = dict(s)
        else:
            raw = {
                "stepId": getattr(s, "stepId", None),
                "name": getattr(s, "name", None),
                "description": getattr(s, "description", None),
                "preferredKind": getattr(s, "preferredKind", None),
            }
        kind = raw.get("preferredKind")
        if kind not in ("ui_screen", "tool"):
            kind = None
        out.append(
            {
                "stepId": raw.get("stepId") or "",
                "name": raw.get("name") or "",
                "description": raw.get("description") or "",
                "preferredKind": kind,
            }
        )
    return out


def _expansion_system_prompt() -> str:
    return """You are an enterprise workflow architect.

Your job is NOT to decompose a sentence into the fewest implementation steps.
Your job is to expand a user request into the major business capabilities of a
production-ready application in that domain.

Do NOT optimize for the minimum number of steps.
Do NOT emit start/end nodes (the graph planner adds those).
Do NOT collapse distinct outcomes into a single success/failure step.

For every request, reason through then emit steps that represent:
1. Business lifecycle (initiate → in-progress states → complete / reject / expire)
2. Actors (customer, system, reviewer, external provider, parent system)
3. Decision points (existing record? valid? match? risk? review needed?)
4. Failure modes as SEPARATE paths: retryable technical failure, business rejection,
   provider unavailable / pending, manual review — never one generic "rejected"
5. Human intervention where a real enterprise process would wait on a person
6. Persistence and audit boundaries (session/state, documents, results, audit events)
7. Integration boundaries (storage, extraction, verification providers, screening,
   notifications, parent workflow events)
8. Security / consent / compliance checkpoints when the domain requires them

preferredKind:
- "ui_screen" only when a human must see or fill a screen (forms, consent, review queue)
- "tool" only when it is unambiguously backend-only with no user UI
- null (preferred) when Stage 1 should split, add routers, or choose the type

Keep names short and action-oriented. Descriptions must say WHAT capability and
WHICH outcomes/states exist (not how to code Gemini prompts).

Do NOT invent per-step inputs or outputs. Topology and I/O bindings are planned
later (Stage 1 skeleton, Stage 2 bindings). Each step is only:
stepId, name, description, preferredKind.

Return ONLY JSON matching the schema."""


def _revision_system_prompt() -> str:
    return (
        _expansion_system_prompt()
        + """

You are now REVISING an existing enterprise step list in a back-and-forth edit session.
Honor the user's change request (add, remove, split, merge, reorder, or reword steps).
Return the FULL updated list, not a delta. Keep unrelated steps unless the user
asks to drop them. Do not collapse the plan back into a minimal happy path
unless the user explicitly asks to simplify."""
    )


def _steps_as_text(steps: List[Any]) -> str:
    lines: List[str] = []
    for i, s in enumerate(steps or [], start=1):
        if hasattr(s, "model_dump"):
            raw = s.model_dump(mode="json")
        elif isinstance(s, dict):
            raw = dict(s)
        else:
            raw = {
                "stepId": getattr(s, "stepId", f"L{i}"),
                "name": getattr(s, "name", ""),
                "description": getattr(s, "description", ""),
                "preferredKind": getattr(s, "preferredKind", None),
            }
        sid = raw.get("stepId") or f"L{i}"
        kind = raw.get("preferredKind") or "FREE"
        lines.append(f"{sid}. {raw.get('name') or ''} [{kind}]")
        lines.append(f"   {raw.get('description') or ''}")
    return "\n".join(lines) if lines else "(none)"


def _finalize_enterprise_steps(
    raw_steps: List[Any],
    *,
    keep_if_thin: Optional[List[LogicalStep]] = None,
    allow_thin: bool = False,
) -> Tuple[List[LogicalStep], List[str], str]:
    """Normalize, strip I/O, enforce min/max. Returns (steps, warnings, source)."""
    from nl_builder.plan_generator import _normalize_logical_steps

    normalized, warnings = _normalize_logical_steps(list(raw_steps or []))
    normalized = [_without_io(s) for s in normalized]
    keep_clean = [_without_io(s) for s in keep_if_thin] if keep_if_thin else None
    source = "llm"
    if len(normalized) < MIN_ENTERPRISE_STEPS:
        warnings.append(
            f"Expander returned {len(normalized)} steps (< {MIN_ENTERPRISE_STEPS}); "
            "treating as too thin."
        )
        if allow_thin and normalized:
            warnings.append("Keeping the revised list because this is a user-requested edit.")
            return normalized, warnings, source
        if keep_clean:
            warnings.append("Kept the previous step list.")
            return keep_clean, warnings, "fallback"
        return [], warnings, "fallback"
    if len(normalized) > MAX_ENTERPRISE_STEPS:
        warnings.append(
            f"Truncated expander output from {len(normalized)} to {MAX_ENTERPRISE_STEPS} steps"
        )
        normalized = [
            LogicalStep(
                stepId=f"L{i}",
                name=step.name,
                description=step.description,
                inputs=[],
                outputs=[],
                preferredKind=step.preferredKind,
            )
            for i, step in enumerate(normalized[:MAX_ENTERPRISE_STEPS], start=1)
        ]
    return normalized, warnings, source


async def _generate_enterprise_steps(
    *,
    system_prompt: str,
    user_prompt: str,
    operation: str,
    sub_operation: str,
    api_key: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    keep_if_thin: Optional[List[LogicalStep]] = None,
    allow_thin: bool = False,
    fail_warning: str = "Enterprise step expansion failed",
) -> EnterpriseExpandResult:
    if api_key is None:
        api_key = get_gemini_api_key()

    started_at = datetime.utcnow()
    try:
        response = await async_generate(
            ENTERPRISE_EXPAND_MODEL,
            f"{system_prompt}\n\n{user_prompt}",
            config=GeminiGenerateConfig(
                temperature=0.25,
                response_mime_type="application/json",
                response_schema=EnterpriseStepsPayload,
                fallback_profile="planner",
            ),
            api_key=api_key,
        )
        finished_at = datetime.utcnow()
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
        total_tokens = getattr(usage, "total_token_count", 0) if usage else (
            prompt_tokens + completion_tokens
        )
        try:
            from agent_new import record_agent_llm_usage
            record_agent_llm_usage(
                ENTERPRISE_EXPAND_MODEL,
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
            project_id=project_id,
            screen_id=None,
            operation=operation,
            sub_operation=sub_operation,
            model=ENTERPRISE_EXPAND_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            status="success",
            started_at=started_at,
            finished_at=finished_at,
        )

        raw_parsed = getattr(response, "parsed", None)
        if isinstance(raw_parsed, EnterpriseStepsPayload):
            parsed = raw_parsed
        else:
            parsed = EnterpriseStepsPayload.model_validate(raw_parsed or {"steps": []})
        steps, warnings, source = _finalize_enterprise_steps(
            parsed.steps, keep_if_thin=keep_if_thin, allow_thin=allow_thin
        )
        logger.info(
            "[nl/expand] enterprise steps=%d source=%s",
            len(steps),
            source,
        )
        return EnterpriseExpandResult(steps=steps, source=source, warnings=warnings)
    except BillingError:
        raise
    except Exception as exc:
        logger.warning("[nl/expand] %s: %s", fail_warning, exc)
        record_llm_call(
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=project_id,
            screen_id=None,
            operation=operation,
            sub_operation=sub_operation,
            model=ENTERPRISE_EXPAND_MODEL,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            status="failed",
            started_at=started_at,
            finished_at=datetime.utcnow(),
            metadata={"error": str(exc)},
        )
        if keep_if_thin:
            return EnterpriseExpandResult(
                steps=[_without_io(s) for s in keep_if_thin],
                source="fallback",
                warnings=[f"{fail_warning}: {exc}", "Kept the previous step list."],
            )
        return EnterpriseExpandResult(
            steps=[],
            source="fallback",
            warnings=[f"{fail_warning}: {exc}"],
        )


async def expand_prompt_to_enterprise_steps(
    prompt: str,
    *,
    api_key: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> EnterpriseExpandResult:
    """LLM expansion of a vague NL prompt into enterprise logical steps."""
    text = (prompt or "").strip()
    if not text:
        return EnterpriseExpandResult(steps=[], source="fallback", warnings=["Empty prompt"])

    user_prompt = f"""USER REQUEST:
\"\"\"{text}\"\"\"

Expand this into production-ready enterprise workflow steps for that domain.
"""
    return await _generate_enterprise_steps(
        system_prompt=_expansion_system_prompt(),
        user_prompt=user_prompt,
        operation="nl-enterprise-expand",
        sub_operation="prompt-to-enterprise-steps",
        api_key=api_key,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        project_id=project_id,
        fail_warning="Enterprise step expansion failed",
    )


async def revise_enterprise_steps(
    prompt: str,
    steps: List[Any],
    feedback: str,
    *,
    api_key: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> EnterpriseExpandResult:
    """Apply NL feedback to an existing enterprise step list. Not graph revise."""
    from nl_builder.plan_generator import _normalize_logical_steps

    text = (prompt or "").strip()
    fb = (feedback or "").strip()
    current, _ = _normalize_logical_steps(list(steps or []), preserve_ids=True)
    current = [_without_io(s) for s in current]
    if not fb:
        return EnterpriseExpandResult(
            steps=current,
            source="fallback",
            warnings=["Empty feedback; returned the current steps unchanged."],
        )
    if not current:
        return await expand_prompt_to_enterprise_steps(
            text or fb,
            api_key=api_key,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=project_id,
        )

    user_prompt = f"""ORIGINAL USER REQUEST:
\"\"\"{text or "(not provided)"}\"\"\"

CURRENT ENTERPRISE STEPS:
{_steps_as_text(current)}

REVISION REQUEST:
\"\"\"{fb}\"\"\"

Return the full updated production-ready enterprise step list.
"""
    return await _generate_enterprise_steps(
        system_prompt=_revision_system_prompt(),
        user_prompt=user_prompt,
        operation="nl-enterprise-expand",
        sub_operation="revise-enterprise-steps",
        api_key=api_key,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        project_id=project_id,
        keep_if_thin=current,
        allow_thin=True,
        fail_warning="Enterprise step revision failed",
    )
