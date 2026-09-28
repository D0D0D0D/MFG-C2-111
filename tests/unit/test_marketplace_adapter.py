"""Marketplace entry adapter and Azure LLM boundary tests."""

from __future__ import annotations

import json

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import SupplierECNImpactBriefAgent
from src.services.azure_llm import AzureLLM
from src.services.service import ServiceBundle
from tests.integration.test_ecn_workflow import FakeDatabase, payload


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._REQUIRED_SECRET_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _contains(value, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


def test_marketplace_conversation_history_never_enters_state_or_output(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNMASKED-CONVERSATION-PII-111"
    agent = cli.MarketplaceGraph()
    agent.compile()
    original_invoke = agent._compiled.invoke
    captured = {}

    def capture_state(initial_state, config=None):
        captured["initial"] = initial_state
        final_state = original_invoke(initial_state, config=config)
        captured["final"] = final_state
        return final_state

    agent._compiled.invoke = capture_state
    incomplete = json.loads(payload())
    incomplete["attachments"] = []
    result = agent.invoke(
        json.dumps(incomplete),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"conversation_history": [{"role": "user", "content": sentinel}]},
    )

    assert captured["initial"]["input_context"] == {"tenant_id": values["STG_DEFAULT_TENANT_ID"]}
    assert not _contains(captured["initial"], sentinel)
    assert not _contains(captured["final"], sentinel)
    assert not _contains(result, sentinel)


def test_marketplace_rebuilds_scope_and_disables_hitl_on_parent_invoke(monkeypatch):
    values = _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(ctx=ctx, input_context=input_context)
        return {"status": "success"}

    monkeypatch.setattr(SupplierECNImpactBriefAgent, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        payload(),
        ctx=original_ctx,
        input_context={"conversation_history": ["untrusted"], "tenant_id": "attacker"},
    )

    assert result["status"] == "success"
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_id == "marketplace-user"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False
    assert captured["input_context"] == {"tenant_id": values["STG_DEFAULT_TENANT_ID"]}


def test_marketplace_hitl_suppression_returns_visible_review_required_advisory(monkeypatch):
    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph(services=ServiceBundle(db=FakeDatabase()))
    agent.compile()

    result = agent.invoke(
        payload(),
        ctx=InvocationContext(
            caller_id="marketplace-user",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=True,
        ),
    )

    assert result["status"] == "success"
    assert result["status"] != "awaiting_human"
    assert result["output"]["requires_human_review"] is True
    assert result["output"]["advisory"] == "Human SQE review remains required for decisions."


def test_none_context_is_forwarded_and_rejected_by_s1(monkeypatch):
    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph()
    agent.compile()

    result = agent.invoke(payload(), ctx=None)

    assert result["status"] == "error"
    assert any("S-1 trust gate denied" in item for item in result["output"]["error_log"])


def test_secret_provider_resolves_only_allowlisted_process_environment(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    provider = agent._secrets_provider
    assert {key: provider.require(key) for key in cli._REQUIRED_SECRET_KEYS} == values
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None
    assert "must-not-be-visible" not in repr(vars(provider))
    assert isinstance(agent._services.llm, AzureLLM)


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_missing_required_value_fails_during_startup_before_invoke(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)
    agent = cli.MarketplaceGraph()
    invoked = False

    def invoke(*args, **kwargs):
        nonlocal invoked
        invoked = True

    agent.invoke = invoke
    with pytest.raises(MissingSecret, match=missing_key):
        agent.provision_secrets(NullProvider())

    assert not invoked


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph()
    invoked = False
    def invoke(*args, **kwargs):
        nonlocal invoked
        invoked = True
    agent.invoke = invoke
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())
    assert not invoked


@pytest.mark.parametrize("invalid_scope", ["", "   ", " tenant-a", "tenant-a ", "tenant\na"])
def test_invalid_scope_value_fails_during_startup(monkeypatch, invalid_scope):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", invalid_scope)

    with pytest.raises((MissingSecret, ValueError), match="STG_DEFAULT_TENANT_ID"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_azure_llm_resolves_fresh_values_without_retaining_them(monkeypatch):
    import shared.services.llm.azure_openai_client as azure_module

    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            yield "ok"

        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(azure_module, "AzureOpenAIClient", FakeAzureOpenAIClient)
    provider = cli._AllowlistedEnvironmentProvider()
    llm = AzureLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}

        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_azure_llm_bind_tools_returns_a_new_instance():
    llm = AzureLLM()
    bound = llm.bind_tools([{"name": "lookup"}])

    assert bound is not llm
    assert vars(llm) == {}
    assert vars(bound) == {"_tools": ({"name": "lookup"},)}


def test_main_loads_repo_config_for_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "MFG-C2-111"
    assert called["namespace"] == "mfg"
    assert called["config"]["max_retry"] == 3
    assert called["config"]["hitl"]["enabled"] is True
