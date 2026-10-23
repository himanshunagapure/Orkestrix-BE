"""
FastAPI Router for Natural Language Workflow Builder endpoints.

Endpoints:
- POST /aiqod-agent/agent/agentic/nl/expand/estimate
- POST /aiqod-agent/agent/agentic/nl/expand
- POST /aiqod-agent/agent/agentic/nl/revise/estimate
- POST /aiqod-agent/agent/agentic/nl/steps/revise/estimate
- POST /aiqod-agent/agent/agentic/nl/steps/revise
- POST /aiqod-agent/agent/agentic/nl/plan
- POST /aiqod-agent/agent/agentic/nl/plan/revise
"""

import logging
from typing import Any, Dict, Literal, Optional
from fastapi import APIRouter, HTTPException, Header, Request, status

from services.workflow_budget_service import BillingError

from nl_builder.models import (
    NLPlanRequest,
    NLPlanReviseRequest,
    NLPlanResponse,
    NLExpandRequest,
    NLStepsReviseRequest,
    NLStepsResponse,
    NLLlmCallEstimateResponse,
)

logger = logging.getLogger(__name__)

nl_router = APIRouter(
    prefix="/aiqod-agent/agent/agentic/nl",
    tags=["Natural Language Builder"],
)

# In-memory store for active plans by session_id (for session-based plan revisions)
_active_plans_cache: Dict[str, NLPlanResponse] = {}


def _load_active_plan(session_id: str, session: Any) -> Optional[NLPlanResponse]:
    cached = _active_plans_cache.get(session_id)
    if cached is not None:
        return cached
    raw = getattr(session, "nl_plan", None)
    if not isinstance(raw, dict):
        return None
    try:
        plan = NLPlanResponse.model_validate(raw)
        _active_plans_cache[session_id] = plan
        return plan
    except Exception as exc:
        logger.warning("Failed to restore NL plan for session %s: %s", session_id, exc)
        return None


async def _store_active_plan(
    session: Any,
    response: NLPlanResponse,
    project_id: Optional[str],
    *,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    selected_name: Optional[str] = None,
    selected_type: Optional[str] = None,
    selected_category: Optional[str] = None,
) -> None:
    _active_plans_cache[response.sessionId] = response
    try:
        session.nl_plan = response.model_dump(mode="json")
        if project_id:
            session.nl_plan_project_id = project_id
        from agent_new import stage0_session_manager
        await stage0_session_manager.save(session)
    except Exception as exc:
        logger.warning("Failed to persist NL plan for session %s: %s", response.sessionId, exc)

    # Mirror /scratch/preview: write phaseGraphs + steps into Process_Configs.
    if project_id:
        try:
            from nl_builder.process_configs import persist_nl_preview_to_process_configs

            persist_nl_preview_to_process_configs(
                project_id=project_id,
                org_id=org_id or getattr(session, "org_id", None),
                subscriber_id=subscriber_id or getattr(session, "subscriber_id", None),
                user_id=user_id or getattr(session, "user_id", None),
                selected_name=selected_name or getattr(session, "workflow_name", None),
                selected_type=selected_type,
                selected_category=selected_category,
                prompt=(
                    getattr(session, "original_user_prompt", None)
                    or getattr(session, "raw_prompt", None)
                ),
                plan_response=response,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist NL plan to Process_Configs for project %s: %s",
                project_id,
                exc,
            )


