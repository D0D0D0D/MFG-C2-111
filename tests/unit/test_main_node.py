"""Unit coverage for the Cat 2 main boundary."""

import inspect

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import ECNImpactGraphNode


def test_main_bypasses_inner_workflow_when_pre_quality_gate_is_not_eligible():
    result = ECNImpactGraphNode().execute(
        {"status": AgentStatus.SUCCESS.value, "processing_eligible": False, "normalized_parts": []}
    )
    assert result == {}


def test_main_bypasses_upstream_error_without_constructing_request_context():
    result = ECNImpactGraphNode().execute({"status": AgentStatus.ERROR.value, "processing_eligible": True})
    assert result == {}


def test_execute_contract_uses_execute_only():
    assert "_invoke_impl" not in ECNImpactGraphNode.__dict__
    assert list(inspect.signature(ECNImpactGraphNode.execute).parameters)[1] == "state"
