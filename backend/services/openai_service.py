"""
OpenAI transport service.

Used exclusively by gemini_service.py as a fallback provider when Gemini
hits 429 / 503 exhaustion on 3.x model series.  Not imported directly by
any call site — all orchestration lives in gemini_service.

Key pool is read from environment variables in .env:
    OPENAI_Dev_1  OPENAI_DEV_2  OPENAI_DEV_3  OPENAI_DEV_4  OPENAI_API_KEY

API routing
-----------
Not every OpenAI model id works on ``chat.completions``:

  chat.completions  →  gpt-4*, gpt-5.4-mini  (standard chat + vision)
  responses.create  →  gpt-5.5, gpt-5.3-codex, other codex / reasoning ids

All responses are normalised to a ChatCompletion-like object before being
returned so ``LLMResponse.from_openai()`` in gemini_service stays unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import random
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

from openai import AsyncOpenAI, OpenAI, RateLimitError, APIStatusError, InternalServerError
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

# ---------------------------------------------------------------------------
# Retry / backoff constants  (same tuning as gemini_service)
# ---------------------------------------------------------------------------

_MAX_RETRIES_PER_KEY = 3
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MULTIPLIER = 3.0
_JITTER_MAX_SECONDS = 1.0

# Vision fallback when a non-chat / codex model is mapped but images are present.
_VISION_CHAT_FALLBACK_MODEL = "gpt-4.1-mini"

# ---------------------------------------------------------------------------
# API key pool  (env-based — no GCP Secret Manager needed for OpenAI)
# ---------------------------------------------------------------------------

_OPENAI_ENV_KEY_NAMES: tuple[str, ...] = (
    "OPENAI_Dev_1",
    "OPENAI_DEV_2",
    "OPENAI_DEV_3",
    "OPENAI_DEV_4",
    "OPENAI_API_KEY",
)

_openai_key_pool: list[str] = []
_key_pool_loaded: bool = False
_key_pool_lock = threading.Lock()

_current_key_index: int = 0
_key_index_lock = threading.Lock()


def _load_key_pool() -> list[str]:
    """Read OpenAI keys from environment; deduplicate; skip blanks."""
    seen: set[str] = set()
    keys: list[str] = []
    for env_name in _OPENAI_ENV_KEY_NAMES:
        val = os.getenv(env_name, "").strip()
        if val and val not in seen:
            seen.add(val)
            keys.append(val)
    if not keys:
        logger.error("openai_service: no OpenAI API keys found in environment variables")
    else:
        logger.info("openai_service: loaded %d OpenAI key(s) from environment", len(keys))
    return keys


def _get_key_pool() -> list[str]:
    global _openai_key_pool, _key_pool_loaded
    if _key_pool_loaded:
        return _openai_key_pool
    with _key_pool_lock:
        if not _key_pool_loaded:
            _openai_key_pool = _load_key_pool()
            _key_pool_loaded = True
    return _openai_key_pool


def get_openai_api_key() -> Optional[str]:
    """Return the currently active OpenAI API key."""
    with _key_index_lock:
        idx = _current_key_index
    pool = _get_key_pool()
    if not pool or idx >= len(pool):
        return None
    return pool[idx]


def _rotate_to_next_key() -> Optional[str]:
    """Advance to the next key in the pool. Returns None when pool is exhausted."""
    global _current_key_index
    with _key_index_lock:
        pool = _get_key_pool()
        if _current_key_index >= len(pool) - 1:
            logger.error("openai_service: all OpenAI API keys exhausted")
            return None
        _current_key_index += 1
        idx = _current_key_index
        logger.warning("openai_service: rotating to OpenAI key index=%d", idx)
    return _get_key_pool()[idx]


# ---------------------------------------------------------------------------
# Retryable error detection + backoff
# ---------------------------------------------------------------------------


def _is_retryable_openai_error(exc: Exception) -> bool:
    """True for 429 rate-limit and 503 service-unavailable responses."""
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, (APIStatusError, InternalServerError)):
        code = getattr(exc, "status_code", None)
        if code in (429, 503):
            return True
    msg = str(exc).lower()
    return any(
        t in msg
        for t in ("429", "503", "rate limit", "too many requests", "service unavailable")
    )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter (matches gemini_service tuning)."""
    return _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** attempt) + random.uniform(
        0, _JITTER_MAX_SECONDS
    )


