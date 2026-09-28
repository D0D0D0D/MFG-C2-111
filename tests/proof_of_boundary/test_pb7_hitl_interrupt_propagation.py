# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# CONDITIONAL: Required only when config/config.yaml has hitl.enabled: true
# Templates that do not use HITL will see this file produce SKIPPED results.
#
# Replace MyMainNode (and the state dict) with the node that calls interrupt().
#
# Two test cases verify the PB-7 boundary behavior:
#
#   test_pb7_hitl_interrupt_propagates()
#       Verifies that interrupt() raises GraphInterrupt and the signal propagates
#       through BaseNode.__call__() to the LangGraph engine — NOT caught by the
#       application error boundary.
#
#   test_pb7_hitl_allowed_false_skips_interrupt()
#       Verifies that the hitl_allowed=False guard prevents interrupt() from
#       firing — no GraphInterrupt raised, no deadlock.
#
# References:
#   Proof-of-boundary item PB-7 (HITL interrupt propagation)
#   The framework's HITL contract: the D6 interrupt() pattern and its
#   mandatory hitl_allowed guard

from __future__ import annotations

import pathlib
import warnings

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

# ---------------------------------------------------------------------------
# Conditional skip — only runs when config/config.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

# ---------------------------------------------------------------------------
# Imports (uncomment and replace MyMainNode with the template node that calls
# interrupt() inside execute())
# ---------------------------------------------------------------------------

# from src.nodes.main_node import MyMainNode  # TODO: replace with actual node class


# ---------------------------------------------------------------------------
# Helper — build a minimal base state for the node under test
# ---------------------------------------------------------------------------

def _base_state(**overrides) -> dict:
    """Return a minimal state dict for PB-7 tests.

    Replace / extend with the fields your node's execute() actually reads.
    """
    state = {
        # Framework-managed fields
        "caller_trust_level": "anonymous",
        "correlation_id": "pb7-test",
        "node_history": [],
        "error_log": [],
        # HITL fields
        "hitl_allowed": True,           # overridden per test case
        "hitl_count": 0,
        # TODO: add domain-specific fields required by execute()
        # e.g. "validated_input": "test value",
        #      "confidence_score": 0.5,
    }
    state.update(overrides)
    return state


def _graph():
    """Compile the real agent. HITL needs the framework checkpointer, so the
    interrupt must be exercised through a compiled graph -- calling the node
    directly fails with "Called get_config outside of a runnable context",
    because LangGraph's interrupt() requires a runnable context to suspend in.
    """
    from src.graph.graph import SupplierECNImpactBriefAgent
    from tests.integration.test_ecn_workflow import FakeDatabase, payload
    from src.services.service import ServiceBundle

    instance = SupplierECNImpactBriefAgent(services=ServiceBundle(db=FakeDatabase()))
    instance.compile()
    return instance, payload()


def _ctx(*, hitl_allowed: bool):
    from framework.schemas.invocation_context import InvocationContext

    return InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=hitl_allowed)


_SCOPE = {"tenant_id": "tenant-a"}


# ---------------------------------------------------------------------------
# PB-7-A: interrupt() raises GraphInterrupt and propagates
# ---------------------------------------------------------------------------


def test_pb7_hitl_interrupt_propagates() -> None:
    """PB-7: GraphInterrupt reaches the LangGraph engine; status is NOT error.

    `BaseNode.__call__()` wraps `execute()` in a broad `except Exception`. Since
    `GraphInterrupt` subclasses `Exception`, a missing sentinel branch would
    convert a HITL suspension into `status: error` -- the run would look like a
    failure and could never be resumed. The framework guards this with
    `except GraphBubbleUp: raise` placed before the generic handler; this test
    fixes that behaviour from the template's side.

    Asserting `awaiting_human` (rather than `pytest.raises`) is what proves the
    signal reached the engine: the engine is the thing that converts a
    propagated GraphInterrupt into a suspended run with a resumable thread.
    """
    instance, body = _graph()

    result = instance.invoke(body, ctx=_ctx(hitl_allowed=True), input_context=_SCOPE)

    assert result["status"] == "awaiting_human", (
        f"interrupt did not reach the engine: status={result['status']!r} "
        f"error_log={result.get('error_log')}"
    )
    assert result["status"] != AgentStatus.ERROR.value
    assert result.get("thread_id"), "a suspended run must expose a resumable thread_id"


# ---------------------------------------------------------------------------
# PB-7-B: hitl_allowed=False guard prevents deadlock
# ---------------------------------------------------------------------------


def test_pb7_hitl_allowed_false_skips_interrupt() -> None:
    """PB-7 guard: hitl_allowed=False must NOT suspend (no deadlock).

    Covers the framework's mandatory hitl_allowed guard. An automated parent (GraphNode) cannot
    resume a thread, so an unguarded interrupt() deadlocks the composition.
    """
    instance, body = _graph()

    result = instance.invoke(body, ctx=_ctx(hitl_allowed=False), input_context=_SCOPE)

    assert result["status"] != "awaiting_human", (
        "hitl_allowed=False still suspended the run -- an automated parent "
        "would deadlock on a thread it cannot resume"
    )
    assert result["status"] != AgentStatus.ERROR.value
    # Suppressing the interrupt must not silently downgrade the finding.
    assert result["output"]["requires_human_review"] is True
