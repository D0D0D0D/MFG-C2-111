"""AGENTIC STAR Marketplace container entry adapter for MFG-C2-111."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.secrets.base import SecretProvider
from framework.utils.config_loader import load_agent_config
from shared.bootstrap import run_agent_marketplace
from shared.secrets.env_provider import EnvProvider

from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import SupplierECNImpactBriefAgent
from src.services.azure_llm import AzureLLM
from src.services.service import ServiceBundle

_NAMESPACE = "mfg"
_AGENT_NAME = "MFG-C2-111"
_PROVIDER_AGENT_NAME = "mfg_c2_111"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Read only this agent's declared values from the process environment."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value else default


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build the declared Marketplace scope without repairing invalid values."""

    scope: dict[str, str] = {}
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        value = provider.require(env_name)
        if value != value.strip() or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(
                f"Marketplace scope environment variable {env_name!r} "
                "must be a clean non-empty string"
            )
        scope[key] = value
    return scope


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = provider.require("AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class MarketplaceGraph(SupplierECNImpactBriefAgent):
    """Marketplace-only boundary hardening and dependency wiring."""

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Marketplace conversation history is untrusted free text and is not
        # covered by this agent's S-2 input gate. Rebuild adapter-owned context
        # from scratch so present and future runner keys cannot enter State.
        del input_context
        environment_provider = _AllowlistedEnvironmentProvider()
        scope = _scope_from_environment(environment_provider)

        # The Marketplace runner cannot resume AWAITING_HUMAN. InvocationContext
        # is frozen, so replace it while preserving identity, trust, and trace
        # fields. A missing context remains missing and is rejected by S-1.
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=scope,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The Marketplace runner's default provider may include dotenv fallback
        # and unrestricted environment lookup. This adapter deliberately owns a
        # smaller process-environment boundary for this single-agent container.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            environment_provider.require(key)
        _scope_from_environment(environment_provider)

        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)
        self._services = ServiceBundle(
            db=self._services.db,
            vector_store=self._services.vector_store,
            llm=AzureLLM(),
        )


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