# ---------------------------------------------------------------------------
# Model routing — chat.completions vs responses.create
# ---------------------------------------------------------------------------


def uses_responses_api(model: str) -> bool:
    """
    Return True when *model* must be called via ``client.responses.create``.

    gpt-5.5 and gpt-5.3-codex are NOT chat-completion models; they require
    the Responses API (confirmed via tests_scripts/check-openai.py).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if "codex" in m:
        return True
    # Reasoning-tier ids that reject chat.completions and custom temperature.
    if m in ("gpt-5.5", "gpt-5.2"):
        return True
    return False


def _model_supports_custom_temperature(model: str) -> bool:
    """Reasoning / codex models only accept the default temperature."""
    return not uses_responses_api(model)


def _effective_model(model: str, messages: list[dict]) -> str:
    """
    Pick the model id to actually call.

    Codex / responses-only models cannot handle vision; downgrade to a
  chat vision model when images are present in the prompt.
    """
    if _messages_have_images(messages) and uses_responses_api(model):
        logger.warning(
            "openai_service: model %s cannot handle images; using %s",
            model,
            _VISION_CHAT_FALLBACK_MODEL,
        )
        return _VISION_CHAT_FALLBACK_MODEL
    return model


# ---------------------------------------------------------------------------
# Content normalization  (Gemini shapes → OpenAI messages)
# ---------------------------------------------------------------------------


def _image_bytes_to_data_url(data: bytes, mime_type: str) -> str:
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


def _pil_to_data_url(image: Any) -> str:
    """Convert a PIL Image to a base64 data URL."""
    buf = io.BytesIO()
    fmt = image.format or "PNG"
    image.save(buf, format=fmt)
    return _image_bytes_to_data_url(buf.getvalue(), f"image/{fmt.lower()}")


def normalize_to_openai_messages(contents: Any) -> list[dict]:
    """
    Convert Gemini-style contents to an OpenAI messages list.

    Accepted input shapes:
      str                                   → text user message
      [str]                                 → text user message
      [str, PIL.Image]                      → text + image_url message
      [str, {"mime_type": ..., "data": b}]  → text + image_url message
      mixed list                            → joined text (images extracted)
    """
    if contents is None:
        return [{"role": "user", "content": ""}]

    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]

    if not isinstance(contents, list):
        return [{"role": "user", "content": str(contents)}]

    text_parts: list[str] = []
    image_blocks: list[dict] = []

    for item in contents:
        if isinstance(item, str):
            text_parts.append(item)
            continue

        # Legacy Gemini dict: {"mime_type": ..., "data": bytes}
        if isinstance(item, dict) and "mime_type" in item and "data" in item:
            url = _image_bytes_to_data_url(item["data"], item["mime_type"])
            image_blocks.append({"type": "image_url", "image_url": {"url": url}})
            continue

        # PIL Image
        try:
            from PIL import Image as PILImage  # noqa: PLC0415

            if isinstance(item, PILImage.Image):
                url = _pil_to_data_url(item)
                image_blocks.append({"type": "image_url", "image_url": {"url": url}})
                continue
        except ImportError:
            pass

        text_parts.append(str(item))

    if not image_blocks:
        return [{"role": "user", "content": "\n".join(text_parts)}]

    # Multimodal content block
    content_blocks: list[dict] = []
    if text_parts:
        content_blocks.append({"type": "text", "text": "\n".join(text_parts)})
    content_blocks.extend(image_blocks)
    return [{"role": "user", "content": content_blocks}]


def _messages_have_images(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def _messages_to_input_text(messages: list[dict]) -> str:
    """Flatten chat messages to a single input string for the Responses API."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = "\n".join(t for t in texts if t)
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Response normalisation  (Responses API → ChatCompletion-like shape)
# ---------------------------------------------------------------------------


