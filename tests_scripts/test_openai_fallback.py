"""
test_openai_fallback.py
=======================
Tests for the Gemini → OpenAI fallback system.

Run from the repo root:
    cd backend && python3 -m pytest ../tests_scripts/test_openai_fallback.py -v

Or run directly (no pytest needed):
    cd backend && python3 ../tests_scripts/test_openai_fallback.py

What is tested (no real API calls required for most tests):
  Unit tests  — pure logic, zero network I/O
    1.  resolve_openai_model   — all model + profile combinations
    2.  _fallback_eligible     — all eligibility gates
    3.  LLMResponse.from_openai — duck-typing: .text, .parsed, .usage_metadata,
                                  .candidates[0].finish_reason, .safety_ratings,
                                  .candidates[0].content.parts
    4.  LLMResponse with Pydantic schema — parsed field populated correctly
    5.  openai_service key pool — env vars loaded, deduped, ordered
    6.  normalize_to_openai_messages — str / list / multimodal shapes
    7.  Fallback disabled guard — LLM_OPENAI_FALLBACK_ALL=false blocks 2.5 models
    8.  Tools guard — calls with tools= are never eligible for fallback
    9.  Explicit api_key guard — explicit key disables fallback
    10. angular_generator finish_reason compat — "stop" → "STOP" in (1, "STOP")
    11. content_filter finish_reason — NOT in (1, "STOP"), error path correct
    12. extract_text works on LLMResponse — via candidates fallback path

  Integration tests (real OpenAI API call — uses key from .env)
    13. openai_service.sync_generate — real text call, verify .text non-empty
    14. openai_service.async_generate — real async text call
    15. openai_service JSON mode — response_mime_type="application/json" round-trip
    16. Full fallback path via gemini_service.async_generate mock — Gemini raises
        503, fallback fires, OpenAI responds, LLMResponse returned

Run only unit tests (skip real API calls):
    cd backend && python3 ../tests_scripts/test_openai_fallback.py --unit-only
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import unittest
import unittest.mock as mock
from types import SimpleNamespace
from typing import Any

# ── Path setup ──────────────────────────────────────────────────────────────
# Allow imports from backend/ when running from repo root or tests_scripts/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.join(_SCRIPT_DIR, "..", "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# Load .env so OpenAI keys are available for integration tests
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ── Argument parsing ─────────────────────────────────────────────────────────
_UNIT_ONLY = "--unit-only" in sys.argv

# ── Lazy-import helpers (avoid importing google.genai at module level) ───────

def _import_gemini_service():
    """Import gemini_service with google.genai mocked out."""
    google_mock = mock.MagicMock()
    genai_mock = mock.MagicMock()
    google_mock.genai = genai_mock
    with mock.patch.dict("sys.modules", {
        "google": google_mock,
        "google.genai": genai_mock,
        "google.genai.types": mock.MagicMock(),
        "google.cloud": mock.MagicMock(),
        "google.cloud.secretmanager": mock.MagicMock(),
    }):
        import importlib
        import services.gemini_service as gs
        importlib.reload(gs)
    return gs


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Pure unit tests (no network)
# ═══════════════════════════════════════════════════════════════════════════

class TestResolveOpenAIModel(unittest.TestCase):
    """resolve_openai_model returns the correct OpenAI model for every Gemini id."""

    def setUp(self):
        self.gs = _import_gemini_service()

    def test_planner_profile(self):
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.1-pro-preview", "planner"),
            "gpt-5.5",
        )

    def test_coding_profile(self):
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.1-pro-preview", "coding"),
            "gpt-5.3-codex",
        )

    def test_default_profile_is_planner(self):
        """gemini-3.1-pro-preview with no profile should use the planner default."""
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.1-pro-preview"),
            "gpt-5.5",
        )

    def test_alias_without_preview(self):
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.1-pro", "planner"),
            "gpt-5.5",
        )
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.1-pro", "coding"),
            "gpt-5.3-codex",
        )

    def test_flash_models(self):
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.5-flash"),
            "gpt-5.3-codex",
        )
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3-flash-preview"),
            "gpt-5.4-mini",
        )
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.5-flash-lite"),
            "gpt-5.4-mini",
        )
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-3.6-flash"),
            "gpt-5.3-codex",
        )

    def test_25_series_mapping_exists(self):
        """2.5 models have mappings even if fallback is disabled for them by default."""
        self.assertEqual(self.gs.resolve_openai_model("gemini-2.5-flash"), "gpt-5.4-mini")
        self.assertEqual(self.gs.resolve_openai_model("gemini-2.5-pro"), "gpt-5.3-codex")

    def test_provider_prefix_stripped(self):
        """gemini/ prefix is stripped before lookup."""
        self.assertEqual(
            self.gs.resolve_openai_model("gemini/gemini-3.1-pro-preview", "coding"),
            "gpt-5.3-codex",
        )
        self.assertEqual(
            self.gs.resolve_openai_model("gemini/gemini-3-flash-preview"),
            "gpt-5.4-mini",
        )

    def test_unknown_model_returns_default(self):
        self.assertEqual(
            self.gs.resolve_openai_model("gemini-99-ultra"),
            "gpt-5.3-codex",
        )


class TestFallbackEligibility(unittest.TestCase):
    """_fallback_eligible enforces all eligibility gates correctly."""

    def setUp(self):
        self.gs = _import_gemini_service()
        # Force master switch ON for these tests
        self.gs._OPENAI_FALLBACK_ENABLED = True
        self.gs._OPENAI_FALLBACK_ALL = False

    def _cfg(self, tools=None):
        return self.gs.GeminiGenerateConfig(tools=tools)

    def test_3x_models_eligible(self):
        for model in [
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3-flash-preview",
        ]:
            with self.subTest(model=model):
                self.assertTrue(self.gs._fallback_eligible(model, self._cfg(), explicit_api_key=None))

    def test_25_models_blocked_when_flag_off(self):
        """2.5 models must NOT fall back unless LLM_OPENAI_FALLBACK_ALL=true."""
        for model in ["gemini-2.5-flash", "gemini-2.5-pro"]:
            with self.subTest(model=model):
                self.assertFalse(self.gs._fallback_eligible(model, self._cfg(), explicit_api_key=None))

    def test_25_models_eligible_when_flag_on(self):
        self.gs._OPENAI_FALLBACK_ALL = True
        for model in ["gemini-2.5-flash", "gemini-2.5-pro"]:
            with self.subTest(model=model):
                self.assertTrue(self.gs._fallback_eligible(model, self._cfg(), explicit_api_key=None))

    def test_master_switch_blocks_all(self):
        self.gs._OPENAI_FALLBACK_ENABLED = False
        self.assertFalse(
            self.gs._fallback_eligible("gemini-3.1-pro-preview", self._cfg(), explicit_api_key=None)
        )

    def test_explicit_api_key_blocks_fallback(self):
        self.assertFalse(
            self.gs._fallback_eligible("gemini-3.1-pro", self._cfg(), explicit_api_key="some-key")
        )

    def test_tools_block_fallback(self):
        """Calls with tools (e.g. Google Search) must never fall back."""
        cfg_with_tools = self._cfg(tools=["google_search"])
        self.assertFalse(
            self.gs._fallback_eligible("gemini-3.1-pro-preview", cfg_with_tools, explicit_api_key=None)
        )


class TestLLMResponse(unittest.TestCase):
    """LLMResponse.from_openai produces correct duck-type-compatible output."""

    def setUp(self):
        self.gs = _import_gemini_service()

    def _fake_openai_response(self, content: str, finish_reason: str = "stop", model: str = "gpt-5.5"):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=200, total_tokens=300)
        choice = SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )
        return SimpleNamespace(choices=[choice], usage=usage, model=model)

    def test_text_extracted(self):
        raw = self._fake_openai_response("Hello world")
        resp = self.gs.LLMResponse.from_openai(raw)
        self.assertEqual(resp.text, "Hello world")

    def test_provider_is_openai(self):
        raw = self._fake_openai_response("Hi")
        resp = self.gs.LLMResponse.from_openai(raw)
        self.assertEqual(resp.provider, "openai")

    def test_fallback_from_recorded(self):
        raw = self._fake_openai_response("Hi")
        resp = self.gs.LLMResponse.from_openai(raw, fallback_from="gemini-3.1-pro-preview")
        self.assertEqual(resp.fallback_from, "gemini-3.1-pro-preview")

    def test_usage_metadata_mapped(self):
        raw = self._fake_openai_response("Hi")
        resp = self.gs.LLMResponse.from_openai(raw)
        self.assertEqual(resp.usage_metadata.prompt_token_count, 100)
        self.assertEqual(resp.usage_metadata.candidates_token_count, 200)
        self.assertEqual(resp.usage_metadata.total_token_count, 300)
        # scratch_graph_planner reads thoughts_token_count
        self.assertEqual(resp.usage_metadata.thoughts_token_count, 0)

    def test_finish_reason_stop_maps_to_STOP(self):
        """angular_generator checks: candidate.finish_reason not in (1, 'STOP')"""
        raw = self._fake_openai_response("Hi", finish_reason="stop")
        resp = self.gs.LLMResponse.from_openai(raw)
        candidate = resp.candidates[0]
        self.assertEqual(candidate.finish_reason, "STOP")
        # Must pass angular_generator gate
        self.assertIn(candidate.finish_reason, (1, "STOP"))

    def test_finish_reason_content_filter(self):
        """content_filter must NOT be in (1, 'STOP') so error path fires correctly."""
        raw = self._fake_openai_response("blocked", finish_reason="content_filter")
        resp = self.gs.LLMResponse.from_openai(raw)
        self.assertNotIn(resp.candidates[0].finish_reason, (1, "STOP"))

    def test_safety_ratings_empty_list(self):
        raw = self._fake_openai_response("Hi")
        resp = self.gs.LLMResponse.from_openai(raw)
        self.assertEqual(resp.candidates[0].safety_ratings, [])

    def test_candidates_content_parts(self):
        """ai_processor iterates c.content.parts and reads getattr(p, 'text', '')."""
        raw = self._fake_openai_response("Some text")
        resp = self.gs.LLMResponse.from_openai(raw)
        parts = resp.candidates[0].content.parts
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].text, "Some text")

    def test_extract_text_helper_works(self):
        """gemini_service.extract_text() must work on LLMResponse too."""
        raw = self._fake_openai_response("extracted text")
        resp = self.gs.LLMResponse.from_openai(raw)
        # extract_text tries response.text first
        self.assertEqual(self.gs.extract_text(resp), "extracted text")

    def test_pydantic_schema_parsed(self):
        from pydantic import BaseModel

        class MyResult(BaseModel):
            answer: str
            score: int

        payload = json.dumps({"answer": "yes", "score": 42})
        raw = self._fake_openai_response(payload)
        resp = self.gs.LLMResponse.from_openai(raw, response_schema=MyResult)
        self.assertIsInstance(resp.parsed, MyResult)
        self.assertEqual(resp.parsed.answer, "yes")
        self.assertEqual(resp.parsed.score, 42)

    def test_invalid_json_schema_parsed_is_none(self):
        from pydantic import BaseModel

        class MyResult(BaseModel):
            answer: str

        raw = self._fake_openai_response("not json at all <<<")
        resp = self.gs.LLMResponse.from_openai(raw, response_schema=MyResult)
        self.assertIsNone(resp.parsed)

    def test_empty_content_no_candidates(self):
        raw = self._fake_openai_response("")
        resp = self.gs.LLMResponse.from_openai(raw)
        # empty text → candidates still built (one candidate with empty text)
        self.assertEqual(resp.candidates[0].content.parts[0].text, "")


class TestOpenAIContentNormalization(unittest.TestCase):
    """normalize_to_openai_messages handles all Gemini content shapes."""

    def setUp(self):
        from services import openai_service
        self.oai = openai_service

    def test_string_input(self):
        msgs = self.oai.normalize_to_openai_messages("Hello")
        self.assertEqual(msgs, [{"role": "user", "content": "Hello"}])

    def test_none_input(self):
        msgs = self.oai.normalize_to_openai_messages(None)
        self.assertEqual(msgs, [{"role": "user", "content": ""}])

    def test_list_of_strings(self):
        msgs = self.oai.normalize_to_openai_messages(["Part 1", "Part 2"])
        self.assertEqual(msgs[0]["content"], "Part 1\nPart 2")

    def test_single_string_in_list(self):
        msgs = self.oai.normalize_to_openai_messages(["Solo"])
        self.assertEqual(msgs[0]["content"], "Solo")

    def test_image_dict_produces_image_url_block(self):
        import base64
        fake_bytes = b"fakeimagebytes"
        mime = "image/png"
        msgs = self.oai.normalize_to_openai_messages(
            ["Describe this image", {"mime_type": mime, "data": fake_bytes}]
        )
        content = msgs[0]["content"]
        self.assertIsInstance(content, list)
        # First block should be text
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "Describe this image")
        # Second block should be image_url with base64 data URL
        self.assertEqual(content[1]["type"], "image_url")
        expected_b64 = base64.b64encode(fake_bytes).decode()
        self.assertIn(expected_b64, content[1]["image_url"]["url"])
        self.assertIn("image/png", content[1]["image_url"]["url"])

    def test_non_list_non_string_stringified(self):
        msgs = self.oai.normalize_to_openai_messages(12345)
        self.assertEqual(msgs[0]["content"], "12345")


class TestOpenAIModelRouting(unittest.TestCase):
    """Model routing: responses API vs chat completions."""

    def setUp(self):
        from services import openai_service
        self.oai = openai_service

    def test_codex_uses_responses_api(self):
        self.assertTrue(self.oai.uses_responses_api("gpt-5.3-codex"))

    def test_gpt55_uses_responses_api(self):
        self.assertTrue(self.oai.uses_responses_api("gpt-5.5"))

    def test_gpt54_mini_uses_chat_completions(self):
        self.assertFalse(self.oai.uses_responses_api("gpt-5.4-mini"))

    def test_gpt41_mini_uses_chat_completions(self):
        self.assertFalse(self.oai.uses_responses_api("gpt-4.1-mini"))

    def test_codex_omits_custom_temperature(self):
        self.assertFalse(self.oai._model_supports_custom_temperature("gpt-5.3-codex"))

    def test_chat_model_supports_temperature(self):
        self.assertTrue(self.oai._model_supports_custom_temperature("gpt-4.1-mini"))

    def test_normalize_responses_api_output(self):
        raw = SimpleNamespace(
            output_text="hello from responses",
            usage=SimpleNamespace(input_tokens=10, output_tokens=20, total_tokens=30),
            model="gpt-5.5",
        )
        norm = self.oai.normalize_to_chat_completion(raw, model="gpt-5.5")
        self.assertEqual(norm.choices[0].message.content, "hello from responses")
        self.assertEqual(norm.usage.prompt_tokens, 10)
        self.assertEqual(norm.usage.completion_tokens, 20)
        self.assertEqual(norm.model, "gpt-5.5")

    def test_vision_downgrades_codex_model(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]}]
        effective = self.oai._effective_model("gpt-5.3-codex", messages)
        self.assertEqual(effective, "gpt-4.1-mini")


class TestOpenAIKeyPool(unittest.TestCase):
    """openai_service loads keys from environment and deduplicates."""

    def setUp(self):
        from services import openai_service
        self.oai = openai_service

    def test_key_pool_non_empty(self):
        pool = self.oai._get_key_pool()
        self.assertGreater(len(pool), 0, "No OpenAI keys found in .env")

    def test_get_openai_api_key_returns_string(self):
        key = self.oai.get_openai_api_key()
        self.assertIsNotNone(key)
        self.assertTrue(key.startswith("sk-"), f"Key doesn't look like an OpenAI key: {key[:8]}…")

    def test_keys_are_deduplicated(self):
        pool = self.oai._get_key_pool()
        self.assertEqual(len(pool), len(set(pool)), "Duplicate keys found in pool")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Integration tests (real OpenAI API calls)
# ═══════════════════════════════════════════════════════════════════════════

@unittest.skipIf(_UNIT_ONLY, "Skipped: --unit-only flag set")
class TestOpenAIServiceIntegration(unittest.TestCase):
    """Real OpenAI API calls — require valid keys in .env."""

    def setUp(self):
        from services import openai_service
        self.oai = openai_service

    def test_sync_generate_text(self):
        """sync_generate returns a ChatCompletion with non-empty text."""
        raw = self.oai.sync_generate(
            "gpt-4.1-mini",
            "Reply with exactly: PONG",
            temperature=0.0,
        )
        content = raw.choices[0].message.content or ""
        self.assertIn("PONG", content.upper(), f"Expected PONG in response, got: {content!r}")
        print(f"\n  [sync] model={raw.model!r}  text={content!r}")

    def test_async_generate_text(self):
        """async_generate returns a ChatCompletion with non-empty text."""
        async def _run():
            return await self.oai.async_generate(
                "gpt-4.1-mini",
                "Reply with exactly: ASYNC_OK",
                temperature=0.0,
            )

        raw = asyncio.run(_run())
        content = raw.choices[0].message.content or ""
        self.assertIn("ASYNC_OK", content.upper(), f"Expected ASYNC_OK in response, got: {content!r}")
        print(f"\n  [async] model={raw.model!r}  text={content!r}")

    def test_sync_generate_json_mode(self):
        """response_mime_type='application/json' returns valid JSON content."""
        raw = self.oai.sync_generate(
            "gpt-4.1-mini",
            'Return JSON: {"status": "ok", "value": 42}',
            temperature=0.0,
            response_mime_type="application/json",
        )
        content = raw.choices[0].message.content or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            self.fail(f"Expected valid JSON, got: {content!r} — {e}")
        self.assertIn("status", data)
        print(f"\n  [json] parsed={data}")

    def test_sync_generate_with_pydantic_schema(self):
        """response_schema with Pydantic model round-trips through LLMResponse.from_openai."""
        from pydantic import BaseModel
        from services import gemini_service

        class SentimentResult(BaseModel):
            sentiment: str
            confidence: float

        raw = self.oai.sync_generate(
            "gpt-4.1-mini",
            'Analyze sentiment of "I love this!". Return JSON with fields: sentiment (positive/negative/neutral), confidence (0.0-1.0).',
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=SentimentResult,
        )
        resp = gemini_service.LLMResponse.from_openai(raw, response_schema=SentimentResult)
        self.assertIsNotNone(resp.parsed, "Expected parsed Pydantic object")
        self.assertIsInstance(resp.parsed, SentimentResult)
        self.assertIn(resp.parsed.sentiment, ("positive", "negative", "neutral"))
        self.assertGreater(resp.parsed.confidence, 0.0)
        print(f"\n  [schema] parsed={resp.parsed}")


@unittest.skipIf(_UNIT_ONLY, "Skipped: --unit-only flag set")
class TestGeminiServiceFallback(unittest.TestCase):
    """
    Tests the full fallback path through gemini_service.async_generate /
    sync_generate by simulating Gemini 503 exhaustion.

    Gemini SDK is mocked to always raise 503; OpenAI is real.
    """

    def _get_gs_with_fallback_enabled(self):
        """Return a gemini_service module with fallback master switch ON."""
        gs = _import_gemini_service()
        gs._OPENAI_FALLBACK_ENABLED = True
        gs._OPENAI_FALLBACK_ALL = False
        return gs

    def _make_gemini_503(self):
        """Return an exception that _is_retryable_gemini_error() recognises as 503."""
        exc = Exception("The model is overloaded. 503 high demand")
        return exc

    def test_async_fallback_fires_on_gemini_503(self):
        """
        When Gemini exhausts all retries with 503, async_generate falls back
        to OpenAI and returns an LLMResponse with provider='openai'.
        """
        gs = self._get_gs_with_fallback_enabled()

        async def _run():
            # Patch _async_gemini_generate to always raise 503
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-3.1-pro-preview",
                    "Reply with exactly: FALLBACK_OK",
                    config=gs.GeminiGenerateConfig(
                        temperature=0.0,
                        fallback_profile="planner",
                    ),
                )

        resp = asyncio.run(_run())
        self.assertIsInstance(resp, gs.LLMResponse, "Expected LLMResponse from fallback")
        self.assertEqual(resp.provider, "openai")
        self.assertEqual(resp.fallback_from, "gemini-3.1-pro-preview")
        self.assertIn("FALLBACK_OK", (resp.text or "").upper())
        print(f"\n  [fallback/async] model={resp.model!r}  text={resp.text!r}")

    def test_sync_fallback_fires_on_gemini_503(self):
        """sync_generate with Gemini mocked as 503 falls back to OpenAI."""
        gs = self._get_gs_with_fallback_enabled()

        # Patch _generate_with_retry to raise the 503
        with mock.patch.object(
            gs,
            "_generate_with_retry",
            side_effect=self._make_gemini_503(),
        ):
            resp = gs.sync_generate(
                "gemini-3.5-flash",
                "Reply with exactly: SYNC_FALLBACK_OK",
                config=gs.GeminiGenerateConfig(temperature=0.0),
            )

        self.assertIsInstance(resp, gs.LLMResponse)
        self.assertEqual(resp.provider, "openai")
        self.assertEqual(resp.fallback_from, "gemini-3.5-flash")
        self.assertIn("SYNC_FALLBACK_OK", (resp.text or "").upper())
        print(f"\n  [fallback/sync] model={resp.model!r}  text={resp.text!r}")

    def test_fallback_disabled_re_raises_gemini_error(self):
        """When fallback is disabled, the original Gemini error is re-raised."""
        gs = self._get_gs_with_fallback_enabled()
        gs._OPENAI_FALLBACK_ENABLED = False  # disable

        async def _run():
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-3.1-pro-preview",
                    "test prompt",
                    config=gs.GeminiGenerateConfig(),
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(_run())
        self.assertIn("503", str(ctx.exception))
        print(f"\n  [fallback/disabled] correctly re-raised: {ctx.exception}")

    def test_fallback_not_eligible_for_25_model(self):
        """2.5 models do NOT fall back (LLM_OPENAI_FALLBACK_ALL=false)."""
        gs = self._get_gs_with_fallback_enabled()

        async def _run():
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-2.5-flash",   # NOT eligible for fallback
                    "test prompt",
                    config=gs.GeminiGenerateConfig(),
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(_run())
        self.assertIn("503", str(ctx.exception))
        print(f"\n  [fallback/2.5-blocked] correctly re-raised: {ctx.exception}")

    def test_fallback_not_eligible_when_tools_set(self):
        """Calls with tools are Gemini-only, never fall back."""
        gs = self._get_gs_with_fallback_enabled()

        async def _run():
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-3.1-pro-preview",
                    "test prompt",
                    config=gs.GeminiGenerateConfig(tools=["google_search"]),
                )

        with self.assertRaises(Exception) as ctx:
            asyncio.run(_run())
        self.assertIn("503", str(ctx.exception))
        print(f"\n  [fallback/tools-blocked] correctly re-raised: {ctx.exception}")

    def test_json_mode_fallback_returns_valid_json(self):
        """Fallback with response_mime_type=application/json returns parseable JSON."""
        gs = self._get_gs_with_fallback_enabled()

        async def _run():
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-3.5-flash",
                    'Return JSON: {"result": "success", "code": 0}',
                    config=gs.GeminiGenerateConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                    ),
                )

        resp = asyncio.run(_run())
        self.assertIsInstance(resp, gs.LLMResponse)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            self.fail(f"Expected valid JSON in resp.text, got: {resp.text!r}")
        print(f"\n  [fallback/json] parsed={data}")

    def test_pydantic_schema_fallback_parses_correctly(self):
        """Fallback with response_schema populates resp.parsed with Pydantic instance."""
        from pydantic import BaseModel

        class AnalysisResult(BaseModel):
            topic: str
            keywords: list[str]

        gs = self._get_gs_with_fallback_enabled()

        async def _run():
            with mock.patch.object(
                gs,
                "_async_gemini_generate",
                side_effect=self._make_gemini_503(),
            ):
                return await gs.async_generate(
                    "gemini-3.1-pro-preview",
                    'Analyze "Python is a great language for AI". Return JSON with: topic (string), keywords (list of strings).',
                    config=gs.GeminiGenerateConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=AnalysisResult,
                        fallback_profile="planner",
                    ),
                )

        resp = asyncio.run(_run())
        self.assertIsInstance(resp, gs.LLMResponse)
        self.assertIsNotNone(resp.parsed, "Expected resp.parsed to be set")
        self.assertIsInstance(resp.parsed, AnalysisResult)
        self.assertIsInstance(resp.parsed.keywords, list)
        print(f"\n  [fallback/schema] parsed={resp.parsed}")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Remove our custom flag so unittest doesn't complain about unknown args
    argv = [a for a in sys.argv if a != "--unit-only"]
    unittest.main(argv=argv, verbosity=2)