def _tenant_from_request(
    request_obj: Any,
    http_request: Request,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    org_id = getattr(request_obj, "orgId", None) or http_request.headers.get("x-org-id")
    subscriber_id = getattr(request_obj, "subscriberId", None) or http_request.headers.get("x-subscriber-id")
    user_id = getattr(request_obj, "userId", None) or http_request.headers.get("x-user-id")
    return org_id, subscriber_id, user_id


def _nl_call_estimate_response(est: Any, *, subscriber_id: Optional[str]) -> NLLlmCallEstimateResponse:
    subscriber_available = None
    sufficient = None
    if subscriber_id:
        try:
            from services.workflow_budget_service import get_subscriber_available_credits

            subscriber_available = get_subscriber_available_credits(subscriber_id)
            if subscriber_available is not None:
                sufficient = subscriber_available >= est.predicted_cost
        except Exception:
            subscriber_available = None
            sufficient = None
    return NLLlmCallEstimateResponse(
        operation=est.operation,
        endpoint=est.endpoint,
        model=est.model,
        predictedCost=est.predicted_cost,
        estimatedCostUsd=est.estimated_cost_usd,
        inputTokens=est.input_tokens,
        outputTokens=est.output_tokens,
        totalTokens=int(est.input_tokens) + int(est.output_tokens),
        source=est.source,
        sampleCount=est.sample_count,
        observedAvgCostUsd=est.observed_avg_cost_usd,
        assumptions=est.assumptions,
        subscriberAvailableUsd=subscriber_available,
        sufficientBalance=sufficient,
    )


@nl_router.post(
    "/expand/estimate",
    response_model=NLLlmCallEstimateResponse,
    summary="Estimate LLM cost for /nl/expand",
    description=(
        "No LLM call. Returns the predicted Gemini cost of one /nl/expand. "
        "Uses llm_token_count averages when available, otherwise token defaults "
        "for gemini-3.1-pro-preview."
    ),
)
async def expand_estimate_endpoint(
    request: NLExpandRequest,
    http_request: Request,
) -> NLLlmCallEstimateResponse:
    from services.plan_credit_estimator import estimate_nl_expand_cost

    prompt = (request.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")
    _, subscriber_id, _ = _tenant_from_request(request, http_request)
    est = await estimate_nl_expand_cost(prompt=prompt)
    return _nl_call_estimate_response(est, subscriber_id=subscriber_id)


@nl_router.post(
    "/expand",
    response_model=NLStepsResponse,
    summary="Expand NL prompt into enterprise logical steps",
    description=(
        "First prompt-mode step. Runs enterprise_expander on the user prompt and "
        "returns the step list for the UI. Does not start Stage 0 or plan a graph. "
        "Show the steps; if the user wants changes, call /nl/steps/revise (repeatable). "
        "When they are satisfied, pass the latest steps to /stage0/start as expandedSteps."
    ),
)
async def expand_prompt_endpoint(
    request: NLExpandRequest,
    http_request: Request,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
) -> NLStepsResponse:
    from nl_builder.enterprise_expander import expand_prompt_to_enterprise_steps

    org_id, subscriber_id, user_id = _tenant_from_request(request, http_request)
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")

    from services.gemini_service import get_gemini_api_key
    if not (x_gemini_api_key or get_gemini_api_key()):
        raise HTTPException(
            status_code=503,
            detail="Gemini API key is not available (GCP Secret Manager / config).",
        )

    try:
        result = await expand_prompt_to_enterprise_steps(
            prompt,
            api_key=x_gemini_api_key,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=request.projectId,
        )
    except BillingError:
        raise
    except Exception as exc:
        logger.exception("Failed to expand prompt: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Step expansion failed: {exc}",
        ) from exc

    source: Literal["llm", "fallback"] = "llm" if result.source == "llm" else "fallback"
    if not result.steps:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "enterprise_expand_empty",
                "message": "Expander returned no usable steps. Retry or rephrase the prompt.",
                "warnings": list(result.warnings or []),
                "source": source,
            },
        )
    return NLStepsResponse(
        prompt=prompt,
        steps=result.steps,
        stepCount=len(result.steps),
        source=source,
        warnings=list(result.warnings or []),
    )


