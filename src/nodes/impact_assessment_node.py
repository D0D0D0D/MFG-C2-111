"""Evidence-bound impact assessment draft stage."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.llm_draft_stage.draft_node import DraftStageHooks, LlmDraftStageNode
from src.services.response_contract import allowed_source_ids, project_response, response_contract, source_catalog


class ImpactAssessmentNode(LlmDraftStageNode):
    """Use an injected LLM only to form hypotheses, never approvals."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, hooks=DraftStageHooks(_prompt, _fallback, _evidence))

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("impact_assessed", {"outcome": "draft_error"}, state)
            return result
        draft = project_response(result.get("draft"), trusted_impact_scope=state.get("impact_scope", {}))
        mapped = {
            "impact_scope": draft["impact_scope"],
            "evidence": result.get("evidence_map", []),
            "evidence_map": result.get("evidence_map", []),
            "cited_source_ids": result.get("cited_source_ids", []),
            "unknowns": draft["unknowns"],
            "risk_hypotheses": draft["risk_hypotheses"],
            "recommended_checks": draft["recommended_checks"],
            "requires_human_review": bool(draft.get("requires_human_review", False) or state.get("requires_human_review", False)),
            "status": AgentStatus.SUCCESS.value,
        }
        if result.get("degradation_reason"):
            mapped["degradation_reason"] = result["degradation_reason"]
        emit_trace_event("impact_assessed", {"outcome": "completed", "citation_count": len(mapped["cited_source_ids"])}, state)
        return mapped

    def _extra_security_gate_input(self, state: dict) -> dict:
        # The LLM prompt is built from an explicit allowlist of deterministic
        # records in `_prompt()` (ecn/impact_scope/retrieved_records/unknowns);
        # this gate additionally fails closed on any credential-bearing field.
        if any(key.startswith("credential") for key in state):
            return {**state, "status": AgentStatus.ERROR.value, "error_log": ["ImpactAssessmentNode: forbidden prompt field"]}
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        if not isinstance(result.get("impact_scope"), dict):
            raise ValueError("ImpactAssessmentNode: mapped impact_scope must be an object")
        return result


def _prompt(state: dict[str, Any]) -> str:
    allowed = {
        "ecn": state.get("ecn", {}),
        "impact_scope": state.get("impact_scope", {}),
        "retrieved_records": state.get("retrieved_records", []),
        "unknowns": state.get("unknowns", []),
    }
    return (
        "Assess ECN impact. Return JSON only. Do not approve, update BOM/AVL, or provide an "
        "external final answer. The response contract below is mandatory.\n"
        + response_contract(retrieved_records=state.get("retrieved_records", []))
        + "\nINPUT:\n"
        + json.dumps(allowed, ensure_ascii=False, default=str)
    )


def _fallback(state: dict[str, Any]) -> dict[str, Any]:
    ids = _source_ids(state)
    ecn_id = _first_id_of_type(state, "ecn")
    db_id = _first_id_of_type(state, "db")
    kb_id = _first_id_of_type(state, "kb")
    scope_ids = [source_id for source_id in (ecn_id, db_id, kb_id) if source_id]
    risks = (
        [{"category": "performance", "claim": "Confirm impact before release.", "source_ids": scope_ids}]
        if scope_ids
        else []
    )
    checks = (
        [{"check": "SQE should review product and lot traceability.", "source_ids": [source_id for source_id in (ecn_id, db_id) if source_id]}]
        if ecn_id or db_id
        else []
    )
    return {
        "unknowns": list(state.get("unknowns", [])) + ([{"kind": "precedent", "reason": "similar ECN history unavailable"}] if not kb_id else []),
        "risk_hypotheses": risks,
        "recommended_checks": checks,
        "requires_human_review": True,
        "cited_source_ids": ids,
    }


def _evidence(state: dict[str, Any], draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    draft = project_response(draft, trusted_impact_scope=state.get("impact_scope", {}))
    record_ids = set(_source_ids(state))
    cited = [str(value) for value in draft.get("cited_source_ids", []) if str(value) in record_ids]
    claims: list[dict[str, Any]] = []
    for index, item in enumerate([*draft.get("risk_hypotheses", []), *draft.get("recommended_checks", [])]):
        if isinstance(item, dict):
            ids = [str(value) for value in item.get("source_ids", []) if str(value) in record_ids]
            if ids:
                claims.append({"claim_id": f"claim_{index + 1}", "claim_text": str(item.get("claim") or item.get("check") or ""), "source_ids": ids})
                cited.extend(ids)
    return claims, list(dict.fromkeys(cited))


def _source_ids(state: dict) -> list[str]:
    return allowed_source_ids(state.get("retrieved_records", []))


def _first_id_of_type(state: dict, source_type: str) -> str:
    for record in source_catalog(state.get("retrieved_records", [])):
        if record["source_type"] == source_type:
            return record["source_id"]
    return ""
