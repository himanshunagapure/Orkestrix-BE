"""
Workflow budget cap (Process_Configs.predictedCost) and subscriber billing (AICredits).

Subscriber wallet:
  available = AICredits.limit - AICredits.used - AICredits.reserved

Queue service reserves predictedCost on subscribers.AICredits.reserved before admission.
POST /agentic/workflow/register creates Process_Configs (type=workflow) after queue admission.
/estimate only returns predictedCost — it does not write Process_Configs.

On each LLM call (workflow-cap mode): used += cost, reserved -= cost, budgetSpentUsd += cost.
On wallet-only workflow calls (Scenario A resume): used += cost, overflowSpentUsd += cost.
On workflow completion/cancel: release unused hold (predictedCost - budgetSpentUsd) from reserved.
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
import asyncio as _asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

PROCESS_CONFIGS_COLLECTION = "Process_Configs"
SUBSCRIBERS_COLLECTION = "subscribers"
ENGINE_TYPE = "agentic_graph"
RESERVATION_ACTIVE = "active"
RESERVATION_RELEASED = "released"

SUBSCRIBER_BILLING_PATH_SUFFIXES = frozenset(
    {
        "/agentic/gap-tool/generate",
        "/generate-angular-app",
        "/update-angular-screen",
        "/generate-angular-flow",
        "/run",
        "/runnew",
        "/runnew_wo",
        "/executeagent",
        "/executeagent-raw",
        "/create",
        "/autoselect",
        "/save",
        "/test",
        "/toolcreate",
        "/tool-regenerate",
        "/tooledit",
        "/save-process-configs",
        "/save-process-configs-old",
        "/rules/generate-expressions",
        "/evaluateAIRule",
        "/evaluateAIRuleOld",
    }
)


class BillingError(Exception):
    code: str = "BILLING_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class PredictedCostMissingError(BillingError):
    code = "PREDICTED_COST_MISSING"


class WorkflowNotRegisteredError(BillingError):
    code = "WORKFLOW_NOT_REGISTERED"


class InsufficientSubscriberBalanceError(BillingError):
    code = "INSUFFICIENT_SUBSCRIBER_BALANCE"

    def __init__(
        self,
        message: str,
        *,
        available: float = 0.0,
        required: float = 0.0,
    ):
        super().__init__(message, code=self.code)
        self.available = available
        self.required = required


class WorkflowBudgetExhaustedError(BillingError):
    code = "WORKFLOW_BUDGET_EXHAUSTED"

    def __init__(
        self,
        message: str,
        *,
        predicted_cost: float = 0.0,
        budget_spent: float = 0.0,
    ):
        super().__init__(message, code=self.code)
        self.predicted_cost = predicted_cost
        self.budget_spent = budget_spent


@dataclass
class BillingContext:
    subscriber_id: str
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    predicted_cost: Optional[float] = None
    budget_spent_usd: float = 0.0
    overflow_spent_usd: float = 0.0
    subscriber_available_usd: float = 0.0
    enforce_workflow_cap: bool = False
    charge_subscriber: bool = True
    # Set to True by the execute handler when a BillingError is caught inside a
    # workflow_reservation_release_scope block, so the scope manager knows to keep
    # the hold active instead of releasing it.
    billing_error_occurred: bool = False

    @property
    def workflow_remaining_usd(self) -> Optional[float]:
        if self.predicted_cost is None:
            return None
        return float(self.predicted_cost) - float(self.budget_spent_usd)

    @property
    def total_workflow_spent_usd(self) -> float:
        return float(self.budget_spent_usd) + float(self.overflow_spent_usd)

    @property
    def subscriber_remaining_usd(self) -> float:
        return float(self.subscriber_available_usd)


_billing_context: ContextVar[Optional[BillingContext]] = ContextVar(
    "billing_context", default=None
)

_db = None


def set_billing_db(db) -> None:
    global _db
    _db = db


def get_billing_context() -> Optional[BillingContext]:
    return _billing_context.get()


def clear_billing_context() -> None:
    _billing_context.set(None)


def _to_object_id(value: Optional[str]) -> Optional[ObjectId]:
    if not value:
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception:
        return None


def _tenant_query(
    *,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    for key, raw in (
        ("orgId", org_id),
        ("subscriberId", subscriber_id),
        ("userId", user_id),
    ):
        if raw is None:
            continue
        oid = _to_object_id(raw)
        q[key] = oid if oid is not None else raw
    return q


def _process_configs_query(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> Dict[str, Any]:
    return {
        **_tenant_query(org_id=org_id, subscriber_id=subscriber_id, user_id=user_id),
        "engineType": ENGINE_TYPE,
        "agentic_process.workflow_id": project_id,
    }


def _subscriber_filter(subscriber_id: str) -> Dict[str, Any]:
    oid = _to_object_id(subscriber_id)
    return {"_id": oid} if oid is not None else {"_id": subscriber_id}


def _credit_field_as_double(field_ref: str) -> Dict[str, Any]:
    """Aggregation expression: coerce an AICredits field to double (legacy strings)."""
    return {
        "$convert": {
            "input": field_ref,
            "to": "double",
            "onError": 0.0,
            "onNull": 0.0,
        }
    }


def _inc_ai_credits_fields(subscriber_id: str, deltas: Dict[str, float]) -> bool:
    """
    Atomically increment AICredits.{limit,used,reserved}.

    Uses a pipeline update so legacy string values (e.g. ``"0"``) are coerced to
    double before arithmetic. Returns True when a subscriber document matched.
    """
    if _db is None or not deltas:
        return False

    set_stage: Dict[str, Any] = {}
    for field, delta in deltas.items():
        if field not in ("limit", "used", "reserved"):
            raise ValueError(f"Unsupported AICredits field: {field!r}")
        delta_f = float(delta)
        if delta_f == 0.0:
            continue
        path = f"AICredits.{field}"
        set_stage[path] = {
            "$add": [
                _credit_field_as_double(f"${path}"),
                delta_f,
            ]
        }

    if not set_stage:
        return False

    result = _db[SUBSCRIBERS_COLLECTION].update_one(
        _subscriber_filter(subscriber_id),
        [{"$set": set_stage}],
    )
    return result.matched_count > 0


def _read_subscriber_wallet(subscriber_id: str) -> Dict[str, float]:
    if _db is None:
        return {"limit": float("inf"), "used": 0.0, "reserved": 0.0, "available": float("inf")}
    filt = _subscriber_filter(subscriber_id)
    doc = _db[SUBSCRIBERS_COLLECTION].find_one(filt, {"AICredits": 1})
    if not doc:
        logger.warning("Subscriber %s not found; skipping balance enforcement", subscriber_id)
        return {"limit": float("inf"), "used": 0.0, "reserved": 0.0, "available": float("inf")}
    ai = doc.get("AICredits")
    if not isinstance(ai, dict):
        logger.warning("Subscriber %s missing AICredits; skipping balance enforcement", subscriber_id)
        return {"limit": float("inf"), "used": 0.0, "reserved": 0.0, "available": float("inf")}
    limit = float(ai.get("limit", 0) or 0)
    used = float(ai.get("used", 0) or 0)
    reserved = float(ai.get("reserved", 0) or 0)
    return {
        "limit": limit,
        "used": used,
        "reserved": reserved,
        "available": limit - used - reserved,
    }


def _read_subscriber_available(subscriber_id: str) -> float:
    return _read_subscriber_wallet(subscriber_id)["available"]


def get_subscriber_available_credits(subscriber_id: Optional[str]) -> Optional[float]:
    """Public helper for /estimate response (limit - used - reserved)."""
    if not subscriber_id:
        return None
    wallet = _read_subscriber_wallet(subscriber_id)
    if wallet["available"] == float("inf"):
        return None
    return wallet["available"]


def get_subscriber_credits_snapshot(subscriber_id: str) -> Optional[Dict[str, Any]]:
    """
    Read subscriber wallet for GET /aiqod-agent/agent/credits.

    Returns None when the subscriber document is not found.
    Values are USD (AICredits.limit / used / reserved).
    """
    subscriber_id = (subscriber_id or "").strip()
    if not subscriber_id:
        raise ValueError("subscriberId is required")
    if _db is None:
        raise RuntimeError("Billing DB not configured")

    doc = _db[SUBSCRIBERS_COLLECTION].find_one(
        _subscriber_filter(subscriber_id),
        {"AICredits": 1, "updatedAt": 1, "updated_at": 1},
    )
    if not doc:
        return None

    ai = doc.get("AICredits")
    if not isinstance(ai, dict):
        ai = {}

    limit = float(ai.get("limit", 0) or 0)
    used = float(ai.get("used", 0) or 0)
    reserved = float(ai.get("reserved", 0) or 0)
    balance = limit - used
    available = limit - used - reserved

    updated_at = doc.get("updatedAt") or doc.get("updated_at")
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()

    return {
        "subscriberId": subscriber_id,
        "credits": {
            "limit": limit,
            "balance": balance,
            "used": used,
            "reserved": reserved,
            # Legacy aliases for frontend docs (generate-fe.txt)
            "total_purchased": limit,
            "total_consumed": used,
        },
        "available_credits": available,
        "available_usd": available,
        "currency": "USD",
        "updatedAt": updated_at,
    }


def top_up_subscriber_wallet(
    subscriber_id: str,
    amount_usd: float,
    *,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Increase ``subscribers.AICredits.limit`` (wallet top-up / recharge).

    Used by POST /aiqod-agent/agent/credits/top-up. Does not change ``used`` or
    ``reserved`` — only adds purchasing power so ``available`` increases.
    """
    subscriber_id = (subscriber_id or "").strip()
    if not subscriber_id:
        raise ValueError("subscriberId is required")
    amount = float(amount_usd)
    if amount <= 0:
        raise ValueError("amountUsd must be greater than 0")
    if _db is None:
        raise RuntimeError("Billing DB not configured")

    filt = _subscriber_filter(subscriber_id)
    before = _db[SUBSCRIBERS_COLLECTION].find_one(filt, {"AICredits": 1})
    if not before:
        raise LookupError(f"Subscriber '{subscriber_id}' not found")

    ai = before.get("AICredits")
    if not isinstance(ai, dict):
        ai = {}
    previous_limit = float(ai.get("limit", 0) or 0)

    now = datetime.utcnow()
    if not isinstance(before.get("AICredits"), dict):
        _db[SUBSCRIBERS_COLLECTION].update_one(
            filt,
            {
                "$set": {
                    "AICredits.used": 0.0,
                    "AICredits.reserved": 0.0,
                    "updatedAt": now,
                }
            },
        )
    else:
        _db[SUBSCRIBERS_COLLECTION].update_one(filt, {"$set": {"updatedAt": now}})

    if not _inc_ai_credits_fields(subscriber_id, {"limit": amount}):
        raise LookupError(f"Subscriber '{subscriber_id}' not found")

    snapshot = get_subscriber_credits_snapshot(subscriber_id)
    if snapshot is None:
        raise LookupError(f"Subscriber '{subscriber_id}' not found")

    new_limit = previous_limit + amount
    logger.info(
        "Top-up subscriber %s: +%.4f USD (limit %.4f -> %.4f)",
        subscriber_id,
        amount,
        previous_limit,
        new_limit,
    )
    return {
        **snapshot,
        "topUp": {
            "amountUsd": amount,
            "previousLimitUsd": previous_limit,
            "newLimitUsd": new_limit,
            "note": (note or "").strip() or None,
            "toppedUpAt": now.isoformat(),
        },
    }


