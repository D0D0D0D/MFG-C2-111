# Test Specification — MFG-C2-111

## AGENTIC STAR Marketplace 実機疎通（2026-08-31・PASS）

AGENTIC STAR DEV 環境の User Page から実際に invoke し、runner Pod の実行結果と応答を確認した。

| 項目 | 実測 |
|---|---|
| 入力 | `{"ecn_id": "ECN-2026-0831", "supplier_id": "SUP-001", "parts": [{"part_key": "PART-001"}], "attachments": [{"name": "spec.pdf"}], "body": "approved part の材質変更通知。耐熱性の規格値が下がる可能性あり。", "effective_date": "2026-09-15"}` |
| runner Pod | 実行 6.0 秒 / exit 0 / agent コンテナのログ 0 行（正常完了時は出力しない） |
| 応答 | `{"impact_scope": {}, "evidence": [], "unknowns": [], "risk_hypotheses": [], "recommended_checks": [], "requires_human_review": true, "advisory": "Human SQE review remains required for decisions.", "mode": "invoke", "gate_kind": "impact_brief"}` |

KB 未投入のため evidence は空。`requires_human_review: true` を返し、断定的な影響判断を出さない fail-closed 挙動を確認。

KB は未投入のため、確認できたのは「該当情報が無いときに正しくそう返すこと」までである。
KB 投入後の検索精度はこの記録の対象外。


## Scope and test seams

DB, vector, and LLM dependencies remain constructor-injectable. Unit and workflow tests use local doubles. PB-3a uses a real in-memory SQLite engine for both approved-register DB lookup and precedent-KB lookup, and PB-3b alone connects to `http://localhost:6333` when available. ECN input is an allowlisted JSON envelope and tenant scope comes from verified request context, not the envelope body. PB-3 writes identical keys/content into separate scope partitions and proves that only the requested stored scope is returned; absence of local Qdrant is an explicit skip rather than a fake PASS.

## Framework compliance (TC)

