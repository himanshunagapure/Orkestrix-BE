"""
Workflow Analyzer Router

Accepts a workflow payload (with steps and sub-step descriptions) and uses
an LLM to suggest meaningful business reports / dashboards that can be
generated from the data collected by that workflow.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import textwrap
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.gemini_service import async_generate, GeminiGenerateConfig
from services.report_planning_service import VALID_REPORT_TYPES
from services.angular_generator import AngularAppGenerator
from services.backend_generator import BackendGenerator, BackendFunctionEntry, FunctionRegistry
from repositories.screen_repository import ScreenRepository
from repositories.capability_repository import CapabilityRepository
from services.capability_sync import sync_screen_single
from services.database import Database
from services.workflow_budget_service import (
    get_subscriber_credits_snapshot,
    init_subscriber_billing,
    clear_billing_context,
    apply_llm_charge,
    get_billing_context,
)
from services.llm_token_tracker import (
    extract_billable_tokens_from_usage_metadata,
    record_llm_call,
    get_tracker,
)
from services.collection_schema import (
    build_collection_schemas,
    fetch_latest_sample_doc_in_db,
    format_collection_schemas_for_prompt,
    resolve_subscriber_db_name,
)

logger = logging.getLogger(__name__)

_SUGGEST_REPORTS_MODEL = "gemini-2.5-flash"


def _record_and_charge_suggest_reports_llm(
    response: Any,
    *,
    model: str,
    sub_operation: str,
) -> None:
    """Record suggest-reports LLM usage and increment subscribers.AICredits.used."""
    ctx = get_billing_context()
    usage = getattr(response, "usage_metadata", None)
    tokens = extract_billable_tokens_from_usage_metadata(usage)
    record_id = record_llm_call(
        org_id=ctx.org_id if ctx else None,
        subscriber_id=ctx.subscriber_id if ctx else None,
        user_id=ctx.user_id if ctx else None,
        project_id=ctx.project_id if ctx else None,
        operation="suggest-reports",
        sub_operation=sub_operation,
        model=model,
        prompt_tokens=tokens["prompt_tokens"],
        completion_tokens=tokens["completion_tokens"],
        total_tokens=tokens["total_tokens"],
        thoughts_tokens=tokens.get("thoughts_tokens"),
        status="success",
        metadata={
            "usage_metadata": {
                "prompt_token_count": tokens["prompt_tokens"],
                "candidates_token_count": tokens.get("candidates_tokens", 0),
                "thoughts_token_count": tokens.get("thoughts_tokens", 0),
                "total_token_count": tokens["total_tokens"],
            }
        },
    )
    # record_llm_call already applies apply_llm_charge when the tracker is live.
    # If tracking was skipped (no collection), still increment AICredits.used.
    if not record_id:
        pricing = get_tracker()._calculate_cost(
            model, tokens["prompt_tokens"], tokens["completion_tokens"]
        )
        apply_llm_charge(pricing.get("total_cost_usd", 0))


router = APIRouter(
    prefix="/aiqod-agent/agent/dashboard-analyzer",
    tags=["Workflow Analyzer"],
)


def diff_new_reports(
    existing_reports: Optional[List[Dict[str, Any]]],
    incoming_reports: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Reports in ``incoming_reports`` that are not already present (and active)
    in ``existing_reports``. Used by dashboard add/update so cost estimate and
    generation only charge for genuinely new widgets.
    """
    existing_names = {
        (r.get("reportName") or "").strip().lower()
        for r in (existing_reports or [])
        if isinstance(r, dict) and not r.get("reportRemoved")
    }
    new_reports: List[Dict[str, Any]] = []
    for report in incoming_reports or []:
        if not isinstance(report, dict) or report.get("reportRemoved"):
            continue
        name = (report.get("reportName") or "").strip().lower()
        if name and name in existing_names:
            continue
        new_reports.append(report)
    return new_reports


