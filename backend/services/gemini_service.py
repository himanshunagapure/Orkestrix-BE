"""
Central Gemini LLM transport service.

Responsibilities
----------------
- API key retrieval from GCP Secret Manager (multi-key rotation on 429/503)
- Gemini client creation
- async / sync generate_content wrappers (new google-genai SDK)
- Exponential backoff + key rotation for rate-limit / overload errors
- Response text extraction helper
- Model-capability gate for thinking_level

What this module does NOT do
-----------------------------
- record_llm_call tracking   — each call site keeps its own tracking logic
- prompt building             — purely a transport layer

Usage
-----
    from services.gemini_service import async_generate, sync_generate, GeminiGenerateConfig

    response = await async_generate(
        "gemini-3.5-flash-lite",
        prompt,
        config=GeminiGenerateConfig(temperature=0.2, response_mime_type="application/json"),
        api_key=api_key,   # optional – service fetches from Secret Manager if omitted
    )
    text = extract_text(response)
    parsed = response.parsed   # when response_schema was set

OpenAI fallback
---------------
    Set LLM_OPENAI_FALLBACK_ENABLED=true in .env to activate transparent
    fallback from Gemini 3.x → OpenAI when Gemini hits 429/503 exhaustion.

    For gemini-3.1-pro-preview which maps differently by role, pass:
        config=GeminiGenerateConfig(fallback_profile="planner")   # → gpt-5.5
        config=GeminiGenerateConfig(fallback_profile="coding")    # → gpt-5.3-codex

    The returned object is always duck-type compatible with the Gemini SDK
    response (.text, .parsed, .usage_metadata, .candidates).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from google import genai
from google.genai import types
from google.cloud import secretmanager
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

# ---------------------------------------------------------------------------
# Retry / backoff constants (429 rate limit, 503 high demand)
# ---------------------------------------------------------------------------

_MAX_RETRIES_PER_KEY = 3
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MULTIPLIER = 3.0  # 2s → 6s → 18s
_JITTER_MAX_SECONDS = 1.0

# Per-call hard timeout (seconds) for the async Gemini generate_content call.
# Without this, a network-level stall (connection accepted, response never
# completed) causes the await to hang forever — which manifests as the
# /resume (and /execute) endpoint running indefinitely even after all DB
# work is done. asyncio.TimeoutError is not retryable, so it immediately
# propagates to the caller rather than spinning through the retry loop.
_ASYNC_CALL_TIMEOUT_SECONDS = 600

# Same cap for the blocking (sync) Gemini generate_content call used by
# angular_generator._invoke_model and other sync callers.  The call runs
# inside a ThreadPoolExecutor thread so we apply the timeout via
# concurrent.futures rather than asyncio.
_SYNC_CALL_TIMEOUT_SECONDS = 600

# config.json keys for Secret Manager secret ids (default first)
_SECRET_CONFIG_KEYS = (
    "gcp_secret_id",
    "gcp_secret_id_2",
    "gcp_secret_id_3",
    "gcp_secret_id_4",
    "gcp_secret_id_5",
)
# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_config_cache: Optional[dict] = None
_config_lock = threading.Lock()


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    with _config_lock:
        if _config_cache is None:
            config_path = Path(__file__).parent.parent / "config.json"
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    _config_cache = json.load(f)
            except Exception as exc:
                logger.warning("gemini_service: failed to load config.json: %s", exc)
                _config_cache = {}
    return _config_cache


# ---------------------------------------------------------------------------
# API key pool (multi-key rotation, cached per index)
# ---------------------------------------------------------------------------

_current_key_index: int = 0
_api_keys_by_index: dict[int, str] = {}
_key_state_lock = threading.Lock()


def _secret_config_entries() -> list[tuple[str, str]]:
    """Return (config_key, secret_id) pairs that are set in config.json."""
    config = _load_config()
    entries: list[tuple[str, str]] = []
    for cfg_key in _SECRET_CONFIG_KEYS:
        secret_id = config.get(cfg_key)
        if secret_id:
            entries.append((cfg_key, str(secret_id)))
    if not entries:
        entries.append(("gcp_secret_id", "Product"))
    return entries


def _fetch_api_key_at_index(index: int, *, force_refresh: bool = False) -> Optional[str]:
    """Fetch (and cache) the API key for the given pool index."""
    entries = _secret_config_entries()
    if index < 0 or index >= len(entries):
        return None

    if not force_refresh and index in _api_keys_by_index:
        return _api_keys_by_index[index]

    cfg_key, secret_id = entries[index]
    config = _load_config()
    project_id = config.get("gcp_project_id", "202844388711")
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    try:
        sm_client = secretmanager.SecretManagerServiceClient()
        response = sm_client.access_secret_version(request={"name": name})
        api_key = response.payload.data.decode("UTF-8")
        _api_keys_by_index[index] = api_key
        logger.debug(
            "gemini_service: fetched API key index=%s config_key=%s secret_id=%s",
            index,
            cfg_key,
            secret_id,
        )
        return api_key
    except Exception as exc:
        logger.warning(
            "gemini_service: could not retrieve API key (index=%s, %s): %s",
            index,
            cfg_key,
            exc,
        )
        return None


def get_gemini_api_key(force_refresh: bool = False) -> Optional[str]:
    """Return the currently active Gemini API key from the rotation pool.

    Default pool entry is ``gcp_secret_id``. Keys are cached in-process.
    """
    with _key_state_lock:
        index = _current_key_index
    return _fetch_api_key_at_index(index, force_refresh=force_refresh)


def _rotate_to_next_api_key() -> Optional[str]:
    """Advance to the next configured secret id and return its API key."""
    global _current_key_index
    with _key_state_lock:
        entries = _secret_config_entries()
        if _current_key_index >= len(entries) - 1:
            logger.error("gemini_service: all API keys exhausted in rotation pool")
            return None
        _current_key_index += 1
        index = _current_key_index
        cfg_key, secret_id = entries[index]
        logger.warning(
            "gemini_service: rotating to API key index=%s config_key=%s secret_id=%s",
            index,
            cfg_key,
            secret_id,
        )
    return _fetch_api_key_at_index(index, force_refresh=True)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_client(api_key: Optional[str] = None) -> genai.Client:
    """Return a Gemini Client.

    Priority: explicit *api_key* → ``get_gemini_api_key()`` → ADC (no key).
    """
    key = api_key or get_gemini_api_key()
    if key:
        return genai.Client(api_key=key)
    return genai.Client()


# ---------------------------------------------------------------------------
# Retryable error detection + backoff
# ---------------------------------------------------------------------------


def _extract_http_status(exc: Exception) -> Optional[int]:
    """Best-effort HTTP status from google.genai / API core exceptions."""
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val
    return None


def _is_retryable_gemini_error(exc: Exception) -> bool:
    """True for 429 quota / 503 high-demand style failures."""
    status = _extract_http_status(exc)
    if status in (429, 503):
        return True

    msg = str(exc).lower()
    if any(token in msg for token in ("429", "503", "resource has been exhausted")):
        return True
    if "high demand" in msg:
        return True
    if "rate limit" in msg or "quota exceeded" in msg or "too many requests" in msg:
        return True

    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_retryable_gemini_error(cause)
    return False


def _is_fallback_eligible_error(exc: Exception) -> bool:
    """True for errors that should trigger OpenAI fallback (retryable + 401 auth errors)."""
    if _is_retryable_gemini_error(exc):
        return True
    # Also allow fallback on 401 UNAUTHENTICATED (API not enabled, invalid key, etc.)
    status = _extract_http_status(exc)
    if status == 401:
        return True
    msg = str(exc).lower()
    if "unauthenticated" in msg or "invalid authentication" in msg or "401" in msg:
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_fallback_eligible_error(cause)
    return False


def _backoff_delay_seconds(attempt: int) -> float:
    """Exponential backoff with random jitter (attempt 0 → ~2s, 1 → ~6s, 2 → ~18s)."""
    base = _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** attempt)
    return base + random.uniform(0, _JITTER_MAX_SECONDS)


def _generate_with_retry(
    call_fn: Callable[[Optional[str]], Any],
    *,
    explicit_api_key: Optional[str],
    sleep_fn: Callable[[float], Any],
) -> Any:
    """Run *call_fn(api_key)* with per-key backoff then key rotation."""
    last_exc: Optional[Exception] = None
    allow_rotation = explicit_api_key is None

    while True:
        api_key = explicit_api_key if explicit_api_key is not None else get_gemini_api_key()

        for attempt in range(_MAX_RETRIES_PER_KEY + 1):
            try:
                return call_fn(api_key)
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_gemini_error(exc):
                    raise
                if attempt >= _MAX_RETRIES_PER_KEY:
                    break
                delay = _backoff_delay_seconds(attempt)
                logger.warning(
                    "gemini_service: retryable error (attempt %s/%s, backoff %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES_PER_KEY,
                    delay,
                    exc,
                )
                sleep_fn(delay)

        if not allow_rotation:
            assert last_exc is not None
            raise last_exc

        if _rotate_to_next_api_key() is None:
            assert last_exc is not None
            raise last_exc

        logger.info("gemini_service: retrying after API key rotation")

# ---------------------------------------------------------------------------
# Model-capability helpers
# ---------------------------------------------------------------------------


def model_supports_thinking_level(model: str) -> bool:
    """Return True if this model accepts ThinkingConfig.thinking_level.

    Gemini 2.5 / 2.0 / 1.5 and all *flash* variants reject thinking_level
    with a 400 INVALID_ARGUMENT.  Gemini 3.x pro / preview models support it.
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    if any(v in name for v in ("2.5", "2.0", "1.5")):
        return False
    if "flash" in name:
        return False
    return "3" in name and ("pro" in name or "preview" in name)