async def _revise_estimate(
    request: NLStepsReviseRequest,
    http_request: Request,
) -> NLLlmCallEstimateResponse:
    from services.plan_credit_estimator import estimate_nl_revise_cost

    prompt = (request.prompt or "").strip()
    feedback = (request.feedback or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required.")
    if not request.steps:
        raise HTTPException(status_code=400, detail="steps is required.")
    _, subscriber_id, _ = _tenant_from_request(request, http_request)
    est = await estimate_nl_revise_cost(
        prompt=prompt,
        steps=list(request.steps),
        feedback=feedback,
    )
    return _nl_call_estimate_response(est, subscriber_id=subscriber_id)


@nl_router.post(
    "/revise/estimate",
    response_model=NLLlmCallEstimateResponse,
    summary="Estimate LLM cost for /nl/steps/revise",
    description=(
        "No LLM call. Same payload as /nl/steps/revise. Predicted Gemini cost "
        "of one step-list revision. Alias of /nl/steps/revise/estimate."
    ),
)
async def revise_estimate_alias_endpoint(
    request: NLStepsReviseRequest,
    http_request: Request,
) -> NLLlmCallEstimateResponse:
    return await _revise_estimate(request, http_request)


@nl_router.post(
    "/steps/revise/estimate",
    response_model=NLLlmCallEstimateResponse,
    summary="Estimate LLM cost for /nl/steps/revise",
    description=(
        "No LLM call. Returns the predicted Gemini cost of one /nl/steps/revise. "
        "Uses llm_token_count averages when available, otherwise token defaults."
    ),
)
async def revise_steps_estimate_endpoint(
    request: NLStepsReviseRequest,
    http_request: Request,
) -> NLLlmCallEstimateResponse:
    return await _revise_estimate(request, http_request)


@nl_router.post(
    "/steps/revise",
    response_model=NLStepsResponse,
    summary="Revise enterprise steps from NL feedback",
    description=(
        "Back-and-forth step editor. Call repeatedly with the latest steps + new feedback "
        "until the user is satisfied. Stateless: send the full current list each time. "
        "Do not use /nl/plan/revise here — that endpoint revises the LangGraph after preview."
    ),
)
async def revise_steps_endpoint(
    request: NLStepsReviseRequest,
    http_request: Request,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
) -> NLStepsResponse:
    from nl_builder.enterprise_expander import revise_enterprise_steps

    org_id, subscriber_id, user_id = _tenant_from_request(request, http_request)
    prompt = request.prompt.strip()
    feedback = request.feedback.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required.")
    if not request.steps:
        raise HTTPException(status_code=400, detail="steps is required.")

    from services.gemini_service import get_gemini_api_key
    if not (x_gemini_api_key or get_gemini_api_key()):
        raise HTTPException(
            status_code=503,
            detail="Gemini API key is not available (GCP Secret Manager / config).",
        )

    try:
        result = await revise_enterprise_steps(
            prompt,
            list(request.steps),
            feedback,
            api_key=x_gemini_api_key,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            project_id=request.projectId,
        )
    except BillingError:
        raise
    except Exception as exc:
        logger.exception("Failed to revise enterprise steps: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Step revision failed: {exc}",
        ) from exc

    source: Literal["llm", "fallback"] = "llm" if result.source == "llm" else "fallback"
    return NLStepsResponse(
        prompt=prompt,
        steps=result.steps,
        stepCount=len(result.steps),
        source=source,
        warnings=list(result.warnings or []),
        feedback=feedback,
    )


@nl_router.post(
    "/plan",
    response_model=NLPlanResponse,
    summary="[Alias] Generate LangGraph plan from Stage 0 session",
    description=(
        "Preferred path: POST /agentic/plan/scratch/preview with "
        "{prompt, projectId, stage0Handoff} — same shape as steps mode. "
        "This endpoint remains as a session-based alias returning NLPlanResponse."
    ),
)
async def generate_plan_endpoint(
    request: NLPlanRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
) -> NLPlanResponse:
    # Lazy imports avoid circular load: agent_new → this router → plan_generator → agent_new
    from agent_new import stage0_session_manager
    from nl_builder.plan_generator import generate_nl_plan

    session = await stage0_session_manager.get(request.sessionId)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stage 0 session '{request.sessionId}' not found or expired.",
        )

    try:
        response = await generate_nl_plan(
            session_id=request.sessionId,
            project_id=request.projectId,
            stage0_session=session,
            org_id=request.orgId or getattr(session, "org_id", None),
            subscriber_id=request.subscriberId or getattr(session, "subscriber_id", None),
            user_id=request.userId or getattr(session, "user_id", None),
            selected_name=request.selectedName,
            selected_type=request.selectedType,
            selected_category=request.selectedCategory,
            api_key=x_gemini_api_key,
        )

        await _store_active_plan(
            session,
            response,
            request.projectId,
            org_id=request.orgId or getattr(session, "org_id", None),
            subscriber_id=request.subscriberId or getattr(session, "subscriber_id", None),
            user_id=request.userId or getattr(session, "user_id", None),
            selected_name=request.selectedName,
            selected_type=request.selectedType,
            selected_category=request.selectedCategory,
        )
        return response

    except HTTPException:
        raise
    except BillingError:
        raise
    except Exception as exc:
        logger.exception("Failed to generate NL plan for session %s: %s", request.sessionId, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Plan generation failed: {str(exc)}",
        )


@nl_router.post(
    "/plan/revise",
    response_model=NLPlanResponse,
    summary="Revise LangGraph Workflow Plan based on Text Feedback",
    description=(
        "Revises the LangGraph after /scratch/preview (sessionId + stored plan). "
        "To change the enterprise step list before Stage 0, use POST /nl/steps/revise."
    ),
)
async def revise_plan_endpoint(
    request: NLPlanReviseRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
) -> NLPlanResponse:
    # Lazy imports avoid circular load: agent_new → this router → plan_reviser → plan_generator → agent_new
    from agent_new import stage0_session_manager
    from nl_builder.plan_reviser import revise_nl_plan

    session = await stage0_session_manager.get(request.sessionId)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stage 0 session '{request.sessionId}' not found or expired.",
        )

    current_plan = _load_active_plan(request.sessionId, session)
    project_id = request.projectId or getattr(session, "nl_plan_project_id", None)

    try:
        response = await revise_nl_plan(
            session_id=request.sessionId,
            feedback=request.feedback,
            stage0_session=session,
            current_plan_response=current_plan,
            project_id=project_id,
            org_id=request.orgId or getattr(session, "org_id", None),
            subscriber_id=request.subscriberId or getattr(session, "subscriber_id", None),
            user_id=request.userId or getattr(session, "user_id", None),
            api_key=x_gemini_api_key,
        )

        await _store_active_plan(
            session,
            response,
            project_id,
            org_id=request.orgId or getattr(session, "org_id", None),
            subscriber_id=request.subscriberId or getattr(session, "subscriber_id", None),
            user_id=request.userId or getattr(session, "user_id", None),
            selected_name=response.workflowName,
        )
        return response

    except HTTPException:
        raise
    except BillingError:
        raise
    except Exception as exc:
        logger.exception("Failed to revise NL plan for session %s: %s", request.sessionId, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Plan revision failed: {str(exc)}",
        )
