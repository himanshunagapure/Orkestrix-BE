"""Pinned external-API spec for tool/backend codegen.

User-stated or Stage 0 specs are treated as a pinned fixture and injected into
code-gen prompts verbatim. Grounding is a last resort for truly unknown external
HTTP services (no host, no recognised public provider name).

Spec classification — call classify_provider_spec() to decide:
  pinned      — user / Stage 0 gave a concrete host (URL or host:port DSN) +
                an HTTP method or a known path. LLM must not swap the host.
                Grounding must NOT run.
  model_known — a well-known public API provider name is present but no custom
                host was given. LLM writes from training knowledge. No grounding.
  unknown     — HTTP is required but neither a concrete host nor a well-known
                public provider exists. Grounding may run once as a last resort.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s)\]'\"<>`]+", re.IGNORECASE)
_DSN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}:\d{2,5}\b",
    re.IGNORECASE,
)
_METHOD_PATH_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(\S+)",
    re.IGNORECASE,
)
# Trailing markdown / list junk — never strip `}` / `]` / `)` (path templates /
# parenthetical segments). URL regex already stops at `)` for markdown links.
_TRAILING_URL_JUNK_RE = re.compile(r"[`\"'\s.,;/\\]+$")
_LEADING_URL_JUNK_RE = re.compile(r"^[`\"'\s]+")
_HANDOFF_API_RE = re.compile(
    r"External API:\s*(?P<provider>[^\n—\-]+)"
    r"(?:\s*[—\-]\s*(?P<purpose>[^\n]+))?"
    r"(?P<body>(?:\n[ \t]+[^\n]+)*)",
    re.IGNORECASE,
)
_HTTP_HINT_RE = re.compile(
    r"\b(https?://|/api/|rest api|endpoint|unipile|webhook|"
    r"requests\.(get|post|put|patch|delete)|httpx)\b",
    re.IGNORECASE,
)
_LLM_ONLY_RE = re.compile(
    r"\b(gemini|openai|anthropic|claude|generativemodel)\b",
    re.IGNORECASE,
)


class ProviderOperation(BaseModel):
    """One concrete HTTP operation the generated tool/function must implement."""

    provider: str = ""
    purpose: str = ""
    endpoint_url: str = ""
    http_method: str = ""
    auth_header: str = ""
    request_schema: str = ""
    response_schema: str = ""
    docs_url: str = ""
    source: str = "description"  # user | stage0 | description | grounding
    verified: bool = False

    def host(self) -> str:
        return _host_from_endpoint(self.endpoint_url)

    def path(self) -> str:
        return _path_from_endpoint(self.endpoint_url)

    def is_usable(self) -> bool:
        return bool((self.endpoint_url or "").strip() or (self.http_method or "").strip())


def sanitize_endpoint_url(endpoint: str) -> str:
    """Strip markdown fence / punctuation artifacts from harvested URLs (Issue 14).

    Preserves template braces in paths/queries (e.g. ``{next_cursor}``).
    """
    raw = (endpoint or "").strip()
    if not raw:
        return ""
    # Unwrap full-string fences: `url` or ```url```
    if raw.startswith("`"):
        raw = raw.lstrip("`").strip()
    if raw.endswith("`"):
        raw = raw.rstrip("`").strip()
    raw = _LEADING_URL_JUNK_RE.sub("", raw)
    raw = _TRAILING_URL_JUNK_RE.sub("", raw)
    return raw.strip()


def finalize_provider_operation(op: ProviderOperation) -> ProviderOperation:
    """Sanitize URL and enforce verified rules (Issue 14).

    - Description-parsed / grounding ops are never verified.
    - ``verified=True`` requires Stage 0 or user source **and** a non-empty provider.
    - Empty provider always forces ``verified=False``.
    """
    url = sanitize_endpoint_url(op.endpoint_url)
    source = (op.source or "description").strip().lower() or "description"
    provider = (op.provider or "").strip()

    if not provider or source in ("description", "grounding"):
        verified = False
    elif source == "stage0":
        verified = True
    elif source == "user":
        # Respect explicit user flag; still requires provider (checked above).
        verified = bool(op.verified)
    else:
        verified = False

    updates: Dict[str, Any] = {}
    if url != (op.endpoint_url or ""):
        updates["endpoint_url"] = url
    if verified != bool(op.verified):
        updates["verified"] = verified
    if provider != (op.provider or ""):
        updates["provider"] = provider
    if not updates:
        return op
    return op.model_copy(update=updates)


def _host_from_endpoint(endpoint: str) -> str:
    raw = sanitize_endpoint_url(endpoint)
    if not raw:
        return ""
    if "://" not in raw:
        # DSN host:port or bare host/path
        hostport = raw.split("/")[0]
        return hostport.lower()
    try:
        parsed = urlparse(raw)
        return (parsed.netloc or "").lower()
    except Exception:
        return ""


def _path_from_endpoint(endpoint: str) -> str:
    raw = sanitize_endpoint_url(endpoint)
    if not raw:
        return ""
    if "://" in raw:
        try:
            return urlparse(raw).path or ""
        except Exception:
            return ""
    if raw.startswith("/"):
        return raw.split("?")[0]
    if "/" in raw and not re.match(r"^[a-z0-9.-]+:\d+", raw, re.I):
        return "/" + raw.split("/", 1)[-1].split("?")[0]
    return ""


def _norm_method(method: str) -> str:
    return (method or "").strip().upper()


def operations_from_stage0_apis(apis: Sequence[Any]) -> List[ProviderOperation]:
    """Build operations from Stage 0 ClarifiedExternalAPI objects or dicts."""
    out: List[ProviderOperation] = []
    for api in apis or []:
        if hasattr(api, "model_dump"):
            data = api.model_dump()
        elif isinstance(api, dict):
            data = api
        else:
            continue
        op = finalize_provider_operation(
            ProviderOperation(
                provider=str(data.get("provider") or "").strip(),
                purpose=str(data.get("purpose") or "").strip(),
                endpoint_url=str(data.get("endpoint_url") or "").strip(),
                http_method=_norm_method(str(data.get("http_method") or "")),
                auth_header=str(data.get("auth_header") or "").strip(),
                request_schema=str(data.get("request_schema") or "").strip(),
                response_schema=str(data.get("response_schema") or "").strip(),
                source="stage0",
                verified=True,
            )
        )
        if op.is_usable() or op.provider:
            out.append(op)
    return out


def extract_provider_operations_from_text(*texts: str) -> List[ProviderOperation]:
    """Harvest pinned HTTP operations from Stage 0 handoff + node descriptions."""
    blob = "\n".join(t for t in texts if t and str(t).strip())
    if not blob.strip():
        return []

    ops: List[ProviderOperation] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(op: ProviderOperation) -> None:
        op = finalize_provider_operation(op)
        if not op.is_usable() and not op.provider:
            return
        key = (op.host(), op.path(), op.http_method)
        if key in seen and any(key):
            return
        if any(key):
            seen.add(key)
        ops.append(op)

    for match in _HANDOFF_API_RE.finditer(blob):
        body = match.group("body") or ""
        fields: Dict[str, str] = {}
        for line in body.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.strip().partition(":")
            fields[k.strip().lower()] = v.strip()
        op = ProviderOperation(
            provider=(match.group("provider") or "").strip(),
            purpose=(match.group("purpose") or "").strip(),
            endpoint_url=fields.get("endpoint") or fields.get("endpoint_url") or fields.get("url") or "",
            http_method=_norm_method(fields.get("method") or fields.get("http_method") or ""),
            auth_header=fields.get("auth") or fields.get("auth_header") or "",
            request_schema=fields.get("request") or fields.get("request_schema") or "",
            response_schema=fields.get("response") or fields.get("response_schema") or "",
            source="stage0",
            verified=True,
        )
        _add(op)

    for url in _URL_RE.findall(blob):
        _add(
            ProviderOperation(
                endpoint_url=url,
                http_method=_nearby_method(blob, url),
                source="description",
                verified=False,
            )
        )

    for dsn in _DSN_RE.findall(blob):
        if any(dsn.lower() in (op.endpoint_url or "").lower() for op in ops):
            continue
        _add(
            ProviderOperation(
                endpoint_url=dsn,
                http_method=_nearby_method(blob, dsn),
                source="description",
                verified=False,
            )
        )

    for method, path in _METHOD_PATH_RE.findall(blob):
        if path.lower().startswith("http"):
            continue
        cleaned_path = sanitize_endpoint_url(path)
        if not cleaned_path.startswith("/"):
            continue
        _add(
            ProviderOperation(
                endpoint_url=cleaned_path.split("?")[0],
                http_method=_norm_method(method),
                source="description",
                verified=False,
            )
        )

    return [op for op in ops if op.is_usable()]


def _nearby_method(blob: str, needle: str) -> str:
    idx = blob.lower().find(needle.lower())
    window = blob[max(0, idx - 80) : idx + len(needle) + 40] if idx >= 0 else blob[:120]
    found = _METHOD_PATH_RE.search(window) or re.search(
        r"\b(GET|POST|PUT|PATCH|DELETE)\b", window, re.I
    )
    return _norm_method(found.group(1) if found else "")


# ---------------------------------------------------------------------------
# Well-known public APIs the LLM can implement from training knowledge.
# No request/response schema needed from the user OR from grounding.
# Keep this list intentionally narrow — when in doubt, leave out.
# ---------------------------------------------------------------------------
_WELL_KNOWN_PUBLIC_APIS: frozenset[str] = frozenset({
    "stripe",
    "github",
    "openweathermap", "openweather", "weather",
    "twilio",
    "sendgrid",
    "slack",
    "hubspot",
    "salesforce",
    "jira",
    "notion",
    "airtable",
    "google_maps", "googlemaps", "mapbox",
    "shopify",
    "paypal",
    "firebase",
    "supabase",
    "zendesk",
    "intercom",
    "mailchimp",
    "resend",
    "postmark",
    "brevo",
    "clearbit",
    "pipedrive",
    "freshdesk",
    "pagerduty",
    "datadog",
    "segment",
    "mixpanel",
    "amplitude",
    "sentry",
    "plaid",
    "razorpay",
    "braintree",
    "square",
    "xero",
    "quickbooks",
    "docusign",
    "calendly",
    "zoom",
    "youtube",
    "spotify",
    "twitter", "x.com",
    "linkedin",
    "instagram",
    "facebook",
    "whatsapp",
    "telegram",
    "discord",
    "pexels",
    "unsplash",
    "abstractapi",
    "ipinfo",
    "ipgeolocation",
    "exchangerate",
    "currencyapi",
    "openai",
    "anthropic",
    "cohere",
    "pinecone",
    "weaviate",
})

ProviderSpecClass = Literal["pinned", "model_known", "unknown"]


def classify_provider_spec(
    ops: "List[ProviderOperation]",
    description: str = "",
    database_logic: str = "",
) -> ProviderSpecClass:
    """
    Classify what we know about the external HTTP spec to decide grounding.

    pinned      — concrete host (URL or host:port DSN) found in ops OR text,
                  plus a method or distinctive path. Never ground; never swap host.
    model_known — a well-known public provider is named but no custom host.
                  LLM writes from training knowledge. No grounding.
    unknown     — HTTP is required but neither a host nor a recognised public
                  provider. Ground once as a last resort.

    When HTTP is not needed at all, returns "model_known" (no grounding required).
    """
    combined = f"{description} {database_logic}".lower()

    # ── 1. pinned: structured ops already carry a real host + method ──────────
    for op in ops:
        if op.host() and op.http_method:
            return "pinned"
    # Also pinned if text contains a URL or host:port DSN — the host is real
    # even if we could not parse a method yet.
    if _URL_RE.search(combined) or _DSN_RE.search(combined):
        return "pinned"

    # ── 2. model_known: well-known public provider name present ───────────────
    for token in _WELL_KNOWN_PUBLIC_APIS:
        # whole-word match to avoid false hits (e.g. "stripe" in "constraint")
        if re.search(r"\b" + re.escape(token) + r"\b", combined):
            return "model_known"
    for op in ops:
        if (op.provider or "").lower() in _WELL_KNOWN_PUBLIC_APIS:
            return "model_known"

    # ── 3. check if HTTP is even needed — if not, no grounding required ───────
    if not description_implies_external_http_api(f"{description} {database_logic}"):
        return "model_known"

    return "unknown"


def description_implies_external_http_api(text: str) -> bool:
    """True when a tool likely needs a REST/HTTP provider spec (not LLM-SDK-only)."""
    blob = text or ""
    if not blob.strip():
        return False
    if _HTTP_HINT_RE.search(blob):
        return True
    if _URL_RE.search(blob) or _DSN_RE.search(blob):
        return True
    # Hosted LLM SDKs are covered by existing model-id research, not REST fixture.
    if _LLM_ONLY_RE.search(blob) and not re.search(
        r"\b(http|endpoint|api\.|/v1/)\b", blob, re.I
    ):
        return False
    return False


def format_pinned_provider_spec(operations: Sequence[ProviderOperation]) -> str:
    usable = [op for op in operations if op.is_usable()]
    if not usable:
        return ""
    lines = [
        "PINNED PROVIDER OPERATION (MUST implement exactly — do NOT substitute a different "
        "host, path, or HTTP method; do NOT invent a generic default like api.unipile.com "
        "when a pinned host is present):"
    ]
    for i, op in enumerate(usable, 1):
        lines.append(f"  Operation {i}:")
        if op.provider:
            lines.append(f"    provider: {op.provider}")
        if op.purpose:
            lines.append(f"    purpose: {op.purpose}")
        if op.endpoint_url:
            lines.append(f"    endpoint: {op.endpoint_url}")
        if op.http_method:
            lines.append(f"    method: {op.http_method}")
        if op.auth_header:
            lines.append(f"    auth: {op.auth_header}")
        if op.request_schema:
            lines.append(f"    request: {op.request_schema}")
        if op.response_schema:
            lines.append(f"    response: {op.response_schema}")
        lines.append(f"    source: {op.source} verified={op.verified}")
    return "\n".join(lines)


def _method_in_code(method: str, code_lower: str) -> bool:
    m = method.lower()
    tokens = (
        f"requests.{m}",
        f"httpx.{m}",
        f".{m}(",
        f'method="{method}"',
        f"method='{method}'",
        f'method="{m}"',
        f"'{method}'",
        f'"{method}"',
    )
    return any(tok.lower() in code_lower for tok in tokens)


def function_code_matches_provider_fixture(
    function_code: str,
    operations: Sequence[ProviderOperation],
) -> Optional[str]:
    """Return a rejection reason when generated code ignores the pinned HTTP fixture."""
    usable = [op for op in operations if op.is_usable()]
    if not usable or not (function_code or "").strip():
        return None
    code = function_code.lower()

    hosts = [op.host() for op in usable if op.host()]
    # Host without port also counts (api20.unipile.com:15002 → api20.unipile.com).
    host_tokens: List[str] = []
    for host in hosts:
        host_tokens.append(host)
        if ":" in host:
            host_tokens.append(host.split(":")[0])
    host_tokens = [h.lower() for h in host_tokens if h]
    if host_tokens and not any(h in code for h in host_tokens):
        return (
            "function_code does not use pinned provider host(s) "
            f"{sorted(set(hosts))}. Implement the PINNED endpoint; do not substitute "
            "a different default host."
        )

    methods = {_norm_method(op.http_method) for op in usable if op.http_method}
    methods.discard("")
    if methods and not any(_method_in_code(m, code) for m in methods):
        return (
            "function_code does not call pinned HTTP method(s) "
            f"{sorted(methods)}. Use the PINNED method."
        )

    # Distinctive literal paths: require at least one pinned path when any exist.
    distinctive_paths = []
    for op in usable:
        path = op.path().rstrip("/")
        if not path or path == "/" or "{" in path or len(path) < 6:
            continue
        distinctive_paths.append(path)
    if distinctive_paths:
        matched_path = False
        for path in distinctive_paths:
            token = path.lower().lstrip("/")
            segment = token.split("/")[-1]
            if token in code or path.lower() in code or (
                len(segment) >= 5 and segment in code
            ):
                matched_path = True
                break
        if not matched_path:
            return (
                "function_code does not include any pinned path "
                f"{distinctive_paths}. Implement a PINNED operation path."
            )
    return None


def operations_to_dicts(operations: Sequence[ProviderOperation]) -> List[Dict[str, Any]]:
    return [op.model_dump() for op in operations]


def operations_from_dicts(raw: Any) -> List[ProviderOperation]:
    if not raw:
        return []
    if isinstance(raw, list):
        out: List[ProviderOperation] = []
        for item in raw:
            try:
                if isinstance(item, ProviderOperation):
                    out.append(finalize_provider_operation(item))
                elif isinstance(item, dict):
                    data = dict(item)
                    # Rows that omit source but carry provider+verified were Stage 0 /
                    # user pins — don't demote them to description/unverified.
                    if not str(data.get("source") or "").strip():
                        if str(data.get("provider") or "").strip() and data.get("verified"):
                            data["source"] = "stage0"
                    out.append(finalize_provider_operation(ProviderOperation(**data)))
            except Exception:
                continue
        return out
    return []


def node_provider_operations(node: Any) -> List[ProviderOperation]:
    cfg = getattr(node, "config", None)
    if cfg is None:
        return []
    if isinstance(cfg, dict):
        return operations_from_dicts(cfg.get("providerOperations"))
    return operations_from_dicts(getattr(cfg, "providerOperations", None))


def merge_provider_operations(*groups: Sequence[ProviderOperation]) -> List[ProviderOperation]:
    merged: List[ProviderOperation] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for op in group or []:
            key = (op.host(), op.path(), op.http_method)
            if key in seen and any(key):
                continue
            if any(key):
                seen.add(key)
            merged.append(op)
    return merged


async def resolve_rest_api_spec_with_grounding(
    description: str,
    *,
    available_credentials: Optional[List[Dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[ProviderOperation]:
    """Google Search grounding → one unverified ProviderOperation, or None."""
    from services.gemini_service import (
        async_generate,
        GeminiGenerateConfig,
        get_gemini_api_key,
        google_search_tool,
    )
    from services.llm_token_tracker import record_llm_call
    from services.llm_utils import extract_token_usage_from_response
    from services.workflow_budget_service import BillingError

    if not get_gemini_api_key():
        return None

    cred_list = [
        {"name": c.get("name"), "type": c.get("type"), "service": c.get("service")}
        for c in (available_credentials or [])
        if c.get("name")
    ]
    research_prompt = f"""You are a technical researcher. Use Google Search to find the CURRENT, OFFICIAL REST API documentation for the external HTTP service in this tool task.

