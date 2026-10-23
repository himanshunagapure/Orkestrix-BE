"""
LLM Token Tracker Service
Tracks and records every LLM API call to MongoDB for cost analysis and monitoring.
"""
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Dict, Any, Optional
from pymongo import MongoClient
from bson import ObjectId
import json
from pathlib import Path
# Request-scoped prompts: set when generate-angular-app or update-angular-screen runs,
# so every record_llm_call during that request gets user_prompt and enhanced_prompt.
_request_user_prompt: ContextVar[Optional[str]] = ContextVar("request_user_prompt", default=None)
_request_enhanced_prompt: ContextVar[Optional[str]] = ContextVar("request_enhanced_prompt", default=None)

# Max length to store per prompt (avoid huge documents)
_MAX_PROMPT_STORAGE_LEN = 40_000

logger = logging.getLogger(__name__)

# Load config
_config = None
def _load_config():
    global _config
    if _config is None:
        try:
            config_path = Path(__file__).parent.parent / 'config.json'
            with open(config_path, 'r') as f:
                _config = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config.json: {e}")
            _config = {}
    return _config

# Model pricing per 1K tokens (USD)
#
# Notes:
# - Some models have tiered pricing based on prompt (input) token count.
# - For tiered models, `tiers` is evaluated against `input_tokens` (prompt tokens).
MODEL_PRICING: Dict[str, Dict[str, Any]] = {
    # Historical — kept for cost replay of older llm_token records
    "gemini-2.5-flash": {
        "input": 0.00030,   # $0.30 per 1M input tokens
        "output": 0.0025    # $2.50 per 1M output tokens
    },
    # Default replacement for gemini-2.5-flash (same price point)
    "gemini-3.5-flash-lite": {
        "input": 0.00030,   # $0.30 per 1M input tokens
        "output": 0.0025    # $2.50 per 1M output tokens
    },
    # Moderate reasoning (Gemini 3 Flash)
    "gemini-3-flash-preview": {
        "input": 0.00050,   # $0.50 per 1M input tokens
        "output": 0.0030    # $3.00 per 1M output tokens
    },
    # Complex coding / agentic multi-step
    "gemini-3.6-flash": {
        "input": 0.00150,   # $1.50 per 1M input tokens
        "output": 0.0075    # $7.50 per 1M output tokens
    },
    "gemini-2.5-pro": {
        "input": 0.00125,   # $1.25 per 1M input tokens
        "output": 0.010     # $10.00 per 1M output tokens
    },
    "gemini-3-pro-preview": {
        "input": 0.002,   # $2 per 1M input tokens
        "output": 0.012     # $12 per 1M output tokens
    },
    "gemini-3.5-flash": {
        "input": 0.0015,    # $1.50 per 1M input tokens
        "output": 0.0090    # $9.00 per 1M output tokens
    },
    # Gemini 3.1 Pro (tiered by prompt tokens)
    # Input:  $2.00 / 1M tokens (<= 200k prompt tokens), else $4.00 / 1M
    # Output: $12.00 / 1M tokens (<= 200k prompt tokens), else $18.00 / 1M
    # Converted to per-1K rates: 0.002/0.004 input, 0.012/0.018 output.
    "gemini-3.1-pro": {
        "tiers": [
            {"max_prompt_tokens": 200_000, "input": 0.002, "output": 0.012},
            {"max_prompt_tokens": None, "input": 0.004, "output": 0.018},
        ]
    },
    # Some call sites may record the preview variant explicitly.
    "gemini-3.1-pro-preview": {
        "tiers": [
            {"max_prompt_tokens": 200_000, "input": 0.002, "output": 0.012},
            {"max_prompt_tokens": None, "input": 0.004, "output": 0.018},
        ]
    },
    # Add more models as needed
}


def extract_billable_tokens_from_usage_metadata(usage: Any) -> Dict[str, int]:
    """
    Map Gemini usage_metadata to billable token counts.

    - prompt_token_count → billable input
    - candidates_token_count + thoughts_token_count → billable output
      (equivalently: total_token_count - prompt_token_count)
    """
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
        }

    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_token_count", 0) or 0)
        candidates = int(usage.get("candidates_token_count", 0) or 0)
        thoughts = int(usage.get("thoughts_token_count", 0) or 0)
        total = int(usage.get("total_token_count", 0) or 0)
    else:
        prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
        candidates = int(getattr(usage, "candidates_token_count", 0) or 0)
        thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
        total = int(getattr(usage, "total_token_count", 0) or 0)

    return _resolve_billable_token_counts(
        prompt_tokens=prompt,
        candidates_tokens=candidates,
        thoughts_tokens=thoughts,
        total_tokens=total,
    )


