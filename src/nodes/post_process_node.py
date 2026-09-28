"""Final advisory envelope and S-3 evidence-preservation gate."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.evidence_gate.gate_node import ProvenanceContract, validate_provenance
from src.output_envelope.envelope_node import OutputEnvelopeNode
from src.services.brief_output_validation import validate_brief_claim_coverage
from src.services.prohibited_content import contains_prohibited_content
from src.services.response_contract import source_catalog


_CONTRACT = ProvenanceContract(
    gate_kind="impact_brief",
    evidence_map_key="evidence",
    cited_ids_key="cited_source_ids",
    retrieved_records_key="retrieved_records",
    claim_source_keys=("source_ids",),
    record_id_keys=("source_id", "id"),
    passthrough_keys=("impact_scope", "unknowns", "risk_hypotheses", "recommended_checks", "requires_human_review"),
)
class BriefPostProcessNode(OutputEnvelopeNode):
    """Emit only an advisory impact brief whose claims retain provenance."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke",)
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "impact_brief"}
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode",
            "gate_kind",
            "impact_scope",
            "evidence",
            "unknowns",
            "risk_hypotheses",
            "recommended_checks",
            "requires_human_review",
        )
    }
    max_field_chars: ClassVar[int | None] = None

    def execute(self, state: dict) -> dict:
        # Quality-gated and unmatched paths deliberately reach post_process and
        # receive the same neutral advisory shape as completed assessments.
        prepared = {
            **state,
            "mode": state.get("mode", "invoke"),
            "impact_scope": state.get("impact_scope", {}),
            "evidence": state.get("evidence", []),
            "unknowns": state.get("unknowns", []),
            "risk_hypotheses": state.get("risk_hypotheses", []),
            "recommended_checks": state.get("recommended_checks", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
        }
        result = super().execute(prepared)
        emit_trace_event("brief_finalized", {"review_required": prepared["requires_human_review"]}, state)
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        return {
            "impact_scope": state.get("impact_scope", {}),
            "evidence": state.get("evidence", []),
            "unknowns": state.get("unknowns", []),
            "risk_hypotheses": state.get("risk_hypotheses", []),
            "recommended_checks": state.get("recommended_checks", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "advisory": "Human SQE review remains required for decisions.",
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        hook_state = result.get("_output_envelope_hook_state")
        if isinstance(hook_state, dict) and result.get("status") != AgentStatus.ERROR.value:
            provenance = {
                "gate_kind": "impact_brief",
                "status": result.get("status", AgentStatus.SUCCESS.value),
                "evidence": hook_state.get("evidence", []),
                "cited_source_ids": hook_state.get("cited_source_ids", []),
                "retrieved_records": hook_state.get("retrieved_records", []),
            }
            validate_provenance(provenance, _CONTRACT)
            _validate_claim_sources(hook_state)
        result = super()._extra_security_gate_output(result)
        formatted = result["formatted_output"]
        validate_brief_claim_coverage(formatted)
        return result

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        sanitized = super()._sanitize_client_echoes(formatted)
        if contains_prohibited_content(sanitized):
            raise SecurityViolationError("S-3: approval, update instruction, regulatory decision, or external final answer is prohibited")
        return sanitized


def _validate_claim_sources(state: dict) -> None:
    source_types = {
        record["source_id"]: record["source_type"]
        for record in source_catalog(state.get("retrieved_records", []))
    }
    for collection in ("risk_hypotheses", "recommended_checks"):
        claims = state.get(collection, [])
        if not isinstance(claims, list):
            raise SecurityViolationError(f"S-3 evidence gate: {collection} must be a list")
        for claim in claims:
            if not isinstance(claim, dict):
                raise SecurityViolationError(f"S-3 evidence gate: {collection} contains non-object claim")
            ids = claim.get("source_ids")
            if not isinstance(ids, list) or not ids:
                raise SecurityViolationError(f"S-3 evidence gate: {collection} claim has no source ID")
            if any(str(source_id) not in source_types for source_id in ids):
                raise SecurityViolationError(f"S-3 evidence gate: {collection} claim source type is not ECN/DB/KB")


PostProcessNode = BriefPostProcessNode
