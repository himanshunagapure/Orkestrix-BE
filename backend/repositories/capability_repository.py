from typing import Any, Dict, List, Optional
from datetime import datetime
import json
import os
import re

from bson import ObjectId

try:
    # Optional: use NLTK PorterStemmer for better normalization if available.
    from nltk.stem import PorterStemmer  # type: ignore

    _PORTER_STEMMER = PorterStemmer()
except Exception:  # pragma: no cover - optional dependency
    _PORTER_STEMMER = None

from services.database import Database

# ── Platform tenant (read once from config.json) ─────────────────────────────
# All globally-available capabilities are stored under these IDs.
# Never use all-null tenant for new writes; use these values instead.
def _load_platform_tenant() -> Dict[str, Optional[str]]:
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        with open(cfg_path, "r") as _f:
            _cfg = json.load(_f)
        return {
            "orgId": _cfg.get("aiqod_orgId") or None,
            "subscriberId": _cfg.get("aiqod_subscriberId") or None,
            "userId": None,
        }
    except Exception:
        return {"orgId": None, "subscriberId": None, "userId": None}

_PLATFORM_TENANT: Dict[str, Optional[str]] = _load_platform_tenant()


def _to_obj_id(value: Any) -> Optional[ObjectId]:
    """Convert a string / ObjectId / None to ObjectId; return None on failure."""
    if value is None:
        return None
    try:
        return ObjectId(value) if isinstance(value, str) else value
    except Exception:
        return None


def dynamic_tool_class_path(tool_key: str) -> str:
    """Stable class_path for gap/user-authored tools (not a Python import path)."""
    key = (tool_key or "").strip()
    if key.startswith("tool:"):
        key = key[5:].strip()
    return f"dynamic:{key}" if key else ""


def extract_tool_runtime_metadata(
    doc: Optional[Dict[str, Any]],
    *,
    capability_id: str = "",
) -> Dict[str, Optional[str]]:
    """Pull source / class_path / version / wrappers from a capability document.

    Gap-generated tools store class_path on the doc (or as ``dynamic:<toolKey>``).
    LangChain tools use a real import path on ``class_path`` / ``source.sourceRef``.
    When the doc is missing, tool:* ids still get a synthesized ``dynamic:`` path so
    graph nodes never persist ``class_path: null``.
    """
    source_type: Optional[str] = None
    class_path: Optional[str] = None
    wrapper_class: Optional[str] = None
    wrapper_kwarg: Optional[str] = None
    version: Optional[str] = None

    if doc:
        raw_version = doc.get("version")
        if raw_version is not None and str(raw_version).strip():
            version = str(raw_version).strip()
        source_raw = doc.get("source")
        if isinstance(source_raw, str):
            source_type = source_raw
        elif isinstance(source_raw, dict):
            source_type = source_raw.get("sourceType") or source_raw.get("source_type")
            src_ref = source_raw.get("sourceRef") or {}
            if isinstance(src_ref, dict):
                class_path = src_ref.get("class_path") or src_ref.get("classPath")
            wrapper_class = source_raw.get("wrapper_class") or source_raw.get("wrapperClass")
            wrapper_kwarg = source_raw.get("wrapper_kwarg") or source_raw.get("wrapperKwarg")
        class_path = doc.get("class_path") or class_path
        wrapper_class = doc.get("wrapper_class") or wrapper_class
        wrapper_kwarg = doc.get("wrapper_kwarg") or wrapper_kwarg
        if not class_path:
            runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
            tool_meta = runtime.get("tool") if isinstance(runtime.get("tool"), dict) else {}
            tool_name = tool_meta.get("toolName") or doc.get("name")
            src = (source_type or "").strip()
            if src in ("gap_tool_llm", "user_authored"):
                class_path = dynamic_tool_class_path(str(tool_name or capability_id))

    if not class_path and (capability_id or "").startswith("tool:"):
        class_path = dynamic_tool_class_path(capability_id)
    if not version:
        version = "1"
    if not source_type and class_path and str(class_path).startswith("dynamic:"):
        source_type = "gap_tool_llm"

    return {
        "source": source_type,
        "class_path": class_path,
        "version": version,
        "wrapper_class": wrapper_class,
        "wrapper_kwarg": wrapper_kwarg,
    }


