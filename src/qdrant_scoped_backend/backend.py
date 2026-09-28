# PART: qdrant-scoped-backend v0.1.0 (parts@541a0e3)
"""Scope-aware adapter for AgentCore's Qdrant vector store.

The SDK adapter can isolate only by ``(namespace, agent_name)``.  This
module folds the caller's trusted KB partition scope into ``agent_name``
with a length-prefixed canonical encoding, then composes a
``BaseVectorStore`` instance for that folded identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import AbstractSet, Any, TypeVar

from shared.services.runtime import AsyncServiceRuntime
from shared.services.vector_store.base_vector_store import (
    BaseVectorStore,
    VectorStoreTimeoutError,
    VectorStoreUnavailableError,
    build_isolation_tag,
    valid_caller_id,
)
from shared.utils.audit_logger import emit_trace_event


logger = logging.getLogger(__name__)

DEFAULT_METADATA_PASSTHROUGH = frozenset(
    {
        "record_id",
        "source_id",
        "source_tier",
        "source_doc",
        "chunk_hash",
        "kb_type",
        "category",
        "record_type",
        "published_date",
        "url",
        "title",
        "kind",
    }
)

StoreFactory = Callable[[dict[str, Any]], BaseVectorStore]
AuditStateProvider = Callable[[], dict[str, Any]]
CacheKey = tuple[str, str, str, str]
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class QdrantConnection:
    """Connection and embedding settings supplied by the template service.

    ``embedding_api_key`` is resolved by the caller with
    ``ctx.secrets.require()``.  It is excluded from ``repr`` and is used only
    to construct the SDK config and a one-way cache fingerprint.
    """

    url: str
    collection_name: str
    embedding_base_url: str
    embedding_api_key: str = field(repr=False)
    vector_size: int = 1536
    distance: str = "cosine"
    embedding_model: str = "text-embedding-ada-002"
    embedding_api_version: str = "2024-02-15-preview"
    embedding_provider: str = "azure"
    timeout_s: float = 30.0
    write_timeout_s: float = 60.0
    max_batch_documents: int = 1000
    max_top_k: int = 100

    def __post_init__(self) -> None:
        required = {
            "url": self.url,
            "collection_name": self.collection_name,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": self.embedding_api_key,
        }
        missing = [name for name, value in required.items() if not isinstance(value, str) or not value]
        if missing:
            raise ValueError(f"QdrantConnection: missing required values {missing}")

    def to_store_config(self) -> dict[str, Any]:
        """Return a fresh SDK config without namespace/agent identity."""

        return {
            "url": self.url,
            "collection_name": self.collection_name,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": self.embedding_api_key,
            "vector_size": self.vector_size,
            "distance": self.distance,
            "embedding_model": self.embedding_model,
            "embedding_api_version": self.embedding_api_version,
            "embedding_provider": self.embedding_provider,
            "timeout_s": self.timeout_s,
            "write_timeout_s": self.write_timeout_s,
            "max_batch_documents": self.max_batch_documents,
            "max_top_k": self.max_top_k,
        }

    def fingerprint(self) -> str:
        """Build an opaque cache identity; never expose raw credentials."""

        encoded = json.dumps(
            self.to_store_config(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def canonicalize_scope(scope: Mapping[str, str]) -> str:
    """Serialize scope exactly as the design's length-prefixed canonical form."""

    if not isinstance(scope, Mapping) or not scope:
        raise ValueError("QdrantScopedBackend: scope must be a non-empty mapping")
    items: list[tuple[str, str]] = []
    for key, value in scope.items():
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise ValueError("QdrantScopedBackend: scope keys must be non-empty identifier strings")
        if not _valid_scope_value(value):
            raise ValueError("QdrantScopedBackend: scope values must be clean non-empty strings")
        items.append((key, value))
    return "".join(f"{len(key)}:{key}={len(value)}:{value};" for key, value in sorted(items))


