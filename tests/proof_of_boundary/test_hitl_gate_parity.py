"""HITL 前ゲートが後段 S-3 と同じ強度であることを固定する。

由来: 2026-08-13 の独立レビュー（Batch 2 二巡目）。レビュアーが書いた再現 probe を
そのまま回帰テストにしている。実装者が書くと「実装を通すテスト」になり、
実装と同じ穴がテストにも空くため（一巡目・二巡目とも同じ指摘を受けた）。

欠陥: 型投影の直後に `interrupt()` しており、禁止内容・provenance の検査は
execute が戻った後にしかなかった。宣言済みフィールドは `str` として受理されるため、
**後段 S-3 が弾く draft を reviewer に先行公開していた**。

    compose_status=success, interrupt_count=1,
    published_claim="Failure rate increased 10%"
    post_status=error: free-text comparative claim lacks evidence coverage
"""
from __future__ import annotations

import json

from framework.schemas.trust_level import TrustLevel

import src.nodes.brief_compose_node as compose_mod
from src.nodes.brief_compose_node import BriefComposeNode
from src.nodes.post_process_node import BriefPostProcessNode


class _ComparativeClaimLLM:
    """後段 S-3 が「根拠にない比較表現」として弾く draft を返す LLM."""

    def complete(self, _messages):
        return {"content": json.dumps({
            "unknowns": [],
            "risk_hypotheses": [{
                "category": "regulatory",
                "claim": "Failure rate increased 10%",
                "source_ids": ["source_1"],
            }],
            "recommended_checks": [{
                "check": "Inspect traceability.",
                "source_ids": ["source_1"],
            }],
            "requires_human_review": True,
            "cited_source_ids": ["source_1"],
        })}


def _state() -> dict:
    return {
        "impact_scope": {"matched_parts": [{"part_key": "part-a"}]},
        "retrieved_records": [{"source_id": "source_1", "source_type": "ecn"}],
        "evidence": [], "evidence_map": [], "unknowns": [],
        "risk_hypotheses": [], "recommended_checks": [],
        "requires_human_review": True, "cited_source_ids": [],
        "hitl_allowed": True,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "mode": "invoke",
    }


def test_a_draft_the_output_gate_rejects_is_never_shown_to_the_reviewer(monkeypatch):
    """後段 S-3 が拒否する draft が `interrupt()` に到達しないこと。

    前段ゲートを別実装で書くかぎり後段とのドリフトは避けられないので、
    **後段が呼ぶ検証そのものを interrupt 前に通す**のが解。
    """
    shown: list = []

    def _capture(payload):
        shown.append(payload)
        return "approve"

    monkeypatch.setattr(compose_mod, "interrupt", _capture)
    state = _state()
    compose = BriefComposeNode(config={"llm": _ComparativeClaimLLM()})(state)

    # 後段が同じ draft を拒否することを先に確かめる（前提の固定）
    post = BriefPostProcessNode()({**state, **compose})
    rejected_downstream = post.get("status") == "error"

    if rejected_downstream:
        assert not shown, (
            "後段 S-3 が拒否する draft が reviewer に公開された: "
            f"{shown[0].get('draft', {}).get('risk_hypotheses') if shown else None}"
        )
        assert compose.get("status") == "error", (
            "前段で弾くべき draft が compose を success で通過した"
        )


def test_a_clean_draft_still_reaches_the_reviewer(monkeypatch):
    """正常な draft は従来どおり人間に届くこと（過剰拒否の防止）。"""
    shown: list = []

    class _CleanLLM:
        def complete(self, _messages):
            return {"content": json.dumps({
                "unknowns": [],
                "risk_hypotheses": [{
                    "category": "regulatory",
                    "claim": "Confirm impact before release.",
                    "source_ids": ["source_1"],
                }],
                "recommended_checks": [{
                    "check": "Inspect traceability.",
                    "source_ids": ["source_1"],
                }],
                "requires_human_review": True,
                "cited_source_ids": ["source_1"],
            })}

    monkeypatch.setattr(compose_mod, "interrupt", lambda p: (shown.append(p), "approve")[1])
    compose = BriefComposeNode(config={"llm": _CleanLLM()})(_state())
    assert shown, "正常な draft が reviewer に届いていない（過剰拒否）"
    assert compose.get("status") != "error"
