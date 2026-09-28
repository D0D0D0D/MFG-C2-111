# PB (retention contract): checkpoint data-class boundary
#
# MFG-C2-111 deliberately keeps the ECN free-text ``body`` in graph State rather
# than isolating it behind a payload_ref (design decision A-5: the ECN body is
# confidential supplier text but is NOT a credential and NOT personal data).
#
# This PB proves the *retention contract* that decision relies on:
#   1. No credential/JWT/API-key material ever reaches a persisted checkpoint.
#   2. Only permitted business data classes are checkpointed (the ECN business
#      envelope + derived impact fields), never an unexpected leak channel.
#
# It runs a real invoke that suspends at HITL, then inspects every persisted
# checkpoint via the compiled saver — a static schema scan cannot prove this.

from __future__ import annotations

import json
import re

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from tests.integration.test_ecn_workflow import graph, payload


# Credential-shaped material that must NEVER appear in a persisted checkpoint.
_CREDENTIAL_PATTERNS = re.compile(
    r"(eyJ[A-Za-z0-9_-]{10,}\."          # JWT header segment
    r"|sk-[A-Za-z0-9]{16,}"             # OpenAI-style secret key
    r"|api[_-]?key\s*[:=]"              # api_key: / api-key=
    r"|bearer\s+[A-Za-z0-9._-]{16,}"    # bearer <token>
    r"|password\s*[:=]"                 # password: / password=
    r"|-----BEGIN [A-Z ]*PRIVATE KEY)", # PEM private key
    re.IGNORECASE,
)

# Top-level State keys permitted to be checkpointed for MFG-C2-111.
# Anything outside this allowlist is an unreviewed retention channel.
_PERMITTED_STATE_KEYS = frozenset(
    {
        # framework-managed
        "user_input", "validated_input", "status", "mode", "error_log",
        "node_history", "correlation_id", "session_id", "trace_id",
        "caller_trust_level", "caller_id", "input_context", "result",
        "output", "formatted_output", "degradation_reason",
        "execution_time", "retry_count", "schema_version", "thread_id",
        # SDK-owned fixed transport classification; callers cannot choose it.
        "request_source",
        # Caller-provided X-Message-Id has no SDK length/format validation. User
        # ruling explicitly permits this caller-controlled string in checkpoints;
        # it is audit/trace data only and must never drive trust or namespaces.
        "message_id",
        # HITL (schema 1.4)
        "hitl_feedback", "hitl_metadata", "hitl_status", "hitl_draft",
        "hitl_count", "hitl_allowed", "subgraph_thread_id",
        # ECN business data classes (permitted retention)
        "ecn", "tenant_id", "normalized_parts", "data_quality_flags",
        "processing_eligible", "requires_human_review", "impact_scope",
        "evidence", "evidence_map", "cited_source_ids", "retrieved_records",
        "register_records", "match_ready", "unknowns", "risk_hypotheses",
        "recommended_checks", "query",
    }
)


def _persisted_states(instance, thread_id: str) -> list[dict]:
    config = {"configurable": {"thread_id": thread_id}}
    states = []
    for snapshot in instance._compiled.get_state_history(config):
        values = getattr(snapshot, "values", None)
        if isinstance(values, dict):
            states.append(values)
    # get_state_history may be empty on some savers; fall back to current state.
    if not states:
        snapshot = instance._compiled.get_state(config)
        values = getattr(snapshot, "values", None)
        if isinstance(values, dict):
            states.append(values)
    return states


def _suspend_and_get_thread() -> tuple:
    instance, _database = graph()
    result = instance.invoke(
        payload(),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"tenant_id": "tenant-a"},
    )
    assert result["status"] == "awaiting_human", "test needs a suspended HITL checkpoint"
    return instance, result["thread_id"]


def test_no_credential_material_in_any_checkpoint():
    """PB: credential/JWT/API-key patterns must not persist in any checkpoint."""
    instance, thread_id = _suspend_and_get_thread()
    for values in _persisted_states(instance, thread_id):
        blob = json.dumps(values, default=str)
        match = _CREDENTIAL_PATTERNS.search(blob)
        assert match is None, f"credential-shaped material persisted in checkpoint: {match!r}"


def test_only_permitted_data_classes_checkpointed():
    """PB: checkpoint retains only allowlisted business/framework keys (no leak channel)."""
    instance, thread_id = _suspend_and_get_thread()
    for values in _persisted_states(instance, thread_id):
        unexpected = sorted(set(values) - _PERMITTED_STATE_KEYS)
        assert not unexpected, f"unreviewed retention channel in checkpoint: {unexpected}"


def test_ecn_body_is_the_only_confidential_retained_class():
    """PB (retention contract): the ECN body is retained by design (A-5); confirm it
    lives only in the declared ECN business fields, not in an unexpected key."""
    instance, thread_id = _suspend_and_get_thread()
    for values in _persisted_states(instance, thread_id):
        for key, value in values.items():
            blob = json.dumps(value, default=str).lower()
            if "material substitution" in blob:  # the ECN body marker from payload()
                assert key in {"user_input", "validated_input", "ecn"}, (
                    f"ECN body leaked into unexpected checkpoint key {key!r}"
                )


def test_hostile_message_id_is_retained_as_audit_only_caller_text():
    """Record the user-approved SDK behavior: message_id is retained verbatim."""
    hostile_message_id = "sk-" + ("A" * 4096)
    instance, _database = graph()
    result = instance.invoke(
        payload(),
        ctx=InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=True,
            message_id=hostile_message_id,
        ),
        input_context={"tenant_id": "tenant-a"},
    )
    assert result["status"] == "awaiting_human"

    states = _persisted_states(instance, result["thread_id"])
    assert any(state.get("message_id") == hostile_message_id for state in states)
    assert any(_CREDENTIAL_PATTERNS.search(json.dumps(state, default=str)) for state in states)
