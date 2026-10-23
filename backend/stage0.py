"""
Stage 0 — Scalable Agentic Graph Planner
=========================================
Implements all three sub-phases before Stage A (Skeleton Planner):

  0-A  Credential Check        — Pure code, DB read, zero LLM calls
  0-B  Requirement Clarification Loop — MCQ-style interactive LLM loop
  0-C  Configuration Guidance  — One-shot LLM call per unknown gap

Design principles
-----------------
* 0-A, 0-B, and 0-C are fully separated — no sub-phase bleeds into another.
* 0-B asks questions only (MCQ clarification).
* 0-C / guidance is on-demand via run_stage0_guidance_turn (templates + LLM), not part of the QnA loop.
* Stage A receives RequirementSummary.planner_handoff (structured clarified requirements).
* Built-in templates in 0-C cost 0 LLM calls for all common integrations.
"""

from __future__ import annotations

import json
import logging
import hashlib
import re
from typing import Any, Literal, Optional
from datetime import datetime
from urllib.parse import urlsplit, parse_qsl
from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response

from json_repair import repair_json
from services.gemini_service import (
    async_generate,
    GeminiGenerateConfig,
    get_or_create_cache,
)
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3-flash-preview"

# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------

class CredentialCheckResult(BaseModel):
    """Output of Sub-Phase 0-A."""
    workflow_id: str
    configured_secret_names: list[str]  # key names only — never values
    has_any_secrets: bool


# Semantic aliases so gemini_key / GEMINI_API_KEY / GOOGLE_API_KEY all match.
_SECRET_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"GEMINI_API_KEY", "GEMINI_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY", "GOOGLE_GENAI_API_KEY"}),
    frozenset({"OPENAI_API_KEY", "OPENAI_KEY"}),
    frozenset({"ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY"}),
    frozenset({"EMAIL_SENDER", "SMTP_SENDER", "EMAIL_FROM"}),
    frozenset({"EMAIL_PASSWORD", "SMTP_PASSWORD", "EMAIL_APP_PASSWORD"}),
)


def _normalize_secret_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def secret_aliases_for(name: str) -> set[str]:
    """Return normalized aliases for a secret name (includes itself)."""
    key = _normalize_secret_key(name)
    if not key:
        return set()
    aliases = {key}
    for group in _SECRET_ALIAS_GROUPS:
        group_norm = {_normalize_secret_key(g) for g in group}
        if key in group_norm or any(key.endswith(g) or g.endswith(key) for g in group_norm if len(g) >= 6):
            aliases |= group_norm
    # Soft match: strip common suffixes/prefixes
    for suffix in ("APIKEY", "KEY", "TOKEN", "SECRET"):
        if key.endswith(suffix) and len(key) > len(suffix) + 2:
            aliases.add(key[: -len(suffix)])
    return aliases


def secret_is_configured(required_name: str, configured_names: list[str]) -> bool:
    """True if required_name (or a semantic alias) appears in configured_names."""
    needed = secret_aliases_for(required_name)
    if not needed:
        return False
    configured = set()
    for n in configured_names or []:
        configured |= secret_aliases_for(n)
    return bool(needed & configured)


def find_configured_secret_name(
    required_name: str, configured_names: list[str]
) -> Optional[str]:
    """Return the actual configured name that satisfies required_name, if any."""
    needed = secret_aliases_for(required_name)
    for n in configured_names or []:
        if secret_aliases_for(n) & needed:
            return n
    return None


class MCQQuestion(BaseModel):
    """A single MCQ presented to the user during Sub-Phase 0-B."""
    question_text: str
    options: list[str]          # last option always = 'Other / type your own'
    flag_type: str              # which RequirementFlag this question resolves
    severity: str               # CRITICAL | WARN


def _any_to_str(v: Any) -> Optional[str]:
    """Coerce any LLM value to a plain string, or None if null/empty."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        # Flatten dict to "key: value, ..." representation
        return ", ".join(f"{k}: {val}" for k, val in v.items())
    return str(v)


class RequirementFlag(BaseModel):
    """A detected requirement gap, with severity and resolution state."""
    flag_type: str
    description: str
    severity: str               # CRITICAL | WARN
    resolved: bool = False
    resolved_value: Optional[str] = None

    @field_validator("resolved_value", mode="before")
    @classmethod
    def _coerce_resolved_value(cls, v: Any) -> Optional[str]:
        return _any_to_str(v)


class DBSchema(BaseModel):
    collection_name: str
    fields: list[dict[str, str]]  # [{"name": "city", "type": "string"}, ...]
    # Set by entity binding (services.entity_resolver) when this schema refers to data that
    # ALREADY EXISTS for the subscriber (possibly written by another agent/project).
    existing: bool = False
    user_decision: Optional[str] = None
    target_store: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_schema(cls, v: Any) -> Any:
        """
        Gemini returns inconsistent shapes for DB schema. Handle all known variants:
          - {collection_name, fields: [{name, type}, ...]}   ← canonical
          - {name, fields: [...]}                            ← wrong key
          - {name, city: "string", temperature: "number"}    ← flat (fields on root)
          - {collection_name, fields: {city: "string", ...}} ← fields as dict
        """
        if not isinstance(v, dict):
            return v
        v = dict(v)

        # Normalise collection_name key
        if "collection_name" not in v and "name" in v:
            v["collection_name"] = v.pop("name")

        # Known non-field keys to exclude when sniffing flat field definitions
        _meta_keys = {
            "collection_name", "name", "fields", "schema", "description", "indexes",
            "existing", "user_decision", "target_store", "labels", "relationships",
        }

        fields = v.get("fields")
        if isinstance(fields, list) and fields:
            # Already a proper list — ensure each item is a dict with name/type
            normalised = []
            for f in fields:
                if isinstance(f, dict) and "name" in f:
                    normalised.append({"name": f["name"], "type": str(f.get("type", "string"))})
                elif isinstance(f, str):
                    normalised.append({"name": f, "type": "string"})
            v["fields"] = normalised
        elif isinstance(fields, dict):
            # fields: {"city": "string", ...}
            v["fields"] = [{"name": k, "type": str(t)} for k, t in fields.items()]
        else:
            # Fields are flat on the root dict — pull everything that isn't a meta key
            flat = {k: val for k, val in v.items() if k not in _meta_keys}
            if flat:
                v["fields"] = [{"name": k, "type": str(t)} for k, t in flat.items()]
            else:
                v.setdefault("fields", [])
        return v


def _coerce_str_list(value: Any) -> list[str]:
    """
    Gemini sometimes returns a list of dicts for llm_providers / external_apis.
    Flatten to strings using the most descriptive field available.
    """
    if not isinstance(value, list):
        return value
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            # Pick the most descriptive field
            for key in ("model", "name", "provider", "api", "value", "type"):
                if item.get(key):
                    result.append(str(item[key]))
                    break
            else:
                result.append(str(next(iter(item.values()), "")))
        else:
            result.append(str(item))
    return result


class ClarifiedLLM(BaseModel):
    provider: str = ""
    model: str = ""
    secret_name: str = ""


class ClarifiedExternalAPI(BaseModel):
    provider: str = ""
    purpose: str = ""
    # API integration details — filled by Stage 0 when user supplies them or when
    # Stage 0 asks for them (private/internal APIs only).
    # Left empty for well-known public APIs → grounding fills them at code-gen time.
    endpoint_url: str = ""     # e.g. "https://api.internal.com/v1/leads"
    http_method: str = ""      # GET | POST | PUT | PATCH | DELETE
    request_schema: str = ""   # short description of request params/body fields
    response_schema: str = ""  # short description of response fields / structure
    is_public: bool = True     # False = private/internal API; True = publicly documented


class ClarifiedEmail(BaseModel):
    secret_names: list[str] = Field(default_factory=list)
    smtp: str = "gmail"

    @field_validator("secret_names", mode="before")
    @classmethod
    def _coerce_secret_names(cls, v: Any) -> Any:
        return _coerce_str_list(v)


class ClarifiedRequirements(BaseModel):
    """
    Structured completion payload from Sub-Phase 0-B.

    ``requirements_analyzed`` are short implementation steps for the planner.
    Missing fields are filled by ``ensure_clarified_requirements`` from flags.
    """
    llm: Optional[ClarifiedLLM] = None
    external_apis: list[ClarifiedExternalAPI] = Field(default_factory=list)
    email: Optional[ClarifiedEmail] = None
    db_schemas: list[DBSchema] = Field(default_factory=list)
    requirements_analyzed: list[str] = Field(default_factory=list)

    @field_validator("requirements_analyzed", mode="before")
    @classmethod
    def _coerce_requirements_analyzed(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        return [_any_to_str(item) or "" for item in v]

    @field_validator("external_apis", mode="before")
    @classmethod
    def _coerce_external_apis(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"provider": item, "purpose": ""})
            elif isinstance(item, dict):
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump())
            else:
                out.append({"provider": str(item), "purpose": ""})
        return out

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_completion_shape(cls, v: Any) -> Any:
        """Accept older 0-B JSON (logical_steps, llm_providers, email_config, …)."""
        if not isinstance(v, dict):
            return v
        data = dict(v)

        if "requirements_analyzed" not in data and data.get("logical_steps"):
            data["requirements_analyzed"] = data.pop("logical_steps")

        if not data.get("llm") and data.get("llm_providers"):
            providers = _coerce_str_list(data.pop("llm_providers"))
            if providers:
                model = providers[0]
                provider = "google_gemini"
                secret = "GEMINI_API_KEY"
                low = model.lower()
                if "gpt" in low or "openai" in low:
                    provider, secret = "openai", "OPENAI_API_KEY"
                elif "claude" in low or "anthropic" in low:
                    provider, secret = "anthropic", "ANTHROPIC_API_KEY"
                data["llm"] = {
                    "provider": provider,
                    "model": model,
                    "secret_name": secret,
                }

        apis = data.get("external_apis")
        if isinstance(apis, list) and apis and isinstance(apis[0], str):
            data["external_apis"] = [
                {"provider": str(p), "purpose": ""} for p in _coerce_str_list(apis)
            ]

        if not data.get("email"):
            email_cfg = data.get("email_config")
            if isinstance(email_cfg, dict) and email_cfg:
                names = email_cfg.get("secret_names") or email_cfg.get("secrets")
                if isinstance(names, list):
                    secret_names = [_any_to_str(n) or "" for n in names if _any_to_str(n)]
                else:
                    secret_names = ["EMAIL_SENDER", "EMAIL_PASSWORD"]
                data["email"] = {
                    "secret_names": secret_names or ["EMAIL_SENDER", "EMAIL_PASSWORD"],
                    "smtp": str(email_cfg.get("smtp") or email_cfg.get("provider") or "gmail"),
                }
            else:
                gaps = data.get("gaps_needing_setup") or []
                if any(
                    "email" in str(g).lower() or "smtp" in str(g).lower() for g in gaps
                ):
                    data["email"] = {
                        "secret_names": ["EMAIL_SENDER", "EMAIL_PASSWORD"],
                        "smtp": "gmail",
                    }

        for drop in ("resolved_conditions", "gaps_needing_setup", "already_configured",
                     "llm_providers", "email_config", "sms_config", "logical_steps"):
            data.pop(drop, None)

        return data


class Stage0B_Response(BaseModel):
    """LLM response schema for each turn of Sub-Phase 0-B."""
    message: str
    questions: list[MCQQuestion]
    detected_flags: list[RequirementFlag] = Field(default_factory=list)
    resolved_flags: list[RequirementFlag] = Field(default_factory=list)
    is_complete: bool
    clarified_requirements: Optional[ClarifiedRequirements] = None


class Turn(BaseModel):
    role: str   # 'assistant' | 'user'
    content: str


class ConfigGuidance(BaseModel):
    """Step-by-step setup instructions for a single credential gap."""
    dependency_type: str
    guide_title: str
    secret_keys: list[str]
    steps: list[str]
    provider_url: Optional[str] = None


class Stage0GuidanceResult(BaseModel):
    """On-demand setup help for a single requirement flag (Sub-Phase 0-C)."""
    markdown: str
    doc_links: list[str] = Field(default_factory=list)
    credential_names: list[str] = Field(default_factory=list)
    source: Literal["template", "llm"] = "template"
    dependency_type: str = ""


class LogicalStep(BaseModel):
    step_number: int
    description: str


class RequirementSummary(BaseModel):
    """
    Handoff object passed to Stage A — never contains the raw prompt.

    ``planner_handoff`` is the authoritative scratch-planner context (built from
  structured clarified requirements). ``logical_steps`` mirrors ``requirements_analyzed``.
    """
    phase_name: str
    phase_goal: str
    logical_steps: list[LogicalStep]
    llm_providers: list[str]
    integrations: list[str]
    db_schemas: list[DBSchema]
    required_secrets: list[str]
    already_configured: list[str]
    config_guidance: list[ConfigGuidance]
    unresolved_warns: list[str]
    planner_handoff: str = ""


class Stage0State(BaseModel):
    """Master state object for the entire Stage 0 pipeline."""
    workflow_id: str
    raw_prompt: str

    # 0-A output
    credential_check: Optional[CredentialCheckResult] = None

    # 0-B state
    conversation_turns: list[Turn] = Field(default_factory=list)
    pending_questions: list[MCQQuestion] = Field(default_factory=list)
    detected_flags: list[RequirementFlag] = Field(default_factory=list)
    resolved_flags: list[RequirementFlag] = Field(default_factory=list)
    clarified_requirements: Optional[ClarifiedRequirements] = None
    stage0b_complete: bool = False

    # 0-C output
    config_guidance: list[ConfigGuidance] = Field(default_factory=list)
    stage0c_complete: bool = False

    # Final handoff
    requirement_summary: Optional[RequirementSummary] = None


# ---------------------------------------------------------------------------
# ── SUB-PHASE 0-A  Credential Check ─────────────────────────────────────────
# ---------------------------------------------------------------------------

async def fetch_configured_secrets(
    workflow_id: str,
    db: Any = None,
    *,
    credential_service: Any = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
) -> CredentialCheckResult:
    """
    Pure code — no LLM call.

    Reads configured secret *names* for the given workflow/project id.
    Values are never exposed.

    Prefer ``credential_service`` (project-scoped CredentialService docs).
    Falls back to ``db`` when only a legacy Mongo client is available.
    """
    secret_names: list[str] = []

    if credential_service is not None:
        items = await credential_service.list_credentials(
            project_id=workflow_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
        )
        if not items and (org_id or subscriber_id):
            items = await credential_service.list_credentials(project_id=workflow_id)
        secret_names = sorted({item["name"] for item in items if item.get("name")})
    elif db is not None:
        coll = db.credentials if hasattr(db, "credentials") else db.get_collection("credentials")

        # Legacy nested document: { workflow_id, secrets: { KEY: value, ... } }
        legacy_doc = await coll.find_one({"workflow_id": workflow_id})
        if legacy_doc and isinstance(legacy_doc.get("secrets"), dict):
            secret_names = list(legacy_doc["secrets"].keys())
        else:
            # Current per-credential documents keyed by project_id
            cursor = coll.find(
                {"project_id": workflow_id},
                projection={"name": 1},
            )
            async for doc in cursor:
                name = doc.get("name")
                if name:
                    secret_names.append(name)
            secret_names = sorted(set(secret_names))

    return CredentialCheckResult(
        workflow_id=workflow_id,
        configured_secret_names=secret_names,
        has_any_secrets=len(secret_names) > 0,
    )


def credentials_added_since(
    previous_names: list[str],
    current_names: list[str],
) -> list[str]:
    """
    Return credential names present in ``current_names`` but not in ``previous_names``.

    Comparison is case-insensitive on the name only — no mapping to requirement types.
    """
    previous_lower = {n.lower() for n in previous_names if n}
    return sorted(
        {n for n in current_names if n and n.lower() not in previous_lower},
        key=str.lower,
    )


# ---------------------------------------------------------------------------
# ── SUB-PHASE 0-B  Requirement Clarification Loop ───────────────────────────
# ---------------------------------------------------------------------------

# Flag types that must NEVER surface as MCQs — workflow builder can assume defaults.
# NOTE: INPUT_SOURCE / file-acquisition path is NOT auto-resolved — when the user can
# choose PDF upload vs email vs shared folder, Stage 0 must ask and only require
# credentials for the selected path.
_AUTO_RESOLVE_FLAG_TYPES: frozenset[str] = frozenset({
    "EMAIL_CONTENT",
    "EMAIL_RECIPIENT",
    "EMAIL_SUBJECT",
    "EMAIL_BODY",
    "OUTPUT_FORMAT",
    "UI_DISPLAY",
    "DB_TYPE",
    "DB_ENGINE",
    "ARCADEDB_CONNECTION",
    "ARCADEDB_CREDENTIALS",
    "GRAPH_DB_CONNECTION",
})

_AUTO_RESOLVE_DEFAULTS: dict[str, str] = {
    "EMAIL_CONTENT": "Default subject/body generated from workflow outputs",
    "EMAIL_RECIPIENT": "User who initiated the workflow",
    "EMAIL_SUBJECT": "Workflow notification (generated at build time)",
    "EMAIL_BODY": "Workflow result summary (generated at build time)",
    "OUTPUT_FORMAT": "Standard workflow output",
    "UI_DISPLAY": "Standard form UI screen",
    "DB_TYPE": "MongoDB (platform default)",
    "DB_ENGINE": "MongoDB (platform default)",
    "ARCADEDB_CONNECTION": "Platform-managed (auto-selected alongside MongoDB)",
    "ARCADEDB_CREDENTIALS": "Platform-managed (auto-selected alongside MongoDB)",
    "GRAPH_DB_CONNECTION": "Platform-managed (auto-selected alongside MongoDB)",
}

_STAGE0B_SYSTEM_PROMPT = """
You are a Requirement Analyst for an AI workflow builder.
Your ONLY job is to ask questions about TRUE BLOCKERS — do NOT generate setup instructions.