def _inc_subscriber_used(subscriber_id: str, cost_usd: float) -> None:
    if _db is None or cost_usd <= 0:
        return
    if not _inc_ai_credits_fields(subscriber_id, {"used": float(cost_usd)}):
        logger.warning("Failed to increment AICredits.used for subscriber %s", subscriber_id)


def _apply_workflow_subscriber_charge(subscriber_id: str, cost_usd: float) -> None:
    """Move cost from reserved hold to actual spend (workflow LLM calls)."""
    if _db is None or cost_usd <= 0:
        return
    cost = float(cost_usd)
    if not _inc_ai_credits_fields(
        subscriber_id,
        {"used": cost, "reserved": -cost},
    ):
        logger.warning(
            "Failed to apply workflow charge for subscriber %s (cost=%s)",
            subscriber_id,
            cost_usd,
        )


def _inc_process_config_spent(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    cost_usd: float,
    field: str = "budgetSpentUsd",
) -> None:
    if _db is None or cost_usd <= 0:
        return
    if field not in ("budgetSpentUsd", "overflowSpentUsd"):
        raise ValueError(f"Unsupported billing field: {field!r}")
    _db[PROCESS_CONFIGS_COLLECTION].update_one(
        _process_configs_query(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        ),
        {"$inc": {field: float(cost_usd)}},
    )


