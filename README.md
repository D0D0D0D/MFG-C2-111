# MFG-C2-111 — SupplierECNImpactBriefAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: MFG

## Overview

SupplierECNImpactBriefAgent prepares the first draft of an impact brief after a supplier sends an
engineering change notice (ECN). It resolves which parts, products and lots the change actually
touches by exact lookup against an approved-parts register, recalls the tenant's own history of
comparable changes, and returns evidence, open unknowns, risk hypotheses and the checks a supplier
quality engineer should run.

The division of labour between the deterministic layer and the model is the design:

- **Scope resolution is exact, never inferred.** Part → product → lot expansion is a database
  lookup against approved records. A model is not asked which products a part is in, because a
  plausible-but-wrong expansion is the failure mode with the highest cost here.
- **Recall is tenant-scoped at the service boundary.** Precedent search is confined to the
  verified tenant by the retrieval layer, not by a filter the caller could influence.
- **The model works on the resolved facts.** It matches precedents to the current change and
  proposes risk hypotheses and checks — over an impact scope it did not compute and cannot widen:
  the composed response is projected back against the trusted scope before it is returned.
- **Citations are verified, not trusted.** Every claim cites a source id from the retrieved set;
  a cited id that is not in that set fails the output gate rather than being returned.

The brief is advisory. The agent does not decide whether to approve a change, does not write or
update BOM/AVL records, does not make a regulatory-compliance determination, and does not issue an
external response to a customer or supplier. Those are excluded from the response schema, not just
from the prompt.

Human review is built into the flow rather than bolted on. When the intake gate finds the ECN
ineligible or the composed brief fails its own validation, the run **suspends** for review
(`status: awaiting_human`) and is continued through `POST /resume`. When the caller is an
automated pipeline that could never resume a thread, the agent detects that and returns a
review-required brief instead of suspending — a suspension nobody can answer is a deadlock, not a
safeguard.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- an approved-parts register reachable through the `ServiceBundle.db` seam
  (`src/services/service.py`). No database client is bundled;
- optionally, a Qdrant-backed precedent knowledge base. The `vector_store` section of
  `config/config.yaml` ships **commented out**, so the default deployment runs with no precedent
  store and degrades explicitly rather than silently. Uncommenting it is not enough on its own:
  the store is built only when the section is present **and** the `AZURE_EMBEDDING_API_KEY`
  secret resolves — otherwise the agent starts without it;
- a `BaseLLM` implementation. The standalone server does not construct one; the Marketplace
  container adapter (`cli.py`) supplies `AzureLLM`, which resolves `AZURE_OPENAI_API_KEY`,
  `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT` per call rather than holding them, so a
  client built for one caller cannot outlive that call;
- caller authentication and verified tenant scope in front of the entry adapter.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`), `timeout_s` (`30`) | Framework retry and timeout |
| `budget_usd` (`1.50`) | Cost ceiling per invocation for the model stages |
| `memory_enabled` (`true`) | Mandatory here — the checkpointer is what makes suspend/resume possible |
| `hitl.enabled` (`true`), `hitl.max_hitl` (`8`) | Human review, and the ceiling on review cycles per session |
| `vector_store.*` (commented out) | Precedent store: endpoint, collection, embedding model and deployment, timeouts, batch and top-k ceilings, snippet field and length. Unknown keys are rejected at start-up rather than ignored |

`config/agent.yaml` declares `requires.secrets: []` — nothing is required for the process to
start. The credentials above are resolved by the components that need them, at the point they are
needed, and their absence degrades the corresponding capability instead of blocking start-up.

The template does not fetch the ECN from a supplier portal, does not parse attachments, and does
not write to any system of record. Input is a JSON ECN envelope.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent resolves its secrets and services through the platform. Without it, it starts but has neither a model nor a precedent store (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. Version 1.0.2 or later is required: the vendored Qdrant backend imports `build_isolation_tag`, which older releases do not have — an older pin fails at import, not at first search |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

Services are wired eagerly, at import: `src/api/server.py` calls `wire_services()` while the
module loads, so a configuration error surfaces at start-up rather than on the first request. With
no platform behind it, that wiring simply finds nothing to wire — no embedding credential, so no
precedent store; no injected model client, because the standalone server never builds one. The
process starts and `/health` answers `200`.

`POST /invoke` then still runs. Intake, normalisation, the data-quality gate and register lookup
are deterministic and unaffected; the stages that need a model fall back and name the shortfall in
`degradation_reason` instead of returning an unsourced brief. The test suite runs without a
platform connection, using an in-process fake database and fake model.

## Input

`POST /invoke` takes `input` — the ECN as a **JSON document encoded as a string** — and an
optional `session_id`.

Callers must be authenticated, and authorisation is two-sided. Trust comes from verified upstream
middleware, or from a matching `Authorization: Bearer` header when `INVOKE_AUTH_TOKEN` is set on
the server environment; a wrong token is **401**, and a caller with no verified trust at all is
**403**. Separately, every request must resolve to a **verified tenant scope**, taken from
verified request state — never from the request body. An ECN whose `tenant_id` disagrees with the
verified scope is rejected rather than reconciled, and an unresolvable scope is **403**.

The two authentication routes are not equivalent, deliberately. A bearer token authenticates a
caller but establishes no identity, so it is accepted for `POST /invoke` only; `POST /resume` and
any write operation require a verified middleware principal. Human review resumption is likewise
offered only to a middleware-identified caller — `hitl_allowed` is set true only for that route.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"ecn_id\":\"ECN-2026-0042\",\"effective_date\":\"2026-10-01\",\"parts\":[{\"part_number\":\"P-1234\"}]}",
    "session_id": "example-session"
  }'
```