CORE PRINCIPLE
--------------
Only ask the user when the workflow CANNOT be built or run without the answer.
If a safe industry-standard default exists and will not cause runtime failure, assume it,
mark the flag resolved in resolved_flags, and do NOT ask a question.

Rules:
1. Analyse the workflow for: LLM usage, external APIs, email/SMS, DB collection writes,
   and missing credentials. Ignore presentation/UI wording choices.
2. ``configured_secrets`` is the live list of secret *names* loaded from the project's
   Configure Credentials store (refreshed from the database before every turn after the
   user answers). Treat any name in that list as satisfied — do not ask for that credential
   again. Use semantic matching (e.g. gemini_key / GEMINI_API_KEY / GOOGLE_API_KEY all
   satisfy a Gemini/LLM API key gap).
3. When ``newly_configured_secrets`` is non-empty, re-evaluate all open flags: mark any
   blocker now covered by those names as resolved in resolved_flags and remove related
   questions.
4. Ask at most 3 questions per turn — only for unresolved CRITICAL BLOCKERS.
5. Format every blocker question as MCQ: 2-4 options + final option
   'Other / I\\'ll type my own answer'.
6. Never include URLs, setup steps, or 'go to Configure Secrets' in questions.
7. Never repeat a question already answered in the conversation history.
   conversation_history contains both your prior questions and the user's answers.
   If the user's answer is a delegation ('you decide', 'your choice', 'feel free',
   'any schema is fine', 'continue', etc.), treat that flag as RESOLVED with a
   workflow-builder-chosen default. Do NOT re-ask it. Add it to resolved_flags with
   resolved_value = "delegated to workflow builder".
8. When all CRITICAL BLOCKER gaps are resolved, set is_complete=true even if minor
   assumable items remain. Auto-resolve WARN items with defaults in resolved_flags.
9. Output JSON only. No markdown. No preamble.
10. Never include setup instructions, Configure Secrets steps, or provider URLs in this
    response — setup guidance is a separate on-demand API. Questions and flag state only.

CREDENTIALS DURING QnA (CRITICAL):
- If the workflow needs an LLM and the user confirms Gemini/OpenAI/Anthropic, check
  configured_secrets with semantic matching. If the matching key is present → resolve,
  do not ask again.
- If the key is NOT configured, ask ONE MCQ with options like:
    1) "I already configured it / refresh and continue"
    2) "I will configure it later — continue building"
    3) Other / type your own
  If the user chooses "configure later" (or equivalent), mark the credential flag
  RESOLVED with resolved_value="configure_later" and set is_complete when other
  blockers are done. Do NOT block the pipeline. Code generation will emit real code
  that returns a runtime error if the credential is still missing later.
- NEVER tell the code generator to create stub/capability_gap tools because a credential
  is missing — real code + runtime Missing credential error is the correct design.

OPTIONAL INPUT PATHS:
- When the user chooses ONE input method (e.g. PDF upload) among alternatives
  (email fetch, shared folder), treat unselected paths as NOT required.
  Do not ask for IMAP/Drive credentials when the user selected PDF upload only.
  Record the chosen path in resolved_flags / requirements_analyzed.

ASSUMPTION RULES — resolve automatically, severity=WARN, do NOT ask:
- OCR / text extraction: assume Tesseract.
- LLM model version: if provider API key is in configured_secrets, assume gemini-3-flash-preview
  (Gemini), gpt-4o (OpenAI), or claude-sonnet-4 (Anthropic) as appropriate.
- Email SMTP host/port: if EMAIL_SENDER and EMAIL_PASSWORD are configured, assume Gmail SMTP
  (smtp.gmail.com, port 587). Do not ask.
- UI form fields for user-entered data (names, amounts, etc.): assume standard UI form.
- When multiple file-acquisition methods exist (PDF upload vs email vs shared folder): ASK which
  method the user wants — do not assume; only require credentials for the selected path.
- Email recipient, subject, or body wording: workflow builder generates sensible defaults.
- Output format / display / notification wording: workflow builder decides.
- DB engine/type when collection schema is already defined: assume MongoDB (platform default),
  UNLESS "entity_resolution" (see EXISTING DATA below) names an existing entity, in which case
  that entity's store applies automatically. Never invent a separate DB_TYPE question.
- ArcadeDB (graph database) is ALSO a platform-provided database, exactly like MongoDB — it is
  already configured by the platform (connection host/port/db/user/password), NOT something the
  user supplies. Whether a given collection uses MongoDB or ArcadeDB is decided automatically later
  by the platform's own schema classifier based on whether it needs N-hop graph traversal.
  NEVER ask "how should the workflow connect to ArcadeDB", and NEVER ask for ARCADEDB_URL,
  ARCADEDB_DB, ARCADEDB_USER, or ARCADEDB_PASSWORD — these do not exist as user-facing secrets.
  If the requirement mentions graphs, nodes/edges, relationships, or hierarchies, resolve the
  storage engine automatically (WARN, not CRITICAL) with resolved_value="platform-managed
  (MongoDB/ArcadeDB, auto-selected)" and do not ask.
- EXISTING DATA: the input JSON may contain "entity_resolution": {"status", "must_ask", "reason",
  "suggested", "candidates": [{"name", "fields", "labels", "relationships", "description",
  "created_at", ...}]}. Candidates are collections that ALREADY EXIST for this account (possibly
  created by another agent or project); "suggested" is only an automated hint.
  * status "bound": the requirement reads exactly these candidates. Do NOT ask. In
    clarified_requirements.db_schemas set collection_name EXACTLY equal to the candidate "name"
    (verbatim, never renamed) and copy its fields.
  * status "ask": the PLATFORM itself asks the user which existing collection to use, or to create a
    new one. Do NOT write a question about this yourself, and never mention any storage engine.
    Once conversation_history contains the answer: if the user chose an existing candidate, use its
    "name" verbatim as collection_name; if the user chose "Create a new one", give the collection a
    NEW descriptive name that differs from every candidate name (the system makes it unique); if the
    user typed their own answer, follow it.
  * status "probe", "none" or absent: no similar existing data; treat the collection as new.
  * Reads and writes of the same entity must always use the same collection_name.
- System variables / API Headers: Do NOT ask the user for any variables listed in the "system_variables" array in the Input JSON. These are injected automatically by the platform at runtime. Assume they are available.

CRITICAL BLOCKERS ONLY — severity=CRITICAL, ask only when ALL apply:
- The workflow will fail at runtime without this answer, AND
- No safe default exists, AND
- The value is not already in configured_secrets.

Examples of TRUE blockers (ask):
- LLM is required but GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY is missing and
  provider not inferable.
- External API needed (weather, payments, etc.) but provider not specified (OpenWeatherMap vs other).
- External API is specified but appears to be PRIVATE / INTERNAL (not publicly documented,
  e.g. company-internal REST service, self-hosted endpoint, undocumented partner API, or a
  DSN / custom host that is clearly not a well-known public SaaS) AND no endpoint URL,
  HTTP method, or request/response schema was provided.
  → Ask: "Is [API name] a publicly documented API or a private/internal endpoint?"
  → If private/custom: ask for endpoint URL, HTTP method, and brief request/response description
    (one follow-up question covering all three — keep it concise).
  → If public and well-known (Stripe, OpenWeatherMap, GitHub, Twilio, SendGrid, OpenAI, etc.)
    and the user did NOT give a custom host or DSN: resolve automatically — the LLM knows
    these APIs and will implement the call from its training knowledge. Do NOT ask for schema.
  → If the user DID give a custom host, URL, or DSN for any API (public or private): copy it
    verbatim into endpoint_url. Ask only for missing http_method or request/response if truly
    needed — never re-ask for the host itself.
  → NEVER invent a placeholder host/URL (e.g. api.internal-crm.com, api.example.com) when the
    user did not state one. If you asked for private API details and the user withheld them
    ("I don't have docs", "continue without them", "you decide", etc.), leave endpoint_url,
    http_method, request_schema, and response_schema EMPTY and still set is_complete when other
    blockers are resolved. Downstream codegen handles the missing-spec gap.
- Writing to a named DB collection but field schema is completely undefined.
- Email send required but EMAIL_SENDER or EMAIL_PASSWORD (or equivalent) not configured.

Examples of NOT blockers (do NOT ask):
- "How will city name be collected?" → assume UI form.
- "What should email subject/body say?" → assume generated defaults.
- "Who receives the email?" → assume initiating user unless explicitly external-only.
- "Which database engine?" when schema fields are already defined → assume MongoDB.
- "How should the workflow connect to ArcadeDB?" / ArcadeDB connection details or credentials →
  ArcadeDB is platform-managed, never ask.

Severity guide:
- CRITICAL = runtime failure / missing credential / undefined required integration choice.
- WARN = assumable with a standard default (use resolved_flags, empty questions).

Output schema (one JSON object):
{
  "message": "<short message shown above the MCQs>",
  "questions": [
    {
      "question_text": "<question>",
      "options": ["<opt1>", ..., "Other / I\\'ll type my own answer"],
      "flag_type": "<DIMENSION_KEY>",
      "severity": "CRITICAL | WARN"
    }
  ],
  "detected_flags": [{"flag_type": "...", "description": "...", "severity": "...", "resolved": false}],
  "resolved_flags":  [{"flag_type": "...", "description": "...", "severity": "...", "resolved": true, "resolved_value": "..."}],
  "is_complete": false,
  "clarified_requirements": null
}

When is_complete=true, populate clarified_requirements with PRECISE structured fields only:
{
  "llm": { "provider": "google_gemini", "model": "gemini-3-flash-preview", "secret_name": "GEMINI_API_KEY" },
  "external_apis": [{ "provider": "openweathermap", "purpose": "weather_by_city", "is_public": true, "endpoint_url": "", "http_method": "GET", "request_schema": "", "response_schema": "" }],
  "email": { "secret_names": ["EMAIL_SENDER", "EMAIL_PASSWORD"], "smtp": "gmail" },
  "db_schemas": [{ "collection_name": "...", "fields": [{"name": "...", "type": "..."}] }],
  "requirements_analyzed": [
    "Fetch weather from OpenWeatherMap for input city",
    "Summarize with gemini-3-flash-preview",
    "Insert row into weather_logs (see db_schemas)",
    "Send email via configured SMTP"
  ]
}
Rules for completion payload:
- llm: exact provider id, model id, and secret_name (match configured_secrets when present).
- external_apis: one entry per external API used. Fill fields as follows:
  • provider: slug (e.g. "stripe", "openweathermap", "internal_crm")
  • purpose: short verb phrase (e.g. "charge_card", "weather_by_city")
  • is_public: true if the API has public documentation (Stripe, OpenWeatherMap, GitHub, Twilio,
    SendGrid, etc.); false if it is a private/internal/undocumented endpoint.
  • endpoint_url: ALWAYS copy verbatim when the user stated a URL, host:port DSN, or path —
    including well-known public APIs (Unipile, Stripe, GitHub, …). Also fill for private APIs
    collected via Stage 0 questioning. Leave empty ONLY when the user did not provide one.
  • http_method: fill when the user stated GET/POST/PUT/PATCH/DELETE, or when known for a
    private API. Leave empty only when unknown.
  • request_schema: copy user-stated request params/body fields verbatim when present.
  • response_schema: copy user-stated response fields/shape verbatim when present.
  For PUBLIC APIs with NO user-stated URL/method: set is_public=true and leave endpoint
  fields empty — the LLM knows these APIs and will implement calls from training knowledge
  (no grounding needed, no schema question needed).
  For PRIVATE APIs: is_public=false. Fill endpoint_url / http_method / request_schema /
  response_schema ONLY with values the user stated (prompt or answers). If the user withheld
  those details after being asked, leave them EMPTY — NEVER invent a host like
  api.internal-crm.com or a guessed path. Empty private-API fields are allowed at completion.
- email: only when workflow sends email; smtp is gmail unless user chose Outlook.
- requirements_analyzed: 3–8 short implementation steps (no UI narration, no repeating db field lists).
- Omit llm / email / external_apis keys when not used. Do NOT emit resolved_conditions,
  gaps_needing_setup, already_configured, or logical_steps.