| ID | Test focus | Expected result | Result |
|---|---|---|---|
| TC-01 | Flat `State` schema | JSON/msgpack-safe primitives only | PASS |
| TC-02 | Invalid ECN / scope gate | `status=error`, `processing_eligible=false` | PASS |
| TC-03 | Checkpoint secret safety | No credential-like State fields | PASS |
| TC-04 | Invocation context boundary | Server passes scope through `input_context`; State carries no context object | PASS |
| TC-05 | S-4 lifecycle events | Domain code emits no duplicate lifecycle event names | PASS |
| TC-06 | S-2 extension contract | FunctionNode uses `_extra_security_gate_input()` only | PASS |
| TC-07 | S-3 extension contract | FunctionNode uses `_extra_security_gate_output()` only | PASS |
| TC-08 | S-1 trust enforcement | `VERIFIED_EXTERNAL` is explicit for every domain node。加えて **不足 trust では `execute()` に到達しないこと**を証明する | PASS — PB-6 `TestInvokeOrder::test_s1_denial_refuses_execution_before_execute` が `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / **`execute()` 未呼び出し** を assert（正本 docs/03 の TC-08 要求） |

## Domain and security tests

| ID | Scenario | Expected result | Result |
|---|---|---|---|
| BL-01 | NFKC/casefold part normalization | Full-width part ID becomes the common exact-match key | PASS |
| BL-02 | Missing attachment / quality failure | Success plus review-required brief; inner graph is bypassed | PASS |
| BL-03 | Duplicate/multiple register match | No guess; `requires_human_review=true` | PASS |
| BL-04 | tenant scope mismatch or non-allowlisted ECN key | Fail closed before processing | PASS |
| BL-05 | Missing tenant on KB search | Backend not called; `status=error` | PASS |
| BL-06 | Scoped KB hit projection | Scope mismatch and non-allowlisted metadata are discarded | PASS |
| BL-07 | LLM absent/failure | Deterministic, fully-shaped advisory fallback | PASS |
| BL-08 | Approval/BOM-AVL/regulatory/external-answer content | S-3 rejects it after NFKC + casefold matching | PASS |
| BL-09 | Evidence preservation | Each risk/check claim needs an ECN/DB/KB source in this invocation | PASS |
| BL-10 | LLM response contract | Declaration-built normal response passes; undeclared keys, wrong direct types/items, and enum outside the contract raise `SecurityViolationError` without echo | PASS |
| BL-11 | Entry operation policy | standalone token is read-only; `/resume` requires middleware caller identity | PASS |
| BL-12 | HITL resume lifecycle | issued session ID is required; completed thread cannot resume twice; two parallel HTTP resume requests yield exactly one success | PASS |
| BL-13 | Pre-interrupt HITL output gate | Prohibited decision/source-text wording and unknown source IDs are rejected before `interrupt()`; a safe invocation-local citation reaches HITL | PASS |

## Proof of boundary (PB)

| ID | Boundary | Test | Expected result | Result |
|---|---|---|---|---|
| PB-1 | Node → audit logger | Concrete node call paths emit audit activity | No silent domain path | PASS |
| PB-2 | State serialization | AST State safety scan | No Pydantic/dataclass objects | PASS |
| PB-3a | L1 → SQLite 接合部 | 実 in-memory SQLite（fake backend ではない）で approved register と precedent KB を検証 | 同一 `part_key` の別 tenant register 行、および別 tenant KB hit は不可視。要求 tenant の各 1 件だけを返す | PASS |
| PB-3b | L1 → Qdrant 接合部 | **実 Qdrant**（`qdrant/qdrant:v1.12.4`・Docker）+ 実 SDK adapter（埋め込み生成だけ決定論 test double） | scope A/B に同一本文を保存し、A 検索は保存レコード由来 scope A の 1 件だけを返す | **PASS（2026-08-30 実行）**。`localhost:6333` 不在時は明示 SKIP（fake PASS にしない） |
| PB-4 | Import isolation | AST scan of `src/` | No Level-0 import | PASS |
| PB-5 | Checkpoint safety | State credential-field scan | No credentials/checkpoint clients | PASS |
| PB-6 | BaseNode invoke order | S-1 → start → S-2 → execute → S-3 → complete | Exact ordered sequence | PASS |
| PB-7 | HITL propagation | GraphInterrupt sentinel plus graph interrupt/resume integration | Interrupt propagates; resume reaches success | PASS |

## Applied-part integration and cross-cutting checks

The copied part tests under `tests/parts/` execute with the full suite. The entry-adapter test fixture restores the part's canonical sample scope, while template-specific server tests exercise the adapted `tenant_id` declaration. This is the template-specific Layer 2/Layer 3 transcription.

| ID | Coverage |
|---|---|
| Layer 2 — scoped-retrieval | The tenant scope key is required; real SQLite query filters it and each hit projects stored-record scope; missing/mismatched hit scope is rejected. |
| Layer 2 — qdrant-scoped-backend | FastAPI startup の `wire_services()` が config + namespaced secret から実 backend を `ServiceBundle` へ渡す。vector_store 節または secret 欠落時は `None` と `[]` 縮退を維持。実 Qdrant 接合では同一本文の scope A/B 分離と保存 scope 投影を確認する。 |
| Layer 2 — cat2 graph | JSON envelope (`default=str`), cached inner graph, separate checkpointers, explicit merge map, propagated errors, and v0.1.5 safe subgraph-error projection that never echoes inner `error_log` text. |
| Layer 2 — LLM draft | Injected LLM, provider failure fail-closed with stable code/no exception echo, full-shape fallback, evidence hooks. |
| Layer 2 — evidence/output | Contract generated from `FindingField` declarations; typed projection in both LLM stages including nested list-of-dict second-pass projection; invocation-local provenance; explicit `invoke`/`impact_brief` output path; v0.3.1 strict helpers compile cleanly under `-W error`. |
| Layer 2 — entry adapter | `READ_INVOKE` vs `RESUME` is selected by actual side effect; token/STG auth cannot resume. |
| X-1 | Ordered S-1/S-2/S-3/S-4 gates across outer and inner nodes. |
| X-2 | State/checkpoint and import-isolation boundaries remain safe. |
| X-3 | DB and KB tenant scopes fail closed without backend leakage. |
| X-4 | No approval decision, BOM/AVL update, final regulatory decision, or external final answer can be emitted. |
| X-5 | HITL interrupt propagates; `hitl_allowed=false` returns a review-required brief; pending ownership/single-flight prevents completed or parallel duplicate resume. |

## Execution result

- Date: 2026-08-30
- **実 LLM 1 回転（2026-08-30・`work/scripts/mrb_live_llm_check.py` / ローカル CLI 経由）**: PASS。
  中身のある出力キー = `impact_scope` / `evidence` / `unknowns` / `risk_hypotheses` /
  `recommended_checks` / `advisory` / `mode` / `gate_kind`（空の器ではない）。
  🔴 初回は `tests/conftest.py` の `ctx.hitl_allowed = False` が SDK 1.0.2 の
  frozen `InvocationContext` に当たり `FrozenInstanceError` で失敗した。
  `dataclasses.replace()` へ置換して解消（本改版に含む）。
- 実 Qdrant（Docker `qdrant/qdrant:v1.12.4`）を `localhost:6333` に起動した環境で全テスト実行 → **392 passed / skip 0**（2026-08-30 当時）。今回の register SQLite テスト追加後は Qdrant ありの全件を再実行していない。
  PB-3b（`test_real_qdrant_returns_only_the_requested_stored_scope`）が実際に実行され PASS。
  **PB-3b の証跡はこのローカル実行のみ**である。CI 設定は Qdrant service を定義していないため、
  CI 上では PB-3b は常に SKIP になる（CI service 化は本改版の範囲外・別途要検討）。
- Qdrant 不在環境での全テスト実行 → **392 passed / 1 skipped**。
  PB-3b は理由付き明示 SKIP（`Qdrant is not reachable at http://localhost:6333`）となり、
  fake PASS にはならないことを確認した。
  SDK 1.0.2 は `qdrant-client==1.19.0` を引くため server 1.12.4 との組合せで互換性警告が出るが動作に影響なし。
  **配布先の Qdrant server 版は qdrant-client の対応版に合わせること。**
- Date: 2026-07-23
- `source .venv/bin/activate && pytest tests/ -q --tb=line` → **107 passed**
- `pytest tests/proof_of_boundary/ -q --tb=line` → **5 passed**

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 2 回 |
| 出力 | `impact_scope` / `evidence` / `unknowns` / `risk_hypotheses` / `recommended_checks` / `advisory` / `mode` / `gate_kind` が非空。縮退理由なし |
| HITL 分岐 | `hitl_allowed=True`（`hitl.enabled: true` の本番設定）でも別途 1 回転を確認。`status: awaiting_human` で中断し、`BriefComposeNode._validate_hitl_payload`（claim coverage / provenance / S-3 出典 allowlist）が実 LLM のドラフトに対して通ることを確認した |

本番の LLM は Marketplace アダプタ `cli.py` が構築する `AzureLLM`（`AzureOpenAIClient`）で、こちらは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（実 LLM の鍵が要るため）。
