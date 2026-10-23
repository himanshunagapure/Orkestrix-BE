"""
LLM Utility Functions
Helper functions for extracting token usage from different LLM response formats.
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def extract_token_usage_from_response(response) -> Dict[str, int]:
    """
    Extract token usage from Gemini API response.
    Handles both SDK response objects and dict responses.
    
    Returns:
        Dict with prompt_tokens, completion_tokens, total_tokens
    """
    try:
        # Try to access usage_metadata attribute (SDK response)
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            return {
                "prompt_tokens": getattr(usage, 'prompt_token_count', 0),
                "completion_tokens": getattr(usage, 'candidates_token_count', 0),
                "total_tokens": getattr(usage, 'total_token_count', 0),
                "cached_content_token_count": getattr(
                    usage, "cached_content_token_count", 0
                ),
            }
        
        # Try dict access (JSON response)
        if isinstance(response, dict):
            usage = response.get('usage_metadata', {})
            return {
                "prompt_tokens": usage.get('prompt_token_count', 0),
                "completion_tokens": usage.get('candidates_token_count', 0),
                "total_tokens": usage.get('total_token_count', 0),
                "cached_content_token_count": usage.get(
                    "cached_content_token_count", 0
                ),
            }
        
        # Fallback: try to access as attributes
        if hasattr(response, 'prompt_token_count'):
            return {
                "prompt_tokens": getattr(response, 'prompt_token_count', 0),
                "completion_tokens": getattr(response, 'candidates_token_count', 0),
                "total_tokens": getattr(response, 'total_token_count', 0),
                "cached_content_token_count": getattr(
                    response, "cached_content_token_count", 0
                ),
            }
        
        logger.warning(f"Could not extract token usage from response type: {type(response)}")
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_content_token_count": 0,
        }
        
    except Exception as e:
        logger.error(f"Error extracting token usage: {e}")
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_content_token_count": 0,
        }


def extract_model_name_from_response(response, default: str = "gemini-3.5-flash-lite") -> str:
    """
    Extract model name from response.
    Falls back to default if not found.
    """
    try:
        if hasattr(response, 'model'):
            return response.model
        if isinstance(response, dict) and 'model' in response:
            return response['model']
        return default
    except Exception:
        return default