""".strip()

MAX_STAGE0B_TURNS = 10

# Global explicit cache for the static 0-B system prompt (~1.4k tokens).
# Key hashes prompt text so edits auto-invalidate without a manual version bump.
_STAGE0B_CACHE_KEY = "stage0b_system_" + hashlib.sha256(
    _STAGE0B_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:12]
_STAGE0B_CACHE_TTL_SECONDS = 3600


async def _get_stage0b_cached_content(api_key: str, model: str) -> Optional[str]:
    """
    Resource name for the shared Stage 0-B system-instruction cache.

    Cost optimization only — returns None on any failure so 0-B keeps working
    via the existing uncached prompt path.
    """
    try:
        return await get_or_create_cache(
            model=model,
            system_text=_STAGE0B_SYSTEM_PROMPT,
            ttl_seconds=_STAGE0B_CACHE_TTL_SECONDS,
            cache_key=_STAGE0B_CACHE_KEY,
            api_key=api_key or None,
            display_name="stage0b-system-prompt",
        )
    except Exception as exc:
        logger.warning("Stage 0-B cache unavailable, continuing uncached: %s", exc)
        return None


def _strip_json_fences(raw: str) -> str:
    """Remove optional markdown code fences from LLM JSON output."""
    clean = raw.strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.split("\n")[1:])
    if clean.endswith("```"):
        clean = "\n".join(clean.split("\n")[:-1])
    return clean.strip()


async def _call_llm(
    system: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    sub_operation: str = "stage0-call",
    cached_content: Optional[str] = None,
) -> str:
    """
    Call Gemini with JSON output.

    Does not pass response_schema — nested Pydantic models (dict fields, etc.)
    produce additionalProperties, which the Gemini API rejects.
    Callers parse the returned text with Pydantic validators.

    When *cached_content* is set, *system* is already stored server-side and
    must not be prepended to the per-request prompt (avoids double billing).
    """
    user_content = messages[-1]["content"] if messages else ""
    if cached_content:
        prompt = f"Input:\n{user_content}"
    else:
        prompt = f"{system}\n\nInput:\n{user_content}"

    started_at = datetime.utcnow()
    try:
        response = await async_generate(
            model,
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.2,
                response_mime_type="application/json",
                cached_content=cached_content,
            ),
            api_key=api_key,
        )
        finished_at = datetime.utcnow()
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty response for Stage 0.")
        
        # Calculate token counts
        tokens = extract_token_usage_from_response(response)
        tracking_metadata: dict[str, Any] = {
            "cached_content_active": bool(cached_content),
        }
        cached_tokens = int(tokens.get("cached_content_token_count", 0) or 0)
        if cached_tokens:
            tracking_metadata["cached_content_token_count"] = cached_tokens
        
        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="stage0",
            sub_operation=sub_operation,
            model=model,
            prompt_tokens=tokens.get("prompt_tokens", 0),
            completion_tokens=tokens.get("completion_tokens", 0),
            total_tokens=tokens.get("total_tokens", 0),
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            metadata=tracking_metadata,
        )
        return text
    except Exception as exc:
        finished_at = datetime.utcnow()
        record_llm_call(
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            operation="stage0",
            sub_operation=sub_operation,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            metadata={"error": str(exc)},
        )
        raise



def _build_stage0b_user_message(
    raw_prompt: str,
    configured_secrets: list[str],
    conversation_history: list[Turn],
    unresolved_flags: list[RequirementFlag],
    *,
    newly_configured_secrets: Optional[list[str]] = None,
    entity_resolution: Optional[dict[str, Any]] = None,
) -> str:

    try:
        with open('config.json', 'r') as _f:
            _sys_vars = [g.get("name") for g in json.load(_f).get("available_global_variables", []) if g.get("name")]
            _extra = [n.split("_")[0] + "".join(p.capitalize() for p in n.split("_")[1:]) for n in _sys_vars if "_" in n]
            _global_variables = sorted(set(_sys_vars + _extra))
    except Exception:
        _global_variables = []

    payload: dict[str, Any] = {
        "raw_prompt": raw_prompt,
        "configured_secrets": configured_secrets,
        "newly_configured_secrets": newly_configured_secrets or [],
        "system_variables": _global_variables,
        "conversation_history": [t.model_dump() for t in conversation_history],
        "unresolved_flags": [f.model_dump() for f in unresolved_flags],
    }
    if entity_resolution:
        payload["entity_resolution"] = entity_resolution
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_stage0b_response(raw: str) -> Stage0B_Response:
    """Parse and validate the LLM JSON output for Sub-Phase 0-B."""
    clean = _strip_json_fences(raw)
    data = json.loads(repair_json(clean))
    return Stage0B_Response.model_validate(data)


# --------------------------------------------------------------------------- #
# Existing data: shortlist (3-4) -> LLM advisor SUGGESTS -> the USER decides
# --------------------------------------------------------------------------- #
ENTITY_FLAG_TYPE = "EXISTING_DATA_SOURCE"
_ENTITY_CATALOG_TTL_S = 60.0
_entity_catalog_cache: dict[tuple[str, str], tuple[float, list]] = {}


async def _get_entity_catalog(subscriber_id: str, org_id: str) -> list:
    """Registry entities for this subscriber/org, enriched with metadata (60 s cache)."""
    import asyncio as _asyncio
    import time as _time

    from services.entity_resolver import attach_metadata, build_catalog, load_collection_metadata

    key = (subscriber_id, org_id)
    hit = _entity_catalog_cache.get(key)
    if hit and (_time.monotonic() - hit[0]) < _ENTITY_CATALOG_TTL_S:
        return hit[1]
    catalog = await build_catalog(subscriber_id, org_id)
    if catalog:
        meta = await _asyncio.to_thread(load_collection_metadata, subscriber_id, org_id)
        attach_metadata(catalog, meta)
    _entity_catalog_cache[key] = (_time.monotonic(), catalog)
    return catalog


async def resolve_existing_entities(
    requirement_text: str,
    *,
    subscriber_id: Optional[str],
    org_id: Optional[str],
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    conversation_history: Optional[list["Turn"]] = None,
    unresolved_flags: Optional[list["RequirementFlag"]] = None,
) -> tuple[Optional[Any], list]:
    """
    Best effort; NEVER raises (on any failure Stage 0 behaves exactly as before).

      1. shortlist the 3-4 registry collections that could be what the requirement means
      2. first turn: ask the advisor LLM to SUGGEST one (it only sees names, fields, structure
         and purpose metadata, never the storage engine)
      3. the caller asks the USER: use X / create a new one / other
      4. later turns: no LLM call; the user's answer is read back from conversation_history
    Nothing similar in the registry -> look at the live databases -> otherwise a new collection.
    """
    if not (subscriber_id and org_id):
        return None, []
    conversation_history = conversation_history or []
    unresolved_flags = unresolved_flags or []
    try:
        from services.entity_resolver import (
            STORE_MONGO, Resolution, advise, decide_resolution, make_live_listers,
            parse_entity_answer, probe_live, resolve_entity, shortlist_candidates,
        )

        catalog = await _get_entity_catalog(subscriber_id, org_id)
        shortlist = shortlist_candidates(requirement_text, catalog)

        if not shortlist:
            list_mongo, list_arcade = make_live_listers(subscriber_id)
            live = await probe_live(
                requirement_text, list_mongo_collections=list_mongo, list_arcade_types=list_arcade
            )
            if live.status == "none":
                logger.warning(
                    "[Stage0 entity] nothing found in registry or live DBs; assuming a new %s collection",
                    STORE_MONGO,
                )
                return Resolution("none", [], live.reason), catalog
            return live, catalog

        keyword = resolve_entity(requirement_text, catalog)
        user_turns = [t for t in conversation_history if t.role == "user"]
        if user_turns:
            answered_choice = any(
                parse_entity_answer(t.content, shortlist)[0] in ("use", "new") for t in user_turns
            )
            asked = answered_choice or any(f.flag_type == ENTITY_FLAG_TYPE for f in unresolved_flags)
            if asked:
                return Resolution("ask", shortlist, "user answered"), catalog
            # never asked (it was bound silently on the first turn): repeat that decision, no LLM call
            return (keyword if keyword.status == "bound" else None), catalog

        async def _advisor_llm(system: str, user: str) -> str:
            return await _call_llm(
                system=system,
                messages=[{"role": "user", "content": user}],
                api_key=api_key,
                model=model,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                sub_operation="stage0-entity-advisor",
            )

        advice = await advise(requirement_text, shortlist, _advisor_llm)
        return decide_resolution(keyword, advice, shortlist), catalog
    except Exception as exc:
        logger.warning("[Stage0 entity] resolution skipped: %s", exc, exc_info=True)
        return None, []


def _apply_entity_resolution_to_response(
    parsed: "Stage0B_Response",
    resolution: Optional[Any],
    conversation_history: list["Turn"],
) -> "Stage0B_Response":
    """
    First turn: make sure the user is asked (deterministically, with metadata, suggestion first).
    Later turns: read the user's answer back (use X / create new / other) and close the flag.
    """
    if resolution is None or resolution.status != "ask":
        return parsed

    from services.entity_resolver import build_question, parse_entity_answer

    user_turns = [t for t in conversation_history if t.role == "user"]
    if user_turns:
        resolution.choice, resolution.chosen = "other", None
        for t in reversed(user_turns):
            kind, ent = parse_entity_answer(t.content, resolution.entities)
            if kind in ("use", "new"):
                resolution.choice, resolution.chosen = kind, ent
                break
        flag = RequirementFlag(
            flag_type=ENTITY_FLAG_TYPE,
            description="Which existing data this requirement refers to",
            severity="CRITICAL",
            resolved=True,
            resolved_value=user_turns[-1].content,
        )
        logger.info(
            "[Stage0 entity] user decision=%s chosen=%s",
            resolution.choice, getattr(resolution.chosen, "name", None),
        )
        return parsed.model_copy(
            update={
                "questions": [q for q in parsed.questions if q.flag_type != ENTITY_FLAG_TYPE],
                "resolved_flags": [*parsed.resolved_flags, flag],
            }
        )

    text, options = build_question(resolution)
    question = MCQQuestion(
        question_text=text,
        options=options,
        flag_type=ENTITY_FLAG_TYPE,
        severity="CRITICAL",
    )
    flag = RequirementFlag(
        flag_type=ENTITY_FLAG_TYPE,
        description=f"Requirement may match existing data ({resolution.reason})",
        severity="CRITICAL",
    )
    return parsed.model_copy(
        update={
            "questions": [question, *[q for q in parsed.questions if q.flag_type != ENTITY_FLAG_TYPE]],
            "detected_flags": [*parsed.detected_flags, flag],
            "is_complete": False,
            "clarified_requirements": None,
        }
    )


def bind_clarified_to_existing(
    clarified: Optional["ClarifiedRequirements"],
    resolution: Optional[Any],
    catalog: list,
) -> Optional["ClarifiedRequirements"]:
    """
    Apply the decision to db_schemas.

      * user chose an existing collection (or a silent exact-name read): the best matching schema is
        rewritten to that collection's canonical name and marked existing (store, labels, relationships)
      * user chose "Create a new one": schemas that overlap the shortlisted collections are marked
        user_decision="new" and renamed if the name would collide, so nothing is merged or reused
      * anything else: db_schemas are left exactly as the LLM produced them
    """
    if clarified is None or resolution is None:
        return clarified

    from services.entity_resolver import tokenize, unique_collection_name

    schemas = list(clarified.db_schemas)

    if resolution.choice == "use" and resolution.chosen is not None:
        targets = [resolution.chosen]
    elif resolution.status == "bound":
        targets = list(resolution.entities)
    else:
        targets = []

    def _field_tokens(names: list[str]) -> set:
        return set(tokenize(" ".join(names)))

    for ent in targets:
        want_name = set(tokenize(ent.name))
        want_fields = _field_tokens(ent.fields)
        best_i, best_score = None, 0
        for i, sch in enumerate(schemas):
            if sch.existing:
                continue
            score = 10 * len(want_name & set(tokenize(sch.collection_name))) + len(
                want_fields & _field_tokens([f.get("name", "") for f in sch.fields])
            )
            if score > best_score:
                best_i, best_score = i, score
        common = dict(
            collection_name=ent.name,
            existing=True,
            user_decision="use_existing",
            target_store=ent.store,
            labels=list(ent.labels),
            relationships=list(ent.relationships),
        )
        if best_i is not None:
            sch = schemas[best_i]
            schemas[best_i] = sch.model_copy(
                update={**common, "fields": sch.fields or [{"name": f, "type": "string"} for f in ent.fields]}
            )
        else:
            schemas.append(
                DBSchema(fields=[{"name": f, "type": "string"} for f in ent.fields], **common)
            )
            logger.info("[Stage0 entity] injected chosen entity %s (%s)", ent.name, ent.store)

    if resolution.choice == "new":
        near: set = set()
        for e in resolution.entities:
            near |= set(tokenize(e.name))
        used = [e.name for e in catalog]
        for i, sch in enumerate(schemas):
            if sch.existing or not (set(tokenize(sch.collection_name)) & near):
                continue
            new_name = unique_collection_name(sch.collection_name, used)
            used.append(new_name)
            schemas[i] = sch.model_copy(update={"collection_name": new_name, "user_decision": "new"})
            logger.info("[Stage0 entity] new collection %s -> %s", sch.collection_name, new_name)

    return clarified.model_copy(update={"db_schemas": schemas})


async def run_stage0b_single_turn(
    raw_prompt: str,
    configured_secret_names: list[str],
    api_key: str,
    conversation_history: list[Turn],
    unresolved_flags: list[RequirementFlag],
    *,
    newly_configured_secrets: Optional[list[str]] = None,
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Stage0B_Response:
    """Run a single Sub-Phase 0-B LLM turn (used by the interactive API)."""
    resolution, entity_catalog = await resolve_existing_entities(
        raw_prompt,
        subscriber_id=subscriber_id,
        org_id=org_id,
        api_key=api_key,
        model=model,
        project_id=project_id,
        user_id=user_id,
        conversation_history=conversation_history,
        unresolved_flags=unresolved_flags,
    )
    entity_payload = None
    if resolution is not None and resolution.status in ("bound", "ask"):
        from services.entity_resolver import prompt_payload

        entity_payload = prompt_payload(resolution)
        logger.info(
            "[Stage0 entity] status=%s candidates=%s reason=%s",
            resolution.status, [e.name for e in resolution.entities], resolution.reason,
        )
    user_msg_content = _build_stage0b_user_message(
        raw_prompt=raw_prompt,
        configured_secrets=configured_secret_names,
        conversation_history=conversation_history,
        unresolved_flags=unresolved_flags,
        newly_configured_secrets=newly_configured_secrets,
        entity_resolution=entity_payload,
    )
    raw = ""
    cached_content = await _get_stage0b_cached_content(api_key, model)
    try:
        raw = await _call_llm(
            system=_STAGE0B_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg_content}],
            api_key=api_key,
            model=model,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            sub_operation="stage0b-single-turn",
            cached_content=cached_content,
        )
        parsed = _parse_stage0b_response(raw)
        parsed = _apply_entity_resolution_to_response(parsed, resolution, conversation_history)
        if parsed.clarified_requirements is not None:
            parsed = parsed.model_copy(
                update={
                    "clarified_requirements": bind_clarified_to_existing(
                        parsed.clarified_requirements, resolution, entity_catalog
                    )
                }
            )
        return parsed
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Stage 0-B parse error: %s", exc)
        logger.debug("Raw LLM output: %s", raw)
        raise RuntimeError(f"Stage 0-B: failed to parse LLM response: {exc}") from exc



def _flag_is_duplicate(new_flag: RequirementFlag, existing_flags: list[RequirementFlag]) -> bool:
    """
    True if a semantically equivalent flag already exists.
    Handles variant LLM names like LLM_MODEL_VERSION vs LLM_MODEL_VERSION_GEMINI,
    UI_INPUT_METHOD vs UI_INPUT_METHOD_CITY, DB_ENGINE vs DB_ENGINE_weather_logs, etc.
    """
    new_ft = new_flag.flag_type.upper()
    for existing in existing_flags:
        ex_ft = existing.flag_type.upper()
        if ex_ft == new_ft:
            return True
        # Prefix match: new is a more specific variant of an existing flag
        if new_ft.startswith(ex_ft + "_") or ex_ft.startswith(new_ft + "_"):
            return True
    return False


def merge_stage0b_flag_state(
    detected_flags: list[RequirementFlag],
    parsed: Stage0B_Response,
    configured_secrets: Optional[list[str]] = None,
) -> list[RequirementFlag]:
    """Merge detected/resolved flags from one 0-B turn into session state.

    IMPORTANT: the LLM's own claim that a flag is "resolved" (e.g. "user
    provided the API token in chat") is a textual judgement, not proof that a
    credential was actually written to the vault. Trusting it blindly let
    workflows report full requirement clarity while CredentialService held
    nothing — the planner (and eventually the deployed workflow) then ran
    with a phantom credential. For any flag with a known implied secret name,
    the LLM's resolution is only accepted once those secrets are confirmed
    present in ``configured_secrets``; otherwise it stays open so the value
    can actually be captured (see ``_resolve_raw_value_flags``).
    """
    configured = list(configured_secrets or [])
    for flag in parsed.detected_flags:
        if not _flag_is_duplicate(flag, detected_flags):
            detected_flags.append(flag)

    for flag in parsed.resolved_flags:
        flag_ft = flag.flag_type.upper()
        for existing in detected_flags:
            ex_ft = existing.flag_type.upper()
            if (ex_ft == flag_ft
                    or ex_ft.startswith(flag_ft + "_")
                    or flag_ft.startswith(ex_ft + "_")):
                implied = _secret_names_implied_by_flag(existing)
                if implied and not all(secret_is_configured(s, configured) for s in implied):
                    # LLM says resolved, vault disagrees — do not trust it.
                    logger.warning(
                        "Stage 0: LLM reported flag %s as resolved but implied "
                        "secrets %s are not in the credential vault yet; "
                        "keeping flag open.",
                        existing.flag_type, implied,
                    )
                    continue
                existing.resolved = True
                if flag.resolved_value:
                    existing.resolved_value = flag.resolved_value

    return detected_flags


_FIELD_CONCEPT_KEYWORDS: list[tuple[str, str]] = [
    ("client_secret", "CLIENT_SECRET"),
    ("client_id", "CLIENT_ID"),
    ("api_token", "API_TOKEN"),
    ("api_key", "API_KEY"),
    ("access_token", "ACCESS_TOKEN"),
    ("refresh_token", "REFRESH_TOKEN"),
    ("auth_token", "AUTH_TOKEN"),
    ("bearer_token", "BEARER_TOKEN"),
    ("access_key", "ACCESS_KEY"),
    ("secret_key", "SECRET_KEY"),
    ("secret_access_key", "SECRET_ACCESS_KEY"),
    ("webhook_url", "WEBHOOK_URL"),
    ("base_url", "BASE_URL"),
    ("account_sid", "ACCOUNT_SID"),
    ("username", "USERNAME"),
    ("password", "PASSWORD"),
    ("email", "EMAIL"),
    ("phone_number", "PHONE_NUMBER"),
    ("token", "TOKEN"),  # generic catch-all, checked after the more specific *_token entries
    ("key", "API_KEY"),  # generic catch-all for bare "key"
    ("secret", "SECRET"),
]

_FLAG_TYPE_GENERIC_SUFFIXES = (
    "_CREDENTIALS", "_CREDENTIAL", "_CREDS", "_AUTH", "_AUTHENTICATION",
    "_CONFIG", "_CONFIGURATION", "_INTEGRATION", "_CONNECTION", "_ACCESS",
    "_SETUP", "_API_KEY", "_APIKEY", "_KEY", "_TOKEN", "_SECRET",
)


def _service_prefix_from_flag_type(flag_type: str) -> str:
    """Recover the bare service name from a flag_type, e.g. JIRA_CREDENTIALS -> JIRA."""
    ft = (flag_type or "").upper().strip()
    for suffix in _FLAG_TYPE_GENERIC_SUFFIXES:
        if ft.endswith(suffix) and len(ft) > len(suffix):
            ft = ft[: -len(suffix)]
            break
    return ft or "SERVICE"


def _derive_secret_names_dynamic(flag: RequirementFlag) -> list[str]:
    """
    Derive secret key name(s) for a credential flag with no static template,
    purely from the flag's own flag_type + description — no per-service entry
    needs to be added anywhere for this to work on a brand-new integration.

    Scans the flag's description (where the LLM typically spells out exactly
    what's required, e.g. "Jira API requires an Email and API Token") for
    generic credential-field concepts, in the order they're mentioned, and
    builds ``{SERVICE}_{FIELD}`` names from them. Falls back to a single
    ``{SERVICE}_API_KEY`` when nothing more specific is mentioned.
    """
    prefix = _service_prefix_from_flag_type(flag.flag_type)
    description = (getattr(flag, "description", "") or "").lower()
    blob = _flag_blob(flag).lower()
    text = f"{description} {blob}"

    found_suffixes: list[str] = []
    accepted: list[tuple[int, int, str]] = []
    for keyword, suffix in _FIELD_CONCEPT_KEYWORDS:
        needle = keyword.replace("_", " ")
        pos = text.find(needle)
        if pos == -1:
            needle = keyword.replace(" ", "_")
            pos = text.find(needle)
        if pos == -1:
            continue
        span_end = pos + len(needle)
        nested_in_existing = any(
            pos >= a_start and span_end <= a_end for a_start, a_end, _ in accepted
        )
        if nested_in_existing:
            continue
        accepted.append((pos, span_end, suffix))

    for _, _, suffix in sorted(accepted):
        if suffix not in found_suffixes:
            found_suffixes.append(suffix)

    if not found_suffixes:
        return [f"{prefix}_API_KEY"]
    return [f"{prefix}_{suffix}" for suffix in found_suffixes]


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_GENERIC_DOMAIN_LABELS = frozenset({
    "www", "api", "app", "apps", "my", "portal", "console", "dashboard",
    "secure", "auth", "login", "id", "accounts", "cdn", "static",
})


def _service_prefix_from_url(url: str) -> str:
    """
    Derive a service-name-like prefix from a URL's host, generically.

    E.g. https://foo-123.atlassian.net/... -> "ATLASSIAN"
         https://api.stripe.com/...        -> "STRIPE"
         https://hooks.slack.com/...       -> "SLACK"

    Domain labels are read right-to-left (skipping the TLD): the label
    immediately before the TLD is normally the actual service/platform name
    ("atlassian" in "*.atlassian.net"), while anything further left is a
    subdomain, site slug, or generic hosting label ("api", "www", a personal
    site slug like "atulbankar1-1788343175457"). No per-provider mapping.
    """
    try:
        host = urlsplit(url).netloc.split("@")[-1].split(":")[0]
    except Exception:
        return "SERVICE"
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        chosen = labels[0] if labels else "SERVICE"
    else:
        # Walk right-to-left starting just before the TLD; first non-generic
        # label wins. Falls back to the label right before the TLD either way.
        chosen = labels[-2]
        for label in reversed(labels[:-1]):
            if label.lower() not in _GENERIC_DOMAIN_LABELS:
                chosen = label
                break
    return re.sub(r"[^A-Za-z0-9]+", "_", chosen).strip("_").upper() or "SERVICE"


def extract_url_config_params(text: str) -> list[tuple[str, str, str]]:
    """
    Find every URL in ``text`` and pull out its base URL and query-string
    parameters as candidate workflow config values.

    Deliberately over-captures (keeps every query param, not just ones that
    "look" project-specific) rather than trying to semantically guess which
    ones matter — losing a value like a project key silently is worse than
    storing a couple of harmless extra config entries like page size.
    """
    out: list[tuple[str, str, str]] = []
    seen_base_urls: set[str] = set()
    for url in _URL_RE.findall(text or ""):
        url = url.rstrip(").,;\"'")
        parsed = urlsplit(url)
        if not parsed.netloc:
            continue
        prefix = _service_prefix_from_url(url)

        base_url = f"{parsed.scheme}://{parsed.netloc}"
        base_name = f"{prefix}_BASE_URL"
        dedup_key = f"{base_name}:{base_url}"
        if dedup_key not in seen_base_urls:
            seen_base_urls.add(dedup_key)
            out.append((base_name, base_url, url))

        if not parsed.query:
            continue
        for key, value in parse_qsl(parsed.query, keep_blank_values=False):
            if not key or not value:
                continue
            name = f"{prefix}_{re.sub(r'[^A-Za-z0-9]+', '_', key).strip('_').upper()}"
            out.append((name, value, url))
    return out


async def persist_url_config_params(
    raw_prompt: str,
    *,
    credential_service: Any,
    workflow_id: str,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
) -> list[str]:
    """
    Extract and store any URL query-string values embedded directly in the
    user's raw prompt (e.g. a Jira ``project=KAN``) as named, non-secret
    config values, so they're recorded under a stable name instead of ending
    up as an untracked literal baked into generated code.

    Returns the list of names actually stored.
    """
    if credential_service is None or not raw_prompt:
        return []
    params = extract_url_config_params(raw_prompt)
    saved: list[str] = []
    for name, value, source_url in params:
        try:
            await _store_credential_adaptive(
                credential_service,
                workflow_id=workflow_id,
                name=name,
                value=value,
                type="config",
                service=_service_prefix_from_url(source_url).lower(),
                org_id=org_id,
                subscriber_id=subscriber_id,
            )
        except Exception:
            logger.exception(
                "Stage 0: failed to auto-save URL config param %s for workflow %s",
                name, workflow_id,
            )
            continue
        saved.append(name)
        logger.info(
            "Stage 0: auto-saved workflow config %s=%s for workflow %s (from prompt URL)",
            name, value, workflow_id,
        )
    return saved

_CREDENTIAL_SIGNAL_WORDS = (
    "credential", "api key", "api_key", "apikey", "token", "password",
    "secret", "auth", "oauth", "login", "access key", "access_key",
    "connection string", "client id", "client secret", "bearer",
)

_CREDENTIAL_FLAG_TYPE_SUFFIXES = (
    "_CREDENTIALS", "_CREDENTIAL", "_CREDS", "_AUTH", "_AUTHENTICATION",
    "_API_KEY", "_APIKEY", "_TOKEN", "_SECRET", "_ACCESS_KEY", "_LOGIN",
    "_OAUTH", "_PASSWORD",
)


def _flag_looks_credential_related(flag: RequirementFlag) -> bool:
    """True only if this flag is plausibly asking for a secret/credential."""
    ft = (flag.flag_type or "").upper()
    if ft.endswith(_CREDENTIAL_FLAG_TYPE_SUFFIXES):
        return True
    text = f"{getattr(flag, 'description', '') or ''} {_flag_blob(flag)}".lower()
    return any(word in text for word in _CREDENTIAL_SIGNAL_WORDS)

def _secret_names_implied_by_flag(flag: RequirementFlag) -> list[str]:
    """
    Secret key name(s) implied by a credential-related flag.

    Order of resolution:
      1. A static GUIDANCE_TEMPLATES / alias-group entry, if one exists —
         these exist only where we want curated setup steps + a provider URL,
         not as a requirement for the credential to be storable at all.
      2. Otherwise, derive the name(s) dynamically from the flag itself
         (see ``_derive_secret_names_dynamic``) — this is what makes a brand
         new integration work with zero code changes.
    """
    canonical = _canonical_guidance_key(flag.flag_type)
    tmpl = GUIDANCE_TEMPLATES.get(canonical)
    if tmpl and tmpl.secret_keys:
        return list(tmpl.secret_keys)
    blob = _flag_blob(flag)
    found: list[str] = []
    for group in _SECRET_ALIAS_GROUPS:
        for name in group:
            if _normalize_secret_key(name) and _normalize_secret_key(name) in _normalize_secret_key(blob):
                found.append(next(iter(group)))
                break
    if found:
        return found
    if not _flag_looks_credential_related(flag):
        return []
    return _derive_secret_names_dynamic(flag)


def _auto_resolve_assumable_flags(
    flags: list[RequirementFlag],
    configured_secrets: list[str],
) -> None:
    """
    Mark non-blocker flags as resolved in place.

    Also deterministically resolves credential flags when a matching secret is
    already present in ``configured_secrets`` (semantic alias match), so Stage 0
    does not keep asking after the user configured Gemini/OpenAI/etc.
    """
    configured = list(configured_secrets or [])

    for flag in flags:
        if flag.resolved:
            continue

        ft = flag.flag_type.upper()

        # Credential already satisfied → resolve (do not re-ask)
        implied = _secret_names_implied_by_flag(flag)
        if implied and all(secret_is_configured(s, configured) for s in implied):
            matched = find_configured_secret_name(implied[0], configured) or implied[0]
            flag.resolved = True
            flag.resolved_value = f"credential configured: {matched}"
            continue

        # Never ask — always assume (exact match, prefix variant, or platform-DB keyword)
        auto_types_upper = {t.upper() for t in _AUTO_RESOLVE_FLAG_TYPES}
        is_auto = (
            ft in auto_types_upper
            or any(ft.startswith(t + "_") or t.startswith(ft + "_") for t in auto_types_upper)
            or _is_platform_db_flag_type(ft)
        )
        if is_auto:
            flag.resolved = True
            flag.resolved_value = _AUTO_RESOLVE_DEFAULTS.get(
                flag.flag_type, _AUTO_RESOLVE_DEFAULTS.get(
                    ft, "platform-managed (MongoDB/ArcadeDB, auto-selected)" if _is_platform_db_flag_type(ft) else "assumed default"
                )
            )
            continue

        # WARN = non-blocker by definition
        if flag.severity == "WARN":
            flag.resolved = True
            flag.resolved_value = flag.resolved_value or "assumed default (non-blocker)"
            continue


_DELEGATION_PHRASES = frozenset({
    "you decide", "your choice", "your call", "up to you",
    "free to", "feel free", "you can choose", "you choose",
    "whatever", "doesn't matter", "don't care",
    "go ahead", "proceed",
})

_CONFIGURE_LATER_PHRASES = frozenset({
    "configure later",
    "configure it later",
    "i'll configure later",
    "i will configure later",
    "setup later",
    "set up later",
    "add later",
    "configure afterwards",
    "continue building",
    "will configure later",
})


def _format_stage0b_assistant_turn(message: str, questions: list[MCQQuestion]) -> str:
    """Build assistant turn content so later turns know what was asked."""
    if not questions:
        return message
    questions_text = "\n".join(f"Q: {q.question_text}" for q in questions)
    return f"{message}\n\nQuestions asked:\n{questions_text}"


def _resolve_delegated_flags(
    questions: list[MCQQuestion],
    user_answer: str,
    detected_flags: list[RequirementFlag],
) -> None:
    """
    If the user's answer delegates the decision, mark asked flags as resolved.

    Prevents the LLM from re-asking when it fails to self-report resolution.
    """
    answer_lower = user_answer.lower().strip()
    is_delegation = any(phrase in answer_lower for phrase in _DELEGATION_PHRASES)
    if not is_delegation:
        return

    asked_flag_types = {q.flag_type.upper() for q in questions}
    for flag in detected_flags:
        ft = flag.flag_type.upper()
        if flag.resolved:
            continue
        if ft in asked_flag_types or any(
            ft.startswith(t + "_") or t.startswith(ft + "_") for t in asked_flag_types
        ):
            flag.resolved = True
            flag.resolved_value = f"Delegated to workflow builder: {user_answer}"


def _resolve_configure_later_flags(
    questions: list[MCQQuestion],
    user_answer: str,
    detected_flags: list[RequirementFlag],
) -> None:
    """
    If the user says they will configure credentials later, mark asked flags resolved.

    Pipeline continues; code generation emits real code that returns a runtime
    Missing credential error until the secret is configured — never stub tools.
    """
    answer_lower = user_answer.lower().strip()
    is_later = any(phrase in answer_lower for phrase in _CONFIGURE_LATER_PHRASES)
    if not is_later:
        return

    asked_flag_types = {q.flag_type.upper() for q in questions}
    for flag in detected_flags:
        ft = flag.flag_type.upper()
        if flag.resolved:
            continue
        if ft in asked_flag_types or any(
            ft.startswith(t + "_") or t.startswith(ft + "_") for t in asked_flag_types
        ):
            flag.resolved = True
            flag.resolved_value = "configure_later"


def _is_intent_answer(user_answer: str, questions: list[MCQQuestion]) -> bool:
    """
    True when the answer expresses an *intent* (option pick / delegation /
    configure-later) rather than supplying an actual credential value.

    Provider-agnostic: the intent side is closed-world (we enumerate the
    phrases and the MCQ options), so anything else on a credential flag is
    treated as a value. No hardcoded API-key formats.
    """
    answer_lower = (user_answer or "").lower().strip()
    if not answer_lower:
        return True
    if any(phrase in answer_lower for phrase in _DELEGATION_PHRASES):
        return True
    if any(phrase in answer_lower for phrase in _CONFIGURE_LATER_PHRASES):
        return True
    for q in questions:
        for opt in (q.options or []):
            opt_l = str(opt).lower().strip()
            if opt_l and (
                answer_lower == opt_l
                or answer_lower in opt_l
                or opt_l in answer_lower
            ):
                return True
    return False


_SECRET_ASSIGNMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9_\-\. ]{1,60}?)\s*[:=]\s*(?P<value>\S.*?)\s*$"
)

_GENERIC_ANSWER_LABELS = frozenset({
    "COMMENT", "COMMENTS", "ANSWER", "ANS", "NOTE", "NOTES", "VALUE",
    "INPUT", "REPLY", "RESPONSE", "TEXT", "DETAILS", "DETAIL", "INFO",
    "DESCRIPTION", "MESSAGE", "MSG", "OTHER", "CUSTOM", "USER_ANSWER",
    "USER_COMMENT", "ADDITIONAL", "ADDITIONAL_INFO", "REMARK", "REMARKS",
})


def _parse_explicit_assignment(text: str) -> Optional[tuple[str, str]]:
    """
    Parse a user-supplied assignment such as ``MY_TOKEN = abc123`` or
    ``api key: abc123`` into ``(secret_name, value)``.

    This is what makes the unknown-platform case work: the user names the key
    themselves, so Stage 0 does not need to recognise the platform at all.
    """
    match = _SECRET_ASSIGNMENT_RE.match(text or "")
    if not match:
        return None
    name = _normalize_secret_key(match.group("name"))
    value = match.group("value").strip().strip("\"'")
    if not name or not value:
        return None
    if name in _GENERIC_ANSWER_LABELS:
        return None
    return name, value


def _strip_generic_answer_label(text: str) -> str:
    """Remove a leading transport label such as ``Comment:`` from an answer."""
    match = _SECRET_ASSIGNMENT_RE.match(text or "")
    if not match:
        return (text or "").strip()
    if _normalize_secret_key(match.group("name")) in _GENERIC_ANSWER_LABELS:
        return match.group("value").strip().strip("\"'")
    return (text or "").strip()


async def _store_credential_adaptive(
    credential_service: Any,
    *,
    workflow_id: str,
    name: str,
    value: str,
    type: str = "api_key",
    service: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
) -> None:
    """
    Call the credential service without hardcoding its method or parameter names.

    The real CredentialService.store_credential() has two REQUIRED params with
    no defaults: ``type`` and ``service``. Earlier this adaptive binder only
    knew about project_id/name/value/org_id/subscriber_id and silently omitted
    them, which raised "missing 2 required positional arguments" at call time.
    This version supplies sensible values for those and, more generally, will
    fill *any* other required parameter it doesn't recognise with a safe
    placeholder rather than failing outright — only raising if a parameter is
    required and no reasonable placeholder can be inferred.
    """
    import inspect

    method = None
    for candidate in (
        "store_credential",
        "save_credential",
        "upsert_credential",
        "create_credential",
        "add_credential",
        "set_credential",
    ):
        fn = getattr(credential_service, candidate, None)
        if callable(fn):
            method = fn
            break
    if method is None:
        raise AttributeError(
            "CredentialService exposes no known credential-write method "
            "(tried store_credential/save_credential/upsert_credential/"
            "create_credential/add_credential/set_credential)"
        )

    service_value = service or name.rsplit("_", 1)[0].lower() or "custom"

    supplied = {
        "project_id": workflow_id,
        "workflow_id": workflow_id,
        "projectId": workflow_id,
        "name": name,
        "key": name,
        "credential_name": name,
        "secret_name": name,
        "value": value,
        "credential_value": value,
        "secret_value": value,
        "type": type,
        "credential_type": type,
        "service": service_value,
        "provider": service_value,
        "scope": "backend_only",
        "org_id": org_id,
        "subscriber_id": subscriber_id,
    }

    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        params = {}

    has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    if params and not has_var_kw:
        kwargs = {k: v for k, v in supplied.items() if k in params and v is not None}
        for pname, p in params.items():
            if pname == "self" or pname in kwargs:
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            lower = pname.lower()
            if "type" in lower:
                kwargs[pname] = type
            elif "service" in lower or "provider" in lower:
                kwargs[pname] = service_value
            elif "scope" in lower:
                kwargs[pname] = "backend_only"
            elif "name" in lower or "key" in lower:
                kwargs[pname] = name
            elif "value" in lower:
                kwargs[pname] = value
            else:
                raise TypeError(
                    f"Cannot auto-bind required parameter '{pname}' on "
                    f"{method.__qualname__}{inspect.signature(method)} — "
                    "add explicit handling in _store_credential_adaptive."
                )

        missing = [
            label
            for label, keys in (
                ("project", ("project_id", "workflow_id", "projectId")),
                ("name", ("name", "key", "credential_name", "secret_name")),
                ("value", ("value", "credential_value", "secret_value")),
            )
            if not any(k in kwargs for k in keys)
        ]
        if missing:
            raise TypeError(
                f"Could not bind {missing} to {method.__qualname__}"
                f"{inspect.signature(method)}"
            )
    else:
        kwargs = {k: v for k, v in supplied.items() if v is not None}

    result = method(**kwargs)
    if inspect.isawaitable(result):
        await result


_SERVICE_LABEL_BY_SECRET_NAME = {
    "GEMINI_API_KEY": "google_gemini",
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "EMAIL_SENDER": "email",
    "EMAIL_PASSWORD": "email",
}


def _infer_service_label(flag: "RequirementFlag", secret_name: str) -> str:
    """Best-effort human-readable service/provider label for a stored credential."""
    known = _SERVICE_LABEL_BY_SECRET_NAME.get(secret_name.upper())
    if known:
        return known
    canonical = _canonical_guidance_key(flag.flag_type)
    tmpl = GUIDANCE_TEMPLATES.get(canonical)
    if tmpl and getattr(tmpl, "service", None):
        return str(tmpl.service)
    stripped = secret_name
    for suffix in ("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "_PASSWORD"):
        if stripped.upper().endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return stripped.lower() or "custom"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A "token-like" chunk: reasonably long, no whitespace, not a plain sentence word.
_TOKEN_LIKE_RE = re.compile(r"[A-Za-z0-9_\-\.\/]{10,}")

_COMMON_ENGLISH_STOPWORDS = frozenset({
    "email", "is", "the", "and", "with", "for", "token", "password",
    "api", "key", "my", "here", "this", "please", "use", "using",
})


def _extract_field_value(secret_name: str, text: str) -> Optional[str]:
    """
    Pull the piece of ``text`` that plausibly satisfies ``secret_name``.

    Field-aware, not format-specific: an *_EMAIL secret looks for an email
    address anywhere in the text; anything else looks for the longest
    token-like chunk that isn't a common English word (so "email is
    jira@mail.com" doesn't cause the literal word "email" to be captured for
    a *_TOKEN secret).
    """
    if not text:
        return None
    upper = secret_name.upper()
    if "EMAIL" in upper:
        match = _EMAIL_RE.search(text)
        return match.group(0) if match else None

    candidates = [
        m.group(0) for m in _TOKEN_LIKE_RE.finditer(text)
        if m.group(0).lower() not in _COMMON_ENGLISH_STOPWORDS
        and not _EMAIL_RE.fullmatch(m.group(0))
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


async def _resolve_raw_value_flags(
    questions: list[MCQQuestion],
    user_answer: str,
    detected_flags: list[RequirementFlag],
    *,
    credential_service: Any,
    workflow_id: str,
    conversation_text: str = "",
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
) -> list[str]:
    """
    Persist a value the user typed directly into the answer box as a credential.

    Without this, a user who does not know the platform and simply pastes the
    value into the free-text answer loses it entirely: the text only ever became
    conversation context, the flag stayed unresolved, and the workflow failed to
    save or generated with a missing credential.

    A flag is only marked resolved once EVERY implied secret has actually been
    written — partial info (e.g. only the email so far) is saved under its own
    name but the flag stays open until the rest arrives.

    Returns the list of secret names actually written this call.
    """
    if credential_service is None:
        return []
    raw = _strip_generic_answer_label((user_answer or "").strip())
    if not raw:
        return []

    explicit = _parse_explicit_assignment(raw)

    # A bare value is only treated as a secret when the answer isn't an intent.
    if not explicit and _is_intent_answer(raw, questions):
        return []

    asked_flag_types = {q.flag_type.upper() for q in questions}
    saved: list[str] = []
    combined_text = f"{conversation_text}\n{raw}" if conversation_text else raw

    for flag in detected_flags:
        ft = flag.flag_type.upper()
        if ft not in asked_flag_types and not any(
            ft.startswith(t + "_") or t.startswith(ft + "_") for t in asked_flag_types
        ):
            continue

        implied = _secret_names_implied_by_flag(flag)

        if flag.resolved and not implied:
            continue  

        pending_names: list[str] = []

        if explicit:
            explicit_name, value = explicit
            secret_name = implied[0] if implied and len(implied) == 1 else explicit_name
            if secret_name not in [n for n, _ in pending_names]:
                pending_names = [(secret_name, value)]
            if implied and len(implied) > 1:
                for other in implied:
                    if other == secret_name:
                        continue
                    other_val = _extract_field_value(other, combined_text)
                    if other_val:
                        pending_names.append((other, other_val))
        elif implied:
            for secret_name in implied:
                value = _extract_field_value(secret_name, combined_text)
                if value:
                    pending_names.append((secret_name, value))
        else:
            continue

        if not pending_names:
            continue

        stored_this_flag: list[str] = []
        for secret_name, value in pending_names:
            service_value = _infer_service_label(flag, secret_name)
            try:
                await _store_credential_adaptive(
                    credential_service,
                    workflow_id=workflow_id,
                    name=secret_name,
                    value=value,
                    type="api_key",
                    service=service_value,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                )
            except Exception:
                logger.exception(
                    "Stage 0: failed to auto-save supplied credential value "
                    "(flag=%s secret=%s workflow=%s)",
                    flag.flag_type,
                    secret_name,
                    workflow_id,
                )
                continue
            stored_this_flag.append(secret_name)
            logger.info(
                "Stage 0: auto-saved user-supplied credential %s for workflow %s (flag=%s)",
                secret_name,
                workflow_id,
                flag.flag_type,
            )

        saved.extend(stored_this_flag)
        if implied:
            still_missing = [n for n in implied if n not in stored_this_flag]
            if still_missing:
                continue
        flag.resolved = True
        flag.resolved_value = f"credential configured: {', '.join(n for n, _ in pending_names)}"

    return saved


_PLATFORM_DB_FLAG_KEYWORDS: tuple[str, ...] = ("ARCADEDB", "GRAPH_DB", "GRAPHDB")


def _is_platform_db_flag_type(flag_type: str) -> bool:
    """
    True for DB_TYPE/DB_ENGINE plus any LLM-invented flag name that is clearly about
    connecting to/configuring ArcadeDB or a graph database — the LLM names these flags
    freely (semantic matching), so an exact-match set alone misses variants like
    ARCADEDB_SETUP or GRAPH_DATABASE_CONFIG.
    """
    ft = flag_type.upper()
    if ft in {t.upper() for t in _AUTO_RESOLVE_FLAG_TYPES}:
        return True
    return any(kw in ft for kw in _PLATFORM_DB_FLAG_KEYWORDS)


def _filter_blocker_questions(questions: list[MCQQuestion]) -> list[MCQQuestion]:
    """Strip MCQs for flag types that must never be shown to the user."""
    auto_types_upper = {t.upper() for t in _AUTO_RESOLVE_FLAG_TYPES}
    result = []
    for q in questions:
        ft = q.flag_type.upper()
        is_auto = (
            ft in auto_types_upper
            or any(ft.startswith(t + "_") or t.startswith(ft + "_") for t in auto_types_upper)
            or _is_platform_db_flag_type(ft)
        )
        if not is_auto:
            result.append(q)
    return result


def critical_unresolved_flags(flags: list[RequirementFlag]) -> list[RequirementFlag]:
    """Unresolved flags that still require user input."""
    return [
        f for f in flags
        if not f.resolved
        and f.severity == "CRITICAL"
        and not _is_platform_db_flag_type(f.flag_type)
    ]


def finalize_stage0b_turn(
    parsed: Stage0B_Response,
    detected_flags: list[RequirementFlag],
    configured_secrets: list[str],
) -> tuple[Stage0B_Response, list[RequirementFlag]]:
    """
    Merge LLM output, auto-resolve non-blockers, and filter questions.

    Returns updated (parsed, detected_flags) safe to drive the API / loop.
    """
    detected_flags = merge_stage0b_flag_state(detected_flags, parsed, configured_secrets)
    _auto_resolve_assumable_flags(detected_flags, configured_secrets)

    filtered_questions = _filter_blocker_questions(parsed.questions)
    critical_open = critical_unresolved_flags(detected_flags)

    # Sync resolved state back into parsed for downstream display
    resolved_from_state = [f for f in detected_flags if f.resolved]

    # Complete only when no blocker questions remain AND summary is ready (or LLM said complete)
    if not critical_open and not filtered_questions:
        is_complete = parsed.is_complete or parsed.clarified_requirements is not None
    else:
        is_complete = parsed.is_complete and not critical_open

    parsed = parsed.model_copy(
        update={
            "questions": filtered_questions,
            "resolved_flags": resolved_from_state,
            "is_complete": is_complete,
        }
    )
    return parsed, detected_flags


async def run_stage0b_loop(
    raw_prompt: str,
    configured_secret_names: list[str],
    api_key: str,
    *,
    user_input_callback,  # async callable(questions: list[MCQQuestion]) -> str
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ClarifiedRequirements:
    """
    Sub-Phase 0-B — MCQ-style clarification loop.
    """
    conversation_history: list[Turn] = []
    detected_flags: list[RequirementFlag] = []
    unresolved_flags: list[RequirementFlag] = []

    def _finalize(clarified: Optional[ClarifiedRequirements]) -> ClarifiedRequirements:
        result = ensure_clarified_requirements(
            clarified,
            detected_flags,
            configured_secret_names,
        )
        # Drop endpoint hosts the LLM invented that the user never stated
        # (scenario D: private API docs withheld).
        return strip_unattested_api_endpoints(
            result,
            _user_attested_source_text(raw_prompt, conversation_history),
        )

    for turn_number in range(1, MAX_STAGE0B_TURNS + 1):
        logger.info("Stage 0-B turn %d", turn_number)

        parsed = await run_stage0b_single_turn(
            raw_prompt=raw_prompt,
            configured_secret_names=configured_secret_names,
            api_key=api_key,
            conversation_history=conversation_history,
            unresolved_flags=unresolved_flags,
            model=model,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )

        parsed, detected_flags = finalize_stage0b_turn(
            parsed, detected_flags, configured_secret_names
        )
        unresolved_flags = critical_unresolved_flags(detected_flags)

        # Record assistant turn — include questions so the LLM knows what it asked
        conversation_history.append(
            Turn(
                role="assistant",
                content=_format_stage0b_assistant_turn(parsed.message, parsed.questions),
            )
        )

        if parsed.is_complete and parsed.clarified_requirements is not None:
            logger.info("Stage 0-B complete after %d turn(s).", turn_number)
            return _finalize(parsed.clarified_requirements)

        if not parsed.questions:
            if (
                not critical_unresolved_flags(detected_flags)
                and parsed.is_complete
                and parsed.clarified_requirements is not None
            ):
                logger.info("Stage 0-B complete after %d turn(s) (no MCQs).", turn_number)
                return _finalize(parsed.clarified_requirements)
            logger.warning(
                "Stage 0-B: no questions on turn %d (is_complete=%s, critical_open=%s)",
                turn_number,
                parsed.is_complete,
                [f.flag_type for f in critical_unresolved_flags(detected_flags)],
            )
            break

        # Present MCQs to user and collect their answer
        user_answer = await user_input_callback(parsed.questions)
        conversation_history.append(Turn(role="user", content=user_answer))

        _resolve_configure_later_flags(parsed.questions, user_answer, detected_flags)
        _resolve_delegated_flags(parsed.questions, user_answer, detected_flags)
        if project_id:
            try:
                from services.credential_service import CredentialService

                newly_saved = await _resolve_raw_value_flags(
                    parsed.questions,
                    user_answer,
                    detected_flags,
                    credential_service=CredentialService.optional(),
                    workflow_id=project_id,
                    conversation_text="\n".join(
                        t.content for t in conversation_history if t.content
                    ),
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                )
                for name in newly_saved:
                    if name not in configured_secret_names:
                        configured_secret_names.append(name)
            except Exception:
                logger.exception("Stage 0-B: raw credential value capture failed")
        unresolved_flags = critical_unresolved_flags(detected_flags)

    # Max turns reached — build a best-effort ClarifiedRequirements
    logger.warning(
        "Stage 0-B: max turns (%d) reached. Unresolved CRITICAL flags: %s",
        MAX_STAGE0B_TURNS,
        [f.flag_type for f in unresolved_flags if f.severity == "CRITICAL"],
    )
    return _finalize(None)


# ---------------------------------------------------------------------------
# ── SUB-PHASE 0-C  Configuration Guidance ───────────────────────────────────
# ---------------------------------------------------------------------------

# Built-in guidance templates — zero LLM cost for all common integrations.

GUIDANCE_TEMPLATES: dict[str, ConfigGuidance] = {
    "GEMINI_LLM": ConfigGuidance(
        dependency_type="GEMINI_LLM",
        guide_title="Gemini (Google AI) API Key",
        secret_keys=["GEMINI_API_KEY"],
        provider_url="https://aistudio.google.com/app/apikey",
        steps=[
            "Go to https://aistudio.google.com/app/apikey and sign in with your Google account.",
            "Click 'Create API Key' — copy the generated key.",
            "In this platform: Configure Secrets → Add Secret → Name: GEMINI_API_KEY → Paste value → Save.",
        ],
    ),
    "OPENAI_LLM": ConfigGuidance(
        dependency_type="OPENAI_LLM",
        guide_title="OpenAI / GPT API Key",
        secret_keys=["OPENAI_API_KEY"],
        provider_url="https://platform.openai.com/api-keys",
        steps=[
            "Go to https://platform.openai.com/api-keys and sign in.",
            "Click 'Create new secret key' — copy the key (shown only once).",
            "Configure Secrets → Add Secret → Name: OPENAI_API_KEY → Paste → Save.",
        ],
    ),
    "ANTHROPIC_LLM": ConfigGuidance(
        dependency_type="ANTHROPIC_LLM",
        guide_title="Anthropic (Claude) API Key",
        secret_keys=["ANTHROPIC_API_KEY"],
        provider_url="https://console.anthropic.com/settings/keys",
        steps=[
            "Go to https://console.anthropic.com/settings/keys and sign in.",
            "Click 'Create Key' — copy the generated API key.",
            "Configure Secrets → Add Secret → Name: ANTHROPIC_API_KEY → Paste → Save.",
        ],
    ),
    "EMAIL_GMAIL": ConfigGuidance(
        dependency_type="EMAIL_GMAIL",
        guide_title="Gmail SMTP — Email Sending",
        secret_keys=["EMAIL_SENDER", "EMAIL_PASSWORD", "SMTP_SERVER", "SMTP_PORT"],
        provider_url="https://myaccount.google.com/apppasswords",
        steps=[
            "Ensure 2-Step Verification is enabled on the Gmail account.",
            "Go to Google Account → Security → App Passwords → select 'Mail' → Generate.",
            "Copy the 16-character app password (not your normal Gmail password).",
            "Configure Secrets → Add four secrets: EMAIL_SENDER (your Gmail address), "
            "EMAIL_PASSWORD (app password), SMTP_SERVER (smtp.gmail.com), SMTP_PORT (587).",
        ],
    ),
    "EMAIL_OUTLOOK": ConfigGuidance(
        dependency_type="EMAIL_OUTLOOK",
        guide_title="Outlook / Microsoft 365 SMTP — Email Sending",
        secret_keys=["EMAIL_SENDER", "EMAIL_PASSWORD", "SMTP_SERVER", "SMTP_PORT"],
        provider_url=None,
        steps=[
            "Use your Outlook email address and account password "
            "(or app password if MFA is enabled).",
            "Configure Secrets → Add: EMAIL_SENDER (Outlook address), EMAIL_PASSWORD, "
            "SMTP_SERVER (smtp.office365.com), SMTP_PORT (587).",
        ],
    ),
    "EMAIL_PASSWORD_ONLY": ConfigGuidance(
        dependency_type="EMAIL_PASSWORD_ONLY",
        guide_title="Gmail App Password (EMAIL_PASSWORD only)",
        secret_keys=["EMAIL_PASSWORD"],
        provider_url="https://myaccount.google.com/apppasswords",
        steps=[
            "Ensure 2-Step Verification is enabled on the Gmail account.",
            "Go to Google Account → Security → App Passwords → select 'Mail' → Generate.",
            "Copy the 16-character app password.",
            "Configure Secrets → Add Secret → Name: EMAIL_PASSWORD → Paste → Save.",
        ],
    ),
    "OPENWEATHERMAP": ConfigGuidance(
        dependency_type="OPENWEATHERMAP",
        guide_title="OpenWeatherMap API Key",
        secret_keys=["WEATHER_API_KEY"],
        provider_url="https://openweathermap.org/api",
        steps=[
            "Go to https://openweathermap.org/api → click 'Sign Up' (free tier available).",
            "After sign-up, go to 'My API Keys' tab → copy the default key.",
            "Note: new keys activate within 10 minutes.",
            "Configure Secrets → Add Secret → Name: WEATHER_API_KEY → Paste → Save.",
        ],
    ),
    "WEATHERAPI": ConfigGuidance(
        dependency_type="WEATHERAPI",
        guide_title="WeatherAPI.com API Key",
        secret_keys=["WEATHERAPI_KEY"],
        provider_url="https://www.weatherapi.com",
        steps=[
            "Go to https://www.weatherapi.com → Sign up for a free account.",
            "Dashboard → copy the API Key shown on the home screen.",
            "Configure Secrets → Add Secret → Name: WEATHERAPI_KEY → Paste → Save.",
        ],
    ),
    "TWILIO_SMS": ConfigGuidance(
        dependency_type="TWILIO_SMS",
        guide_title="Twilio — SMS Sending",
        secret_keys=["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
        provider_url="https://console.twilio.com",
        steps=[
            "Go to https://www.twilio.com → Create a free account.",
            "From the Console Dashboard, copy Account SID and Auth Token.",
            "Under 'Phone Numbers' → buy or use a trial number as the sender.",
            "Configure Secrets → Add: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_FROM_NUMBER (+E.164 format, e.g. +14155552671).",
        ],
    ),
    "SLACK_WEBHOOK": ConfigGuidance(
        dependency_type="SLACK_WEBHOOK",
        guide_title="Slack — Incoming Webhook Notification",
        secret_keys=["SLACK_WEBHOOK_URL"],
        provider_url="https://api.slack.com/apps",
        steps=[
            "Go to https://api.slack.com/apps → Create a New App → 'From scratch'.",
            "Choose 'Incoming Webhooks' → toggle Activate Incoming Webhooks → On.",
            "Click 'Add New Webhook to Workspace' → select the target channel → Allow.",
            "Copy the Webhook URL (starts with https://hooks.slack.com/...).",
            "Configure Secrets → Add Secret → Name: SLACK_WEBHOOK_URL → Paste → Save.",
        ],
    ),
    "GOOGLE_MAPS": ConfigGuidance(
        dependency_type="GOOGLE_MAPS",
        guide_title="Google Maps / Geocoding API Key",
        secret_keys=["GOOGLE_MAPS_API_KEY"],
        provider_url="https://console.cloud.google.com",
        steps=[
            "Go to https://console.cloud.google.com → Enable the required API "
            "(Maps JavaScript API, Geocoding API, or Places API).",
            "Navigate to 'APIs & Services' → Credentials → Create Credentials → API Key.",
            "Copy the key and optionally restrict it to your domain or IP.",
            "Configure Secrets → Add Secret → Name: GOOGLE_MAPS_API_KEY → Paste → Save.",
        ],
    ),
    "RAZORPAY": ConfigGuidance(
        dependency_type="RAZORPAY",
        guide_title="Razorpay — Payment Gateway",
        secret_keys=["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"],
        provider_url="https://dashboard.razorpay.com",
        steps=[
            "Go to https://dashboard.razorpay.com → Settings → API Keys.",
            "Click 'Generate Test Key' (for testing) or 'Generate Live Key' (for production).",
            "Copy Key ID and Key Secret.",
            "Configure Secrets → Add: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
        ],
    ),
    "AWS_S3": ConfigGuidance(
        dependency_type="AWS_S3",
        guide_title="AWS S3 — File Storage",
        secret_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "S3_BUCKET_NAME"],
        provider_url="https://console.aws.amazon.com/iam",
        steps=[
            "Go to https://console.aws.amazon.com → IAM → Users → Create User.",
            "Attach policy 'AmazonS3FullAccess' (or a scoped bucket policy).",
            "Security credentials tab → Create Access Key → copy Access Key ID and Secret.",
            "Configure Secrets → Add: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, "
            "AWS_REGION (e.g. ap-south-1), S3_BUCKET_NAME.",
        ],
    ),
}


_STAGE0C_SYSTEM_PROMPT = """
You are a developer documentation writer.
Generate step-by-step instructions for a user to obtain API credentials for the
given integration and add them to a Configure Secrets panel.
Output JSON only. No markdown. No preamble.

