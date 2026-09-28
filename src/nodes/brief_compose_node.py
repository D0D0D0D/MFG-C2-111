"""Brief composition and D6 HITL interruption."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from langgraph.types import interrupt

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.evidence_gate.gate_node import ProvenanceContract, validate_provenance
from src.llm_draft_stage.draft_node import DraftStageHooks, LlmDraftStageNode
from src.services.brief_output_validation import validate_brief_claim_coverage
from src.services.prohibited_content import contains_prohibited_content
from src.services.response_contract import allowed_source_ids, project_response, response_contract


_HITL_PROVENANCE_CONTRACT = ProvenanceContract()


class BriefComposeNode(LlmDraftStageNode):
    """Create an advisory brief and suspend only when a reviewer is available."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, hooks=DraftStageHooks(_prompt, _fallback, _evidence))

    def execute(self, state: dict) -> dict:
        # A saved draft avoids a second model call on LangGraph's resume replay.
        saved = state.get("hitl_draft")
        if isinstance(saved, dict):
            draft_result = {"draft": saved, "status": AgentStatus.SUCCESS.value}
            evidence_map, cited = _evidence(state, saved)
            draft_result.update({"evidence_map": evidence_map, "cited_source_ids": cited})
        else:
            draft_result = super().execute(state)
        if draft_result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("brief_composed", {"outcome": "draft_error"}, state)
            return draft_result
        draft = project_response(draft_result["draft"], trusted_impact_scope=state.get("impact_scope", {}))

        result = _map_draft(state, draft, draft_result)
        review_required = bool(result["requires_human_review"])
        if review_required and state.get("hitl_allowed", True):
            self._validate_hitl_payload(state, result, draft)
            emit_trace_event("brief_composed", {"outcome": "hitl_requested"}, state)
            feedback = interrupt(
                {
                    "kind": "sqe_ecn_review",
                    "message": "SQE review is required before this advisory brief is used.",
                    "draft": draft,
                }
            )
            result["hitl_feedback"] = feedback
            result["hitl_status"] = HitlStatus.from_feedback(feedback).value
            result["hitl_draft"] = draft
        elif review_required:
            # Automated parents must terminate with a review-required brief,
            # rather than creating an interrupt that cannot be resumed.
            emit_trace_event("brief_composed", {"outcome": "review_required_no_interrupt"}, state)
        else:
            emit_trace_event("brief_composed", {"outcome": "completed"}, state)
        return result

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        if contains_prohibited_content(result):
            raise ValueError("BriefComposeNode: prohibited decision or update instruction in draft")
        return result

    def _validate_hitl_payload(self, state: dict, result: dict, draft: dict) -> None:
        """Apply the domain S-3 and provenance gates before publishing a HITL draft."""
        self._extra_security_gate_output(result)
        validate_brief_claim_coverage(result)

        evidence_map = result.get("evidence_map", [])
        hitl_evidence = list(evidence_map) if isinstance(evidence_map, list) else evidence_map
        if isinstance(hitl_evidence, list):
            for collection in ("risk_hypotheses", "recommended_checks"):
                hitl_evidence.extend(
                    {"source_ids": claim.get("source_ids")}
                    for claim in draft.get(collection, [])
                    if isinstance(claim, dict)
                )

        provenance = {
            "gate_kind": _HITL_PROVENANCE_CONTRACT.gate_kind,
            "status": result.get("status", AgentStatus.SUCCESS.value),
            "cited_source_ids": result.get("cited_source_ids", []),
            "retrieved_records": state.get("retrieved_records", []),
            "evidence_map": hitl_evidence,
        }
        validate_provenance(provenance, _HITL_PROVENANCE_CONTRACT)

        approved_ids = set(allowed_source_ids(state.get("retrieved_records", [])))
        referenced_ids = {
            str(source_id)
            for claim in hitl_evidence
            if isinstance(claim, dict)
            for source_id in claim.get("source_ids", [])
        }
        referenced_ids.update(str(source_id) for source_id in result.get("cited_source_ids", []))
        if not referenced_ids <= approved_ids:
            raise SecurityViolationError("S-3 provenance gate: HITL draft cites a source outside the approved catalog")


def _prompt(state: dict[str, Any]) -> str:
    payload = {key: state.get(key) for key in ("impact_scope", "evidence", "unknowns", "risk_hypotheses", "recommended_checks")}
    return (
        "Compose an SQE advisory ECN impact brief as JSON. Preserve source_ids and unknowns. "
        "Do not decide approval, change BOM/AVL, decide regulatory compliance, or issue an external answer. "
        "The response contract below is mandatory.\n"
        + response_contract(retrieved_records=state.get("retrieved_records", []))
        + "\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _fallback(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "unknowns": state.get("unknowns", []),
        "risk_hypotheses": state.get("risk_hypotheses", []),
        "recommended_checks": state.get("recommended_checks", []),
        "requires_human_review": bool(state.get("requires_human_review", False)),
        "cited_source_ids": state.get("cited_source_ids", []),
    }


def _evidence(state: dict[str, Any], draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    draft = project_response(draft, trusted_impact_scope=state.get("impact_scope", {}))
    existing = state.get("evidence_map") or state.get("evidence") or []
    cited = draft.get("cited_source_ids") or state.get("cited_source_ids") or []
    return (list(existing) if isinstance(existing, list) else [], [str(value) for value in cited])


def _map_draft(state: dict, draft: dict, stage: dict) -> dict:
    result = {
        "impact_scope": draft.get("impact_scope", state.get("impact_scope", {})),
        "evidence": stage.get("evidence_map", state.get("evidence", [])),
        "evidence_map": stage.get("evidence_map", state.get("evidence_map", [])),
        "cited_source_ids": stage.get("cited_source_ids", state.get("cited_source_ids", [])),
        "unknowns": draft.get("unknowns", state.get("unknowns", [])),
        "risk_hypotheses": draft.get("risk_hypotheses", state.get("risk_hypotheses", [])),
        "recommended_checks": draft.get("recommended_checks", state.get("recommended_checks", [])),
        "requires_human_review": bool(draft.get("requires_human_review", state.get("requires_human_review", False))),
        "status": AgentStatus.SUCCESS.value,
    }
    if stage.get("degradation_reason"):
        result["degradation_reason"] = stage["degradation_reason"]
    return result
