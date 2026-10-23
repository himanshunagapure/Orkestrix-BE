"""
Report Planning Service

Implements a planning and validation flow for **Suggested Reports** within a
workflow payload.  The validation is integrated into the existing Stage 0
/start, /respond, and /guidance endpoints — no separate endpoints are needed.

**Flow** (when ``suggestedReports`` is a non-empty array):

    Step X   ──► POST /stage0/start
                 Initializes the report planning session, validates the workflow,
                 and loads available schema/data sources.

    Step X+1 ──► POST /stage0/respond   (repeated until status="complete")
                 Collects any missing information, validates report configuration,
                 resolves ambiguities, updates the session, and returns the next
                 question if additional input is required.

    Step X+2 ──► POST /stage0/guidance   (optional)
                 Returns guidance whenever report planning is blocked (missing
                 fields, invalid aggregation, unsupported report type, unavailable
                 collection, etc.).

**Validation performed at each stage:**
    1. ``reportName`` — non-empty, at least 3 characters
    2. ``reportType`` — one of Table, Summary, Chart, Dashboard
    3. ``collectionName`` — must be specified and exist in available schema
    4. ``requiredFields`` — non-empty list, check against available schema
    5. ``optionalFilters`` — validate against schema if provided
    6. ``aggregation`` — ensure it has SQL-like function keyword if present

**Multiple reports are handled sequentially** — one at a time, each must
pass validation before the next is processed.

**Error handling**: reports with critical issues are marked ``failed`` but
do not block the planning session; the session continues with the next report.

If ``suggestedReports`` is empty or absent, no report planning stages are
generated and no report planning session state is created.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional
from enum import Enum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Valid report types recognised by the system
VALID_REPORT_TYPES: frozenset[str] = frozenset({
    "Tabular",
    "Bar Chart",
    "Column Chart",
    "Line Chart",
    "Pie Chart",
    "Donut Chart",
    "Area Chart",
    "KPI Cards"
})

REPORT_FIELD_FLAG_PREFIX = "REPORT_"

# Report type → field role descriptions for generating context-aware questions
# Each entry maps a report type to a dict of field roles and their descriptions.
REPORT_TYPE_FIELD_ROLES: dict[str, dict[str, str]] = {
    "Line Chart": {
        "x_axis": "X-axis (time or category field)",
        "y_axis": "Y-axis (value / metric field)",
        "series": "Series / group-by field (optional)",
    },
    "Bar Chart": {
        "x_axis": "X-axis (category field)",
        "y_axis": "Y-axis (value / metric field)",
        "series": "Series / group-by field (optional)",
    },
    "Column Chart": {
        "x_axis": "X-axis (category field)",
        "y_axis": "Y-axis (value / metric field)",
        "series": "Series / group-by field (optional)",
    },
    "Area Chart": {
        "x_axis": "X-axis (time or category field)",
        "y_axis": "Y-axis (value / metric field)",
        "series": "Series / group-by field (optional)",
    },
    "Pie Chart": {
        "label": "Label / category field (slices)",
        "value": "Value / metric field (slice sizes)",
    },
    "Donut Chart": {
        "label": "Label / category field (segments)",
        "value": "Value / metric field (segment sizes)",
    },
    "KPI Cards": {
        "value": "Primary metric / value field",
        "comparison": "Comparison / target field (optional)",
    },
    "Tabular": {
        "columns": "Columns / fields to display in the table",
    }
}


# ===========================================================================
# Pydantic Models
# ===========================================================================


class ReportValidationResult(BaseModel):
    """Outcome of validating a single report field."""
    field: str = ""
    status: Literal["valid", "invalid", "missing", "ambiguous"] = "valid"
    message: str = ""
    suggestion: Optional[str] = None


class ReportPlanningState(str, Enum):
    """Lifecycle of a single report within the planning session."""
    PENDING = "pending"
    VALIDATING = "validating"
    QUESTION_ASKED = "question_asked"
    PLANNED = "planned"
    FAILED = "failed"


class ReportPlanningEntry(BaseModel):
    """
    Tracks the planning progress of one SuggestedReport.
    """
    reportIndex: int = 0
    reportName: str = ""
    reportType: str = ""
    collectionName: str = ""
    requiredFields: list[str] = Field(default_factory=list)
    optionalFilters: list[str] = Field(default_factory=list)
    aggregation: Optional[str] = None
    state: ReportPlanningState = ReportPlanningState.PENDING
    validationResults: list[ReportValidationResult] = Field(default_factory=list)
    questionAsked: Optional[str] = None
    questionAnswer: Optional[str] = None
    resolvedFields: dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None

    def to_out(self) -> dict[str, Any]:
        """Convert to a JSON-safe output dict."""
        return {
            "reportIndex": self.reportIndex,
            "reportName": self.reportName,
            "reportType": self.reportType,
            "collectionName": self.collectionName,
            "requiredFields": list(self.requiredFields),
            "optionalFilters": list(self.optionalFilters),
            "aggregation": self.aggregation,
            "state": self.state.value,
            "validationResults": [v.model_dump() for v in self.validationResults],
            "questionAsked": self.questionAsked,
            "questionAnswer": self.questionAnswer,
            "resolvedFields": dict(self.resolvedFields),
            "error": self.error,
        }


class ReportPlanningSession(BaseModel):
    """
    Session state for sequential suggested-report planning within Stage 0.
    """
    entries: list[ReportPlanningEntry] = Field(default_factory=list)
    currentIndex: int = 0
    complete: bool = False
    pendingQuestion: Optional[dict[str, Any]] = None
    availableSchema: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    turnCount: int = 0

    def to_out(self) -> dict[str, Any]:
        planned = sum(1 for e in self.entries if e.state == ReportPlanningState.PLANNED)
        failed = sum(1 for e in self.entries if e.state == ReportPlanningState.FAILED)
        return {
            "status": "complete" if self.complete else "needs_input",
            "currentReportIndex": self.currentIndex,
            "turnCount": self.turnCount,
            "reports": [e.to_out() for e in self.entries],
            "summary": {
                "total": len(self.entries),
                "planned": planned,
                "failed": failed,
                "warnings": list(self.warnings),
            },
        }


# ===========================================================================
# Execution plan (documentation-style text)
# ===========================================================================


def generate_suggested_reports_execution_plan(*, base_step: int = 5) -> str:
    """
    Return a workflow-style execution plan for Suggested Reports planning.

    Only meaningful when the workflow payload carries a non-empty
    ``suggestedReports`` array.  Uses the same formatting style as
    ``AGENTIC_WORKFLOW_DOCUMENTATION.md``.
    """
    step = base_step
    lines = [
        "Suggested Reports Planning (integrated into Stage 0 endpoints)",
        "",
        f"Step {step}  ──► POST /stage0/start",
        "            Initializes the report planning session, validates the workflow,",
        "            and loads available schema/data sources.",
        "",
        f"Step {step + 1} ──► POST /stage0/respond   (repeated until status=\"complete\")",
        "            Collects any missing information, validates report configuration,",
        "            resolves ambiguities, updates the session, and returns the next",
        "            question if additional input is required.",
        "",
        f"Step {step + 2} ──► POST /stage0/guidance   (optional)",
        "            Returns guidance whenever report planning is blocked (missing fields,",
        "            invalid aggregation, unsupported report type, unavailable collection, etc.).",
        "",
        "Responsibilities:",
        "  • /stage0/start — create Mongo session; run credential check (0-A); when",
        "    suggestedReports is non-empty, initialise one ReportPlanningEntry per report,",
        "    build availableSchema from clarified db_schemas when present, and begin",
        "    sequential validation starting at report index 0.",
        "  • /stage0/respond — while report planning is incomplete, apply the user's MCQ",
        "    answer to the active report, re-validate, mark the report planned when all",
        "    checks pass, advance to the next report, then continue Stage 0-B QnA.",
        "  • /stage0/guidance — on-demand help for report fields via flagKey",
        "    REPORT_<field> (e.g. REPORT_reportName, REPORT_aggregation).",
        "",
        "Validation at each stage:",
        "  • reportName, reportType, collectionName, requiredFields, optionalFilters, aggregation (if present)",
        "  • collectionName must exist in availableSchema when schema is loaded",
        "  • requiredFields must exist in availableSchema when schema is loaded",
        "",
        "Session state updates:",
        "  • reportPlanning entries, currentIndex, pendingQuestion, warnings persisted",
        "    in stage0_sessions after every respond turn.",
        "",
        "Completion conditions:",
        "  • Every report is marked planned or failed; reportPlanning.complete = true.",
        "  • Stage 0 overall status=complete requires Stage 0-B QnA complete as well.",
        "",
        "Error handling and recovery:",
        "  • Critical validation failures mark a report failed and advance to the next;",
        "    the session continues.  Expired sessions (30 min TTL) require /stage0/start.",
        "",
        "Multiple reports:",
        "  • Processed sequentially (index 0 → N-1).  Each report must pass validation",
        "    (or be marked failed) before the next report is evaluated.",
    ]
    return "\n".join(lines)


def build_available_schema_from_db_schemas(
    db_schemas: list[dict[str, Any]] | list[Any],
) -> dict[str, Any]:
    """Convert Stage 0 ``db_schemas`` into the flat schema map used for validation."""
    schema: dict[str, Any] = {}
    for item in db_schemas or []:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        coll_name = item.get("collection_name") or item.get("name") or "unknown"
        fields = item.get("fields", [])
        field_names: list[str] = []
        for f in fields:
            if isinstance(f, dict):
                name = f.get("name")
                if name:
                    field_names.append(str(name))
            elif isinstance(f, str):
                field_names.append(f)
        schema[str(coll_name)] = {"fields": field_names}
    return schema


def is_report_field_flag_key(flag_key: str) -> bool:
    return flag_key.strip().upper().startswith(REPORT_FIELD_FLAG_PREFIX)


def parse_report_field_from_flag_key(flag_key: str) -> Optional[str]:
    key = flag_key.strip()
    if not key.upper().startswith(REPORT_FIELD_FLAG_PREFIX):
        return None
    return key[len(REPORT_FIELD_FLAG_PREFIX):]


def report_field_flag_key(field_name: str) -> str:
    return f"{REPORT_FIELD_FLAG_PREFIX}{field_name}"


# ===========================================================================
# Validation Logic
# ===========================================================================


def validate_report_name(entry: ReportPlanningEntry) -> list[ReportValidationResult]:
    """Validate that the report name is non-empty and reasonable."""
    results: list[ReportValidationResult] = []
    name = (entry.reportName or "").strip()
    if not name:
        results.append(ReportValidationResult(
            field="reportName",
            status="missing",
            message="Report name is required.",
            suggestion="Provide a short, descriptive name (e.g. 'Customer Summary').",
        ))
    elif len(name) < 3:
        results.append(ReportValidationResult(
            field="reportName",
            status="invalid",
            message=f"Report name '{name}' is too short (minimum 3 characters).",
            suggestion="Use a more descriptive name.",
        ))
    else:
        results.append(ReportValidationResult(
            field="reportName",
            status="valid",
            message="Report name is valid.",
        ))
    return results


def validate_report_type(entry: ReportPlanningEntry) -> list[ReportValidationResult]:
    """Validate that reportType is one of the supported values."""
    results: list[ReportValidationResult] = []
    rtype = (entry.reportType or "").strip()
    if not rtype:
        results.append(ReportValidationResult(
            field="reportType",
            status="missing",
            message="Report type is required.",
            suggestion=f"Choose one of: {', '.join(sorted(VALID_REPORT_TYPES))}.",
        ))
    elif rtype not in VALID_REPORT_TYPES:
        results.append(ReportValidationResult(
            field="reportType",
            status="invalid",
            message=f"'{rtype}' is not a supported report type.",
            suggestion=f"Use one of: {', '.join(sorted(VALID_REPORT_TYPES))}.",
        ))
    else:
        results.append(ReportValidationResult(
            field="reportType",
            status="valid",
            message=f"Report type '{rtype}' is valid.",
        ))
    return results


def validate_collection_name(
    entry: ReportPlanningEntry,
    available_schema: dict[str, Any],
) -> list[ReportValidationResult]:
    """
    Validate that the collection name is specified and exists in available schema.
    """
    results: list[ReportValidationResult] = []
    coll = (entry.collectionName or "").strip()

    if not coll:
        results.append(ReportValidationResult(
            field="collectionName",
            status="missing",
            message="Collection / data source name is required.",
            suggestion="Specify the database collection or data source for this report.",
        ))
        return results

    if available_schema:
        known_collections = list(available_schema.keys())
        if coll not in known_collections:
            results.append(ReportValidationResult(
                field="collectionName",
                status="invalid",
                message=f"Collection '{coll}' not found in available schema.",
                suggestion=(
                    f"Use one of the available collections: {known_collections}."
                    if known_collections
                    else "Verify the collection name against your data sources."
                ),
            ))
            return results

    results.append(ReportValidationResult(
        field="collectionName",
        status="valid",
        message=f"Collection '{coll}' is valid.",
    ))
    return results


def validate_required_fields(
    entry: ReportPlanningEntry,
    available_schema: dict[str, Any],
) -> list[ReportValidationResult]:
    """
    Validate that each required field exists in the available schema.
    If no schema is provided, skip schema-level checks.
    """
    results: list[ReportValidationResult] = []
    fields = entry.requiredFields or []

    if not fields:
        results.append(ReportValidationResult(
            field="requiredFields",
            status="missing",
            message="At least one required field must be specified.",
            suggestion="List the fields needed to build this report.",
        ))
        return results

    clean_fields = [f.strip() for f in fields if f and f.strip()]
    if not clean_fields:
        results.append(ReportValidationResult(
            field="requiredFields",
            status="invalid",
            message="Required fields list contains only empty entries.",
            suggestion="Provide field names (e.g. 'status', 'amount', 'createdAt').",
        ))
        return results

    if available_schema:
        # Get fields from the report's specified collection, or all collections
        coll = (entry.collectionName or "").strip()
        known_fields = set()
        if coll and coll in available_schema:
            coll_info = available_schema[coll]
            if isinstance(coll_info, dict):
                coll_fields = coll_info.get("fields", coll_info.keys())
                for f in coll_fields:
                    if isinstance(f, str):
                        known_fields.add(f)
        else:
            # Fall back to all collections
            for coll_name, coll_info in available_schema.items():
                if isinstance(coll_info, dict):
                    coll_fields = coll_info.get("fields", coll_info.keys())
                    for f in coll_fields:
                        if isinstance(f, str):
                            known_fields.add(f)

        missing_fields = [f for f in clean_fields if f not in known_fields]
        if missing_fields:
            known_sample = sorted(known_fields)[:10] if known_fields else []
            results.append(ReportValidationResult(
                field="requiredFields",
                status="ambiguous",
                message=f"Fields not found in available schema: {missing_fields}.",
                suggestion=(
                    f"Use only fields that exist. Available examples: {known_sample}."
                    if known_sample
                    else "Verify the field names against your data collections."
                ),
            ))
            return results

    results.append(ReportValidationResult(
        field="requiredFields",
        status="valid",
        message=f"{len(clean_fields)} required field(s) specified.",
    ))
    return results


def validate_optional_filters(
    entry: ReportPlanningEntry,
    available_schema: dict[str, Any],
) -> list[ReportValidationResult]:
    """Validate optional filter fields (non-blocking, advisory)."""
    results: list[ReportValidationResult] = []
    filters = entry.optionalFilters or []

    if not filters:
        results.append(ReportValidationResult(
            field="optionalFilters",
            status="valid",
            message="No optional filters specified (acceptable).",
        ))
        return results

    clean_filters = [f.strip() for f in filters if f and f.strip()]
    if not clean_filters:
        results.append(ReportValidationResult(
            field="optionalFilters",
            status="valid",
            message="Optional filters contained only empty entries; ignored.",
        ))
        return results

    if available_schema:
        known_fields = set()
        for coll_name, coll_info in available_schema.items():
            if isinstance(coll_info, dict):
                coll_fields = coll_info.get("fields", coll_info.keys())
                for f in coll_fields:
                    if isinstance(f, str):
                        known_fields.add(f)

        unknown = [f for f in clean_filters if f not in known_fields]
        if unknown:
            results.append(ReportValidationResult(
                field="optionalFilters",
                status="ambiguous",
                message=f"Filter fields not in schema: {unknown}. They will be treated as metadata filters.",
            ))
            return results

    results.append(ReportValidationResult(
        field="optionalFilters",
        status="valid",
        message=f"{len(clean_filters)} optional filter(s) specified.",
    ))
    return results


def validate_aggregation(entry: ReportPlanningEntry) -> list[ReportValidationResult]:
    """
    Validate the aggregation expression if present.
    Checks for a plausible SQL-like pattern; exact syntax validation
    is deferred to the report generation stage.
    """
    results: list[ReportValidationResult] = []
    agg = entry.aggregation

    if not agg or not agg.strip():
        results.append(ReportValidationResult(
            field="aggregation",
            status="valid",
            message="No aggregation specified (report may be a raw data table).",
        ))
        return results

    agg_clean = agg.strip()
    agg_keywords = {"COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP BY", "ORDER BY"}
    has_keyword = any(kw in agg_clean.upper() for kw in agg_keywords)

    if not has_keyword:
        results.append(ReportValidationResult(
            field="aggregation",
            status="ambiguous",
            message=f"Aggregation expression '{agg_clean}' does not contain a known function.",
            suggestion="Use SQL-like: COUNT(field), SUM(field) GROUP BY other_field, etc.",
        ))
    else:
        results.append(ReportValidationResult(
            field="aggregation",
            status="valid",
            message="Aggregation expression is syntactically plausible.",
        ))

    return results


def report_has_critical_issues(results: list[ReportValidationResult]) -> bool:
    """Determine if a report has blocking validation issues."""
    return any(r.status in ("missing", "invalid") for r in results)


def report_has_ambiguities(results: list[ReportValidationResult]) -> bool:
    """Determine if a report has non-blocking ambiguities."""
    return any(r.status == "ambiguous" for r in results)


def validate_single_report(
    entry: ReportPlanningEntry,
    available_schema: dict[str, Any],
) -> list[ReportValidationResult]:
    """Run all validations on a single report entry."""
    results: list[ReportValidationResult] = []
    results.extend(validate_report_name(entry))
    results.extend(validate_report_type(entry))
    results.extend(validate_collection_name(entry, available_schema))
    results.extend(validate_required_fields(entry, available_schema))
    results.extend(validate_optional_filters(entry, available_schema))
    results.extend(validate_aggregation(entry))
    entry.validationResults = results
    return results


def _get_field_roles_for_report_type(report_type: str) -> dict[str, str]:
    """
    Get the field role descriptions for a given report type.
    Returns a dict mapping role keys (e.g. 'x_axis', 'y_axis') to human-readable descriptions.
    Falls back to a generic description if the report type is not specifically mapped.
    """
    roles = REPORT_TYPE_FIELD_ROLES.get(report_type)
    if roles:
        return roles
    # Generic fallback
    return {
        "fields": "Fields / columns to include in the report",
    }


def generate_mcq_questions(
    entry: ReportPlanningEntry,
    results: list[ReportValidationResult],
) -> list[dict[str, Any]]:
    """
    Produce MCQ-style questions for the user to resolve validation issues.
    Returns at most 2 questions per turn.

    Question priority order:
    1. collectionName (always asked first)
    2. reportName
    3. reportType
    4. requiredFields (with report-type-aware field role descriptions)
    5. optionalFilters
    6. aggregation
    """
    questions: list[dict[str, Any]] = []
    added_types: set[str] = set()

    # Define priority order for question types
    priority_order = ["collectionName", "reportName", "reportType", "requiredFields", "optionalFilters", "aggregation"]

    # Collect all fields that need questions, sorted by priority
    fields_needing_questions: list[str] = []
    for vr in results:
        if vr.status in ("missing", "invalid", "ambiguous"):
            ftype = vr.field
            if ftype not in added_types:
                added_types.add(ftype)
                fields_needing_questions.append(ftype)

    # Sort by priority order
    fields_needing_questions.sort(key=lambda f: priority_order.index(f) if f in priority_order else 99)

    report_label = entry.reportName or f"Report #{entry.reportIndex + 1}"

    for ftype in fields_needing_questions:
        if len(questions) >= 2:
            break

        if ftype == "collectionName":
            questions.append({
                "question_text": (
                    f"'{report_label}': Which collection / data source should this report use?"
                ),
                "options": [
                    "Use the first available collection",
                    "I'll specify the collection name",
                ],
                "field": "collectionName",
                "flag_type": report_field_flag_key("collectionName"),
                "reportIndex": entry.reportIndex,
                "severity": "CRITICAL",
            })

        elif ftype == "reportName":
            questions.append({
                "question_text": (
                    f"Report '{report_label}': What should this report be called?"
                ),
                "options": [
                    entry.reportName if entry.reportName else "Use workflow phase name",
                    "I'll type my own report name",
                ],
                "field": "reportName",
                "flag_type": report_field_flag_key("reportName"),
                "reportIndex": entry.reportIndex,
                "severity": "CRITICAL",
            })

        elif ftype == "reportType":
            questions.append({
                "question_text": (
                    f"'{report_label}': Which report type best describes it?"
                ),
                "options": sorted(VALID_REPORT_TYPES),
                "field": "reportType",
                "flag_type": report_field_flag_key("reportType"),
                "reportIndex": entry.reportIndex,
                "severity": "CRITICAL",
            })

        elif ftype == "requiredFields":
            # Build report-type-aware question text
            report_type = entry.reportType or ""
            field_roles = _get_field_roles_for_report_type(report_type)

            if field_roles and len(field_roles) > 1:
                # Multiple specific roles - describe them
                role_descriptions = "; ".join(
                    f"{key}: {desc}" for key, desc in field_roles.items()
                )
                question_text = (
                    f"'{report_label}': Which fields are required to build this {report_type or 'report'}?\n"
                    f"Field roles: {role_descriptions}"
                )
            elif field_roles:
                # Single role description
                single_desc = next(iter(field_roles.values()))
                question_text = (
                    f"'{report_label}': Which {single_desc.lower()} are required?"
                )
            else:
                question_text = (
                    f"'{report_label}': Which fields are required to build this report?"
                )

            questions.append({
                "question_text": question_text,
                "options": [
                    "Use all available fields",
                    "I'll specify the fields",
                ],
                "field": "requiredFields",
                "flag_type": report_field_flag_key("requiredFields"),
                "reportIndex": entry.reportIndex,
                "severity": "CRITICAL",
            })

        elif ftype == "optionalFilters":
            questions.append({
                "question_text": (
                    f"'{report_label}': Some optional filters are not in the schema. "
                    "Keep them or remove unknown filters?"
                ),
                "options": [
                    "Keep as metadata filters",
                    "I'll specify corrected filter fields",
                ],
                "field": "optionalFilters",
                "flag_type": report_field_flag_key("optionalFilters"),
                "reportIndex": entry.reportIndex,
                "severity": "WARN",
            })

        elif ftype == "aggregation":
            if any(vr.status == "ambiguous" for vr in results if vr.field == "aggregation"):
                questions.append({
                    "question_text": (
                        f"'{report_label}': The aggregation expression "
                        f"'{entry.aggregation}' looks unusual. "
                        "Would you like to clarify or keep it as-is?"
                    ),
                    "options": [
                        "Keep as-is",
                        "I'll provide a corrected expression",
                    ],
                    "field": "aggregation",
                    "flag_type": report_field_flag_key("aggregation"),
                    "reportIndex": entry.reportIndex,
                    "severity": "WARN",
                })

    return questions


def resolve_answer_on_entry(
    entry: ReportPlanningEntry,
    answer: str,
    question_field: str,
) -> None:
    """
    Apply the user's answer to resolve a validation gap on a report entry.
    """
    answer_stripped = answer.strip()

    if question_field == "collectionName":
        if answer_stripped and answer_stripped.lower() not in (
            "use the first available collection", "i'll specify the collection name"
        ):
            entry.collectionName = answer_stripped
            entry.resolvedFields["collectionName"] = answer_stripped
        else:
            if not entry.collectionName:
                entry.collectionName = "default"
            entry.resolvedFields["collectionName"] = entry.collectionName

    elif question_field == "reportName":
        if answer_stripped and answer_stripped.lower() not in (
            "use workflow phase name", "i'll type my own report name"
        ):
            entry.reportName = answer_stripped
            entry.resolvedFields["reportName"] = answer_stripped
        else:
            if not entry.reportName:
                entry.reportName = f"Report #{entry.reportIndex + 1}"
            entry.resolvedFields["reportName"] = entry.reportName

    elif question_field == "reportType":
        if answer_stripped in VALID_REPORT_TYPES:
            entry.reportType = answer_stripped
            entry.resolvedFields["reportType"] = answer_stripped
        else:
            candidate = answer_stripped.capitalize()
            entry.reportType = candidate if candidate in VALID_REPORT_TYPES else "Table"
            entry.resolvedFields["reportType"] = entry.reportType

    elif question_field == "requiredFields":
        if answer_stripped.lower() == "use all available fields":
            entry.resolvedFields["requiredFields"] = ",".join(entry.requiredFields)
        else:
            parsed = [f.strip() for f in answer_stripped.split(",") if f.strip()]
            if parsed:
                entry.requiredFields = parsed
            entry.resolvedFields["requiredFields"] = ",".join(entry.requiredFields)

    elif question_field == "optionalFilters":
        if answer_stripped.lower() == "keep as metadata filters":
            entry.resolvedFields["optionalFilters"] = ",".join(entry.optionalFilters)
        else:
            parsed = [f.strip() for f in answer_stripped.split(",") if f.strip()]
            if parsed:
                entry.optionalFilters = parsed
            entry.resolvedFields["optionalFilters"] = ",".join(entry.optionalFilters)

    elif question_field == "aggregation":
        if answer_stripped.lower() in ("keep as-is",):
            entry.resolvedFields["aggregation"] = entry.aggregation or ""
        elif answer_stripped.lower() != "i'll provide a corrected expression":
            entry.aggregation = answer_stripped
            entry.resolvedFields["aggregation"] = answer_stripped


def _mark_entry_outcome(
    entry: ReportPlanningEntry,
    results: list[ReportValidationResult],
    session: ReportPlanningSession,
) -> Literal["planned", "question", "failed", "continue"]:
    """Classify validation outcome for the current report entry."""
    has_critical = report_has_critical_issues(results)
    has_ambiguous = report_has_ambiguities(results)

    if not has_critical and not has_ambiguous:
        entry.state = ReportPlanningState.PLANNED
        entry.error = None
        return "planned"

    questions = generate_mcq_questions(entry, results)
    if questions:
        entry.state = ReportPlanningState.QUESTION_ASKED
        session.pendingQuestion = questions[0]
        entry.questionAsked = questions[0]["question_text"]
        return "question"

    if has_critical:
        entry.state = ReportPlanningState.FAILED
        entry.error = "Critical validation issues could not be resolved."
        session.warnings.append(
            f"Report #{entry.reportIndex + 1} ('{entry.reportName or entry.reportType}'): {entry.error}"
        )
        return "failed"

    entry.state = ReportPlanningState.PLANNED
    return "planned"


# ---------------------------------------------------------------------------
# Report-type inference from name / description
# ---------------------------------------------------------------------------

# Ordered list of (keyword, canonical_type) pairs.  First match wins.
# Keywords are matched case-insensitively as whole words or substrings.
_REPORT_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("line",          "Line Chart"),
    ("bar",           "Bar Chart"),
    ("column chart",  "Column Chart"),
    ("column",        "Column Chart"),
    ("pie",           "Pie Chart"),
    ("donut",         "Donut Chart"),
    ("doughnut",      "Donut Chart"),
    ("area",          "Area Chart"),
    ("kpi",           "KPI Cards"),
    ("tabular",       "Tabular"),
    ("table",         "Tabular"),
]


def _infer_report_type(text: str) -> Optional[str]:
    """
    Infer a canonical report type from free-form text (report name or description).

    Returns the canonical type string (e.g. "Line Chart") when a keyword is
    found, or ``None`` when no known keyword matches.
    """
    if not text:
        return None
    haystack = text.lower()
    for keyword, canonical in _REPORT_TYPE_KEYWORDS:
        if keyword in haystack:
            return canonical
    return None


def initialize_report_planning_session(
    suggested_reports: list[dict[str, Any]],
    available_schema: Optional[dict[str, Any]] = None,
) -> ReportPlanningSession:
    """Create a new report planning session from the suggestedReports payload."""
    entries: list[ReportPlanningEntry] = []
    for idx, rep in enumerate(suggested_reports):
        raw_type = (rep.get("reportType") or "").strip()

        # Auto-infer reportType from description / reportName when the caller
        # left reportType empty or used a free-form string not in VALID_REPORT_TYPES.
        if not raw_type or raw_type not in VALID_REPORT_TYPES:
            description = (rep.get("description") or "").strip()
            report_name = (rep.get("reportName") or "").strip()
            inferred = (
                _infer_report_type(description)
                or _infer_report_type(report_name)
            )
            if inferred:
                logger.info(
                    "report_planning: auto-inferred reportType=%r for report %r from text %r",
                    inferred,
                    report_name or f"index {idx}",
                    description or report_name,
                )
                raw_type = inferred

        entries.append(ReportPlanningEntry(
            reportIndex=idx,
            reportName=rep.get("reportName", ""),
            reportType=raw_type,
            collectionName=rep.get("collectionName", ""),
            requiredFields=rep.get("requiredFields", []),
            optionalFilters=rep.get("optionalFilters", []),
            aggregation=rep.get("aggregation"),
            state=ReportPlanningState.PENDING,
        ))

    session = ReportPlanningSession(
        entries=entries,
        currentIndex=0,
        availableSchema=available_schema or {},
    )
    advance_report_planning(session)
    return session


def refresh_report_planning_schema(
    session: ReportPlanningSession,
    db_schemas: list[dict[str, Any]] | list[Any],
) -> None:
    """Update available schema and re-validate any non-planned reports."""
    session.availableSchema = build_available_schema_from_db_schemas(db_schemas)
    for entry in session.entries:
        if entry.state in (ReportPlanningState.PLANNED, ReportPlanningState.FAILED):
            continue
        validate_single_report(entry, session.availableSchema)


def advance_report_planning(session: ReportPlanningSession) -> None:
    """
    Validate reports sequentially starting at ``currentIndex``.

    Stops when a question must be asked or all reports are processed.
    """
    session.pendingQuestion = None

    while session.currentIndex < len(session.entries):
        entry = session.entries[session.currentIndex]
        if entry.state == ReportPlanningState.PLANNED:
            session.currentIndex += 1
            continue
        if entry.state == ReportPlanningState.FAILED:
            session.currentIndex += 1
            continue

        entry.state = ReportPlanningState.VALIDATING
        results = validate_single_report(entry, session.availableSchema)
        outcome = _mark_entry_outcome(entry, results, session)

        if outcome == "question":
            return

        if outcome in ("planned", "failed"):
            session.currentIndex += 1
            continue

    session.complete = True
    session.pendingQuestion = None


def apply_report_planning_answer(
    session: ReportPlanningSession,
    answer: str,
) -> None:
    """Apply a user answer to the pending report question and advance planning."""
    if session.complete or not session.pendingQuestion:
        return

    question = session.pendingQuestion
    report_index = int(question.get("reportIndex", session.currentIndex))
    field = str(question.get("field", ""))

    if report_index < 0 or report_index >= len(session.entries):
        logger.warning("Report planning answer ignored — invalid report index %s", report_index)
        return

    entry = session.entries[report_index]
    resolve_answer_on_entry(entry, answer, field)
    entry.questionAnswer = answer.strip()
    session.turnCount += 1
    session.pendingQuestion = None

    entry.state = ReportPlanningState.VALIDATING
    results = validate_single_report(entry, session.availableSchema)
    outcome = _mark_entry_outcome(entry, results, session)

    if outcome == "question":
        return

    if outcome in ("planned", "failed"):
        session.currentIndex = max(session.currentIndex, report_index + 1)

    advance_report_planning(session)


def report_planning_has_pending_input(session: ReportPlanningSession) -> bool:
    return not session.complete and session.pendingQuestion is not None


def report_planning_questions_for_response(
    session: ReportPlanningSession,
) -> list[dict[str, Any]]:
    """Return MCQ dicts suitable for Stage0Response.questions."""
    if session.complete or not session.pendingQuestion:
        return []
    return [session.pendingQuestion]


def validate_suggested_reports(
    suggested_reports: list[dict[str, Any]],
    available_schema: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    One-shot validation summary for the entire ``suggestedReports`` array.

    Used when a non-interactive snapshot is needed (e.g. scratch/preview warnings).
    """
    session = initialize_report_planning_session(suggested_reports, available_schema)
    return session.to_out()


