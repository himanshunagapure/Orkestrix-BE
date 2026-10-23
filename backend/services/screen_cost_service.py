import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from services.database import Database

logger = logging.getLogger(__name__)

SCREEN_COST_COLLECTION = "screen_cost_summary"
LLM_TOKEN_COUNT_COLLECTION = "llm_token_count"


def _make_object_id(value: Optional[str]) -> Optional[ObjectId]:
    """Utility: safely convert a string to ObjectId, returning None on failure."""
    if not value:
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to convert value %r to ObjectId: %s", value, exc)
        return None


async def aggregate_screen_cost(
    project_id: str,
    screen_id: str,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    status: str = "completed",
) -> Dict[str, Any]:
    """
    Aggregate LLM token/cost usage for a single screen and upsert into screen_cost_summary.

    Source collection: llm_token_count (written by LLMTokenTracker).

    Returns the aggregated summary document (without Mongo _id).
    """
    if not project_id or not screen_id:
        raise ValueError("Both project_id and screen_id are required to aggregate screen cost.")

    llm_coll = Database.get_collection(LLM_TOKEN_COUNT_COLLECTION)

    pipeline = [
        {
            "$match": {
                "project_id": project_id,
                "screen_id": screen_id,
            }
        },
        {
            "$group": {
                "_id": {"project_id": "$project_id", "screen_id": "$screen_id"},
                "total_prompt_tokens": {"$sum": {"$ifNull": ["$prompt_tokens", 0]}},
                "total_completion_tokens": {"$sum": {"$ifNull": ["$completion_tokens", 0]}},
                "total_tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                "total_cost_usd": {
                    "$sum": {
                        "$ifNull": ["$pricing.total_cost_usd", 0.0],
                    }
                },
                "total_llm_calls": {"$sum": 1},
                "retry_calls": {
                    "$sum": {
                        "$cond": [
                            {"$ifNull": ["$is_retry", False]},
                            1,
                            0,
                        ]
                    }
                },
                "started_at": {"$min": {"$ifNull": ["$createdAt", None]}},
                "completed_at": {"$max": {"$ifNull": ["$finishedAt", None]}},
                "orgId": {"$first": {"$ifNull": ["$orgId", None]}},
                "subscriberId": {"$first": {"$ifNull": ["$subscriberId", None]}},
                "user_prompt": {"$first": {"$ifNull": ["$user_prompt", None]}},
            }
        },
    ]

    cursor = llm_coll.aggregate(pipeline)
    results: List[Dict[str, Any]] = await cursor.to_list(length=1)

    if not results:
        # No LLM calls yet for this screen – nothing to aggregate.
        logger.info(
            "No llm_token_count records found for project_id=%s, screen_id=%s",
            project_id,
            screen_id,
        )
        return {}

    row = results[0]

    org_obj = row.get("orgId") or _make_object_id(org_id)
    sub_obj = row.get("subscriberId") or _make_object_id(subscriber_id)

    summary: Dict[str, Any] = {
        "project_id": project_id,
        "screen_id": screen_id,
        "orgId": org_obj,
        "subscriberId": sub_obj,
        "total_prompt_tokens": int(row.get("total_prompt_tokens", 0) or 0),
        "total_completion_tokens": int(row.get("total_completion_tokens", 0) or 0),
        "total_tokens": int(row.get("total_tokens", 0) or 0),
        "total_cost_usd": float(row.get("total_cost_usd", 0.0) or 0.0),
        "total_llm_calls": int(row.get("total_llm_calls", 0) or 0),
        "retry_calls": int(row.get("retry_calls", 0) or 0),
        "status": status,
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "user_prompt": row.get("user_prompt"),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    summary_coll = Database.get_collection(SCREEN_COST_COLLECTION)
    await summary_coll.update_one(
        {"project_id": project_id, "screen_id": screen_id},
        {"$set": summary},
        upsert=True,
    )

    # Do not return Mongo's internal _id to callers – keep the API clean.
    return summary


async def get_screen_summary(
    project_id: str,
    screen_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch screen cost summary for a given (project_id, screen_id).
    Falls back to on-demand aggregation when no summary exists yet.
    """
    if not project_id or not screen_id:
        return None

    summary_coll = Database.get_collection(SCREEN_COST_COLLECTION)
    existing = await summary_coll.find_one(
        {"project_id": project_id, "screen_id": screen_id}
    )
    if existing:
        # Normalize for API consumers
        existing["_id"] = str(existing["_id"])
        return existing

    # No pre-computed summary – aggregate on-demand.
    summary = await aggregate_screen_cost(project_id=project_id, screen_id=screen_id)
    if not summary:
        return None

    return summary


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    """Compute a simple percentile on a sorted list of numbers."""
    if not values:
        return None
    if percentile <= 0:
        return values[0]
    if percentile >= 100:
        return values[-1]
    k = (len(values) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1


async def calculate_historical_stats(
    days: int = 90,
) -> Dict[str, Any]:
    """
    Analyze historical screen generations over the last `days` days.

    Uses screen_cost_summary as the primary source; if it's empty, falls back
    to aggregating from llm_token_count.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    summary_coll = Database.get_collection(SCREEN_COST_COLLECTION)

    # Prefer screen_cost_summary – one row per screen.
    cursor = summary_coll.find(
        {
            "completed_at": {"$gte": cutoff.isoformat()},
        }
    )
    screens: List[Dict[str, Any]] = await cursor.to_list(length=None)

    if not screens:
        # Fallback: derive per-screen totals directly from llm_token_count.
        logger.info(
            "No documents in %s for last %s days – deriving stats from %s",
            SCREEN_COST_COLLECTION,
            days,
            LLM_TOKEN_COUNT_COLLECTION,
        )
        llm_coll = Database.get_collection(LLM_TOKEN_COUNT_COLLECTION)
        pipeline = [
            {
                "$match": {
                    "finishedAt": {"$gte": cutoff.isoformat()},
                }
            },
            {
                "$group": {
                    "_id": {
                        "project_id": "$project_id",
                        "screen_id": "$screen_id",
                    },
                    "total_tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "total_cost_usd": {
                        "$sum": {
                            "$ifNull": ["$pricing.total_cost_usd", 0.0],
                        }
                    },
                }
            },
        ]
        cursor = llm_coll.aggregate(pipeline)
        grouped = await cursor.to_list(length=None)
        screens = [
            {
                "project_id": g["_id"]["project_id"],
                "screen_id": g["_id"]["screen_id"],
                "total_tokens": g.get("total_tokens", 0),
                "total_cost_usd": g.get("total_cost_usd", 0.0),
            }
            for g in grouped
        ]

    if not screens:
        return {
            "total_screens": 0,
            "avg_tokens_per_screen": 0,
            "avg_cost_per_screen": 0,
            "min_cost": 0,
            "max_cost": 0,
            "median_cost": 0,
            "percentiles": {},
        }

    total_screens = len(screens)
    token_counts = [float(s.get("total_tokens", 0) or 0) for s in screens]
    costs = [float(s.get("total_cost_usd", 0.0) or 0.0) for s in screens]

    token_counts.sort()
    costs.sort()

    avg_tokens = sum(token_counts) / total_screens
    avg_cost = sum(costs) / total_screens

    median_cost = _percentile(costs, 50) or 0.0

    percentiles = {
        "p25": _percentile(costs, 25) or 0.0,
        "p50": median_cost,
        "p75": _percentile(costs, 75) or 0.0,
        "p90": _percentile(costs, 90) or 0.0,
        "p95": _percentile(costs, 95) or 0.0,
    }

    return {
        "total_screens": total_screens,
        "avg_tokens_per_screen": avg_tokens,
        "avg_cost_per_screen": avg_cost,
        "min_cost": min(costs),
        "max_cost": max(costs),
        "median_cost": median_cost,
        "percentiles": percentiles,
    }

