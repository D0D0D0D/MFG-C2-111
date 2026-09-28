from __future__ import annotations

import importlib.util
import inspect
import uuid
from typing import Any

import pytest

import qdrant_scoped_backend.backend as backend_module
from qdrant_scoped_backend import (
    QdrantConnection,
    QdrantScopedBackend,
    derive_caller_id_from_source_id,
)
from shared.services.vector_store.base_vector_store import (
    BaseVectorStore,
    VectorStoreTimeoutError,
    VectorStoreUnavailableError,
    VectorStoreValidationError,
    valid_caller_id,
)


SCOPE = {"company_id": "company-a", "project_id": "project-requested"}


class FakeQdrantVectorStore(BaseVectorStore):
    """Test double with the SDK adapter's exact public hit shape."""

    def __init__(self, config: dict[str, Any], *, hits: list[dict[str, Any]] | None = None) -> None:
        self.config = dict(config)
        self.hits = list(hits or [])
        self.upserted: list[dict[str, Any]] | None = None
        self.deleted: list[Any] | None = None
        self.closed = 0
        self.search_error: Exception | None = None

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        if self.search_error is not None:
            raise self.search_error
        return self.hits[:top_k]

    def upsert(self, documents: list) -> None:
        self.upserted = documents

    def delete(self, ids: list) -> None:
        self.deleted = ids

    def close(self) -> None:
        self.closed += 1


class Factory:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self.hits = hits
        self.stores: list[FakeQdrantVectorStore] = []

    def __call__(self, config: dict[str, Any]) -> BaseVectorStore:
        store = FakeQdrantVectorStore(config, hits=self.hits)
        self.stores.append(store)
        return store


