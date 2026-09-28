"""The prompt contract and typed S-3 projection share one declaration."""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel
from src.nodes.brief_compose_node import BriefComposeNode
from src.nodes.impact_assessment_node import ImpactAssessmentNode
from src.services.response_contract import (
    RESPONSE_FIELDS,
    RISK_CATEGORIES,
    example_response_from_declarations,
    project_response,
    response_contract,
)


class JsonLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict[str, str]] = []

    def complete(self, messages):
        self.messages = list(messages)
        return {"content": json.dumps(self.payload)}


def _state() -> dict:
    return {
        "impact_scope": {"matched_parts": [{"part_key": "part-a"}]},
        "retrieved_records": [{"source_id": "source_1", "source_type": "ecn"}],
        "evidence": [],
        "evidence_map": [],
        "unknowns": [],
        "risk_hypotheses": [],
        "recommended_checks": [],
        "requires_human_review": True,
        "cited_source_ids": [],
        "hitl_allowed": False,
    }


def _response(*, claim: str = "Review material compatibility.", source_id: str = "source_1") -> dict:
    return {
        "unknowns": [],
        "risk_hypotheses": [{"category": "regulatory", "claim": claim, "source_ids": [source_id]}],
        "recommended_checks": [{"check": "Inspect lot traceability.", "source_ids": [source_id]}],
        "requires_human_review": True,
        "cited_source_ids": [source_id],
    }


def test_declarations_mechanically_build_a_response_accepted_by_both_stages() -> None:
    response = example_response_from_declarations()
    projected = project_response(response, trusted_impact_scope=_state()["impact_scope"])
    assert projected["impact_scope"] == _state()["impact_scope"]

    impact_llm = JsonLLM(response)
    impact = ImpactAssessmentNode(config={"llm": impact_llm}).execute(_state())
    assert impact["status"] == "success"
    assert impact["risk_hypotheses"][0]["category"] in RISK_CATEGORIES

    brief_llm = JsonLLM(response)
    brief = BriefComposeNode(config={"llm": brief_llm}).execute(_state())
    assert brief["status"] == "success"
    assert set(json.loads(response_contract())["top_level"][0]) >= {"name", "type", "required"}
    for field in RESPONSE_FIELDS:
        assert field.name in impact_llm.messages[0]["content"]
        assert field.name in brief_llm.messages[0]["content"]


@pytest.mark.parametrize(
    ("response", "blocked_value"),
    [
        (_response(claim="承認済み。BOMを更新。原文: SENSITIVE-DRAWING-TEXT"), "SENSITIVE-DRAWING-TEXT"),
        (_response(source_id="invented_source_999"), "invented_source_999"),
    ],
    ids=("prohibited-decision-and-source-text", "unverified-source-id"),
)
def test_hitl_draft_is_blocked_by_s3_and_provenance_before_interrupt(
    monkeypatch, response, blocked_value
) -> None:
    interrupt_payloads: list[dict] = []
    monkeypatch.setattr("src.nodes.brief_compose_node.interrupt", interrupt_payloads.append)
    state = {**_state(), "hitl_allowed": True, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}

    result = BriefComposeNode(config={"llm": JsonLLM(response)})(state)

    assert result["status"] == "error"
    assert interrupt_payloads == []
    assert blocked_value not in " ".join(result.get("error_log", []))


def test_safe_provenance_checked_draft_reaches_hitl_interrupt(monkeypatch) -> None:
    interrupt_payloads: list[dict] = []

    def approve(payload: dict) -> str:
        interrupt_payloads.append(payload)
        return "approve"

    monkeypatch.setattr("src.nodes.brief_compose_node.interrupt", approve)
    state = {**_state(), "hitl_allowed": True, "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}

    result = BriefComposeNode(config={"llm": JsonLLM(_response())})(state)

    assert result["status"] == "success"
    assert result["hitl_status"] == "approved"
    assert interrupt_payloads[0]["draft"]["cited_source_ids"] == ["source_1"]


def test_impact_prompt_lists_exact_s3_citable_source_ids_and_catalog() -> None:
    state = _state()
    state["retrieved_records"] = [
        {"source_id": "source_1", "source_type": "ecn", "summary": "Material change notice"},
        {"source_id": "db_1", "source_type": "db", "metadata": {"part_key": "part-a"}},
        {"source_id": "kb_1", "source_type": "kb", "snippet": "Prior traceability finding"},
        {"source_id": "web_1", "source_type": "web", "summary": "Not allowed by S-3"},
    ]
    llm = JsonLLM(example_response_from_declarations())

    result = ImpactAssessmentNode(config={"llm": llm}).execute(state)

    assert result["status"] == "success"
    prompt = llm.messages[0]["content"]
    contract_text = prompt.split("mandatory.\n", 1)[1].split("\nINPUT:\n", 1)[0]
    contract = json.loads(contract_text)
    assert contract["allowed_source_ids"] == ["source_1", "db_1", "kb_1"]
    assert contract["source_catalog"] == [
        {"source_id": "source_1", "source_type": "ecn", "excerpt": "Material change notice"},
        {"source_id": "db_1", "source_type": "db", "excerpt": "part-a"},
        {"source_id": "kb_1", "source_type": "kb", "excerpt": "Prior traceability finding"},
    ]
    assert "web_1" not in contract["allowed_source_ids"]
    assert "Do NOT invent source IDs." in contract["rules"]
    assert any("unknown source_id is rejected" in rule for rule in contract["rules"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.update({"undeclared_raw_document": "sensitive source text"}),
        lambda response: response["risk_hypotheses"][0].update(
            {"undeclared_raw_document": "sensitive source text"}
        ),
        lambda response: response["risk_hypotheses"][0].update(
            {"claim": {"raw_document_text": "sensitive source text"}}
        ),
        lambda response: response["risk_hypotheses"][0].update(
            {"source_ids": [{"raw_document_text": "sensitive source text"}]}
        ),
        lambda response: response["risk_hypotheses"][0].update({"category": "invented-risk"}),
    ],
)
def test_undeclared_or_wrong_typed_llm_content_fails_closed_without_echo(mutate) -> None:
    response = example_response_from_declarations()
    mutate(response)

    with pytest.raises(SecurityViolationError) as caught:
        ImpactAssessmentNode(config={"llm": JsonLLM(response)}).execute(_state())

    assert "sensitive source text" not in str(caught.value)
    assert "undeclared_raw_document" not in str(caught.value)