def fold_agent_name(agent_name: str, scope: Mapping[str, str]) -> str:
    """Fold one agent identity and one canonical scope without collisions."""

    if not isinstance(agent_name, str) or not agent_name:
        raise ValueError("QdrantScopedBackend: agent_name must be a non-empty string")
    return f"{len(agent_name)}:{agent_name}|{canonicalize_scope(scope)}"


def build_scoped_tag(namespace: str, agent_name: str, scope: Mapping[str, str]) -> str:
    """Return the exact isolation tag the composed SDK store will use."""

    if not isinstance(namespace, str) or not namespace:
        raise ValueError("QdrantScopedBackend: namespace must be a non-empty string")
    return build_isolation_tag(namespace, fold_agent_name(agent_name, scope))


def derive_caller_id_from_source_id(source_id: str) -> str:
    """Derive the SDK-facing canonical UUID from one stable business ID."""

    if not _valid_source_id(source_id):
        raise ValueError("QdrantScopedBackend: metadata source_id must be a clean non-empty string")
    return str(uuid.uuid5(uuid.NAMESPACE_OID, source_id))


@dataclass
class _CacheEntry:
    store: BaseVectorStore
    leases: int = 0
    retired: bool = False


class _StoreLease:
    def __init__(self, cache: _StoreCache, entry: _CacheEntry) -> None:
        self._cache = cache
        self._entry = entry

    def __enter__(self) -> BaseVectorStore:
        return self._entry.store

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._cache.release(self._entry)


class _StoreCache:
    """Bounded thread-safe LRU whose evicted stores are always closed."""

    def __init__(self, max_size: int) -> None:
        if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size <= 0:
            raise ValueError("QdrantScopedBackend: cache_size must be a positive integer")
        self._max_size = max_size
        self._entries: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()
        self._lock = Lock()
        self._closed = False

    def acquire(self, key: CacheKey, create: Callable[[], BaseVectorStore]) -> _StoreLease:
        evicted_store: BaseVectorStore | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("QdrantScopedBackend is closed")
            entry = self._entries.get(key)
            if entry is None:
                entry = _CacheEntry(store=create())
                self._entries[key] = entry
                if len(self._entries) > self._max_size:
                    _, evicted = self._entries.popitem(last=False)
                    evicted.retired = True
                    if evicted.leases == 0:
                        evicted_store = evicted.store
            else:
                self._entries.move_to_end(key)
            entry.leases += 1
            lease = _StoreLease(self, entry)
        if evicted_store is not None:
            self._close_evicted(evicted_store)
        return lease

    def release(self, entry: _CacheEntry) -> None:
        evicted_store: BaseVectorStore | None = None
        with self._lock:
            entry.leases -= 1
            if entry.leases < 0:
                raise RuntimeError("QdrantScopedBackend cache lease underflow")
            if entry.retired and entry.leases == 0:
                evicted_store = entry.store
        if evicted_store is not None:
            self._close_evicted(evicted_store)

    def close_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.retired = True
            stores = [entry.store for entry in entries if entry.leases == 0]
        failures: list[BaseException] = []
        for index, store in enumerate(stores):
            try:
                store.close()
            except BaseException as exc:  # close every store before propagating
                logger.error(
                    "QdrantScopedBackend: shutdown store close failed store_index=%d error_type=%s",
                    index,
                    type(exc).__name__,
                )
                failures.append(exc)
        if failures:
            raise failures[0]

    @staticmethod
    def _close_evicted(store: BaseVectorStore) -> None:
        try:
            store.close()
        except Exception as exc:
            logger.error(
                "QdrantScopedBackend: evicted store close failed error_type=%s",
                type(exc).__name__,
            )