# ---------------------------------------------------------------------------
# Google Search tool helper
# ---------------------------------------------------------------------------


def google_search_tool() -> list:
    """Return a tools list with a Google Search grounding tool."""
    return [types.Tool(google_search=types.GoogleSearch())]


# ---------------------------------------------------------------------------
# Generate config dataclass
# ---------------------------------------------------------------------------


@dataclass
class GeminiGenerateConfig:
    """All optional knobs for a Gemini generate_content call.

    Only non-None values are forwarded to ``GenerateContentConfig``, so callers
    can safely leave unused fields at their default (``None``).

    ``thinking_level`` defaults to ``None`` (no thinking).  Set it to ``"high"``
    (or ``"low"`` / ``"medium"``) to enable thinking for models that support it.
    The service gate (``model_supports_thinking_level``) prevents a 400 error on
    models that don't support it.
    """

    temperature: float = 0.2
    response_mime_type: Optional[str] = None
    response_schema: Any = None
    # Thinking is opt-in; service gates on model_supports_thinking_level()
    thinking_level: Optional[str] = None
    include_thoughts: bool = False
    tools: Optional[list] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_output_tokens: Optional[int] = None
    stop_sequences: Optional[list] = None
    # OpenAI fallback profile — used only when Gemini 3.x is exhausted.
    # "planner" → gpt-5.5   "coding" → gpt-5.3-codex   None → model default
    fallback_profile: Optional[Literal["planner", "coding"]] = None
    # Explicit Gemini context cache resource name (from client.caches.create).
    # When set, static system/context is read from the cache — do not repeat it
    # in the per-request contents string.
    cached_content: Optional[str] = None


