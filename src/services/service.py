"""Small, injectable adapters for the ECN domain service boundaries."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from shared.services.database.base_db import BaseDatabase
from shared.services.llm.base_llm import BaseLLM
from shared.services.vector_store.base_vector_store import (
    BaseVectorStore,
    VectorStoreTimeoutError,
    VectorStoreUnavailableError,
    VectorStoreValidationError,
)
from src.qdrant_scoped_backend import QdrantScopedBackend


_qdrant_audit_state: ContextVar[dict[str, Any] | None] = ContextVar(
    "qdrant_audit_state",
    default=None,
)


class KbUnavailableError(RuntimeError):
    """A stable, non-provider-derived KB operational failure."""


class KbValidationError(ValueError):
    """A stable, non-provider-derived KB request/configuration failure."""


@contextmanager
def bind_qdrant_audit_state(state: Mapping[str, Any]) -> Iterator[None]:
    """Bind safe request correlation fields for Qdrant operational audits."""

    audit_state = {
        key: value
        for key in ("trace_id", "correlation_id", "session_id")
        if isinstance((value := state.get(key)), str) and value
    }
    token = _qdrant_audit_state.set(audit_state)
    try:
        yield
    finally:
        _qdrant_audit_state.reset(token)


def current_qdrant_audit_state() -> dict[str, Any]:
    """Return an invocation-local snapshot suitable for ``emit_trace_event``."""

    return dict(_qdrant_audit_state.get() or {})


def normalize_part_id(value: Any) -> str:
    """Return the common register key used by both the ECN and DB write path."""
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalize_ecn_envelope(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Normalise supplied parts without inferring missing identifiers."""
    ecn = dict(raw)
    normalized: list[dict[str, Any]] = []
    flags: list[str] = []
    seen: set[str] = set()
    parts = raw.get("parts", [])
    if not isinstance(parts, list) or not parts:
        return ecn, normalized, ["parts_missing"]
    for index, item in enumerate(parts):
        original = item.get("part_id") if isinstance(item, dict) else item
        key = normalize_part_id(original)
        if not key:
            flags.append(f"part_missing:{index}")
            continue
        if key in seen:
            flags.append(f"part_duplicate:{key}")
            continue
        seen.add(key)
        normalized.append({"part_key": key, "original_part_id": str(original), "normalization": "nfkc_casefold"})
    return ecn, normalized, flags


class ScopedVectorBackend:
    """Adapt ``BaseVectorStore`` to the scoped-retrieval part's backend port.

    The SDK's plain ``search()`` API cannot express a scope predicate and is
    therefore never called here. Deployments must provide ``search_scoped``;
    it filters on every scope key server-side and projects each stored row's
    scope into the hit. The retrieval part independently verifies that scope.
    """

    def __init__(self, vector_store: BaseVectorStore | ScopedVectorStore | None) -> None:
        self._vector_store = vector_store

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        if not scope or not all(isinstance(value, str) and value for value in scope.values()):
            return []
        if self._vector_store is None:
            return []
        scoped_search = getattr(self._vector_store, "search_scoped", None)
        if not callable(scoped_search):
            return []
        return scoped_search(scope=dict(scope), query=query, top_k=top_k)


class ScopedVectorStore(Protocol):
    """Deployment seam that enforces and returns stored-record scope."""

    def search_scoped(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]: ...


class QdrantScopedVectorStore:
    """Expose ``QdrantScopedBackend`` through the deployment store protocol."""

    def __init__(self, backend: QdrantScopedBackend) -> None:
        self._backend = backend

    def search_scoped(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        try:
            return self._backend.search(scope=scope, query=query, top_k=top_k)
        except (VectorStoreUnavailableError, VectorStoreTimeoutError):
            # ``BaseNode`` persists both str(exc) and traceback.format_exc().
            # Suppress chaining so provider text cannot re-enter through the
            # traceback even though the client-visible category is normalized.
            raise KbUnavailableError("kb_unavailable") from None
        except VectorStoreValidationError:
            raise KbValidationError("kb_invalid_request") from None


@dataclass(frozen=True)
class ServiceBundle:
    """Runtime injection seam; concrete external clients are supplied by deployment."""

    db: BaseDatabase | None = None
    vector_store: BaseVectorStore | ScopedVectorStore | None = None
    llm: BaseLLM | None = None

    @property
    def retrieval_backend(self) -> ScopedVectorBackend:
        return ScopedVectorBackend(self.vector_store)
