"""Temporary SDK + HTTP monkeypatches to capture nested LLM calls."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

from workflow_observability.http_hooks import HttpPatchSession, suppress_http_llm_record
from workflow_observability.models import SpanDocument, new_span_id, utc_now_iso


def _google_usage(resp: Any) -> Optional[dict]:
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return None
    prompt = int(getattr(um, "prompt_token_count", 0) or 0)
    completion = int(getattr(um, "candidates_token_count", 0) or 0)
    total = int(getattr(um, "total_token_count", 0) or (prompt + completion))
    if total <= 0:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


class SdkPatchSession:
    def __init__(self, *, emit_llm_span: Callable[[SpanDocument], None], parent_span_id: Optional[str], trace_id: str, kind: str, metadata: dict):
        self._emit = emit_llm_span
        self._parent_span_id = parent_span_id
        self._trace_id = trace_id
        self._kind = kind
        self._metadata = metadata
        self._restores: list[tuple[Any, str, Any]] = []
        self._http: Optional[HttpPatchSession] = None

    def _record(self, *, provider: str, model: str, prompt: Any, completion: Any, latency_ms: float, token_usage: Optional[dict] = None, status: str = "success", error: Optional[str] = None) -> None:
        span = SpanDocument(
            span_id=new_span_id(),
            trace_id=self._trace_id,
            parent_span_id=self._parent_span_id,
            kind=self._kind,
            name=f"{provider}:{model}",
            status=status,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            prompt=prompt,
            completion=completion,
            token_usage=token_usage,
            error=error,
            ended_at=utc_now_iso(),
            metadata=dict(self._metadata),
        )
        try:
            self._emit(span)
        except Exception:
            pass

    def _patch(self, module: Any, attr: str, wrapper_factory: Callable[[Any], Any]) -> None:
        if module is None:
            return
        original = getattr(module, attr, None)
        if original is None or not callable(original):
            return
        wrapped = wrapper_factory(original)
        setattr(module, attr, wrapped)
        self._restores.append((module, attr, original))

    def install(self) -> None:
        self._patch_google()
        self._patch_google_genai()
        self._patch_openai()
        self._patch_anthropic()
        try:
            self._http = HttpPatchSession(self._record)
            self._http.install()
        except Exception:
            self._http = None

    def restore(self) -> None:
        if self._http is not None:
            try:
                self._http.restore()
            except Exception:
                pass
            self._http = None
        for module, attr, original in reversed(self._restores):
            try:
                setattr(module, attr, original)
            except Exception:
                pass
        self._restores.clear()

    def _patch_google(self) -> None:
        try:
            import google.generativeai as genai  # type: ignore
        except Exception:
            return

        original_cls = getattr(genai, "GenerativeModel", None)
        if original_cls is None:
            return

        session = self
        original_sync = getattr(original_cls, "generate_content", None)
        if original_sync is not None:
            def wrapped_sync(self_model, *args, **kwargs):
                started = time.perf_counter()
                prompt = args[0] if args else kwargs.get("contents")
                model_name = getattr(self_model, "model_name", None) or getattr(self_model, "_model_name", "gemini")
                try:
                    with suppress_http_llm_record():
                        resp = original_sync(self_model, *args, **kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    completion = getattr(resp, "text", None) or str(resp)
                    session._record(
                        provider="google",
                        model=str(model_name),
                        prompt=prompt,
                        completion=completion,
                        latency_ms=latency_ms,
                        token_usage=_google_usage(resp),
                    )
                    return resp
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    session._record(
                        provider="google",
                        model=str(model_name),
                        prompt=prompt,
                        completion=None,
                        latency_ms=latency_ms,
                        status="error",
                        error=str(exc),
                    )
                    raise

            self._patch(original_cls, "generate_content", lambda _: wrapped_sync)

        original_async = getattr(original_cls, "generate_content_async", None)
        if original_async is not None:
            async def wrapped_async(self_model, *args, **kwargs):
                started = time.perf_counter()
                prompt = args[0] if args else kwargs.get("contents")
                model_name = getattr(self_model, "model_name", None) or getattr(self_model, "_model_name", "gemini")
                try:
                    with suppress_http_llm_record():
                        resp = await original_async(self_model, *args, **kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    completion = getattr(resp, "text", None) or str(resp)
                    session._record(
                        provider="google",
                        model=str(model_name),
                        prompt=prompt,
                        completion=completion,
                        latency_ms=latency_ms,
                        token_usage=_google_usage(resp),
                    )
                    return resp
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    session._record(
                        provider="google",
                        model=str(model_name),
                        prompt=prompt,
                        completion=None,
                        latency_ms=latency_ms,
                        status="error",
                        error=str(exc),
                    )
                    raise

            self._patch(original_cls, "generate_content_async", lambda _: wrapped_async)

    def _patch_google_genai(self) -> None:
        """Patch the newer ``google.genai`` Client models API (replaces generativeai)."""
        try:
            from google.genai import models as genai_models  # type: ignore
        except Exception:
            return

        session = self

        def _usage_from_resp(resp: Any) -> Optional[dict]:
            usage = _google_usage(resp)
            if usage:
                return usage
            um = getattr(resp, "usage_metadata", None)
            if um is None and isinstance(resp, dict):
                um = resp.get("usage_metadata") or resp.get("usageMetadata")
            if isinstance(um, dict):
                prompt = int(um.get("prompt_token_count") or um.get("promptTokenCount") or 0)
                completion = int(
                    um.get("candidates_token_count") or um.get("candidatesTokenCount") or 0
                )
                total = int(um.get("total_token_count") or um.get("totalTokenCount") or 0) or (
                    prompt + completion
                )
                if total > 0:
                    return {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": total,
                    }
            return None

        def _text_from_resp(resp: Any) -> Any:
            text = getattr(resp, "text", None)
            if text:
                return text
            try:
                return str(resp)
            except Exception:
                return None

        models_cls = getattr(genai_models, "Models", None)
        if models_cls is not None:
            original = getattr(models_cls, "generate_content", None)
            if original is not None:

                def wrapped_sync(models_self, *args, **kwargs):
                    started = time.perf_counter()
                    model_name = kwargs.get("model") or (args[0] if args else "gemini")
                    prompt = kwargs.get("contents")
                    if prompt is None and len(args) > 1:
                        prompt = args[1]
                    try:
                        with suppress_http_llm_record():
                            resp = original(models_self, *args, **kwargs)
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        session._record(
                            provider="google",
                            model=str(model_name),
                            prompt=prompt,
                            completion=_text_from_resp(resp),
                            latency_ms=latency_ms,
                            token_usage=_usage_from_resp(resp),
                        )
                        return resp
                    except Exception as exc:
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        session._record(
                            provider="google",
                            model=str(model_name),
                            prompt=prompt,
                            completion=None,
                            latency_ms=latency_ms,
                            status="error",
                            error=str(exc),
                        )
                        raise

                self._patch(models_cls, "generate_content", lambda _: wrapped_sync)

        async_cls = getattr(genai_models, "AsyncModels", None)
        if async_cls is not None:
            original_async = getattr(async_cls, "generate_content", None)
            if original_async is not None:

                async def wrapped_async(models_self, *args, **kwargs):
                    started = time.perf_counter()
                    model_name = kwargs.get("model") or (args[0] if args else "gemini")
                    prompt = kwargs.get("contents")
                    if prompt is None and len(args) > 1:
                        prompt = args[1]
                    try:
                        with suppress_http_llm_record():
                            resp = await original_async(models_self, *args, **kwargs)
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        session._record(
                            provider="google",
                            model=str(model_name),
                            prompt=prompt,
                            completion=_text_from_resp(resp),
                            latency_ms=latency_ms,
                            token_usage=_usage_from_resp(resp),
                        )
                        return resp
                    except Exception as exc:
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        session._record(
                            provider="google",
                            model=str(model_name),
                            prompt=prompt,
                            completion=None,
                            latency_ms=latency_ms,
                            status="error",
                            error=str(exc),
                        )
                        raise

                self._patch(async_cls, "generate_content", lambda _: wrapped_async)

    def _patch_openai(self) -> None:
        try:
            from openai.resources.chat.completions import Completions  # type: ignore
        except Exception:
            try:
                from openai import OpenAI  # type: ignore
                Completions = OpenAI.chat.completions.__class__  # type: ignore
            except Exception:
                return

        session = self
        original = Completions.create

        def wrapped_create(completions_self, *args, **kwargs):
            started = time.perf_counter()
            model = kwargs.get("model") or (args[0] if args else "openai")
            messages = kwargs.get("messages")
            try:
                with suppress_http_llm_record():
                    resp = original(completions_self, *args, **kwargs)
                latency_ms = (time.perf_counter() - started) * 1000.0
                completion = None
                usage = None
                try:
                    choice = resp.choices[0]
                    completion = getattr(getattr(choice, "message", None), "content", None)
                    usage_obj = getattr(resp, "usage", None)
                    if usage_obj is not None:
                        usage = {
                            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                        }
                except Exception:
                    pass
                session._record(
                    provider="openai",
                    model=str(model),
                    prompt=messages,
                    completion=completion,
                    latency_ms=latency_ms,
                    token_usage=usage,
                )
                return resp
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                session._record(
                    provider="openai",
                    model=str(model),
                    prompt=messages,
                    completion=None,
                    latency_ms=latency_ms,
                    status="error",
                    error=str(exc),
                )
                raise

        self._patch(Completions, "create", lambda _: wrapped_create)

        try:
            from openai.resources.chat.completions import AsyncCompletions  # type: ignore

            original_async = AsyncCompletions.create

            async def wrapped_async(completions_self, *args, **kwargs):
                started = time.perf_counter()
                model = kwargs.get("model") or (args[0] if args else "openai")
                messages = kwargs.get("messages")
                try:
                    with suppress_http_llm_record():
                        resp = await original_async(completions_self, *args, **kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    completion = None
                    usage = None
                    try:
                        choice = resp.choices[0]
                        completion = getattr(getattr(choice, "message", None), "content", None)
                        usage_obj = getattr(resp, "usage", None)
                        if usage_obj is not None:
                            usage = {
                                "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                                "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                            }
                    except Exception:
                        pass
                    session._record(
                        provider="openai",
                        model=str(model),
                        prompt=messages,
                        completion=completion,
                        latency_ms=latency_ms,
                        token_usage=usage,
                    )
                    return resp
                except Exception as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    session._record(
                        provider="openai",
                        model=str(model),
                        prompt=messages,
                        completion=None,
                        latency_ms=latency_ms,
                        status="error",
                        error=str(exc),
                    )
                    raise

            self._patch(AsyncCompletions, "create", lambda _: wrapped_async)
        except Exception:
            pass

    def _patch_anthropic(self) -> None:
        try:
            from anthropic.resources.messages import Messages  # type: ignore
        except Exception:
            return

        session = self
        original = Messages.create

        def wrapped_create(messages_self, *args, **kwargs):
            started = time.perf_counter()
            model = kwargs.get("model") or "claude"
            messages = kwargs.get("messages")
            try:
                with suppress_http_llm_record():
                    resp = original(messages_self, *args, **kwargs)
                latency_ms = (time.perf_counter() - started) * 1000.0
                completion = None
                usage = None
                try:
                    content = getattr(resp, "content", None)
                    if content:
                        completion = getattr(content[0], "text", None)
                    usage_obj = getattr(resp, "usage", None)
                    if usage_obj is not None:
                        usage = {
                            "prompt_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                            "completion_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
                            "total_tokens": int(
                                (getattr(usage_obj, "input_tokens", 0) or 0)
                                + (getattr(usage_obj, "output_tokens", 0) or 0)
                            ),
                        }
                except Exception:
                    pass
                session._record(
                    provider="anthropic",
                    model=str(model),
                    prompt=messages,
                    completion=completion,
                    latency_ms=latency_ms,
                    token_usage=usage,
                )
                return resp
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                session._record(
                    provider="anthropic",
                    model=str(model),
                    prompt=messages,
                    completion=None,
                    latency_ms=latency_ms,
                    status="error",
                    error=str(exc),
                )
                raise

        self._patch(Messages, "create", lambda _: wrapped_create)


@contextmanager
def sdk_patch_context(
    *,
    emit_llm_span: Callable[[SpanDocument], None],
    parent_span_id: Optional[str],
    trace_id: str,
    kind: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    session = SdkPatchSession(
        emit_llm_span=emit_llm_span,
        parent_span_id=parent_span_id,
        trace_id=trace_id,
        kind=kind,
        metadata=metadata or {},
    )
    session.install()
    try:
        yield
    finally:
        session.restore()