Tool task description:
{description.strip()}

Configured project credentials (names only):
{json.dumps(cred_list, indent=2) if cred_list else "[]"}

Return ONE JSON object only (no markdown):
{{
  "provider": "slug",
  "purpose": "short verb phrase",
  "endpoint_url": "https://host/path",
  "http_method": "GET|POST|PUT|PATCH|DELETE",
  "auth_header": "e.g. X-API-KEY or Authorization: Bearer",
  "request_schema": "key fields",
  "response_schema": "key fields",
  "docs_url": "canonical documentation URL"
}}
If this is a private/internal API with no public docs, return {{"provider": "PRIVATE_API"}}.
Prefer the vendor's official docs over blogs. Use the latest stable API version."""

    started_at = datetime.utcnow()
    try:
        response = await async_generate(
            "gemini-3-flash-preview",
            research_prompt,
            config=GeminiGenerateConfig(
                temperature=0.1,
                tools=google_search_tool(),
            ),
        )
        text = (response.text or "").strip()
        usage = extract_token_usage_from_response(response)
        record_llm_call(
            project_id=project_id or "unknown",
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="graph-planning",
            sub_operation="gap-tool-rest-spec-grounding",
            model="gemini-3-flash-preview",
            prompt_tokens=usage.get("prompt_tokens") or len(research_prompt) // 4,
            completion_tokens=usage.get("completion_tokens") or len(text) // 4,
            total_tokens=(usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0),
            status="success",
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )
        if not text or "PRIVATE_API" in text:
            return None
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except Exception:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                return None
            data = json.loads(cleaned[start : end + 1])
        if not isinstance(data, dict):
            return None
        op = finalize_provider_operation(
            ProviderOperation(
                provider=str(data.get("provider") or "").strip(),
                purpose=str(data.get("purpose") or "").strip(),
                endpoint_url=str(data.get("endpoint_url") or "").strip(),
                http_method=_norm_method(str(data.get("http_method") or "")),
                auth_header=str(data.get("auth_header") or "").strip(),
                request_schema=str(data.get("request_schema") or "").strip(),
                response_schema=str(data.get("response_schema") or "").strip(),
                docs_url=str(data.get("docs_url") or "").strip(),
                source="grounding",
                verified=False,
            )
        )
        if not op.is_usable():
            return None
        logger.info(
            "[provider-op/grounding] resolved %s %s (%s)",
            op.http_method,
            op.endpoint_url,
            op.provider,
        )
        return op
    except BillingError:
        raise
    except Exception as exc:
        logger.warning("[provider-op/grounding] REST spec lookup failed (non-fatal): %s", exc)
        return None
