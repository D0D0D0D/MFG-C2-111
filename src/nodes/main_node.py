"""Cat 2 main slot wrapping the ECN inner workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import DomainWorkflowGraph


class ECNImpactGraphNode(DomainWorkflowGraphNode):
    """Fail-closed boundary between the fixed backbone and inner workflow."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True
    hitl_output_keys: ClassVar[tuple[str, ...]] = (
        "hitl_draft",
        "hitl_feedback",
        "hitl_status",
        "subgraph_thread_id",
    )
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="impact_brief",
            fields={
                "impact_scope": "impact_scope",
                "evidence": "evidence",
                "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "unknowns": "unknowns",
                "risk_hypotheses": "risk_hypotheses",
                "recommended_checks": "recommended_checks",
                "requires_human_review": "requires_human_review",
                "status": "status",
            },
        )
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, subgraph_factory=lambda inner_config: DomainWorkflowGraph(config=inner_config))

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value or state.get("processing_eligible") is not True:
            emit_trace_event("ecn_inner_workflow", {"outcome": "bypassed"}, state)
            return {}
        emit_trace_event("ecn_inner_workflow", {"outcome": "started"}, state)
        return super().execute(state)

    def extract_input(self, state: dict) -> str:
        ecn: Any = state.get("ecn")
        if not isinstance(ecn, dict):
            try:
                ecn = json.loads(state.get("validated_input", ""))
            except (TypeError, json.JSONDecodeError):
                ecn = {}
        return json.dumps(
            {
                "tenant_id": state.get("tenant_id"),
                "normalized_parts": state.get("normalized_parts", []),
                "ecn": ecn,
            },
            ensure_ascii=False,
            default=str,
        )

    def _parent_config(self) -> dict[str, Any]:
        return dict(self._config)


# Compatibility name for older template test imports.
MainNode = ECNImpactGraphNode