def guidance_for_report_field(field_name: str) -> str:
    """Return guidance text for a specific report field (no session needed)."""
    guidance_templates: dict[str, str] = {
        "collectionName": (
            "The collection (or data source) is the database table or API endpoint "
            "that contains the data for this report.\n\n"
            "**Tips:**\n"
            "- Use the exact collection name as defined in your data schema.\n"
            "- If you're unsure, check the available collections in your workflow's "
            "database configuration.\n"
            "- The collection must contain the fields you plan to use in the report."
        ),
        "reportName": (
            "A report name should be short, descriptive, and business-friendly.\n\n"
            "**Good examples:**\n"
            "- Customer Summary\n"
            "- Order Status Report\n"
            "- Employee Activity Dashboard\n\n"
            "**Bad examples:**\n"
            "- rep1\n"
            "- my_report\n"
            "- data\n\n"
            "Use nouns that describe what the report shows."
        ),
        "reportType": (
            "Choose the report type that best matches the data presentation:\n\n"
            "- **Table** — Row-and-column data display, best for listing records.\n"
            "- **Summary** — Aggregated totals, counts, or key metrics.\n"
            "- **Chart** — Visual representation (bar, line, pie).\n"
            "- **Dashboard** — Combination of multiple metrics/charts on one page."
        ),
        "requiredFields": (
            "Required fields are the data columns that *must* be present to build the report.\n\n"
            "Tips:\n"
            "- Only include fields explicitly collected by the workflow.\n"
            "- Use exact field names as they appear in your data schema.\n"
            "- For cross-collection reports, prefix with collection name (e.g. `orders.status`)."
        ),
        "optionalFilters": (
            "Optional filters let users narrow down the report at view time.\n\n"
            "Common filter fields:\n"
            "- Status\n"
            "- Date range\n"
            "- Category\n"
            "- Assigned User\n"
            "- Department\n\n"
            "Only include filters that make sense for the data."
        ),
        "aggregation": (
            "Aggregation expressions tell the system how to summarise data.\n\n"
            "Use SQL-like syntax:\n"
            "- `COUNT(OrderId)`\n"
            "- `SUM(Amount) GROUP BY Status`\n"
            "- `AVG(Duration) GROUP BY Month`\n\n"
            "If no aggregation is needed, leave this field empty (raw data table)."
        ),
    }

    return guidance_templates.get(field_name, (
        f"**Field: `{field_name}`**\n\n"
        "This field is part of the report specification. "
        "Please provide a valid value so the report can be planned correctly."
    ))