def _extract_responses_text(raw: Any) -> str:
    """Extract text from a Responses API result."""
    text = getattr(raw, "output_text", None)
    if text:
        return text
    chunks: list[str] = []
    for item in getattr(raw, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text":
                chunks.append(getattr(block, "text", "") or "")
    return "".join(chunks)


def normalize_to_chat_completion(raw: Any, *, model: str = "") -> Any:
    """
    Normalise either a ChatCompletion or a Responses API result into a
    ChatCompletion-like object that ``LLMResponse.from_openai()`` understands.
    """
    # Already chat-completion shaped?
    if hasattr(raw, "choices") and raw.choices:
        return raw

    content = _extract_responses_text(raw)
    usage = getattr(raw, "usage", None)
    prompt_tokens = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    completion_tokens = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    total_tokens = (
        getattr(usage, "total_tokens", None)
        or (prompt_tokens + completion_tokens)
    )

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
        model=getattr(raw, "model", None) or model,
    )


# ---------------------------------------------------------------------------
# Call-kwargs builders
# ---------------------------------------------------------------------------


def _wants_json(
    response_mime_type: Optional[str],
    response_schema: Any,
) -> bool:
    return response_schema is not None or response_mime_type == "application/json"


def _build_chat_kwargs(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: Optional[int],
    response_mime_type: Optional[str],
    response_schema: Any,
    top_p: Optional[float],
    stop_sequences: Optional[list],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if _model_supports_custom_temperature(model):
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if top_p is not None and _model_supports_custom_temperature(model):
        kwargs["top_p"] = top_p
    if stop_sequences is not None:
        kwargs["stop"] = stop_sequences
    if _wants_json(response_mime_type, response_schema):
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def _build_responses_kwargs(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: Optional[int],
    response_mime_type: Optional[str],
    response_schema: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "input": _messages_to_input_text(messages),
    }
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    else:
        kwargs["max_output_tokens"] = 4096
    # Reasoning models reject non-default temperature — omit entirely.
    if _model_supports_custom_temperature(model):
        kwargs["temperature"] = temperature
    if _wants_json(response_mime_type, response_schema):
        kwargs["text"] = {"format": {"type": "json_object"}}
    return kwargs


async def _call_openai_async(client: AsyncOpenAI, model: str, messages: list[dict], **cfg) -> Any:
    effective = _effective_model(model, messages)
    shared = {
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens"),
        "response_mime_type": cfg.get("response_mime_type"),
        "response_schema": cfg.get("response_schema"),
    }
    if uses_responses_api(effective):
        raw = await client.responses.create(
            **_build_responses_kwargs(effective, messages, **shared)
        )
        return normalize_to_chat_completion(raw, model=effective)
    raw = await client.chat.completions.create(
        **_build_chat_kwargs(
            effective,
            messages,
            top_p=cfg.get("top_p"),
            stop_sequences=cfg.get("stop_sequences"),
            **shared,
        )
    )
    return normalize_to_chat_completion(raw, model=effective)


def _call_openai_sync(client: OpenAI, model: str, messages: list[dict], **cfg) -> Any:
    effective = _effective_model(model, messages)
    shared = {
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens"),
        "response_mime_type": cfg.get("response_mime_type"),
        "response_schema": cfg.get("response_schema"),
    }
    if uses_responses_api(effective):
        raw = client.responses.create(
            **_build_responses_kwargs(effective, messages, **shared)
        )
        return normalize_to_chat_completion(raw, model=effective)
    raw = client.chat.completions.create(
        **_build_chat_kwargs(
            effective,
            messages,
            top_p=cfg.get("top_p"),
            stop_sequences=cfg.get("stop_sequences"),
            **shared,
        )
    )
    return normalize_to_chat_completion(raw, model=effective)


# ---------------------------------------------------------------------------
# Core async / sync generate
# ---------------------------------------------------------------------------


async def async_generate(
    model: str,
    contents: Any,
    *,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    response_mime_type: Optional[str] = None,
    response_schema: Any = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list] = None,
    api_key: Optional[str] = None,
) -> Any:
    """
    Async OpenAI call with per-key backoff and key rotation.

    Routes to ``responses.create`` or ``chat.completions.create`` based on
    model id, then returns a ChatCompletion-like object.
    """
    from services.workflow_budget_service import check_before_llm_call

    check_before_llm_call()

    messages = normalize_to_openai_messages(contents)
    call_cfg = dict(
        temperature=temperature,
        max_tokens=max_tokens,
        response_mime_type=response_mime_type,
        response_schema=response_schema,
        top_p=top_p,
        stop_sequences=stop_sequences,
    )

    last_exc: Optional[Exception] = None
    allow_rotation = api_key is None

    while True:
        current_key = api_key if api_key is not None else get_openai_api_key()
        if not current_key:
            raise RuntimeError("openai_service: no OpenAI API key available")
        client = AsyncOpenAI(api_key=current_key)

        for attempt in range(_MAX_RETRIES_PER_KEY + 1):
            try:
                return await _call_openai_async(client, model, messages, **call_cfg)
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_openai_error(exc):
                    raise
                if attempt >= _MAX_RETRIES_PER_KEY:
                    break
                delay = _backoff_delay(attempt)
                logger.warning(
                    "openai_service: retryable error (attempt %d/%d, backoff %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES_PER_KEY,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        if not allow_rotation:
            assert last_exc is not None
            raise last_exc

        if _rotate_to_next_key() is None:
            assert last_exc is not None
            raise last_exc

        logger.info("openai_service: retrying after key rotation")


def sync_generate(
    model: str,
    contents: Any,
    *,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    response_mime_type: Optional[str] = None,
    response_schema: Any = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list] = None,
    api_key: Optional[str] = None,
) -> Any:
    """
    Synchronous OpenAI call with per-key backoff and key rotation.

    Routes to ``responses.create`` or ``chat.completions.create`` based on
    model id, then returns a ChatCompletion-like object.
    """
    from services.workflow_budget_service import check_before_llm_call

    check_before_llm_call()

    messages = normalize_to_openai_messages(contents)
    call_cfg = dict(
        temperature=temperature,
        max_tokens=max_tokens,
        response_mime_type=response_mime_type,
        response_schema=response_schema,
        top_p=top_p,
        stop_sequences=stop_sequences,
    )

    last_exc: Optional[Exception] = None
    allow_rotation = api_key is None

    while True:
        current_key = api_key if api_key is not None else get_openai_api_key()
        if not current_key:
            raise RuntimeError("openai_service: no OpenAI API key available")
        client = OpenAI(api_key=current_key)

        for attempt in range(_MAX_RETRIES_PER_KEY + 1):
            try:
                return _call_openai_sync(client, model, messages, **call_cfg)
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_openai_error(exc):
                    raise
                if attempt >= _MAX_RETRIES_PER_KEY:
                    break
                delay = _backoff_delay(attempt)
                logger.warning(
                    "openai_service: retryable error (attempt %d/%d, backoff %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES_PER_KEY,
                    delay,
                    exc,
                )
                time.sleep(delay)

        if not allow_rotation:
            assert last_exc is not None
            raise last_exc

        if _rotate_to_next_key() is None:
            assert last_exc is not None
            raise last_exc

        logger.info("openai_service: retrying after key rotation")