# ---------------------------------------------------------------------------
# Explicit context cache (global singleton caches, e.g. Stage 0-B system prompt)
# ---------------------------------------------------------------------------

_EXPLICIT_CACHE_ENABLED: bool = (
    os.getenv("GEMINI_EXPLICIT_CACHE_ENABLED", "true").lower() == "true"
)


@dataclass
class _ContextCacheEntry:
    name: str
    expires_at: float  # unix timestamp


_cache_registry: dict[str, _ContextCacheEntry] = {}
_cache_registry_lock = threading.Lock()
_cache_create_lock: Optional[asyncio.Lock] = None


def _cache_registry_key(cache_key: str, model: str, api_key: Optional[str]) -> str:
    key_fingerprint = hashlib.sha256((api_key or "").encode()).hexdigest()[:10]
    return f"{cache_key}:{model}:{key_fingerprint}"


def _create_context_cache_sync(
    *,
    model: str,
    system_text: str,
    ttl_seconds: int,
    display_name: str,
    api_key: Optional[str],
) -> str:
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    cached = client.caches.create(
        model=model,
        config=types.CreateCachedContentConfig(
            system_instruction=system_text,
            display_name=display_name,
            ttl=f"{max(60, int(ttl_seconds))}s",
        ),
    )
    if not cached or not getattr(cached, "name", None):
        raise RuntimeError("Gemini caches.create returned no cache name")
    return str(cached.name)