class StatefulFakeQdrantVectorStore(FakeQdrantVectorStore):
    """In-memory double that enforces the real adapter's caller-ID contract."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.records: dict[int | str, dict[str, Any]] = {}

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        return [
            {
                "id": caller_id,
                "score": 0.91,
                "similarity": 0.955,
                "payload": dict(document["metadata"]),
            }
            for caller_id, document in list(self.records.items())[:top_k]
        ]

    def upsert(self, documents: list) -> None:
        for document in documents:
            caller_id = document["id"]
            if not valid_caller_id(caller_id):
                raise VectorStoreValidationError(f"invalid caller id: {caller_id!r}")
            self.records[caller_id] = document

    def delete(self, ids: list) -> None:
        for caller_id in ids:
            if not valid_caller_id(caller_id):
                raise VectorStoreValidationError(f"invalid caller id: {caller_id!r}")
            self.records.pop(caller_id, None)


class StatefulFactory(Factory):
    def __init__(self) -> None:
        super().__init__()
        self.stores: list[StatefulFakeQdrantVectorStore] = []

    def __call__(self, config: dict[str, Any]) -> BaseVectorStore:
        store = StatefulFakeQdrantVectorStore(config)
        self.stores.append(store)
        return store


def connection() -> QdrantConnection:
    return QdrantConnection(
        url="http://qdrant.invalid:6333",
        collection_name="documents",
        embedding_base_url="https://embedding.invalid",
        embedding_api_key="test-only-secret",
    )


def backend(factory: Factory, **kwargs: Any) -> QdrantScopedBackend:
    return QdrantScopedBackend(
        namespace="customer-a",
        agent_name="AG-A",
        connection=connection(),
        runtime=object(),
        store_factory=factory,
        **kwargs,
    )


def sdk_hit(*, payload: dict[str, Any], point_id: str = "point-1") -> dict[str, Any]:
    return {
        "id": point_id,
        "score": 0.91,
        "similarity": 0.955,
        "payload": payload,
    }


def test_search_returns_scope_from_stored_payload_not_requested_scope() -> None:
    factory = Factory(
        [
            sdk_hit(
                payload={
                    "source_id": "source-1",
                    "company_id": "company-a",
                    "project_id": "project-stored-different",
                    "snippet": "stored excerpt",
                }
            )
        ]
    )

    hits = backend(factory).search(scope=SCOPE, query="query", top_k=3)

    assert hits[0]["project_id"] == "project-stored-different"
    assert hits[0]["project_id"] != SCOPE["project_id"]


def test_missing_stored_scope_key_is_not_injected_into_hit() -> None:
    factory = Factory(
        [
            sdk_hit(
                payload={
                    "source_id": "source-1",
                    "company_id": "company-a",
                    "snippet": "stored excerpt",
                }
            )
        ]
    )

    hit = backend(factory).search(scope=SCOPE, query="query", top_k=3)[0]

    assert "project_id" not in hit


def test_snippet_is_lifted_to_top_level_and_removed_from_metadata() -> None:
    factory = Factory(
        [
            sdk_hit(
                payload={
                    "source_id": "source-1",
                    **SCOPE,
                    "snippet": "stored excerpt",
                    "title": "safe title",
                    "description": "must not pass",
                }
            )
        ]
    )

    hit = backend(factory).search(scope=SCOPE, query="query", top_k=3)[0]

    assert hit["snippet"] == "stored excerpt"
    assert "snippet" not in hit["metadata"]
    assert hit["metadata"] == {"source_id": "source-1", "title": "safe title"}


def test_upsert_stamps_trusted_scope_and_content_prefix() -> None:
    factory = Factory()
    service = backend(factory, snippet_chars=7)

    service.upsert(
        scope=SCOPE,
        documents=[
            {
                "id": 7,
                "content": "abcdefghijk",
                "metadata": {"source_id": "source-7", "title": "title"},
            }
        ],
    )

    stored = factory.stores[0].upserted
    assert stored is not None
    assert stored[0]["metadata"] == {
        "source_id": "source-7",
        "title": "title",
        **SCOPE,
        "snippet": "abcdefg",
    }


@pytest.mark.parametrize("metadata", [{}, {"source_id": ""}, {"source_id": " "}, {"source_id": 7}])
def test_upsert_requires_non_empty_business_source_id(metadata: dict[str, Any]) -> None:
    factory = Factory()

    with pytest.raises(ValueError, match="source_id"):
        backend(factory).upsert(
            scope=SCOPE,
            documents=[{"content": "body", "metadata": metadata}],
        )

    assert factory.stores == []


def test_business_source_id_derives_deterministic_sdk_id() -> None:
    source_id = "ECN-2024-001"

    assert derive_caller_id_from_source_id(source_id) == str(uuid.uuid5(uuid.NAMESPACE_OID, source_id))
    assert derive_caller_id_from_source_id(source_id) == derive_caller_id_from_source_id(source_id)


def test_same_business_source_id_reuses_same_sdk_id_on_upsert() -> None:
    factory = Factory()
    service = backend(factory)

    service.upsert(
        scope=SCOPE,
        documents=[{"content": "first", "metadata": {"source_id": "SDS-REV-12"}}],
    )
    first_id = factory.stores[0].upserted[0]["id"]
    service.upsert(
        scope=SCOPE,
        documents=[{"content": "second", "metadata": {"source_id": "SDS-REV-12"}}],
    )
    second_id = factory.stores[0].upserted[0]["id"]

    assert first_id == second_id
    assert first_id == str(uuid.uuid5(uuid.NAMESPACE_OID, "SDS-REV-12"))


def test_search_returns_business_source_id_from_stored_payload() -> None:
    factory = Factory(
        [
            sdk_hit(
                point_id="4f893ef4-b512-4ac0-95ac-73b876675ad9",
                payload={"source_id": "ECN-2024-001", **SCOPE, "snippet": "stored excerpt"},
            )
        ]
    )

    hit = backend(factory).search(scope=SCOPE, query="query", top_k=1)[0]

    assert hit["id"] == "ECN-2024-001"
    assert hit["source_id"] == "ECN-2024-001"


def test_delete_accepts_same_business_source_id_used_for_upsert() -> None:
    factory = StatefulFactory()
    service = backend(factory)
    service.upsert(
        scope=SCOPE,
        documents=[{"content": "body", "metadata": {"source_id": "ECN-A-1"}}],
    )

    service.delete(scope=SCOPE, ids=["ECN-A-1"])

    assert service.search(scope=SCOPE, query="body", top_k=10) == []


def test_search_business_source_id_can_be_passed_directly_to_delete() -> None:
    factory = StatefulFactory()
    service = backend(factory)
    service.upsert(
        scope=SCOPE,
        documents=[{"content": "body", "metadata": {"source_id": "SDS-REV-12"}}],
    )
    hit = service.search(scope=SCOPE, query="body", top_k=10)[0]

    service.delete(scope=SCOPE, ids=[hit["source_id"]])

    assert service.search(scope=SCOPE, query="body", top_k=10) == []


def test_delete_business_source_id_does_not_cross_scope() -> None:
    factory = StatefulFactory()
    service = backend(factory)
    scope_a = {"company_id": "company-a", "project_id": "project-a"}
    scope_b = {"company_id": "company-a", "project_id": "project-b"}
    document = {"content": "body", "metadata": {"source_id": "ECN-SHARED-1"}}
    service.upsert(scope=scope_a, documents=[document])
    service.upsert(scope=scope_b, documents=[document])

    service.delete(scope=scope_a, ids=["ECN-SHARED-1"])

    assert service.search(scope=scope_a, query="body", top_k=10) == []
    assert [hit["source_id"] for hit in service.search(scope=scope_b, query="body", top_k=10)] == [
        "ECN-SHARED-1"
    ]


def test_uuid_shaped_business_source_id_is_symmetric_for_upsert_and_delete() -> None:
    factory = StatefulFactory()
    service = backend(factory)
    source_id = "4f893ef4-b512-4ac0-95ac-73b876675ad9"
    service.upsert(
        scope=SCOPE,
        documents=[{"content": "body", "metadata": {"source_id": source_id}}],
    )

    service.delete(scope=SCOPE, ids=[source_id])

    assert service.search(scope=SCOPE, query="body", top_k=10) == []


def test_explicit_sdk_caller_id_is_preserved_for_upsert_and_delete() -> None:
    factory = StatefulFactory()
    service = backend(factory)
    caller_id = "f2dd650f-fef6-45b1-ad80-c8e004fa8143"
    service.upsert(
        scope=SCOPE,
        documents=[
            {
                "id": caller_id,
                "content": "body",
                "metadata": {"source_id": "ECN-EXPLICIT-1"},
            }
        ],
    )

    assert caller_id in factory.stores[0].records
    service.delete(scope=SCOPE, ids=[caller_id])
    assert service.search(scope=SCOPE, query="body", top_k=10) == []


@pytest.mark.parametrize("scope_key", ["company_id", "project_id"])
def test_upsert_rejects_caller_metadata_scope_keys(scope_key: str) -> None:
    factory = Factory()

    with pytest.raises(ValueError, match="trusted scope keys"):
        backend(factory).upsert(
            scope=SCOPE,
            documents=[
                {
                    "id": 7,
                    "content": "body",
                    "metadata": {"source_id": "source-7", scope_key: "spoofed"},
                }
            ],
        )

    assert factory.stores == []


def test_upsert_rejects_caller_metadata_snippet_field() -> None:
    factory = Factory()

    with pytest.raises(ValueError, match="snippet_field"):
        backend(factory).upsert(
            scope=SCOPE,
            documents=[
                {
                    "content": "generated body",
                    "metadata": {"source_id": "source-7", "snippet": "caller snippet"},
                }
            ],
        )

    assert factory.stores == []


def test_snippet_field_cannot_shadow_a_trusted_scope_key() -> None:
    factory = Factory()

    with pytest.raises(ValueError, match="snippet_field must not be a scope key"):
        backend(factory).upsert(
            scope={"snippet": "partition-a"},
            documents=[{"id": 7, "content": "body", "metadata": {"source_id": "source-7"}}],
        )

    assert factory.stores == []


@pytest.mark.parametrize("error_type", [VectorStoreUnavailableError, VectorStoreTimeoutError])
def test_operational_errors_are_audited_once_and_re_raised(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    factory = Factory()
    service = backend(factory)
    service.search(scope=SCOPE, query="warm cache", top_k=1)
    error = error_type("provider detail")
    factory.stores[0].search_error = error
    monkeypatch.setattr(
        backend_module,
        "emit_trace_event",
        lambda event, payload, state: events.append((event, payload, state)),
    )

    with pytest.raises(error_type) as raised:
        service.search(scope=SCOPE, query="query", top_k=1)

    assert raised.value is error
    assert events == [
        (
            "qdrant_scoped_backend_failure",
            {
                "operation": "search",
                "error_type": error_type.__name__,
                "scope_keys": ["company_id", "project_id"],
            },
            {},
        )
    ]
    assert "provider detail" not in str(events)


def test_operational_error_uses_audit_state_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    audit_state = {
        "trace_id": "trace-17",
        "correlation_id": "corr-17",
        "session_id": "session-17",
    }
    factory = Factory()
    service = backend(factory, audit_state_provider=lambda: audit_state)
    service.search(scope=SCOPE, query="warm cache", top_k=1)
    factory.stores[0].search_error = VectorStoreUnavailableError("provider detail")
    monkeypatch.setattr(
        backend_module,
        "emit_trace_event",
        lambda event, payload, state: events.append((event, payload, state)),
    )

    with pytest.raises(VectorStoreUnavailableError):
        service.search(scope=SCOPE, query="query", top_k=1)

    assert len(events) == 1
    assert events[0][2] is audit_state


def test_audit_state_provider_failure_does_not_replace_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    factory = Factory()

    def failing_provider() -> dict[str, Any]:
        raise RuntimeError("audit state unavailable")

    service = backend(factory, audit_state_provider=failing_provider)
    service.search(scope=SCOPE, query="warm cache", top_k=1)
    provider_error = VectorStoreTimeoutError("provider timeout")
    factory.stores[0].search_error = provider_error
    monkeypatch.setattr(
        backend_module,
        "emit_trace_event",
        lambda event, payload, state: events.append((event, payload, state)),
    )

    with pytest.raises(VectorStoreTimeoutError) as raised:
        service.search(scope=SCOPE, query="query", top_k=1)

    assert raised.value is provider_error
    assert len(events) == 1
    assert events[0][2] == {}


def test_validation_error_is_not_relabelled_or_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    factory = Factory()
    service = backend(factory)
    service.search(scope=SCOPE, query="warm cache", top_k=1)
    error = VectorStoreValidationError("bad query")
    factory.stores[0].search_error = error
    monkeypatch.setattr(backend_module, "emit_trace_event", lambda *args: events.append(args))

    with pytest.raises(VectorStoreValidationError) as raised:
        service.search(scope=SCOPE, query="query", top_k=1)

    assert raised.value is error
    assert events == []


def test_real_sdk_qdrant_store_method_signatures() -> None:
    module_name = "shared.services.vector_store.qdrant_vector_store"
    assert importlib.util.find_spec(module_name) is not None
    from shared.services.vector_store.qdrant_vector_store import QdrantVectorStore

    expected = {
        "search": ["self", "query", "top_k"],
        "upsert": ["self", "documents"],
        "delete": ["self", "ids"],
        "close": ["self"],
    }
    for method_name, parameter_names in expected.items():
        signature = inspect.signature(getattr(QdrantVectorStore, method_name))
        assert list(signature.parameters) == parameter_names


def test_module_has_no_level_zero_import() -> None:
    source = inspect.getsource(backend_module)

    assert "from agenticstar_platform" not in source
    assert "import agenticstar_platform" not in source
    assert "from agenticstar " not in source
    assert "import agenticstar " not in source