def find_report_by_name(
    reports: Optional[List[Dict[str, Any]]],
    report_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the first report whose reportName matches (case-insensitive)."""
    target = (report_name or "").strip().lower()
    if not target:
        return None
    for report in reports or []:
        if isinstance(report, dict) and (report.get("reportName") or "").strip().lower() == target:
            return report
    return None

# ---------------------------------------------------------------------------
# Pydantic models — both inbound & outbound
# ---------------------------------------------------------------------------


class SubStep(BaseModel):
    stepId: str = ""
    name: str = ""
    description: str = ""


class Step(BaseModel):
    stepId: str = ""
    name: str = ""
    description: str = ""
    phaseType: str = ""
    subSteps: list[SubStep] = Field(default_factory=list)


class WorkflowAnalyzeRequest(BaseModel):
    workflowName: str = ""
    selectedCategory: str = ""
    type: str = ""
    steps: list[Step] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None
    projectId: Optional[str] = None
    selectedName: Optional[str] = None
    selectedType: Optional[str] = None
    prompt: Optional[str] = None
    minimumCredits: Optional[int] = None

    model_config = {"extra": "allow"}


class BusinessSemanticModel(BaseModel):
    domain: str = ""
    industry: str = ""
    capability: str = ""
    subdomain: str = ""
    purpose: str = ""
    start_event: str = ""
    end_event: str = ""
    primary_actor: str = ""
    secondary_actors: list[str] = Field(default_factory=list)
    business_goals: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    decision_points: list[str] = Field(default_factory=list)
    kpis: list[KPIDefinition] = Field(default_factory=list)


class KPIDefinition(BaseModel):
    name: str = ""
    formula: str = ""
    aggregation: str = ""
    frequency: str = ""
    stakeholder: str = ""


class KPIListResponse(BaseModel):
    kpis: list[KPIDefinition] = Field(default_factory=list)


class ReportSuggestion(BaseModel):
    reportName: str = Field(..., description="Short, descriptive report name")
    description: str = Field(..., description="What the report shows with collection name(s) as is in the workflow description and business value")
    reportType: str = Field(..., description="Tabular | Bar Chart | Column Chart | Line Chart | Pie Chart | Donut Chart | Area Chart | KPI Cards")
    collectionName: str = Field(..., description="The MongoDB collection name this report is derived from")
    requiredFields: list[str] = Field(
        default_factory=list,
        description=(
            "Exact field names that MUST appear in the query results, in display order. "
            "For Tabular/find reports list every projected column. "
            "For aggregation reports list every output field produced by the final $project stage."
        ),
    )
    optionalFilters: list[str] = Field(default_factory=list, description="Fields that can be used as filters")
    # Typed as str because Gemini's constrained JSON generation (response_schema)
    # requires fully-specified schemas; a bare list/array without concrete item
    # types causes Gemini to produce malformed JSON.  The downstream parser in
    # build_dashboard_definition uses raw_decode (tolerates trailing `}"` garbage)
    # and _strip_pipeline_key_spaces (removes leading-space-before-$ artefacts)
    # so both common Gemini double-serialisation artefacts are handled there.
    aggregation: Optional[str] = Field(None, description="MongoDB aggregation pipeline as a JSON array string, or null for tabular reports.")

    # Do NOT add extra = "allow" here — this model is used as response_schema for
    # the Gemini Developer API, which does NOT support additionalProperties.
    model_config = {}


class ReportSuggestionList(BaseModel):
    reports: list[ReportSuggestion]


class WorkflowAnalyzeResponse(BaseModel):
    success: bool = True
    reports: list[ReportSuggestion] = Field(default_factory=list)
    source: str = "llm"
    semantic_model: BusinessSemanticModel = Field(default_factory=BusinessSemanticModel)


# ---------------------------------------------------------------------------
# Deterministic report -> widget/query/aggregation-pipeline mapper.
#
# Reports are already fully structured (reportName/reportType/collectionName/
# optionalFilters/aggregation), so turning them into dashboard widgets + real
# MongoDB queries needs no LLM call at all — unlike free-text workflow steps.
# This also backs the real (non-mock) backend functions registered below, so
# the generated Angular dashboard fetches live data from Mongo instead of a
# mock data service.
#
# Two input schemas are supported:
#   New: report has 'collectionName' (string) — requiredFields is derived from
#        the aggregation pipeline $project/$group stages automatically.
#   Legacy: report has 'requiredFields' (list[str]) — backward compatible.
# ---------------------------------------------------------------------------

REPORT_TYPE_TO_WIDGET: Dict[str, str] = {
    "tabular": "table",
    "bar chart": "bar",
    "column chart": "column",
    "line chart": "line",
    "pie chart": "pie",
    "donut chart": "donut",
    "area chart": "area",
    "kpi cards": "kpi"
}

_COLLECTION_NAME_RE = re.compile(
    r"['\"]([A-Za-z0-9_]+)['\"]\s+(?:MongoDB\s+)?collections?\b",
    re.IGNORECASE,
)
# Backtick-quoted names as used by LLM descriptions: `all_Candidates`
_COLLECTION_NAME_BACKTICK_RE = re.compile(r"`([A-Za-z0-9_]+)`")
# Also matches unquoted collection names: "in the all_Candidates collection"
_COLLECTION_NAME_UNQUOTED_RE = re.compile(
    r"\b(?:in\s+the\s+|from\s+the\s+)?([A-Za-z0-9_]+)\s+(?:MongoDB\s+)?collections?\b",
    re.IGNORECASE,
)


def _strip_pipeline_key_spaces(obj: Any) -> Any:
    """Recursively strip leading/trailing whitespace from every dict key.

    LLM-generated aggregation JSON sometimes includes a space before the
    dollar sign, e.g. ``" $facet"`` instead of ``"$facet"``.  MongoDB rejects
    any pipeline stage whose name contains leading/trailing whitespace, so we
    normalise all keys before the pipeline is used.
    """
    if isinstance(obj, dict):
        return {k.strip(): _strip_pipeline_key_spaces(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_pipeline_key_spaces(item) for item in obj]
    return obj


def _normalize_pipeline_stage_keys(pipeline: Any) -> Any:
    """Normalize double-dollar-sign stage operator keys produced by the LLM.

    Gemini sometimes generates ``$$group`` / ``$$sort`` / ``$$project`` etc.
    instead of the correct ``$group`` / ``$sort`` / ``$project`` for top-level
    pipeline stage operators.  This happens because the model interprets the
    instruction to "use Python terms" as licence to apply Python
    ``string.Template`` escaping where ``$$`` represents a literal ``$``.

    Only the TOP-LEVEL key of each pipeline stage dict is touched — ``$$``
    prefixes inside stage bodies (e.g. ``$$ROOT``, ``$$CURRENT``, user-defined
    ``$$var``) are valid MongoDB syntax and must NOT be modified.

    Examples::

        [{"$$group": {...}}, {"$$sort": {...}}]
        → [{"$group": {...}}, {"$sort": {...}}]
    """
    if not isinstance(pipeline, list):
        return pipeline
    normalized = []
    for stage in pipeline:
        if isinstance(stage, dict):
            new_stage: Dict[str, Any] = {}
            for k, v in stage.items():
                if k.startswith("$$"):
                    new_stage["$" + k[2:]] = v
                else:
                    new_stage[k] = v
            normalized.append(new_stage)
        else:
            normalized.append(stage)
    return normalized


def _is_nonfinite_bucket_bound(val: Any) -> bool:
    """True if a $bucket.boundaries value is null, Inf, NaN, or an Infinity string."""
    if val is None:
        return True
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return val != val or val == float("inf") or val == float("-inf")
    if isinstance(val, str) and val.strip().lower() in (
        "infinity", "+infinity", "-infinity", "inf", "-inf", "nan",
    ):
        return True
    return False


def _bucket_bound_type_key(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    return type(val).__name__


def _has_invalid_bucket_boundaries(pipeline: Any) -> bool:
    """True if any $bucket (including nested $facet) has illegal boundaries."""
    def walk(stages: Any) -> bool:
        if not isinstance(stages, list):
            return False
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            bucket = stage.get("$bucket")
            if isinstance(bucket, dict):
                bounds = bucket.get("boundaries")
                if not isinstance(bounds, list) or len(bounds) < 2:
                    return True
                if any(_is_nonfinite_bucket_bound(b) for b in bounds):
                    return True
                type_keys = {_bucket_bound_type_key(b) for b in bounds}
                if not (type_keys <= {"int", "float"}) and len(type_keys) > 1:
                    return True
            facet = stage.get("$facet")
            if isinstance(facet, dict):
                for sub in facet.values():
                    if walk(sub):
                        return True
        return False
    return walk(pipeline)


def _sanitize_bucket_boundaries(pipeline: Any) -> Any:
    """Strip null/Inf/NaN from $bucket.boundaries and add default if missing."""
    if not isinstance(pipeline, list):
        return pipeline
    sanitized = []
    for stage in pipeline:
        if not isinstance(stage, dict):
            sanitized.append(stage)
            continue
        stage = dict(stage)
        bucket = stage.get("$bucket")
        if isinstance(bucket, dict):
            bucket = dict(bucket)
            bounds = bucket.get("boundaries")
            if isinstance(bounds, list):
                cleaned = [b for b in bounds if not _is_nonfinite_bucket_bound(b)]
                bucket["boundaries"] = cleaned
                if "default" not in bucket:
                    bucket["default"] = "Other"
            stage["$bucket"] = bucket
        facet = stage.get("$facet")
        if isinstance(facet, dict):
            stage["$facet"] = {
                k: _sanitize_bucket_boundaries(v) for k, v in facet.items()
            }
        sanitized.append(stage)
    return sanitized


def _lookup_from_collections(aggregation_raw) -> set:
    """Return the set of collection names used in $lookup.from stages of the
    aggregation pipeline.  These are *secondary* collections (joined into),
    so the primary datasource collection must NOT be one of them."""
    pipeline = aggregation_raw
    if isinstance(pipeline, str):
        try:
            pipeline = json.loads(pipeline)
        except (json.JSONDecodeError, ValueError):
            return set()
    if not isinstance(pipeline, list):
        return set()
    from_cols: set = set()
    for stage in pipeline:
        if isinstance(stage, dict):
            lookup = stage.get("$lookup")
            if isinstance(lookup, dict) and "from" in lookup:
                from_cols.add(str(lookup["from"]))
    return from_cols


def _infer_report_collection(report: Dict[str, Any]) -> str:
    """Best-effort collection-name inference from the report description.

    Strategy (in order of preference):
      1. Quoted collection name ('name' collection / "name" collection).
      2. Backtick-quoted names (`name`) — common in LLM-generated descriptions.
      3. Unquoted prose (in the name collection / from the name collection).
      For strategies 2 & 3, skip any name that appears as a $lookup.from
      target (those are secondary/joined collections, not the primary datasource).
      4. Fallback: snake_case slug of the report name.
    """
    desc = str(report.get("description") or "")

    # Priority 1: single/double-quoted name next to the word "collection"
    m = _COLLECTION_NAME_RE.search(desc)
    if m:
        return m.group(1)

    # Collect $lookup.from values to exclude secondary collections
    lookup_froms = _lookup_from_collections(report.get("aggregation"))

    # Priority 2: backtick-quoted names (`collection_name`)
    backtick_candidates = _COLLECTION_NAME_BACKTICK_RE.findall(desc)
    for cand in backtick_candidates:
        if cand not in lookup_froms:
            return cand
    if backtick_candidates:
        return backtick_candidates[0]

    # Priority 3: unquoted prose name, preferring a non-lookup collection
    candidates = [m.group(1) for m in _COLLECTION_NAME_UNQUOTED_RE.finditer(desc)]
    for cand in candidates:
        if cand not in lookup_froms:
            return cand
    if candidates:
        return candidates[0]

    name = str(report.get("reportName") or "Report")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    return slug or "Report"


def _clean_xfield(raw: Optional[str]) -> str:
    """
    Extract a clean, TypeScript-safe field name from a potentially SQL-like expression.

    Examples:
      "DATE(ticket_created_at)"                              → "ticket_created_at"
      "ticket_requester_id ORDER BY COUNT(ticket_id) DESC"   → "ticket_requester_id"
      "AVG(confidence_score)"                                → "confidence_score"
      "suggested_category"                                   → "suggested_category"  (unchanged)

    The cleaned value is used as the display xField sent to the LLM so it never
    receives parentheses, SQL keywords, or other characters that cause TypeScript
    syntax errors when the LLM forgets to quote the string literal.

    The MongoDB $project stage is updated to use the CLEANED name as the output
    field, so Angular's `item[xField]` bracket access works correctly.
    """
    if not raw:
        return raw or ""
    s = raw.strip()
    # Strip SQL-like ORDER BY / LIMIT / HAVING suffixes
    s = re.split(r'\s+ORDER\s+BY\b', s, flags=re.IGNORECASE)[0].strip()
    s = re.split(r'\s+LIMIT\b', s, flags=re.IGNORECASE)[0].strip()
    s = re.split(r'\s+HAVING\b', s, flags=re.IGNORECASE)[0].strip()
    # Unwrap function calls like DATE(field), LOWER(field), YEAR(field), etc.
    fn_match = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\((.+)\)$', s)
    if fn_match:
        s = fn_match.group(1).strip()
    # Take only the first identifier if multiple comma-separated values remain
    s = s.split(",")[0].strip()
    # Strip any remaining non-identifier characters (keep letters, digits, underscores)
    s = re.sub(r'[^A-Za-z0-9_]', '_', s).strip('_')
    return s or raw


def _parse_aggregation_pipeline(
    aggregation: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Extract aggregation pipeline and derive chart metadata from a MongoDB aggregation pipeline list.
    Returns ([], {}) when aggregation is empty or not a list.

    Handles multi-metric pipelines: extracts ALL accumulator fields from $group,
    then scans subsequent $project stages to map any renames so that the returned
    metricKeys reflect the actual output field names the frontend will receive.
    """
    if not aggregation or not isinstance(aggregation, list):
        return [], {}

    pipeline = aggregation
    chart_meta = {}

    # Handle $count stage: [{"$count": "fieldName"}] — yields a single KPI value
    for stage in pipeline:
        if isinstance(stage, dict) and "$count" in stage:
            metric_field = stage["$count"] if isinstance(stage["$count"], str) else "count"
            chart_meta = {
                "aggregationFunction": "COUNT",
                "measureField": "1",
                "metricKey": metric_field,
                "metricKeys": [metric_field],
                "groupByFields": [],
            }
            return pipeline, chart_meta

    # Find the $group stage to extract group fields and metrics
    group_stage = None
    for stage in pipeline:
        if isinstance(stage, dict) and "$group" in stage:
            group_stage = stage["$group"]
            break

    if group_stage and isinstance(group_stage, dict):
        group_id = group_stage.get("_id")
        group_fields = []
        if isinstance(group_id, str):
            if group_id.startswith("$"):
                group_fields.append(group_id[1:])
            else:
                group_fields.append(group_id)
        elif isinstance(group_id, dict):
            for k, v in group_id.items():
                if isinstance(v, str) and v.startswith("$"):
                    group_fields.append(v[1:])
                elif k.startswith("$"):
                    # k is a MongoDB operator (e.g. $dateToString, $toLower), not a
                    # user-defined alias.  The whole _id expression computes a single
                    # anonymous value that MongoDB stores as _id.  Push '_id' as a
                    # placeholder so the $project rename scan below can resolve it to
                    # the actual output field name (e.g. 'date' from "$_id" rename).
                    group_fields.append("_id")
                else:
                    group_fields.append(k)

        # Extract ALL accumulator fields from $group (not just the first one)
        metric_field = "value"
        metric_keys: List[str] = []
        func = "COUNT"
        field = "1"
        for k, v in group_stage.items():
            if k != "_id" and isinstance(v, dict):
                if not metric_keys:
                    # First accumulator sets the primary metricKey
                    metric_field = k
                    for op, val in v.items():
                        if op == "$sum":
                            if val == 1:
                                func = "COUNT"
                                field = "1"
                            else:
                                func = "SUM"
                                field = str(val)[1:] if isinstance(val, str) and val.startswith("$") else str(val)
                        elif op == "$avg":
                            func = "AVG"
                            field = str(val)[1:] if isinstance(val, str) and val.startswith("$") else str(val)
                        elif op == "$min":
                            func = "MIN"
                            field = str(val)[1:] if isinstance(val, str) and val.startswith("$") else str(val)
                        elif op == "$max":
                            func = "MAX"
                            field = str(val)[1:] if isinstance(val, str) and val.startswith("$") else str(val)
                metric_keys.append(k)

        # Scan $project stages after $group to map renames
        # e.g. {'Q1_FY26_Net_Movement': '$q1_movement'} means q1_movement → Q1_FY26_Net_Movement
        if metric_keys:
            seen_group = False
            for stage in pipeline:
                if isinstance(stage, dict) and "$group" in stage:
                    seen_group = True
                    continue
                if seen_group and isinstance(stage, dict) and "$project" in stage:
                    proj = stage["$project"]
                    if isinstance(proj, dict):
                        rename_map = {}
                        for out_key, val_expr in proj.items():
                            if isinstance(val_expr, str) and val_expr.startswith("$"):
                                rename_map[val_expr[1:]] = out_key
                        if rename_map:
                            metric_keys = [rename_map.get(k, k) for k in metric_keys]
                            metric_field = rename_map.get(metric_field, metric_field)
                            group_fields = [rename_map.get(g, g) for g in group_fields]

        # ── $replaceRoot detection ──────────────────────────────────────────────────
        # When a pipeline uses the pattern:
        #   $group(null, col1=$sum(...), col2=$sum(...), ...)
        #   $project(data=[{xField: 'Label', yField: '$col1'}, ...])
        #   $unwind($data)
        #   $replaceRoot(newRoot=$data)
        # …the actual output fields come from the array element objects, NOT from the
        # $group accumulators. The rename-map scan above only handles string-to-string
        # renames and misses the literal-array pattern, leaving metricKey wrong (e.g.
        # 'current' instead of 'total_amount'), which causes a blank chart because
        # the Angular yfield doesn't match any field in the response data.
        for stage_idx, stage in enumerate(pipeline):
            if not (isinstance(stage, dict) and "$replaceRoot" in stage):
                continue
            new_root_expr = (stage["$replaceRoot"] or {}).get("newRoot", "")
            if not (isinstance(new_root_expr, str) and new_root_expr.startswith("$")):
                continue
            root_field = new_root_expr[1:]  # e.g. "data" from "$data"
            # Search backwards for the $project stage that created this field as a
            # literal array so we can read the actual output field names from it.
            for prev_stage in reversed(pipeline[:stage_idx]):
                if not (isinstance(prev_stage, dict) and "$project" in prev_stage):
                    continue
                proj = prev_stage.get("$project") or {}
                if not isinstance(proj, dict) or root_field not in proj:
                    continue
                array_val = proj[root_field]
                if not (isinstance(array_val, list) and array_val):
                    break
                first_elem = array_val[0]
                if not isinstance(first_elem, dict):
                    break
                keys = [k for k in first_elem.keys()]
                if len(keys) >= 2:
                    group_fields = [keys[0]]     # x-axis / dimension field
                    metric_field = keys[1]       # primary y-axis / metric field
                    metric_keys  = keys[1:]      # all metric fields after the x-axis key
                elif len(keys) == 1:
                    metric_field = keys[0]
                    metric_keys  = [keys[0]]
                break
            break  # only process the first $replaceRoot in the pipeline

        # ── Final $project output fields ────────────────────────────────────────────
        # Multi-stage pipelines often compute a NEW metric in the last $project
        # (e.g. percentage = round(count/total*100)) after an intermediate $group
        # that only has `count`.  The first-$group + string-rename scan above would
        # leave metricKey='count', but `count` is NOT in the documents Angular
        # receives — the chart yField must be the computed output field.
        # Only string "$path" renames were mapped; dict expressions ($round, $divide,
        # $multiply, …) were ignored.  Honor those computed keys here.
        last_proj = None
        for stage in pipeline:
            if isinstance(stage, dict) and "$project" in stage:
                last_proj = stage.get("$project")
        if isinstance(last_proj, dict) and not any(isinstance(v, list) for v in last_proj.values()):
            computed_metrics: List[str] = []
            dimension_fields: List[str] = []
            for out_key, val_expr in last_proj.items():
                if val_expr in (0, False):
                    continue
                if isinstance(val_expr, dict):
                    computed_metrics.append(out_key)
                elif isinstance(val_expr, str) and val_expr.startswith("$"):
                    dimension_fields.append(out_key)
            if computed_metrics:
                metric_keys = computed_metrics
                metric_field = computed_metrics[0]
                if dimension_fields:
                    group_fields = dimension_fields

        chart_meta = {
            "aggregationFunction": func,
            "measureField": field,
            "metricKey": metric_field,
            "metricKeys": metric_keys,
            "groupByFields": group_fields,
        }

    return pipeline, chart_meta


def _smart_fix_pipeline(
    pipeline: List[Dict[str, Any]],
    chart_meta: Dict[str, Any],
    required_fields: List[str],
) -> List[Dict[str, Any]]:
    """
    Patch common field-name mismatches that prevent chart aggregations from
    returning real labels instead of null/_id:

    Pattern 1 — singular/plural array mismatch:
      Group field 'skill' not in required_fields but plural 'skills' is → add
      $unwind before $group so every skill array element is counted separately.

    Pattern 2 — computed bucket suffix:
      Group field 'experience_bucket' not in required_fields but base field
      'experience' is → inject $addFields to alias the base field under the
      bucket name so documents pass through the $group correctly.
    """
    if not pipeline:
        return pipeline

    group_fields = chart_meta.get("groupByFields", [])
    if len(group_fields) != 1:
        return pipeline

    group_field = group_fields[0]

    # Pattern 1: singular 'skill' → plural array 'skills'
    plural_candidate = group_field + "s"
    if plural_candidate in required_fields and group_field not in required_fields:
        new_pipeline: List[Dict[str, Any]] = []
        for stage in pipeline:
            if "$group" in stage:
                new_pipeline.append({"$unwind": f"${plural_candidate}"})
                patched_group = dict(stage["$group"])
                patched_group["_id"] = f"${plural_candidate}"
                new_pipeline.append({"$group": patched_group})
            elif "$project" in stage:
                # The $project already renames _id → group_field; keep as-is
                # so Angular still reads item[group_field] (e.g. item['skill'])
                new_pipeline.append(stage)
            else:
                new_pipeline.append(stage)
        return new_pipeline

    # Pattern 2: 'experience_bucket' → base field 'experience'
    if group_field.endswith("_bucket"):
        base_field = group_field[: -len("_bucket")]
        if base_field in required_fields and group_field not in required_fields:
            return [{"$addFields": {group_field: f"${base_field}"}}] + pipeline

    return pipeline


class DashboardWidget(BaseModel):
    """One dashboard widget derived from a single report definition."""
    reportId: str
    reportName: str
    reportType: str
    widgetType: str
    title: str
    description: Optional[str] = None
    datasource: str
    requiredFields: list[str] = Field(default_factory=list)
    query: Dict[str, Any] = Field(default_factory=dict)
    aggregationPipeline: list[Dict[str, Any]] = Field(default_factory=list)
    filters: list[Dict[str, Any]] = Field(default_factory=list)
    sort: Dict[str, int] = Field(default_factory=dict)
    chartMeta: Dict[str, Any] = Field(default_factory=dict)
    # Populated once register_dashboard_backend_functions() runs — the real
    # /dynamic/execute function_id the Angular widget must call for live data.
    functionId: Optional[str] = None
    # Grid layout fields — deterministic positioning used by Angular template generator.
    # A 12-column CSS grid is assumed (same as Bootstrap / Angular Flex-Layout).
    # KPI cards span the full width (gridWidth=12); all other widgets use a
    # two-column layout (gridWidth=6) with the last widget promoted to full width
    # when the non-KPI count is odd.
    gridRow: Optional[int] = None      # 1-based row index in the dashboard grid
    gridCol: Optional[int] = None      # 1-based starting column (1 or 7 in a 2-col layout)
    gridWidth: Optional[int] = None    # Column span (1–12)
    gridHeight: Optional[int] = None   # Row span (number of grid row units, e.g. 4 = ~400px)


class DashboardDefinition(BaseModel):
    """Full dashboard configuration generated from a `report` array."""
    dashboardId: str
    dashboardName: str
    widgets: list[DashboardWidget] = Field(default_factory=list)
def _derive_expanded_schema(sample: dict) -> dict:
    """Recursively derive nested schema from a MongoDB sample document.
    
    Returns format:
      {"field_name": "string", "nested_field": {"sub": "number"}, ...}
    """
    def _infer_type(value):
        if value is None:
            return "null"
        if isinstance(value, ObjectId):
            return "ObjectId"
        if isinstance(value, datetime):
            return "Date"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "number"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            if not value:
                return ["null"]
            element_types = [_infer_type(v) for v in value]
            # Deduplicate while preserving order
            seen = []
            for t in element_types:
                if t not in seen:
                    seen.append(t)
            return seen if len(seen) > 1 else seen[:1]
        if isinstance(value, dict):
            return {k: _infer_type(v) for k, v in value.items()}
        return "any"
    
    if not isinstance(sample, dict):
        return {}
    return {k: _infer_type(v) for k, v in sample.items()}


def _derive_fields_from_pipeline(pipeline: List[Dict[str, Any]]) -> List[str]:
    """Extract field names referenced in an aggregation pipeline's $project, $group, and other stages.
    
    Used as a fallback when requiredFields is not provided in the report (e.g. when
    the new collectionName-only schema is used).
    """
    fields: List[str] = []
    seen: set = set()
    for stage in pipeline:
        if not isinstance(stage, dict):
            continue
        for op, body in stage.items():
            if op == "$project" and isinstance(body, dict):
                for k, v in body.items():
                    if k != "_id":
                        if isinstance(v, int) and v == 1:
                            if k not in seen:
                                fields.append(k)
                                seen.add(k)
                        elif isinstance(v, str) and v.startswith("$"):
                            # v is a rename expression like "$_id"; k is the OUTPUT
                            # field name the frontend will actually receive — not v[1:]
                            # (the source field, which may be '_id' or another internal name).
                            if k not in seen:
                                fields.append(k)
                                seen.add(k)
            elif op == "$group" and isinstance(body, dict):
                group_id = body.get("_id")
                if isinstance(group_id, str) and group_id.startswith("$"):
                    field_name = group_id[1:]
                    if field_name not in seen:
                        fields.append(field_name)
                        seen.add(field_name)
                elif isinstance(group_id, dict):
                    for v in group_id.values():
                        if isinstance(v, str) and v.startswith("$"):
                            field_name = v[1:]
                            if field_name not in seen:
                                fields.append(field_name)
                                seen.add(field_name)
    return fields


def _assign_grid_layout(widgets: list["DashboardWidget"]) -> None:
    """
    Assign deterministic grid positions to a list of dashboard widgets using a
    12-column CSS grid model.

    Layout rules:
    - KPI Cards widgets always span the full width (12 columns) and are placed
      first in their own row.
    - All remaining widgets are placed in a 2-column layout (6 columns each).
      When the non-KPI count is odd the last widget is promoted to full width.
    - ``gridHeight`` defaults to 4 row-units (≈ 400 px) for chart/table widgets
      and 2 row-units for KPI cards.
    """
    kpi_widgets = [w for w in widgets if w.widgetType == "kpi"]
    other_widgets = [w for w in widgets if w.widgetType != "kpi"]

    current_row = 1

    for w in kpi_widgets:
        w.gridRow = current_row
        w.gridCol = 1
        w.gridWidth = 12
        w.gridHeight = 2
        current_row += 2  # KPI row takes 2 row-units

    for idx, w in enumerate(other_widgets):
        col_position = idx % 2  # 0 = left, 1 = right
        is_last_and_odd = (idx == len(other_widgets) - 1) and (len(other_widgets) % 2 == 1)

        if col_position == 0:
            w.gridRow = current_row
        else:
            w.gridRow = current_row  # same row as the left widget

        if is_last_and_odd:
            w.gridCol = 1
            w.gridWidth = 12
        else:
            w.gridCol = 1 if col_position == 0 else 7
            w.gridWidth = 6

        w.gridHeight = 4

        if col_position == 1 or is_last_and_odd:
            current_row += 4  # advance row after filling both columns (or the lone last)


async def _llm_build_query_plan_descriptions(
    widgets: list["DashboardWidget"],
) -> dict[str, str]:
    """
    Use an LLM to generate clear, plain-English bullet-point descriptions for
    every widget in a single batched call, explaining the underlying MongoDB
    query step by step.

    Returns a dict mapping reportName → enriched description string.
    Falls back to a compact deterministic summary per widget if the LLM call
    fails, so the system is always resilient.
    """

    def _deterministic_fallback(w: "DashboardWidget") -> str:
        """Minimal deterministic fallback used when the LLM call fails."""
        parts = [f"• Collection: {w.datasource}"]
        parts.append("• Operation: aggregate" if w.aggregationPipeline else "• Operation: find")
        if w.requiredFields:
            parts.append(f"• Projected fields: {', '.join(w.requiredFields)}")
        filter_fields = [f.get("field", "") for f in (w.filters or []) if f.get("field")]
        if filter_fields:
            parts.append(f"• Filterable by: {', '.join(filter_fields)}")
        if not w.aggregationPipeline and w.sort:
            sort_str = ", ".join(
                f"{k} {'asc' if v > 0 else 'desc'}" for k, v in w.sort.items()
            )
            if sort_str:
                parts.append(f"• Sorted by: {sort_str}")
        return "\n".join(parts)

    if not widgets:
        return {}

    # Build a compact JSON payload describing each widget's query
    widget_specs = []
    for w in widgets:
        spec: Dict[str, Any] = {
            "reportName": w.reportName,
            "originalDescription": (w.description or "").split("\n")[0].strip(),
            "collection": w.datasource,
            "operation": "aggregate" if w.aggregationPipeline else "find",
            "projectedFields": w.requiredFields or [],
            "filterableFields": [f.get("field", "") for f in (w.filters or []) if f.get("field")],
        }
        if w.aggregationPipeline:
            spec["aggregationPipeline"] = w.aggregationPipeline
        elif w.sort:
            spec["sort"] = w.sort
        widget_specs.append(spec)

    prompt = f"""You are a technical documentation writer for a Business Intelligence dashboard.

For each report widget below, write a structured description that has exactly two parts:
1. One concise sentence (≤ 20 words) summarising the business value — use the provided originalDescription as context.
2. A blank line, then bullet points (use the • character) explaining the MongoDB query step by step in plain English. Each bullet should be ≤ 15 words. Use the actual field and collection names provided. Include bullets for:
   • Collection queried
   • Query type (find or aggregate)
   • Fields returned (list all projected fields)
   • Filters applied (omit if none)
   • How data is grouped (omit if no $group)
   • Sort order (omit if no sort)
   • Result limit (omit if no $limit)
   • Any joins (omit if no $lookup)

Widget specs (JSON):
{json.dumps(widget_specs, indent=2)}

Return a JSON object with this shape:
{{
  "descriptions": [
    {{ "reportName": "<exact reportName>", "description": "<full description with bullet points>" }},
    ...
  ]
}}
Do NOT omit any widget. Preserve the exact reportName values.
"""

    try:
        response = await async_generate(
            "gemini-2.5-flash",
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )
        raw = getattr(response, "text", None) or str(response)
        parsed = json.loads(raw)
        result: dict[str, str] = {}
        for item in parsed.get("descriptions") or []:
            name = item.get("reportName", "")
            desc = item.get("description", "")
            if name and desc:
                result[name] = desc
        # Fill in any widgets the LLM missed with the deterministic fallback
        for w in widgets:
            if w.reportName not in result:
                result[w.reportName] = _deterministic_fallback(w)
        return result
    except Exception as _llm_err:
        logger.warning("_llm_build_query_plan_descriptions failed (%s); using deterministic fallback", _llm_err)
        return {w.reportName: _deterministic_fallback(w) for w in widgets}



def build_dashboard_definition(
    project_id: str,
    dashboard_name: str,
    reports: list[Dict[str, Any]],
    collection_schemas: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[DashboardDefinition, list[str]]:
    """
    Deterministic report -> widget/query/aggregation-pipeline mapper. This is the
    dashboard-flow analogue of the workflow builder's scratch graph planner: it
    turns the `report` array into a fully-specified DashboardDefinition without
    any LLM calls (reports are already structured, unlike free-text workflow steps).
    
    Supports both the new and legacy report schemas:
      - New schema: report has 'collectionName' (string) instead of 'requiredFields'
      - Legacy schema: report has 'requiredFields' (list of strings)
      Fields are derived from the aggregation pipeline $project/$group stages when
      requiredFields is absent.

    ``collection_schemas`` is an optional list of schema entries (each a dict with
    ``collection_name``/``name`` and ``schema``/``fields`` keys). When provided,
    the schema for each collection is used as a fallback to populate requiredFields
    for tabular reports that have no explicit fields and no aggregation pipeline.
    """
    # Build a fast lookup: collection name → ordered list of non-_id field names
    _schema_fields_by_collection: Dict[str, List[str]] = {}
    if collection_schemas:
        for _entry in collection_schemas:
            if not isinstance(_entry, dict):
                continue
            _coll_key = _entry.get("collection_name") or _entry.get("name") or ""
            _fields_obj = _entry.get("schema") or _entry.get("fields") or {}
            if _coll_key and isinstance(_fields_obj, dict):
                _schema_fields_by_collection[_coll_key] = [
                    k for k in _fields_obj if k != "_id"
                ]

    warnings: list[str] = []
    widgets: list[DashboardWidget] = []
    for idx, report in enumerate(reports):
        report_id = f"report_{idx + 1}"
        report_name = report.get("reportName") or f"Report {idx + 1}"
        report_type_raw = str(report.get("reportType") or "Tabular")
        widget_type = REPORT_TYPE_TO_WIDGET.get(report_type_raw.strip().lower())
        if not widget_type:
            widgets_warning = f"[dashboard] Unknown reportType '{report_type_raw}' for '{report_name}' — defaulting to table widget."
            warnings.append(widgets_warning)
            widget_type = "table"

        # Normalise empty strings that callers sometimes send instead of null
        raw_collection = report.get("collectionName")
        if raw_collection == "":
            raw_collection = None
        raw_aggregation = report.get("aggregation")
        if raw_aggregation == "" or raw_aggregation == []:
            raw_aggregation = None
        if raw_aggregation is not None:
            report = {**report, "aggregation": raw_aggregation}

        # Collection name: prefer explicit collectionName, fall back to inference from description
        collection = raw_collection or _infer_report_collection(report)
        
        optional_filters = [f for f in (report.get("optionalFilters") or []) if isinstance(f, str)]
        aggregation = report.get("aggregation")
        # Treat empty string as null (callers sometimes send "" instead of null)
        if aggregation == "":
            aggregation = None
        # Aggregation may arrive as a JSON-encoded string — parse it to a list
        # before passing to _parse_aggregation_pipeline which requires a list.
        if isinstance(aggregation, str):
            try:
                # Some LLMs (e.g. Gemini with response_schema) double-quote every
                # key, producing ""$group"" instead of "$group".  Collapse any run
                # of consecutive double-quote characters that surrounds a token back
                # to a single double-quote so the string becomes valid JSON before
                # we attempt to parse it.
                # Strategy: replace every occurrence of two or more consecutive `"`
                # that appear immediately before or after a non-quote character with
                # a single `"`.  A simple `replace('""', '"')` is safe here because
                # legitimate JSON never has adjacent double-quotes at the same level.
                _agg_str = aggregation.strip()
                if '""' in _agg_str:
                    _agg_str = _agg_str.replace('""', '"')
                # Use raw_decode instead of json.loads so that trailing garbage
                # characters (e.g. the extra `}"` that LLMs commonly append after
                # a valid JSON array) are silently ignored rather than causing the
                # entire pipeline to be discarded.
                aggregation, _ = json.JSONDecoder().raw_decode(_agg_str)
                # Strip leading/trailing whitespace from every dict key so that
                # LLM-generated pipelines with " $stage" (space before $) are
                # normalised to "$stage" before being sent to MongoDB.
                aggregation = _strip_pipeline_key_spaces(aggregation)
                # Normalize double-dollar stage keys ($$group → $group) that
                # Gemini generates when it confuses Python string.Template
                # escaping with MongoDB operator syntax.
                aggregation = _normalize_pipeline_stage_keys(aggregation)
                aggregation = _sanitize_bucket_boundaries(aggregation)
            except (json.JSONDecodeError, ValueError):
                aggregation = None
        elif isinstance(aggregation, list):
            # Aggregation already parsed (e.g. passed as a list directly) —
            # still normalise to catch any upstream $$-prefixed stage keys.
            aggregation = _normalize_pipeline_stage_keys(aggregation)
            aggregation = _sanitize_bucket_boundaries(aggregation)
        pipeline, chart_meta = _parse_aggregation_pipeline(aggregation)

        # Required fields: use explicit list, or derive from pipeline stages
        explicit_required = [f for f in (report.get("requiredFields") or []) if isinstance(f, str)]
        if explicit_required:
            required_fields = explicit_required
        elif pipeline:
            required_fields = _derive_fields_from_pipeline(pipeline)
        else:
            # For tabular reports without explicit fields, try (in order):
            #   1. Live collection schema provided by the caller
            #   2. "Projected fields: ..." extracted from the description text
            #   3. optionalFilters as a last resort
            #   4. Generic ["name", "value"] stub
            if collection in _schema_fields_by_collection:
                required_fields = _schema_fields_by_collection[collection]
            else:
                # Parse "Projected fields: f1, f2, ..." or "• Projected fields: f1, f2"
                # from the description so the bullet-point text written by the LLM
                # is never wasted even when the schema is unavailable.
                _desc_text = report.get("description") or ""
                _proj_match = re.search(
                    r"[Pp]rojected\s+fields?[:\s]+([^\n•]+)",
                    _desc_text,
                )
                if _proj_match:
                    required_fields = [
                        f.strip()
                        for f in _proj_match.group(1).split(",")
                        if f.strip()
                    ]
                elif optional_filters:
                    required_fields = optional_filters[:]
                else:
                    required_fields = ["name", "value"]
        
        pipeline = _smart_fix_pipeline(pipeline, chart_meta, required_fields)

        mongo_query: Dict[str, Any] = {
            "collection": collection,
            "operation": "aggregate" if pipeline else "find",
            "filter": {},
            "projection": {f: 1 for f in required_fields} if required_fields else {},
        }

        filters = [
            {"field": f, "operator": "eq", "value": None, "required": False}
            for f in optional_filters
        ]

        if pipeline:
            sort_spec: Dict[str, int] = {chart_meta.get("metricKey", "value"): -1}
        else:
            sort_spec = {required_fields[0]: 1} if required_fields else {}

        # Derive xField from groupByFields or requiredFields, then clean it so the
        # Angular/TypeScript code never receives SQL expressions like DATE(field) or
        # "field ORDER BY COUNT(...)" as identifiers — those cause TS syntax errors.
        raw_xfield = (chart_meta.get("groupByFields") or required_fields or [None])[0]
        clean_xfield = _clean_xfield(raw_xfield) if raw_xfield else None
        # Also clean any groupByFields that were inherited from the incoming report's
        # chartMeta (i.e. not parsed by _parse_aggregation_pipeline) so the LLM prompt
        # never receives raw SQL expressions as field identifiers.
        if "groupByFields" in chart_meta:
            chart_meta = {
                **chart_meta,
                "groupByFields": [_clean_xfield(g) for g in chart_meta["groupByFields"]],
            }
        # Extract metricKeys for multi-metric charts (e.g. column chart with multiple y-values).
        # Always pass them through — including a single computed field such as `percentage` —
        # so the Angular generator binds yFields to the FINAL output, not an intermediate $group key.
        metric_keys: list = chart_meta.get("metricKeys") or []
        y_fields = metric_keys if metric_keys else None

        chart_meta = {
            **chart_meta,
            "title": report_name,
            "xField": clean_xfield,
            "yField": chart_meta.get("metricKey") if pipeline else None,
            "yFields": y_fields,
        }

        widgets.append(
            DashboardWidget(
                reportId=report_id,
                reportName=report_name,
                reportType=report_type_raw,
                widgetType=widget_type,
                title=report_name,
                description=report.get("description"),
                datasource=collection,
                requiredFields=required_fields,
                query=mongo_query,
                aggregationPipeline=pipeline,
                filters=filters,
                sort=sort_spec,
                chartMeta=chart_meta,
            )
        )

    dashboard_id = f"dashboard_{project_id or uuid.uuid4().hex[:8]}"
    _assign_grid_layout(widgets)
    return DashboardDefinition(dashboardId=dashboard_id, dashboardName=dashboard_name, widgets=widgets), warnings


# ---------------------------------------------------------------------------
# Real backend function registration — one deterministic MongoDB find/aggregate
# function per widget, registered through the SAME FunctionRegistry /
# FunctionRepository / /dynamic/execute runtime contract used by ordinary
# workflow "tool" nodes. No LLM call is involved (the query is already fully
# known from build_dashboard_definition), so this is fast and 100% reliable —
# it is what makes the generated dashboard show real DB data instead of mocks.
# ---------------------------------------------------------------------------

_dashboard_backend_generator = BackendGenerator()


def _build_dashboard_query_function_source(
    func_id: str,
    widget: "DashboardWidget",
    *,
    subscriber_id: Optional[str] = None,
    field_aliases: Optional[Dict[str, str]] = None,
) -> str:
    """Author a minimal, deterministic async function matching the
    /aiqod-agent/agent/dynamic/execute runtime contract for one dashboard widget.

    When *subscriber_id* is provided the generated function will also fetch a
    sample document from the subscriber-scoped DB
    (``{main_db_name}_sb_{subscriber_id}``) to derive and return the
    collection schema alongside the query results under ``data.schema``.

    When *field_aliases* is provided (a mapping of old_name → current_name) the
    generated function will translate incoming filter keys through the alias map
    before falling back to the schema-aware stem resolver.  This ensures filters
    still work after a field is renamed in the project.
    """
    import json as _json
    from services.collection_schema import (
        get_main_db_name,
        subscriber_db_name_from_subscriber_id,
    )

    collection = widget.datasource
    pipeline = widget.aggregationPipeline or []
    projection = (widget.query or {}).get("projection") or {}
    default_limit = 500
    is_kpi = widget.widgetType == "kpi"
    metric_key = (widget.chartMeta or {}).get("metricKey") or ""

    # Embed the field alias dict as a Python literal so the generated function
    # is self-contained (no extra runtime lookups required).
    field_aliases_lit = repr(field_aliases or {})

    # Build the function body using safe string formatting instead of an f-string
    # because pipeline / projection dicts contain braces ("{", "}") that would
    # collide with f-string expression syntax.
    collection_lit   = _json.dumps(collection)
    # repr() is used for pipeline/projection so that Python literals are
    # emitted (None, True, False) instead of JSON literals (null, true, false)
    # which are not valid Python identifiers.
    pipeline_lit     = repr(pipeline)
    projection_lit   = repr(projection) if projection else _json.dumps(projection)
    is_kpi_lit       = "True" if is_kpi else "False"
    metric_key_lit   = _json.dumps(metric_key)
    # Embed the widget-level sort spec so the generated function can apply it.
    sort_spec_lit    = repr(widget.sort or {})

    # Compute the subscriber DB name at generation time and embed as a literal
    # so the generated function is self-contained (no extra imports required).
    subscriber_db_lit = _json.dumps(
        subscriber_db_name_from_subscriber_id(subscriber_id)
        if subscriber_id
        else None
    )

    template = f'''
async def {func_id}(*, params, db_wrapper=None, secure_http=None, request_meta=None):
    """Dashboard widget query for report '{widget.reportName}' ({widget.reportType})."""
    from bson import ObjectId
    from datetime import date, datetime
    from decimal import Decimal
    from services.database import Database
    from services.collection_schema import (
        fetch_latest_sample_doc_in_db,
        derive_collection_schema_from_sample,
    )

    def _serialize(value):
        if isinstance(value, dict):
            return {{k: _serialize(v) for k, v in value.items()}}
        if isinstance(value, list):
            return [_serialize(v) for v in value]
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return round(float(value), 2)
        if isinstance(value, float):
            return round(value, 2)
        return value

    # ---------------------------------------------------------------------------
    # 1. Fetch a sample document from the subscriber-scoped DB to derive the
    #    live collection schema.
    #    DB  : {{subscriber_db_lit or "main DB"}}
    #    Coll: {collection_lit}
    # ---------------------------------------------------------------------------
    _subscriber_db = {subscriber_db_lit}
    _schema: dict = {{}}
    try:
        _sample = await fetch_latest_sample_doc_in_db(
            {collection_lit}, db_name=_subscriber_db
        )
        if _sample:
            _schema = derive_collection_schema_from_sample(_sample)
    except Exception:
        pass  # Schema fetch is best-effort; never block the data query

    # ---------------------------------------------------------------------------
    # 2. Schema-aware field name resolver.
    #
    #    Resolution order:
    #      a) Field alias map (_field_aliases): handles fields renamed after generation
    #      b) Exact match against live schema
    #      c) Case-insensitive match
    #      d) Underscore/hyphen/space-normalised stem match
    #
    #    This corrects filter keys, projection fields, and pipeline field refs
    #    ($group _id, $project keys, $sort keys, $unwind paths, $addFields) so
    #    every generated query uses the exact field names that exist in the
    #    collection — making the query *proper* regardless of minor naming
    #    differences between the LLM-generated plan and the real documents.
    # ---------------------------------------------------------------------------
    _field_aliases: dict = {field_aliases_lit}

    def _resolve_field(name: str, schema: dict) -> str:
        # Check alias map first (handles fields renamed after dashboard generation)
        if _field_aliases and name in _field_aliases:
            return _field_aliases[name]
        if not schema or not isinstance(name, str):
            return name
        if name in schema:
            return name
        name_lower = name.lower()
        for k in schema:
            if k.lower() == name_lower:
                return k
        def _stem(s):
            return s.lower().replace("_", "").replace("-", "").replace(" ", "")
        name_stem = _stem(name)
        for k in schema:
            if _stem(k) == name_stem:
                return k
        return name

    def _resolve_projection(proj: dict, schema: dict) -> dict:
        if not proj or not schema:
            return proj
        return {{_resolve_field(k, schema): v for k, v in proj.items()}}

    def _resolve_filter_keys(f: dict, schema: dict) -> dict:
        if not f or not schema:
            return f
        return {{_resolve_field(k, schema): v for k, v in f.items()}}

    def _resolve_ref(ref: str, schema: dict) -> str:
        """Resolve a MongoDB field reference like \"$fieldName\" or a plain name."""
        if not ref or not isinstance(ref, str):
            return ref
        if ref.startswith("$"):
            return f"${{_resolve_field(ref[1:], schema)}}"
        return _resolve_field(ref, schema)

    def _resolve_stage(stage: dict, schema: dict) -> dict:
        """Best-effort field-name resolution inside a single aggregation stage."""
        if not schema or not isinstance(stage, dict):
            return stage
        out = {{}}
        for op, body in stage.items():
            if op == "$match":
                out[op] = _resolve_filter_keys(body, schema) if isinstance(body, dict) else body
            elif op == "$group":
                if isinstance(body, dict):
                    new_body = {{}}
                    for gk, gv in body.items():
                        if gk == "_id":
                            if isinstance(gv, str):
                                new_body[gk] = _resolve_ref(gv, schema)
                            elif isinstance(gv, dict):
                                new_body[gk] = {{
                                    rk: _resolve_ref(rv, schema) if isinstance(rv, str) else rv
                                    for rk, rv in gv.items()
                                }}
                            else:
                                new_body[gk] = gv
                        elif isinstance(gv, dict):
                            new_body[gk] = {{
                                acc: _resolve_ref(av, schema) if isinstance(av, str) else av
                                for acc, av in gv.items()
                            }}
                        else:
                            new_body[gk] = gv
                    out[op] = new_body
                else:
                    out[op] = body
            elif op == "$project":
                out[op] = _resolve_projection(body, schema) if isinstance(body, dict) else body
            elif op == "$sort":
                out[op] = _resolve_filter_keys(body, schema) if isinstance(body, dict) else body
            elif op == "$unwind":
                out[op] = _resolve_ref(body, schema) if isinstance(body, str) else body
            elif op == "$addFields":
                if isinstance(body, dict):
                    out[op] = {{
                        _resolve_field(fk, schema): _resolve_ref(fv, schema) if isinstance(fv, str) else fv
                        for fk, fv in body.items()
                    }}
                else:
                    out[op] = body
            else:
                out[op] = body
        return out

    # Widget-level sort specification embedded at code-generation time.
    _sort_spec = {sort_spec_lit}

    try:
        params = params or {{}}
        collection = Database.get_collection({collection_lit})
        pipeline = {pipeline_lit}
        raw_filters = params.get("filters") or {{}}
        projection = params.get("projection") or {projection_lit} or None
        limit = int(params.get("limit") or {default_limit})

        # Resolve projection field names against the live schema
        if projection and _schema:
            projection = _resolve_projection(projection, _schema)

        # Resolve pipeline field names against the live schema so references
        # like $group._id / $project keys / $sort keys match real field names.
        if pipeline and _schema:
            pipeline = [_resolve_stage(s, _schema) for s in pipeline]

        # Transform string filter values to case-insensitive partial-match regex
        # so that e.g. {{"location": "P"}} matches "Puducherry, India"
        def _transform_filter(fv):
            if isinstance(fv, str):
                return {{"$regex": fv, "$options": "i"}}
            if isinstance(fv, dict):
                has_mongo_op = any(k.startswith("$") for k in fv)
                if has_mongo_op:
                    return fv
                return {{k: _transform_filter(v) for k, v in fv.items()}}
            if isinstance(fv, list):
                return [_transform_filter(v) for v in fv]
            return fv

        # Resolve filter field keys against the live schema, then transform values
        resolved_raw = _resolve_filter_keys(raw_filters, _schema) if _schema else raw_filters
        filters = {{k: _transform_filter(v) for k, v in resolved_raw.items()}}

        if pipeline:
            stages = list(pipeline)
            if filters:
                stages = [{{"$match": filters}}] + stages
            # Append $sort before $limit when a sort spec is defined and the
            # pipeline does not already contain an explicit $sort stage.
            _has_sort_stage = any("$sort" in s for s in stages)
            if _sort_spec and not _has_sort_stage:
                stages.append({{"$sort": _sort_spec}})
            stages.append({{"$limit": limit}})
            # Motor returns a cursor directly; PyMongo native-async returns a
            # coroutine.  Handle both so the function works in both sandbox
            # (AsyncMongoClient) and normal (AsyncIOMotorClient) environments.
            _agg = collection.aggregate(stages)
            cursor = await _agg if hasattr(_agg, "__await__") else _agg
        else:
            cursor = collection.find(filters, projection)
            if _sort_spec:
                cursor = cursor.sort(list(_sort_spec.items()))
            cursor = cursor.limit(limit)

        items = []
        async for doc in cursor:
            items.append(_serialize(doc))

        # For KPI widgets extract the primary metric value into 'count' so
        # the frontend can display it directly without unpacking items[0].
        _is_kpi = {is_kpi_lit}
        _metric_key = {metric_key_lit}
        if _is_kpi and items:
            _first = items[0]
            _kpi_val = _first.get(_metric_key) if _metric_key else None
            if _kpi_val is None:
                # Fallback: first numeric value in the document
                for _v in _first.values():
                    if isinstance(_v, (int, float)):
                        _kpi_val = _v
                        break
            _count_out = _kpi_val if isinstance(_kpi_val, (int, float)) else len(items)
        else:
            _count_out = len(items)

        return {{
            "status": "success",
            "data": {{
                "count": _count_out,
                "items": items,
                "schema": _schema,
                "db": _subscriber_db,
                "collection": {collection_lit},
            }},
            "message": "",
            "error": None,
        }}
    except Exception as e:
        return {{
            "status": "error",
            "data": {{"count": 0, "items": [], "schema": _schema}},
            "message": str(e),
            "error": str(e),
        }}
'''
    return textwrap.dedent(template).strip() + "\n"


def _dashboard_widget_data_source(widget: "DashboardWidget") -> Dict[str, Any]:
    func_id = f"{widget.reportId}_query"
    action = "aggregate" if widget.aggregationPipeline else "find"
    return {
        "id": func_id,
        "type": "dynamic",
        "description": (
            f"Fetch data for dashboard report '{widget.reportName}' ({widget.reportType}) "
            f"from collection '{widget.datasource}'."
        ),
        "inputModel": f"{func_id}_Input",
        "responseModel": f"{func_id}_Response",
        "operation": {
            "action": action,
            "collection": widget.datasource,
            "filters": {},
            "projection": (widget.query or {}).get("projection") or {},
            "limit": 500,
        },
    }


async def _dry_run_validate_pipeline(
    collection: str,
    pipeline: list,
    subscriber_id: Optional[str],
    project_id: str,
) -> Optional[str]:
    """
    Test-execute an aggregation pipeline with a leading ``$limit: 1`` stage
    against the subscriber DB before registering the backend function.

    Returns ``None`` when the dry run succeeds (or is skipped because the
    pipeline is empty / no DB is resolvable), or a short error message string
    when the pipeline is provably invalid so the caller can log a warning.

    This is non-blocking: a failure here records a warning but does NOT
    prevent the widget function from being registered — some pipelines
    reference fields that only exist at query time, and a false-positive
    here would break otherwise-correct widgets.
    """
    if not pipeline or not collection:
        return None
    try:
        subscriber_db_name = resolve_subscriber_db_name(
            project_id=project_id,
            subscriber_id=subscriber_id,
        )
        if not subscriber_db_name:
            return None
        client = Database.client or Database.native_async_client
        if client is None:
            return None
        db = client[subscriber_db_name]
        # Append $limit:1 at the END so the full pipeline logic executes
        # (grouping, facets, etc.) before output is capped — matching what
        # the real query function does.  Prepending would limit input docs
        # to 1 before the pipeline runs and produce false passes/failures.
        probe_pipeline = list(pipeline) + [{"$limit": 1}]
        await db[collection].aggregate(probe_pipeline).to_list(length=1)
        return None
    except Exception as exc:
        return str(exc)


async def register_dashboard_backend_functions(
    *,
    project_id: str,
    dashboard_def: DashboardDefinition,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    field_aliases_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Deterministically create + register one real MongoDB-backed function per
    dashboard widget (find or aggregate — the exact query is already known
    from build_dashboard_definition, so no LLM call is used or risked).
    Returns a `backend_metadata` dict shaped exactly like the one BackendGenerator
    produces for ordinary workflow screens, so AngularAppGenerator.generate_app()
    wires the dashboard to call real /dynamic/execute functions instead of mocks.

    ``field_aliases_map`` is an optional mapping of collection_name →
    {old_field_name: current_field_name} loaded from Process_Configs.  When
    provided the aliases are embedded into each query function so filters sent
    by the Angular app still work even after a field is renamed in the project.
    """
    data_sources = [_dashboard_widget_data_source(w) for w in dashboard_def.widgets]
    if not data_sources:
        return {"base_path": None, "functions": []}

    data_models = _dashboard_backend_generator._infer_data_models_from_sources(data_sources)
    compiled_models, models_source = _dashboard_backend_generator._compile_pydantic_models(
        project_id, data_models
    )

    functions_meta: List[Dict[str, Any]] = []
    for widget, ds in zip(dashboard_def.widgets, data_sources):
        func_id = ds["id"]
        input_model_name = ds["inputModel"]
        response_model_name = ds["responseModel"]
        input_model = compiled_models.get(input_model_name)
        response_model = compiled_models.get(response_model_name)
        if input_model is None or response_model is None:
            logger.warning("[dashboard] Missing compiled model for widget function %s; skipping", func_id)
            continue

        source_code = _build_dashboard_query_function_source(
            func_id, widget, subscriber_id=subscriber_id,
            field_aliases=(field_aliases_map or {}).get(widget.datasource),
        )

        # Dry-run: test-execute the aggregation pipeline with $limit:1 before
        # registration so obvious field-name / collection errors surface early.
        if widget.aggregationPipeline:
            dry_run_err = await _dry_run_validate_pipeline(
                collection=widget.datasource,
                pipeline=widget.aggregationPipeline,
                subscriber_id=subscriber_id,
                project_id=project_id,
            )
            if dry_run_err:
                logger.warning(
                    "[dashboard] Dry-run validation failed for widget '%s' (collection: %s): %s — "
                    "registering anyway; widget may show an error at runtime.",
                    widget.reportName, widget.datasource, dry_run_err,
                )

        try:
            compiled_fn = _dashboard_backend_generator._compile_function(func_id, source_code)
        except Exception:
            logger.exception("[dashboard] Failed to compile widget function %s", func_id)
            continue

        entry = BackendFunctionEntry(
            project_id=project_id,
            function_id=func_id,
            type="dynamic",
            callable=compiled_fn,
            input_model=input_model,
            response_model=response_model,
            source_code=source_code,
            description=ds["description"],
        )
        # Additive registration — never clear the project's function registry here,
        # since a dashboard tied to a parent workflow project_id must not wipe out
        # that workflow's other tool functions.
        FunctionRegistry.register(project_id, func_id, entry)
        widget.functionId = func_id

        try:
            await _dashboard_backend_generator.function_repository.save_function(
                project_id=project_id,
                function_id=func_id,
                function_type="dynamic",
                source_code=source_code,
                input_model_name=input_model_name,
                response_model_name=response_model_name,
                models_source=models_source,
                description=ds["description"],
                metadata={"data_source": ds, "compiled_models": list(compiled_models.keys())},
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                create_new_version=True,
            )
        except Exception:
            logger.exception(
                "[dashboard] Failed to persist widget function %s for project %s (still usable this session)",
                func_id, project_id,
            )

        functions_meta.append({
            "id": func_id,
            "type": "dynamic",
            "description": ds["description"],
            "input_model": input_model_name,
            "response_model": response_model_name,
            # Widget schema contract — passed to the Angular generator so the
            # frontend is generated from the backend-determined field shapes.
            "widgetType": widget.widgetType,
            "reportType": widget.reportType,
            "datasource": widget.datasource,
            "requiredFields": widget.requiredFields,
            "filterFields": [f.get("field", "") for f in widget.filters if f.get("field")],
            "chartMeta": widget.chartMeta,
            "gridRow": widget.gridRow,
            "gridCol": widget.gridCol,
            "gridWidth": widget.gridWidth,
            "gridHeight": widget.gridHeight,
        })

    relative_path = f"/aiqod-agent/agent/dynamic/execute/{project_id}"
    base_url = _dashboard_backend_generator.dynamic_execute_base_url
    base_path = f"{base_url}{relative_path}" if base_url else relative_path

    return {
        "base_path": base_path,
        "project_id": project_id,
        "api_base_url": base_url,
        "functions": functions_meta,
    }


class CreateDashboardScreenRequest(BaseModel):
    workflowId: str = Field(..., description="Workflow identifier (used as project prefix)")
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None
    projectId: Optional[str] = None
    reports: List[Dict[str, Any]] = Field(default_factory=list, description="List of report definitions")
    # Caller-supplied collection schema; merged with any schema stored in
    # Process_Configs so the LLM enhancement and validation see real field names.
    collectionSchema: Optional[List[Dict[str, Any]]] = None


class ReportScreenItem(BaseModel):
    screenId: str
    screenName: str
    reportName: str
    reportType: str
    success: bool
    error: Optional[str] = None


class CreateDashboardScreenResponse(BaseModel):
    success: bool = True
    workflowId: str
    projectId: str
    screens: List[ReportScreenItem] = Field(default_factory=list)
    dashboardScreenId: Optional[str] = None
    dashboardUrl: Optional[str] = None
    message: str = "Dashboard screens created successfully"
    # Non-fatal warnings produced during dashboard definition building
    # (e.g. unknown reportType, missing required fields).
    warnings: List[str] = Field(default_factory=list)
    # Full widget/query/aggregation definition used to generate the screen —
    # populated by generate_dashboard_screens_for_reports().
    dashboard: Optional[DashboardDefinition] = None


class ReportDownloadRequest(BaseModel):
    """Request body for POST /dashboard-analyzer/reports/download."""
    projectId: Optional[str] = None
    collectionName: str
    aggregationPipeline: Optional[List[Dict[str, Any]]] = None
    filters: Optional[Dict[str, Any]] = None          # {field: value} applied as $match
    format: str = "csv"                                # "csv" | "json"
    subscriberId: Optional[str] = None
    orgId: Optional[str] = None
    limit: int = Field(default=10000, ge=1, le=50000)  # max rows in a single download


class UpdateDashboardScreenRequest(BaseModel):
    """Request body for POST /dashboard-analyzer/update-screen."""
    projectId: str
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None
    workflowId: Optional[str] = None
    dashboardName: Optional[str] = None
    # New reports to add (may be free-text descriptions or fully structured).
    # Unstructured ones are passed through the LLM for enhancement before processing.
    reports: List[Dict[str, Any]] = Field(default_factory=list)
    # Optimistic concurrency control: if provided and it differs from the stored
    # dashboard_version the endpoint returns HTTP 409 Conflict.
    version: Optional[int] = None
    # Caller-supplied collection schema merged with any schema already in
    # Process_Configs so the LLM enhancement and validation use real field names.
    collectionSchema: Optional[List[Dict[str, Any]]] = None


class UpdateDashboardScreenResponse(BaseModel):
    """Response for POST /dashboard-analyzer/update-screen."""
    success: bool = True
    projectId: str
    dashboardScreenId: Optional[str] = None
    dashboardUrl: Optional[str] = None
    dashboardName: Optional[str] = None
    dashboard: Optional[DashboardDefinition] = None
    screens: List[ReportScreenItem] = Field(default_factory=list)
    version: int = 1          # New dashboard version after this update
    warnings: List[str] = Field(default_factory=list)
    message: str = ""


REPORT_ANALYSIS_SYSTEM_PROMPT = """You are acting as a senior Business Intelligence Analyst whose responsibility is to analyze business workflows and identify valuable reports and dashboards that can be generated from the data captured within those workflows.

Analyze the provided workflow definition and generate a list of meaningful business reports and dashboards that would provide actionable insights to business users.

Your recommendations must be based solely on the workflow steps, sub-steps, collected data, database operations and database collection(s) described in the workflow.

The objective is to identify reports that help users monitor business performance, track operational activities, analyze stored data, and support decision-making.

Minimum Report Requirement
Generate at least 10 reports whenever the workflow contains sufficient data to support them.
If the workflow reasonably supports more than 10 reports, generate all meaningful reports without limiting the output.
Only return fewer than 10 reports if the workflow genuinely does not contain enough explicit information. Do not invent entities, fields, relationships, or calculations simply to reach the minimum.

Rules

### A. Report Generation Rules

1. Generate every meaningful report that is directly supported by the workflow.
2. Explore all valid combinations of:
   - collected fields
   - workflow steps
   - CRUD operations
   - business entities
   - collections
3. Generate multiple reports from the same collection whenever they answer different business questions.
4. Prefer comprehensive coverage rather than a minimal report list.
5. Reports should include operational, analytical, tactical and executive perspectives whenever supported.
6. If the same data can be visualized in multiple meaningful ways (Table, Bar Chart, Pie Chart, Dashboard, KPI Cards, Heatmap, Matrix, etc.), generate each as a separate report if it provides a different business insight.
7. Combine workflow steps only when they describe the same business entity or business process.
8. Never invent fields, entities, relationships or calculations that are not explicitly supported.
9. Description must always mention the collection name(s) used as in the workflow description (eg:- Collection name=Candidate_Master).
10. Avoid exact duplicates, but do not suppress reports simply because they use some of the same fields.
### A1. Report Discovery Strategy

For every collection and business entity, identify all possible report opportunities including but not limited to:

- Total records
- Record listings
- Status distribution
- Category distribution
- Type distribution
- Priority distribution
- Owner/Assigned User workload
- Department-wise analysis
- Location-wise analysis
- Date-wise trends
- Monthly trends
- Weekly trends
- Daily trends
- Recent activity
- Lifecycle tracking
- Stage progression
- Approval progress
- Completion analysis
- Pending work
- Exception reports
- Missing information
- Duplicate detection (if supported)
- Update activity
- Creation trends
- Relationship summaries
- Cross-tab analysis
- Distribution analysis
- Frequency analysis
- KPI summaries
- Executive dashboards

Generate every report that can be supported solely by the available workflow data.

### B. Supported Report Types

Use only one of the following values:

- Tabular
- Bar Chart
- Column Chart
- Line Chart
- Pie Chart
- Donut Chart
- Area Chart
- KPI Cards

### C. Report Design Rules

Each report must contain:

- reportName
  - Short and business-friendly.
  - Examples:
    - Customer Summary
    - Order Status Report
    - Employee Activity Dashboard

- description
  - Always include the collection name(s) as mentioned in the workflow description from which the report is derived.
  - Explain what the report displays.
  - Explain why the report is useful for business users.

- reportType
  - Must be exactly one of:
    - Tabular
    - Bar Chart
    - Column Chart
    - Line Chart
    - Pie Chart
    - Donut Chart
    - Area Chart
    - KPI Cards

- requiredFields
  - Include only fields explicitly available from the workflow.
  - These fields are mandatory for constructing the report.

- optionalFilters
  - Include fields that users could reasonably use to filter the report.
  - Examples:
    - Status
    - Date
    - Category
    - Assigned User
    - Department
  - Only include filters if those fields exist in the workflow.

- aggregation
  - Provide a valid MongoDB aggregation pipeline array (list of dicts) if this report is an aggregate report (like a bar, column, line, pie, donut, or area chart or a KPI count card).
  - Stage operator keys MUST use a SINGLE dollar sign prefix: $group, $sort, $project, $match, $limit, etc.
  - NEVER use double-dollar-sign ($$) as a stage operator prefix. $$ is reserved for MongoDB variable references inside expressions (e.g. $$ROOT, $$CURRENT) — it is NOT an operator prefix.
  - Always project the grouped field(s) and the computed metric/value field(s) in a `$project` stage at the end of the pipeline so they have clean, descriptive names.
  - Examples:
    - `[{"$group": {"_id": "$status", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$project": {"_id": 0, "status": "$_id", "count": 1}}]`
    - `[{"$group": {"_id": "$month", "total_amount": {"$sum": "$amount"}}}, {"$sort": {"total_amount": -1}}, {"$project": {"_id": 0, "month": "$_id", "total_amount": 1}}]`
  - If aggregation is not applicable (e.g. for a plain tabular report that lists raw items), use null.

### D. Dashboard Rules

A Dashboard should combine multiple metrics or summaries derived from the workflow.

Examples include:
- Total records
- Records by status
- Recent activity
- Monthly trends
- Category distribution
- User workload

Only recommend dashboards when the workflow supports multiple meaningful visualizations.

### E. CRUD Awareness

Consider the database operations described in the workflow:

- Insert → creation reports, growth trends, new records
- Update → status changes, progress tracking, lifecycle reports
- Delete → deletion summaries or audit reports (only if deletion data exists)
- Find/Retrieve → lookup or operational activity reports only if relevant

### F. Field Usage Rules

Only use fields that are explicitly mentioned in the workflow.

Do not invent:
- IDs
- timestamps
- statuses
- amounts
- users
- locations
- categories
or any other attributes unless they are explicitly described.
### F1. Report Combination Rules

For every supported entity, attempt to generate reports across all applicable visualization types.

For example, if Status exists, valid reports include:

- Status Report (Tabular)
- Status Distribution (Pie Chart)
- Status Comparison (Bar Chart)
- Status Dashboard
- Status KPI Cards

These are considered distinct reports because they answer different business questions.

### G. Quality Rules

Each report should answer a unique business question.

Different reports may use the same fields if they provide different analytical value.

Examples include:

- What records currently exist?
- How are records distributed?
- How are records changing over time?
- Which groups have the highest volume?
- Which items require attention?
- Which users have the greatest workload?
- Which categories dominate?
- What is the progress through the workflow?
- What trends are emerging?
- What operational bottlenecks exist?

Only eliminate reports that are true duplicates.

### H. Output Constraints

- Return ONLY valid JSON.
- Do not wrap the JSON in markdown.
- Do not include explanations.
- Do not include notes.
- Do not include reasoning.
- Ensure the output strictly follows the required schema.
- If no meaningful reports are supported by the workflow, return:
  {
    "reports": []
  }"""


def _build_analysis_prompt(workflow: dict[str, Any]) -> str:
    workflow_name = workflow.get("workflowName", "Unnamed Workflow")
    steps = workflow.get("steps", [])

    steps_text_parts: list[str] = []
    for i, step in enumerate(steps, start=1):
        step_name = step.get("name", f"Step {i}")
        step_desc = step.get("description", "")
        phase_type = step.get("phaseType", "")
        sub_steps = step.get("subSteps", [])

        steps_text_parts.append(f"\n--- Step {i}: {step_name} ---")
        if step_desc:
            steps_text_parts.append(f"  Description: {step_desc}")
        if phase_type:
            steps_text_parts.append(f"  Phase Type: {phase_type}")

        for j, sub in enumerate(sub_steps, start=1):
            sub_name = sub.get("name", f"Sub-step {j}")
            sub_desc = sub.get("description", "")
            steps_text_parts.append(f"  Sub-step {j}: {sub_name}")
            if sub_desc:
                steps_text_parts.append(f"    Description: {sub_desc}")

    steps_text = "\n".join(steps_text_parts)

    prompt = f"""
###Input

You will receive the following input:

Workflow Name:
{workflow_name}

Workflow Steps and Sub-Steps:
{steps_text}

Each workflow contains:
- Steps
- Sub-steps
- Descriptions
- User actions
- Data being collected
- CRUD/database operations
- Business entities
- Fields being captured or updated
- Relationships between entities (if any)

The descriptions explain:
- What users do
- What information is entered
- What records are created, updated, deleted, or retrieved
- What business process is being executed

###Output

Return ONLY valid JSON matching the following schema:
CRITICAL INSTRUCTION: You MUST generate AT LEAST 10 distinct reports.
To reach this number, slice the data across various dimensions (e.g., geographic distribution, skill frequency, experience buckets, JD match success rates, KPIs, and time-series analysis of shortlists). Use different reportType representations where appropriate.

{{
  "reports": [
    {{
      "reportName": "string",
      "description": "string (Include the collection name(s) as from which the report is derived. Explain what the report displays and why it is useful for business users.)",
      "reportType": "Tabular | Bar Chart | Column Chart | Line Chart | Pie Chart | Donut Chart | Area Chart | KPI Cards",
      "collectionName": "string (The MongoDB collection name this report is derived from)",
      "optionalFilters": [
        "field1",
        "field2"
      ],
      "aggregation": "array of objects | null"
    }}
  ]
}}

If no meaningful reports can be generated, return:

{{
  "reports": []
}}

Do NOT include markdown, explanations, comments, or additional text outside the JSON.
"""
    return prompt


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def derive_business_context(
    workflow_dict: dict[str, Any]
) -> BusinessSemanticModel:
    steps = workflow_dict.get("steps", [])
    steps_text_parts: list[str] = []
    for i, step in enumerate(steps, start=1):
        step_name = step.get("name", f"Step {i}")
        step_desc = step.get("description", "")
        phase_type = step.get("phaseType", "")
        sub_steps = step.get("subSteps", [])

        steps_text_parts.append(f"\n--- Step {i}: {step_name} ---")
        if step_desc:
            steps_text_parts.append(f"  Description: {step_desc}")
        if phase_type:
            steps_text_parts.append(f"  Phase Type: {phase_type}")

        for j, sub in enumerate(sub_steps, start=1):
            sub_name = sub.get("name", f"Sub-step {j}")
            sub_desc = sub.get("description", "")
            steps_text_parts.append(f"  Sub-step {j}: {sub_name}")
            if sub_desc:
                steps_text_parts.append(f"    Description: {sub_desc}")
    steps_text = "\n".join(steps_text_parts)

    workflow_name = workflow_dict.get("workflowName", "Unnamed Workflow")

    stage1_prompt = f"""You are a business context extractor. Analyze the following workflow and derive its business semantic metadata.

Workflow Name: {workflow_name}
Workflow Steps:
{steps_text}

Extract the following metadata:
- domain: The overarching business domain (e.g., HR, Sales, Procurement).
- industry: The typical industry for this workflow.
- capability: The core business capability (e.g., Talent Acquisition, Order Management).
- subdomain: Specific subdomain of capability.
- purpose: The primary business purpose/objective of the workflow.
- start_event: The event that triggers this workflow.
- end_event: The final outcome/event of this workflow.
- primary_actor: The main user/persona executing the workflow.
- secondary_actors: Other actors/personas involved.
- business_goals: Primary business goals/objectives of the process.
- stakeholders: Key business stakeholders interested in this process or its outcomes.
- entities: Principal business entities/data objects captured or manipulated (e.g., Candidate, JobOrder, Customer).
- events: Key business events occurring during the flow (e.g., Candidate Applied, Job Offered).
- decision_points: Major logical decision gates or approvals.

Leave the kpis field as an empty list (do not attempt to generate KPIs in this stage).
"""

    response1 = await async_generate(
        _SUGGEST_REPORTS_MODEL,
        stage1_prompt,
        config=GeminiGenerateConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=BusinessSemanticModel,
        ),
    )
    _record_and_charge_suggest_reports_llm(
        response1, model=_SUGGEST_REPORTS_MODEL, sub_operation="business-context",
    )

    parsed1: BusinessSemanticModel | None = getattr(response1, "parsed", None)
    if parsed1 is None:
        text = getattr(response1, "text", None) or str(response1)
        try:
            data = json.loads(text)
            parsed1 = BusinessSemanticModel(**data)
        except Exception as e:
            logger.warning("Stage 1 LLM did not return valid JSON: %s", e)
            parsed1 = BusinessSemanticModel()
            try:
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    parsed1 = BusinessSemanticModel(**json.loads(match.group(0)))
            except Exception:
                pass

    return parsed1


async def enhance_report_descriptions(
    user_reports: List[Dict[str, Any]],
    collection_schema: Optional[Any] = None,
    workflow_name: str = "Dashboard",
) -> List[Dict[str, Any]]:
    """
    Pass user-provided report descriptions through the LLM to generate structured,
    fully-specified report definitions.

    When users add reports to a dashboard update they typically provide a short
    free-text description (e.g. "show revenue by region") rather than a fully
    structured report with ``reportType``, ``collectionName``, and an aggregation
    pipeline. This function uses Gemini to turn those descriptions into the same
    shape as LLM-generated suggestions so they can flow through
    ``build_dashboard_definition`` without any missing fields.

    Already-structured reports (those that already have ``reportType`` and
    ``collectionName`` set) are returned unchanged.
    """
    if not user_reports:
        return user_reports

    # Pre-process: normalise obviously-empty aggregation/collectionName so
    # the merge step doesn't accidentally keep empty strings as "user intent".
    for rep in user_reports:
        if rep.get("aggregation") == "" or rep.get("aggregation") == []:
            rep["aggregation"] = None
        if rep.get("collectionName") == "":
            rep["collectionName"] = None

    # Separate reports that already have full structure from those that need enhancement
    needs_enhancement: List[Dict[str, Any]] = []
    already_structured: List[Dict[str, Any]] = []
    idx_map: Dict[int, bool] = {}  # original index → needs enhancement

    for i, rep in enumerate(user_reports):
        has_structure = bool(
            rep.get("reportType")
            and rep.get("collectionName")
        )
        idx_map[i] = not has_structure
        if not has_structure:
            needs_enhancement.append(rep)
        else:
            already_structured.append(rep)

    if not needs_enhancement:
        return user_reports

    # Build schema context for the prompt — collect available collection names
    # as an explicit allow-list that the LLM MUST honour.
    schema_section = ""
    available_collection_names: List[str] = []
    if collection_schema:
        schema_parts = ["Available database collections and their fields:"]
        if isinstance(collection_schema, dict):
            for coll_name, coll_info in collection_schema.items():
                if coll_name == "__meta__":
                    continue
                available_collection_names.append(coll_name)
                fields = (coll_info or {}).get("fields") or {}
                if isinstance(fields, dict) and fields:
                    type_hints = ", ".join(f"{k} ({v})" for k, v in sorted(fields.items())[:30])
                    schema_parts.append(f"  - {coll_name}: {type_hints}")
                else:
                    schema_parts.append(f"  - {coll_name}: (no field info)")
        elif isinstance(collection_schema, list):
            for entry in collection_schema:
                if isinstance(entry, dict):
                    coll_name = entry.get("collection_name") or entry.get("name", "")
                    coll_fields = entry.get("schema") or entry.get("fields") or {}
                    if coll_name:
                        available_collection_names.append(coll_name)
                    if coll_name and isinstance(coll_fields, dict) and coll_fields:
                        type_hints = ", ".join(f"{k} ({v})" for k, v in sorted(coll_fields.items())[:30])
                        schema_parts.append(f"  - {coll_name}: {type_hints}")
                    elif coll_name:
                        schema_parts.append(f"  - {coll_name}: (no field info)")
        schema_section = "\n".join(schema_parts)

    collection_allow_list = (
        "\n\nALLOWED collection names (collectionName MUST be EXACTLY one of these):\n"
        + "\n".join(f"  - {n}" for n in available_collection_names)
        if available_collection_names
        else ""
    )

    # Include the full original report context (not just the description) so
    # the LLM can see the user's intended reportType and any partial fields.
    reports_text = json.dumps(
        [
            {
                "index": i,
                "reportName": r.get("reportName") or "",
                "description": r.get("description") or r.get("reportName") or str(r),
                "requestedReportType": r.get("reportType") or "",   # hint to LLM
                "partialCollectionName": r.get("collectionName") or "",
                "partialAggregation": r.get("aggregation") or None,
                "optionalFilters": r.get("optionalFilters") or [],
            }
            for i, r in enumerate(needs_enhancement)
        ],
        indent=2,
    )

    prompt = f"""You are a Business Intelligence report designer.

Dashboard: {workflow_name}
{schema_section}{collection_allow_list}

The user wants to add these reports to the dashboard. Each entry includes:
- "index": position in the list (preserve exactly)
- "reportName": the name the user gave this report
- "description": what the report should show
- "requestedReportType": the chart/table type the user asked for — HONOUR this unless it is empty
- "partialCollectionName": a partial or empty collection name hint
{reports_text}

For EACH report, produce a fully structured definition:
- reportName: use the user's reportName as-is (do NOT change it)
- reportType: use "requestedReportType" if it is not empty and is one of the allowed values; otherwise infer the most appropriate type. Allowed values: "Tabular", "Bar Chart", "Column Chart", "Line Chart", "Pie Chart", "Donut Chart", "Area Chart", "KPI Cards"
- collectionName: MUST be EXACTLY one of the allowed collection names listed above. Choose the one whose fields best match the report's intent. NEVER invent a collection name.
- requiredFields: List every field that will appear in the query result, in display order.
    For Tabular / find reports: every column the user will see (the projection). Use exact field names from the schema. NEVER leave empty for Tabular reports.
    For aggregation reports: every output field produced by the final $project stage.
- description: A structured description in the following format:
    Line 1: One concise sentence summarising what the report shows (business value).
    Then a blank line, then bullet points explaining each query step in plain English, for example:
    • Queries the <collectionName> collection
    • Filters records where <field> = <value>  (omit if no filter)
    • Groups results by <field>
    • Calculates <aggregation> of <field>
    • Sorts by <field> descending  (omit if no sort)
    • Returns the top <N> results  (omit if no limit)
    Keep bullet points concise (≤ 12 words each). Use actual field/collection names where known.
- optionalFilters: list of field names that would make useful filters (empty list if none)
- aggregation: MongoDB aggregation pipeline as a JSON array using fields from the chosen collection, or null for a plain find. For "KPI Cards" type always provide an aggregation that produces one or more numeric summary values.

Return a JSON object:
{{
  "reports": [
    {{ "index": 0, "reportName": "...", "reportType": "...", "collectionName": "...", "requiredFields": ["field1", "field2"], "description": "...", "optionalFilters": [], "aggregation": null }},
    ...
  ]
}}
"""

    try:
        response = await async_generate(
            "gemini-2.5-flash",
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        raw = getattr(response, "text", None) or str(response)
        parsed = json.loads(raw)
        enhanced_list: List[Dict[str, Any]] = parsed.get("reports") or []
        # Merge enhanced definitions back into the originals by index
        enhanced_by_idx = {item["index"]: item for item in enhanced_list if "index" in item}
        enh_counter = 0
        for i, rep in enumerate(user_reports):
            if idx_map.get(i, False):
                enh = enhanced_by_idx.get(enh_counter) or {}
                enh_counter += 1
                if enh:
                    # Start with LLM-generated structure, then overlay user-provided
                    # fields that are non-empty.  Special-case: reportType and reportName
                    # are ALWAYS preserved from the user's original when they were set
                    # (even if the LLM would have picked a different value).
                    # "description" is intentionally excluded from the user-override so
                    # the LLM-generated structured description (with query-plan bullets)
                    # is always used instead of the original free-text description.
                    merged = {**enh, **{k: v for k, v in rep.items() if v and k != "description"}}
                    # Always keep the user's reportType if it was originally specified
                    original_type = rep.get("reportType")
                    if original_type:
                        merged["reportType"] = original_type
                    original_name = rep.get("reportName")
                    if original_name:
                        merged["reportName"] = original_name
                    # Guard: if the LLM chose a collection not in the schema, warn and
                    # fall back to the first available collection.
                    if available_collection_names:
                        llm_coll = merged.get("collectionName", "")
                        if llm_coll and llm_coll not in available_collection_names:
                            logger.warning(
                                "enhance_report_descriptions: LLM returned unknown collection '%s' "
                                "(not in schema); falling back to first available collection '%s'",
                                llm_coll, available_collection_names[0],
                            )
                            merged["collectionName"] = available_collection_names[0]
                    user_reports[i] = merged
        return user_reports
    except Exception as exc:
        logger.warning(
            "enhance_report_descriptions: LLM call failed (%s) — using original reports as-is", exc
        )
        return user_reports


async def generate_reports_from_semantic_model(
    semantic_model: BusinessSemanticModel,
    workflow_dict: Optional[dict[str, Any]] = None,
    collection_schema_dict: Optional[Any] = None,
) -> list[ReportSuggestion]:
    reference_data = ""
    if workflow_dict:
        steps_summary = []
        for i, step in enumerate(workflow_dict.get("steps", []), start=1):
            name = step.get("name", "")
            desc = step.get("description", "")

            substep = step.get("subSteps", {})

            if isinstance(substep, dict):
                substep_name = substep.get("name", "")
                substep_desc = substep.get("description", "")
                substeps_text = f"{substep_name} ({substep_desc})"
            elif isinstance(substep, list):
                substeps_text = "\n".join(
                f"{item.get('name', '')} ({item.get('description', '')})"
                for item in substep
                if isinstance(item, dict)
                )

            else:
                substeps_text = ""

            steps_summary.append(
                f"Step {i}: {name} ({desc}). Substeps: {substeps_text}"
            )
        reference_data = "\nAvailable Workflow Reference (use these actual collection names & fields in reports):\n" + "\n".join(steps_summary)

    # ── Parse enum values and FK references from schema field type strings ──────────────
    # Enum pattern  : "string, enum options are ['val1', 'val2']"
    # FK pattern    : "string, reference of <collection> collection field <field>"
    import re as _re

    # enum_fields_map  : { collection_name: { field_name: [allowed_values] } }
    # declared_joins   : [ {"from_coll":..,"local_field":..,"foreign_coll":..,"foreign_field":..} ]
    enum_fields_map: dict[str, dict[str, list[str]]] = {}
    declared_joins: list[dict[str, str]] = []

    def _parse_schema_annotations(coll_name: str, fields: dict) -> None:
        """Extract enum options and FK references from field type strings."""
        for field_name, ftype in fields.items():
            if not isinstance(ftype, str):
                continue
            # Enum options
            enum_match = _re.search(r"enum options are \[([^\]]+)\]", ftype, _re.IGNORECASE)
            if enum_match:
                raw = enum_match.group(1)
                values = [v.strip().strip("'\"") for v in raw.split(",") if v.strip()]
                enum_fields_map.setdefault(coll_name, {})[field_name] = values
            # Foreign-key reference: "reference of <foreign_coll> collection field <foreign_field>"
            ref_match = _re.search(
                r"reference of\s+(\S+)\s+collection\s+field\s+(\S+)", ftype, _re.IGNORECASE
            )
            if ref_match:
                declared_joins.append({
                    "from_coll": coll_name,
                    "local_field": field_name,
                    "foreign_coll": ref_match.group(1),
                    "foreign_field": ref_match.group(2),
                })

    # Build collection schema reference for the prompt
    schema_section = ""
    allowed_collection_names: set[str] = set()  # populated when collection_schema_dict is present
    if collection_schema_dict:
        schema_parts = ["\nDATABASE COLLECTION SCHEMAS (actual fields in each collection):"]
        if isinstance(collection_schema_dict, dict):
            for coll_name, coll_info in (collection_schema_dict or {}).items():
                if coll_name == "__meta__":
                    continue
                allowed_collection_names.add(coll_name)
                fields = (coll_info or {}).get("fields") or {}
                if fields:
                    _parse_schema_annotations(coll_name, fields)
                    type_hints = ", ".join(f"{k} ({v})" for k, v in sorted(fields.items())[:40])
                    schema_parts.append(f"  - {coll_name}: {type_hints}")
                else:
                    schema_parts.append(f"  - {coll_name}: (no sample data available)")
        else:
            for coll in collection_schema_dict:
                coll_name = coll.get("collection_name")
                coll_info = coll.get("schema")
                if coll_name == "__meta__":
                    continue
                if coll_name:
                    allowed_collection_names.add(coll_name)
                if isinstance(coll_info, dict):
                    fields = coll_info
                    if fields:
                        _parse_schema_annotations(coll_name, fields)
                        type_hints = ", ".join(f"{k} ({v})" for k, v in sorted(fields.items())[:40])
                        schema_parts.append(f"  - {coll_name}: {type_hints}")
                    else:
                        schema_parts.append(f"  - {coll_name}: (no sample data available)")
                else:
                    schema_parts.append(f"  - {coll_name}: (schema info unavailable)")

        # ── Enum summary block ────────────────────────────────────────────────────────────
        if enum_fields_map:
            schema_parts.append("\nFIELDS WITH KNOWN ENUM VALUES (you MAY use these exact values in $match stages):")
            for coll_name, field_enums in sorted(enum_fields_map.items()):
                for field_name, values in sorted(field_enums.items()):
                    vals_str = ", ".join(f'"{v}"' for v in values)
                    schema_parts.append(f"  - {coll_name}.{field_name}: [{vals_str}]")

        # ── Declared foreign-key / join reference block ───────────────────────────────────
        if declared_joins:
            schema_parts.append("\nDECLARED FOREIGN KEY REFERENCES (you MAY use $lookup ONLY for these exact joins):")
            for j in declared_joins:
                schema_parts.append(
                    f"  - {j['from_coll']}.{j['local_field']}  →  {j['foreign_coll']}.{j['foreign_field']}"
                )

        # Append an explicit whitelist so the LLM cannot miss it
        if allowed_collection_names:
            schema_parts.append(
                f"\nALLOWED COLLECTIONS (STRICT WHITELIST): {', '.join(sorted(allowed_collection_names))}"
            )
        schema_section = "\n".join(schema_parts)

    # Build the collection constraint block injected prominently into the prompt
    collection_constraint = ""
    if allowed_collection_names:
        allowed_list = ", ".join(f'"{c}"' for c in sorted(allowed_collection_names))
        collection_constraint = f"""
⚠️ STRICT COLLECTION WHITELIST — NON-NEGOTIABLE ⚠️
The ONLY permitted collection names are: {allowed_list}
You MUST NOT use any other collection name under any circumstances.
Do NOT predict, invent, or assume additional collections exist.
Every report's "collectionName" MUST exactly match one of the names listed above.
Any report referencing a collection not in this whitelist will be discarded.
"""

    prompt = f"""You are acting as a senior Business Intelligence Analyst whose responsibility is to analyze business workflows/context and identify valuable reports and dashboards.
{collection_constraint}
Based on the Business Semantic Model below, generate a list of meaningful reports:
- Domain: {semantic_model.domain}
- Industry: {semantic_model.industry}
- Capability: {semantic_model.capability}
- Subdomain: {semantic_model.subdomain}
- Purpose: {semantic_model.purpose}
- Business Goals: {", ".join(semantic_model.business_goals)}
- Stakeholders: {", ".join(semantic_model.stakeholders)}
- Entities: {", ".join(semantic_model.entities)}
- Events: {", ".join(semantic_model.events)}
- Decision Points: {", ".join(semantic_model.decision_points)}
{reference_data}
{schema_section}

Rules:
1. Generate every meaningful report that is directly supported by the DATABASE COLLECTION SCHEMAS above. Explore all valid combinations of the actual fields listed.
2. CRITICAL INSTRUCTION: You MUST generate AT LEAST 10 distinct reports. Slice the data across various dimensions (e.g. status distribution, workload, trends, KPIs, and locations). Use different reportType representations where appropriate.
3. Supported reportType: Tabular | Bar Chart | Column Chart | Line Chart | Pie Chart | Donut Chart | Area Chart | KPI Cards.
4. Description MUST follow this exact format:
   Line 1: One concise sentence summarising the business value of the report.
   Blank line.
   Then bullet points (use •) explaining the query plan with actual field/collection names:
   • Collection: <collectionName>
   • Operation: find  (for Tabular reports without aggregation pipeline)
   • Operation: aggregate  (for Chart / KPI reports with a pipeline)
   • Projected fields: <field1>, <field2>, ...  (list every returned field)
   • Filters: <field> = <value>  (omit if no match/filter stage)
   • Groups by: <field>  (omit if no $group stage)
   • Sorts by: <field> desc  (omit if no $sort stage)
   • Returns top <N> results  (omit if no $limit stage)
   Keep each bullet ≤ 12 words. Use exact collection and field names from the schema.
5. ABSOLUTE RULE: Every "collectionName" MUST be one of the names from the ALLOWED COLLECTIONS WHITELIST above. Do NOT invent or predict any collection name that is not explicitly listed. Use only the exact field names shown in the DATABASE COLLECTION SCHEMAS for "requiredFields" and "optionalFilters".
6. If the report type is not Tabular (i.e. it is a Chart or KPI Card), provide a valid MongoDB aggregation pipeline as a JSON array (list of dicts) in the "aggregation" field. The pipeline must use standard MongoDB stage operators with a SINGLE dollar sign: $group, $sort, $project, $match, $limit, etc. NEVER use double-dollar-sign ($$) as a stage operator prefix — $$ is reserved for MongoDB variable references inside expressions only (e.g. $$ROOT). A $project stage at the end should project the group key and aggregated value to clean output names. If not applicable (e.g. for Tabular reports), use null.
7. Output the aggregation as a valid JSON array string. Use JSON boolean and null literals (true, false, null) — NOT Python literals (True, False, None).
8. For each report, set the "collectionName" field to the exact MongoDB collection name the report is derived from. This MUST be one of the whitelisted names above.
9. "requiredFields": List every field that will appear in the query result, in display order.
   - For Tabular / find reports: list every column the user will see (i.e. the projection fields).
   - For aggregation reports: list every output field produced by the final $project stage.
   - Use exact field names from the DATABASE COLLECTION SCHEMAS. NEVER leave this empty for Tabular reports.
10. CROSS-COLLECTION JOINS ($lookup) — DECLARED REFERENCES ONLY:
    - By default, do NOT use $lookup. The schema usually lists only field names and types without defining relationships.
    - EXCEPTION: If the schema section above lists a "DECLARED FOREIGN KEY REFERENCES" block, you MAY use $lookup ONLY for those exact joins (same from-collection, local field, foreign collection, and foreign field as declared). Do NOT infer any other joins.
    - Any $lookup that is NOT listed in the declared references will be automatically discarded.
11. HARDCODED VALUES IN $match STAGES — ENUM FIELDS ONLY:
    - By default, do NOT hardcode string equality values in $match stages because the schema does not list stored values and predictions will produce empty results.
    - EXCEPTION: If the schema section above lists a "FIELDS WITH KNOWN ENUM VALUES" block, you MAY use those EXACT listed values in $match stages for those specific fields. Do NOT use any other string values beyond what is listed.
    - The ONLY other values permitted in $match stages are: null (existence check), numeric comparisons ($gt/$lt/$gte/$lte), and boolean true/false for fields whose schema type is "boolean"/"bool".
    - For any plain "string" field NOT listed in the enum block, use $group to show its full value distribution instead of filtering on a guessed value.
12. $bucket BOUNDARIES — FINITE SAME-TYPE VALUES ONLY:
    - NEVER put Infinity, -Infinity, NaN, or null in $bucket.boundaries. JSON serializes Infinity to null, and MongoDB then fails with "conflicting types int and null".
    - Every boundaries value MUST be a finite number (or all dates). Use at least two strictly ascending values of the SAME type.
    - Open-ended last buckets (e.g. 61+) belong in the $bucket "default" field, NOT as Infinity/null in boundaries.
    - Example: {{"$bucket": {{"groupBy": "$age", "boundaries": [0, 19, 36, 61], "default": "61+", "output": {{"count": {{"$sum": 1}}}}}}}}
    - Prefer $group for categorical fields. Any $bucket whose boundaries contain null/Infinity/mixed types will be discarded.
"""

    response = await async_generate(
        _SUGGEST_REPORTS_MODEL,
        prompt,
        config=GeminiGenerateConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=ReportSuggestionList,
        ),
    )
    _record_and_charge_suggest_reports_llm(
        response, model=_SUGGEST_REPORTS_MODEL, sub_operation="generate-reports",
    )

    parsed: ReportSuggestionList | None = getattr(response, "parsed", None)
    if parsed is None:
        text = getattr(response, "text", None) or str(response)
        try:
            data = json.loads(text)
            parsed = ReportSuggestionList(**data)
        except Exception as e:
            logger.warning("Stage 3 LLM did not return valid JSON: %s", e)
            parsed = ReportSuggestionList(reports=[])
            try:
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    parsed = ReportSuggestionList(**json.loads(match.group(0)))
            except Exception:
                pass

    # Post-filter: remove any report whose collectionName is not in the whitelist
    if allowed_collection_names and parsed.reports:
        filtered_reports = []
        for rep in parsed.reports:
            coll = getattr(rep, "collectionName", None) or ""
            if coll in allowed_collection_names:
                filtered_reports.append(rep)
            else:
                logger.warning(
                    "Discarding report '%s' — collectionName '%s' not in collection_schema whitelist.",
                    getattr(rep, "reportName", "?"),
                    coll,
                )
        parsed.reports = filtered_reports

    # ── Build lookup sets used by both post-filters ──────────────────────────────────────
    # schema_bool_fields : field names typed as boolean across all collections
    schema_bool_fields: set[str] = set()
    if collection_schema_dict:
        src_list = collection_schema_dict if isinstance(collection_schema_dict, list) else []
        if isinstance(collection_schema_dict, dict):
            src_list = [{"collection_name": k, "schema": v} for k, v in collection_schema_dict.items() if k != "__meta__"]
        for coll in src_list:
            s = coll.get("schema") or {}
            for field, ftype in s.items():
                if isinstance(ftype, str) and ftype.lower() in ("boolean", "bool"):
                    schema_bool_fields.add(field)

    # declared_join_keys : set of (local_field, foreign_coll, foreign_field) tuples that are
    # explicitly declared as FK references — $lookup using these is permitted.
    declared_join_keys: set[tuple] = {
        (j["local_field"], j["foreign_coll"], j["foreign_field"]) for j in declared_joins
    }

    # Post-filter: remove reports whose $match stages contain hardcoded string values for
    # fields that have NO declared enum options (predicted values → likely wrong/empty results).
    if parsed.reports:
        def _has_undeclared_string_match(pipeline: list, coll_name: str) -> bool:
            coll_enums = enum_fields_map.get(coll_name, {})
            for stage in pipeline:
                if not isinstance(stage, dict):
                    continue
                match_doc = stage.get("$match")
                if not isinstance(match_doc, dict):
                    continue
                for field, condition in match_doc.items():
                    if field.startswith("$"):
                        continue
                    allowed_values = coll_enums.get(field)
                    # Plain string equality: {"field": "SomeValue"}
                    if isinstance(condition, str) and field not in schema_bool_fields:
                        if allowed_values is None:
                            return True  # no enum declared → predicted value
                        if condition not in allowed_values:
                            return True  # value not in declared enum → predicted
                    # Equality inside $eq: {"field": {"$eq": "SomeValue"}}
                    if isinstance(condition, dict):
                        eq_val = condition.get("$eq")
                        if isinstance(eq_val, str) and field not in schema_bool_fields:
                            if allowed_values is None:
                                return True
                            if eq_val not in allowed_values:
                                return True
            return False

        clean_reports = []
        for rep in parsed.reports:
            aggregation_raw = getattr(rep, "aggregation", None)
            coll_name = getattr(rep, "collectionName", "") or ""
            flagged = False
            if aggregation_raw:
                try:
                    pipeline = json.loads(aggregation_raw) if isinstance(aggregation_raw, str) else aggregation_raw
                    if isinstance(pipeline, list) and _has_undeclared_string_match(pipeline, coll_name):
                        flagged = True
                except Exception:
                    pass
            if flagged:
                logger.warning(
                    "Discarding report '%s' — aggregation contains a hardcoded string $match "
                    "value for a field with no declared enum options.",
                    getattr(rep, "reportName", "?"),
                )
            else:
                clean_reports.append(rep)
        parsed.reports = clean_reports

    # Post-filter: remove reports whose $lookup stages are NOT in the declared FK references.
    # Joins that are explicitly declared in the schema are allowed; all others are hallucinated.
    if parsed.reports:
        def _has_undeclared_lookup(pipeline: list) -> bool:
            for stage in pipeline:
                if not isinstance(stage, dict):
                    continue
                lookup = stage.get("$lookup")
                if not isinstance(lookup, dict):
                    continue
                local_f  = lookup.get("localField", "")
                foreign_c = lookup.get("from", "")
                foreign_f = lookup.get("foreignField", "")
                if (local_f, foreign_c, foreign_f) not in declared_join_keys:
                    return True  # this join was not declared in schema
            return False

        no_lookup_reports = []
        for rep in parsed.reports:
            aggregation_raw = getattr(rep, "aggregation", None)
            flagged = False
            if aggregation_raw:
                try:
                    pipeline = json.loads(aggregation_raw) if isinstance(aggregation_raw, str) else aggregation_raw
                    if isinstance(pipeline, list) and _has_undeclared_lookup(pipeline):
                        flagged = True
                except Exception:
                    pass
            if flagged:
                logger.warning(
                    "Discarding report '%s' — aggregation contains a $lookup that is not in the "
                    "declared foreign key references.",
                    getattr(rep, "reportName", "?"),
                )
            else:
                no_lookup_reports.append(rep)
        parsed.reports = no_lookup_reports

    # Post-filter: discard $bucket pipelines with null / Infinity / mixed-type boundaries.
    # Infinity is not valid JSON; it becomes null on serialize and MongoDB rejects the stage.
    if parsed.reports:
        valid_bucket_reports = []
        for rep in parsed.reports:
            aggregation_raw = getattr(rep, "aggregation", None)
            flagged = False
            if aggregation_raw:
                try:
                    pipeline = json.loads(aggregation_raw) if isinstance(aggregation_raw, str) else aggregation_raw
                    if isinstance(pipeline, list) and _has_invalid_bucket_boundaries(pipeline):
                        flagged = True
                except Exception:
                    pass
            if flagged:
                logger.warning(
                    "Discarding report '%s' — $bucket.boundaries contains null, Infinity, "
                    "or mixed types (MongoDB requires finite same-type bounds).",
                    getattr(rep, "reportName", "?"),
                )
            else:
                valid_bucket_reports.append(rep)
        parsed.reports = valid_bucket_reports

    return parsed.reports


@router.post("/suggest-reports", response_model=WorkflowAnalyzeResponse)
async def analyze_workflow_reports(
    request: WorkflowAnalyzeRequest, http_request: Request
):
    try:
        subscriber_id = (
            (request.subscriberId or http_request.headers.get("x-subscriber-id") or "")
        ).strip()
        if not subscriber_id:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "subscriberId is required"},
            )
        try:
            snapshot = get_subscriber_credits_snapshot(subscriber_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": str(exc)},
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail={"success": False, "error": str(exc)},
            )
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error": f"Subscriber '{subscriber_id}' not found",
                },
            )
        available = float(snapshot.get("available_credits") or 0)
        if available <= 0:
            raise HTTPException(
                status_code=402,
                detail={
                    "success": False,
                    "error": "you dont have credit please recharge",
                    "code": "INSUFFICIENT_SUBSCRIBER_BALANCE",
                    "available_usd": available,
                },
            )

        workflow_dict = request.model_dump(exclude={"extra"}, by_alias=True)
        workflow_dict.update(request.extra)

        steps = workflow_dict.get("steps", [])
        if not steps:
            return WorkflowAnalyzeResponse(
                success=True,
                reports=[],
                source="llm",
            )

        org_id = request.orgId or http_request.headers.get("x-org-id")
        user_id = request.userId or http_request.headers.get("x-user-id")
        init_subscriber_billing(subscriber_id, org_id=org_id, user_id=user_id)

        # Step 0: Query Process_Configs for collection_schema
        collection_schema_dict: Optional[dict[str, Any]] = None
        process_doc: Optional[dict] = None
        project_id = request.projectId
        if project_id:
            try:
                # Build the query matching Process_Configs.agentic_process.workflow_id
                query: Dict[str, Any] = {
                    "engineType": "agentic_graph",
                    "agentic_process.workflow_id": project_id,
                }
                # Add tenant filters if available
                if request.orgId:
                    try:
                        query["orgId"] = ObjectId(request.orgId) if isinstance(request.orgId, str) else request.orgId
                    except Exception:
                        query["orgId"] = request.orgId
                if request.subscriberId:
                    try:
                        query["subscriberId"] = ObjectId(request.subscriberId) if isinstance(request.subscriberId, str) else request.subscriberId
                    except Exception:
                        query["subscriberId"] = request.subscriberId
                if request.userId:
                    try:
                        query["userId"] = ObjectId(request.userId) if isinstance(request.userId, str) else request.userId
                    except Exception:
                        query["userId"] = request.userId

                process_configs_coll = Database.get_collection("Process_Configs")
                process_doc = await process_configs_coll.find_one(query)
                if process_doc:
                    agentic_process = process_doc.get("agentic_process") or {}
                    # Extract collection_schema — it may be stored as a dict of collection schemas
                    raw_schema = agentic_process.get("collection_schema")
                    if raw_schema:
                        collection_schema_dict = raw_schema
                        logger.info(
                            "Found collection_schema in Process_Configs for project %s: %d collections",
                            project_id,
                            len(raw_schema),
                        )
                    else:
                        logger.info(
                            "No collection_schema found in Process_Configs for project %s",
                            project_id,
                        )

                        screen_gen_coll = Database.get_collection("screen_generations")
                        screen_docs = await screen_gen_coll.find(
                            {"project_id": project_id}
                        ).to_list(length=None)
                        collections = list(dict.fromkeys(
                            ds["operation"]["collection"]
                            for doc in screen_docs
                            for ds in doc.get("ir_schema", {}).get("dataSources", [])
                            if "operation" in ds and "collection" in ds["operation"]
                        ))
                        logger.info("Extracted collections from screen_generations for project %s: %s", project_id, collections)
                        if collections:
                            subscriber_db = resolve_subscriber_db_name(
                            project_id=project_id,
                            subscriber_id=request.subscriberId,
                            )
                            collection_schema_list = []
                            for name in collections:
                                sample = await fetch_latest_sample_doc_in_db(
                                name, db_name=subscriber_db
                            )
                                schema = _derive_expanded_schema(sample) if sample else {}
                                collection_schema_list.append({
                                "collection_name": name,
                                "schema": schema,
                                })
                            collection_schema_dict = collection_schema_list
                            logger.info(
                            "Built collection schema from subscriber DB '%s': %s",
                            subscriber_db, [c["collection_name"] for c in collection_schema_list],
                            )
                            await process_configs_coll.update_one(
                                {"_id": process_doc["_id"]},
                                {"$set": {"agentic_process.collection_schema": collection_schema_list}}
                            )

                else:
                    logger.info(
                        "No Process_Configs document found for project %s",
                        project_id,
                    )
            except Exception as schema_err:
                logger.warning(
                    "Failed to fetch collection_schema from Process_Configs for project %s: %s",
                    project_id,
                    schema_err,
                )

        # Step 1: Derive Business Context (2 LLM calls)
        semantic_model = await derive_business_context(workflow_dict)

        # Persist semantic model to Process_Configs so subsequent calls can skip
        # re-computation.  Non-fatal — a failure here must not block report delivery.
        if project_id and process_doc:
            try:
                process_configs_coll = Database.get_collection("Process_Configs")
                await process_configs_coll.update_one(
                    {"_id": process_doc["_id"]},
                    {"$set": {"agentic_process.semantic_model": semantic_model.model_dump()}},
                )
                logger.info("Persisted semantic_model to Process_Configs for project %s", project_id)
            except Exception as sem_err:
                logger.warning(
                    "Failed to persist semantic_model to Process_Configs for project %s: %s",
                    project_id,
                    sem_err,
                )

        # Step 2: Generate reports from semantic model (1 LLM call)
        reports = await generate_reports_from_semantic_model(
            semantic_model,
            workflow_dict,
            collection_schema_dict=collection_schema_dict,
        )

        return WorkflowAnalyzeResponse(
            success=True,
            reports=reports,
            source="llm",
            semantic_model=semantic_model
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Workflow analysis failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": str(e)},
        )
    finally:
        clear_billing_context()


@router.post("/analyze-business-context", response_model=BusinessSemanticModel)
async def analyze_business_context_endpoint(
    request: WorkflowAnalyzeRequest, http_request: Request
):
    try:
        workflow_dict = request.model_dump(exclude={"extra"}, by_alias=True)
        workflow_dict.update(request.extra)

        steps = workflow_dict.get("steps", [])
        if not steps:
            return BusinessSemanticModel()

        semantic_model = await derive_business_context(workflow_dict)
        return semantic_model

    except Exception as e:
        logger.error("Business context derivation failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": str(e)},
        )


def _build_dashboard_ir_schema(
    dashboard_def: "DashboardDefinition",
    backend_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a structured IR schema from a finalized DashboardDefinition so the
    Angular generator receives an explicit, backend-determined field contract
    rather than deriving field shapes from free-text prompt alone.

    The IR schema mirrors the structure expected by ``AngularAppGenerator.generate_app``
    and makes the generation truly sequential: backend schema → frontend generation.
    """
    base_path = backend_metadata.get("base_path") or ""
    return {
        "screen": {
            "id": dashboard_def.dashboardId,
            "name": dashboard_def.dashboardName,
            "type": "dashboard",
            "selector": "app-screen",
        },
        "components": [
            {
                "id": w.reportId,
                "type": w.widgetType,
                "reportName": w.reportName,
                "reportType": w.reportType,
                "title": w.title,
                "description": w.description or "",
                "datasource": w.datasource,
                "requiredFields": w.requiredFields,
                "chartMeta": w.chartMeta,
                "filters": w.filters,
                "functionId": w.functionId,
                "gridRow": w.gridRow,
                "gridCol": w.gridCol,
                "gridWidth": w.gridWidth,
                "gridHeight": w.gridHeight,
            }
            for w in dashboard_def.widgets
        ],
        "dataSources": [
            {
                "id": w.reportId,
                "type": "dynamic",
                "functionId": w.functionId,
                "collection": w.datasource,
                "requiredFields": w.requiredFields,
                "operation": "aggregate" if w.aggregationPipeline else "find",
                "endpoint": f"{base_path}/{w.functionId}" if base_path and w.functionId else None,
                "chartMeta": w.chartMeta,
            }
            for w in dashboard_def.widgets
        ],
        "inputParameters": [],
        "outputParameters": [],
    }


async def generate_dashboard_screens_for_reports(
    *,
    workflow_id: str,
    project_id: str,
    reports: List[Dict[str, Any]],
    dashboard: Optional[DashboardDefinition] = None,
    dashboard_name: Optional[str] = None,
    org_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    user_id: Optional[str] = None,
    collection_schema: Optional[Any] = None,
) -> CreateDashboardScreenResponse:
    """
    Core dashboard-screen generation logic, reusable outside of the FastAPI
    route (e.g. called directly from /agentic/plan/execute after gap
    resolution, or from /agentic/dashboard/plan/execute).

    Generates a SINGLE Angular application containing one widget per report
    (not one Angular app per report + a separate composite app) — a real BI
    dashboard is one screen with multiple widgets, and generating N+1 full
    Angular builds per dashboard is slower, more expensive, and gives a worse
    UX than one cohesive page. Each widget is wired to its OWN real MongoDB
    find/aggregate backend function (registered via
    register_dashboard_backend_functions) so the screen renders live DB data
    instead of mock/sample data.
    """
    if not reports:
        raise HTTPException(
            status_code=400,
            detail="reports array is required and must not be empty",
        )

    _all_warnings: List[str] = []  # accumulates non-fatal warnings to surface in response

    validated_reports = []
    for idx, rep in enumerate(reports):
        rtype = rep.get("reportType", "")
        if rtype not in VALID_REPORT_TYPES:
            rep["reportType"] = "Tabular"
            msg = f"Report {idx}: Invalid reportType '{rtype}', defaulting to Tabular"
            logger.warning(msg)
            _all_warnings.append(msg)
        validated_reports.append(rep)

    # Merge caller-supplied collection_schema with any schema stored in
    # Process_Configs so free-text descriptions are enhanced with real field info.
    merged_collection_schema: Optional[Any] = collection_schema
    if project_id:
        try:
            pc_coll = Database.get_collection("Process_Configs")
            stored_doc = await pc_coll.find_one(
                {"engineType": "agentic_graph", "agentic_process.workflow_id": project_id}
            )
            if stored_doc:
                stored_schema = (stored_doc.get("agentic_process") or {}).get("collection_schema")
                if collection_schema and stored_schema is None:
                    # Persist new schema to Process_Configs (non-fatal)
                    try:
                        await pc_coll.update_one(
                            {"_id": stored_doc["_id"]},
                            {"$set": {"agentic_process.collection_schema": collection_schema}},
                        )
                    except Exception as _persist_err:
                        logger.warning("[create-screen] Failed to persist collectionSchema: %s", _persist_err)
                elif collection_schema and stored_schema is not None and isinstance(stored_schema, list) and isinstance(collection_schema, list):
                    stored_by_name = {
                        (e.get("collection_name") or e.get("name") or ""): e
                        for e in stored_schema if isinstance(e, dict)
                    }
                    for new_entry in collection_schema:
                        if isinstance(new_entry, dict):
                            key = new_entry.get("collection_name") or new_entry.get("name") or ""
                            stored_by_name[key] = new_entry
                    merged_collection_schema = list(stored_by_name.values())
                    try:
                        await pc_coll.update_one(
                            {"_id": stored_doc["_id"]},
                            {"$set": {"agentic_process.collection_schema": merged_collection_schema}},
                        )
                    except Exception as _persist_err:
                        logger.warning("[create-screen] Failed to persist merged collectionSchema: %s", _persist_err)
                elif stored_schema is not None and collection_schema is None:
                    merged_collection_schema = stored_schema
        except Exception as _schema_err:
            logger.warning("[create-screen] Failed to load/merge collectionSchema: %s", _schema_err)

    # Load field aliases from Process_Configs (populated via /update-field-aliases)
    field_aliases_map: Optional[Dict[str, Dict[str, str]]] = None
    if project_id:
        try:
            pc_coll = Database.get_collection("Process_Configs")
            alias_doc = await pc_coll.find_one(
                {"engineType": "agentic_graph", "agentic_process.workflow_id": project_id},
                {"agentic_process.field_aliases": 1},
            )
            if alias_doc:
                raw_aliases = (alias_doc.get("agentic_process") or {}).get("field_aliases")
                if raw_aliases and isinstance(raw_aliases, dict):
                    field_aliases_map = raw_aliases
        except Exception as _alias_err:
            logger.warning("[create-screen] Failed to load field_aliases: %s", _alias_err)

    # Optionally enhance free-text report descriptions using the merged schema
    if merged_collection_schema:
        validated_reports = await enhance_report_descriptions(
            user_reports=validated_reports,
            collection_schema=merged_collection_schema,
            workflow_name=dashboard_name or workflow_id or "Dashboard",
        )

    dashboard_def = dashboard
    if dashboard_def is None:
        dashboard_def, _build_warnings = build_dashboard_definition(
            project_id, dashboard_name or workflow_id or "Dashboard", validated_reports,
            collection_schemas=merged_collection_schema,
        )
        if _build_warnings:
            _all_warnings.extend(_build_warnings)
        # Enrich each widget description with an LLM-generated query-plan summary
        _llm_descs = await _llm_build_query_plan_descriptions(dashboard_def.widgets)
        for _w in dashboard_def.widgets:
            if _w.reportName in _llm_descs:
                _w.description = _llm_descs[_w.reportName]
        # Sync enriched descriptions back to validated_reports so they are
        # reflected in the LLM prompt and persisted alongside the report list.
        # Also sync collectionName and requiredFields from the resolved widget.
        _widget_sync = {
            _w.reportName: {
                "description": _w.description or "",
                "collectionName": _w.datasource,
                "requiredFields": _w.requiredFields,
                "optionalFilters": [f.get("field", "") for f in (_w.filters or []) if f.get("field")],
            }
            for _w in dashboard_def.widgets
        }
        for _rep in validated_reports:
            _rname = _rep.get("reportName", "")
            if _rname in _widget_sync:
                _rep.update(_widget_sync[_rname])

    # Register one real MongoDB-backed function per widget (find/aggregate —
    # deterministic, no LLM call) and get the backend_metadata contract that
    # AngularAppGenerator uses to wire real /dynamic/execute calls.
    backend_metadata: Dict[str, Any] = {"base_path": None, "functions": []}
    try:
        backend_metadata = await register_dashboard_backend_functions(
            project_id=project_id,
            dashboard_def=dashboard_def,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
            field_aliases_map=field_aliases_map,
        )
    except Exception:
        logger.exception("[dashboard] Failed to register backend functions for project %s", project_id)

    widgets_desc = "\n\n".join(
        f"Widget {i + 1}: \"{w.title}\" ({w.widgetType})\n"
        f"  reportId: {w.reportId}\n"
        f"  functionId (call this via dynamic execute for real data): {w.functionId}\n"
        f"  datasource collection: {w.datasource}\n"
        f"  requiredFields (TABLE COLUMNS — display ONLY these fields, nothing else): {', '.join(w.requiredFields) or 'None'}\n"
        f"  filterableFields (optional extras; EVERY requiredFields column still gets a header filter): {', '.join(f.get('field', '') for f in w.filters) or 'None'}\n"
        f"  chart metadata: {json.dumps(w.chartMeta)}\n"
        f"  description: {w.description or ''}"
        for i, w in enumerate(dashboard_def.widgets)
    )

    # Build a dynamic KPI derivation section from the actual KPI widgets present in this dashboard.
    # This replaces the static hardcoded candidate-shortlisting template that was polluting every
    # dashboard prompt with wrong function IDs (report_11_query, report_12_query, etc.).
    kpi_widgets = [w for w in dashboard_def.widgets if w.widgetType == "kpi"]
    accent_colors = ["blue", "green", "amber", "red"]
    if kpi_widgets:
        kpi_items = []
        for idx, kw in enumerate(kpi_widgets):
            accent = accent_colors[idx % len(accent_colors)]
            filterable = ", ".join(f.get("field", "") for f in kw.filters) if kw.filters else "none"
            kpi_items.append(
                f'{idx + 1}. "{kw.title}" ({accent} accent):\n'
                f'   Call {kw.functionId}.\n'
                f'   Read response.data.items or response.data.count for the KPI value.\n'
                f'   xField from chartMeta: {kw.chartMeta.get("xField", "N/A")}\n'
                f'   requiredFields: {", ".join(kw.requiredFields) or "N/A"}\n'
                f'   filterable: {filterable}'
            )
        kpi_example_card = "\n".join(
            f'  <div class="kpi-card kpi-accent-{accent_colors[i % len(accent_colors)]}">\n'
            f'    <div class="kpi-card-label">{kw.title.upper()}</div>\n'
            f'    <div class="kpi-card-value">{{{{ kpi{i + 1}Value }}}}</div>\n'
            f'    <div class="kpi-card-sub">{{{{ kpi{i + 1}Sub }}}}</div>\n'
            f'  </div>'
            for i, kw in enumerate(kpi_widgets)
        )
        kpi_derivation_section = f"""=====================
KPI CARDS — DERIVATION & TEMPLATE
=====================

This dashboard has {len(kpi_widgets)} KPI widget(s). Derive values by calling their functionIds on init:

{"".join(kpi_items)}

**EXACT template pattern for KPI row (NO mat-icon, NO mat-card, NO badge text):**

Example:- ```html
<div class="kpi-grid">
{kpi_example_card}
</div>
```

Do NOT use mat-card, mat-icon, or any Angular Material components for KPI cards.
Do NOT add any animation state label text inside KPI cards.
Use ONLY the plain HTML divs with the CSS classes defined above."""
    else:
        # No KPI widgets — omit the section entirely to avoid confusing the LLM.
        kpi_derivation_section = """=====================
KPI CARDS — DERIVATION & TEMPLATE
=====================

This dashboard has NO dedicated KPI widget. Do NOT render a KPI card row.
Skip the .kpi-grid section entirely."""

    prompt = f"""
Generate a production-quality Angular analytics dashboard application named "{dashboard_def.dashboardName}".

The dashboard MUST look like a modern light-themed enterprise analytics application (Grafana / Power BI / Metabase style).

=====================
TECH STACK
=====================

Use:
- Angular 17+ Standalone Components with ChangeDetectionStrategy.OnPush
- Angular Material (MatCard, MatTable, MatPaginator, MatSort, MatSelect, MatInput, MatButton, MatIcon, MatProgressSpinner, MatChipsModule, MatTooltipModule)
- ng2-charts + chart.js for ALL chart widgets (exact import rules below)
- Angular Animations (@angular/animations) for card hover and KPI counter effects
- RxJS (BehaviorSubject, combineLatest, from, mergeMap, map, scan, takeUntilDestroyed)
- HttpClient
- **xlsx ^0.18.5** for Excel export on tabular widgets (`import * as XLSX from 'xlsx'`)
- **jspdf ^2.5.1** for PDF export on chart widgets (`import {{ jsPDF }} from 'jspdf'`)

NEVER use mock data, fake values, hardcoded arrays, or placeholder data.

=====================
ONPUSH CHANGE DETECTION — CRITICAL (PREVENTS SPINNER-STUCK BUG)
=====================

ALL components use ChangeDetectionStrategy.OnPush.
OnPush DOES NOT automatically re-render after async HTTP responses.

MANDATORY PATTERN — every component that loads data via HTTP MUST:
1. Inject ChangeDetectorRef: `private cdr = inject(ChangeDetectorRef);`
2. Call `this.cdr.markForCheck()` IMMEDIATELY after setting data AND after setting isLoading = false:

```typescript
loadData(): void {{
  this.isLoading = true;
  this.error = null;
  this.cdr.markForCheck();  // show spinner immediately
  this.apiService.callWidget(this.functionId, this.buildFilters())
    .pipe(takeUntilDestroyed(this.destroyRef))
    .subscribe({{
      next: (res: any) => {{
        this.data = res?.data?.items ?? [];
        this.isLoading = false;
        this.cdr.markForCheck();  // ← REQUIRED: trigger re-render after data arrives
      }},
      error: (err: any) => {{
        this.error = err?.message ?? 'Failed to load data';
        this.isLoading = false;
        this.cdr.markForCheck();  // ← REQUIRED: trigger re-render after error
      }}
    }});
}}
```

If you forget `cdr.markForCheck()` after data loads, the component will be permanently
stuck showing the loading spinner even when data has been fetched successfully.

=====================
ROOT COMPONENT SELECTOR (CRITICAL — PREVENTS NG05104)
=====================

The root AppComponent MUST use EXACTLY this selector:

  selector: 'app-screen'

Do NOT use 'app-dashboard', 'das-ats-dashboard', or any other selector.
The platform's auto-injected main.ts bootstraps into <app-screen>; any other selector
causes NG05104 (host element not found) at runtime.
Do NOT name the main Component as AppScreenComponent
STRICTLY use app-screen as the root selector in index.html and main.ts

=====================
NG2-CHARTS IMPORT RULES (CRITICAL — PREVENTS NG2011 BUILD ERROR)
=====================

ng2-charts BaseChartDirective IS standalone in ng2-charts v6+.
Import it DIRECTLY (no NgModule wrapper needed):

```typescript
// In any component that renders a chart:
import {{ BaseChartDirective }} from 'ng2-charts';
import {{ Chart, ChartData, ChartOptions, ChartType, registerables }} from 'chart.js';
Chart.register(...registerables);

@Component({{
  standalone: true,
  imports: [CommonModule, BaseChartDirective, /* other imports */],
  ...
}})
```

Do NOT import NgChartsModule — it does not exist in v6+.
Do NOT import NG2ChartsModule.

=====================
TRACKBY RULES (CRITICAL — PREVENTS TS2339)
=====================

When using *ngFor with trackBy in templates (e.g. *ngFor="let x of list; trackBy: myFn"),
ALWAYS define the trackBy function in the same component class:

```typescript
trackById(index: number, item: any): any {{
  return item?.id ?? item?.reportId ?? index;
}}
```

Never reference a trackBy function that is not declared on the component.
Use `track item.id` syntax only with Angular 17 @for control flow (not *ngFor).

=====================
LIGHT ENTERPRISE VISUAL THEME (MANDATORY)
=====================

**FIRST LINE of styles.css MUST be the Material Icons font import:**
```css
@import url('https://fonts.googleapis.com/icon?family=Material+Icons');
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
```

This is CRITICAL. Without the font, mat-icon renders raw text like "refresh", "sync", "group" which
breaks the UI completely. The @import MUST be the very first lines of styles.css.

CSS Variables (define in styles.css :root block, after the @import lines):
```css
:root {{
  --dash-bg: #f8fafc;
  --dash-surface: #ffffff;
  --dash-border: #e2e8f0;
  --dash-primary: #3b82f6;
  --dash-success: #22c55e;
  --dash-warning: #f59e0b;
  --dash-danger:  #ef4444;
  --dash-text: #1e293b;
  --dash-muted:   #94a3b8;
  --dash-hover:   #f1f5f9;
}}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

html, body {{
  height: 100%;
  background-color: var(--dash-bg);
  color: var(--dash-text);
  font-family: 'Inter', 'Segoe UI', sans-serif;
  font-size: 14px;
}}

/* Angular Material dark overrides */
.mat-mdc-card {{ background: var(--dash-surface) !important; color: var(--dash-text) !important; border: 1px solid var(--dash-border) !important; }}
.mat-mdc-card-header {{ padding-bottom: 8px !important; }}
.mat-mdc-card-title {{ color: var(--dash-text) !important; font-size: 14px !important; font-weight: 600 !important; }}
.mat-mdc-card-subtitle {{ color: var(--dash-muted) !important; font-size: 12px !important; }}
.mat-mdc-table {{ background: var(--dash-surface) !important; width: 100%; }}
.mat-mdc-header-row {{ background: #f8fafc !important; position: sticky; top: 0; z-index: 2; height: auto !important; }}
.mat-mdc-header-cell {{ color: var(--dash-muted) !important; font-weight: 600 !important; text-transform: uppercase !important; font-size: 11px !important; letter-spacing: 0.05em !important; border-bottom: 1px solid var(--dash-border) !important; vertical-align: top; height: auto !important; }}
.mat-mdc-cell {{ color: var(--dash-text) !important; border-bottom-color: var(--dash-border) !important; font-size: 13px !important; }}
.mat-mdc-row:hover {{ background: var(--dash-hover) !important; }}
.mat-mdc-paginator {{ background: var(--dash-surface) !important; color: var(--dash-muted) !important; border-top: 1px solid var(--dash-border) !important; }}
.mat-mdc-paginator-icon, .mat-mdc-paginator-range-label {{ color: var(--dash-muted) !important; }}
/* CRITICAL: paginator page-size select — prevent black box on items-per-page dropdown */
.mat-mdc-paginator .mat-mdc-select-value,
.mat-mdc-paginator .mat-mdc-select-trigger,
.mat-mdc-paginator .mat-mdc-select-arrow {{ color: var(--dash-text) !important; }}
.mat-mdc-paginator .mdc-text-field {{ background: transparent !important; }}
.mat-mdc-paginator .mat-mdc-form-field .mdc-text-field {{ background: transparent !important; }}
.mat-mdc-paginator .mdc-text-field--filled:not(.mdc-text-field--disabled) {{ background-color: transparent !important; }}
/* CRITICAL: CDK overlay — ensure the items-per-page dropdown panel renders next to the
   paginator, NOT at the bottom of the viewport. The CDK portal appends panels to <body>
   by default; using position:fixed with a high z-index keeps it correctly positioned
   even inside overflow:hidden / transform containers (widget cards). */
.cdk-overlay-container {{ position: fixed !important; z-index: 1050 !important; }}
.cdk-overlay-pane {{ position: absolute; }}
/* Style the options panel that appears for mat-paginator page-size select */
.mat-mdc-select-panel {{ background: var(--dash-surface) !important; border: 1px solid var(--dash-border) !important; border-radius: 6px !important; box-shadow: 0 4px 16px rgba(0,0,0,0.12) !important; }}
.mat-mdc-select-value {{ color: var(--dash-text) !important; }}
.mat-mdc-option {{ color: var(--dash-text) !important; background: var(--dash-surface) !important; }}
.mat-mdc-option:hover {{ background: var(--dash-hover) !important; }}
.mdc-text-field--outlined .mdc-notched-outline__leading,
.mdc-text-field--outlined .mdc-notched-outline__notch,
.mdc-text-field--outlined .mdc-notched-outline__trailing {{ border-color: var(--dash-border) !important; }}
.mdc-text-field--outlined:hover .mdc-notched-outline__leading,
.mdc-text-field--outlined:hover .mdc-notched-outline__notch,
.mdc-text-field--outlined:hover .mdc-notched-outline__trailing {{ border-color: var(--dash-primary) !important; }}
.mat-mdc-form-field .mdc-text-field {{ background: transparent !important; }}
.mat-mdc-input-element, .mat-mdc-select-trigger {{ color: var(--dash-text) !important; }}
.mat-mdc-floating-label {{ color: var(--dash-muted) !important; }}
/* CRITICAL: table sticky header must use light background, NOT dark */
.mat-mdc-header-row {{ background: #f8fafc !important; height: auto !important; }}
.mat-mdc-header-cell {{ color: var(--dash-muted) !important; font-weight: 600 !important; text-transform: uppercase !important; font-size: 11px !important; letter-spacing: 0.05em !important; border-bottom: 1px solid var(--dash-border) !important; height: auto !important; vertical-align: top; }}
::placeholder {{ color: var(--dash-muted) !important; opacity: 1; }}
.mat-icon, .material-icons {{ font-family: 'Material Icons' !important; font-size: 20px !important; line-height: 1 !important; vertical-align: middle !important; }}
```

=====================
LAYOUT & CSS GRID (MANDATORY — USE THESE EXACT CSS CLASSES)
=====================

Single scrollable page — NO routing.

⚠️ VIEW ENCAPSULATION RULE — CRITICAL (PREVENTS MISSING STYLES IN CHILD COMPONENTS):
Angular's default Emulated View Encapsulation SCOPES styles to the component that declares them.
Styles placed in app.component.css are ONLY visible inside AppComponent's own template.
Standalone child components (ChartWidgetComponent, TableWidgetComponent, KpiWidgetComponent, etc.)
CANNOT see app.component.css styles — their widget-card, widget-card-header, chart-canvas-wrapper
classes will be completely UNSTYLED if you put those rules in app.component.css.

MANDATORY PLACEMENT RULES:
- app.component.css  → ONLY layout containers used DIRECTLY in AppComponent's own template
  (.dash-shell, .dash-header, .dash-header-right, .dash-datetime, .btn-refresh-all,
   .dash-content, .dash-section-title, .kpi-grid, .chart-grid, .table-grid)
- styles.css         → ALL shared/widget classes used by ANY standalone child component,
  INCLUDING kpi-card styles (KPI cards may be rendered inside a standalone child component
  that cannot see app.component.css due to Angular's Emulated View Encapsulation):
  (.widget-card, .widget-card-header, .widget-card-title, .widget-card-subtitle,
   .widget-card-body, .btn-widget-refresh, .chart-canvas-wrapper, .table-scroll-wrapper,
   .table-filter-bar, .filter-input, .filter-label, .btn-clear-filters,
   .cell-clamp, .cell-expand, .btn-row-expand, .col-expand,
   .header-cell-inner, .header-filter-input,
   .widget-state-center, .widget-error-msg, .btn-retry,
   .kpi-card, .kpi-card-label, .kpi-card-value, .kpi-card-sub,
   .kpi-accent-blue, .kpi-accent-green, .kpi-accent-amber, .kpi-accent-red)
⚠️ DO NOT place .kpi-card or .kpi-accent-* in app.component.css — they MUST be in styles.css.

**Add these EXACT CSS classes — split between app.component.css and styles.css as shown:**

--- app.component.css (AppComponent template-only styles) ---

```css
/* ─── Dashboard shell ─── */
.dash-shell {{
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--dash-bg);
}}

/* ─── Header bar ─── */
.dash-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;  /* title LEFT, datetime+button RIGHT */
  padding: 0 24px;
  height: 56px;
  background: var(--dash-surface);
  border-bottom: 1px solid var(--dash-border);
  position: sticky;
  top: 0;
  z-index: 100;
  flex-shrink: 0;
}}
.dash-header h1 {{
  font-size: 18px;
  font-weight: 700;
  color: var(--dash-text);
  letter-spacing: 0.02em;
  margin: 0;
  flex: 1;          /* take remaining space so right group stays at far right */
}}
.dash-header-right {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto; /* push to the far right */
}}
.dash-datetime {{
  font-size: 12px;
  color: var(--dash-muted);
}}
.btn-refresh-all {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  background: var(--dash-primary);
  color: #fff;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s;
}}
.btn-refresh-all:hover {{ background: #2563eb; }}
.btn-refresh-all .refresh-icon {{ font-size: 16px; line-height: 1; }}

/* ─── Page content ─── */
.dash-content {{
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}}

/* ─── KPI grid: ALWAYS 4 columns on desktop ─── */
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}}
@media (max-width: 1023px) {{ .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 599px)  {{ .kpi-grid {{ grid-template-columns: 1fr; }} }}

```

--- styles.css (global — visible to ALL standalone child components) ---

Append the following classes to styles.css AFTER the existing :root / Material overrides block:

```css
/* ─── Section heading ─── */
.dash-section-title {{
  font-size: 16px;
  font-weight: 600;
  color: var(--dash-text);
  margin-bottom: 4px;
}}

/* ─── Chart grid: 2 columns on desktop ─── */
.chart-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 20px;
}}
@media (max-width: 1023px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}

/* ─── Table grid: ALWAYS single column (full width per table) ─── */
.table-grid {{
  display: flex;
  flex-direction: column;
  gap: 20px;
  min-width: 90%;
  max-width: 100%;
}}
/* CRITICAL: every direct child of .table-grid MUST be a .widget-card div               */
/* so it inherits the border, border-radius, and shadow — never put mat-table here bare. */

/* ─── Widget card (charts & tables) ─── */
.widget-card {{
  background: var(--dash-surface);
  border: 1px solid var(--dash-border);  /* REQUIRED — visible border around every widget */
  border-radius: 10px;
  overflow: hidden;
  transition: border-color 0.2s, box-shadow 0.2s;
}}
.widget-card:hover {{
  border-color: #475569;
  box-shadow: 0 4px 20px rgba(0,0,0,0.15);
}}
.widget-card-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--dash-border);
}}
.widget-card-title {{
  font-size: 13px;
  font-weight: 600;
  color: var(--dash-text);
}}
.widget-card-subtitle {{
  font-size: 11px;
  color: var(--dash-muted);
  margin-top: 2px;
}}
.btn-widget-refresh {{
  background: transparent;
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-muted);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 14px;
  transition: color 0.2s, border-color 0.2s;
  flex-shrink: 0;
  margin-left: auto; /* REQUIRED: always push refresh icon to the RIGHT of the card header */
}}
.btn-widget-refresh:hover {{ color: var(--dash-primary); border-color: var(--dash-primary); }}
.widget-header-actions {{
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto; /* REQUIRED: pushes the action buttons group to the far right */
}}
.btn-widget-download {{
  background: transparent;
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-muted);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 14px;
  transition: color 0.2s, border-color 0.2s;
  flex-shrink: 0;
}}
.btn-widget-download:hover {{ color: #10b981; border-color: #10b981; }}
.widget-card-body {{
  padding: 16px;
}}

/* ─── Chart canvas wrapper ─── */
.chart-canvas-wrapper {{
  position: relative;
  height: 200px;
  max-height: 220px;
  width: 100%;
}}

/* ─── Table wrapper ─── */
.table-scroll-wrapper {{
  overflow-x: auto;
  overflow-y: auto;
  max-height: 380px;
}}

/* ─── Table cell 2-line clamp + single row expand ─── */
.cell-clamp {{
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
  line-height: 1.4;
  max-width: 280px;
}}
.cell-expand {{
  display: block;
  word-break: break-word;
  white-space: pre-wrap;
  line-height: 1.4;
  max-width: 280px;
}}
.col-expand {{
  flex: 0 0 44px;
  width: 44px;
  max-width: 44px;
  justify-content: center;
}}
.btn-row-expand {{
  background: none;
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-primary);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
}}
.btn-row-expand:hover {{ border-color: var(--dash-primary); background: var(--dash-hover); }}
.btn-row-expand .material-icons {{ font-size: 18px; line-height: 1; }}

/* ─── Per-column header filter (directly under the column title) ─── */
.header-cell-inner {{
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  width: 100%;
  padding: 4px 0 6px;
}}
.header-cell-inner .mat-sort-header-container {{
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.header-filter-input {{
  height: 28px;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  padding: 0 8px;
  background: var(--dash-surface);
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-text);
  font-size: 12px;
  font-weight: 400;
  text-transform: none;
  letter-spacing: normal;
  outline: none;
}}
.header-filter-input:focus {{ border-color: var(--dash-primary); }}
.header-filter-input::placeholder {{ color: var(--dash-muted); font-weight: 400; }}

/* ─── Table filter bar ─── */
.table-filter-bar {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--dash-border);
  background: var(--dash-hover);  /* light grey — NEVER use a dark colour here */
}}
.filter-input {{
  height: 32px;
  padding: 0 10px;
  background: var(--dash-surface);
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-text);
  font-size: 12px;
  outline: none;
  min-width: 100px;
  max-width: 150px;
  transition: border-color 0.2s;
}}
.filter-input:focus {{ border-color: var(--dash-primary); }}
.filter-input::placeholder {{ color: var(--dash-muted); }}
.filter-label {{
  font-size: 11px;
  color: var(--dash-muted);
  font-weight: 500;
}}
.btn-clear-filters {{
  height: 32px;
  padding: 0 12px;
  background: transparent;
  border: 1px solid var(--dash-border);
  border-radius: 6px;
  color: var(--dash-muted);
  font-size: 12px;
  cursor: pointer;
  transition: color 0.2s, border-color 0.2s;
  white-space: nowrap;
}}
.btn-clear-filters:hover {{ color: var(--dash-text); border-color: var(--dash-text); }}

/* ─── Loading / Error / Empty states ─── */
.widget-state-center {{
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 180px;
  flex-direction: column;
  gap: 8px;
  color: var(--dash-muted);
  font-size: 13px;
}}
.widget-error-msg {{
  color: var(--dash-danger);
  font-size: 13px;
  text-align: center;
}}
.btn-retry {{
  margin-top: 8px;
  padding: 6px 16px;
  background: transparent;
  border: 1px solid var(--dash-danger);
  border-radius: 6px;
  color: var(--dash-danger);
  font-size: 12px;
  cursor: pointer;
}}
.btn-retry:hover {{ background: rgba(239,68,68,0.1); }}

/* ─── KPI card (global — used directly in AppComponent OR any child KPI component) ─── */
/* CRITICAL: these rules MUST live in styles.css (not app.component.css) so they are     */
/* visible whether the card is rendered in AppComponent or a standalone child component.  */
.kpi-card {{
  background: var(--dash-surface);
  border: 1px solid var(--dash-border);  /* REQUIRED visible card border */
  border-radius: 10px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color 0.2s, transform 0.2s;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}}
.kpi-card:hover {{ border-color: var(--dash-primary); transform: translateY(-2px); box-shadow: 0 4px 16px rgba(59,130,246,0.12); }}
.kpi-card-label {{
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--dash-muted);
}}
.kpi-card-value {{
  font-size: 28px;
  font-weight: 700;
  color: var(--dash-text);
  line-height: 1.2;
}}
.kpi-card-sub {{
  font-size: 12px;
  color: var(--dash-muted);
}}
/* Accent left-border colours — MUST be in styles.css so the left accent renders        */
/* regardless of whether the .kpi-card div lives in AppComponent or a child component.  */
.kpi-accent-blue  {{ border-left: 4px solid var(--dash-primary) !important; }}
.kpi-accent-green {{ border-left: 4px solid var(--dash-success) !important; }}
.kpi-accent-amber {{ border-left: 4px solid var(--dash-warning) !important; }}
.kpi-accent-red   {{ border-left: 4px solid var(--dash-danger)  !important; }}
```

SUMMARY: styles.css must contain every CSS class referenced inside ANY standalone child component
template. app.component.css must ONLY contain classes used in AppComponent's own template.
Violating this rule causes widget-card borders, headers, and body padding to be completely missing
at runtime.

**Template structure to use (EXACT pattern):**

```html
<div class="dash-shell">
  <!-- Header: title on LEFT, datetime + Refresh All button on RIGHT -->
  <header class="dash-header">
    <h1>{{{{ dashboardName }}}}</h1>
    <!-- dash-header-right MUST be the LAST child so it stays on the right -->
    <div class="dash-header-right">
      <span class="dash-datetime">{{{{ currentDateTime }}}}</span>
      <!-- Refresh All button is ALWAYS the rightmost element -->
      <button class="btn-refresh-all" (click)="refreshAll()">
        <span class="material-icons refresh-icon">refresh</span>
        Refresh All
      </button>
    </div>
  </header>

  <div class="dash-content">
    <!-- KPI Row (only if the dashboard has KPI widgets — see KPI CARDS section below) -->
    <!-- CRITICAL: every .kpi-card MUST have border:1px solid var(--dash-border) visible -->
    <div class="kpi-grid">
      <!-- One .kpi-card per KPI widget defined in the WIDGETS section -->
      <!-- ... more KPI cards derived from actual KPI widgets ... -->
    </div>

    <!-- Visual Analytics -->
    <div>
      <h2 class="dash-section-title">Visual Analytics</h2>
      <div class="chart-grid">
        <div class="widget-card">  <!-- .widget-card REQUIRED: provides the border -->
          <div class="widget-card-header">
            <div>
              <div class="widget-card-title">Chart Title</div>
              <div class="widget-card-subtitle">Subtitle</div>
            </div>
            <!-- CRITICAL: .widget-header-actions MUST be the LAST child of .widget-card-header -->
            <!-- margin-left:auto on .widget-header-actions already pushes it to the right -->
            <div class="widget-header-actions">
              <!-- Download button: for chart widgets downloads chart canvas as PDF -->
              <button class="btn-widget-download" (click)="downloadPdf()" title="Download PDF">
                <span class="material-icons" style="font-size:14px">download</span>
              </button>
              <button class="btn-widget-refresh" (click)="loadChart()" title="Refresh">
                <span class="material-icons" style="font-size:14px">refresh</span>
              </button>
            </div>
          </div>
          <div class="widget-card-body">
            <div class="chart-canvas-wrapper">
              <canvas baseChart ...></canvas>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Detailed Reports -->
    <div>
      <h2 class="dash-section-title">Detailed Reports</h2>
      <!-- CRITICAL: each table MUST be inside a .widget-card div for the border to show -->
      <div class="table-grid">
        <div class="widget-card">  <!-- .widget-card REQUIRED: provides the border -->
          <div class="widget-card-header">
            <div>
              <div class="widget-card-title">Table Title</div>
              <div class="widget-card-subtitle">Subtitle</div>
            </div>
            <!-- Download + Refresh icons grouped in .widget-header-actions, always last/rightmost -->
            <div class="widget-header-actions">
              <!-- Download button: for table widgets exports ALL data rows as Excel (.xlsx) -->
              <button class="btn-widget-download" (click)="downloadExcel()" title="Download Excel">
                <span class="material-icons" style="font-size:14px">download</span>
              </button>
              <button class="btn-widget-refresh" (click)="loadData()" title="Refresh">
                <span class="material-icons" style="font-size:14px">refresh</span>
              </button>
            </div>
          </div>
          <div class="table-scroll-wrapper">
            <mat-table ...></mat-table>
          </div>
          <mat-paginator ...></mat-paginator>
        </div>
      </div>
    </div>
  </div>
</div>
```

CRITICAL RULES:
- Use `<span class="material-icons">refresh</span>` for ALL icons — NOT `<mat-icon>`. This uses the loaded Google font directly.
- Use `<span class="material-icons">download</span>` for the download icon — NOT `<mat-icon>`.
- DO NOT use `<mat-icon>` anywhere in templates (it needs extra module setup).
- DO NOT add any extra text, badges, or animation state labels before section titles.
- The `.kpi-grid`, `.chart-grid`, `.table-grid` classes MUST be applied to the grid containers.
- Each KPI card MUST use class `.kpi-card` plus one of `.kpi-accent-blue/green/amber/red`.
- **TABLE BORDER**: Every table widget MUST be wrapped in `<div class="widget-card">`. Placing `<mat-table>` directly inside `.table-grid` without a `.widget-card` wrapper will result in a MISSING BORDER — this is a bug.
- **KPI BORDER**: Every KPI card div MUST carry class `kpi-card`. The CSS rule `border: 1px solid var(--dash-border)` is defined there. Do NOT override or remove the border property.
- **HEADER ACTIONS POSITION**: In `.widget-card-header`, always place the title `<div>` first and the `<div class="widget-header-actions">` last. The `margin-left: auto` on `.widget-header-actions` ensures both download and refresh buttons are flush to the right edge.
- **DOWNLOAD + REFRESH ORDER**: Inside `.widget-header-actions`, always put the download button BEFORE the refresh button (left to right: download, refresh).

=====================
WIDGET CARDS
=====================

Use the CSS classes defined in the LAYOUT section above (`.widget-card`, `.widget-card-header`, etc.)
Do NOT use Angular Material mat-card for widget containers — use the plain div classes instead.

Loading state:
```html
<div class="widget-state-center">
  <mat-spinner diameter="36"></mat-spinner>
  <span>Loading...</span>
</div>
```

Error state:
```html
<div class="widget-state-center">
  <span class="widget-error-msg">{{{{ errorMessage }}}}</span>
  <button class="btn-retry" (click)="loadData()">Retry</button>
</div>
```

Empty state:
```html
<div class="widget-state-center">
  <span class="material-icons" style="font-size:32px;opacity:0.3">inbox</span>
  <span>No data matching the current filters.</span>
</div>
```

For mat-spinner, import MatProgressSpinnerModule in the component imports array.

=====================
CHART WIDGETS — CRITICAL DATA MAPPING
=====================

Render charts using BaseChartDirective (ng2-charts) with Chart.js.

Set Chart.js global defaults (once, in app.component.ts ngOnInit or a shared setup function):
  Chart.defaults.color = '#94a3b8';
  Chart.defaults.borderColor = '#334155';
  Chart.defaults.backgroundColor = 'transparent';

⚠️ XFIELD SAFETY RULE — PREVENTS TS SYNTAX ERRORS:
The `xField` values in chartMeta are ALWAYS clean snake_case identifiers
(e.g. `ticket_created_at`, `suggested_category`, `ticket_requester_id`).
- ALWAYS store xField as a TypeScript STRING LITERAL: `const xField = 'ticket_created_at';`
- NEVER use xField as a bare JavaScript expression or variable name.
- NEVER write: `item[ticket_created_at]` — ALWAYS write: `item['ticket_created_at']` or `item[xField]`
- Use `item['_id']` as the primary fallback if item[xField] is undefined.

For EACH chart widget, map API response to chart data as follows:

**Single-metric chart** (chartMeta.yFields is absent or has 1 element):
```typescript
// After receiving response.data.items from the API:
const items: any[] = response?.data?.items ?? [];

// xField and yField are plain snake_case strings from the widget's chartMeta.
// ALWAYS use them as string literals — NEVER as bare identifiers or expressions.
const xField = 'ticket_created_at';  // use the actual xField value from chartMeta, in quotes
const yField = 'value';              // use the actual yField value from chartMeta, in quotes

// Extract labels — always use bracket notation with a quoted string.
// item['_id'] is the primary fallback (MongoDB aggregation _id before $project).
this.chartLabels = items.map(item =>
  String(item[xField] ?? item['_id'] ?? item['label'] ?? 'N/A')
);

// Extract values
this.chartData = items.map(item =>
  Number(item[yField] ?? item['value'] ?? item['count'] ?? 0)
);
```

**Multi-metric chart** (chartMeta.yFields is an array with 2+ elements, e.g. column chart with multiple quarterly metrics):
When `chartMeta.yFields` exists and has multiple entries, create ONE dataset per yField so each metric gets its own bar/line in the chart:

```typescript
const items: any[] = response?.data?.items ?? [];
const xField = 'group';  // use the actual xField from chartMeta
const yFields: string[] = ['Q1_FY26_Net_Movement', 'Q2_FY26_Net_Movement'];  // use the actual yFields from chartMeta
const palette = ['#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#f97316','#ec4899'];

const labels = items.map(item =>
  String(item[xField] ?? item['_id'] ?? item['label'] ?? 'N/A')
);

const datasets = yFields.map((yf, i) => ({{
  label: yf.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),  // e.g. "Q1_FY26_Net_Movement" → "Q1 FY26 Net Movement"
  data: items.map(item => Number(item[yf] ?? 0)),
  backgroundColor: palette[i % palette.length],
  borderColor: 'transparent',
  borderWidth: 0,
  hoverBackgroundColor: palette[i % palette.length] + 'cc'
}}));

this.chartData = {{ labels, datasets }};
```

NEVER leave labels as a static ['Unknown']. Always derive them from the live API data.
NEVER hardcode chart data. Always read from items[].
NEVER use an xField value as an unquoted JavaScript expression — it is always a string key.
For multi-metric charts, ALWAYS read yFields from chartMeta.yFields (an array) — never hardcode field names.

Chart type mappings (widget type → Chart.js type):
  pie     → 'pie'
  donut   → 'doughnut'
  bar     → 'bar' (horizontal: indexAxis: 'y')
  column  → 'bar' (vertical, default)
  line    → 'line'

Color palettes:
  Pie/Donut backgroundColor: ['#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#f97316','#ec4899']
  Bar/Column backgroundColor: '#3b82f6', hoverBackgroundColor: '#60a5fa'
  Line borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, tension: 0.4

Shared chart options (adapt per type):
```typescript
chartOptions: ChartOptions = {{
  responsive: true,
  maintainAspectRatio: false,
  plugins: {{
    legend: {{
      display: true,
      labels: {{ color: '#94a3b8', font: {{ size: 12 }} }}
    }},
    tooltip: {{
      backgroundColor: '#1e293b',
      titleColor: '#f1f5f9',
      bodyColor: '#94a3b8',
      borderColor: '#334155',
      borderWidth: 1
    }}
  }},
  scales: {{   // omit 'scales' for pie/doughnut
    x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }}, beginAtZero: true }}
  }}
}};
```

Canvas wrapper must have explicit height: `<div style="position:relative; height:280px;"><canvas baseChart ...></canvas></div>`

=====================
BAR CHART PAGINATION (MANDATORY FOR HORIZONTAL BAR CHARTS)
=====================

Horizontal bar charts (indexAxis: 'y') frequently have more items than can fit in a fixed-height
canvas. When ALL data is crammed into a 200-280 px height, the Y-axis labels overlap and most bars
become invisible. Users hover over the chart but can barely read label names.

MANDATORY RULE: For EVERY horizontal bar chart ('bar' with indexAxis: 'y'), implement client-side
pagination so only a PAGE of items is rendered at a time:

```typescript
// Component properties
readonly PAGE_SIZE = 8;         // items visible per chart page
chartPageIndex = 0;              // current page (0-based)
private allLabels: string[] = [];
private allData: number[] = [];

// After mapping API response into labels/data arrays:
loadChartPage(): void {{
  const start = this.chartPageIndex * this.PAGE_SIZE;
  const pageLabels = this.allLabels.slice(start, start + this.PAGE_SIZE);
  const pageData   = this.allData.slice(start, start + this.PAGE_SIZE);

  this.chartLabels = pageLabels;
  this.chartDatasets = [{{
    label: this.title,
    data: pageData,
    backgroundColor: '#3b82f6',
    hoverBackgroundColor: '#60a5fa',
  }}];
  // Dynamic canvas height: 28px per bar, minimum 220px
  this.canvasHeight = Math.max(220, pageLabels.length * 28);
  this.cdr.markForCheck();
}}

get totalPages(): number {{
  return Math.ceil(this.allLabels.length / this.PAGE_SIZE);
}}
prevPage(): void {{ if (this.chartPageIndex > 0) {{ this.chartPageIndex--; this.loadChartPage(); }} }}
nextPage(): void {{ if (this.chartPageIndex < this.totalPages - 1) {{ this.chartPageIndex++; this.loadChartPage(); }} }}
```

Bind canvas height dynamically:
```html
<!-- Horizontal bar chart wrapper — height scales with visible items -->
<div [style.position]="'relative'" [style.height.px]="canvasHeight">
  <canvas baseChart
    [data]="barChartData"
    [options]="chartOptions"
    [type]="'bar'">
  </canvas>
</div>

<!-- Pagination controls (only show when total items > PAGE_SIZE) -->
@if (allLabels.length > PAGE_SIZE) {{
  <div style="display:flex;align-items:center;justify-content:center;gap:12px;padding:8px 0;font-size:12px;color:var(--dash-muted);">
    <button class="btn-clear-filters" (click)="prevPage()" [disabled]="chartPageIndex === 0">‹ Prev</button>
    <span>Page {{{{ chartPageIndex + 1 }}}} / {{{{ totalPages }}}}</span>
    <button class="btn-clear-filters" (click)="nextPage()" [disabled]="chartPageIndex >= totalPages - 1">Next ›</button>
  </div>
}}
```

For VERTICAL bar charts (column charts, indexAxis not set / 'x'), pagination is NOT needed — Chart.js
handles many x-axis labels with auto-rotation. Pagination is ONLY for horizontal bar charts.

=====================
TABLE WIDGETS
=====================

Use mat-table with MatTableDataSource<any>.

Columns: derive ONLY from the widget's requiredFields (display all except 'summary').
⚠️ COLUMN RULE — CRITICAL: The Angular widget config's requiredFields MUST be EXACTLY the fields
listed under "requiredFields (TABLE COLUMNS)" for that widget. NEVER include extra fields as columns
unless they are explicitly listed in requiredFields.
EVERY requiredFields column MUST have a Filter input directly under its header title (see header pattern).
Do NOT add a separate top filter bar.
Include:
- MatSort (matSort directive on mat-table, matSortHeader on each column header)
- MatPaginator (pageSize=5, pageSizeOptions=[5,10,25])
- Loading: overlay spinner
- Sticky header: position: sticky; top: 0; z-index: 2; background: #f8fafc; (LIGHT background — NOT dark)
- Row hover: var(--dash-hover)
- Wrap table in: <div class="table-scroll-wrapper">

⚠️ TABLE EMPTY/LOADING STATE RULE — CRITICAL (PREVENTS HEADER-AT-BOTTOM BUG):
When showing loading, error, or empty state, the <mat-table> MUST be HIDDEN (not just overlaid).
If the <mat-table> is present in the DOM alongside the empty state div, its sticky header row
will render below the empty state, causing the dark header to appear at the bottom — this is a bug.

MANDATORY pattern — use @if/@else if/@else to show EITHER the state OR the table, NEVER both:
```html
@if (isLoading) {{
  <div class="widget-state-center">
    <mat-spinner diameter="36"></mat-spinner>
    <span>Loading...</span>
  </div>
}} @else if (error) {{
  <div class="widget-state-center">
    <span class="widget-error-msg">{{ error }}</span>
    <button class="btn-retry" (click)="loadData()">Retry</button>
  </div>
}} @else if (dataSource.data.length === 0) {{
  <div class="widget-state-center">
    <span class="material-icons" style="font-size:32px;opacity:0.3">inbox</span>
    <span>No data matching the current filters.</span>
  </div>
}} @else {{
  <div class="table-scroll-wrapper">
    <mat-table [dataSource]="dataSource" matSort>
      <!-- columns here -->
      <mat-header-row *matHeaderRowDef="displayedColumns; sticky: true"></mat-header-row>
      <mat-row *matRowDef="let row; columns: displayedColumns;"></mat-row>
    </mat-table>
  </div>
  <mat-paginator [pageSizeOptions]="[5,10,25]" pageSize="5" showFirstLastButtons></mat-paginator>
}}
```

DO NOT place the mat-table outside of the @else block.
DO NOT show mat-table and the empty state div at the same time.

⚠️ ARRAY / OBJECT CELL VALUE RULE — PREVENTS "[object Object]" IN CELLS:
MongoDB documents often contain fields whose values are arrays (e.g. skills: ["TypeScript","Python"])
or arrays of objects (e.g. education: [{{degree:"B.Tech",...}}]). Rendering such a value directly with
`{{{{ row['skills'] }}}}` outputs "[object Object]" or "[object Object],[object Object]".

MANDATORY: add a `displayValue()` helper method to every table component and use it in every
mat-cell that might contain an array or object:

```typescript
displayValue(val: any): string {{
  if (val === null || val === undefined) return '';
  if (Array.isArray(val)) {{
    if (val.length === 0) return '';
    // array of primitives → join with comma
    if (typeof val[0] !== 'object') return val.join(', ');
    // array of objects → extract first meaningful string field
    const keys = ['name','title','degree','label','value','text'];
    return val.map(item => {{
      for (const k of keys) {{ if (item[k] != null) return String(item[k]); }}
      return JSON.stringify(item);
    }}).join('; ');
  }}
  if (typeof val === 'object') return JSON.stringify(val);
  return String(val);
}}
```

⚠️ ROW EXPAND (NOT PER-CELL READ MORE) — MANDATORY FOR EVERY TABLE:
Long fields must NEVER stretch a row to many lines by default.
Clamp EVERY data cell to TWO lines. Do NOT put a "Read more" / "Show less" button on each cell.
Add ONE expand icon as the LAST table column. That icon expands/collapses the ENTIRE row
(all cells in that row unclamp together).

Add these helpers to every table component (alongside displayValue):
```typescript
expandedRows = new Set<number>();
displayedColumns: string[] = [/* requiredFields... */, '_expand'];  // _expand MUST be last
filterValues: Record<string, string> = {{}};  // one key per data column

isRowExpanded(index: number): boolean {{
  return this.expandedRows.has(index);
}}
toggleRow(index: number, event: Event): void {{
  event.stopPropagation();
  if (this.expandedRows.has(index)) this.expandedRows.delete(index);
  else this.expandedRows.add(index);
  this.cdr.markForCheck();
}}
rowNeedsExpand(row: any): boolean {{
  return this.displayedColumns
    .filter(c => c !== '_expand')
    .some(c => this.displayValue(row[c]).length > 80);
}}
clearFilters(): void {{
  for (const k of Object.keys(this.filterValues)) this.filterValues[k] = '';
  this.loadData();
}}
```

Data-cell pattern (NO per-cell button). Header MUST stack title + filter input:
```html
<ng-container matColumnDef="person_name">
  <mat-header-cell *matHeaderCellDef>
    <div class="header-cell-inner">
      <span mat-sort-header>Person Name</span>
      <input class="header-filter-input"
             placeholder="Filter"
             [(ngModel)]="filterValues['person_name']"
             (click)="$event.stopPropagation()"
             (mousedown)="$event.stopPropagation()"
             (input)="onFilterChange()">
    </div>
  </mat-header-cell>
  <mat-cell *matCellDef="let row; let i = index">
    <span [class.cell-clamp]="!isRowExpanded(i)"
          [class.cell-expand]="isRowExpanded(i)">
      {{{{ displayValue(row['person_name']) }}}}
    </span>
  </mat-cell>
</ng-container>
```

Single expand column — MUST be the last column, and MUST be in displayedColumns.
The expand header has a Clear button (no text filter):
```html
<ng-container matColumnDef="_expand">
  <mat-header-cell *matHeaderCellDef class="col-expand">
    <div class="header-cell-inner">
      <span>&nbsp;</span>
      <button type="button" class="btn-clear-filters" (click)="clearFilters()" title="Clear filters">✕</button>
    </div>
  </mat-header-cell>
  <mat-cell *matCellDef="let row; let i = index" class="col-expand">
    @if (rowNeedsExpand(row)) {{
      <button type="button" class="btn-row-expand"
              (click)="toggleRow(i, $event)"
              [attr.title]="isRowExpanded(i) ? 'Show less' : 'Read more'">
        <span class="material-icons">{{{{ isRowExpanded(i) ? 'expand_less' : 'expand_more' }}}}</span>
      </button>
    }}
  </mat-cell>
</ng-container>
```

Rules:
- NEVER add per-cell "Read more" / "Show less" text buttons.
- NEVER dump full cell text without `.cell-clamp` when the row is collapsed.
- Show the expand icon only when `rowNeedsExpand(row)` is true (any cell text longer than ~80 chars).
- Put `.cell-clamp`, `.cell-expand`, `.btn-row-expand`, `.col-expand`, `.header-cell-inner`, `.header-filter-input` in styles.css (not app.component.css).
- Do NOT use `{{{{ row['skills'] }}}}` or `{{{{ row.skills }}}}` directly — ALWAYS wrap with `displayValue()`.

Paginator integration: after data loads, ALWAYS wire the paginator and sort inside a `setTimeout`
so Angular's `@ViewChild` is guaranteed to be initialized before assignment:

```typescript
@ViewChild(MatPaginator) paginator!: MatPaginator;
@ViewChild(MatSort) sort!: MatSort;
dataSource = new MatTableDataSource<any>([]);

loadData(): void {{
  this.isLoading = true;
  this.cdr.markForCheck();
  this.apiService.callWidget(this.functionId, this.buildFilters())
    .pipe(takeUntilDestroyed(this.destroyRef))
    .subscribe({{
      next: (res: any) => {{
        const items = res?.data?.items ?? [];
        this.dataSource.data = items;
        this.isLoading = false;
        // Wire paginator and sort INSIDE setTimeout to ensure ViewChild refs are ready
        setTimeout(() => {{
          this.dataSource.paginator = this.paginator;
          this.dataSource.sort = this.sort;
        }}, 0);
        this.cdr.markForCheck();
      }},
      error: (err: any) => {{
        this.error = err?.message ?? 'Failed to load data';
        this.isLoading = false;
        this.cdr.markForCheck();
      }}
    }});
}}
```

NEVER set `dataSource.paginator` or `dataSource.sort` outside of a `setTimeout(() => {{ ... }}, 0)`
block — doing so synchronously causes the paginator to see stale page counts and show all rows
regardless of the selected page size.

{kpi_derivation_section}

=====================
TABLE WIDGET FILTERS (TABULAR ONLY)
=====================

IMPORTANT: Filter controls ONLY on tabular (table) widgets. Chart widgets have NO filters.

Do NOT use a separate top `.table-filter-bar` row of inputs.
EVERY data column MUST have its own filter `<input>` placed DIRECTLY BELOW the column header title
inside `mat-header-cell` (Excel-style). The `_expand` column has no text filter — put a Clear button there instead.

Use `filterValues: Record<string, string> = {{}}` keyed by the exact column field name.
One input per requiredFields column. Bind `[(ngModel)]="filterValues['<field>']"`.
Stop click/mousedown on the input so it does not trigger mat-sort.

```html
<mat-header-cell *matHeaderCellDef>
  <div class="header-cell-inner">
    <span mat-sort-header>{{{{ columnTitle }}}}</span>
    <input class="header-filter-input" placeholder="Filter"
           [(ngModel)]="filterValues['field_name']"
           (click)="$event.stopPropagation()"
           (mousedown)="$event.stopPropagation()"
           (input)="onFilterChange()">
  </div>
</mat-header-cell>
```

Use ONLY plain HTML `<input>` elements — NOT MatFormField/mat-input for filters.

Store filter values as a Record (no FormGroup needed).
Use (input) event with debounceTime(400) to re-fetch data:
```typescript
filterSubject$ = new Subject<void>();
ngOnInit() {{
  this.filterSubject$.pipe(debounceTime(400), takeUntilDestroyed(this.destroyRef))
    .subscribe(() => this.loadData());
  this.loadData();
}}
onFilterChange() {{ this.filterSubject$.next(); }}
```

LIVE SCHEMA FILTER RECONCILIATION — IMPORTANT:
Table widgets must reconcile their hardcoded filterableFields against the live collection
schema returned in every API response (response.data.schema). This ensures filters still
work correctly even if field names were changed in the project after the dashboard was
originally generated.

After the first successful data load, run this reconciliation once:
```typescript
private liveSchemaKeys: string[] = [];
private filtersReconciled = false;

// call this inside loadData() after receiving a successful response:
private reconcileFiltersWithLiveSchema(schema: Record<string, any>): void {{
  if (this.filtersReconciled || !schema || !Object.keys(schema).length) return;
  this.liveSchemaKeys = Object.keys(schema);
  // For each hardcoded filter field, find the best match in the live schema keys.
  // If the field exists as-is, keep it; otherwise try case-insensitive / stem match.
  const stem = (s: string) => s.toLowerCase().replace(/[_\\-\\s]/g, '');
  this.filterableFields = this.filterableFields.map(f => {{
    if (this.liveSchemaKeys.includes(f)) return f;
    const lower = f.toLowerCase();
    const found = this.liveSchemaKeys.find(k => k.toLowerCase() === lower)
               || this.liveSchemaKeys.find(k => stem(k) === stem(f));
    return found ?? f;
  }});
  // Reset filter values to avoid sending stale keys
  const newValues: Record<string, string> = {{}};
  for (const field of this.filterableFields) {{
    newValues[field] = this.filterValues[field] ?? '';
  }}
  this.filterValues = newValues;
  this.filtersReconciled = true;
}}
```

Call `this.reconcileFiltersWithLiveSchema(response.data.schema ?? {{}})` immediately after
a successful `callWidget` response, before setting `this.dataSource.data`.

Build the filters object from header filterValues (every data column, not a separate bar):
```typescript
buildFilters(): Record<string, any> {{
  const f: Record<string, any> = {{}};
  for (const [field, val] of Object.entries(this.filterValues)) {{
    if (typeof val === 'string' && val.trim()) {{
      f[field] = val.trim();
    }}
  }}
  return f;
}}
```

=====================
RESPONSIVENESS
=====================

Use CSS Grid with media queries (not Tailwind breakpoints):

  /* Desktop */
  @media (min-width: 1024px) {{
    .kpi-grid   {{ grid-template-columns: repeat(4, 1fr); }}
    .chart-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .table-grid {{ min-width: 90%; max-width: 100%; }}  /* tables always full-width */
  }}
  /* Tablet */
  @media (min-width: 600px) and (max-width: 1023px) {{
    .kpi-grid   {{ grid-template-columns: repeat(2, 1fr); }}
    .chart-grid {{ grid-template-columns: 1fr; }}
    .table-grid {{ min-width: 90%; max-width: 100%; }}
  }}
  /* Mobile */
  @media (max-width: 599px) {{
    .kpi-grid, .chart-grid, .table-grid {{ min-width: 90%; max-width: 100%; }}
  }}

Apply display: grid; gap: 20px; to each grid container.
NOTE: .table-grid is always full-width.

=====================
DOWNLOAD — EXCEL (TABULAR) & PDF (CHARTS)
=====================

Every widget card header MUST include a download icon button (class `.btn-widget-download`) placed
BEFORE the refresh button inside a `.widget-header-actions` wrapper.

**Required npm packages** (add to package.json dependencies, they MUST be listed in package.json):
```json
"xlsx": "^0.18.5",
"jspdf": "^2.5.1"
```

**Imports at the top of each component file:**
```typescript
import * as XLSX from 'xlsx';
import {{ jsPDF }} from 'jspdf';
```

---

### TABULAR WIDGET — Download all rows as Excel (.xlsx)

The download exports ALL currently loaded rows (this.dataSource array) — NOT just the visible page.
Use the SheetJS `xlsx` library.

```typescript
downloadExcel(): void {{
  const rows: any[] = Array.isArray(this.dataSource)
    ? this.dataSource
    : (this.dataSource?.data ?? []);
  if (!rows.length) return;

  // Export only visible table columns. NEVER include MongoDB _id (or the _expand column).
  const skip = new Set(['_id', '_expand']);
  const cols: string[] = (this.displayedColumns?.length
    ? this.displayedColumns
    : Object.keys(rows[0] || {{}}))
    .filter((c: string) => !skip.has(c) && !c.startsWith('_'));

  const exportRows = rows.map(row => {{
    const out: Record<string, any> = {{}};
    for (const col of cols) {{
      out[col] = typeof this.displayValue === 'function' ? this.displayValue(row[col]) : row[col];
    }}
    return out;
  }});

  const ws: XLSX.WorkSheet = XLSX.utils.json_to_sheet(exportRows);

  const colWidths = cols.map(key => ({{ wch: Math.max(key.length, 12) }}));
  ws['!cols'] = colWidths;

  const wb: XLSX.WorkBook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Report');
  XLSX.writeFile(wb, `${{this.title || 'report'}}.xlsx`);
}}
```

The `downloadExcel()` method must be called from the download button in the widget header:
```html
<button class="btn-widget-download" (click)="downloadExcel()" title="Download Excel">
  <span class="material-icons" style="font-size:14px">download</span>
</button>
```

---

### CHART WIDGET — Download chart canvas as PDF

For ALL non-tabular widgets (Bar Chart, Column Chart, Line Chart, Pie Chart, Donut Chart, Area Chart):
export the chart as a PDF using `jsPDF` directly from the Chart.js canvas element.

Each chart component MUST have a `@ViewChild` reference to the `BaseChartDirective`:
```typescript
@ViewChild(BaseChartDirective) chartRef!: BaseChartDirective;
```

```typescript
downloadPdf(): void {{
  if (!this.chartRef?.chart) return;

  const canvas = this.chartRef.chart.canvas as HTMLCanvasElement;
  const imgData = canvas.toDataURL('image/png', 1.0);

  // Use canvas pixel dimensions for the PDF page size so the chart fills the page
  const pxW = canvas.width;
  const pxH = canvas.height;

  // jsPDF uses points; pass px dimensions directly with hotfixes for correct scaling
  const pdf = new jsPDF({{
    orientation: pxW >= pxH ? 'landscape' : 'portrait',
    unit: 'px',
    format: [pxW, pxH],
    hotfixes: ['px_scaling'],
  }});

  // Add a white background so transparent chart areas export correctly
  pdf.setFillColor(255, 255, 255);
  pdf.rect(0, 0, pxW, pxH, 'F');

  pdf.addImage(imgData, 'PNG', 0, 0, pxW, pxH);
  pdf.save(`${{this.title || 'chart'}}.pdf`);
}}
```

The `downloadPdf()` method must be called from the download button in the widget header:
```html
<button class="btn-widget-download" (click)="downloadPdf()" title="Download PDF">
  <span class="material-icons" style="font-size:14px">download</span>
</button>
```

CRITICAL RULES for download:
- `xlsx` is imported as `import * as XLSX from 'xlsx'` — NOT as a default import.
- `jsPDF` is imported as `import {{ jsPDF }} from 'jspdf'` (named export).
- The `BaseChartDirective` `@ViewChild` reference (`this.chartRef`) is the ONLY correct way to access the Chart.js canvas — do NOT use `document.querySelector`.
- Do NOT use `html2canvas` — the chart canvas is directly accessible via `chartRef.chart.canvas`.
- For KPI cards there is NO download button — KPI widgets only show a refresh button (if any).
- If `this.dataSource` is empty (no data loaded yet), `downloadExcel()` must silently return without error.
- NEVER include `_id` or `_expand` in the Excel file. Export only the visible table columns (`displayedColumns` minus `_expand`). Do NOT pass raw MongoDB documents to `json_to_sheet`.
- If `this.chartRef?.chart` is undefined (chart not yet rendered), `downloadPdf()` must silently return without error.

=====================
API SERVICE & ERROR HANDLING
=====================
- Injectable providedIn root
- Method for individual widget refresh: callWidget(functionId: string, filters: Record<string, any> = {{}}): Observable<any>
- Method for concurrency-limited batch loading / Refresh All (max 2 concurrent API calls):
  ```typescript
  loadAllWidgets(widgetConfigs: {{ functionId: string }}[]): Observable<any[]> {{
    return from(widgetConfigs).pipe(
      mergeMap(config => this.callWidget(config.functionId, {{}}).pipe(
        map(response => ({{ functionId: config.functionId, response }}))
      ), 2),  // max 2 concurrent
      scan((acc, val) => [...acc, val], [] as any[]),
    );
  }}
  ```
- Payload format EXACTLY:
  {{ "function_id": functionId, "params": {{ "filters": filters, "limit": 500 }}, "request_meta": {{}} }}
  IMPORTANT: always send "filters" as an object ({{}}), never null or undefined
- Build URL from environment.apiBaseUrl + environment.agentBasePath + '/dynamic/execute/<PROJECT_ID>'
- Use catchError to NEVER let an error propagate — each widget is independently isolated:
  ```typescript
  catchError((err: HttpErrorResponse) => {{
    const msg = err?.error?.message ?? err?.message ?? `HTTP ${{err?.status ?? 'error'}}`;
    return of({{ status: 'error', data: {{ count: 0, items: [] }}, message: msg, error: msg }});
  }})
  ```
- Each widget component catches errors locally — a failure in one widget MUST NOT affect any other widget
- Refresh All: Call the shared loadAllWidgets() method on the DashboardApiService (max 2 concurrent requests) to refresh all widgets.

CRITICAL: Do NOT share subscriptions between widgets. Each widget has its own independent HTTP call.

=====================
BACKEND
=====================

Every widget calls its own functionId via the dynamic execute endpoint.

Request format:
{{
    "function_id": "<functionId>",
    "params": {{
        "filters": {{}},
        "limit": 500
    }},
    "request_meta": {{}}
}}

Response format:
{{
    "status": "success",
    "data": {{
        "count": 0,
        "items": []
    }},
    "message": "",
    "error": null
}}

Always read response.data.items for chart/table data.
For count-based KPIs, use response.data.count.

=====================
WIDGETS
=====================

Note: The initial load of all widgets MUST call the concurrency-limited loadAllWidgets() method on the DashboardApiService (max 2 concurrent API calls) to load widgets in batches, rather than triggering them all independently.

{widgets_desc}

=====================
GENERAL REQUIREMENTS
=====================

- One page, no routing, no router-outlet.
- Every widget fetches using the concurrency-limited loadAllWidgets() method on initial load; table widgets also re-fetch on their own filter change using callWidget().
- Chart widgets have NO filter controls.
- Table widgets show filter controls ONLY inside their own card, above the table.
- Proper loading spinner + error state + empty state per widget.
- Each widget is fully isolated — a HTTP 4xx/5xx error in one widget must NOT affect others.
- Charts: labels and values always derived from live API items[] using chartMeta.xField / yField.
- Professional light-themed enterprise UI matching the visual theme spec above.
- No mock data anywhere.
- Strongly typed (interfaces for each widget's data shape).
- RxJS cleanup via takeUntilDestroyed() or async pipe.
- Angular animations on KPI card entrance (fade + slide-up).
- Refresh All: Call the shared loadAllWidgets() method on the DashboardApiService (max 2 concurrent requests) to refresh all widgets, rather than triggering all requests independently.
- **DOWNLOAD**: Every chart/table widget MUST include a download button (`class="btn-widget-download"`) inside `.widget-header-actions` BEFORE the refresh button. Chart widgets call `downloadPdf()`, table widgets call `downloadExcel()`. KPI cards have NO download button. Include `xlsx` and `jspdf` in package.json.
"""

    dashboard_screen_id = f"dashboard_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    dashboard_url = None
    error_message: Optional[str] = None
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    try:
        angular_generator = AngularAppGenerator()
        # Build backend-determined IR schema AFTER backend functions are registered
        # so the frontend is generated from the exact field contracts the backend exposes
        # (sequential: backend schema → frontend generation).
        ir_schema = _build_dashboard_ir_schema(dashboard_def, backend_metadata)
        result = await angular_generator.generate_app(
            user_prompt=prompt,
            project_id=project_id,
            screen_id=dashboard_screen_id,
            backend_metadata=backend_metadata,
            ir_schema=ir_schema,
        )
        token_usage = result.get("token_usage", {})
        logger.info(
            "Token usage for dashboard screen %s: prompt_tokens=%s, completion_tokens=%s, total_tokens=%s",
            dashboard_screen_id,
            token_usage.get("prompt_tokens"),
            token_usage.get("completion_tokens"),
            token_usage.get("total_tokens"),
        )
        base_url = getattr(angular_generator, "deploy_base_url", "")
        if base_url:
            dashboard_url = f"{base_url.rstrip('/')}/{dashboard_screen_id}"
        success = True

        # Persist to screen_generations and screens collections
        try:
            deploy_base_url = getattr(angular_generator, "deploy_base_url", "https://liberty-fs.gibots.com:843")
            public_url = result.get("public_url")
            if not public_url:
                public_url = f"{deploy_base_url.rstrip('/')}/{dashboard_screen_id}-v{version}"

            angular_metadata = {
                "workspace_directory": result.get("workspace_directory"),
                "public_url": public_url,
                "version": result.get("version"),
                "last_build_at": datetime.utcnow(),
            }
            backend_meta_save = {
                "base_path": backend_metadata.get("base_path"),
                "functions": [
                    {
                        "function_id": func.get("id"),
                        "type": func.get("type"),
                        "description": func.get("description"),
                        "input_model": func.get("input_model"),
                        "response_model": func.get("response_model"),
                    }
                    for func in backend_metadata.get("functions", [])
                ],
            }

            # Build a minimal dashboard-definition-derived IR schema for persistence
            dashboard_ir_schema = _build_dashboard_ir_schema(dashboard_def, backend_metadata)

            v1_doc: Dict[str, Any] = {
                "ir_schema": dashboard_ir_schema,
                "angular": angular_metadata,
                "backend": backend_meta_save,
                "prompt": prompt,
                "screen_name": dashboard_def.dashboardName,
                "description": f"Dashboard with {len(dashboard_def.widgets)} widget(s): {', '.join(w.reportName for w in dashboard_def.widgets)}",
                "original_prompt_enhanced": "",
                "screen_spec": {},
                "components": dashboard_ir_schema.get("components", []),
                "permissions": None,
                "theme_preferences": None,
                "screen_type": "dashboard",
                "status": "generated",
                "chat_history": {"max_messages": 10, "messages": []},
            }
            if org_id is not None:
                try:
                    v1_doc["orgId"] = ObjectId(org_id) if isinstance(org_id, str) else org_id
                except Exception:
                    pass
            if subscriber_id is not None:
                try:
                    v1_doc["subscriberId"] = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
                except Exception:
                    pass
            if user_id is not None:
                try:
                    v1_doc["userId"] = ObjectId(user_id) if isinstance(user_id, str) else user_id
                except Exception:
                    pass

            screen_repo = ScreenRepository()
            await screen_repo.insert_screen_version_doc(
                project_id,
                dashboard_screen_id,
                version,
                "v1",
                v1_doc,
                build_status="success",
                is_stable=True,
            )
            # Update screens pointer collection
            from services.database import Database
            subscriber_db_name = f"{Database._db_name}_sb_{subscriber_id}" if subscriber_id else None
            await screen_repo.update_screens_pointer(
                project_id,
                dashboard_screen_id,
                version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                subscriber_db_name=subscriber_db_name,
                user_id=user_id,
            )
            # Sync to capability repository (non-fatal if it fails)
            try:
                screen_doc = {**v1_doc, "version": version}
                capability_repo = CapabilityRepository()
                await sync_screen_single(capability_repo, screen_doc)
            except Exception as cap_exc:
                logger.warning("Capability sync after dashboard screen generation failed: %s", cap_exc)
        except Exception as persist_exc:
            logger.warning("Failed to persist dashboard screen record (screen still deployed): %s", persist_exc)
    except Exception as e:
        logger.exception("Failed to generate dashboard screen for project %s", project_id)
        success = False
        error_message = str(e)

    screens_created = [
        ReportScreenItem(
            screenId=dashboard_screen_id,
            screenName=dashboard_def.dashboardName,
            reportName=w.reportName,
            reportType=w.reportType,
            success=success,
            error=error_message,
        )
        for w in dashboard_def.widgets
    ]

    return CreateDashboardScreenResponse(
        success=success,
        workflowId=workflow_id or "",
        projectId=project_id or "",
        screens=screens_created,
        dashboardScreenId=dashboard_screen_id if success else None,
        dashboardUrl=dashboard_url,
        warnings=_all_warnings,
        message=(
            f"Created 1 dashboard screen with {len(dashboard_def.widgets)} live widget(s)"
            if success
            else f"Failed to generate dashboard screen: {error_message}"
        ),
        dashboard=dashboard_def,
    )


@router.post("/update-screen", response_model=UpdateDashboardScreenResponse)
async def update_dashboard_screen(
    request: UpdateDashboardScreenRequest,
    http_request: Request,
) -> UpdateDashboardScreenResponse:
    """
    Add new widgets to an existing dashboard (or create a new one if none exists)
    with the following enhancements over the legacy update path:

    1. **Sequential backend-first generation** — backend functions are registered
       and their field schemas determined before the Angular generator runs.
    2. **LLM report description enhancement** — free-text user descriptions are
       passed through Gemini to produce fully-structured report definitions before
       ``build_dashboard_definition`` processes them.
    3. **Dashboard versioning** — each successful update increments
       ``agentic_process.dashboard_version``; a ``version`` mismatch returns 409.
    4. **Schema merging** — ``request.collectionSchema`` is merged with any schema
       already stored in ``Process_Configs`` so the LLM sees up-to-date field info.
    5. **Report validation** — new reports are validated via
       ``initialize_report_planning_session`` before generation.
    """
    from services.report_planning_service import (
        initialize_report_planning_session,
        build_available_schema_from_db_schemas,
        VALID_REPORT_TYPES as _VALID_REPORT_TYPES,
    )

    org_id = request.orgId or http_request.headers.get("x-org-id")
    user_id = request.userId or http_request.headers.get("x-user-id")
    subscriber_id = request.subscriberId or http_request.headers.get("x-subscriber-id")

    if not request.reports:
        raise HTTPException(status_code=400, detail="'reports' array is required and must not be empty.")

    project_id = request.projectId.strip()
    dashboard_name = request.dashboardName or "Dashboard"
    warnings: List[str] = []

    # ── 1. Load existing Process_Configs document ────────────────────────────
    pc_coll = Database.get_collection("Process_Configs")
    pc_query: Dict[str, Any] = {"agentic_process.workflow_id": project_id}
    if subscriber_id:
        try:
            pc_query["subscriberId"] = {"$in": [ObjectId(subscriber_id), subscriber_id]}
        except Exception:
            pc_query["subscriberId"] = subscriber_id
    if org_id:
        try:
            pc_query["orgId"] = {"$in": [ObjectId(org_id), org_id]}
        except Exception:
            pc_query["orgId"] = org_id

    existing_doc: Optional[Dict[str, Any]] = await pc_coll.find_one(pc_query)
    agentic_proc: Dict[str, Any] = (existing_doc or {}).get("agentic_process") or {}

    existing_screen_id: Optional[str] = agentic_proc.get("dashboardScreenId")
    existing_def_data: Optional[Dict[str, Any]] = agentic_proc.get("dashboardDefinition")
    existing_reports_stored: List[Dict[str, Any]] = list(agentic_proc.get("report") or [])
    existing_dashboard_url: Optional[str] = agentic_proc.get("dashboardUrl")

    # ── 2. Version check (optimistic concurrency) ────────────────────────────
    stored_version: int = int(agentic_proc.get("dashboard_version") or 0)
    if request.version is not None and request.version != stored_version:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Version conflict",
                "clientVersion": request.version,
                "serverVersion": stored_version,
                "message": (
                    f"Dashboard was updated since you last fetched it "
                    f"(client version {request.version}, server version {stored_version}). "
                    "Reload the dashboard and retry."
                ),
            },
        )

    # ── 3. Merge collection schemas ──────────────────────────────────────────
    # Priority: request.collectionSchema overrides stored schema entries for matching
    # collection names; new collections are appended.
    stored_schema: Optional[Any] = agentic_proc.get("collection_schema")
    merged_schema: Optional[Any] = stored_schema

    if request.collectionSchema:
        if stored_schema is None:
            merged_schema = request.collectionSchema
        elif isinstance(stored_schema, list) and isinstance(request.collectionSchema, list):
            stored_by_name = {
                (e.get("collection_name") or e.get("name") or ""): e
                for e in stored_schema
                if isinstance(e, dict)
            }
            for new_entry in request.collectionSchema:
                if isinstance(new_entry, dict):
                    key = new_entry.get("collection_name") or new_entry.get("name") or ""
                    stored_by_name[key] = new_entry
            merged_schema = list(stored_by_name.values())
        # Persist merged schema back to Process_Configs (non-fatal)
        if existing_doc:
            try:
                await pc_coll.update_one(
                    {"_id": existing_doc["_id"]},
                    {"$set": {"agentic_process.collection_schema": merged_schema}},
                )
            except Exception as schema_merge_err:
                logger.warning("update-screen: Failed to persist merged schema: %s", schema_merge_err)

    # ── 4. LLM enhancement of unstructured report descriptions ───────────────
    enhanced_reports = await enhance_report_descriptions(
        user_reports=list(request.reports),
        collection_schema=merged_schema,
        workflow_name=dashboard_name,
    )

    # ── 5. Validate reports via report_planning_service ──────────────────────
    available_schema: Dict[str, Any] = {}
    if merged_schema:
        available_schema = build_available_schema_from_db_schemas(
            merged_schema if isinstance(merged_schema, list) else []
        )
    planning_session = initialize_report_planning_session(enhanced_reports, available_schema or None)
    for entry in planning_session.entries:
        for vr in entry.validationResults:
            if vr.status in ("invalid", "missing"):
                warnings.append(
                    f"Report '{entry.reportName}' [{vr.field}]: {vr.message}"
                )
    # Ensure all reports have a valid reportType before building widget def
    for rep in enhanced_reports:
        if rep.get("reportType") not in _VALID_REPORT_TYPES:
            rep["reportType"] = "Tabular"

    # ── 6. Build definitions, register backend functions, generate/update UI ──
    if existing_screen_id and existing_def_data:
        # UPDATE PATH: incremental — only process new reports
        try:
            existing_def = DashboardDefinition(**existing_def_data)
        except Exception as parse_err:
            logger.warning("update-screen: Failed to parse stored DashboardDefinition: %s", parse_err)
            # Attempt widget-by-widget reconstruction to avoid silently wiping
            # all existing widgets when the outer model parse fails (e.g. after
            # a schema migration that adds required fields).
            try:
                raw_widgets = [
                    DashboardWidget(**w)
                    for w in (existing_def_data.get("widgets") or [])
                ]
                existing_def = DashboardDefinition(
                    dashboardId=existing_def_data.get("dashboardId", f"dashboard_{project_id}"),
                    dashboardName=existing_def_data.get("dashboardName", dashboard_name),
                    widgets=raw_widgets,
                )
                logger.info(
                    "update-screen: Recovered %d existing widgets via raw reconstruction",
                    len(raw_widgets),
                )
                warnings.append(
                    f"Stored dashboard definition had a schema mismatch; "
                    f"recovered {len(raw_widgets)} widget(s) via raw reconstruction."
                )
            except Exception as recovery_err:
                logger.error(
                    "update-screen: Cannot recover existing widgets, aborting update: %s",
                    recovery_err,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Stored dashboard definition is corrupted and cannot be safely updated. "
                        "Please contact support or recreate the dashboard."
                    ),
                )

        existing_names: set = {
            r.get("reportName", "").strip().lower()
            for r in existing_reports_stored
        }
        _existing_stored_by_name = {
            r.get("reportName", "").strip().lower(): r
            for r in existing_reports_stored
        }

        new_reports = diff_new_reports(existing_reports_stored, enhanced_reports)

        # Detect existing reports whose description (first line = user intent) changed.
        # Compare the first line of the incoming enhanced description against what
        # is currently stored so trivial re-wording triggers a refresh.
        def _first_line(text: str) -> str:
            return (text or "").split("\n")[0].strip().lower()

        changed_reports = [
            r for r in enhanced_reports
            if r.get("reportName", "").strip().lower() in existing_names
            and _first_line(r.get("description", "")) != _first_line(
                _existing_stored_by_name.get(r.get("reportName", "").strip().lower(), {}).get("description", "")
            )
        ]

        # ── No new reports AND no description changes → return early ──────────
        if not new_reports and not changed_reports:
            new_version = stored_version  # no change
            return UpdateDashboardScreenResponse(
                success=True,
                projectId=project_id,
                dashboardScreenId=existing_screen_id,
                dashboardUrl=existing_dashboard_url,
                dashboardName=dashboard_name,
                dashboard=existing_def,
                screens=[
                    ReportScreenItem(
                        screenId=existing_screen_id,
                        screenName=dashboard_name,
                        reportName=w.reportName,
                        reportType=w.reportType,
                        success=True,
                    )
                    for w in (existing_def.widgets if existing_def else [])
                ],
                version=new_version,
                warnings=["No new reports detected; existing screen returned unchanged."],
                message="Dashboard unchanged — all requested reports already exist.",
            )

        # ── Enrich changed existing reports without adding new widgets ─────────
        # Find the already-registered widgets for changed reports and re-run the
        # LLM query-plan description against them, then update both the widget
        # (so combined_def is current) and the report entry (so it's persisted).
        if changed_reports and existing_def:
            # Use case-insensitive lookup to match the change-detection logic above
            # (which also uses .strip().lower()). Without this, a report stored as
            # "Sales Report" would not be found if the incoming name is "sales report".
            _changed_names_ci = {r.get("reportName", "").strip().lower() for r in changed_reports}
            _changed_widgets = [
                w for w in existing_def.widgets
                if w.reportName.strip().lower() in _changed_names_ci
            ]
            if _changed_widgets:
                # Build a CI-keyed lookup so intent seeding works regardless of
                # whether the incoming name differs in casing from the stored one.
                _new_intent_by_name_ci = {
                    r.get("reportName", "").strip().lower(): r.get("description", "")
                    for r in changed_reports
                }
                # Seed each widget with the new first-line intent before the LLM run
                for _cw in _changed_widgets:
                    _cw.description = _first_line(
                        _new_intent_by_name_ci.get(_cw.reportName.strip().lower(), "")
                    )
                _changed_llm_descs = await _llm_build_query_plan_descriptions(_changed_widgets)
                for _cw in _changed_widgets:
                    if _cw.reportName in _changed_llm_descs:
                        _cw.description = _changed_llm_descs[_cw.reportName]
                # Sync enriched data back to enhanced_reports for persistence.
                # Key by lower-cased name so the sync also tolerates case differences.
                _changed_widget_data_ci = {
                    _cw.reportName.strip().lower(): {
                        "description": _cw.description or "",
                        "collectionName": _cw.datasource,
                        "requiredFields": _cw.requiredFields,
                        "optionalFilters": [f.get("field", "") for f in (_cw.filters or []) if f.get("field")],
                    }
                    for _cw in _changed_widgets
                }
                for _rep in enhanced_reports:
                    _rep_key = _rep.get("reportName", "").strip().lower()
                    if _rep_key in _changed_widget_data_ci:
                        _rep.update(_changed_widget_data_ci[_rep_key])

        # ── Description-only early return (no new widgets) ───────────────────
        # When all incoming reports already exist and only their descriptions
        # changed, skip Angular regeneration entirely: persist the updated
        # metadata to Process_Configs and return.  Regenerating Angular with
        # "Add 0 new widget(s)" wastes an LLM call and risks introducing
        # unintended UI drift.
        if not new_reports:
            combined_def = DashboardDefinition(
                dashboardId=(existing_def.dashboardId if existing_def else f"dashboard_{project_id}"),
                dashboardName=dashboard_name,
                widgets=(existing_def.widgets if existing_def else []),
            )
            _enhanced_by_name_lower = {
                r.get("reportName", "").strip().lower(): r for r in enhanced_reports
            }
            _changed_names_lower = {r.get("reportName", "").strip().lower() for r in changed_reports}
            all_reports_combined = [
                _enhanced_by_name_lower[r.get("reportName", "").strip().lower()]
                if r.get("reportName", "").strip().lower() in _changed_names_lower
                else r
                for r in existing_reports_stored
            ] + [
                r for r in enhanced_reports
                if r.get("reportName", "").strip().lower() not in existing_names
            ]
            new_version = stored_version + 1
            desc_set_update: Dict[str, Any] = {
                "agentic_process.dashboardName": dashboard_name,
                "agentic_process.report": all_reports_combined,
                "agentic_process.dashboardDefinition": combined_def.model_dump(),
                "agentic_process.dashboardScreenId": existing_screen_id,
                "agentic_process.dashboardUrl": existing_dashboard_url,
                "agentic_process.dashboard_version": new_version,
                "updated_at": datetime.utcnow(),
            }
            if existing_doc:
                await pc_coll.update_one({"_id": existing_doc["_id"]}, {"$set": desc_set_update})
            screens_out = [
                ReportScreenItem(
                    screenId=existing_screen_id,
                    screenName=dashboard_name,
                    reportName=w.reportName,
                    reportType=w.reportType,
                    success=True,
                )
                for w in combined_def.widgets
            ]
            return UpdateDashboardScreenResponse(
                success=True,
                projectId=project_id,
                dashboardScreenId=existing_screen_id,
                dashboardUrl=existing_dashboard_url,
                dashboardName=dashboard_name,
                dashboard=combined_def,
                screens=screens_out,
                version=new_version,
                warnings=warnings,
                message=f"Updated description for {len(changed_reports)} existing report(s).",
            )

        widget_offset = len(existing_def.widgets) if existing_def else 0
        new_def, new_def_warnings = build_dashboard_definition(
            project_id, dashboard_name, new_reports,
            collection_schemas=merged_schema,
        )
        warnings.extend(new_def_warnings)
        for idx, w in enumerate(new_def.widgets):
            w.reportId = f"report_{widget_offset + idx + 1}"

        # Offset grid rows so new widgets are placed below the existing ones
        # rather than overlapping at row 1 (build_dashboard_definition always
        # starts _assign_grid_layout at row 1).
        if existing_def and existing_def.widgets:
            max_existing_row = max(
                (w.gridRow or 1) + (w.gridHeight or 4) - 1
                for w in existing_def.widgets
            )
            for w in new_def.widgets:
                if w.gridRow is not None:
                    w.gridRow += max_existing_row

        # Enrich widget descriptions with LLM-generated query-plan bullet points
        _llm_descs = await _llm_build_query_plan_descriptions(new_def.widgets)
        for w in new_def.widgets:
            if w.reportName in _llm_descs:
                w.description = _llm_descs[w.reportName]

        # Sync enriched descriptions back to enhanced_reports so they are
        # persisted in agentic_process.report at the $set save below.
        # Also sync the widget-resolved collectionName and requiredFields so the
        # stored report objects contain ALL LLM-enriched details in one place.
        _widget_data_by_name = {
            w.reportName: {
                "description": w.description or "",
                "collectionName": w.datasource,
                "requiredFields": w.requiredFields,
                "optionalFilters": [f.get("field", "") for f in (w.filters or []) if f.get("field")],
            }
            for w in new_def.widgets
        }
        for _rep in enhanced_reports:
            _rname = _rep.get("reportName", "")
            if _rname in _widget_data_by_name:
                # Explicitly overwrite each key so the save reflects the fully
                # resolved widget data regardless of what came in from the user.
                _rep["description"] = _widget_data_by_name[_rname]["description"]
                _rep["collectionName"] = _widget_data_by_name[_rname]["collectionName"]
                _rep["requiredFields"] = _widget_data_by_name[_rname]["requiredFields"]
                _rep["optionalFilters"] = _widget_data_by_name[_rname]["optionalFilters"]

        # Register backend functions (sequential — before Angular generation)
        new_backend_metadata: Dict[str, Any] = {"base_path": None, "functions": []}
        try:
            # Load field aliases for this project (populated via /update-field-aliases)
            _update_field_aliases_map: Optional[Dict[str, Dict[str, str]]] = None
            _raw_aliases = agentic_proc.get("field_aliases")
            if _raw_aliases and isinstance(_raw_aliases, dict):
                _update_field_aliases_map = _raw_aliases
            new_backend_metadata = await register_dashboard_backend_functions(
                project_id=project_id,
                dashboard_def=new_def,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                field_aliases_map=_update_field_aliases_map,
            )
        except Exception:
            logger.exception("update-screen: Failed to register new backend functions for project %s", project_id)

        combined_def = DashboardDefinition(
            dashboardId=(existing_def.dashboardId if existing_def else f"dashboard_{project_id}"),
            dashboardName=dashboard_name,
            widgets=((existing_def.widgets if existing_def else []) + new_def.widgets),
        )

        # Build IR schema from backend-determined field contracts
        update_ir_schema = _build_dashboard_ir_schema(combined_def, new_backend_metadata)

        update_prompt = (
            f'Add {len(new_reports)} new widget(s) to the "{dashboard_name}" dashboard.\n\n'
            "DO NOT remove or modify any existing widgets. Only INSERT these new widgets:\n\n"
            + "\n".join(
                f'  - "{w.title}" ({w.widgetType}), functionId={w.functionId}, '
                f'collection={w.datasource}, '
                f'TABLE COLUMNS (requiredFields — display only these)={w.requiredFields}, '
                f'HEADER FILTERS: one input under every requiredFields column title; _expand column gets Clear only'
                for w in new_def.widgets
            )
            + "\n\nFollow the same visual theme, layout, and Angular patterns already present.\n"
            "Each new widget must call its functionId via the existing ApiService pattern.\n"
            "Use ChangeDetectorRef.markForCheck() after every HTTP data load.\n"
        )
        if merged_schema:
            schema_parts = ["\n\nDATABASE COLLECTION SCHEMAS (actual field names):"]
            if isinstance(merged_schema, list):
                for entry in merged_schema:
                    if isinstance(entry, dict):
                        coll = entry.get("collection_name") or entry.get("name", "")
                        flds = entry.get("schema") or entry.get("fields") or {}
                        if coll and isinstance(flds, dict) and flds:
                            schema_parts.append(
                                f"  - {coll}: " + ", ".join(f"{k} ({v})" for k, v in sorted(flds.items())[:30])
                            )
            update_prompt += "\n".join(schema_parts)

        angular_generator = AngularAppGenerator()
        new_version_str = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        # Try workspace-based update first; fall back to full generation
        current_state = None
        try:
            screen_repo = ScreenRepository()
            current_state = await screen_repo.get_screen_state(project_id, existing_screen_id)
        except Exception:
            pass
        workspace_dir: Optional[str] = (current_state or {}).get("angular_workspace_directory")

        if not workspace_dir:
            angular_result = await angular_generator.generate_app(
                user_prompt=update_prompt,
                project_id=project_id,
                screen_id=existing_screen_id,
                backend_metadata=new_backend_metadata,
                ir_schema=update_ir_schema,
                version=new_version_str,
                is_update=True,
            )
        else:
            current_code = angular_generator._read_workspace_code(workspace_dir)
            analysis = angular_generator.analyze_update_scope(
                update_prompt, update_ir_schema, current_code,
                project_id=project_id, screen_id=existing_screen_id,
                version=new_version_str, org_id=org_id,
                subscriber_id=subscriber_id, user_id=user_id,
            )
            affected_files = analysis.get("affected_files") or [
                {"path": "src/app/app.component.ts", "reason": "Add new widget components", "change_type": "component-add"},
                {"path": "src/app/app.component.html", "reason": "Add new widget templates", "change_type": "component-add"},
            ]
            angular_result = await angular_generator.update_app(
                user_prompt=update_prompt,
                project_id=project_id,
                screen_id=existing_screen_id,
                updated_ir_schema=update_ir_schema,
                affected_files=affected_files,
                backend_metadata=new_backend_metadata,
                org_id=org_id, subscriber_id=subscriber_id, user_id=user_id,
                version=new_version_str,
            )

        dashboard_url = (
            angular_result.get("public_url")
            or existing_dashboard_url
            or f"{existing_screen_id}-v{new_version_str}"
        )

        # Persist new screen version
        new_version = stored_version + 1
        try:
            screen_repo = ScreenRepository()
            current_doc = await screen_repo.get_latest_screen_doc(project_id, existing_screen_id)
            version_count = await screen_repo.count_versions(project_id, existing_screen_id)
            if current_doc:
                v_next = dict(current_doc)
                v_next.pop("_id", None)
                v_next["ir_schema"] = update_ir_schema
                v_next["angular"] = {
                    "workspace_directory": angular_result.get("workspace_directory"),
                    "public_url": dashboard_url,
                    "version": new_version_str,
                    "last_build_at": datetime.utcnow(),
                }
                v_next["screen_name"] = dashboard_name
                v_next["screen_type"] = "dashboard"
                await screen_repo.insert_screen_version_doc(
                    project_id, existing_screen_id, new_version_str,
                    f"v{version_count + 1}", v_next,
                    build_status="success", is_stable=True,
                )
                subscriber_db_name = (
                    f"{Database._db_name}_sb_{subscriber_id}" if subscriber_id else None
                )
                await screen_repo.update_screens_pointer(
                    project_id, existing_screen_id, new_version_str,
                    org_id=org_id, subscriber_id=subscriber_id,
                    subscriber_db_name=subscriber_db_name, user_id=user_id,
                )
                try:
                    cap_doc = {**v_next, "screen_id": existing_screen_id,
                               "project_id": project_id, "version": new_version_str}
                    capability_repo = CapabilityRepository()
                    await sync_screen_single(capability_repo, cap_doc)
                except Exception as cap_exc:
                    logger.warning("update-screen: Capability sync failed (non-fatal): %s", cap_exc)
        except Exception as persist_exc:
            logger.warning("update-screen: Failed to persist screen version: %s", persist_exc)

        # Update Process_Configs
        # Build all_reports_combined: keep existing, replace changed ones with
        # the enriched version, and append new ones.
        _enhanced_by_name_lower = {
            r.get("reportName", "").strip().lower(): r for r in enhanced_reports
        }
        _changed_names_lower = {r.get("reportName", "").strip().lower() for r in changed_reports}
        all_reports_combined = [
            _enhanced_by_name_lower[r.get("reportName", "").strip().lower()]
            if r.get("reportName", "").strip().lower() in _changed_names_lower
            else r
            for r in existing_reports_stored
        ] + [
            r for r in enhanced_reports
            if r.get("reportName", "").strip().lower() not in existing_names
        ]

        set_update: Dict[str, Any] = {
            "agentic_process.dashboardName": dashboard_name,
            "agentic_process.report": all_reports_combined,
            "agentic_process.dashboardDefinition": combined_def.model_dump(),
            "agentic_process.dashboardScreenId": existing_screen_id,
            "agentic_process.dashboardUrl": dashboard_url,
            "agentic_process.dashboard_version": new_version,
            "updated_at": datetime.utcnow(),
        }
        if existing_doc:
            await pc_coll.update_one({"_id": existing_doc["_id"]}, {"$set": set_update})

        screens_out = [
            ReportScreenItem(
                screenId=existing_screen_id,
                screenName=dashboard_name,
                reportName=w.reportName,
                reportType=w.reportType,
                success=True,
            )
            for w in combined_def.widgets
        ]
        return UpdateDashboardScreenResponse(
            success=True,
            projectId=project_id,
            dashboardScreenId=existing_screen_id,
            dashboardUrl=dashboard_url,
            dashboardName=dashboard_name,
            dashboard=combined_def,
            screens=screens_out,
            version=new_version,
            warnings=warnings,
            message=(
                f"Added {len(new_reports)} new widget(s) to the dashboard."
                + (f" Updated description for {len(changed_reports)} existing report(s)." if changed_reports else "")
            ),
        )

    # ── CREATE PATH: no prior screen ─────────────────────────────────────────
    create_result = await generate_dashboard_screens_for_reports(
        workflow_id=request.workflowId or project_id,
        project_id=project_id,
        reports=enhanced_reports,
        dashboard_name=dashboard_name,
        org_id=org_id,
        subscriber_id=subscriber_id,
        user_id=user_id,
    )
    new_version = 1
    # Persist to Process_Configs (upsert)
    now = datetime.utcnow()

    upsert_set: Dict[str, Any] = {
        "agentic_process.workflow_id": project_id,
        "agentic_process.kind": "dashboard",
        "agentic_process.status": "Active" if create_result.success else "draft",
        "agentic_process.dashboardName": dashboard_name,
        "agentic_process.report": enhanced_reports,
        "agentic_process.dashboardDefinition": (
            create_result.dashboard.model_dump() if create_result.dashboard else {}
        ),
        "agentic_process.dashboardScreenId": create_result.dashboardScreenId,
        "agentic_process.dashboardUrl": create_result.dashboardUrl,
        "agentic_process.dashboard_version": new_version,
        "updated_at": now,
    }
    if merged_schema:
        upsert_set["agentic_process.collection_schema"] = merged_schema
    try:
        await pc_coll.update_one(
            {"agentic_process.workflow_id": project_id},
            {"$set": upsert_set, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as pc_err:
        logger.warning("update-screen: Failed to upsert Process_Configs: %s", pc_err)

    screens_out = [
        ReportScreenItem(
            screenId=create_result.dashboardScreenId,
            screenName=dashboard_name,
            reportName=s.reportName,
            reportType=s.reportType,
            success=s.success,
            error=s.error,
        )
        for s in create_result.screens
    ]
    return UpdateDashboardScreenResponse(
        success=create_result.success,
        projectId=project_id,
        dashboardScreenId=create_result.dashboardScreenId,
        dashboardUrl=create_result.dashboardUrl,
        dashboardName=dashboard_name,
        dashboard=create_result.dashboard,
        screens=screens_out,
        version=new_version,
        warnings=warnings,
        message=create_result.message,
    )


@router.post("/reports/download")
async def download_report_data(
    request: ReportDownloadRequest,
    http_request: Request,
):
    """
    Execute a report query against the subscriber DB and return the results
    as a downloadable CSV or JSON file.

    The pipeline is executed with an appended ``$limit`` stage so large
    collections cannot exhaust memory. The caller controls the cap via
    ``request.limit`` (1 – 50 000 rows, default 10 000).
    """
    subscriber_id = request.subscriberId or http_request.headers.get("x-subscriber-id")
    project_id = request.projectId or ""

    subscriber_db_name = resolve_subscriber_db_name(
        project_id=project_id,
        subscriber_id=subscriber_id,
    )
    if not subscriber_db_name:
        raise HTTPException(
            status_code=400,
            detail="Cannot resolve subscriber database. Provide subscriberId or projectId.",
        )

    client = Database.client or Database.native_async_client
    if client is None:
        raise HTTPException(status_code=503, detail="Database not connected.")

    db_ref = client[subscriber_db_name]
    collection = request.collectionName

    try:
        if request.aggregationPipeline:
            pipeline: List[Dict[str, Any]] = list(request.aggregationPipeline)
            # Inject user-supplied filters as a $match at the front
            if request.filters:
                pipeline = [{"$match": request.filters}] + pipeline
            # Cap result size
            pipeline.append({"$limit": request.limit})
            cursor = db_ref[collection].aggregate(pipeline)
        else:
            find_filter: Dict[str, Any] = request.filters or {}
            cursor = db_ref[collection].find(find_filter).limit(request.limit)

        rows: List[Dict[str, Any]] = await cursor.to_list(length=request.limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Query execution failed: {exc}",
        )

    # Sanitize ObjectId / datetime for serialization
    def _serialize(obj: Any) -> Any:
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_serialize(i) for i in obj]
        return obj

    rows = [_serialize(r) for r in rows]

    fmt = (request.format or "csv").lower()
    filename_base = re.sub(r"[^\w-]", "_", collection)

    if fmt == "json":
        content = json.dumps(rows, indent=2, default=str)
        media_type = "application/json"
        filename = f"{filename_base}.json"
        return StreamingResponse(
            iter([content]),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV output
    if not rows:
        csv_content = ""
    else:
        output = io.StringIO()
        # Flatten nested dicts one level deep for CSV readability
        def _flatten(row: Dict[str, Any]) -> Dict[str, Any]:
            flat: Dict[str, Any] = {}
            for k, v in row.items():
                if isinstance(v, (dict,)):
                    for sub_k, sub_v in v.items():
                        flat[f"{k}.{sub_k}"] = sub_v
                elif isinstance(v, list):
                    flat[k] = json.dumps(v, default=str)
                else:
                    flat[k] = v
            return flat

        flat_rows = [_flatten(r) for r in rows]
        headers = list({k: None for row in flat_rows for k in row}.keys())
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat_rows)
        csv_content = output.getvalue()

    media_type = "text/csv"
    filename = f"{filename_base}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )





# ---------------------------------------------------------------------------
# Field alias management — lets callers update the old→new field name map
# stored in Process_Configs.  The embedded _field_aliases dict in each query
# function is only populated at registration time, so after updating aliases
# the caller should trigger a dashboard update to re-register the functions.
# ---------------------------------------------------------------------------

class UpdateFieldAliasesRequest(BaseModel):
    projectId: str
    """Collection-level alias map: {collection_name: {old_field: new_field}}"""
    aliases: Dict[str, Dict[str, str]] = Field(
        default_factory=dict,
        description="Mapping of collection_name → {old_field_name: current_field_name}",
    )
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None


class UpdateFieldAliasesResponse(BaseModel):
    success: bool = True
    projectId: str
    message: str = ""
    aliases: Dict[str, Dict[str, str]] = Field(default_factory=dict)


@router.post("/update-field-aliases", response_model=UpdateFieldAliasesResponse)
async def update_field_aliases(
    request: UpdateFieldAliasesRequest,
    http_request: Request,
) -> UpdateFieldAliasesResponse:
    """
    Persist a field alias map for a project into Process_Configs.

    The map is keyed by collection name; each value is a dict of
    ``{old_field_name: current_field_name}``.  On the next dashboard
    registration (create-screen or update-screen) the alias map is embedded
    into each widget query function so that filters continue to work even
    when a field was renamed in the project after the dashboard was generated.

    Example request body::

        {
          "projectId": "wf_abc123",
          "aliases": {
            "orders": {"customer_name": "client_name", "amt": "amount"}
          }
        }
    """
    project_id = request.projectId
    pc_coll = Database.get_collection("Process_Configs")
    existing_doc = await pc_coll.find_one(
        {"engineType": "agentic_graph", "agentic_process.workflow_id": project_id}
    )
    if not existing_doc:
        raise HTTPException(
            status_code=404,
            detail=f"No Process_Configs document found for projectId '{project_id}'.",
        )

    # Merge with any existing aliases
    stored_aliases: Dict[str, Any] = (
        (existing_doc.get("agentic_process") or {}).get("field_aliases") or {}
    )
    for coll_name, mapping in request.aliases.items():
        if coll_name in stored_aliases and isinstance(stored_aliases[coll_name], dict):
            stored_aliases[coll_name].update(mapping)
        else:
            stored_aliases[coll_name] = mapping

    await pc_coll.update_one(
        {"_id": existing_doc["_id"]},
        {"$set": {"agentic_process.field_aliases": stored_aliases}},
    )
    logger.info(
        "[update-field-aliases] project=%s aliases updated for collections: %s",
        project_id,
        list(request.aliases.keys()),
    )
    return UpdateFieldAliasesResponse(
        projectId=project_id,
        aliases=stored_aliases,
        message=f"Field aliases updated for {len(request.aliases)} collection(s). "
                "Re-generate the dashboard (POST /update-screen) to apply the new aliases.",
    )


# ---------------------------------------------------------------------------
# Dashboard versioning endpoints
# ---------------------------------------------------------------------------

class ScreenVersionItem(BaseModel):
    version: str
    version_label: Optional[str] = None
    created_at: Optional[str] = None
    deployed_url: Optional[str] = None
    preview_url: Optional[str] = None


class ListScreenVersionsResponse(BaseModel):
    success: bool = True
    projectId: str
    screenId: str
    versions: List[ScreenVersionItem] = Field(default_factory=list)


class RestoreScreenVersionRequest(BaseModel):
    projectId: str
    screenId: str
    version: str
    orgId: Optional[str] = None
    subscriberId: Optional[str] = None
    userId: Optional[str] = None


class RestoreScreenVersionResponse(BaseModel):
    success: bool = True
    projectId: str
    screenId: str
    currentVersion: str
    deployedUrl: Optional[str] = None
    message: str = ""


@router.get("/screen-versions", response_model=ListScreenVersionsResponse)
async def list_screen_versions(
    projectId: str,
    screenId: str,
    http_request: Request,
) -> ListScreenVersionsResponse:
    """
    Return all saved version snapshots for a dashboard screen, ordered newest
    first. The caller can use the returned ``version`` values with
    ``POST /restore-screen-version`` to switch back to any prior state.
    """
    screen_repo = ScreenRepository()
    raw_versions = await screen_repo.get_screen_versions(projectId, screenId)
    versions = []
    for v in raw_versions:
        created_at_raw = v.get("created_at")
        created_at_str: Optional[str] = None
        if created_at_raw is not None:
            try:
                created_at_str = (
                    created_at_raw.isoformat()
                    if hasattr(created_at_raw, "isoformat")
                    else str(created_at_raw)
                )
            except Exception:
                created_at_str = str(created_at_raw)
        versions.append(
            ScreenVersionItem(
                version=str(v.get("version", "")),
                version_label=v.get("version_label"),
                created_at=created_at_str,
                deployed_url=v.get("deployed_url") or v.get("preview_url"),
                preview_url=v.get("preview_url"),
            )
        )
    return ListScreenVersionsResponse(
        projectId=projectId,
        screenId=screenId,
        versions=versions,
    )


@router.post("/restore-screen-version", response_model=RestoreScreenVersionResponse)
async def restore_screen_version(
    request: RestoreScreenVersionRequest,
    http_request: Request,
) -> RestoreScreenVersionResponse:
    """
    Switch a dashboard screen's active (``current_version``) pointer to a
    previously saved version. This does NOT regenerate any Angular code — it
    simply changes which build the runtime serves for the given ``screenId``.

    After restoring, the caller should reload the dashboard URL (or fetch it
    again from Process_Configs) to see the older version.
    """
    screen_repo = ScreenRepository()

    # Verify the version exists before touching the pointer
    version_doc = await screen_repo.get_screen_version_data(
        request.projectId, request.screenId, request.version
    )
    if not version_doc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Version '{request.version}' not found for screenId '{request.screenId}' "
                f"in project '{request.projectId}'."
            ),
        )

    await screen_repo.update_screens_pointer(
        request.projectId,
        request.screenId,
        request.version,
        org_id=request.orgId,
        subscriber_id=request.subscriberId,
        user_id=request.userId,
    )

    deployed_url = (
        version_doc.get("angular", {}).get("public_url")
        or version_doc.get("deployed_url")
        or version_doc.get("preview_url")
    )

    # Sync the restored URL (and dashboard definition, if available) back to
    # Process_Configs so the persisted state matches the version now being served.
    # Without this, Process_Configs describes a different version than what the
    # runtime displays after a restore.
    try:
        pc_coll_r = Database.get_collection("Process_Configs")
        pc_query_r: Dict[str, Any] = {"agentic_process.workflow_id": request.projectId}
        if request.subscriberId:
            try:
                pc_query_r["subscriberId"] = {
                    "$in": [ObjectId(request.subscriberId), request.subscriberId]
                }
            except Exception:
                pc_query_r["subscriberId"] = request.subscriberId
        if request.orgId:
            try:
                pc_query_r["orgId"] = {"$in": [ObjectId(request.orgId), request.orgId]}
            except Exception:
                pc_query_r["orgId"] = request.orgId
        restore_pc_doc = await pc_coll_r.find_one(pc_query_r)
        if restore_pc_doc:
            restore_set: Dict[str, Any] = {"updated_at": datetime.utcnow()}
            if deployed_url:
                restore_set["agentic_process.dashboardUrl"] = deployed_url
            # Sync the DashboardDefinition stored inside the version's ir_schema
            # so the definition in Process_Configs matches the restored build.
            ir = version_doc.get("ir_schema") or {}
            if ir.get("dashboard"):
                restore_set["agentic_process.dashboardDefinition"] = ir["dashboard"]
            await pc_coll_r.update_one(
                {"_id": restore_pc_doc["_id"]}, {"$set": restore_set}
            )
    except Exception as pc_sync_err:
        logger.warning(
            "[restore-screen-version] Process_Configs sync failed (non-fatal): %s", pc_sync_err
        )

    logger.info(
        "[restore-screen-version] project=%s screen=%s → version=%s url=%s",
        request.projectId, request.screenId, request.version, deployed_url,
    )

    return RestoreScreenVersionResponse(
        projectId=request.projectId,
        screenId=request.screenId,
        currentVersion=request.version,
        deployedUrl=deployed_url,
        message=f"Dashboard screen restored to version '{request.version}'.",
    )


@router.post("/create-screen", response_model=CreateDashboardScreenResponse)
async def create_dashboard_screen(
    request: CreateDashboardScreenRequest,
    http_request: Request,
):
    workflow_id = request.workflowId or ""
    project_id = request.projectId or workflow_id
    return await generate_dashboard_screens_for_reports(
        workflow_id=workflow_id,
        project_id=project_id,
        reports=request.reports,
        org_id=request.orgId,
        subscriber_id=request.subscriberId,
        user_id=request.userId,
        collection_schema=request.collectionSchema,
    )