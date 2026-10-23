"""HTTP hooks for REST-based LLM calls (Gemini / OpenAI / Anthropic)."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

_tls = threading.local()

LLM_HOST_SUFFIXES = (
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
    "api.groq.com",
    "api.mistral.ai",
)


def host_is_llm(url: Any) -> bool:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in LLM_HOST_SUFFIXES)


def provider_from_url(url: Any) -> str:
    host = ""
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return "http"
    if "googleapis" in host or "aiplatform" in host:
        return "google"
    if "openai" in host:
        return "openai"
    if "anthropic" in host:
        return "anthropic"
    if "groq" in host:
        return "groq"
    if "mistral" in host:
        return "mistral"
    return "http"


def model_from_url(url: Any) -> str:
    try:
        path = urlparse(str(url)).path or ""
    except Exception:
        return "llm"
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part in {"models", "model"} and i + 1 < len(parts):
            return parts[i + 1].split(":")[0]
    return parts[-1] if parts else "llm"


def _int_field(obj: dict, *names: str) -> int:
    for name in names:
        value = obj.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def token_usage_from_http_body(body: Any) -> Optional[dict]:
    """Parse token counts from Gemini / OpenAI / Anthropic REST JSON bodies."""
    if not isinstance(body, dict):
        return None
    um = body.get("usageMetadata") or body.get("usage_metadata")
    if isinstance(um, dict):
        prompt = _int_field(um, "promptTokenCount", "prompt_token_count", "inputTokenCount")
        completion = _int_field(
            um,
            "candidatesTokenCount",
            "candidates_token_count",
            "outputTokenCount",
            "output_token_count",
        )
        total = _int_field(um, "totalTokenCount", "total_token_count")
        if total <= 0:
            total = prompt + completion
        if total > 0:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
    usage = body.get("usage")
    if isinstance(usage, dict):
        prompt = _int_field(usage, "prompt_tokens", "input_tokens")
        completion = _int_field(usage, "completion_tokens", "output_tokens")
        total = _int_field(usage, "total_tokens") or (prompt + completion)
        if total > 0:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
    return None


def _http_record_blocked() -> bool:
    return getattr(_tls, "recording", 0) > 0 or getattr(_tls, "suppress", 0) > 0


@contextmanager
def suppress_http_llm_record() -> Iterator[None]:
    """Skip HTTP LLM spans while an SDK wrapper already records the same call."""
    _tls.suppress = getattr(_tls, "suppress", 0) + 1
    try:
        yield
    finally:
        _tls.suppress = max(0, getattr(_tls, "suppress", 0) - 1)


@contextmanager
def _http_record_guard() -> Iterator[bool]:
    """Allow only the outermost HTTP wrapper to emit a span (post → request → Client)."""
    if _http_record_blocked():
        yield False
        return
    _tls.recording = getattr(_tls, "recording", 0) + 1
    try:
        yield True
    finally:
        _tls.recording = max(0, getattr(_tls, "recording", 0) - 1)


class HttpPatchSession:
    def __init__(self, record: Callable[..., None]):
        self._record = record
        self._restores: list[tuple[Any, str, Any]] = []

    def _patch(self, obj: Any, attr: str, wrapper: Any) -> None:
        original = getattr(obj, attr, None)
        if original is None or not callable(original):
            return
        setattr(obj, attr, wrapper)
        self._restores.append((obj, attr, original))

    def install(self) -> None:
        self._patch_httpx()
        self._patch_requests()

    def restore(self) -> None:
        for obj, attr, original in reversed(self._restores):
            try:
                setattr(obj, attr, original)
            except Exception:
                pass
        self._restores.clear()

    def _extract_url(self, args, kwargs) -> Any:
        url = kwargs.get("url")
        if url:
            return url
        for arg in args:
            if isinstance(arg, str) and arg.startswith(("http://", "https://")):
                return arg
        return None

    def _wrap_sync(self, original: Callable) -> Callable:
        record = self._record

        def wrapped(*args, **kwargs):
            url = self._extract_url(args, kwargs)
            if not host_is_llm(url):
                return original(*args, **kwargs)
            with _http_record_guard() as should_record:
                if not should_record:
                    return original(*args, **kwargs)
                started = time.perf_counter()
                prompt = kwargs.get("json") or kwargs.get("data")
                try:
                    resp = original(*args, **kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    completion = None
                    try:
                        completion = resp.json() if hasattr(resp, "json") else getattr(resp, "text", None)
                    except Exception:
                        completion = getattr(resp, "text", None)
                    record(
                        provider=provider_from_url(url),
                        model=model_from_url(url),
                        prompt=prompt,
                        completion=completion,
                        latency_ms=latency_ms,
                        token_usage=token_usage_from_http_body(completion),
                    )
                    return resp
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    record(
                        provider=provider_from_url(url),
                        model=model_from_url(url),
                        prompt=prompt,
                        completion=None,
                        latency_ms=latency_ms,
                        status="error",
                        error=str(exc),
                    )
                    raise

        return wrapped

    def _wrap_async(self, original: Callable) -> Callable:
        record = self._record

        async def wrapped(*args, **kwargs):
            url = self._extract_url(args, kwargs)
            if not host_is_llm(url):
                return await original(*args, **kwargs)
            with _http_record_guard() as should_record:
                if not should_record:
                    return await original(*args, **kwargs)
                started = time.perf_counter()
                prompt = kwargs.get("json") or kwargs.get("data")
                try:
                    resp = await original(*args, **kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    completion = None
                    try:
                        if hasattr(resp, "json"):
                            maybe = resp.json()
                            completion = await maybe if hasattr(maybe, "__await__") else maybe
                        else:
                            completion = getattr(resp, "text", None)
                    except Exception:
                        completion = getattr(resp, "text", None)
                    record(
                        provider=provider_from_url(url),
                        model=model_from_url(url),
                        prompt=prompt,
                        completion=completion,
                        latency_ms=latency_ms,
                        token_usage=token_usage_from_http_body(completion),
                    )
                    return resp
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    record(
                        provider=provider_from_url(url),
                        model=model_from_url(url),
                        prompt=prompt,
                        completion=None,
                        latency_ms=latency_ms,
                        status="error",
                        error=str(exc),
                    )
                    raise

        return wrapped

    def _patch_httpx(self) -> None:
        try:
            import httpx  # type: ignore
        except Exception:
            return
        if getattr(httpx, "post", None):
            self._patch(httpx, "post", self._wrap_sync(httpx.post))
        if getattr(httpx, "request", None):
            self._patch(httpx, "request", self._wrap_sync(httpx.request))
        try:
            self._patch(httpx.Client, "request", self._wrap_sync(httpx.Client.request))
        except Exception:
            pass
        try:
            self._patch(httpx.AsyncClient, "request", self._wrap_async(httpx.AsyncClient.request))
        except Exception:
            pass

    def _patch_requests(self) -> None:
        try:
            import requests  # type: ignore
        except Exception:
            return
        if getattr(requests, "post", None):
            self._patch(requests, "post", self._wrap_sync(requests.post))
        if getattr(requests, "request", None):
            self._patch(requests, "request", self._wrap_sync(requests.request))
        try:
            self._patch(requests.Session, "request", self._wrap_sync(requests.Session.request))
        except Exception:
            pass