Output schema:
{
  "dependency_type": "<TYPE>",
  "guide_title": "<short title>",
  "secret_keys": ["KEY_NAME_1", "KEY_NAME_2"],
  "steps": ["Step 1: ...", "Step 2: ...", ...],
  "provider_url": "<direct URL to API key page or null>"
}

Rules:
- Include exact secret key names in UPPER_SNAKE_CASE.
- Include exact provider URLs.
- Number each step clearly.
- Do not include vague steps like 'follow the documentation'.
""".strip()


async def _generate_custom_guidance(
    gap_type: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    workflow_context: Optional[str] = None,
    flag_description: Optional[str] = None,
    user_question: Optional[str] = None,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ConfigGuidance:
    """One LLM call for an unknown integration not covered by built-in templates."""
    parts = [f"Generate API credential setup guidance for: {gap_type}"]
    if flag_description:
        parts.append(f"Requirement description: {flag_description}")
    if workflow_context:
        parts.append(f"Workflow context: {workflow_context}")
    if user_question:
        parts.append(f"User's specific question: {user_question}")
    parts.append("Include exact secret key names, exact provider URLs, and numbered steps.")
    prompt = "\n".join(parts)
    raw = await _call_llm(
        system=_STAGE0C_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        model=model,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
        sub_operation="stage0c-custom-guidance",
    )
    clean = _strip_json_fences(raw)
    return ConfigGuidance.model_validate(json.loads(repair_json(clean)))



def _config_guidance_to_markdown(g: ConfigGuidance) -> str:
    lines = [f"## {g.guide_title}", ""]
    for i, step in enumerate(g.steps, 1):
        lines.append(f"{i}. {step}")
    if g.provider_url:
        lines.append("")
        lines.append(f"**Provider:** {g.provider_url}")
    return "\n".join(lines)


def _config_guidance_to_result(g: ConfigGuidance, source: Literal["template", "llm"]) -> Stage0GuidanceResult:
    doc_links = [g.provider_url] if g.provider_url else []
    return Stage0GuidanceResult(
        markdown=_config_guidance_to_markdown(g),
        doc_links=doc_links,
        credential_names=list(g.secret_keys),
        source=source,
        dependency_type=g.dependency_type,
    )


def _canonical_guidance_key(flag_type: str) -> str:
    """Map a flag_type to the best GUIDANCE_TEMPLATES key (no LLM)."""
    canonical = _normalise_gap_type(flag_type)
    ft = flag_type.upper()
    email_keys = {
        "EMAIL_GMAIL", "EMAIL_OUTLOOK", "EMAIL_PASSWORD_ONLY",
        "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_SENDER_CREDENTIALS",
        "EMAIL_CREDENTIALS", "EMAIL_CONFIG", "EMAIL_PROVIDER", "EMAIL_SMTP",
    }
    if canonical in email_keys or ft in _GAP_TYPE_ALIASES and _GAP_TYPE_ALIASES.get(ft, "").startswith("EMAIL"):
        return "EMAIL_GMAIL"
    return canonical


async def run_stage0_guidance_turn(
    flag: RequirementFlag,
    raw_prompt: str,
    api_key: str,
    *,
    user_question: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Stage0GuidanceResult:
    """
    On-demand setup guidance for one flag.
    """
    canonical = _canonical_guidance_key(flag.flag_type)
    template = GUIDANCE_TEMPLATES.get(canonical)

    if template and not user_question:
        logger.info("Stage 0 guidance: built-in template for flag '%s' -> '%s'", flag.flag_type, canonical)
        return _config_guidance_to_result(template, "template")

    if template and user_question:
        logger.info(
            "Stage 0 guidance: template + LLM for flag '%s' (user question)",
            flag.flag_type,
        )
        prompt = (
            f"Workflow: {raw_prompt}\n\n"
            f"Requirement flag: {flag.flag_type}\n"
            f"Description: {flag.description}\n\n"
            f"Built-in setup steps already known:\n{_config_guidance_to_markdown(template)}\n\n"
            f"User question: {user_question}\n\n"
            "Answer the user's question briefly, then restate the numbered setup steps if needed."
        )
        raw = await _call_llm(
            system="You are a workflow setup assistant. Respond with clear Markdown only.",
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            model=model,
            project_id=project_id,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            sub_operation="stage0c-guidance-turn",
        )
        md = _strip_json_fences(raw)
        doc_links = [template.provider_url] if template.provider_url else []
        return Stage0GuidanceResult(
            markdown=md,
            doc_links=doc_links,
            credential_names=list(template.secret_keys),
            source="llm",
            dependency_type=template.dependency_type,
        )

    logger.info("Stage 0 guidance: LLM for unknown flag '%s' (canonical '%s')", flag.flag_type, canonical)
    guidance = await _generate_custom_guidance(
        canonical,
        api_key,
        model,
        workflow_context=raw_prompt,
        flag_description=flag.description,
        user_question=user_question,
        project_id=project_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    return _config_guidance_to_result(guidance, "llm")



# ---------------------------------------------------------------------------
# Normalise LLM-invented gap names → canonical template keys
# The LLM sometimes emits free-form names like "EMAIL_SENDER", "EMAIL_SENDER_CREDENTIALS",
# "OPENWEATHERMAP", "LLM_GEMINI", etc. This mapping redirects them to built-in templates.
# ---------------------------------------------------------------------------
_GAP_TYPE_ALIASES: dict[str, str] = {
    # Email
    "EMAIL_SENDER": "EMAIL_GMAIL",
    "EMAIL_SENDER_CREDENTIALS": "EMAIL_GMAIL",
    "EMAIL_CREDENTIALS": "EMAIL_GMAIL",
    "EMAIL_PASSWORD": "EMAIL_GMAIL",
    "EMAIL_PASSWORD_GMAIL": "EMAIL_GMAIL",
    "EMAIL_SMTP": "EMAIL_GMAIL",
    "EMAIL_SMTP_GMAIL": "EMAIL_GMAIL",
    "EMAIL_CONFIG": "EMAIL_GMAIL",
    "EMAIL_PROVIDER": "EMAIL_GMAIL",
    "EMAIL_SENDER_PASSWORD": "EMAIL_GMAIL",
    "GMAIL": "EMAIL_GMAIL",
    "GMAIL_SMTP": "EMAIL_GMAIL",
    "SMTP_EMAIL": "EMAIL_GMAIL",
    "SMTP_CREDENTIALS": "EMAIL_GMAIL",
    "EMAIL_OUTLOOK": "EMAIL_OUTLOOK",
    "OUTLOOK_SMTP": "EMAIL_OUTLOOK",
    # LLM
    "GEMINI_LLM": "GEMINI_LLM",
    "LLM_GEMINI": "GEMINI_LLM",
    "LLM_PROVIDER_GEMINI": "GEMINI_LLM",
    "GEMINI": "GEMINI_LLM",
    "GEMINI_API": "GEMINI_LLM",
    "OPENAI": "OPENAI_LLM",
    "OPENAI_API": "OPENAI_LLM",
    "LLM_OPENAI": "OPENAI_LLM",
    "GPT": "OPENAI_LLM",
    "ANTHROPIC": "ANTHROPIC_LLM",
    "CLAUDE": "ANTHROPIC_LLM",
    # Weather
    "OPENWEATHERMAP": "OPENWEATHERMAP",
    "OPEN_WEATHER_MAP": "OPENWEATHERMAP",
    "WEATHER_API": "OPENWEATHERMAP",
    "WEATHER_API_KEY": "OPENWEATHERMAP",
    "WEATHERAPI": "WEATHERAPI",
    # Other
    "TWILIO": "TWILIO_SMS",
    "SLACK": "SLACK_WEBHOOK",
    "GOOGLE_MAPS": "GOOGLE_MAPS",
    "RAZORPAY": "RAZORPAY",
    "AWS_S3": "AWS_S3",
    "S3": "AWS_S3",
}


def _normalise_gap_type(gap_type: str) -> str:
    """Return the canonical template key for an LLM-invented gap name."""
    key = gap_type.upper().strip()
    return _GAP_TYPE_ALIASES.get(key, gap_type)


def _deduplicate_guidance(guidance_list: list[ConfigGuidance]) -> list[ConfigGuidance]:
    """Remove duplicate guidance items (same dependency_type)."""
    seen: set[str] = set()
    out: list[ConfigGuidance] = []
    for g in guidance_list:
        if g.dependency_type not in seen:
            seen.add(g.dependency_type)
            out.append(g)
    return out


_LLM_FROM_CANONICAL: dict[str, tuple[str, str, str]] = {
    "GEMINI_LLM": ("google_gemini", "gemini-3-flash-preview", "GEMINI_API_KEY"),
    "OPENAI_LLM": ("openai", "gpt-4o", "OPENAI_API_KEY"),
    "ANTHROPIC_LLM": ("anthropic", "claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
}

_API_FROM_CANONICAL: dict[str, tuple[str, str]] = {
    "OPENWEATHERMAP": ("openweathermap", "weather_by_city"),
    "WEATHERAPI": ("weatherapi", "weather_by_city"),
    "GOOGLE_MAPS": ("google_maps", "geocoding_or_maps"),
    "RAZORPAY": ("razorpay", "payments"),
    "AWS_S3": ("aws_s3", "object_storage"),
    "SLACK_WEBHOOK": ("slack", "notifications"),
    "TWILIO_SMS": ("twilio", "sms"),
}


def _flag_blob(flag: RequirementFlag) -> str:
    return f"{flag.flag_type} {flag.description} {flag.resolved_value or ''}".upper()


def _selected_input_excludes_email(flags: list[RequirementFlag]) -> bool:
    """True when Stage 0 chose a non-email input path (e.g. PDF upload only)."""
    for flag in flags:
        if not flag.resolved:
            continue
        ft = flag.flag_type.upper()
        if ft != "INPUT_SOURCE" and not ft.startswith("INPUT_SOURCE_") and "INPUT" not in ft:
            continue
        rv = (flag.resolved_value or flag.description or "").lower()
        if any(
            k in rv
            for k in ("email", "imap", "inbox", "mailbox", "shared folder", "drive", "google drive")
        ):
            return False
        if any(k in rv for k in ("pdf", "upload", "file upload", "manual", "direct upload")):
            return True
    return False


def _infer_canonical_from_flag(flag: RequirementFlag) -> Optional[str]:
    """Map a flag to a GUIDANCE_TEMPLATES key when possible."""
    canonical = _canonical_guidance_key(flag.flag_type)
    if canonical in GUIDANCE_TEMPLATES:
        return canonical
    blob = _flag_blob(flag)
    for key in GUIDANCE_TEMPLATES:
        if key.replace("_", "") in blob.replace("_", ""):
            return key
    return None


def synthesize_clarified_from_flags(
    flags: list[RequirementFlag],
    configured_secrets: list[str],
) -> ClarifiedRequirements:
    """
    Build structured clarified requirements from detected flags when the LLM
    omits or partially fills the completion payload.
    """
    del configured_secrets  # reserved; credential gaps are tracked via flags + summary

    llm: Optional[ClarifiedLLM] = None
    external_apis: list[ClarifiedExternalAPI] = []
    email: Optional[ClarifiedEmail] = None
    requirements: list[str] = []
    seen_api: set[str] = set()

    for flag in flags:
        canonical = _infer_canonical_from_flag(flag)
        if not canonical:
            continue
        blob = _flag_blob(flag)

        if canonical in _LLM_FROM_CANONICAL and llm is None:
            provider, model, secret = _LLM_FROM_CANONICAL[canonical]
            llm = ClarifiedLLM(provider=provider, model=model, secret_name=secret)
            if flag.resolved_value and "gemini" in flag.resolved_value.lower():
                llm = ClarifiedLLM(
                    provider="google_gemini",
                    model=flag.resolved_value.strip(),
                    secret_name=secret,
                )

        if canonical in _API_FROM_CANONICAL:
            provider, purpose = _API_FROM_CANONICAL[canonical]
            if provider not in seen_api:
                seen_api.add(provider)
                external_apis.append(ClarifiedExternalAPI(provider=provider, purpose=purpose))

        if canonical.startswith("EMAIL") and not _selected_input_excludes_email(flags):
            if email is None:
                email = ClarifiedEmail(
                    secret_names=["EMAIL_SENDER", "EMAIL_PASSWORD"],
                    smtp="outlook" if "OUTLOOK" in canonical else "gmail",
                )

        if flag.resolved and flag.resolved_value and flag.severity == "CRITICAL":
            desc = (flag.resolved_value or flag.description).strip()
            if desc and desc not in requirements:
                requirements.append(desc)

    if llm:
        requirements.insert(0, f"Use {llm.model} ({llm.provider}) for LLM steps")
    for api in external_apis:
        step = f"Call {api.provider} API ({api.purpose})"
        if step not in requirements:
            requirements.append(step)
    if email:
        requirements.append(f"Send email via {email.smtp} SMTP")

    return ClarifiedRequirements(
        llm=llm,
        external_apis=external_apis,
        email=email,
        requirements_analyzed=requirements,
    )


def merge_clarified_requirements(
    primary: ClarifiedRequirements,
    fallback: ClarifiedRequirements,
) -> ClarifiedRequirements:
    """Fill empty primary fields from flag-based fallback."""
    llm = primary.llm
    if llm is None or not (llm.provider and llm.model):
        llm = fallback.llm

    external_apis = list(primary.external_apis)
    if not external_apis:
        external_apis = list(fallback.external_apis)
    else:
        seen = {a.provider.lower() for a in external_apis if a.provider}
        for api in fallback.external_apis:
            if api.provider.lower() not in seen:
                external_apis.append(api)
                seen.add(api.provider.lower())

    email = primary.email or fallback.email
    db_schemas = list(primary.db_schemas) or list(fallback.db_schemas)
    requirements = list(primary.requirements_analyzed) or list(fallback.requirements_analyzed)

    return ClarifiedRequirements(
        llm=llm,
        external_apis=external_apis,
        email=email,
        db_schemas=db_schemas,
        requirements_analyzed=requirements,
    )


def ensure_clarified_requirements(
    clarified: Optional[ClarifiedRequirements],
    flags: list[RequirementFlag],
    configured_secrets: list[str],
) -> ClarifiedRequirements:
    """Normalize LLM output and merge with flag-based synthesis."""
    fallback = synthesize_clarified_from_flags(flags, configured_secrets)
    if clarified is None:
        result = fallback
    else:
        result = merge_clarified_requirements(clarified, fallback)
    if _selected_input_excludes_email(flags):
        result = result.model_copy(update={"email": None})
    return result


def _endpoint_attestation_tokens(endpoint_url: str) -> list[str]:
    """Host / URL tokens that must appear in user-provided text to keep endpoint_url."""
    raw = (endpoint_url or "").strip().rstrip(".,;)")
    if not raw:
        return []
    tokens: list[str] = [raw.lower()]
    # Strip scheme for host match
    no_scheme = re.sub(r"^https?://", "", raw, flags=re.I)
    hostport = no_scheme.split("/")[0].lower()
    if hostport:
        tokens.append(hostport)
        if ":" in hostport:
            tokens.append(hostport.split(":")[0])
    return [t for t in tokens if len(t) >= 4]


def endpoint_url_attested_in_text(endpoint_url: str, source_text: str) -> bool:
    """True when the endpoint host/URL/DSN appears in user-attested text."""
    blob = (source_text or "").lower()
    if not blob.strip() or not (endpoint_url or "").strip():
        return False
    for token in _endpoint_attestation_tokens(endpoint_url):
        if token in blob:
            return True
    return False


def strip_unattested_api_endpoints(
    clarified: ClarifiedRequirements,
    *source_texts: str,
) -> ClarifiedRequirements:
    """
    Remove endpoint_url (and private request/response schemas) that the user never stated.

    Prevents Stage 0 from inventing hosts like api.internal-crm.com when the user
    withheld private API docs. Only hosts/URLs/DSNs present in the raw prompt or
    user answers are kept.
    """
    source = "\n".join(t for t in source_texts if t and str(t).strip())
    if not clarified.external_apis:
        return clarified

    cleaned: list[ClarifiedExternalAPI] = []
    changed = False
    for api in clarified.external_apis:
        ep = (api.endpoint_url or "").strip()
        if not ep:
            cleaned.append(api)
            continue
        if endpoint_url_attested_in_text(ep, source):
            cleaned.append(api)
            continue

        logger.warning(
            "Stage 0: stripping unattested endpoint_url %r for provider=%r "
            "(not found in user prompt/answers — refusing invented host)",
            ep,
            api.provider,
        )
        changed = True
        updates: dict[str, Any] = {"endpoint_url": ""}
        # Invented private schemas usually ride along with invented hosts.
        if api.is_public is False:
            req = (api.request_schema or "").strip()
            resp = (api.response_schema or "").strip()
            if req and req.lower() not in source.lower():
                updates["request_schema"] = ""
            if resp and resp.lower() not in source.lower():
                updates["response_schema"] = ""
            # Method alone is too common to attest; clear when host was invented.
            if (api.http_method or "").strip():
                updates["http_method"] = ""
        cleaned.append(api.model_copy(update=updates))

    if not changed:
        return clarified
    return clarified.model_copy(update={"external_apis": cleaned})


def _user_attested_source_text(
    raw_prompt: str,
    conversation_history: list[Turn],
) -> str:
    parts = [raw_prompt or ""]
    for turn in conversation_history or []:
        if getattr(turn, "role", None) == "user" and getattr(turn, "content", None):
            parts.append(str(turn.content))
    return "\n".join(parts)


def build_planner_handoff_text(clarified: ClarifiedRequirements) -> str:
    """Authoritative scratch-planner context from structured Stage 0 completion."""
    lines: list[str] = []

    if clarified.llm and (clarified.llm.model or clarified.llm.provider):
        secret = clarified.llm.secret_name or "(configured)"
        lines.append(
            f"LLM: {clarified.llm.provider} / {clarified.llm.model} "
            f"(secret: {secret})"
        )

    for api in clarified.external_apis:
        if not api.provider and not api.endpoint_url:
            continue
        purpose = f" — {api.purpose}" if api.purpose else ""
        provider = api.provider or "external"
        lines.append(f"External API: {provider}{purpose}")
        if api.endpoint_url:
            lines.append(f"  endpoint: {api.endpoint_url}")
        if api.http_method:
            lines.append(f"  method: {api.http_method}")
        if getattr(api, "auth_header", None):
            lines.append(f"  auth: {api.auth_header}")
        if api.request_schema:
            lines.append(f"  request: {api.request_schema}")
        if api.response_schema:
            lines.append(f"  response: {api.response_schema}")
        if api.is_public is False:
            lines.append("  is_public: false")

    if clarified.email and clarified.email.secret_names:
        names = ", ".join(clarified.email.secret_names)
        lines.append(f"Email: SMTP {clarified.email.smtp or 'gmail'} ({names})")

    for schema in clarified.db_schemas:
        field_bits = [
            f"{f.get('name', '?')}:{f.get('type', 'string')}"
            for f in (schema.fields or [])
        ]
        fields_txt = ", ".join(field_bits) if field_bits else "(fields TBD)"
        existing_txt = ""
        if getattr(schema, "existing", False):
            bits = [f"EXISTING data, store={schema.target_store or 'mongodb'}"]
            if schema.labels:
                bits.append("labels=" + "/".join(schema.labels))
            if schema.relationships:
                bits.append("relationships=" + "/".join(schema.relationships))
            existing_txt = " [" + "; ".join(bits) + "; reuse this exact name and store]"
        elif getattr(schema, "user_decision", None) == "new":
            existing_txt = (
                " [NEW collection: the user chose to create a new one; use this exact name and do not "
                "reuse or merge with similar existing collections]"
            )
        lines.append(f"DB `{schema.collection_name}`{existing_txt}: {fields_txt}")

    steps = [s.strip() for s in clarified.requirements_analyzed if s and s.strip()]
    if steps:
        lines.append("Workflow steps:")
        lines.extend(f"  - {step}" for step in steps)

    if not lines:
        return ""

    body = "\n".join(lines)
    return (
        "STAGE 0 CLARIFIED REQUIREMENTS (authoritative — do not contradict):\n"
        f"{body}"
    )


def _llm_providers_from_clarified(clarified: ClarifiedRequirements) -> list[str]:
    if clarified.llm and clarified.llm.model:
        return [clarified.llm.model]
    return []


def _integrations_from_clarified(clarified: ClarifiedRequirements) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for api in clarified.external_apis:
        if api.provider and api.provider.lower() not in seen:
            seen.add(api.provider.lower())
            out.append(api.provider)
    if clarified.email and "email" not in seen:
        out.append("email")
    return out


def _gap_types_from_clarified(clarified: ClarifiedRequirements) -> list[str]:
    """Infer guidance template keys from structured completion (batch 0-C)."""
    gaps: list[str] = []
    if clarified.llm and clarified.llm.secret_name:
        prov = clarified.llm.provider.lower()
        if "openai" in prov or "gpt" in clarified.llm.model.lower():
            gaps.append("OPENAI_LLM")
        elif "anthropic" in prov or "claude" in clarified.llm.model.lower():
            gaps.append("ANTHROPIC_LLM")
        else:
            gaps.append("GEMINI_LLM")
    for api in clarified.external_apis:
        gaps.append(_normalise_gap_type(api.provider.upper()))
    if clarified.email:
        gaps.append("EMAIL_GMAIL" if clarified.email.smtp != "outlook" else "EMAIL_OUTLOOK")
    return gaps


def _required_secrets_from_clarified(
    clarified: ClarifiedRequirements,
    cred_result: CredentialCheckResult,
) -> list[str]:
    """Secret names still needed, from structured fields + guidance templates.

    Uses semantic alias matching (GEMINI_API_KEY ≈ gemini_key). Secrets the user
    deferred with configure_later still appear here so guidance UIs can list them,
    but Stage 0 completion is NOT blocked on them.
    """
    configured = list(cred_result.configured_secret_names or [])
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: str) -> None:
        key = (name or "").strip()
        if not key:
            return
        if secret_is_configured(key, configured):
            return
        norm = _normalize_secret_key(key)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(key)

    if clarified.llm and clarified.llm.secret_name:
        _add(clarified.llm.secret_name)
    if clarified.email:
        for name in clarified.email.secret_names:
            _add(name)
    for gap in _gap_types_from_clarified(clarified):
        for key in _required_secret_names_from_gaps([gap]):
            _add(key)
    return out


async def run_stage0c(
    gap_types: list[str],
    configured_secret_names: list[str],
    api_key: str,
    model: str = DEFAULT_MODEL,
    project_id: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> list[ConfigGuidance]:
    """
    Sub-Phase 0-C — Configuration Guidance (batch path).

    Generates guidance for each unresolved gap type.
    Uses built-in templates wherever possible (zero LLM cost).
    """
    guidance_list: list[ConfigGuidance] = []
    has_email_gap = any(
        _normalise_gap_type(g) in {
            "EMAIL_GMAIL", "EMAIL_OUTLOOK", "EMAIL_PASSWORD_ONLY",
            "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_SENDER_CREDENTIALS",
            "EMAIL_CREDENTIALS", "EMAIL_CONFIG", "EMAIL_PROVIDER",
        }
        for g in gap_types
    )
    email_already_ok = (
        secret_is_configured("EMAIL_SENDER", configured_secret_names)
        and secret_is_configured("EMAIL_PASSWORD", configured_secret_names)
    )

    seen_canonical: set[str] = set()

    for gap_type in gap_types:
        canonical = _normalise_gap_type(gap_type)

        # Skip if already covered by a seen canonical type
        if canonical in seen_canonical:
            logger.info("Stage 0-C: skipping duplicate gap '%s' (canonical '%s')", gap_type, canonical)
            continue

        # For any email gap, emit a single Gmail template unless creds are configured
        if canonical in {
            "EMAIL_GMAIL", "EMAIL_OUTLOOK", "EMAIL_PASSWORD_ONLY",
        } or (has_email_gap and gap_type.upper() in _GAP_TYPE_ALIASES):
            if email_already_ok:
                logger.info("Stage 0-C: email creds already configured, skipping '%s'", gap_type)
                seen_canonical.add("EMAIL_GMAIL")
                continue
            canonical = "EMAIL_GMAIL"   # always default to Gmail
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)
            template = GUIDANCE_TEMPLATES["EMAIL_GMAIL"]
            logger.info("Stage 0-C: using built-in Gmail template for '%s'", gap_type)
            guidance_list.append(template)
            continue

        seen_canonical.add(canonical)
        template = GUIDANCE_TEMPLATES.get(canonical)
        if template:
            logger.info("Stage 0-C: using built-in template for '%s' (canonical '%s')", gap_type, canonical)
            guidance_list.append(template)
        else:
            logger.info("Stage 0-C: no built-in template for '%s' — calling LLM", canonical)
            guidance = await _generate_custom_guidance(
                canonical, 
                api_key=api_key, 
                model=model,
                project_id=project_id,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id
            )
            guidance_list.append(guidance)

    return _deduplicate_guidance(guidance_list)


# ---------------------------------------------------------------------------
# ── Build RequirementSummary ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _required_secret_names_from_gaps(gaps: list[str]) -> list[str]:
    """Infer secret key names from gap types via templates only (no LLM, no markdown)."""
    seen: set[str] = set()
    out: list[str] = []
    for gap in gaps:
        canonical = _canonical_guidance_key(gap)
        tmpl = GUIDANCE_TEMPLATES.get(canonical)
        if not tmpl:
            continue
        for key in tmpl.secret_keys:
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def build_requirement_summary(
    clarified: ClarifiedRequirements,
    guidance: list[ConfigGuidance],
    cred_result: CredentialCheckResult,
) -> RequirementSummary:
    """
    Assembles the final RequirementSummary passed to Stage A.

    ``guidance`` may be empty when QnA completes; setup help is fetched on demand
    via run_stage0_guidance_turn / POST /stage0/guidance.
    """
    logical_steps = [
        LogicalStep(step_number=i + 1, description=step)
        for i, step in enumerate(clarified.requirements_analyzed)
        if step and step.strip()
    ]

    seen_secrets: set[str] = set()
    required_secrets: list[str] = []
    if guidance:
        for g in guidance:
            for key in g.secret_keys:
                if key not in seen_secrets:
                    seen_secrets.add(key)
                    required_secrets.append(key)
    else:
        required_secrets = _required_secrets_from_clarified(clarified, cred_result)

    integrations = _integrations_from_clarified(clarified)
    if guidance and clarified.email is None:
        has_email_guidance = any(
            "email" in g.dependency_type.lower() or "smtp" in g.dependency_type.lower()
            for g in guidance
        )
        if has_email_guidance and "email" not in {i.lower() for i in integrations}:
            integrations.append("email")

    unresolved_warns: list[str] = []  # populated by caller if WARN flags deferred
    planner_handoff = build_planner_handoff_text(clarified)

    return RequirementSummary(
        phase_name="Stage 0",
        phase_goal="Validate credentials and clarify requirements before workflow planning.",
        logical_steps=logical_steps,
        llm_providers=_llm_providers_from_clarified(clarified),
        integrations=integrations,
        db_schemas=clarified.db_schemas,
        required_secrets=required_secrets,
        already_configured=cred_result.configured_secret_names,
        config_guidance=guidance,
        unresolved_warns=unresolved_warns,
        planner_handoff=planner_handoff,
    )


def format_stage0_planner_context(summary: Any) -> str:
    """
    Scratch-planner context from Stage 0.

    Prefers ``planner_handoff`` (structured clarified requirements). Falls back to
    ``logical_steps`` descriptions when handoff text is empty.
    """
    if summary is None:
        return ""

    if hasattr(summary, "model_dump"):
        s: dict[str, Any] = summary.model_dump()
    elif isinstance(summary, dict):
        s = summary
    else:
        return ""

    handoff = _any_to_str(s.get("planner_handoff")) or ""
    if handoff.strip():
        return handoff.strip()

    raw_steps = s.get("logical_steps") or []
    lines: list[str] = []
    for item in raw_steps:
        if isinstance(item, dict):
            desc = _any_to_str(item.get("description")) or ""
        else:
            desc = _any_to_str(getattr(item, "description", None)) or ""
        desc = desc.strip()
        if desc:
            lines.append(desc)

    if not lines:
        return ""

    body = "\n".join(f"  - {line}" for line in lines)
    return (
        "STAGE 0 CLARIFIED REQUIREMENTS (authoritative — do not contradict):\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# ── Top-level Stage 0 Orchestrator ───────────────────────────────────────────
# ---------------------------------------------------------------------------

async def run_stage0(
    workflow_id: str,
    raw_prompt: str,
    db: Any = None,
    api_key: str = "",
    user_input_callback=None,
    model: str = DEFAULT_MODEL,
    *,
    credential_service: Any = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> tuple[RequirementSummary, Stage0State]:
    """
    Orchestrates all three sub-phases of Stage 0.

    Parameters
    ----------
    workflow_id : str
    raw_prompt : str
        The user's original, unstructured workflow description.
    db : Any
        Database client with a ``credentials`` collection supporting
        ``find_one({'workflow_id': workflow_id})``.
    api_key : str
        Anthropic API key.
    user_input_callback : async callable
        Receives list[MCQQuestion], returns str (user's answer).
    model : str

    Returns
    -------
    (RequirementSummary, Stage0State)
        RequirementSummary is the structured handoff to Stage A.
        Stage0State contains the full audit trail.
    """
    state = Stage0State(workflow_id=workflow_id, raw_prompt=raw_prompt)

    # ── 0-A  Credential Check ─────────────────────────────────────────────
    logger.info("Stage 0-A: fetching configured secrets for workflow_id=%s", workflow_id)
    cred_result = await fetch_configured_secrets(
        workflow_id,
        db,
        credential_service=credential_service,
        org_id=org_id,
        subscriber_id=subscriber_id,
    )
    state.credential_check = cred_result
    logger.info(
        "Stage 0-A complete. Configured secrets: %s",
        cred_result.configured_secret_names,
    )

    # ── 0-B  Requirement Clarification Loop ───────────────────────────────
    logger.info("Stage 0-B: starting clarification loop")
    clarified = await run_stage0b_loop(
        raw_prompt=raw_prompt,
        configured_secret_names=cred_result.configured_secret_names,
        api_key=api_key,
        user_input_callback=user_input_callback,
        model=model,
        project_id=workflow_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    state.clarified_requirements = clarified
    state.stage0b_complete = True
    logger.info(
        "Stage 0-B complete. %d requirement step(s), planner handoff length=%d",
        len(clarified.requirements_analyzed),
        len(build_planner_handoff_text(clarified)),
    )

    # ── Build RequirementSummary for Stage A (guidance is on-demand, not batch 0-C) ──
    req_summary = build_requirement_summary(clarified, [], cred_result)
    state.requirement_summary = req_summary

    logger.info(
        "Stage 0 complete. %d logical step(s) handed off to Stage A.",
        len(req_summary.logical_steps),
    )
    return req_summary, state


# ---------------------------------------------------------------------------
# Usage example (not executed on import)
# ---------------------------------------------------------------------------

async def _example_usage() -> None:
    """
    Shows how Stage 0 plugs into _plan_phase_graph_with_llm().
    Replace the stubs with your real db client and API key.
    """
    import os

    class _FakeDB:
        """Minimal stub for demonstration."""
        class credentials:
            @staticmethod
            async def find_one(query: dict) -> dict | None:
                # Simulate partial email config already present
                return {
                    "workflow_id": query["workflow_id"],
                    "secrets": {
                        "EMAIL_SENDER": "...",
                        "SMTP_SERVER": "...",
                        "SMTP_PORT": "...",
                    },
                }

    async def _demo_callback(questions: list[MCQQuestion]) -> str:
        """Auto-select option A for demo purposes."""
        for q in questions:
            print(f"\n[MCQ] {q.question_text}")
            for i, opt in enumerate(q.options):
                print(f"  {chr(65+i)}. {opt}")
        return questions[0].options[0] if questions else "A"

    raw_prompt = (
        "Build a workflow that takes a city name, gets weather using Gemini to "
        "summarise it, saves to weather_logs, and emails the result to the user."
    )

    req_summary, state = await run_stage0(
        workflow_id="wf_demo_001",
        raw_prompt=raw_prompt,
        db=_FakeDB(),
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        user_input_callback=_demo_callback,
    )

    print("\n=== RequirementSummary (Stage A input) ===")
    print(req_summary.model_dump_json(indent=2))


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_example_usage())