class QdrantScopedBackend:
    """Compose per-scope ``BaseVectorStore`` instances for scoped retrieval."""

    def __init__(
        self,
        *,
        namespace: str,
        agent_name: str,
        connection: QdrantConnection,
        runtime: AsyncServiceRuntime,
        snippet_field: str = "snippet",
        snippet_chars: int = 400,
        metadata_passthrough: AbstractSet[str] = DEFAULT_METADATA_PASSTHROUGH,
        cache_size: int = 32,
        store_factory: StoreFactory | None = None,
        audit_state_provider: AuditStateProvider | None = None,
    ) -> None:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("QdrantScopedBackend: namespace must be a non-empty string")
        if not isinstance(agent_name, str) or not agent_name:
            raise ValueError("QdrantScopedBackend: agent_name must be a non-empty string")
        if not isinstance(snippet_field, str) or not snippet_field or not snippet_field.isidentifier():
            raise ValueError("QdrantScopedBackend: snippet_field must be an identifier string")
        if not isinstance(snippet_chars, int) or isinstance(snippet_chars, bool) or snippet_chars <= 0:
            raise ValueError("QdrantScopedBackend: snippet_chars must be a positive integer")
        passthrough = frozenset(metadata_passthrough)
        if any(not isinstance(key, str) or not key for key in passthrough):
            raise ValueError("QdrantScopedBackend: metadata_passthrough must contain non-empty strings")
        if audit_state_provider is not None and not callable(audit_state_provider):
            raise ValueError("QdrantScopedBackend: audit_state_provider must be callable")

        self._namespace = namespace
        self._agent_name = agent_name
        self._connection = connection
        self._snippet_field = snippet_field
        self._snippet_chars = snippet_chars
        self._metadata_passthrough = passthrough
        self._cache = _StoreCache(cache_size)
        self._audit_state_provider = audit_state_provider
        self._store_factory = store_factory or (
            lambda config: _create_default_store(config, runtime=runtime)
        )

    def search(
        self,
        *,
        scope: Mapping[str, str],
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Search one folded scope and map SDK hits to RetrievalBackend hits."""

        bound_scope = _validated_scope(scope)
        return self._with_store(
            "search",
            bound_scope,
            lambda store: [self._map_hit(hit, bound_scope) for hit in store.search(query, top_k)],
        )

    def upsert(self, *, scope: Mapping[str, str], documents: list[dict[str, Any]]) -> None:
        """Stamp trusted scope and a bounded snippet into every stored payload."""

        bound_scope = _validated_scope(scope)
        if not isinstance(documents, list):
            raise ValueError("QdrantScopedBackend: documents must be a list")
        prepared: list[dict[str, Any]] = []
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("QdrantScopedBackend: each document must be a dict")
            content = document.get("content")
            if not isinstance(content, str) or not content:
                raise ValueError("QdrantScopedBackend: document content must be a non-empty string")
            metadata = document.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("QdrantScopedBackend: document metadata must be a dict")
            source_id = metadata.get("source_id")
            if not _valid_source_id(source_id):
                raise ValueError(
                    "QdrantScopedBackend: metadata source_id must be a clean non-empty string"
                )
            conflicts = sorted(set(metadata) & set(bound_scope))
            if conflicts:
                raise ValueError(
                    "QdrantScopedBackend: caller metadata must not contain trusted scope keys "
                    f"{conflicts}"
                )
            if self._snippet_field in metadata:
                raise ValueError(
                    "QdrantScopedBackend: caller metadata must not contain snippet_field "
                    f"{self._snippet_field!r}"
                )
            caller_id = document.get("id")
            if caller_id is None:
                caller_id = _normalize_caller_id(source_id)
            prepared.append(
                {
                    **document,
                    "id": caller_id,
                    "metadata": {
                        **metadata,
                        **bound_scope,
                        self._snippet_field: content[: self._snippet_chars],
                    },
                }
            )
        self._with_store("upsert", bound_scope, lambda store: store.upsert(prepared))

    def delete(self, *, scope: Mapping[str, str], ids: list[Any]) -> None:
        """Delete business source IDs or SDK caller IDs in the folded scope."""

        normalized_ids: Any = ids
        if isinstance(ids, list):
            normalized_ids = [_normalize_caller_id(value) for value in ids]
        self._with_store("delete", scope, lambda store: store.delete(normalized_ids))

    def close(self) -> None:
        """Close all cached stores; repeated calls are harmless."""

        self._cache.close_all()

    def _with_store(
        self,
        operation: str,
        scope: Mapping[str, str],
        call: Callable[[BaseVectorStore], ResultT],
    ) -> ResultT:
        bound_scope = _validated_scope(scope)
        if self._snippet_field in bound_scope:
            raise ValueError("QdrantScopedBackend: snippet_field must not be a scope key")
        canon = canonicalize_scope(bound_scope)
        key = (self._namespace, self._agent_name, canon, self._connection.fingerprint())
        config = self._connection.to_store_config()
        config.update(
            {
                "namespace": self._namespace,
                "agent_name": fold_agent_name(self._agent_name, bound_scope),
            }
        )
        try:
            with self._cache.acquire(key, lambda: self._store_factory(config)) as store:
                return call(store)
        except (VectorStoreUnavailableError, VectorStoreTimeoutError) as exc:
            self._audit_operational_failure(operation, bound_scope, exc)
            raise

    def _map_hit(self, hit: dict[str, Any], requested_scope: Mapping[str, str]) -> dict[str, Any]:
        if not isinstance(hit, dict):
            raise ValueError("QdrantScopedBackend: vector store hit must be a dict")
        payload_value = hit.get("payload")
        payload = dict(payload_value) if isinstance(payload_value, dict) else {}
        source_id = payload.get("source_id") or hit.get("id")
        mapped: dict[str, Any] = {
            "id": source_id,
            "source_id": source_id,
            "score": hit.get("score"),
        }
        for key in requested_scope:
            if key in payload:
                mapped[key] = payload[key]
        if self._snippet_field in payload:
            mapped["snippet"] = payload.pop(self._snippet_field)
        mapped["metadata"] = {
            key: value
            for key, value in payload.items()
            if key in self._metadata_passthrough and key not in requested_scope
        }
        return mapped

    def _audit_operational_failure(
        self,
        operation: str,
        scope: Mapping[str, str],
        exc: VectorStoreUnavailableError | VectorStoreTimeoutError,
    ) -> None:
        audit_state: dict[str, Any] = {}
        if self._audit_state_provider is not None:
            try:
                provided_state = self._audit_state_provider()
                if isinstance(provided_state, dict):
                    audit_state = provided_state
                else:
                    logger.error(
                        "QdrantScopedBackend: audit_state_provider returned invalid type=%s",
                        type(provided_state).__name__,
                    )
            except Exception as provider_error:
                logger.error(
                    "QdrantScopedBackend: audit_state_provider failed error_type=%s",
                    type(provider_error).__name__,
                )
        try:
            emit_trace_event(
                "qdrant_scoped_backend_failure",
                {
                    "operation": operation,
                    "error_type": type(exc).__name__,
                    "scope_keys": sorted(scope),
                },
                audit_state,
            )
        except Exception as audit_error:  # audit failure must not replace the provider error
            logger.error(
                "QdrantScopedBackend: failed to emit operational failure audit event error_type=%s",
                type(audit_error).__name__,
            )


def _create_default_store(
    config: dict[str, Any],
    *,
    runtime: AsyncServiceRuntime,
) -> BaseVectorStore:
    """Import the optional Qdrant adapter only when a store is first needed."""

    from shared.services.vector_store.qdrant_vector_store import QdrantVectorStore

    return QdrantVectorStore(config, runtime=runtime)


def _validated_scope(scope: Mapping[str, str]) -> dict[str, str]:
    canonicalize_scope(scope)
    return dict(scope)


def _valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _valid_source_id(value: Any) -> bool:
    return _valid_scope_value(value)


def _normalize_caller_id(value: Any) -> Any:
    """Apply the same source-ID-to-caller-ID rule on write and delete."""

    if valid_caller_id(value):
        return value
    if _valid_source_id(value):
        return derive_caller_id_from_source_id(value)
    return value
