"""Outer fixed backbone for SupplierECNImpactBriefAgent (Cat 2)."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import ECNImpactGraphNode
from src.nodes.post_process_node import BriefPostProcessNode
from src.nodes.pre_process_node import ECNPreProcessNode
from src.schemas.state import State
from src.services.service import ServiceBundle, current_qdrant_audit_state


class SupplierECNImpactBriefAgent(AgentBaseGraph):
    """Fixed backbone with a cached, separately checkpointed inner graph."""

    def __init__(self, config: dict[str, Any] | None = None, *, services: ServiceBundle | None = None) -> None:
        runtime = {
            "max_retry": 3,
            "timeout_s": 30,
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 8},
        }
        runtime.update(config or {})
        super().__init__(config=runtime)
        self._services = services or ServiceBundle()
        self._outer_checkpointer = InMemorySaver()

    @property
    def name(self) -> str:
        return "SupplierECNImpactBriefAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _extra_initial_state(self) -> dict[str, Any]:
        """Reuse the entry adapter's trace IDs for request-local KB audits."""

        return current_qdrant_audit_state()

    def register_nodes(self) -> None:
        super().register_nodes()
        inner_config = {**self.config, "services": self._services}
        self._nodes["pre_process"] = ECNPreProcessNode()
        self._nodes["main"] = ECNImpactGraphNode(config=inner_config)
        self._nodes["post_process"] = BriefPostProcessNode()

    def compile(self, checkpointer=None) -> None:
        # Explicit and distinct from ECNImpactGraphNode's inner InMemorySaver.
        self._outer_checkpointer = checkpointer or InMemorySaver()
        super().compile(checkpointer=self._outer_checkpointer)

    def get_output(self, state) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {
                "output": {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])},
                "status": AgentStatus.ERROR.value,
                "trace_id": state.get("trace_id", ""),
                "correlation_id": state.get("correlation_id", ""),
                "node_history": state.get("node_history", []),
            }
        return super().get_output(state)


# The manifest entry point refers to this conventional alias.
Graph = SupplierECNImpactBriefAgent