async def get_or_create_cache(
    *,
    model: str,
    system_text: str,
    ttl_seconds: int,
    cache_key: str,
    api_key: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Optional[str]:
    """
    Return the resource name of a shared explicit context cache.

    One cache entry is reused across workflows/users until TTL expiry.
    Any failure returns None so callers can fall back to uncached prompts.
    """
    if not _EXPLICIT_CACHE_ENABLED:
        return None
    if not (system_text or "").strip():
        return None

    reg_key = _cache_registry_key(cache_key, model, api_key)
    now = time.time()
    refresh_buffer_seconds = 60.0

    with _cache_registry_lock:
        entry = _cache_registry.get(reg_key)
        if entry and entry.expires_at > now + refresh_buffer_seconds:
            return entry.name

    global _cache_create_lock
    if _cache_create_lock is None:
        _cache_create_lock = asyncio.Lock()

    async with _cache_create_lock:
        with _cache_registry_lock:
            entry = _cache_registry.get(reg_key)
            if entry and entry.expires_at > now + refresh_buffer_seconds:
                return entry.name

        try:
            loop = asyncio.get_running_loop()
            cache_name = await loop.run_in_executor(
                None,
                lambda: _create_context_cache_sync(
                    model=model,
                    system_text=system_text,
                    ttl_seconds=ttl_seconds,
                    display_name=display_name or cache_key,
                    api_key=api_key,
                ),
            )
        except Exception as exc:
            logger.warning(
                "gemini_service: context cache unavailable for %s: %s",
                cache_key,
                exc,
            )
            return None

        with _cache_registry_lock:
            _cache_registry[reg_key] = _ContextCacheEntry(
                name=cache_name,
                expires_at=now + ttl_seconds,
            )
        logger.info(
            "gemini_service: created context cache key=%s model=%s ttl=%ss",
            cache_key,
            model,
            ttl_seconds,
        )
        return cache_name


# ---------------------------------------------------------------------------
# Internal: build SDK config from our dataclass
# ---------------------------------------------------------------------------


def _build_sdk_config(model: str, cfg: GeminiGenerateConfig) -> types.GenerateContentConfig:
    kwargs: dict[str, Any] = {"temperature": cfg.temperature}

    if cfg.top_p is not None:
        kwargs["top_p"] = cfg.top_p
    if cfg.top_k is not None:
        kwargs["top_k"] = cfg.top_k
    if cfg.max_output_tokens is not None:
        kwargs["max_output_tokens"] = cfg.max_output_tokens
    if cfg.stop_sequences is not None:
        kwargs["stop_sequences"] = cfg.stop_sequences
    if cfg.response_mime_type is not None:
        kwargs["response_mime_type"] = cfg.response_mime_type
    if cfg.response_schema is not None:
        kwargs["response_schema"] = cfg.response_schema
    if cfg.tools is not None:
        kwargs["tools"] = cfg.tools
    if cfg.cached_content is not None:
        kwargs["cached_content"] = cfg.cached_content

    # Only add ThinkingConfig when caller explicitly requested it AND model supports it
    if cfg.thinking_level is not None and model_supports_thinking_level(model):
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=cfg.thinking_level,
            include_thoughts=cfg.include_thoughts,
        )

    return types.GenerateContentConfig(**kwargs)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_text(response: Any) -> str:
    """Return the text content from a Gemini SDK response.

    Tries ``response.text`` first (new SDK convenience accessor); falls back to
    iterating ``candidates[0].content.parts``.  Returns ``""`` on any failure.
    """
    try:
        text = response.text
        if text:
            return text
    except Exception:
        pass
    try:
        parts = response.candidates[0].content.parts
        return "\n".join(p.text for p in parts if getattr(p, "text", None))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# OpenAI fallback — model mapping
# ---------------------------------------------------------------------------

# Master switch.  Set LLM_OPENAI_FALLBACK_ENABLED=true in .env to activate.
_OPENAI_FALLBACK_ENABLED: bool = os.getenv("LLM_OPENAI_FALLBACK_ENABLED", "false").lower() == "true"

