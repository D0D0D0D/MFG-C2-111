"""Regression tests for deployment pins and Qdrant service boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.secrets import InMemoryProvider
from shared.services.runtime import AsyncServiceRuntime
from shared.services.vector_store.base_vector_store import (
    VectorStoreTimeoutError,
    VectorStoreUnavailableError,
    VectorStoreValidationError,
)
from src.nodes.precedent_recall_node import PrecedentRecallNode
from src.services.service import (
    KbUnavailableError,
    KbValidationError,
    QdrantScopedVectorStore,
    ScopedVectorBackend,
    ServiceBundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SPEC = (
    "agenticstar-agentcore[marketplace,openai,platform-rag,platform-memory,"
    "platform-db,platform-storage-azure]==1.0.3"
)
VECTOR_CONFIG: dict[str, Any] = {
    "memory_enabled": True,
    "hitl": {"enabled": True, "max_hitl": 8},
    "vector_store": {
        "url": "http://qdrant.invalid:6333",
        "collection_name": "mfg_c2_111_test",
        "embedding_base_url": "http://embedding.invalid",
        "vector_size": 8,
    },
}


class _Provider(InMemoryProvider):
    def __init__(self, *, with_key: bool = True) -> None:
        values = {"AZURE_EMBEDDING_API_KEY": "test-only-key"} if with_key else {}
        super().__init__(values, namespace="mfg", agent_name="SupplierECNImpactBriefAgent")



@pytest.mark.parametrize(
    ("error", "expected_type", "expected_message"),
    [
        (VectorStoreUnavailableError("provider echoed bearer secret-token"), KbUnavailableError, "kb_unavailable"),
        (VectorStoreTimeoutError("provider echoed /private/customer/query"), KbUnavailableError, "kb_unavailable"),
        (VectorStoreValidationError("provider echoed query body"), KbValidationError, "kb_invalid_request"),
    ],
)
def test_qdrant_adapter_normalizes_provider_exception_text(
    error: Exception,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    class RaisingBackend:
        def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise error

    store = QdrantScopedVectorStore(cast(Any, RaisingBackend()))

    with pytest.raises(expected_type) as raised:
        store.search_scoped(
            scope={"tenant_id": "tenant-a"},
            query="sensitive query",
            top_k=5,
        )

    assert str(raised.value) == expected_message
    assert "provider echoed" not in str(raised.value)


def test_provider_failure_is_fail_closed_without_checkpoint_or_audit_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "provider echoed bearer secret-token /private/customer/query"

    class RaisingBackend:
        def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise VectorStoreUnavailableError(marker)

    store = QdrantScopedVectorStore(cast(Any, RaisingBackend()))
    node = PrecedentRecallNode(backend=ScopedVectorBackend(store))
    with caplog.at_level(logging.INFO):
        result = node(
            {
                "status": AgentStatus.PENDING.value,
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "tenant_id": "tenant-a",
                "ecn": {"body": "material substitution"},
                "trace_id": "trace-a",
                "correlation_id": "correlation-a",
                "session_id": "session-a",
            }
        )

    assert result["status"] == AgentStatus.ERROR.value
    assert "kb_unavailable" in json.dumps(result)
    assert marker not in json.dumps(result)
    assert str(REPO_ROOT) not in json.dumps(result)
    assert marker not in caplog.text


def test_configured_backend_receives_request_local_audit_state_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.api.server as server

    captured: dict[str, Any] = {}
    observed: dict[str, dict[str, Any]] = {}

    class CapturingBackend:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(server, "QdrantScopedBackend", CapturingBackend)
    server.wire_services(config=VECTOR_CONFIG, secret_provider=_Provider())

    def capture_invoke(instance: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        observed["audit"] = captured["audit_state_provider"]()
        observed["graph"] = instance._extra_initial_state()
        return {"status": AgentStatus.SUCCESS.value}

    auth = SimpleNamespace(
        principal={"caller_id": "caller-a"},
        auth_source=server.AuthSource.MIDDLEWARE,
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope={"tenant_id": "tenant-a"},
    )
    monkeypatch.setattr(server, "authenticate", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(server.SupplierECNImpactBriefAgent, "invoke", capture_invoke)
    try:
        asyncio.run(server.invoke(server.InvokeRequest(input="{}", session_id="session-a"), cast(Any, object())))
        assert observed["audit"] == observed["graph"]
        assert observed["audit"]["session_id"] == "session-a"
        assert observed["audit"]["trace_id"]
        assert observed["audit"]["correlation_id"]
        assert captured["audit_state_provider"]() == {}
    finally:
        asyncio.run(server.shutdown_services())


def test_runtime_is_closed_when_backend_construction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    runtimes: list[AsyncServiceRuntime] = []

    class TrackingRuntime(AsyncServiceRuntime):
        def __init__(self) -> None:
            super().__init__()
            runtimes.append(self)

    class FailingBackend:
        def __init__(self, **_kwargs: Any) -> None:
            raise RuntimeError("backend construction failed")

    asyncio.run(server.shutdown_services())
    monkeypatch.setattr(server, "AsyncServiceRuntime", TrackingRuntime)
    monkeypatch.setattr(server, "QdrantScopedBackend", FailingBackend)
    try:
        with pytest.raises(RuntimeError, match="backend construction failed"):
            server.wire_services(config=VECTOR_CONFIG, secret_provider=_Provider())
        assert runtimes[0]._thread is not None
        assert not runtimes[0]._thread.is_alive()
    finally:
        for runtime in runtimes:
            if runtime._thread is not None and runtime._thread.is_alive():
                asyncio.run(runtime.aclose(timeout=0.2))


def test_runtime_is_closed_when_agent_provisioning_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    runtimes: list[AsyncServiceRuntime] = []

    class TrackingRuntime(AsyncServiceRuntime):
        def __init__(self) -> None:
            super().__init__()
            runtimes.append(self)

    def fail_provision(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("provision failed")

    asyncio.run(server.shutdown_services())
    monkeypatch.setattr(server, "AsyncServiceRuntime", TrackingRuntime)
    monkeypatch.setattr(server.SupplierECNImpactBriefAgent, "provision_secrets", fail_provision)
    try:
        with pytest.raises(RuntimeError, match="provision failed"):
            server.wire_services(config=VECTOR_CONFIG, secret_provider=_Provider())
        assert runtimes[0]._thread is not None
        assert not runtimes[0]._thread.is_alive()
    finally:
        asyncio.run(server.shutdown_services())
        for runtime in runtimes:
            if runtime._thread is not None and runtime._thread.is_alive():
                asyncio.run(runtime.aclose(timeout=0.2))


def test_lifespan_startup_failure_closes_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    runtimes: list[AsyncServiceRuntime] = []

    class TrackingRuntime(AsyncServiceRuntime):
        def __init__(self) -> None:
            super().__init__()
            runtimes.append(self)

    def fail_provision(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("provision failed in lifespan")

    asyncio.run(server.shutdown_services())
    monkeypatch.setattr(server, "AsyncServiceRuntime", TrackingRuntime)
    monkeypatch.setattr(server.SupplierECNImpactBriefAgent, "provision_secrets", fail_provision)
    server.configure_app_startup(config=VECTOR_CONFIG, secret_provider=_Provider())
    try:
        from fastapi.testclient import TestClient

        with pytest.raises(RuntimeError, match="provision failed in lifespan"):
            with TestClient(server.app):
                pass
        assert runtimes[0]._thread is not None
        assert not runtimes[0]._thread.is_alive()
    finally:
        asyncio.run(server.shutdown_services())
        for runtime in runtimes:
            if runtime._thread is not None and runtime._thread.is_alive():
                asyncio.run(runtime.aclose(timeout=0.2))


def test_runtime_close_passes_timeout() -> None:
    import src.api.server as server

    class TimeoutRecordingRuntime:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        async def aclose(self, *, timeout: float | None) -> None:
            self.timeouts.append(timeout)

    runtime = TimeoutRecordingRuntime()
    server._close_runtime_sync(cast(Any, runtime))
    assert runtime.timeouts == [server.RUNTIME_CLOSE_TIMEOUT_S]


def test_runtime_closer_join_is_bounded(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import src.api.server as server

    class SlowRuntime:
        async def aclose(self, *, timeout: float | None) -> None:
            # Simulate a broken close implementation that blocks its event-loop
            # thread and therefore cannot be interrupted by asyncio.wait_for().
            time.sleep(0.2)

    monkeypatch.setattr(server, "RUNTIME_CLOSE_TIMEOUT_S", 1.0)
    monkeypatch.setattr(server, "RUNTIME_CLOSER_JOIN_TIMEOUT_S", 0.01)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        asyncio.run(_close_from_running_loop(server, SlowRuntime()))

    assert time.monotonic() - started < 0.1
    assert "runtime close thread exceeded" in caplog.text
    time.sleep(0.21)  # let the daemon test double finish before the next test


async def _close_from_running_loop(server: Any, runtime: Any) -> None:
    server._close_runtime_sync(runtime)


@pytest.mark.parametrize(
    "bad_vector_store",
    [
        None,
        {},
        {"url": "", "collection_name": "c", "embedding_base_url": "http://e"},
        {"url": "   ", "collection_name": "c", "embedding_base_url": "http://e"},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "vector_size": 0},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "timeout_s": "30"},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "timeout_s": float("nan")},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "distance": "angular"},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "distance": []},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "max_top_k": True},
        {"url": "http://q", "collection_name": "c", "embedding_base_url": "http://e", "unknown": "value"},
    ],
)
def test_present_invalid_vector_store_config_fails_without_secret(bad_vector_store: Any) -> None:
    import src.api.server as server

    with pytest.raises(ValueError, match="config.vector_store"):
        server.wire_services(
            config={"vector_store": bad_vector_store},
            secret_provider=_Provider(with_key=False),
        )


def test_absent_vector_store_section_still_degrades_without_secret() -> None:
    import src.api.server as server

    try:
        server.wire_services(config={}, secret_provider=_Provider(with_key=False))
        assert isinstance(server.active_services, ServiceBundle)
        assert server.active_services.vector_store is None
    finally:
        asyncio.run(server.shutdown_services())
