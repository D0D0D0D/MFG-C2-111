"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from shared.services.database.base_db import BaseDatabase
from src.graph.graph import SupplierECNImpactBriefAgent
from src.services.service import ServiceBundle
from dataclasses import replace


_LIVE_SCOPE = {"tenant_id": "tenant-live"}
_LIVE_SESSION_ID = "live-1"


class _LiveApprovedRegister(BaseDatabase):
    """Exact-match register adapter for the ECN part used by this case."""

    def fetch_one(self, query, params=None):
        return None

    def fetch_many(self, query, params=None):
        values = dict(params or {})
        if values != {**_LIVE_SCOPE, "part_key": "conn-hsg-440"}:
            return []
        return [
            {
                "part_key": "conn-hsg-440",
                "product_id": "ev-inverter-x7",
                "lot_id": "lot-2026-08-a",
                "register_id": "register_conn_hsg_440",
            }
        ]

    def execute(self, query, params=None):
        return None


class _LivePrecedentStore:
    """Stored-scope projection for a relevant historical ECN observation."""

    def search_scoped(
        self, *, scope: Mapping[str, str], query: str, top_k: int
    ) -> list[dict[str, Any]]:
        if dict(scope) != _LIVE_SCOPE or not query.strip():
            return []
        return [
            {
                "source_id": "kb_polymer_substitution_2025",
                "summary": (
                    "A connector-housing resin downgrade was associated with moisture-driven "
                    "dimensional drift and reduced terminal-retention margin."
                ),
                "metadata": {
                    **_LIVE_SCOPE,
                    "category": "performance",
                    "record_type": "ecn_observation",
                },
            }
        ][:top_k]


def _live_ecn() -> dict:
    """A complete ECN whose material change requires real impact reasoning."""
    return {
        "ecn_id": "ECN-2026-440",
        "supplier_id": "supplier-koyo-live",
        "parts": [{"part_id": "CONN-HSG-440"}],
        "attachments": [
            {"name": "supplier_material_change_notice.pdf"},
            {"name": "pa6_gf15_datasheet.pdf"},
        ],
        "body": (
            "The supplier proposes replacing PA66-GF30 with PA6-GF15 in the high-voltage "
            "connector housing, with no tooling change and an effective date of 2026-09-01. "
            "The notice claims equivalent fit but supplies no humidity ageing, terminal "
            "retention, flammability, or thermal-cycling comparison. Assess affected product "
            "and lot scope, likely performance and revalidation risks, and checks for SQE."
        ),
        "effective_date": "2026-09-01",
    }


@pytest.fixture
def _live_runtime(monkeypatch):
    services = ServiceBundle(
        db=_LiveApprovedRegister(),
        vector_store=_LivePrecedentStore(),
    )
    original_init = SupplierECNImpactBriefAgent.__init__
    original_invoke = SupplierECNImpactBriefAgent.invoke

    def init_with_live_services(self, config=None, *, services=None):
        resolved = dict(config or {})
        configured = resolved.pop("services", services) or services
        if not isinstance(configured, ServiceBundle):
            raise TypeError("live-check services must be a ServiceBundle")
        request_services = ServiceBundle(
            db=configured.db,
            vector_store=configured.vector_store,
            llm=resolved.get("llm"),
        )
        original_init(self, resolved, services=request_services)

    def invoke_with_verified_scope(self, user_input, *args, **kwargs):
        # The generic checker cannot express this server's auth-derived scope or
        # resumable HITL policy. Mirror /invoke for a non-resumable parent.
        context = kwargs.get("input_context")
        kwargs["input_context"] = {
            **(context if isinstance(context, dict) else {}),
            **_LIVE_SCOPE,
        }
        ctx = kwargs.get("ctx")
        if ctx is not None:
            # SDK 1.0.2 で InvocationContext は @dataclass(frozen=True) になったため
            # 属性代入は FrozenInstanceError。差し替えは dataclasses.replace で行う。
            kwargs["ctx"] = replace(ctx, hitl_allowed=False)
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(SupplierECNImpactBriefAgent, "__init__", init_with_live_services)
    monkeypatch.setattr(SupplierECNImpactBriefAgent, "invoke", invoke_with_verified_scope)
    return services


@pytest.fixture
def agent_config(_live_runtime) -> dict:
    return {
        "services": _live_runtime,
        "memory_enabled": True,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_runtime) -> dict:
    return _live_ecn()
