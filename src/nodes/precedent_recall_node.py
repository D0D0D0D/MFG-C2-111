"""Scoped recall of prior ECN observations, never prior approvals."""

from __future__ import annotations

import unicodedata
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.scoped_retrieval.retrieval_node import ScopedRetrievalNode
from src.services.service import KbUnavailableError, KbValidationError, ScopedVectorBackend


class PrecedentRecallNode(ScopedRetrievalNode):
    """Apply the scoped-retrieval part with the ECN's mandatory tenant scope."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, backend: ScopedVectorBackend | None = None, top_k: int = 5) -> None:
        super().__init__(
            scope_keys=("tenant_id",),
            backend=backend,
            normalize_query=_normalize_query,
            top_k=top_k,
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("precedent_recall", {"outcome": "upstream_error"}, state)
            return {}
        query_state = dict(state)
        query_state["query"] = _query_from_ecn(state.get("ecn"))
        try:
            result = super().execute(query_state)
        except (KbUnavailableError, KbValidationError) as exc:
            error_code = str(exc)
            emit_trace_event("precedent_recall", {"outcome": error_code}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [error_code],
                "retrieved_records": [],
            }
        db_and_ecn = state.get("register_records", [])
        records = list(db_and_ecn) if isinstance(db_and_ecn, list) else []
        kb_records = result.get("retrieved_records", [])
        for record in kb_records:
            if isinstance(record, dict) and record.get("source_type") == "internal_kb":
                # The copied part safely projected the hit already; the domain
                # vocabulary uses the source kind requested by the design.
                record["source_type"] = "kb"
        records.extend(kb_records)
        result["retrieved_records"] = records
        if not result.get("degradation_reason") and len(records) == len(db_and_ecn):
            result["degradation_reason"] = "no scoped retrieval hits"
        emit_trace_event("precedent_recall", {"record_count": len(records)}, state)
        return result

    def _extra_security_gate_input(self, state: dict) -> dict:
        if "tenant_id" not in state:
            return state
        if not isinstance(state.get("tenant_id"), str):
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": ["PrecedentRecallNode: tenant_id retrieval scope is required"],
            }
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        # The copied part performs scope mismatch rejection and allowlist projection.
        return result


def _normalize_query(value: Any) -> str:
    if isinstance(value, dict):
        value = " ".join(str(value.get(key, "")) for key in ("ecn_id", "body", "supplier_id"))
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _query_from_ecn(ecn: Any) -> dict:
    return ecn if isinstance(ecn, dict) else {"body": ""}
