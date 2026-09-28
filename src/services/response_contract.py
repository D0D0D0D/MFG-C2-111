"""Single-source LLM response contract for both ECN drafting stages."""

from __future__ import annotations

import json
import re
from typing import Any

from framework.errors import SecurityViolationError
from src.evidence_gate import FindingField, project_typed_finding, project_typed_findings


MAX_FINDINGS_PER_SECTION = 20
MAX_SOURCE_IDS = 50
MAX_FINDING_TEXT_CHARS = 1_000
MAX_SOURCE_EXCERPT_CHARS = 500
IMPACT_SCOPE_FIELD = "impact_scope"
UNKNOWN_SECTION = "unknowns"
RISK_SECTION = "risk_hypotheses"
CHECK_SECTION = "recommended_checks"
REVIEW_FIELD = "requires_human_review"
CITED_IDS_FIELD = "cited_source_ids"
RISK_CATEGORIES = (
    "safety",
    "performance",
    "regulatory",
    "customer_approval",
    "revalidation",
    "traceability",
    "other",
)
UNKNOWN_KINDS = (
    "attachment",
    "register_match",
    "precedent",
    "customer_requirement",
    "effective_date",
    "data_quality",
    "other",
)
CITABLE_SOURCE_TYPES = frozenset({"ecn", "db", "kb"})
SOURCE_EXCERPT_FIELDS = ("snippet", "summary", "title")
METADATA_EXCERPT_FIELDS = ("title", "source_doc", "kind", "category", "record_type", "part_key")

