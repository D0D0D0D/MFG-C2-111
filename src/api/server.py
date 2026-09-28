"""HTTP adapter: authenticate callers, establish verified scope, invoke graph."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.secrets.base import SecretProvider
from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory
from shared.services.runtime import AsyncServiceRuntime
from src.entry_adapter import AuthSource, Operation, authenticate
from src.graph.graph import SupplierECNImpactBriefAgent
from src.qdrant_scoped_backend import QdrantConnection, QdrantScopedBackend
from src.services.service import (
    QdrantScopedVectorStore,
    ServiceBundle,
    bind_qdrant_audit_state,
    current_qdrant_audit_state,
)


_NAMESPACE = "mfg"
_AGENT_NAME = "SupplierECNImpactBriefAgent"
_EMBEDDING_API_KEY_SECRET = "AZURE_EMBEDDING_API_KEY"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
RUNTIME_CLOSE_TIMEOUT_S = 5.0
RUNTIME_CLOSER_JOIN_TIMEOUT_S = 6.0
_VECTOR_STORE_KEYS = frozenset(
    {
        "url",
        "collection_name",
        "embedding_base_url",
        "vector_size",
        "distance",
        "embedding_model",
        "embedding_api_version",
        "embedding_provider",
        "timeout_s",
        "write_timeout_s",
        "max_batch_documents",
        "max_top_k",
        "cache_size",
        "snippet_field",
        "snippet_chars",
    }
)
_logger = logging.getLogger(__name__)

agent: SupplierECNImpactBriefAgent
active_services = ServiceBundle()
_owned_qdrant_backend: QdrantScopedBackend | None = None
_service_runtime: AsyncServiceRuntime | None = None
_services_ready = False


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    has_config_override = hasattr(application.state, "service_config")
    has_provider_override = hasattr(application.state, "secret_provider")
    config = getattr(application.state, "service_config", None)
    provider = getattr(application.state, "secret_provider", None)
    try:
        if has_config_override or has_provider_override or not _services_ready:
            wire_services(config=config, secret_provider=provider)
        yield
    finally:
        await shutdown_services()
        if has_config_override:
            del application.state.service_config
        if has_provider_override:
            del application.state.secret_provider


app = FastAPI(title=_AGENT_NAME, lifespan=_lifespan)


def configure_app_startup(*, config: dict[str, Any], secret_provider: SecretProvider) -> None:
    """Set explicit startup inputs for an embedded app or integration test."""

    app.state.service_config = dict(config)
    app.state.secret_provider = secret_provider


class PendingResumeRegistry:
    """Process-local ownership and single-flight guard for pending HITL threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[tuple[tuple[str, str], ...], str, str, bool]] = {}

    def register(self, thread_id: str, scope: dict[str, str], caller_id: str, session_id: str) -> None:
        owner = (tuple(sorted(scope.items())), caller_id, session_id, False)
        with self._lock:
            self._pending[thread_id] = owner

    def begin(self, thread_id: str, scope: dict[str, str], caller_id: str, session_id: str) -> bool:
        with self._lock:
            owner = self._pending.get(thread_id)
            if owner is None or owner[3] or owner[:3] != (tuple(sorted(scope.items())), caller_id, session_id):
                return False
            self._pending[thread_id] = (owner[0], owner[1], owner[2], True)
            return True

    def finish(self, thread_id: str, *, still_pending: bool) -> None:
        with self._lock:
            owner = self._pending.get(thread_id)
            if owner is None:
                return
            if still_pending:
                self._pending[thread_id] = (owner[0], owner[1], owner[2], False)
            else:
                self._pending.pop(thread_id, None)


pending_resumes = PendingResumeRegistry()


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


class ResumeRequest(BaseModel):
    thread_id: str
    session_id: str
    feedback: str | dict[str, Any] | None = None


def wire_services(
    *,
    services: ServiceBundle | None = None,
    config: dict[str, Any] | None = None,
    secret_provider: SecretProvider | None = None,
) -> SupplierECNImpactBriefAgent:
    """Construct the agent with deployment-provided or configured services.

    Explicitly injected clients win. Otherwise a configured Qdrant store is
    built only when its section and embedding credential are both present.
    """
    global agent, active_services, pending_resumes, _services_ready
    global _owned_qdrant_backend, _service_runtime

    _close_owned_services_sync()
    runtime_config = dict(config) if config is not None else load_config(str(CONFIG_PATH))
    provider = secret_provider or secrets_factory(namespace=_NAMESPACE, agent_name=_AGENT_NAME)
    supplied = services or ServiceBundle()
    vector_store = supplied.vector_store
    new_backend: QdrantScopedBackend | None = None
    new_runtime: AsyncServiceRuntime | None = None
    try:
        if vector_store is None:
            configured = _configured_qdrant_store(runtime_config, provider)
            if configured is not None:
                vector_store, new_backend, new_runtime = configured

        new_services = ServiceBundle(db=supplied.db, vector_store=vector_store, llm=supplied.llm)
        new_agent = SupplierECNImpactBriefAgent(config=runtime_config, services=new_services)
        new_agent.compile()
        new_agent.provision_secrets(provider)
    except BaseException:
        try:
            _close_service_pair_sync(new_backend, new_runtime)
        except BaseException:
            _logger.exception("Failed to clean up Qdrant resources after agent startup failure")
        raise

    active_services = new_services
    agent = new_agent
    _owned_qdrant_backend = new_backend
    _service_runtime = new_runtime
    pending_resumes = PendingResumeRegistry()
    _services_ready = True
    return agent


