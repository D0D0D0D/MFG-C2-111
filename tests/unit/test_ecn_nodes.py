"""Domain unit tests: both allowed and blocked security paths use fakes."""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.brief_compose_node import BriefComposeNode
from shared.services.database.base_db import BaseDatabase
from src.nodes.post_process_node import BriefPostProcessNode
from src.nodes.pre_process_node import ECNPreProcessNode
from src.nodes.precedent_recall_node import PrecedentRecallNode
from src.nodes.register_match_node import RegisterMatchNode
from src.services.prohibited_content import contains_prohibited_content


class FakeDatabase(BaseDatabase):
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[dict] = []

    def fetch_one(self, query, params=None):
        return None

    def fetch_many(self, query, params=None):
        self.calls.append(dict(params or {}))
        return self.rows

    def execute(self, query, params=None):
        return None


def ecn(**extra):
    payload = {
        "ecn_id": "ECN-01",
        "supplier_id": "supplier-a",
        "parts": [{"part_id": "ＡＢＣ-1"}],
        "attachments": [{"name": "notice.pdf"}],
        "body": "material change",
    }
    payload.update(extra)
    return json.dumps(payload)


def test_preprocess_accepts_allowlisted_ecn_and_uses_verified_scope():
    node = ECNPreProcessNode()
    result = node(
        {
            "user_input": ecn(),
            "input_context": {"tenant_id": "tenant-a"},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["processing_eligible"] is True
    assert result["normalized_parts"][0]["part_key"] == "abc-1"


def test_preprocess_blocks_client_scope_mismatch_and_unknown_business_key():
    node = ECNPreProcessNode()
    mismatch = node(
        {
            "user_input": ecn(tenant_id="other"),
            "input_context": {"tenant_id": "tenant-a"},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )
    assert mismatch["status"] == AgentStatus.ERROR.value
    unknown = node(
        {
            "user_input": ecn(unapproved_field="no"),
            "input_context": {"tenant_id": "tenant-a"},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )
    assert unknown["status"] == AgentStatus.ERROR.value


def test_attachment_missing_is_review_required_but_not_error():
    result = ECNPreProcessNode().execute(
        {"user_input": ecn(attachments=[]), "input_context": {"tenant_id": "tenant-a"}}
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["processing_eligible"] is False
    assert result["requires_human_review"] is True


def test_register_uses_exact_tenant_scope_query_and_never_guesses_multiple_rows():
    db = FakeDatabase(
        [
            {"part_key": "abc-1", "product_id": "P1", "lot_id": "L1"},
            {"part_key": "abc-1", "product_id": "P2", "lot_id": "L2"},
        ]
    )
    envelope = json.dumps({"tenant_id": "tenant-a", "normalized_parts": [{"part_key": "abc-1"}], "ecn": {"ecn_id": "E1"}})
    result = RegisterMatchNode(db=db).execute({"user_input": envelope})
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["match_ready"] is False
    assert result["requires_human_review"] is True
    assert db.calls == [{"tenant_id": "tenant-a", "part_key": "abc-1"}]


def test_retrieval_missing_tenant_scope_fails_closed_without_backend_call():
    class Backend:
        calls = []

        def search(self, **kwargs):
            self.calls.append(kwargs)
            return []

    backend = Backend()
    result = PrecedentRecallNode(backend=backend).execute({"ecn": {"body": "x"}})
    assert result["status"] == AgentStatus.ERROR.value
    assert backend.calls == []


def test_post_gate_rejects_unproven_claim_and_prohibited_approval_wording():
    node = BriefPostProcessNode()
    state = {
        "mode": "invoke",
        "impact_scope": {},
        "evidence": [],
        "unknowns": [],
        "risk_hypotheses": [{"claim": "Approve this change", "source_ids": ["ecn_1"]}],
        "recommended_checks": [],
        "requires_human_review": True,
        "cited_source_ids": ["ecn_1"],
        "retrieved_records": [{"source_id": "ecn_1", "source_type": "ecn"}],
    }
    with pytest.raises(SecurityViolationError):
        node._extra_security_gate_output(node.execute(state))


def test_post_gate_rejects_claim_citing_id_outside_prompt_allowlist():
    node = BriefPostProcessNode()
    state = {
        "mode": "invoke",
        "impact_scope": {},
        "evidence": [],
        "unknowns": [],
        "risk_hypotheses": [{"claim": "Review material compatibility.", "source_ids": ["invented_1"]}],
        "recommended_checks": [],
        "requires_human_review": True,
        "cited_source_ids": ["ecn_1"],
        "retrieved_records": [{"source_id": "ecn_1", "source_type": "ecn"}],
    }

    with pytest.raises(SecurityViolationError, match="claim source type is not ECN/DB/KB"):
        node._extra_security_gate_output(node.execute(state))


def test_post_gate_accepts_claim_citing_only_prompt_allowlisted_id():
    node = BriefPostProcessNode()
    state = {
        "mode": "invoke",
        "impact_scope": {},
        "evidence": [],
        "unknowns": [],
        "risk_hypotheses": [{"claim": "Review material compatibility.", "source_ids": ["ecn_1"]}],
        "recommended_checks": [{"check": "Inspect lot traceability.", "source_ids": ["db_1"]}],
        "requires_human_review": True,
        "cited_source_ids": ["ecn_1", "db_1"],
        "retrieved_records": [
            {"source_id": "ecn_1", "source_type": "ecn"},
            {"source_id": "db_1", "source_type": "db"},
        ],
    }

    result = node._extra_security_gate_output(node.execute(state))

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["formatted_output"]["risk_hypotheses"][0]["source_ids"] == ["ecn_1"]
    assert result["formatted_output"]["recommended_checks"][0]["source_ids"] == ["db_1"]


@pytest.mark.parametrize(
    "content",
    [
        "Please update the BOM immediately",
        "revise the AVL",
        "change BOM revision",
        "ＢＯＭを更新",
        "承認します",
        "規制適合の最終判断",
    ],
)
def test_shared_s3_lexicon_blocks_bom_avl_instructions_in_both_nodes(content):
    assert contains_prohibited_content(content) is True
    with pytest.raises(ValueError, match="prohibited"):
        BriefComposeNode()._extra_security_gate_output({"status": AgentStatus.SUCCESS.value, "draft": {"text": content}})
    with pytest.raises(SecurityViolationError, match="prohibited"):
        BriefPostProcessNode()._sanitize_client_echoes({"summary": content})


@pytest.mark.parametrize("content", ["performance", "the BOM structure is unchanged"])
def test_shared_s3_lexicon_does_not_flag_non_instruction_content(content):
    assert contains_prohibited_content(content) is False
    result = {"status": AgentStatus.SUCCESS.value, "draft": {"text": content}}
    assert BriefComposeNode()._extra_security_gate_output(result) is result
    assert BriefPostProcessNode()._sanitize_client_echoes({"summary": content}) == {"summary": content}
