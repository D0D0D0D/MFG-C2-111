"""Inner deterministic/retrieval/drafting workflow for ECN impact briefs."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.brief_compose_node import BriefComposeNode
from src.nodes.impact_assessment_node import ImpactAssessmentNode
from src.nodes.precedent_recall_node import PrecedentRecallNode
from src.nodes.register_match_node import RegisterMatchNode
from src.schemas.state import State
from src.services.service import ServiceBundle


class DomainWorkflowGraph(BaseGraph):
    """Register exact matches before scoped recall and advisory drafting."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        self._services = self.config.get("services") or ServiceBundle(
            db=self.config.get("db"), vector_store=self.config.get("vector_store"), llm=self.config.get("llm")
        )

    @property
    def name(self) -> str:
        return "supplier_ecn_impact_domain_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # Clients are optional because deterministic review-required fallbacks
        # are valid in local/test operation.  They are injected, never created.
        return None

    def register_nodes(self) -> None:
        self._nodes["register_match"] = RegisterMatchNode(db=self._services.db)
        self._nodes["precedent_recall"] = PrecedentRecallNode(backend=self._services.retrieval_backend)
        stage_config = {"llm": self._services.llm}
        self._nodes["impact_assessment"] = ImpactAssessmentNode(config=stage_config)
        self._nodes["brief_compose"] = BriefComposeNode(config=stage_config)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "register_match")
        self._sg.add_conditional_edges("register_match", self.route_after_register)
        self._sg.add_edge("precedent_recall", "impact_assessment")
        self._sg.add_edge("impact_assessment", "brief_compose")
        self._sg.add_edge("brief_compose", END)

    def route_after_register(self, state: State) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "precedent_recall" if state.get("match_ready") else END

    def route(self, state: State) -> str:
        # Required BaseGraph contract; conditional routing is declared above.
        return self.route_after_register(state)

    def get_output(self, state: State) -> dict:
        return {
            "mode": state.get("mode", "invoke"),
            "impact_scope": state.get("impact_scope", {}),
            "evidence": state.get("evidence", []),
            "evidence_map": state.get("evidence_map", state.get("evidence", [])),
            "cited_source_ids": state.get("cited_source_ids", []),
            "retrieved_records": state.get("retrieved_records", state.get("register_records", [])),
            "unknowns": state.get("unknowns", []),
            "risk_hypotheses": state.get("risk_hypotheses", []),
            "recommended_checks": state.get("recommended_checks", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "status": state.get("status", AgentStatus.SUCCESS.value),
            "hitl_status": state.get("hitl_status"),
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "subgraph_thread_id": state.get("subgraph_thread_id"),
            "degradation_reason": state.get("degradation_reason"),
        }