def _configured_qdrant_store(
    config: Mapping[str, Any], provider: SecretProvider
) -> tuple[QdrantScopedVectorStore, QdrantScopedBackend, AsyncServiceRuntime] | None:
    if "vector_store" not in config:
        return None
    raw = config["vector_store"]
    validated = _validate_vector_store_config(raw)
    embedding_api_key = provider.get(_EMBEDDING_API_KEY_SECRET)
    if not embedding_api_key:
        return None

    connection = QdrantConnection(
        url=validated["url"],
        collection_name=validated["collection_name"],
        embedding_base_url=validated["embedding_base_url"],
        embedding_api_key=embedding_api_key,
        vector_size=validated["vector_size"],
        distance=validated["distance"],
        embedding_model=validated["embedding_model"],
        embedding_api_version=validated["embedding_api_version"],
        embedding_provider=validated["embedding_provider"],
        timeout_s=validated["timeout_s"],
        write_timeout_s=validated["write_timeout_s"],
        max_batch_documents=validated["max_batch_documents"],
        max_top_k=validated["max_top_k"],
    )
    runtime = AsyncServiceRuntime()
    try:
        backend = QdrantScopedBackend(
            namespace=_NAMESPACE,
            agent_name=_AGENT_NAME,
            connection=connection,
            runtime=runtime,
            snippet_field=validated["snippet_field"],
            snippet_chars=validated["snippet_chars"],
            cache_size=validated["cache_size"],
            audit_state_provider=current_qdrant_audit_state,
        )
    except BaseException:
        try:
            _close_runtime_sync(runtime)
        except BaseException:
            _logger.exception("Failed to close Qdrant runtime after backend construction failure")
        raise
    return QdrantScopedVectorStore(backend), backend, runtime


def _validate_vector_store_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("config.vector_store must be a mapping")
    unknown = sorted(set(raw) - _VECTOR_STORE_KEYS)
    if unknown:
        raise ValueError(f"config.vector_store has unknown keys: {unknown}")

    validated = dict(raw)
    for key in ("url", "collection_name", "embedding_base_url"):
        validated[key] = _required_vector_string(raw, key)
    for string_key, string_default in (
        ("embedding_model", "text-embedding-ada-002"),
        ("embedding_api_version", "2024-02-15-preview"),
        ("embedding_provider", "azure"),
    ):
        value = raw.get(string_key, string_default)
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"config.vector_store.{string_key} must be a non-empty string")
        validated[string_key] = value

    distance = raw.get("distance", "cosine")
    if not isinstance(distance, str) or distance not in {"cosine", "euclid", "dot"}:
        raise ValueError("config.vector_store.distance must be one of cosine, euclid, dot")
    validated["distance"] = distance
    for int_key, int_default in (
        ("vector_size", 1536),
        ("max_batch_documents", 1000),
        ("max_top_k", 100),
        ("cache_size", 32),
        ("snippet_chars", 400),
    ):
        validated[int_key] = _positive_int(raw, int_key, int_default)
    for number_key, number_default in (("timeout_s", 30.0), ("write_timeout_s", 60.0)):
        validated[number_key] = _positive_number(raw, number_key, number_default)

    snippet_field = raw.get("snippet_field", "snippet")
    if not isinstance(snippet_field, str) or not snippet_field.isidentifier():
        raise ValueError("config.vector_store.snippet_field must be an identifier string")
    validated["snippet_field"] = snippet_field
    return validated


def _required_vector_string(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"config.vector_store.{key} must be a non-empty string")
    return value


def _positive_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"config.vector_store.{key} must be a positive integer")
    return value


def _positive_number(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"config.vector_store.{key} must be a positive number")
    return float(value)


def _take_owned_services() -> tuple[QdrantScopedBackend | None, AsyncServiceRuntime | None]:
    global _owned_qdrant_backend, _service_runtime, _services_ready
    backend, runtime = _owned_qdrant_backend, _service_runtime
    _owned_qdrant_backend = None
    _service_runtime = None
    _services_ready = False
    return backend, runtime


def _close_owned_services_sync() -> None:
    """Close the previous process-owned pair before synchronous rewiring."""

    backend, runtime = _take_owned_services()
    _close_service_pair_sync(backend, runtime)


def _close_service_pair_sync(
    backend: QdrantScopedBackend | None,
    runtime: AsyncServiceRuntime | None,
) -> None:
    """Close one owned backend/runtime pair, attempting both sides."""

    failures: list[BaseException] = []
    if backend is not None:
        try:
            backend.close()
        except BaseException as exc:
            failures.append(exc)
    if runtime is not None:
        try:
            _close_runtime_sync(runtime)
        except BaseException as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