RESPONSE_FIELDS = (
    FindingField(UNKNOWN_SECTION, list, required=True, item_type=dict),
    FindingField(RISK_SECTION, list, required=True, item_type=dict),
    FindingField(CHECK_SECTION, list, required=True, item_type=dict),
    FindingField(REVIEW_FIELD, bool, required=True),
    FindingField(CITED_IDS_FIELD, list, required=True, item_type=str),
)
UNKNOWN_FIELDS = (
    FindingField("kind", str, required=True, allowed_values=UNKNOWN_KINDS),
    FindingField("reason", str, required=True),
)
RISK_FIELDS = (
    FindingField("category", str, required=True, allowed_values=RISK_CATEGORIES),
    FindingField("claim", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
)
CHECK_FIELDS = (
    FindingField("check", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
)

_SAFE_SOURCE_ID = re.compile(r"^[0-9A-Za-z_-]+$")


def response_contract(*, retrieved_records: Any = None) -> str:
    """Render the prompt contract from the same declarations used by validation."""
    catalog = source_catalog(retrieved_records)
    contract = {
        "top_level": _field_contract(RESPONSE_FIELDS),
        "sections": {
            UNKNOWN_SECTION: _field_contract(UNKNOWN_FIELDS),
            RISK_SECTION: _field_contract(RISK_FIELDS),
            CHECK_SECTION: _field_contract(CHECK_FIELDS),
        },
        "limits": {
            "items_per_section": MAX_FINDINGS_PER_SECTION,
            "source_ids_per_list": MAX_SOURCE_IDS,
            "finding_text_chars": MAX_FINDING_TEXT_CHARS,
        },
        "source_catalog": catalog,
        "allowed_source_ids": [item["source_id"] for item in catalog],
        "rules": [
            "Return exactly the declared keys; do not add keys.",
            "Use JSON booleans and arrays with the declared direct item types.",
            "Do not return impact_scope; the framework preserves its deterministic value.",
            "Use only allowed_source_ids for every source_ids and cited_source_ids value.",
            "Do NOT invent source IDs.",
            "An item citing an unknown source_id is rejected by the S-3 evidence gate.",
        ],
    }
    return json.dumps(contract, ensure_ascii=False, sort_keys=True)


def source_catalog(retrieved_records: Any) -> list[dict[str, str]]:
    """Project exactly the ECN/DB/KB records that S-3 permits claims to cite."""
    by_id: dict[str, dict[str, str]] = {}
    if not isinstance(retrieved_records, list):
        return []
    for record in retrieved_records:
        if not isinstance(record, dict) or not record.get("source_id"):
            continue
        source_id = str(record["source_id"])
        source_type = str(record.get("source_type", ""))
        by_id[source_id] = {
            "source_id": source_id,
            "source_type": source_type,
            "excerpt": _source_excerpt(record),
        }
    return [
        item
        for item in by_id.values()
        if item["source_type"] in CITABLE_SOURCE_TYPES and _SAFE_SOURCE_ID.fullmatch(item["source_id"])
    ]


def allowed_source_ids(retrieved_records: Any) -> list[str]:
    """Return the source ID allowlist rendered in prompts and enforced by S-3."""
    return [item["source_id"] for item in source_catalog(retrieved_records)]


def project_response(value: Any, *, trusted_impact_scope: Any) -> dict[str, Any]:
    """Validate, type-project, and add the deterministic impact scope."""
    candidate = value
    if isinstance(value, dict) and IMPACT_SCOPE_FIELD in value:
        if value[IMPACT_SCOPE_FIELD] != trusted_impact_scope:
            raise SecurityViolationError("S-3 response projection: impact scope is not framework-owned")
        candidate = {key: item for key, item in value.items() if key != IMPACT_SCOPE_FIELD}
    projected = project_typed_finding(candidate, RESPONSE_FIELDS)
    unknowns = project_typed_findings(projected[UNKNOWN_SECTION], UNKNOWN_FIELDS)
    risks = project_typed_findings(projected[RISK_SECTION], RISK_FIELDS)
    checks = project_typed_findings(projected[CHECK_SECTION], CHECK_FIELDS)
    _check_count(unknowns)
    _check_count(risks)
    _check_count(checks)
    for unknown in unknowns:
        _check_text(unknown["reason"])
    for risk in risks:
        _check_text(risk["claim"])
        _check_source_ids(risk["source_ids"], require_nonempty=True)
    for check in checks:
        _check_text(check["check"])
        _check_source_ids(check["source_ids"], require_nonempty=True)
    cited = projected[CITED_IDS_FIELD]
    _check_source_ids(cited, require_nonempty=False)
    if not isinstance(trusted_impact_scope, dict):
        raise SecurityViolationError("S-3 response projection: trusted impact scope is unavailable")
    return {
        IMPACT_SCOPE_FIELD: dict(trusted_impact_scope),
        UNKNOWN_SECTION: unknowns,
        RISK_SECTION: risks,
        CHECK_SECTION: checks,
        REVIEW_FIELD: projected[REVIEW_FIELD],
        CITED_IDS_FIELD: list(cited),
    }


def example_response_from_declarations() -> dict[str, Any]:
    """Build a validator-ready example mechanically from the declarations."""
    section_values = {
        UNKNOWN_SECTION: [_example_finding(UNKNOWN_FIELDS)],
        RISK_SECTION: [_example_finding(RISK_FIELDS)],
        CHECK_SECTION: [_example_finding(CHECK_FIELDS)],
    }
    response: dict[str, Any] = {}
    for field in RESPONSE_FIELDS:
        if field.name in section_values:
            response[field.name] = section_values[field.name]
        elif field.expected_type is bool:
            response[field.name] = True
        elif field.expected_type is list:
            response[field.name] = []
        else:  # pragma: no cover - declaration additions must define an example branch
            raise AssertionError("response declaration lacks an example builder")
    return response


def _field_contract(fields: tuple[FindingField, ...]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for field in fields:
        expected = field.expected_type if isinstance(field.expected_type, tuple) else (field.expected_type,)
        declaration: dict[str, Any] = {
            "name": field.name,
            "type": "|".join(item.__name__ for item in expected),
            "required": field.required,
        }
        if field.item_type is not None:
            item_types = field.item_type if isinstance(field.item_type, tuple) else (field.item_type,)
            declaration["item_type"] = "|".join(item.__name__ for item in item_types)
        if field.allowed_values:
            declaration["enum"] = list(field.allowed_values)
        rendered.append(declaration)
    return rendered


def _example_finding(fields: tuple[FindingField, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        if field.allowed_values:
            result[field.name] = field.allowed_values[0]
        elif field.expected_type is str:
            result[field.name] = "example"
        elif field.expected_type is list:
            result[field.name] = ["source_1"] if field.item_type is str else []
        else:  # pragma: no cover - declaration additions must define an example branch
            raise AssertionError("finding declaration lacks an example builder")
    return result


def _source_excerpt(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for container, fields in (
        (record, SOURCE_EXCERPT_FIELDS),
        (metadata, METADATA_EXCERPT_FIELDS),
    ):
        for field in fields:
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_SOURCE_EXCERPT_CHARS]
    return ""


def _check_count(items: list[dict[str, Any]]) -> None:
    if len(items) > MAX_FINDINGS_PER_SECTION:
        raise SecurityViolationError("S-3 response projection: section item limit exceeded")


def _check_text(value: str) -> None:
    if not value or len(value) > MAX_FINDING_TEXT_CHARS or "\x00" in value:
        raise SecurityViolationError("S-3 response projection: finding text has an invalid shape")


def _check_source_ids(values: list[str], *, require_nonempty: bool) -> None:
    if (require_nonempty and not values) or len(values) > MAX_SOURCE_IDS:
        raise SecurityViolationError("S-3 response projection: source id list has an invalid shape")
    if any(not _SAFE_SOURCE_ID.fullmatch(value) for value in values):
        raise SecurityViolationError("S-3 response projection: source id has an invalid shape")