# When true, also allow 2.5-* models to fall back (phase 2 opt-in).
_OPENAI_FALLBACK_ALL: bool = os.getenv("LLM_OPENAI_FALLBACK_ALL", "false").lower() == "true"

# Simple 1-to-1 map (phase 2 entries kept but 2.5 only active when _OPENAI_FALLBACK_ALL=true)
_GEMINI_TO_OPENAI: dict[str, str] = {
    # ── 3.x series — active when _OPENAI_FALLBACK_ENABLED=true (v1)
    "gemini-3.5-flash":        "gpt-5.3-codex",
    "gemini-3.5-flash-lite":   "gpt-5.4-mini",
    "gemini-3.6-flash":        "gpt-5.3-codex",
    "gemini-3-flash-preview":  "gpt-5.4-mini",
    # ── 2.5 series — only active when _OPENAI_FALLBACK_ALL=true (phase 2)
    "gemini-2.5-flash":        "gpt-5.4-mini",
    "gemini-2.5-pro":          "gpt-5.3-codex",
    # ── default catch-all
    "_default":                "gpt-5.3-codex",
}

# Profile-aware map: same Gemini model id, different OpenAI target by use case.
_GEMINI_TO_OPENAI_BY_PROFILE: dict[str, dict[str, str]] = {
    "gemini-3.1-pro-preview": {
        "planner": "gpt-5.5",        # graph planning, skeleton planning
        "coding":  "gpt-5.3-codex",  # Angular / UI codegen
        "default": "gpt-5.5",        # safe default = planner (main 503 source)
    },
    "gemini-3.1-pro": {              # alias without -preview suffix
        "planner": "gpt-5.5",
        "coding":  "gpt-5.3-codex",
        "default": "gpt-5.5",
    },
}


def _normalize_model_id(model: str) -> str:
    """Strip provider prefix and normalize legacy Gemini aliases to supported models."""
    if not model:
        return ""

    model = model.strip().lower()
    if "/" in model:
        model = model.split("/", 1)[1]

    legacy_aliases = {
        "gemini-2.0-flash": "gemini-3.5-flash-lite",
        "gemini-2.0-pro": "gemini-2.5-pro",
        "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
        "2.0-flash": "gemini-3.5-flash-lite",
        "2.0-pro": "gemini-2.5-pro",
    }
    return legacy_aliases.get(model, model)


def resolve_openai_model(gemini_model: str, profile: Optional[str] = None) -> str:
    """
    Return the OpenAI model id to use as fallback for *gemini_model*.

    Profile "planner" / "coding" disambiguates models like gemini-3.1-pro-preview
    that serve different roles; None uses the model's default profile.
    """
    key = _normalize_model_id(gemini_model)
    if key in _GEMINI_TO_OPENAI_BY_PROFILE:
        profiles = _GEMINI_TO_OPENAI_BY_PROFILE[key]
        return profiles.get(profile or "default", profiles["default"])
    return _GEMINI_TO_OPENAI.get(key, _GEMINI_TO_OPENAI["_default"])


def _fallback_eligible(
    model: str,
    cfg: "GeminiGenerateConfig",
    *,
    explicit_api_key: Optional[str],
) -> bool:
    """
    Return True when an OpenAI fallback attempt is permitted.

    Rules:
      - Master switch must be enabled.
      - Explicit api_key disables fallback (same policy as key rotation).
      - Tool-using calls (Google Search etc.) are Gemini-only.
      - By default only gemini-3* models fall back; set LLM_OPENAI_FALLBACK_ALL=true for 2.5.
    """
    if not _OPENAI_FALLBACK_ENABLED:
        return False
    if explicit_api_key is not None:
        return False
    if cfg.tools:
        return False

    norm = _normalize_model_id(model)
    if norm.startswith("gemini-3"):
        return True
    if _OPENAI_FALLBACK_ALL and norm.startswith("gemini-2.5"):
        return True
    return False


# ---------------------------------------------------------------------------
# LLMResponse — Gemini-duck-type-compatible wrapper returned on OpenAI fallback
# ---------------------------------------------------------------------------


class _OAIUsageMeta:
    """Mimics Gemini usage_metadata attribute names."""

    def __init__(self, prompt: int, candidates: int, total: int) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total
        # scratch_graph_planner reads thoughts_token_count too
        self.thoughts_token_count = 0


