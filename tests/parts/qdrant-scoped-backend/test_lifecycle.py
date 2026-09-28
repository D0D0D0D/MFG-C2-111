from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from qdrant_scoped_backend import QdrantConnection, QdrantScopedBackend
from shared.services.vector_store.base_vector_store import BaseVectorStore


class Store(BaseVectorStore):
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.closed = 0
        self.close_error: Exception | None = None

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        return []

    def upsert(self, documents: list) -> None:
        return None

    def delete(self, ids: list) -> None:
        return None

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class Factory:
    def __init__(self) -> None:
        self.stores: list[Store] = []

    def __call__(self, config: dict[str, Any]) -> BaseVectorStore:
        store = Store(config)
        self.stores.append(store)
        return store


def make_backend(factory: Factory, *, cache_size: int = 32) -> QdrantScopedBackend:
    return QdrantScopedBackend(
        namespace="customer-a",
        agent_name="AG-A",
        connection=QdrantConnection(
            url="http://qdrant.invalid:6333",
            collection_name="documents",
            embedding_base_url="https://embedding.invalid",
            embedding_api_key="test-only-secret",
        ),
        runtime=object(),
        cache_size=cache_size,
        store_factory=factory,
    )


def search(service: QdrantScopedBackend, partition: str) -> None:
    service.search(scope={"partition_id": partition}, query="query", top_k=1)


def test_lru_evicts_and_closes_oldest_then_close_closes_all_remaining() -> None:
    factory = Factory()
    service = make_backend(factory, cache_size=2)

    search(service, "a")
    search(service, "b")
    search(service, "a")  # make b the least-recently-used entry
    search(service, "c")

    assert len(factory.stores) == 3
    assert factory.stores[0].closed == 0
    assert factory.stores[1].closed == 1
    assert factory.stores[2].closed == 0

    service.close()

    assert [store.closed for store in factory.stores] == [1, 1, 1]
    service.close()
    assert [store.closed for store in factory.stores] == [1, 1, 1]


def test_cache_key_includes_scope_and_reuses_only_same_canonical_scope() -> None:
    factory = Factory()
    service = make_backend(factory)

    service.search(scope={"company_id": "co", "project_id": "a"}, query="q", top_k=1)
    service.search(scope={"project_id": "a", "company_id": "co"}, query="q", top_k=1)
    service.search(scope={"company_id": "co", "project_id": "b"}, query="q", top_k=1)

    assert len(factory.stores) == 2
    assert factory.stores[0] is not factory.stores[1]
    assert factory.stores[0].config["agent_name"] != factory.stores[1].config["agent_name"]


def test_concurrent_first_use_creates_one_store_for_one_scope() -> None:
    factory = Factory()
    service = make_backend(factory)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: search(service, "same"), range(32)))

    assert len(factory.stores) == 1
    service.close()
    assert factory.stores[0].closed == 1


def test_evict_close_failure_is_logged_without_failing_acquire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = Factory()
    service = make_backend(factory, cache_size=1)
    search(service, "a")
    factory.stores[0].close_error = RuntimeError("evict-close-failure")

    with caplog.at_level(logging.ERROR, logger="qdrant_scoped_backend.backend"):
        search(service, "b")

    assert len(factory.stores) == 2
    assert factory.stores[0].closed == 1
    assert any("evicted store close failed" in record.message for record in caplog.records)


def test_close_all_logs_every_store_close_failure(caplog: pytest.LogCaptureFixture) -> None:
    factory = Factory()
    service = make_backend(factory, cache_size=2)
    search(service, "a")
    search(service, "b")
    factory.stores[0].close_error = RuntimeError("close-a")
    factory.stores[1].close_error = RuntimeError("close-b")

    with caplog.at_level(logging.ERROR, logger="qdrant_scoped_backend.backend"):
        with pytest.raises(RuntimeError, match="close-a"):
            service.close()

    failures = [record for record in caplog.records if "shutdown store close failed" in record.message]
    assert len(failures) == 2
    assert "store_index=0" in failures[0].message
    assert "store_index=1" in failures[1].message