ECN fields are allow-listed: an envelope carrying a field the schema does not know is rejected,
rather than passed through on the assumption that unknown means harmless. A body that is not a
JSON object, an ECN over the size limit, or a missing `ecn_id` fails closed with the reason in
`error_log`.

When a run suspends, the response carries `status: awaiting_human`, a `thread_id` and the
`session_id`. Continue it with the same identity that started it:

```bash
curl -X POST http://127.0.0.1:8000/resume \
  -H 'Content-Type: application/json' \
  -d '{"thread_id": "<thread_id from the suspended response>",
       "session_id": "<session_id from the same response>",
       "feedback": {"action": "approve"}}'
```

A resume is admitted only when the scope, caller and session all match the run that suspended, and
only when the checkpoint really is still waiting — a duplicate or mismatched resume is **409**,
and a second concurrent resume of the same thread is refused rather than run twice.

## Output

The standard AgentCore envelope is preserved. `output` carries the brief:

```json
{
  "mode": "invoke",
  "gate_kind": "draft",
  "impact_scope": {"parts": ["P-1234"], "products": ["PRD-9"], "lots": ["L-2026-07"]},
  "evidence": [{"claim": "...", "source_id": "prc-001", "excerpt": "..."}],
  "cited_source_ids": ["prc-001"],
  "unknowns": ["..."],
  "risk_hypotheses": ["..."],
  "recommended_checks": ["..."],
  "requires_human_review": false
}
```

`error_code`, `error_type` and `error_log` are surfaced when a run fails, and `degradation_reason`
names any stage that ran short — a missing precedent store or an unavailable model produces a
narrower brief that says so, not a quiet one. Values above are illustrative.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node
body runs, and every register and precedent query is scoped to the verified tenant at the service
boundary.

The input gate is an allow-list, not a deny-list: unknown ECN field names are rejected. A
deny-list fails in the direction of letting an unrecognised field through, which is the wrong
direction for a gate whose mistakes reach a knowledge base. The input and output gates read only
what the caller supplied, never state a node wrote — a gate that depends on its own node's output
cannot run before that node, and would not be a gate.

Provider exception text never reaches state or the response. A vector-store failure is normalised
to `kb_unavailable` or `kb_invalid_request` at the adapter, because provider errors routinely echo
the request — including the query body and, in some shapes, an authorization header.

On the way out, the composed brief is projected against the impact scope the deterministic layer
computed, so a model cannot widen the scope through the text it writes, and cited ids are checked
against the retrieved set.

Known limits:

- **Recall depends on a precedent store that is off by default.** Without it the agent still
  produces a brief, but from the current ECN and the register alone.
- **Exact register matching means exact.** A part whose identifier is not in the approved register
  in the expected form does not expand; it is reported as an unknown rather than guessed at.
- **Checkpoints are process-local by default.** Suspend/resume works within a running deployment;
  a multi-instance or restart-tolerant deployment needs a durable checkpointer configured.
- **Citation verification proves provenance, not correctness.** A verified claim quotes a record
  that says that. Whether the record was right is outside the agent's reach.

Output is machine-generated and assistive. A qualified reviewer — and, where applicable, customer
and legal functions — makes the final determination.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm`
- HITL: **enabled** (`hitl.max_hitl: 8`), with a `hitl_allowed` guard for automated callers
- Persistence: framework checkpointer required (`memory_enabled: true`); optional Qdrant precedent
  store, off by default
- External API clients: Azure OpenAI via the Marketplace adapter; Qdrant when configured; none
  bundled in the standalone server

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using an in-process fake database and fake model. Running
the agent itself starts cleanly with `uvicorn src.api.server:app`, but with no model and no
precedent store — see *Behaviour without the platform*.

## Documentation

`docs/02_design.md` maps the workflow onto the node backbone and the inner domain graph, and
carries the state schema, the per-node security-gate placement and the design decision record.
`docs/03_test_spec.md` records the test specification and the verification results, including a
live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
