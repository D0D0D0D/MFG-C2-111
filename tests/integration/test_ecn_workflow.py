"""Integration tests use in-memory fakes; no network service is contacted."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.database.base_db import BaseDatabase
from src.graph.graph import SupplierECNImpactBriefAgent
from src.services.service import ServiceBundle


class FakeDatabase(BaseDatabase):
    def __init__(self):
        self.fetch_many_calls = 0

    def fetch_one(self, query, params=None):
        return None

    def fetch_many(self, query, params=None):
        self.fetch_many_calls += 1
        return [{"part_key": params["part_key"], "product_id": "product-a", "lot_id": "lot-a", "register_id": "db_a"}]

    def execute(self, query, params=None):
        return None


def payload() -> str:
    return json.dumps(
        {
            "ecn_id": "ecn-a",
            "supplier_id": "supplier-a",
            "parts": [{"part_id": "part-a"}],
            "attachments": [{"name": "ecn.pdf"}],
            "body": "material substitution notice",
        }
    )


def graph() -> tuple[SupplierECNImpactBriefAgent, FakeDatabase]:
    database = FakeDatabase()
    instance = SupplierECNImpactBriefAgent(services=ServiceBundle(db=database))
    instance.compile()
    return instance, database


def test_automated_parent_skips_interrupt_and_returns_review_required_brief():
    instance, _database = graph()
    result = instance.invoke(
        payload(),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False),
        input_context={"tenant_id": "tenant-a"},
    )
    assert result["status"] == "success"
    assert result["output"]["requires_human_review"] is True
    assert set(result["output"]) >= {"impact_scope", "evidence", "unknowns", "risk_hypotheses", "recommended_checks"}


def test_hitl_interrupt_then_resume_reaches_success_without_rerunning_llm():
    instance, _database = graph()
    interrupted = instance.invoke(
        payload(),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"tenant_id": "tenant-a"},
    )
    assert interrupted["status"] == "awaiting_human"
    resumed = instance.resume(interrupted["thread_id"], {"action": "approve"})
    assert resumed["status"] == "success"
    assert resumed["output"]["mode"] == "invoke"


def test_missing_attachment_routes_to_neutral_review_brief_without_inner_db_call():
    instance, database = graph()
    incomplete = json.loads(payload())
    incomplete["attachments"] = []
    result = instance.invoke(
        json.dumps(incomplete),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False),
        input_context={"tenant_id": "tenant-a"},
    )
    assert result["status"] == "success"
    assert result["output"]["requires_human_review"] is True
    assert database.fetch_many_calls == 0
