"""Checkpoint-safe flat state for SupplierECNImpactBriefAgent."""

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Domain fields only; framework fields are inherited from ``AgentState``.

    Every field is JSON/msgpack-compatible.  In particular, credentials and
    service clients must remain in the invocation/service wiring, never here.
    """

    tenant_id: NotRequired[str]
    normalized_parts: NotRequired[list[dict]]
    processing_eligible: NotRequired[bool]
    data_quality_flags: NotRequired[list[str]]
    ecn: NotRequired[dict]
    mode: NotRequired[str]
    impact_scope: NotRequired[dict]
    evidence: NotRequired[list[dict]]
    unknowns: NotRequired[list[dict]]
    risk_hypotheses: NotRequired[list[dict]]
    recommended_checks: NotRequired[list[dict]]
    requires_human_review: NotRequired[bool]
    retrieved_records: NotRequired[list[dict]]
    cited_source_ids: NotRequired[list[str]]
    evidence_map: NotRequired[list[dict]]
    degradation_reason: NotRequired[str]
    match_ready: NotRequired[bool]
    register_records: NotRequired[list[dict]]
    gate_kind: NotRequired[str]
    hitl_feedback: NotRequired[str | int | float | bool | list | dict | None]
    hitl_status: NotRequired[str | None]
    hitl_draft: NotRequired[str | int | float | bool | list | dict | None]
    hitl_metadata: NotRequired[dict]
    hitl_allowed: NotRequired[bool]
    hitl_count: NotRequired[int]
    subgraph_thread_id: NotRequired[str | None]
