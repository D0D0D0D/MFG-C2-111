"""Production wiring and real-Qdrant checks for the scoped KB backend."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from shared.secrets import InMemoryProvider
from shared.services.runtime import AsyncServiceRuntime
from src.qdrant_scoped_backend import QdrantConnection, QdrantScopedBackend
from src.services.service import QdrantScopedVectorStore, ServiceBundle


_QDRANT_URL = "http://localhost:6333"
_VECTOR_CONFIG: dict[str, Any] = {
    "memory_enabled": True,
    "hitl": {"enabled": True, "max_hitl": 8},
    "vector_store": {
        "url": _QDRANT_URL,
        "collection_name": "mfg_c2_111_wiring_test",
        "embedding_base_url": "http://embedding.invalid",
        "vector_size": 8,
    },
}


def _provider(*, with_embedding_key: bool) -> InMemoryProvider:
    values = {"AZURE_EMBEDDING_API_KEY": "test-only-key"} if with_embedding_key else {}
    return InMemoryProvider(values, namespace="mfg", agent_name="SupplierECNImpactBriefAgent")


class _CountingBackend:
    instances: list[_CountingBackend] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.close_calls = 0
        self.__class__.instances.append(self)

    def close(self) -> None:
        self.close_calls += 1


class _CountingRuntime:
    instances: list[_CountingRuntime] = []

    def __init__(self) -> None:
        self.aclose_calls = 0
        self.__class__.instances.append(self)

    async def aclose(self, *, timeout: float | None = None) -> None:
        self.aclose_calls += 1


def _install_counting_services(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    _CountingBackend.instances.clear()
    _CountingRuntime.instances.clear()
    monkeypatch.setattr(server, "QdrantScopedBackend", _CountingBackend)
    monkeypatch.setattr(server, "AsyncServiceRuntime", _CountingRuntime)


def test_wire_services_closes_owned_backend_and_runtime_before_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.api.server as server

    asyncio.run(server.shutdown_services())
    _install_counting_services(monkeypatch)
    try:
        server.wire_services(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))
        first_backend = _CountingBackend.instances[0]
        first_runtime = _CountingRuntime.instances[0]

        server.wire_services(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))

        assert first_backend.close_calls == 1
        assert first_runtime.aclose_calls == 1
    finally:
        asyncio.run(server.shutdown_services())


def test_lifespan_does_not_rewire_module_initialized_services(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    asyncio.run(server.shutdown_services())
    _install_counting_services(monkeypatch)
    monkeypatch.setattr(server, "load_config", lambda _path: dict(_VECTOR_CONFIG))
    monkeypatch.setattr(
        server,
        "secrets_factory",
        lambda **_kwargs: _provider(with_embedding_key=True),
    )
    server.wire_services(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))

    with TestClient(server.app):
        assert len(_CountingBackend.instances) == 1
        assert len(_CountingRuntime.instances) == 1

    assert [backend.close_calls for backend in _CountingBackend.instances] == [1]
    assert [runtime.aclose_calls for runtime in _CountingRuntime.instances] == [1]


def test_lifespan_override_closes_every_owned_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.api.server as server

    asyncio.run(server.shutdown_services())
    _install_counting_services(monkeypatch)
    server.wire_services(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))
    server.configure_app_startup(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))

    with TestClient(server.app):
        assert len(_CountingBackend.instances) == 2
        assert len(_CountingRuntime.instances) == 2

    assert [backend.close_calls for backend in _CountingBackend.instances] == [1, 1]
    assert [runtime.aclose_calls for runtime in _CountingRuntime.instances] == [1, 1]


def test_rewire_never_closes_injected_vector_store() -> None:
    import src.api.server as server

    class InjectedStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def search_scoped(self, *, scope: dict[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
            return []

        def close(self) -> None:
            self.close_calls += 1

    asyncio.run(server.shutdown_services())
    injected = InjectedStore()
    server.wire_services(services=ServiceBundle(vector_store=injected), config={})
    try:
        server.wire_services(config={})
        assert injected.close_calls == 0
    finally:
        asyncio.run(server.shutdown_services())


def test_startup_wires_configured_qdrant_into_service_bundle() -> None:
    import src.api.server as server

    server.configure_app_startup(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=True))
    with TestClient(server.app):
        vector_store = server.active_services.vector_store
        assert isinstance(vector_store, QdrantScopedVectorStore)

    with pytest.raises(RuntimeError, match="closed"):
        vector_store.search_scoped(
            scope={"tenant_id": "tenant-a"},
            query="material substitution",
            top_k=5,
        )


def test_default_startup_loads_committed_runtime_config() -> None:
    import src.api.server as server

    with TestClient(server.app):
        assert server.agent.config["budget_usd"] == 1.5
        assert server.agent.config["hitl"] == {"enabled": True, "max_hitl": 8}


def test_startup_without_vector_store_preserves_empty_retrieval_degradation() -> None:
    import src.api.server as server

    config = {"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}}
    server.configure_app_startup(config=config, secret_provider=_provider(with_embedding_key=True))
    with TestClient(server.app):
        assert server.active_services.vector_store is None
        assert (
            server.active_services.retrieval_backend.search(
                scope={"tenant_id": "tenant-a"},
                query="material substitution",
                top_k=5,
            )
            == []
        )


def test_startup_without_embedding_secret_preserves_empty_retrieval_degradation() -> None:
    import src.api.server as server

    server.configure_app_startup(config=_VECTOR_CONFIG, secret_provider=_provider(with_embedding_key=False))
    with TestClient(server.app):
        assert server.active_services.vector_store is None


def _require_qdrant() -> None:
    try:
        with urlopen(f"{_QDRANT_URL}/collections", timeout=1.0) as response:
            if response.status != 200:
                pytest.skip(f"Qdrant returned HTTP {response.status}")
    except (OSError, URLError) as exc:
        pytest.skip(f"Qdrant is not reachable at {_QDRANT_URL}: {type(exc).__name__}")


def test_real_qdrant_returns_only_the_requested_stored_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_qdrant()
    import agenticstar_platform.rag.embedding as embedding_module
    from qdrant_client import QdrantClient

    dimensions = 8

    class DeterministicEmbeddingGenerator:
        def __init__(self, config: Any) -> None:
            self.dimensions = getattr(config, "dimensions", dimensions)
            self.model_name = "deterministic-test-embedding"

        def _vector(self, text: str) -> list[float]:
            digest = hashlib.sha256(text.encode()).digest()
            return [((digest[index] / 255.0) * 2) - 1 for index in range(self.dimensions)]

        async def generate(self, text: str) -> list[float]:
            return self._vector(text)

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            return [self._vector(text) for text in texts]

    monkeypatch.setattr(embedding_module, "EmbeddingGenerator", DeterministicEmbeddingGenerator)
    collection_name = f"mfg_c2_111_scope_{uuid.uuid4().hex}"
    runtime = AsyncServiceRuntime()
    backend = QdrantScopedBackend(
        namespace="mfg",
        agent_name="SupplierECNImpactBriefAgent",
        connection=QdrantConnection(
            url=_QDRANT_URL,
            collection_name=collection_name,
            embedding_base_url="http://embedding.invalid",
            embedding_api_key="test-only-key",
            vector_size=dimensions,
        ),
        runtime=runtime,
    )
    store = QdrantScopedVectorStore(backend)
    scope_a = {"tenant_id": "tenant-a"}
    scope_b = {"tenant_id": "tenant-b"}
    content = "torque specification deviation on bracket"
    try:
        backend.upsert(
            scope=scope_a,
            documents=[{"content": content, "metadata": {"source_id": "scope-a-record"}}],
        )
        backend.upsert(
            scope=scope_b,
            documents=[{"content": content, "metadata": {"source_id": "scope-b-record"}}],
        )

        hits = store.search_scoped(scope=scope_a, query=content, top_k=10)

        assert [hit["source_id"] for hit in hits] == ["scope-a-record"]
        assert all(hit.get("tenant_id") == "tenant-a" for hit in hits)
    finally:
        backend.close()
        asyncio.run(runtime.aclose())
        client = QdrantClient(url=_QDRANT_URL)
        if client.collection_exists(collection_name):
            client.delete_collection(collection_name)