def _resolve_billable_token_counts(
    *,
    prompt_tokens: int,
    candidates_tokens: int = 0,
    thoughts_tokens: int = 0,
    total_tokens: int = 0,
    completion_tokens: Optional[int] = None,
) -> Dict[str, int]:
    """
    Resolve billable input/output tokens for Gemini (including reasoning models).

    Reasoning/thinking tokens are billed at the output rate. When total_token_count
    is present, billable output is total - prompt (covers candidates + thoughts).
    """
    prompt = max(0, int(prompt_tokens or 0))
    candidates = max(0, int(candidates_tokens or 0))
    if completion_tokens is not None and candidates == 0:
        candidates = max(0, int(completion_tokens or 0))
    thoughts = max(0, int(thoughts_tokens or 0))
    total = max(0, int(total_tokens or 0))

    if total > prompt:
        billable_output = total - prompt
    elif thoughts > 0:
        billable_output = candidates + thoughts
    else:
        billable_output = candidates

    if thoughts == 0 and billable_output > candidates:
        thoughts = billable_output - candidates

    if total <= 0:
        total = prompt + billable_output

    return {
        "prompt_tokens": prompt,
        "completion_tokens": billable_output,
        "candidates_tokens": candidates,
        "thoughts_tokens": thoughts,
        "total_tokens": total,
    }


