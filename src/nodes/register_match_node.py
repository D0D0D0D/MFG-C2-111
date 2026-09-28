"""Exact approved-register expansion for ECN part keys."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from shared.services.database.base_db import BaseDatabase


_SAFE_ID = re.compile(r"[^0-9A-Za-z_-]+")
_REGISTER_QUERY = (
    "SELECT part_key, product_id, lot_id, register_id "
    "FROM approved_part_register "
    "WHERE tenant_id = :tenant_id AND part_key = :part_key"
)


class RegisterMatchNode(FunctionNode):
    """Perform only exact, tenant-scoped register lookups."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, db: BaseDatabase | None = None) -> None:
        super().__init__()
        self._db = db

    def execute(self, state: dict) -> dict:
        seeded, error = _decode_envelope(state.get("user_input"))
        if error:
            emit_trace_event("register_match", {"outcome": "invalid_envelope"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [error]}
        tenant_id = seeded["tenant_id"]
        parts = seeded["normalized_parts"]
        if self._db is None:
            emit_trace_event("register_match", {"outcome": "review_no_database"}, state)
            return _review_result(seeded, "approved_register_unavailable")

        matches: list[dict[str, Any]] = []
        unresolved: list[dict[str, str]] = []
        source_records = [_ecn_record(seeded["ecn"])]
        try:
            for part in parts:
                part_key = part["part_key"]
                rows = self._db.fetch_many(
                    _REGISTER_QUERY,
                    {"tenant_id": tenant_id, "part_key": part_key},
                )
                if len(rows) != 1:
                    unresolved.append({"part_key": part_key, "reason": "unmapped" if not rows else "multiple_candidates"})
                    continue
                row = rows[0]
                if not isinstance(row, dict) or not row.get("product_id") or not row.get("lot_id"):
                    unresolved.append({"part_key": part_key, "reason": "invalid_register_row"})
                    continue
                source_id = _safe_id(row.get("register_id") or f"db_{part_key}")
                matches.append(
                    {
                        "part_key": part_key,
                        "product_id": str(row["product_id"]),
                        "lot_id": str(row["lot_id"]),
                        "source_id": source_id,
                    }
                )
                source_records.append(
                    {
                        "id": source_id,
                        "source_id": source_id,
                        "source_type": "db",
                        "metadata": {"tenant_id": tenant_id, "part_key": part_key},
                    }
                )
        except Exception as exc:  # external service seam; no partial business result
            emit_trace_event("register_match", {"outcome": "database_failure"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"RegisterMatchNode: register lookup failed: {exc}"]}

        if unresolved:
            emit_trace_event("register_match", {"outcome": "review_unresolved", "count": len(unresolved)}, state)
            return _review_result(seeded, "register_match_requires_review", unresolved, source_records)

        impact_scope = {
            "matched_parts": matches,
            "search_scope": {"tenant_id": tenant_id},
        }
        emit_trace_event("register_match", {"outcome": "matched", "count": len(matches)}, state)
        return {
            **seeded,
            "impact_scope": impact_scope,
            "register_records": source_records,
            "match_ready": True,
            "requires_human_review": False,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        # The envelope itself is validated again by execute; this gate must use
        # only pre-existing state because it runs before execute.
        if state.get("user_input") is None:
            return state
        envelope, error = _decode_envelope(state.get("user_input"))
        if error:
            return {**state, "status": AgentStatus.ERROR.value, "error_log": [error]}
        if not envelope["tenant_id"]:
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["RegisterMatchNode: missing scope (fail closed)"]}
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        scope = result.get("impact_scope", {}).get("search_scope", {}) if isinstance(result.get("impact_scope"), dict) else {}
        if scope and not scope.get("tenant_id"):
            raise ValueError("RegisterMatchNode: output omitted register scope")
        return result


def _decode_envelope(value: Any) -> tuple[dict[str, Any], str | None]:
    try:
        envelope = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        envelope = None
    if not isinstance(envelope, dict):
        return {}, "RegisterMatchNode: inner input must be a JSON envelope"
    tenant_id = envelope.get("tenant_id")
    parts, ecn = envelope.get("normalized_parts"), envelope.get("ecn")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return {}, "RegisterMatchNode: tenant_id is required (fail closed)"
    if not isinstance(parts, list) or not parts or not isinstance(ecn, dict):
        return {}, "RegisterMatchNode: normalised parts and ECN are required"
    clean_parts = [part for part in parts if isinstance(part, dict) and isinstance(part.get("part_key"), str) and part["part_key"]]
    if len(clean_parts) != len(parts):
        return {}, "RegisterMatchNode: invalid normalised part key"
    return {"tenant_id": tenant_id, "normalized_parts": clean_parts, "ecn": ecn, "mode": "invoke"}, None


def _review_result(
    seeded: dict,
    reason: str,
    unresolved: list[dict[str, str]] | None = None,
    records: list[dict] | None = None,
) -> dict:
    return {
        **seeded,
        "impact_scope": {"matched_parts": [], "unresolved_parts": unresolved or [], "search_scope": {"tenant_id": seeded["tenant_id"]}},
        "unknowns": [{"kind": "register_match", "reason": reason}],
        "evidence": [],
        "risk_hypotheses": [],
        "recommended_checks": [{"check": "Confirm approved register mapping with SQE", "source_ids": [_ecn_record(seeded["ecn"])["source_id"]]}],
        "register_records": records or [_ecn_record(seeded["ecn"])],
        "match_ready": False,
        "requires_human_review": True,
        "status": AgentStatus.SUCCESS.value,
    }


def _ecn_record(ecn: dict) -> dict:
    source_id = _safe_id(f"ecn_{ecn.get('ecn_id', 'unknown')}")
    return {"id": source_id, "source_id": source_id, "source_type": "ecn", "metadata": {}}


def _safe_id(value: Any) -> str:
    cleaned = _SAFE_ID.sub("_", str(value)).strip("_")
    return cleaned[:96] or "source_unknown"