class _OAIPart:
    def __init__(self, text: str) -> None:
        self.text = text


class _OAIContent:
    def __init__(self, text: str) -> None:
        self.parts = [_OAIPart(text)]


class _OAICandidate:
    """
    Mimics Gemini Candidate.

    angular_generator checks:  ``candidate.finish_reason not in (1, "STOP")``
    ai_processor iterates:     ``c.content.parts``
    backend_generator reads:   ``candidate.safety_ratings``
    """

    def __init__(self, text: str, finish_reason: Any = "STOP") -> None:
        self.finish_reason = finish_reason
        self.safety_ratings: list = []
        self.content = _OAIContent(text)


class LLMResponse:
    """
    Returned by gemini_service when the OpenAI fallback fires.

    Duck-type compatible with the Gemini SDK response so that no call site
    needs to change:  .text  .parsed  .usage_metadata  .candidates[0].*
    """

    def __init__(
        self,
        text: str,
        parsed: Any = None,
        usage_metadata: Optional[_OAIUsageMeta] = None,
        candidates: Optional[list] = None,
        model: str = "",
        provider: str = "openai",
        fallback_from: Optional[str] = None,
    ) -> None:
        self.text = text
        self.parsed = parsed
        self.usage_metadata = usage_metadata or _OAIUsageMeta(0, 0, 0)
        self.candidates = candidates if candidates is not None else (
            [_OAICandidate(text)] if text else []
        )
        self.model = model
        self.provider = provider
        self.fallback_from = fallback_from

    @classmethod
    def from_openai(
        cls,
        raw: Any,
        *,
        response_schema: Any = None,
        fallback_from: Optional[str] = None,
    ) -> "LLMResponse":
        """
        Build an LLMResponse from a raw openai ChatCompletion object.

        Mapping:
          choices[0].message.content        → .text
          usage.prompt_tokens               → .usage_metadata.prompt_token_count
          usage.completion_tokens           → .usage_metadata.candidates_token_count
          "stop" finish_reason              → "STOP"  (angular_generator compat)
          response_schema.model_validate()  → .parsed
        """
        choice = raw.choices[0]
        content: str = choice.message.content or ""

        # Map OpenAI finish reason → Gemini-compatible value
        oai_finish = (choice.finish_reason or "stop").lower()
        finish_reason = "STOP" if oai_finish == "stop" else oai_finish.upper()

        # Structured output: parse JSON and validate against Pydantic schema
        parsed: Any = None
        if response_schema is not None and content:
            try:
                data = json.loads(content)
                if hasattr(response_schema, "model_validate"):
                    parsed = response_schema.model_validate(data)
                else:
                    parsed = data
            except Exception as parse_exc:
                logger.warning(
                    "LLMResponse.from_openai: JSON parse failed for schema %s: %s",
                    getattr(response_schema, "__name__", type(response_schema)),
                    parse_exc,
                )

        # Usage metadata (chat completions: prompt_tokens; responses API: input_tokens)
        usage = getattr(raw, "usage", None)
        prompt_tok = (
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", None)
            or 0
        )
        completion_tok = (
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", None)
            or 0
        )
        total_tok = getattr(usage, "total_tokens", None) or (prompt_tok + completion_tok)
        usage_meta = _OAIUsageMeta(
            prompt=prompt_tok,
            candidates=completion_tok,
            total=total_tok,
        )

        return cls(
            text=content,
            parsed=parsed,
            usage_metadata=usage_meta,
            candidates=[_OAICandidate(content, finish_reason)],
            model=getattr(raw, "model", ""),
            provider="openai",
            fallback_from=fallback_from,
        )


# ---------------------------------------------------------------------------
# Contents normalization (multimodal / legacy shapes)
# ---------------------------------------------------------------------------


def _pil_image_to_part(image: Any) -> types.Part:
    """Convert a PIL Image to a google.genai Part (bytes + mime_type)."""
    buf = io.BytesIO()
    fmt = image.format or "PNG"
    image.save(buf, format=fmt)
    mime_type = (
        image.get_format_mimetype()
        if hasattr(image, "get_format_mimetype")
        else f"image/{fmt.lower()}"
    )
    return types.Part.from_bytes(data=buf.getvalue(), mime_type=mime_type)


