"""ECN intake, allowlist validation, and normalisation quality gate."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.service import normalize_ecn_envelope


_ALLOWED_ECN_KEYS = frozenset({"ecn_id", "supplier_id", "tenant_id", "parts", "attachments", "body", "effective_date"})
_MAX_ECN_BYTES = 128_000


class ECNPreProcessNode(FunctionNode):
    """Establish verified scope and make unprocessable ECNs review-only."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        parsed, error = _parse_ecn(state.get("user_input"))
        if error:
            emit_trace_event("ecn_intake", {"outcome": "invalid_input"}, state)
            return _error(error)

        verified_tenant = _verified_scope(state)
        if not verified_tenant:
            emit_trace_event("ecn_intake", {"outcome": "scope_unavailable"}, state)
            return _error("ECNPreProcessNode: tenant_id must be verified before processing")
        if parsed.get("tenant_id") not in (None, verified_tenant):
            emit_trace_event("ecn_intake", {"outcome": "scope_mismatch"}, state)
            return _error("ECNPreProcessNode: ECN scope does not match verified request scope")

        ecn, normalized_parts, quality_flags = normalize_ecn_envelope(parsed)
        attachments = ecn.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            quality_flags.append("attachments_missing")
        if not isinstance(ecn.get("ecn_id"), str) or not ecn["ecn_id"].strip():
            quality_flags.append("ecn_id_missing")
        if not isinstance(ecn.get("supplier_id"), str) or not ecn["supplier_id"].strip():
            quality_flags.append("supplier_id_missing")

        validated = {**ecn, "tenant_id": verified_tenant}
        eligible = not quality_flags
        emit_trace_event(
            "ecn_intake",
            {"outcome": "eligible" if eligible else "review_required", "part_count": len(normalized_parts)},
            state,
        )
        return {
            "validated_input": json.dumps(validated, ensure_ascii=False, default=str),
            "ecn": validated,
            "tenant_id": verified_tenant,
            "normalized_parts": normalized_parts,
            "data_quality_flags": quality_flags,
            "processing_eligible": eligible,
            "requires_human_review": not eligible,
            "mode": "invoke",
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2: reject unauthenticated scope and non-allowlisted ECN fields."""
        # Framework contract probes call nodes with only audit/trust fields.
        # Let execute produce the structured invalid-input result in that case.
        if state.get("user_input") is None:
            return state
        parsed, error = _parse_ecn(state.get("user_input"))
        tenant_id = _verified_scope(state)
        if error:
            return {**state, **_error(error)}
        if not tenant_id:
            return {**state, **_error("ECNPreProcessNode: verified tenant_id is required")}
        unknown = sorted(set(parsed) - _ALLOWED_ECN_KEYS)
        if unknown:
            return {**state, **_error("ECNPreProcessNode: ECN contains non-allowlisted field names")}
        if parsed.get("tenant_id") not in (None, tenant_id):
            return {**state, **_error("ECNPreProcessNode: requested scope differs from verified scope")}
        return state


def _parse_ecn(raw: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return {}, "ECNPreProcessNode: user_input must be a non-empty ECN JSON object"
    if len(raw.encode("utf-8")) > _MAX_ECN_BYTES:
        return {}, "ECNPreProcessNode: ECN exceeds the input size limit"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "ECNPreProcessNode: user_input must be valid ECN JSON"
    if not isinstance(parsed, dict):
        return {}, "ECNPreProcessNode: user_input must be an ECN object"
    return parsed, None


def _verified_scope(state: dict) -> str:
    context = state.get("input_context")
    context = context if isinstance(context, dict) else {}
    tenant_id = context.get("tenant_id")
    return tenant_id.strip() if isinstance(tenant_id, str) else ""


def _error(message: str) -> dict:
    return {
        "status": AgentStatus.ERROR.value,
        "processing_eligible": False,
        "requires_human_review": True,
        "error_log": [message],
    }


# Compatibility aliases are intentionally domain-specific, not generic stubs.
PreProcessNode = ECNPreProcessNode