class LLMTokenTracker:
    """Singleton service to track LLM token usage"""
    
    _instance = None
    _db = None
    _collection = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LLMTokenTracker, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        """Initialize MongoDB connection"""
        try:
            config = _load_config()
            client = MongoClient(config.get('devDbLink'))
            self._db = client[config.get('devDb')]
            self._collection = self._db['llm_token_count']
            logger.info("LLM Token Tracker initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize LLM Token Tracker: {e}")
    
    @staticmethod
    def _resolve_billable_tokens(
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        thoughts_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """Resolve billable counts from call arguments and optional usage_metadata in metadata."""
        candidates = max(0, int(completion_tokens or 0))
        thoughts = thoughts_tokens

        if thoughts is None and metadata:
            usage = metadata.get("usage_metadata") or metadata.get("usage")
            if usage is not None:
                extracted = extract_billable_tokens_from_usage_metadata(usage)
                return extracted

        return _resolve_billable_token_counts(
            prompt_tokens=prompt_tokens,
            candidates_tokens=candidates,
            thoughts_tokens=int(thoughts or 0),
            total_tokens=total_tokens,
            completion_tokens=completion_tokens,
        )

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> Dict[str, Any]:
        """Calculate cost based on model pricing (billable input/output token counts)."""
        pricing_cfg = MODEL_PRICING.get(model, MODEL_PRICING.get("gemini-3.5-flash-lite"))

        # Flat pricing (default)
        rate_in = float(pricing_cfg.get("input", 0.0) or 0.0)
        rate_out = float(pricing_cfg.get("output", 0.0) or 0.0)

        # Tiered pricing based on prompt (input) tokens
        tiers = pricing_cfg.get("tiers")
        if isinstance(tiers, list) and tiers:
            for tier in tiers:
                max_prompt = tier.get("max_prompt_tokens", None)
                if max_prompt is None or int(input_tokens or 0) <= int(max_prompt):
                    rate_in = float(tier.get("input", rate_in) or rate_in)
                    rate_out = float(tier.get("output", rate_out) or rate_out)
                    break

        input_cost = (float(input_tokens or 0) / 1000.0) * rate_in
        output_cost = (float(output_tokens or 0) / 1000.0) * rate_out
        total_cost = input_cost + output_cost
        
        return {
            "rate_per_1k_input_tokens": rate_in,
            "rate_per_1k_output_tokens": rate_out,
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(total_cost, 6),
            "currency": "USD"
        }

    @staticmethod
    def _truncate_prompt(text: Optional[str]) -> Optional[str]:
        """Truncate prompt for storage to avoid huge documents."""
        if text is None:
            return None
        if len(text) <= _MAX_PROMPT_STORAGE_LEN:
            return text
        return text[:_MAX_PROMPT_STORAGE_LEN] + " ... [truncated]"

    def set_request_prompts(
        self,
        user_prompt: Optional[str] = None,
        enhanced_prompt: Optional[str] = None,
    ) -> None:
        """
        Set request-scoped prompts for the current async context (e.g. generate-angular-app
        or update-angular-screen). Every record_llm_call during this request will include
        these fields. Pass None for a parameter to leave that value unchanged.
        """
        if user_prompt is not None:
            _request_user_prompt.set(user_prompt)
        if enhanced_prompt is not None:
            _request_enhanced_prompt.set(enhanced_prompt)

    def clear_request_prompts(self) -> None:
        """Clear request-scoped prompts so the next request does not inherit them."""
        try:
            _request_user_prompt.set(None)
        except LookupError:
            pass
        try:
            _request_enhanced_prompt.set(None)
        except LookupError:
            pass

    def record_llm_call(
        self,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        operation: str = "unknown",
        sub_operation: str = "unknown",
        model: str = "unknown",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        thoughts_tokens: Optional[int] = None,
        status: str = "success",
        version: Optional[str] = None,
        attempt: int = 1,
        is_retry: bool = False,
        retry_group_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None
    ) -> str:
        """
        Record a single LLM API call to database
        
        Args:
            project_id: Project identifier (for screen-builder endpoints)
            screen_id: Screen identifier (optional for project-level operations)
            org_id: Organization ID (for agent/crewai endpoints)
            subscriber_id: Subscriber ID (for agent/crewai endpoints)
            operation: Main operation (e.g., "generate-angular-app", "generate-backend", "agent-task")
            sub_operation: Specific sub-operation (e.g., "angular-build-fix", "ir-generation", "ocr-extraction")
            model: Model name (e.g., "gemini-3.5-flash-lite")
            prompt_tokens: Number of input tokens
            completion_tokens: Visible output tokens (candidates_token_count when from Gemini)
            total_tokens: Total tokens used (total_token_count from Gemini usage_metadata)
            thoughts_tokens: Internal reasoning tokens (thoughts_token_count); included in
                billable output at the output rate when total_tokens is provided
            status: "success" or "failed"
            version: Version/timestamp identifier
            attempt: Attempt number for retries
            is_retry: Whether this is a retry
            retry_group_id: Group ID for related retry attempts
            metadata: Additional metadata (files, function_id, error_hash, etc.)
            started_at: Call start time (default: now)
            finished_at: Call end time (default: now)
            
        Returns:
            Record ID (UUID)
        """
        if self._collection is None:
            logger.warning("LLM Token Tracker not initialized, skipping record")
            return ""
        
        try:
            now = datetime.utcnow()
            started_at = started_at or now
            finished_at = finished_at or now
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            
            billable = self._resolve_billable_tokens(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                thoughts_tokens=thoughts_tokens,
                metadata=metadata,
            )
            billable_prompt = billable["prompt_tokens"]
            billable_completion = billable["completion_tokens"]
            billable_total = billable["total_tokens"]
            candidates_tokens = billable.get("candidates_tokens", completion_tokens)
            billable_thoughts = billable.get("thoughts_tokens", 0)

            # Calculate pricing on billable input/output (reasoning tokens at output rate)
            pricing = self._calculate_cost(
                model, billable_prompt, billable_completion
            )
            pricing["billable_input_tokens"] = billable_prompt
            pricing["billable_output_tokens"] = billable_completion
            
            # Convert org_id and subscriber_id to ObjectId if they are strings
            if org_id:
                if isinstance(org_id, str):
                    try:
                        org_id = ObjectId(org_id)
                    except Exception as e:
                        logger.warning(f"Failed to convert org_id to ObjectId: {e}")
                        org_id = None
                elif isinstance(org_id, ObjectId):
                    # Already an ObjectId, use as is
                    pass
                else:
                    logger.warning(f"org_id has unexpected type: {type(org_id)}")
                    org_id = None

            if subscriber_id:
                if isinstance(subscriber_id, str):
                    try:
                        subscriber_id = ObjectId(subscriber_id)
                    except Exception as e:
                        logger.warning(f"Failed to convert subscriber_id to ObjectId: {e}")
                        subscriber_id = None
                elif isinstance(subscriber_id, ObjectId):
                    # Already an ObjectId, use as is
                    pass
                else:
                    logger.warning(f"subscriber_id has unexpected type: {type(subscriber_id)}")
                    subscriber_id = None
            
            user_id_obj = None
            if user_id:
                if isinstance(user_id, str):
                    try:
                        user_id_obj = ObjectId(user_id)
                    except Exception as e:
                        logger.warning(f"Failed to convert user_id to ObjectId: {e}")
                        user_id_obj = None
                elif isinstance(user_id, ObjectId):
                    # Already an ObjectId, use as is
                    user_id_obj = user_id
                    pass
                else:
                    logger.warning(f"user_id has unexpected type: {type(user_id)}")
                    user_id_obj = None
            
            # Create record
            record_id = str(uuid.uuid4())
            record = {
                "record_id": record_id,
                "project_id": project_id,
                "screen_id": screen_id,
                "orgId": org_id,
                "subscriberId": subscriber_id,
                "userId": user_id_obj,
                "process": "ai-screen-builder",
                "operation": operation,
                "sub_operation": sub_operation,
                "version": version,
                "model": model,
                "attempt": attempt,
                "is_retry": is_retry,
                "retry_group_id": retry_group_id,
                "prompt_tokens": billable_prompt,
                "completion_tokens": billable_completion,
                "total_tokens": billable_total,
                "pricing": pricing,
                "status": status,
                "createdAt": started_at.isoformat() + "Z",
                "finishedAt": finished_at.isoformat() + "Z",
                "duration_ms": duration_ms,
                "metadata": metadata or {}
            }

            if candidates_tokens and candidates_tokens != billable_completion:
                record["candidates_tokens"] = candidates_tokens
            if billable_thoughts > 0:
                record["thoughts_tokens"] = billable_thoughts

            # Attach request-scoped prompts when set (e.g. generate-angular-app / update-angular-screen)
            try:
                user_prompt_val = _request_user_prompt.get()
                if user_prompt_val is not None:
                    record["user_prompt"] = self._truncate_prompt(user_prompt_val)
            except LookupError:
                pass
            try:
                enhanced_prompt_val = _request_enhanced_prompt.get()
                if enhanced_prompt_val is not None:
                    record["enhanced_prompt"] = self._truncate_prompt(enhanced_prompt_val)
            except LookupError:
                pass
            
            # Insert to database FIRST to guarantee we track the spent tokens
            self._collection.insert_one(record)
            logger.info(
                f"Recorded LLM call: {operation}/{sub_operation} - "
                f"{billable_total} tokens (in={billable_prompt}, out={billable_completion}) - "
                f"${pricing['total_cost_usd']}"
            )

            if status == "success":
                try:
                    from services.workflow_budget_service import apply_llm_charge

                    apply_llm_charge(pricing.get("total_cost_usd", 0))
                except Exception as billing_exc:
                    logger.error(
                        "Failed to apply LLM billing charge: %s (operation=%s/%s)",
                        billing_exc,
                        operation,
                        sub_operation,
                    )
                    raise

            return record_id
            
        except Exception as e:
            err_msg = str(e)
            is_safety_limit = (
                "exceeded" in err_msg
                or "limit" in err_msg.lower()
            )
            if is_safety_limit:
                raise
            logger.error(
                "Failed to record LLM call: %s "
                "(project_id=%s, screen_id=%s, operation=%s/%s)",
                e,
                project_id,
                screen_id,
                operation,
                sub_operation,
            )
            return ""

    def get_project_usage(self, project_id: str, screen_id: Optional[str] = None) -> Dict[str, Any]:
        """Get token usage summary for a project/screen"""
        if self._collection is None:
            return {}
        
        try:
            query = {"project_id": project_id}
            if screen_id:
                query["screen_id"] = screen_id
            
            records = list(self._collection.find(query))
            
            total_tokens = sum(r.get("total_tokens", 0) for r in records)
            total_cost = sum(r.get("pricing", {}).get("total_cost_usd", 0) for r in records)
            
            return {
                "project_id": project_id,
                "screen_id": screen_id,
                "total_calls": len(records),
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 6),
                "records": records
            }
        except Exception as e:
            logger.error(f"Failed to get project usage: {e}")
            return {}


# Global tracker instance
_tracker = None

def get_tracker() -> LLMTokenTracker:
    """Get global tracker instance"""
    global _tracker
    if _tracker is None:
        _tracker = LLMTokenTracker()
    return _tracker


def record_llm_call(**kwargs) -> str:
    """Convenience function to record LLM call"""
    tracker = get_tracker()
    return tracker.record_llm_call(**kwargs)