def _normalize_content_item(item: Any) -> Any:
    """Normalize a single content item for the new google.genai SDK."""
    # Legacy ai_processor Figma path: [prompt_str, PIL.Image]
    try:
        from PIL import Image as PILImage

        if isinstance(item, PILImage.Image):
            return _pil_image_to_part(item)
    except ImportError:
        pass

    # Legacy google.generativeai dict image payload: {"mime_type": "...", "data": bytes}
    if isinstance(item, dict) and "mime_type" in item and "data" in item:
        return types.Part.from_bytes(data=item["data"], mime_type=item["mime_type"])

    return item


def _normalize_contents(contents: Any) -> Any:
    """Adapt caller contents for google.genai (str, list, PIL images, legacy dicts)."""
    if contents is None or isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        return [_normalize_content_item(item) for item in contents]
    return _normalize_content_item(contents)


# ---------------------------------------------------------------------------
# Core call wrappers
# ---------------------------------------------------------------------------


async def _async_gemini_generate(
    model: str,
    normalized_contents: Any,
    sdk_config: Any,
    api_key: Optional[str],
) -> Any:
    """
    Internal: run Gemini generate_content with backoff + key rotation.

    Raises the last retryable exception when all keys are exhausted so that
    the caller (async_generate) can decide whether to try an OpenAI fallback.
    """
    last_exc: Optional[Exception] = None
    allow_rotation = api_key is None

    while True:
        current_key = api_key if api_key is not None else get_gemini_api_key()
        client = genai.Client(api_key=current_key) if current_key else genai.Client()

        for attempt in range(_MAX_RETRIES_PER_KEY + 1):
            try:
                return await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=normalized_contents,
                        config=sdk_config,
                    ),
                    timeout=_ASYNC_CALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                # Hard network stall — not a rate-limit, do not retry.
                logger.error(
                    "gemini_service: generate_content timed out after %ss "
                    "(attempt %s/%s, model=%s)",
                    _ASYNC_CALL_TIMEOUT_SECONDS,
                    attempt + 1,
                    _MAX_RETRIES_PER_KEY + 1,
                    model,
                )
                raise
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_gemini_error(exc):
                    raise
                if attempt >= _MAX_RETRIES_PER_KEY:
                    break
                delay = _backoff_delay_seconds(attempt)
                logger.warning(
                    "gemini_service: retryable error (attempt %s/%s, backoff %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES_PER_KEY,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        if not allow_rotation:
            assert last_exc is not None
            raise last_exc

        if _rotate_to_next_api_key() is None:
            assert last_exc is not None
            raise last_exc

        logger.info("gemini_service: retrying after API key rotation")


async def async_generate(
    model: str,
    contents: Any,
    *,
    config: Optional[GeminiGenerateConfig] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Async Gemini generate_content call with optional OpenAI fallback.

    Returns the raw Gemini SDK response on success, or an LLMResponse
    (duck-type compatible) when the OpenAI fallback fires.

    Fallback fires when:
      - LLM_OPENAI_FALLBACK_ENABLED=true
      - Gemini is exhausted (all keys / retries hit 429 or 503)
      - Model is gemini-3* (or 2.5* with LLM_OPENAI_FALLBACK_ALL=true)
      - No explicit api_key was passed
      - No Gemini-only tools (e.g. google_search_tool) are in the config

    On 429 / 503: exponential backoff (3 retries per key), then key rotation.
    Explicit *api_key* disables both rotation and fallback.
    """
    from services.workflow_budget_service import check_before_llm_call

    check_before_llm_call()

    cfg = config or GeminiGenerateConfig()
    sdk_config = _build_sdk_config(model, cfg)
    normalized = _normalize_contents(contents)

    gemini_exc: Optional[Exception] = None
    try:
        return await _async_gemini_generate(model, normalized, sdk_config, api_key)
    except Exception as exc:
        # Non-fallback-eligible errors propagate immediately; fallback-eligible ones are
        # candidates for the OpenAI fallback path.
        if not _is_fallback_eligible_error(exc):
            raise
        gemini_exc = exc

    # Gemini exhausted with a fallback-eligible error — try OpenAI fallback.
    if not _fallback_eligible(model, cfg, explicit_api_key=api_key):
        assert gemini_exc is not None
        raise gemini_exc

    openai_model = resolve_openai_model(model, cfg.fallback_profile)
    logger.warning(
        "gemini_service: Gemini exhausted (%s) model=%s → OpenAI fallback model=%s (profile=%s)",
        gemini_exc,
        model,
        openai_model,
        cfg.fallback_profile,
    )

    try:
        from services import openai_service  # lazy import — keeps module self-contained

        raw_oai = await openai_service.async_generate(
            openai_model,
            contents,  # pass original (openai_service normalizes independently)
            temperature=cfg.temperature,
            max_tokens=cfg.max_output_tokens,
            response_mime_type=cfg.response_mime_type,
            response_schema=cfg.response_schema,
            top_p=cfg.top_p,
            stop_sequences=cfg.stop_sequences,
        )
        return LLMResponse.from_openai(
            raw_oai,
            response_schema=cfg.response_schema,
            fallback_from=model,
        )
    except Exception as oai_exc:
        logger.error(
            "gemini_service: OpenAI fallback also failed (model=%s): %s",
            openai_model,
            oai_exc,
        )
        assert gemini_exc is not None
        raise gemini_exc from oai_exc


def sync_generate(
    model: str,
    contents: Any,
    *,
    config: Optional[GeminiGenerateConfig] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Synchronous Gemini generate_content call with optional OpenAI fallback.

    Returns the raw Gemini SDK response on success, or an LLMResponse
    (duck-type compatible) when the OpenAI fallback fires.

    Same fallback policy as async_generate — see its docstring for details.
    Use inside ``run_in_executor`` / ``run_sync`` to avoid blocking the loop.
    """
    from services.workflow_budget_service import check_before_llm_call

    check_before_llm_call()

    cfg = config or GeminiGenerateConfig()
    sdk_config = _build_sdk_config(model, cfg)
    normalized = _normalize_contents(contents)

    def _gemini_call(key: Optional[str]) -> Any:
        import concurrent.futures as _cf

        client = genai.Client(api_key=key) if key else genai.Client()

        # Wrap in a dedicated future so we can impose a hard wall-clock cap.
        # Running the HTTP call inside yet another thread pool slot is cheap
        # because sync_generate itself already executes inside run_in_executor.
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(
                client.models.generate_content,
                model=model,
                contents=normalized,
                config=sdk_config,
            )
            try:
                return _fut.result(timeout=_SYNC_CALL_TIMEOUT_SECONDS)
            except _cf.TimeoutError as _te:
                logger.error(
                    "gemini_service: sync generate_content timed out after %ss",
                    _SYNC_CALL_TIMEOUT_SECONDS,
                )
                raise TimeoutError(
                    f"Sync Gemini call timed out after {_SYNC_CALL_TIMEOUT_SECONDS}s"
                ) from _te

    gemini_exc: Optional[Exception] = None
    try:
        return _generate_with_retry(
            _gemini_call,
            explicit_api_key=api_key,
            sleep_fn=time.sleep,
        )
    except Exception as exc:
        if not _is_fallback_eligible_error(exc):
            raise
        gemini_exc = exc

    # Gemini exhausted with a fallback-eligible error — try OpenAI fallback.
    if not _fallback_eligible(model, cfg, explicit_api_key=api_key):
        assert gemini_exc is not None
        raise gemini_exc

    openai_model = resolve_openai_model(model, cfg.fallback_profile)
    logger.warning(
        "gemini_service: Gemini exhausted (%s) model=%s → OpenAI fallback model=%s (profile=%s)",
        gemini_exc,
        model,
        openai_model,
        cfg.fallback_profile,
    )

    try:
        from services import openai_service  # lazy import

        raw_oai = openai_service.sync_generate(
            openai_model,
            contents,
            temperature=cfg.temperature,
            max_tokens=cfg.max_output_tokens,
            response_mime_type=cfg.response_mime_type,
            response_schema=cfg.response_schema,
            top_p=cfg.top_p,
            stop_sequences=cfg.stop_sequences,
        )
        return LLMResponse.from_openai(
            raw_oai,
            response_schema=cfg.response_schema,
            fallback_from=model,
        )
    except Exception as oai_exc:
        logger.error(
            "gemini_service: OpenAI fallback also failed (model=%s): %s",
            openai_model,
            oai_exc,
        )
        assert gemini_exc is not None
        raise gemini_exc from oai_exc