def merge_tool_capability_ref_metadata(
    *,
    capability_id: str,
    doc: Optional[Dict[str, Any]],
    existing_source: Optional[str] = None,
    existing_class_path: Optional[str] = None,
    existing_version: Optional[str] = None,
    existing_wrapper_class: Optional[str] = None,
    existing_wrapper_kwarg: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Merge registry metadata onto a graph capabilityRef (Issue 12).

    - Lookup hit: registry wins for source/class_path/wrappers; version fills if unset.
    - Lookup miss: never overwrite a non-null existing class_path (protects langchain
      import paths). Synthesize ``dynamic:<toolKey>`` only when class_path is empty.
    """
    meta = extract_tool_runtime_metadata(doc, capability_id=capability_id)
    source = existing_source
    class_path = existing_class_path
    version = existing_version
    wrapper_class = existing_wrapper_class
    wrapper_kwarg = existing_wrapper_kwarg

    if meta.get("source") and (doc is not None or not source):
        source = meta["source"]
    if doc is not None and meta.get("class_path"):
        class_path = meta["class_path"]
    elif not (class_path or "").strip():
        synthesized = meta.get("class_path") or dynamic_tool_class_path(capability_id)
        if synthesized:
            class_path = synthesized
            source = source or "gap_tool_llm"
    if meta.get("version") and not version:
        version = meta["version"]
    elif not version:
        version = "1"
    if meta.get("wrapper_class"):
        wrapper_class = meta["wrapper_class"]
    if meta.get("wrapper_kwarg"):
        wrapper_kwarg = meta["wrapper_kwarg"]

    return {
        "source": source,
        "class_path": class_path,
        "version": version,
        "wrapper_class": wrapper_class,
        "wrapper_kwarg": wrapper_kwarg,
    }


class CapabilityRepository:
    """
    Repository for the capability registry.

    The underlying MongoDB collection is `capability_repository` with:
    - One document per capability *version* (append-only, versioned).
    - Tenant scoping by orgId, subscriberId, and optionally userId.

    Canonical shape (see docs/CAPABILITY_REPOSITORY_TECHNICAL_DESIGN.md):
      {
        orgId, subscriberId, userId,
        capabilityId, kind,
        name, description,
        inputParameters[], outputParameters[],
        version, isLatest, status,
        source: { sourceType, sourceRef, sourceUpdatedAt, syncedAt },
        runtime: { ui | tool | agent },
        searchText, tags[],
        createdAt, updatedAt
      }
    """

    def __init__(self) -> None:
        self.collection_name = "capability_repository"
        self.collection = None

    def get_collection(self):
        """Get or initialize the underlying collection."""
        if self.collection is None:
            self.collection = Database.get_collection(self.collection_name)
        return self.collection

    async def create_indexes(self) -> None:
        """
        Create collection indexes as per the technical design:

        - Unique per capability version per tenant:
            { orgId, subscriberId, userId, capabilityId, version }
        - Fast latest lookup:
            { orgId, subscriberId, userId, kind, isLatest, name }
        - Optional text search over main text fields.
        """
        collection = self.get_collection()
        try:
            # Unique per capability version per tenant (orgId, subscriberId, userId)
            await collection.create_index(
                [
                    ("orgId", 1),
                    ("subscriberId", 1),
                    ("userId", 1),
                    ("capabilityId", 1),
                    ("version", 1),
                ],
                unique=True,
                name="tenant_capability_version_unique",
            )

            # Latest lookup for planners (filter by kind + isLatest + name)
            await collection.create_index(
                [
                    ("orgId", 1),
                    ("subscriberId", 1),
                    ("userId", 1),
                    ("kind", 1),
                    ("isLatest", 1),
                    ("name", 1),
                ],
                background=True,
                name="tenant_kind_latest_name",
            )

            # Text search across name / description / tags / searchText
            try:
                await collection.create_index(
                    [
                        ("name", "text"),
                        ("description", "text"),
                        ("searchText", "text"),
                        ("tags", "text"),
                    ],
                    name="capability_text_search",
                )
            except Exception:
                # Text index may fail if unsupported in some environments; non-fatal.
                pass
        except Exception as e:
            print(f"Error creating capability_repository indexes: {e}")

    async def upsert_capability(
        self,
        tenant: Dict[str, Optional[str]],
        capability: Dict[str, Any],
    ) -> None:
        """
        Generic upsert helper for a single capability version.

        This does NOT implement adapters; it assumes `capability` already
        follows the canonical schema (including capabilityId, kind, version).
        """
        collection = self.get_collection()

        org_id = tenant.get("orgId")
        subscriber_id = tenant.get("subscriberId")
        user_id = tenant.get("userId")

        org_obj = None
        sub_obj = None
        user_obj = None
        if org_id:
            try:
                org_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                pass
        if subscriber_id:
            try:
                sub_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                pass
        if user_id:
            try:
                user_obj = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception:
                pass

        now = datetime.utcnow()

        capability_doc: Dict[str, Any] = {
            **capability,
            "orgId": org_obj,
            "subscriberId": sub_obj,
            "userId": user_obj,
            "updatedAt": now,
        }

        # Ensure createdAt is set on first insert
        filter_query: Dict[str, Any] = {
            "orgId": org_obj,
            "subscriberId": sub_obj,
            "userId": user_obj,
            "capabilityId": capability.get("capabilityId"),
            "version": capability.get("version"),
        }

        await collection.update_one(
            filter_query,
            {
                "$setOnInsert": {"createdAt": now},
                "$set": capability_doc,
            },
            upsert=True,
        )

    async def set_is_latest_for_capability(
        self,
        tenant: Dict[str, Optional[str]],
        capability_id: str,
    ) -> None:
        """
        Set isLatest=True for the newest version of this capability (by version/updatedAt)
        and isLatest=False for all other versions of the same capabilityId.
        """
        collection = self.get_collection()
        org_id = tenant.get("orgId")
        subscriber_id = tenant.get("subscriberId")
        user_id = tenant.get("userId")
        org_obj = None
        sub_obj = None
        user_obj = None
        if org_id is not None:
            try:
                org_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                pass
        if subscriber_id is not None:
            try:
                sub_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                pass
        if user_id is not None:
            try:
                user_obj = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception:
                pass

        # Build tenant filter. userId may be None in some docs (e.g. system-generated
        # capabilities) — we must match BOTH docs that have userId set to the tenant's
        # userId AND docs that have userId=None, otherwise isLatest flags get stuck.
        tenant_filter: Dict[str, Any] = {"capabilityId": capability_id}
        tenant_filter["orgId"] = org_obj
        tenant_filter["subscriberId"] = sub_obj
        if user_obj is not None:
            tenant_filter["userId"] = {"$in": [user_obj, None]}
        else:
            # Tenant has no userId — match docs that also have no userId
            tenant_filter["userId"] = None

        # Find the latest by version (desc) then updatedAt (desc)
        latest = await collection.find_one(
            tenant_filter,
            sort=[("version", -1), ("updatedAt", -1)],
            projection={"_id": 1},
        )
        if not latest:
            return
        await collection.update_many(
            {**tenant_filter, "_id": {"$ne": latest["_id"]}},
            {"$set": {"isLatest": False, "updatedAt": datetime.utcnow()}},
        )
        await collection.update_one(
            {"_id": latest["_id"]},
            {"$set": {"isLatest": True, "updatedAt": datetime.utcnow()}},
        )
    
    async def search_capabilities_for_step(
        self,
        tenant: Dict[str, Optional[str]],
        step_name: str,
        step_description: str,
        *,
        max_results: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        Search capability_repository for candidates that may satisfy a logical step.

        Improvements over naive text search:
        - Does NOT filter by preferredKind (caller can post-filter if needed).
        - Uses a combined query string: "{step.name}. {step.description}".
        - Searches across name, description, searchText, and tags via $text when available.
        - Applies a simple scoring metric:
            • Base on textScore (when text index is present).
            • Boost when tags overlap with step tokens.
            • Penalize / discard capabilities whose tags are clearly unrelated
              (negative filtering, e.g. "weather" tag when step has no weather words).
        """
        collection = self.get_collection()

        org_id = tenant.get("orgId")
        subscriber_id = tenant.get("subscriberId")
        user_id = tenant.get("userId")

        org_obj = None
        sub_obj = None
        user_obj = None
        if org_id:
            try:
                org_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                pass
        if subscriber_id:
            try:
                sub_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                pass
        if user_id:
            try:
                user_obj = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception:
                pass

        base_filter: Dict[str, Any] = {
            "isLatest": True,
            # Only return capabilities with at least one input AND one output parameter.
            # "inputParameters": {"$exists": True, "$not": {"$size": 0}},
            "outputParameters": {"$exists": True, "$not": {"$size": 0}},
        }

        plat_org_obj = _to_obj_id(_PLATFORM_TENANT.get("orgId"))
        plat_sub_obj = _to_obj_id(_PLATFORM_TENANT.get("subscriberId"))
        platform_pair = {"orgId": plat_org_obj, "subscriberId": plat_sub_obj}

        is_subscriber = sub_obj is not None and sub_obj != plat_sub_obj
        if is_subscriber:
            tenant_branches = [
                {"orgId": org_obj, "subscriberId": sub_obj},
                {"orgId": None, "subscriberId": sub_obj},
                platform_pair,
                {"orgId": None, "subscriberId": None},
            ]
        else:
            tenant_branches = [
                platform_pair,
                {"orgId": None, "subscriberId": None},
            ]

        tenant_filter: Dict[str, Any] = {"$or": tenant_branches}

        # Build combined search string as suggested: "{step.name}. {step.description}"
        combined_text = f"{(step_name or '').strip()}. {(step_description or '').strip()}".strip()

        # Basic tokenization + normalization for overlap and tag checks.
        def _normalize_token(t: str) -> str:
            """
            Normalize a token for matching:
            - lowercase
            - apply NLTK PorterStemmer when available (requested),
              otherwise fall back to a simple suffix-stripper.
            """
            t = (t or "").lower()
            if not t:
                return t
            if _PORTER_STEMMER is not None:
                try:
                    return _PORTER_STEMMER.stem(t)
                except Exception:
                    # Fallback if NLTK encounters an unexpected token
                    pass
            # Lightweight stemmer fallback
            for suffix in ("ing", "ed", "es", "s"):
                if len(t) > 4 and t.endswith(suffix):
                    t = t[: -len(suffix)]
                    break
            return t

        # Stop words excluded from per-token regex matching (too common to be useful).
        _STOP_WORDS = {
            "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "is", "it", "be", "as", "by", "we", "he", "she", "they", "are",
            "was", "were", "has", "have", "had", "will", "do", "does", "did", "not",
            "no", "so", "if", "with", "from", "that", "this", "these", "those",
            "then", "than", "can", "its", "also", "all", "any", "each", "into",
            "per", "our", "their", "which", "when", "where", "who", "how",
        }

        raw_tokens = [
            tok for tok in combined_text.replace(".", " ").replace(",", " ").split()
            if tok and tok.lower() not in _STOP_WORDS and len(tok) > 2
        ]
        step_tokens = {_normalize_token(tok) for tok in raw_tokens if tok}

        projection: Dict[str, Any] = {
            "capabilityId": 1,
            "kind": 1,
            "name": 1,
            "description": 1,
            "inputParameters": 1,
            "outputParameters": 1,
            # Persistence metadata (optional). Used by agentic planner to avoid
            # "hidden backend" ambiguity for UI screens and generated tools.
            "dataStores": 1,
            # Optional shallow schema snapshots for collections used by the capability.
            "collectionSchemas": 1,
            "tags": 1,
            "searchText": 1,
        }

        docs: List[Dict[str, Any]] = []
        used_text_index = False

        try:
            # Primary path: $text index (fast, ranked by textScore).
            text_filter: Dict[str, Any] = {
                "$and": [
                    base_filter,
                    tenant_filter,
                    {"$text": {"$search": combined_text}},
                ],
            }
            cursor = collection.find(
                text_filter,
                projection={**projection, "score": {"$meta": "textScore"}},
            ).sort([("score", {"$meta": "textScore"})])
            docs = await cursor.to_list(length=max_results * 4)
            used_text_index = True

        except Exception:
            # Fallback: no text index available.
            # Build per-token case-insensitive regexes across all searchable fields.
            # A document is fetched if it contains ANY of the meaningful step tokens
            # in name, description, searchText, tags, or capabilityId.
            # All relevance ranking is then done in Python via the scoring block below.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "capability_repository: $text unavailable, using per-token regex fallback"
            )

            # Dedupe while preserving order (step name tokens from combined_text come first).
            # Do NOT take only the longest tokens: long JSON / sentence fragments often
            # match nothing in capability_repository while short tokens (icp, lead, tool)
            # would match — that bug produced raw_docs=0 with a non-empty tenant.
            seen_lower: set[str] = set()
            search_tokens: List[str] = []
            for tok in raw_tokens:
                key = tok.lower()
                if key in seen_lower:
                    continue
                seen_lower.add(key)
                search_tokens.append(tok)
                if len(search_tokens) >= 18:
                    break

            if search_tokens:
                token_clauses: List[Dict[str, Any]] = []
                for tok in search_tokens:
                    pat = re.escape(tok)
                    tok_re = {"$regex": pat, "$options": "i"}
                    token_clauses.extend(
                        [
                            {"name": tok_re},
                            {"description": tok_re},
                            {"searchText": tok_re},
                            {"tags": tok_re},
                            {"capabilityId": tok_re},
                        ]
                    )

                regex_filter: Dict[str, Any] = {
                    "$and": [
                        base_filter,
                        tenant_filter,
                        {"$or": token_clauses},
                    ],
                }
                cursor = collection.find(regex_filter, projection=projection)
            else:
                # No usable tokens; fetch everything matching the tenant/isLatest filter
                # and rely entirely on in-Python scoring to discard irrelevant docs.
                cursor = collection.find(
                    {"$and": [base_filter, tenant_filter]},
                    projection=projection,
                )

            docs = await cursor.to_list(length=max_results * 4)

        # When no text index is available, scoring multipliers are raised to
        # compensate for the absence of textScore.  With a text index the
        # textScore component typically contributes 0.7–5+ to the base score;
        # without it we start every regex-matched doc at 1.0 and widen the
        # tag/name weights so a modestly-relevant tool (e.g. "email_notification"
        # for an "Email notification" step) can still reach the 2.0 threshold.
        if used_text_index:
            _fallback_base: float = 0.0     # textScore is already in doc["score"]
            _tag_weight: float = 0.5
            _name_weight: float = 0.35
        else:
            _fallback_base = 0.5            # regex hit = at least a mediocre match
            _tag_weight = 0.5
            _name_weight = 0.4

        candidates: List[Dict[str, Any]] = []

        for doc in docs:
            tags = [str(t).lower() for t in doc.get("tags", [])]
            tag_tokens = {_normalize_token(t) for t in tags}

            name_desc = (
                f"{doc.get('name', '')} {doc.get('description', '')} "
                f"{doc.get('searchText', '')}"
            )
            name_desc_tokens = {
                _normalize_token(tok)
                for tok in name_desc.replace(".", " ").replace(",", " ").split()
                if tok
            }

            capability_tokens = {t for t in (tag_tokens | name_desc_tokens) if t}

            # Generic negative filtering: discard docs with zero token overlap.
            # This removes completely unrelated capabilities without hard-coding
            # domain word lists.
            if not (step_tokens & capability_tokens):
                continue

            # Base: textScore when index was used, otherwise flat 1.0 for regex match.
            base_score = float(doc.get("score", 0.0)) + _fallback_base

            # Tag overlap boost.
            tag_overlap = step_tokens & tag_tokens
            tag_score = _tag_weight * len(tag_overlap)

            # Name/description overlap boost.  Higher weight than tags so that
            # tools with descriptive names (e.g. "send_email_notification") score
            # well even when their tag list is sparse.
            name_desc_overlap = step_tokens & name_desc_tokens
            name_desc_score = _name_weight * len(name_desc_overlap)

            # usageCount boost: capabilities selected in prior runs are preferred.
            usage_count = int(doc.get("usageCount", 0) or 0)
            usage_score = min(usage_count * 0.3, 3.0)

            total_score = base_score + tag_score + name_desc_score + usage_score
            doc["capabilityScore"] = total_score
            candidates.append(doc)

            if len(candidates) >= max_results * 2:
                # Collect a bit more than needed, then sort/trim.
                break

        # Final sort and trim.
        candidates.sort(key=lambda d: d.get("capabilityScore", 0.0), reverse=True)
        trimmed = candidates[:max_results]

        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.info(
            "capability search: step=%r text_index=%s raw_docs=%d surviving_candidates=%d "
            "top_scores=%s sample_tokens=%r",
            step_name,
            used_text_index,
            len(docs),
            len(trimmed),
            [round(c.get("capabilityScore", 0.0), 2) for c in trimmed[:5]],
            search_tokens[:8] if not used_text_index else None,
        )
        return trimmed

    async def increment_usage_counts(
        self,
        tenant: Dict[str, Optional[str]],
        capability_ids: List[str],
    ) -> None:
        """
        Atomically increment usageCount for each capability in `capability_ids`.
        Used after a successful plan run to boost capabilities that were actually
        selected (non-GENERATE) so they surface higher in future searches.
        """
        if not capability_ids:
            return
        collection = self.get_collection()
        org_id = tenant.get("orgId")
        subscriber_id = tenant.get("subscriberId")

        org_obj = None
        sub_obj = None
        if org_id:
            try:
                org_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                pass
        if subscriber_id:
            try:
                sub_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                pass

        plat_org_obj = _to_obj_id(_PLATFORM_TENANT.get("orgId"))
        plat_sub_obj = _to_obj_id(_PLATFORM_TENANT.get("subscriberId"))

        # Update both subscriber-scoped and platform-scoped documents in one shot.
        await collection.update_many(
            {
                "capabilityId": {"$in": capability_ids},
                "isLatest": True,
                "$or": [
                    {"orgId": org_obj, "subscriberId": sub_obj},
                    {"orgId": None, "subscriberId": sub_obj},
                    {"orgId": plat_org_obj, "subscriberId": plat_sub_obj},
                    {"orgId": None, "subscriberId": None},
                ],
            },
            {"$inc": {"usageCount": 1}},
        )

    async def get_latest_capability_by_id(
        self,
        tenant: Dict[str, Optional[str]],
        capability_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch the latest capability document by `capabilityId`.

        Tenant scoping:
        - If tenant fields are provided, we match both the tenant-specific doc
          and the global doc where org/sub are null (legacy).
        - If tenant fields are missing/invalid, we match only global docs.
        - Prefer `isLatest=True` and fall back to the newest by (version, updatedAt).
        - userId is not part of the match filter.
        """
        if not capability_id or not capability_id.strip():
            return None

        collection = self.get_collection()

        org_id = tenant.get("orgId")
        subscriber_id = tenant.get("subscriberId")

        org_obj = None
        sub_obj = None

        if org_id:
            try:
                org_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                org_obj = None
        if subscriber_id:
            try:
                sub_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                sub_obj = None

        # ── Tenant-aware query: subscriber rows OR platform rows ─────────────
        # Use $or so that orgId+subscriberId are matched as atomic pairs.
        plat_org_obj = _to_obj_id(_PLATFORM_TENANT.get("orgId"))
        plat_sub_obj = _to_obj_id(_PLATFORM_TENANT.get("subscriberId"))
        platform_pair = {"orgId": plat_org_obj, "subscriberId": plat_sub_obj}

        is_subscriber = sub_obj is not None and sub_obj != plat_sub_obj
        tenant_filter: Dict[str, Any]
        if is_subscriber:
            tenant_filter = {"$or": [
                {"orgId": org_obj, "subscriberId": sub_obj},
                {"orgId": None, "subscriberId": sub_obj},
                platform_pair,
                {"orgId": None, "subscriberId": None},  # pre-migration safety net
            ]}
        else:
            tenant_filter = {"$or": [
                platform_pair,
                {"orgId": None, "subscriberId": None},
            ]}

        query: Dict[str, Any] = {
            "capabilityId": capability_id,
            **tenant_filter,
        }

        doc = await collection.find_one({**query, "isLatest": True})
        if doc:
            return dict(doc)

        # Fallback: pick best among all versions.
        doc = await collection.find_one(
            query,
            sort=[("version", -1), ("updatedAt", -1)],
        )
        return dict(doc) if doc else None
