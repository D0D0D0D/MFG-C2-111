"""Entry-point auth + token-gated STG default scope for src/api/server.py."""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from framework.schemas.trust_level import TrustLevel
from shared.services.database.base_db import BaseDatabase
from src.services.service import ServiceBundle


def _client(monkeypatch, *, token: str | None = None, stg_tenant: str | None = None):
    if token is not None:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", token)
    else:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    if stg_tenant is not None:
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", stg_tenant)
    else:
        monkeypatch.delenv("STG_DEFAULT_TENANT_ID", raising=False)
    import src.api.server as srv
    importlib.reload(srv)
    return srv, TestClient(srv.app)


_ECN = json.dumps(
    {
        "ecn_id": "ECN-STG-001",
        "supplier_id": "SUP-STG-001",
        "parts": [{"part_id": "PART-001"}],
        "attachments": [{"name": "spec.pdf"}],
        "body": "smoke: material substitution notice.",
        "effective_date": "2026-08-01",
    }
)


def test_anonymous_without_token_is_rejected(monkeypatch):
    _srv, client = _client(monkeypatch, token=None)
    r = client.post("/invoke", json={"input": _ECN, "session_id": "s"})
    assert r.status_code == 403


def test_bad_token_is_rejected(monkeypatch):
    _srv, client = _client(monkeypatch, token="good-token", stg_tenant="stg-tenant")
    r = client.post("/invoke", json={"input": _ECN, "session_id": "s"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_auth_without_stg_default_scope_is_403(monkeypatch):
    # Token promotes trust, but no verified/default scope still fails closed.
    _srv, client = _client(monkeypatch, token="good-token")
    r = client.post("/invoke", json={"input": _ECN, "session_id": "s"}, headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 403


def test_token_auth_with_stg_default_scope_succeeds(monkeypatch):
    # STG provisional smoke path: token auth + STG default scope → 200 success.
    _srv, client = _client(monkeypatch, token="good-token", stg_tenant="stg-tenant")
    r = client.post("/invoke", json={"input": _ECN, "session_id": "s"}, headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_stg_default_never_applies_without_token_auth(monkeypatch):
    # A broken/absent verifier that leaves ANONYMOUS must be rejected before the STG
    # default is reachable — the default must not silently serve unauthenticated callers.
    _srv, client = _client(monkeypatch, token=None, stg_tenant="stg-tenant")
    r = client.post("/invoke", json={"input": _ECN, "session_id": "s"})
    assert r.status_code == 403


def test_resume_requires_same_entry_auth(monkeypatch):
    _srv, client = _client(monkeypatch, token="good-token", stg_tenant="stg-tenant")
    r = client.post(
        "/resume",
        json={"thread_id": "t", "session_id": "s", "feedback": {"action": "approve"}},
    )
    assert r.status_code == 401


def test_read_only_token_cannot_resume_even_with_stg_scope(monkeypatch):
    _srv, client = _client(monkeypatch, token="good-token", stg_tenant="stg-tenant")
    r = client.post(
        "/resume",
        json={"thread_id": "t", "session_id": "s", "feedback": {"action": "approve"}},
        headers={"Authorization": "Bearer good-token"},
    )
    assert r.status_code == 403


class MatchingDatabase(BaseDatabase):
    def fetch_one(self, query, params=None):
        return None

    def fetch_many(self, query, params=None):
        return [
            {
                "part_key": params["part_key"],
                "product_id": "product-a",
                "lot_id": "lot-a",
                "register_id": "db-a",
            }
        ]

    def execute(self, query, params=None):
        return None


def _middleware_client(monkeypatch):
    srv, _unused = _client(monkeypatch, token=None)

    @srv.app.middleware("http")
    async def verified_middleware(request, call_next):
        request.state.trust_level = TrustLevel.VERIFIED_EXTERNAL
        request.state.caller_id = "sqe-operator"
        request.state.tenant_id = "tenant-a"
        return await call_next(request)

    srv.wire_services(services=ServiceBundle(db=MatchingDatabase()))
    return srv, TestClient(srv.app)


def _pending_thread(client: TestClient) -> tuple[str, str]:
    invoked = client.post("/invoke", json={"input": _ECN, "session_id": "session-a"})
    assert invoked.status_code == 200
    assert invoked.json()["status"] == "awaiting_human"
    return invoked.json()["thread_id"], invoked.json()["session_id"]


def test_completed_hitl_thread_cannot_be_resumed_twice(monkeypatch):
    _srv, client = _middleware_client(monkeypatch)
    thread_id, session_id = _pending_thread(client)

    first = client.post(
        "/resume",
        json={"thread_id": thread_id, "session_id": session_id, "feedback": {"action": "approve"}},
    )
    second = client.post(
        "/resume",
        json={"thread_id": thread_id, "session_id": session_id, "feedback": {"action": "approve"}},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "success"
    assert second.status_code == 409


def test_resume_requires_the_issued_session_id(monkeypatch):
    _srv, client = _middleware_client(monkeypatch)
    thread_id, _session_id = _pending_thread(client)

    response = client.post(
        "/resume",
        json={"thread_id": thread_id, "session_id": "wrong-session", "feedback": {"action": "approve"}},
    )

    assert response.status_code == 409


def test_parallel_resume_requests_have_exactly_one_winner(monkeypatch):
    _srv, client = _middleware_client(monkeypatch)
    thread_id, session_id = _pending_thread(client)
    barrier = Barrier(2)

    def resume_once():
        barrier.wait()
        return client.post(
            "/resume",
            json={"thread_id": thread_id, "session_id": session_id, "feedback": {"action": "approve"}},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [future.result() for future in [executor.submit(resume_once), executor.submit(resume_once)]]

    assert sorted(response.status_code for response in responses) == [200, 409]