def workflow_billing_snapshot(
    *,
    predicted_cost: float,
    budget_spent_usd: float,
    overflow_spent_usd: float = 0.0,
) -> Dict[str, float]:
    return {
        "predictedCost": float(predicted_cost),
        "budgetSpentUsd": float(budget_spent_usd),
        "overflowSpentUsd": float(overflow_spent_usd),
        "totalWorkflowSpentUsd": float(budget_spent_usd) + float(overflow_spent_usd),
        "capRemainingUsd": max(0.0, float(predicted_cost) - float(budget_spent_usd)),
    }


def workflow_billing_from_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, float]:
    if not doc:
        return {}
    return workflow_billing_snapshot(
        predicted_cost=float(doc.get("predictedCost") or 0),
        budget_spent_usd=float(doc.get("budgetSpentUsd") or 0),
        overflow_spent_usd=float(doc.get("overflowSpentUsd") or 0),
    )


def register_workflow_process_config(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: str,
    user_id: Optional[str],
    predicted_cost: float,
    workflow_name: Optional[str] = None,
    selected_name: Optional[str] = None,
    selected_type: Optional[str] = None,
    selected_category: Optional[str] = None,
    total_input_tokens: Optional[int] = None,
    total_output_tokens: Optional[int] = None,
    suggested_reports: Optional[list] = None,
    kind: str = "workflow",
    reserve_subscriber_credits: bool = False,
) -> Dict[str, Any]:
    """
    Create or refresh Process_Configs after queue admission.
    Queue service must have already incremented subscribers.AICredits.reserved.

    ``kind`` distinguishes a standalone dashboard-creation record ("dashboard")
    from a regular workflow record ("workflow"). Both share the same
    Process_Configs collection / billing plumbing (engineType="agentic_graph",
    agentic_process.workflow_id=project_id) so the queue, billing and
    reservation-release logic is fully reused between the two flows.
    """
    if _db is None:
        raise RuntimeError("Billing DB not configured")
    project_id = (project_id or "").strip()
    if not project_id:
        raise PredictedCostMissingError("projectId is required.")
    predicted_f = float(predicted_cost or 0.0)
    if predicted_f <= 0:
        raise PredictedCostMissingError("predictedCost must be greater than zero.")

    name = workflow_name or selected_name or "workflow"
    now = datetime.utcnow()
    query = _process_configs_query(
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    existing = _db[PROCESS_CONFIGS_COLLECTION].find_one(query)
    if existing:
        status = existing.get("reservationStatus")
        if status == RESERVATION_ACTIVE:
            existing_predicted = float(existing.get("predictedCost") or 0)
            if abs(existing_predicted - predicted_f) > 1e-6:
                raise WorkflowNotRegisteredError(
                    f"Workflow '{project_id}' already registered with predictedCost "
                    f"{existing_predicted:.4f}; requested {predicted_f:.4f}."
                )
            return {
                "projectId": project_id,
                "predictedCost": existing_predicted,
                "reservedUsd": float(existing.get("reservedUsd") or existing_predicted),
                "reservationStatus": RESERVATION_ACTIVE,
                "created": False,
            }

    if reserve_subscriber_credits:
        wallet = _read_subscriber_wallet(subscriber_id)
        if wallet["available"] != float("inf") and wallet["available"] < predicted_f:
            raise InsufficientSubscriberBalanceError(
                f"Insufficient balance to register workflow '{project_id}': "
                f"required {predicted_f:.4f}, available {wallet['available']:.4f}.",
                available=wallet["available"],
                required=predicted_f,
            )
    else:
        # Defense in depth when hold was reserved externally (e.g. queue service).
        available = _read_subscriber_available(subscriber_id)
        if available != float("inf") and available < 0:
            raise InsufficientSubscriberBalanceError(
                "Subscriber balance is overdrawn (limit - used - reserved < 0).",
                available=available,
                required=0.0,
            )

    _db[PROCESS_CONFIGS_COLLECTION].update_one(
        query,
        {
            "$set": {
                **_tenant_query(org_id=org_id, subscriber_id=subscriber_id, user_id=user_id),
                "engineType": ENGINE_TYPE,
                "selectedName": selected_name,
                "selectedType": selected_type,
                "selectedCategory": selected_category,
                "updated_at": now,
                "predictedCost": predicted_f,
                "reservedUsd": predicted_f,
                "budgetSpentUsd": 0.0,
                "overflowSpentUsd": 0.0,
                "reservationStatus": RESERVATION_ACTIVE,
                "totalInputTokens": total_input_tokens,
                "totalOutputTokens": total_output_tokens,
                "agentic_process": {
                    "workflow_id": project_id,
                    "workflowName": name,
                    "projectId": project_id,
                    "kind": kind,
                    "status": "queued",
                    "suggestedReports": suggested_reports or [],
                    "reports": [],
                },
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    if reserve_subscriber_credits:
        if not _inc_ai_credits_fields(subscriber_id, {"reserved": predicted_f}):
            logger.warning(
                "Failed to increment AICredits.reserved for subscriber %s during register",
                subscriber_id,
            )

    return {
        "projectId": project_id,
        "predictedCost": predicted_f,
        "reservedUsd": predicted_f,
        "reservationStatus": RESERVATION_ACTIVE,
        "created": True,
        "subscriberCreditsReserved": reserve_subscriber_credits,
    }


def reactivate_workflow_reservation(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: str,
    user_id: Optional[str],
    additional_predicted_cost: float,
) -> Dict[str, Any]:
    """
    Re-open a previously released workflow reservation for a follow-up action
    (e.g. updating an already-executed workflow).

    Unlike register_workflow_process_config (which assumes the queue service
    already incremented subscribers.AICredits.reserved at admission time),
    this function performs that reservation itself, since updates have no
    separate queue-admission step. Idempotent no-op if already active.
    """
    if _db is None:
        raise RuntimeError("Billing DB not configured")
    project_id = (project_id or "").strip()
    if not project_id:
        raise PredictedCostMissingError("projectId is required.")
    additional_f = float(additional_predicted_cost or 0.0)
    if additional_f <= 0:
        raise PredictedCostMissingError("additionalPredictedCost must be greater than zero.")

    query = _process_configs_query(
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    doc = _db[PROCESS_CONFIGS_COLLECTION].find_one(query)
    if not doc:
        raise WorkflowNotRegisteredError(
            f"Workflow '{project_id}' is not registered in Process_Configs."
        )

    if doc.get("reservationStatus") == RESERVATION_ACTIVE:
        return {
            "projectId": project_id,
            "predictedCost": float(doc.get("predictedCost") or 0.0),
            "reservedUsd": float(doc.get("reservedUsd") or 0.0),
            "reservationStatus": RESERVATION_ACTIVE,
            "reactivated": False,
        }

    wallet = _read_subscriber_wallet(subscriber_id)
    if wallet["available"] != float("inf") and wallet["available"] < additional_f:
        raise InsufficientSubscriberBalanceError(
            f"Insufficient balance to reopen workflow '{project_id}': "
            f"required {additional_f:.4f}, available {wallet['available']:.4f}.",
            available=wallet["available"],
            required=additional_f,
        )

    if not _inc_ai_credits_fields(subscriber_id, {"reserved": additional_f}):
        logger.warning(
            "Failed to increment AICredits.reserved for subscriber %s during reactivation",
            subscriber_id,
        )

    already_spent = float(doc.get("budgetSpentUsd") or 0.0)
    new_predicted = already_spent + additional_f
    now = datetime.utcnow()

    _db[PROCESS_CONFIGS_COLLECTION].update_one(
        query,
        {
            "$set": {
                "predictedCost": new_predicted,
                "reservedUsd": additional_f,
                "reservationStatus": RESERVATION_ACTIVE,
                "updated_at": now,
            }
        },
    )
    logger.info(
        "Reactivated workflow reservation projectId=%s additional_usd=%.6f "
        "already_spent=%.6f new_predicted=%.6f",
        project_id,
        additional_f,
        already_spent,
        new_predicted,
    )
    return {
        "projectId": project_id,
        "predictedCost": new_predicted,
        "reservedUsd": additional_f,
        "reservationStatus": RESERVATION_ACTIVE,
        "reactivated": True,
    }


def release_workflow_reservation(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
) -> float:
    """
    Release unused hold (predictedCost - budgetSpentUsd) from subscribers.AICredits.reserved.
    Idempotent when reservation is already released.
    Returns USD released.
    """
    if _db is None or not project_id or not subscriber_id:
        return 0.0

    query = _process_configs_query(
        project_id=project_id.strip(),
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    doc = _db[PROCESS_CONFIGS_COLLECTION].find_one(query)
    if not doc:
        return 0.0
    if doc.get("reservationStatus") != RESERVATION_ACTIVE:
        return 0.0

    predicted = float(doc.get("predictedCost") or 0.0)
    spent = float(doc.get("budgetSpentUsd") or 0.0)
    release = max(0.0, predicted - spent)

    if release > 0:
        _inc_ai_credits_fields(subscriber_id, {"reserved": -float(release)})

    _db[PROCESS_CONFIGS_COLLECTION].update_one(
        query,
        {
            "$set": {
                "reservationStatus": RESERVATION_RELEASED,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    logger.info(
        "Released workflow reservation projectId=%s release_usd=%.6f spent=%.6f predicted=%.6f",
        project_id,
        release,
        spent,
        predicted,
    )
    return release


@asynccontextmanager
async def workflow_reservation_release_scope(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
):
    """Release unused workflow hold when a scoped block ends (e.g. /execute).

    On BillingError: keeps the reservation active so the workflow can be resumed.
    On any other exit (success or non-billing error): releases the unused hold.

    Design note: the caller (agentic_plan_execute) catches BillingError inside the
    with-block, sets ``billing_context.billing_error_occurred = True``, writes the
    checkpoint, then raises HTTPException.  The scope manager reads the flag in its
    finally block — no need for the BillingError exception to propagate through here.

    Async so that the sync PyMongo call in release_workflow_reservation runs in a
    thread (via asyncio.to_thread) rather than blocking the asyncio event loop.
    Blocking the event loop in Starlette 0.50 (anyio) stalls the HTTP response
    pipeline indefinitely even after all work is complete.
    """
    _t_scope = time.monotonic()
    logger.info(
        "[billing/resume] entering workflow_reservation_release_scope project_id=%s "
        "subscriber_id=%s",
        project_id,
        subscriber_id,
    )
    try:
        yield
    finally:
        ctx = _billing_context.get()
        billing_error = ctx is not None and ctx.billing_error_occurred
        logger.info(
            "[billing/resume] exiting workflow_reservation_release_scope project_id=%s "
            "billing_error=%s scope_elapsed=%.2fs",
            project_id,
            billing_error,
            time.monotonic() - _t_scope,
        )
        if not billing_error:
            # Normal exit (success or non-billing error) → release unused hold.
            try:
                _t_release = time.monotonic()
                logger.info(
                    "[billing/resume] releasing workflow reservation project_id=%s",
                    project_id,
                )
                released = await _asyncio.to_thread(
                    release_workflow_reservation,
                    project_id=project_id,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                logger.info(
                    "[billing/resume] workflow reservation released project_id=%s "
                    "released_usd=%.6f duration=%.2fs",
                    project_id,
                    released,
                    time.monotonic() - _t_release,
                )
            except Exception as exc:
                logger.warning(
                    "[billing/resume] failed to release workflow reservation project_id=%s: %s",
                    project_id,
                    exc,
                )
        else:
            # Credit exhaustion — keep reservation active for resume.
            logger.info(
                "[billing/resume] BillingError — reservation kept active for resume. "
                "project_id=%s",
                project_id,
            )


def init_subscriber_billing(
    subscriber_id: Optional[str],
    *,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    if not subscriber_id:
        return
    available = _read_subscriber_available(subscriber_id)
    _billing_context.set(
        BillingContext(
            subscriber_id=subscriber_id,
            org_id=org_id,
            user_id=user_id,
            subscriber_available_usd=available,
            enforce_workflow_cap=False,
            charge_subscriber=True,
        )
    )


def init_workflow_billing(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: Optional[str],
    user_id: Optional[str],
    strict_predicted_cost: bool = True,
    use_subscriber_credits: bool = False,
) -> BillingContext:
    if not project_id:
        raise PredictedCostMissingError("projectId is required for workflow billing.")
    if not subscriber_id:
        raise InsufficientSubscriberBalanceError(
            "subscriberId is required for workflow billing.",
            available=0.0,
            required=0.0,
        )
    if _db is None:
        logger.warning("Billing DB not configured; workflow billing disabled for this request.")
        ctx = BillingContext(
            subscriber_id=subscriber_id,
            org_id=org_id,
            user_id=user_id,
            project_id=project_id,
            enforce_workflow_cap=False,
            charge_subscriber=False,
        )
        _billing_context.set(ctx)
        return ctx

    doc = _db[PROCESS_CONFIGS_COLLECTION].find_one(
        _process_configs_query(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
    )
    if not doc or doc.get("predictedCost") is None:
        if strict_predicted_cost:
            raise WorkflowNotRegisteredError(
                "Workflow is not registered in Process_Configs. "
                "Call POST /agentic/workflow/register after queue admission."
            )
        predicted_f = 0.0
        spent = 0.0
        overflow_spent = 0.0
    else:
        if doc.get("reservationStatus") not in (None, RESERVATION_ACTIVE):
            if doc.get("creditPauseStatus") != "paused_credit":
                raise WorkflowNotRegisteredError(
                    f"Workflow reservation is not active (status={doc.get('reservationStatus')!r})."
                )
            _db[PROCESS_CONFIGS_COLLECTION].update_one(
                _process_configs_query(
                    project_id=project_id,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                ),
                {"$set": {"reservationStatus": RESERVATION_ACTIVE}},
            )
        predicted_f = float(doc.get("predictedCost") or 0.0)
        spent = float(doc.get("budgetSpentUsd", 0) or 0)
        overflow_spent = float(doc.get("overflowSpentUsd", 0) or 0)

    available = _read_subscriber_available(subscriber_id)

    # If use_subscriber_credits is True, allow workflow to proceed even if workflow budget is exhausted
    # as long as subscriber has available credits. Skip the workflow budget exhausted check.
    if strict_predicted_cost and predicted_f > 0 and spent >= predicted_f and not use_subscriber_credits:
        raise WorkflowBudgetExhaustedError(
            f"Workflow budget exhausted ({spent:.4f} / {predicted_f:.4f} USD).",
            predicted_cost=predicted_f,
            budget_spent=spent,
        )

    ctx = BillingContext(
        subscriber_id=subscriber_id,
        org_id=org_id,
        user_id=user_id,
        project_id=project_id,
        predicted_cost=predicted_f if predicted_f > 0 else None,
        budget_spent_usd=spent,
        overflow_spent_usd=overflow_spent,
        subscriber_available_usd=available,
        enforce_workflow_cap=bool(strict_predicted_cost and predicted_f > 0 and not use_subscriber_credits),
        charge_subscriber=True,
    )
    _billing_context.set(ctx)
    return ctx


def _refresh_workflow_spent(ctx: BillingContext) -> None:
    if not ctx.enforce_workflow_cap or not ctx.project_id or _db is None:
        return
    doc = _db[PROCESS_CONFIGS_COLLECTION].find_one(
        _process_configs_query(
            project_id=ctx.project_id,
            org_id=ctx.org_id,
            subscriber_id=ctx.subscriber_id,
            user_id=ctx.user_id,
        ),
        {"budgetSpentUsd": 1},
    )
    if doc:
        ctx.budget_spent_usd = float(doc.get("budgetSpentUsd", 0) or 0)


def check_before_llm_call() -> None:
    ctx = _billing_context.get()
    if ctx is None or not ctx.charge_subscriber:
        return

    if ctx.enforce_workflow_cap:
        _refresh_workflow_spent(ctx)

    ctx.subscriber_available_usd = _read_subscriber_available(ctx.subscriber_id)

    # Workflow runs against an already-reserved hold; do not block on wallet-wide
    # available==0 when other jobs hold the rest of the subscriber balance.
    if not ctx.enforce_workflow_cap and ctx.subscriber_remaining_usd <= 0:
        raise InsufficientSubscriberBalanceError(
            "Subscriber balance exhausted (limit - used - reserved).",
            available=max(0.0, ctx.subscriber_remaining_usd),
            required=0.0,
        )

    if ctx.enforce_workflow_cap and ctx.predicted_cost is not None:
        remaining = ctx.workflow_remaining_usd or 0.0
        if remaining <= 0:
            raise WorkflowBudgetExhaustedError(
                f"Workflow budget exhausted ({ctx.budget_spent_usd:.4f} / "
                f"{ctx.predicted_cost:.4f} USD).",
                predicted_cost=float(ctx.predicted_cost),
                budget_spent=float(ctx.budget_spent_usd),
            )


def apply_llm_charge(cost_usd: float) -> None:
    cost = float(cost_usd or 0.0)
    if cost <= 0:
        return

    ctx = _billing_context.get()
    if ctx is None or not ctx.charge_subscriber:
        return

    if ctx.enforce_workflow_cap and ctx.project_id:
        _apply_workflow_subscriber_charge(ctx.subscriber_id, cost)
        _inc_process_config_spent(
            project_id=ctx.project_id,
            org_id=ctx.org_id,
            subscriber_id=ctx.subscriber_id,
            user_id=ctx.user_id,
            cost_usd=cost,
            field="budgetSpentUsd",
        )
        ctx.budget_spent_usd += cost
    else:
        _inc_subscriber_used(ctx.subscriber_id, cost)
        if ctx.project_id:
            _inc_process_config_spent(
                project_id=ctx.project_id,
                org_id=ctx.org_id,
                subscriber_id=ctx.subscriber_id,
                user_id=ctx.user_id,
                cost_usd=cost,
                field="overflowSpentUsd",
            )
            ctx.overflow_spent_usd += cost

    ctx.subscriber_available_usd = max(0.0, ctx.subscriber_available_usd - cost)


def init_billing_for_resume(
    *,
    project_id: str,
    org_id: Optional[str],
    subscriber_id: str,
    user_id: Optional[str],
) -> BillingContext:
    """
    Initialise billing for a resume call on a paused_credit workflow.

    Scenario B (cap not exhausted): continue workflow-cap mode.
    Scenario A (cap exhausted): switch to wallet-only billing.
    """
    if _db is None:
        raise RuntimeError("Billing DB not configured")

    logger.info(
        "[billing/resume] init_billing_for_resume start project_id=%s subscriber_id=%s",
        project_id,
        subscriber_id,
    )
    query = _process_configs_query(
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    doc = _db[PROCESS_CONFIGS_COLLECTION].find_one(query)
    if not doc:
        raise WorkflowNotRegisteredError(
            f"Workflow '{project_id}' not found in Process_Configs."
        )

    # Note: agentic_process.status is NOT checked here. The caller (/resume endpoint)
    # already validates the checkpoint status. External job runners can freely overwrite
    # agentic_process.status; the checkpoint in workflow_build_checkpoints is the single
    # authoritative source for pause state.
    predicted = float(doc.get("predictedCost") or 0)
    spent = float(doc.get("budgetSpentUsd") or 0)
    overflow_spent = float(doc.get("overflowSpentUsd") or 0)
    remaining_hold = predicted - spent
    available = _read_subscriber_available(subscriber_id)

    if remaining_hold > 0.0001:
        logger.info(
            "init_billing_for_resume: cap has %.4f remaining — workflow-cap mode. "
            "project_id=%s",
            remaining_hold,
            project_id,
        )
        ctx = BillingContext(
            subscriber_id=subscriber_id,
            org_id=org_id,
            user_id=user_id,
            project_id=project_id,
            predicted_cost=predicted,
            budget_spent_usd=spent,
            overflow_spent_usd=overflow_spent,
            subscriber_available_usd=available,
            enforce_workflow_cap=True,
            charge_subscriber=True,
        )
    else:
        logger.info(
            "init_billing_for_resume: cap exhausted — wallet-only mode. project_id=%s",
            project_id,
        )
        release_workflow_reservation(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        if available <= 0:
            raise InsufficientSubscriberBalanceError(
                "Subscriber wallet is still empty after recharge check.",
                available=available,
                required=0.0,
            )
        ctx = BillingContext(
            subscriber_id=subscriber_id,
            org_id=org_id,
            user_id=user_id,
            project_id=project_id,
            predicted_cost=predicted,
            budget_spent_usd=spent,
            overflow_spent_usd=overflow_spent,
            subscriber_available_usd=available,
            enforce_workflow_cap=False,
            charge_subscriber=True,
        )

    _db[PROCESS_CONFIGS_COLLECTION].update_one(
        query,
        {
            "$set": {
                "reservationStatus": RESERVATION_ACTIVE,
                "agentic_process.status": "building",
                "updated_at": datetime.utcnow(),
            }
        },
    )
    _billing_context.set(ctx)
    logger.info(
        "[billing/resume] init_billing_for_resume complete project_id=%s "
        "enforce_workflow_cap=%s predicted=%.4f spent=%.4f available=%.4f",
        project_id,
        ctx.enforce_workflow_cap,
        predicted,
        spent,
        available,
    )
    return ctx


def path_uses_subscriber_billing(path: str) -> bool:
    if not path:
        return False
    normalized = path.rstrip("/")
    for suffix in SUBSCRIBER_BILLING_PATH_SUFFIXES:
        if normalized.endswith(suffix):
            return True
    return False


def billing_error_to_http_detail(exc: BillingError) -> Dict[str, Any]:
    detail: Dict[str, Any] = {
        "success": False,
        "error": str(exc),
        "code": exc.code,
    }
    if isinstance(exc, InsufficientSubscriberBalanceError):
        detail["available_usd"] = exc.available
        detail["required_usd"] = exc.required
    elif isinstance(exc, WorkflowBudgetExhaustedError):
        detail["predicted_cost"] = exc.predicted_cost
        detail["budget_spent_usd"] = exc.budget_spent

    ctx = get_billing_context()
    if ctx is not None and ctx.project_id:
        detail.update(
            workflow_billing_snapshot(
                predicted_cost=float(ctx.predicted_cost or 0),
                budget_spent_usd=ctx.budget_spent_usd,
                overflow_spent_usd=ctx.overflow_spent_usd,
            )
        )
    return detail