def _close_runtime_sync(runtime: AsyncServiceRuntime) -> None:
    """Await an owned runtime even when rewiring from an async startup hook."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_close_runtime_bounded(runtime))
        return

    failures: list[BaseException] = []

    def close_on_fresh_loop() -> None:
        try:
            asyncio.run(_close_runtime_bounded(runtime))
        except BaseException as exc:
            failures.append(exc)

    closer = threading.Thread(target=close_on_fresh_loop, name="QdrantRuntimeCloser", daemon=True)
    closer.start()
    closer.join(RUNTIME_CLOSER_JOIN_TIMEOUT_S)
    if closer.is_alive():
        _logger.warning(
            "Qdrant runtime close thread exceeded %.3fs; process shutdown will continue",
            RUNTIME_CLOSER_JOIN_TIMEOUT_S,
        )
        return
    if failures:
        raise failures[0]


async def _close_runtime_bounded(runtime: AsyncServiceRuntime) -> None:
    try:
        await asyncio.wait_for(
            runtime.aclose(timeout=RUNTIME_CLOSE_TIMEOUT_S),
            timeout=RUNTIME_CLOSER_JOIN_TIMEOUT_S,
        )
    except TimeoutError:
        _logger.warning(
            "Qdrant runtime close exceeded %.3fs; process shutdown will continue",
            RUNTIME_CLOSER_JOIN_TIMEOUT_S,
        )


async def shutdown_services() -> None:
    """Close process-owned Qdrant stores before their async runtime."""

    backend, runtime = _take_owned_services()
    failures: list[BaseException] = []
    if backend is not None:
        try:
            backend.close()
        except BaseException as exc:
            failures.append(exc)
    if runtime is not None:
        try:
            await _close_runtime_bounded(runtime)
        except BaseException as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


wire_services()


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    auth = authenticate(request, operation=Operation.READ_INVOKE)
    caller_id = auth.principal["caller_id"]
    hitl_allowed = auth.auth_source is AuthSource.MIDDLEWARE and bool(caller_id)
    ctx = InvocationContext(
        session_id=req.session_id or str(uuid4()),
        caller_trust_level=auth.trust,
        caller_id=caller_id,
        hitl_allowed=hitl_allowed,
    )
    audit_state = {
        "trace_id": str(uuid4()),
        "correlation_id": ctx.correlation_id,
        "session_id": ctx.session_id,
    }
    with bind_qdrant_audit_state(audit_state), bound_secrets(agent._secrets_provider):
        result = cast(dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=auth.scope))
    if result.get("status") == "awaiting_human":
        thread_id = result.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise HTTPException(status_code=500, detail="HITL thread registration failed.")
        result["session_id"] = ctx.session_id
        pending_resumes.register(thread_id, auth.scope, caller_id, ctx.session_id)
    return result


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request) -> dict[str, Any]:
    auth = authenticate(request, operation=Operation.RESUME)
    caller_id = auth.principal["caller_id"]
    if not pending_resumes.begin(req.thread_id, auth.scope, caller_id, req.session_id):
        raise HTTPException(status_code=409, detail="HITL thread is not pending for this caller.")
    if not _checkpoint_is_pending(req.thread_id):
        pending_resumes.finish(req.thread_id, still_pending=False)
        raise HTTPException(status_code=409, detail="HITL thread is not pending for this caller.")
    try:
        with (
            bind_qdrant_audit_state(_checkpoint_audit_state(req.thread_id, req.session_id)),
            bound_secrets(agent._secrets_provider),
        ):
            result = cast(dict[str, Any], agent.resume(req.thread_id, req.feedback))
    except Exception:
        pending_resumes.finish(req.thread_id, still_pending=True)
        raise
    still_pending = result.get("status") == "awaiting_human" and _checkpoint_is_pending(req.thread_id)
    pending_resumes.finish(req.thread_id, still_pending=still_pending)
    return result


def _checkpoint_is_pending(thread_id: str) -> bool:
    """Confirm both outer and inner checkpointers still have resumable work."""
    try:
        config = agent._thread_config(thread_id)
        outer = agent._compiled.get_state(config)
        main_node = agent._nodes["main"]
        inner_graph = main_node.get_subgraph()
        inner = inner_graph._compiled.get_state(config)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return (
        "main" in outer.next
        and bool(inner.next)
        and isinstance(inner.values.get("hitl_draft"), dict)
        and inner.values.get("hitl_status") is None
    )


def _checkpoint_audit_state(thread_id: str, session_id: str) -> dict[str, str]:
    """Recover stable trace correlation for a resumed invocation."""

    try:
        values = agent._compiled.get_state(agent._thread_config(thread_id)).values
    except (AttributeError, KeyError, TypeError, ValueError):
        values = {}
    return {
        "trace_id": str(values.get("trace_id") or uuid4()),
        "correlation_id": str(values.get("correlation_id") or uuid4()),
        "session_id": str(values.get("session_id") or session_id),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "SupplierECNImpactBriefAgent"}
