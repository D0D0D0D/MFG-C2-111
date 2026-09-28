# Template Design Specification — MFG-C2-111 SupplierECNImpactBriefAgent

> 本 Design は承認済み提案 §4 の
> 7-Stage workflow を AgentCore SDK v1.0 の固定 5 ノード backbone + inner domain workflow graph
> にマッピングしたものである。

## Position in AgentCore Architecture

- **Agent Class**: `SupplierECNImpactBriefAgent`（`src/graph/graph.py`）
- **L1 Base**: **AgentBaseGraph**（L1 直継承。DocGenerationAgent パターン。`AutonomousBaseGraph` ではない
  — 終了は固定パイプラインで決まり、agent 自身が次アクションを決める think→act ループではないため）
- **Composition**: Cat 2 = outer `AgentBaseGraph` + `main` slot に `GraphNode`（内側 `DomainWorkflowGraph`）
- **AGENTIC STAR Marketplace adapter**: repo 直下 `cli.py` が環境変数から `tenant_id` scope を fail-closed で再構築し、Marketplace に限り `InvocationContext.hitl_allowed=False` と Azure LLM/secret 境界を設定する。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不使用 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state: dict) -> dict` のみ override）
  - Graph: composition（`register_nodes()` で node 差し込み、`add_edges()` は override しない）

## Architecture Overview

### 7-Stage workflow → 5-node backbone マッピング

Proposal §4 の Stage 0–6 を、固定 backbone と inner graph に割り当てる。**決定論的処理（Stage 0-2）は
LLM に委ねず、pre_process と inner の deterministic ノードで実装する。LLM 核は Stage 3-4（過去 ECN 想起 +
影響仮説の当てはめ）に限定する。**

| Proposal Stage | 処理 | 担当層 | 実装先 |
|---|---|---|---|
| Stage 0 | ECN 受領・テナント/工場/サプライヤー/ECN ID 確定・添付/権限チェック | deterministic | `pre_process`（ECNIntakeNode） |
| Stage 1 | 正規化アダプタ（ECN 部品 ID → 共通キー）+ データ品質ゲート | deterministic | `pre_process`（同ノード内 or 正規化 service） |
| Stage 2 | 承認部品レジスタ DB を SQL 完全一致検索・部品→製品→ロット展開 | deterministic | inner graph: `RegisterMatchNode` |
| Stage 3 | 過去 ECN 対応履歴 KB から類似失敗モード・確認観点を意味検索 | retrieval | inner graph: `PrecedentRecallNode` |
| Stage 4 | DB 展開 + ECN 自由記述 + KB 事例を統合し影響観点/必要試験を当てはめ評価 | LLM | inner graph: `ImpactAssessmentNode` |
| Stage 5 | 影響ブリーフ生成（根拠/不明点/反証/試験候補/レビュー先）+ HITL 判定 | LLM + HITL | inner graph: `BriefComposeNode` |
| Stage 6 | ブリーフ整形・fail-closed/HITL の最終ルーティング・監査確定 | deterministic | `post_process`（BriefFinalizeNode） |

### Node Configuration（outer backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | framework 既定（InvocationContext seed・S-1 trust 初期化） | user_input | validated context | InitializeNode (default) |
| pre_process | Stage 0-1: ECN 受領・識別子確定・正規化・データ品質ゲート。fail-closed 判定（添付欠落/権限不明/重複/失効/対応不能 → `requires_human_review`） | user_input（ECN envelope） | validated_input, tenant_id, normalized_parts, data_quality_flags, status | `ECNPreProcessNode(FunctionNode)` |
| main | Stage 2-5: inner `DomainWorkflowGraph` を呼び、DB 展開→KB 想起→影響評価→ブリーフ生成を実行。上流 error は passthrough | validated_input | impact_brief, evidence, unknowns, risk_hypotheses, recommended_checks, hitl_* | `ECNImpactGraphNode(GraphNode)` |
| post_process | Stage 6: ブリーフ整形・formatted_output 生成・監査確定 | impact_brief 他 | formatted_output | `BriefPostProcessNode(FunctionNode)` |
| finalize | framework 既定（get_output で formatted_output 露出） | formatted_output | output | FinalizeNode (default) |

### Data Flow（outer）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

- **pre→main は無条件辺**。`ECNImpactGraphNode.execute()` を override し、下表の bypass 条件に
  該当する場合は inner graph を起動せず `return {}` で passthrough する
  （`GraphNode` subgraph 契約の必須パターン）。
- `route()` は main の後に `state["status"]` を読む（`success`→post / `retry`→pre / `error`→finalize）。
  HITL enabled のため `hitl_status` セット時は post/finalize へ分岐（framework の HITL routing 契約）。

#### fail-closed 状態遷移表（pre_process の判定 → main の起動可否）

Proposal の fail-closed 要件を、`status` と `requires_human_review` の 2 軸で表現する。**重要**: pre→main は
無条件辺のため、「業務上の処理不可（品質不合格・未一致予兆）」を `status=success` にすると main(GraphNode)
が未検証入力で inner を起動してしまう。これを防ぐため、pre_process は `processing_eligible: bool` を State に
立て、`ECNImpactGraphNode.execute()` は `status=="error"` **または** `processing_eligible==False` の場合に
`{}` passthrough する。

| pre_process の事象 | `status` | `requires_human_review` | `processing_eligible` | main(GraphNode) | 備考 |
|---|---|---|---|---|---|
| 不正入力・認可不能・tenant 不確定 | `AgentStatus.ERROR.value` | true | false | `{}` passthrough → route=error → finalize | エラー用 `formatted_output` は post をスキップするため **finalize が既定エラー出力を生成**（下記 route 補足） |
| 添付欠落・品質ゲート不合格（欠損/重複/失効/参照切れ/展開不能） | `AgentStatus.SUCCESS.value` | true | false | `{}` passthrough → route=success → post | post が「レビュー要・処理未実施」ブリーフを整形（承認結論なし） |
| 一時的な基盤障害（DB/KB 接続断等） | `AgentStatus.RETRY.value` | false | —（未評価） | `{}` passthrough → route=retry → pre（max 3） | backbone の retry ループ |
| 正常（品質ゲート通過） | `AgentStatus.SUCCESS.value` | false | true | inner graph 起動 | Stage 2-5 実行 |

- inner graph 内の **DB 未一致/複数候補** は `status=success` + `requires_human_review=true` で正常に返す
  （error ではない）。inner 早期終了辺（RegisterMatchNode → END）でブリーフを「影響範囲未確定・SQE 確認要」
  として構成する。
- **route 補足（error 時の出力責務）**: `status=error` は `finalize` へ直行し post_process をスキップする。
  この場合の最終出力は `finalize`（既定 FinalizeNode の get_output）が `error_log` を含む最小 error ブリーフ
  として露出する。承認結論・影響範囲は含めない。

### Inner graph（`src/graph/domain_workflow_graph.py`）

`DomainWorkflowGraph(BaseGraph)`。4 ノードの直列トポロジ（fail-closed 早期終了辺あり）:

```
START → register_match → precedent_recall → impact_assessment → brief_compose → END
             ↓ (unmapped / multiple-candidate → 早期終了)
           END（requires_human_review を立てて返す）
```

| Inner Node | Stage | 種別 | 責務 |
|---|---|---|---|
| `RegisterMatchNode` | 2 | deterministic（DB/SQL） | `shared.services.database.base_db.BaseDatabase` で承認部品レジスタを完全一致検索。部品→製品→ロット展開。未一致/複数候補は `requires_human_review`。**推測補完禁止** |
| `PrecedentRecallNode` | 3 | retrieval（KB/vector） | **scoped-retrieval v0.3.0 部品を適用**（`ScopedRetrievalNode`・`scope_keys=("tenant_id",)`）。plain `BaseVectorStore.search()` は scope 条件を表せないため呼ばず、deployment の `search_scoped(*, scope, query, top_k)` だけを使用する。backend は全 scope を server-side 条件にし、各 hit に保存レコード由来 scope を投影する。欠落/不一致 hit は全て捨てる。**retrieval 出力 schema は「承認/却下結論」フィールドを持たず**（`METADATA_SAFE_KEYS` allowlist に結論を入れない）、失敗モード・反証事情・追加確認観点のみを返す（過去結論の自動継承を構造的に禁止）。薄い場合は `degradation_reason` で「類似事例なし」明示 |
| `ImpactAssessmentNode` | 4 | LLM | `shared.services.llm.base_llm.BaseLLM.complete(messages)` で DB 展開・ECN 自由記述・KB 事例を統合。S-3 と共通の検証関数から `source_catalog`（source ID・ECN/DB/KB 種別・抜粋）と `allowed_source_ids` を prompt の response contract に生成し、未知 ID の生成を禁止する。`evidence-gate v0.3.1` の型付き射影で未知キー・型違い・enum 外・上限超過を fail-closed。`impact_scope` は LLM に委ねず framework 所有値を維持する |
| `BriefComposeNode` | 5 | LLM + HITL | 同じ response contract（S-3 と共通の `source_catalog` / `allowed_source_ids` を含む）と型付き射影で影響ブリーフを構成。低 confidence/高リスク時は、禁止内容検査、当該実行の ECN/DB/KB source catalog による provenance 検証、後段 S-3 と同じ `validate_brief_claim_coverage()` による数値・比較表現の根拠 coverage 検証を先に通し、その後だけ `interrupt()`（`hitl_allowed` ガード付き）で SQE レビューへ。承認可否欄・BOM/AVL 更新 payload・対外確定回答は schema から排除 |

#### `ECNImpactGraphNode`（`GraphNode`）の 3 abstract 契約

| method | 実装方針 |
|---|---|
| `get_subgraph()` | `DomainWorkflowGraph(config=self._config)` で直接構築し `inner.compile()` して返す。**`_parent_config()` は wheel に存在しない**＝sample のみ。inner に渡す config（llm/db/vector store の注入シーム、tenant knobs）は register_nodes で `ECNImpactGraphNode(config=...)` として明示注入 |
| `extract_input(state)` | inner へは `input_context`（dict）が**自動伝播しない**ため、per-request の tenant/正規化部品/knobs を **JSON envelope 文字列**にパックして返す（`json.dumps({"tenant_id":..., "normalized_parts":..., "ecn":...}, default=str)` 必須）。inner 先頭ノード（RegisterMatchNode）が `json.loads` で復元し inner state に seed |
| `merge_output(state, sub_result)` | inner の `get_output()` が返す dict を outer state へ**明示マッピング**（raw pass-through 禁止 — 合成一貫性のレビュー観点）。下表参照。**変更キーのみ返す**（full state 禁止・reducer 二重化防止） |

**`merge_output` マッピング表**（inner `sub_result` → outer state）:

| inner sub_result key | outer state field | 型 |
|---|---|---|
| `impact_scope` | `impact_scope` | `dict` |
| `evidence` | `evidence` | `list[dict]`（reducer でないので置換） |
| `unknowns` | `unknowns` | `list[dict]` |
| `risk_hypotheses` | `risk_hypotheses` | `list[dict]` |
| `recommended_checks` | `recommended_checks` | `list[dict]` |
| `requires_human_review` | `requires_human_review` | `bool` |
| `status` | `status` | `str`（`AgentStatus.<X>.value`） |
| （HITL 発生時）`hitl_status` / `hitl_draft` / `subgraph_thread_id` | 同名 | schema 1.4 |

- `ECNImpactGraphNode.execute()` の override は、fail-closed 状態遷移表の bypass 条件（`status=="error"`
  or `processing_eligible==False`）でのみ `{}` を返す。それ以外は `GraphNode` の標準 subgraph 実行
  （`get_subgraph`→`extract_input`→`invoke`→`merge_output`）へ委譲する（super().execute() 相当）。
- **`error_log` / `node_history` は `Annotated[list, operator.add]` reducer**。merge_output やノード戻り値では
  **新規分のみ**返す（既存分と連結して返すと二重化）。

### State Definition（flat TypedDict, `AgentState` 継承）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `user_input` | `str` | ECN envelope（JSON 文字列: ecn 本文/添付メタ/部品 ID/tenant） | ✅（framework） |
| `validated_input` | `str` | pre_process が正規化した ECN envelope（inner へ渡す JSON 文字列） | pre 成功時のみ（fail-closed 時 optional） |
| `tenant_id` | `str` | 工場/事業部の分離キー（全 DB/KB 検索に貫通） | pre 成功時のみ |
| `normalized_parts` | `list[dict]` | 正規化アダプタ出力（共通キー + 原本 + 変換規則 + 品質結果） | pre 成功時のみ |
| `processing_eligible` | `bool` | 品質ゲート通過フラグ。`ECNImpactGraphNode` の bypass 判定に使用 | ✅ |
| `data_quality_flags` | `list[str]` | 欠損/重複/失効/参照切れ/展開不能の検知結果 | — |
| `impact_scope` | `dict` | DB で確定した影響部品/製品/ロット + 検索条件 | — |
| `evidence` | `list[dict]` | ECN/DB/KB source ID 付き根拠 | — |
| `unknowns` | `list[dict]` | BOM/AVL 未一致・添付不足・顧客要求未取得・適用時期不明 | — |
| `risk_hypotheses` | `list[dict]` | 安全性/性能/法規制/顧客承認/再検証の確認仮説（source ID 付き） | — |
| `recommended_checks` | `list[dict]` | SQE が検討する試験/確認/エスカレーション候補 | — |
| `requires_human_review` | `bool` | fail-closed / データ品質不合格 / 高リスクのフラグ | — |
| `formatted_output` | `dict` | post_process が整形した最終ブリーフ（get_output で露出） | — |
| `status` | `str` | `AgentStatus.<X>.value`（文字列）。route() が読む | ✅ |
| `hitl_feedback` / `hitl_status` / `hitl_draft` / `hitl_metadata` / `hitl_allowed` / `hitl_count` / `subgraph_thread_id` | schema 1.4 | HITL 7 フィールド（framework の HITL state schema） | HITL 時 |
| `error_log` | `Annotated[list[str], operator.add]` | framework 継承（reducer）。各ノードは**新規分のみ**返す | framework |
| `node_history` | `Annotated[list[dict], operator.add]` | framework 継承（reducer）。`__call__` が 1 件追記。ノード戻り値に含めない | framework |

> `evidence` / `unknowns` / `risk_hypotheses` / `recommended_checks` は reducer では**ない**（通常の置換フィールド）。
> reducer は `error_log` / `node_history` のみ。State は `AgentState`（flat TypedDict）を継承し、ドメイン
> フィールドを追加する。上記 fail-closed 時 optional なフィールドは `total=False` 相当（NotRequired）で宣言する。

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable types）。Pydantic/dataclass/任意 Python object 禁止（msgpack 非互換）
- JWT/API key/credential を State に置かない（checkpoint DB 漏洩）。secret は `ctx.secrets.require()`
- InvocationContext は `config["configurable"]` 経由のみ（State に格納しない・直接構築しない）
- `status` は必ず `AgentStatus.<X>.value`（文字列）で格納。`error_log`/`node_history` は reducer（新規分のみ返す）

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed）
- [x] `shared.services.database.base_db.BaseDatabase`（承認部品レジスタ DB / SQL 完全一致）
- [x] `shared.services.vector_store.base_vector_store.BaseVectorStore`（過去 ECN 履歴 KB / 意味検索）
- [x] `shared.services.llm.base_llm.BaseLLM.complete(messages)`（Stage 4-5 の影響評価・ブリーフ生成）
- [x] `shared.security.pii_detector.detect_pii`（既定 S-2 が PII_SCAN_FIELDS をマスク）
- [x] SecurityViolationError
- [x] **S-2**: `_extra_security_gate_input()`（`ECNPreProcessNode`）— テナント越境参照の検出、
      サプライヤー機密/品質不具合フィールドの許可 allowlist 検証、入力サイズ上限。
      **allowlist 方式**で業務キーを検証（denylist ではない — 未知キーを黙って通す向きに失敗するため）
- [x] **S-3**: `_extra_security_gate_output()`（`BriefPostProcessNode`）— **preservation variant + content filter**:
      (1) 承認結論/規制適合の最終判断/BOM-AVL 更新指図/対外確定回答が出力に混入していないか検出して reject
      （承認可否・書換え payload は schema から構造排除するが、LLM 由来文字列に混入し得るため二重防御）、
      (2) source ID のない `risk_hypotheses`/`recommended_checks` claim を検出して reject（根拠必須の保全検証）
- [x] **S-4**: `emit_trace_event()` — 各 `execute()` 内で最低 1 回のドメインイベント（例:
      `ecn_intake` / `register_match` / `precedent_recall` / `impact_assessed` / `brief_composed`）。
      `node_start`/`node_complete`/`node_error` は `__call__` が自動 emit するため重複させない

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `ECNPreProcessNode` / `BriefPostProcessNode` は `FunctionNode` 継承 → framework `@final` gate が
>   自動実行。ドメイン拡張は `_extra_security_gate_input/output()` のみ（`_security_gate_*` 直 override は TypeError）
> - `ECNImpactGraphNode`（`GraphNode`）→ 両ゲート no-op（inner graph 側 / 上流の gate が適用済み）
> - inner の DB/KB/LLM ノード（`FunctionNode`）→ 各々 `_extra_*` で必要な追加チェック

### Composition Pattern

- **Pattern**: Cat 2 = outer `AgentBaseGraph` + `main` slot に **GraphNode**（inner `DomainWorkflowGraph`）
- **Composition target**: `src/graph/domain_workflow_graph.py`（`src/subagents/` は不可）
- **Error propagation strategy**: **`error_strategy = "propagate"`**（inner error は `SubgraphError` として
  再送出・fail-fast）。inner graph 内の fail-closed（未一致/データ品質不合格）は error ではなく
  `requires_human_review=True` + `status=success` で正常に返し、post_process/HITL でルーティングする

## Import Isolation Confirmation
- [x] Template は agenticstar-platform SDK（Level 0）を import しない
- [x] Import 対象は framework/ と shared/ のみ（agents/base/ 不要）

## Security & Governance Design（Proposal §11 リスク対応の設計反映）

| # | リスク | 設計での対応 |
|---|---|---|
| 1 | BOM/AVL 未構造化・企業差で影響範囲を展開できない | 生 BOM 直結禁止。正規化アダプタ出力（`normalized_parts`）のみで後続処理。データ品質ゲート不合格は fail-closed |
| 2 | 過去失敗事例が承認/却下結論として誤用 | `PrecedentRecallNode` は失敗モード/反証/追加確認のみ返す。出力 schema に承認結論欄なし。過去結論コピーをテストで禁止（TC 追加） |
| 3 | 仕様差分誤読で根拠なき示唆 | claim→source ID 必須（S-3 preservation で強制）。根拠不一致は `unknowns` へ。最終規制/顧客判断を自動化しない |
| 4 | 機密情報の越境参照 | `tenant_id` を全 DB/KB 検索の必須引数に貫通（スコープ引数省略不可）。欠落は fail-closed（検索実行しない）。顧客分離はデプロイごとの DB/KB 接続・資格情報で担保する。最小入力・allowlist フィールド・参照 ID 化（S-2） |
| 6 | 出力が承認/更新指図として運用 | 承認ステータス/更新 payload/書込み credential/対外確定回答を UI/API/schema から構造排除（S-3 二重防御） |

## Security Gate 配置表（ノード別・実装フェーズの契約）

| ノード | S-2 `_extra_security_gate_input` | S-3 `_extra_security_gate_output` |
|---|---|---|
| `ECNPreProcessNode`（FunctionNode） | tenant 越境参照検出・サプライヤー機密/品質不具合フィールドの **allowlist** 検証・入力サイズ上限。`tenant_id` 未確定は reject（`status=error`） | —（出力は inner へ渡す中間物） |
| `ECNImpactGraphNode`（GraphNode） | no-op（inner FunctionNode 側で適用） | no-op |
| `RegisterMatchNode`（inner FunctionNode） | 入力は tenant_id（必須）・認可済み register・SQL 完全一致キーのみ許可（allowlist）。生 BOM 直結を reject | 展開結果の part/product/lot が tenant スコープ外を含まないか検証 |
| `PrecedentRecallNode`（inner FunctionNode） | retrieval query に tenant_id + ACL filter が必須（欠落は reject） | KB ヒットが tenant/品質フラグ/閲覧権限を満たすか検証 |
| `ImpactAssessmentNode`（inner FunctionNode） | LLM 入力の許可フィールド限定（allowlist）。生 credential/PII をプロンプトに載せない | claim schema 検証・source ID 付与の位置 |
| `BriefComposeNode`（inner FunctionNode） | — | 承認結論/規制適合最終判断/BOM-AVL 更新 payload/対外確定回答が混入していないか reject。HITL payload は `interrupt()` より前に同じ禁止内容検査と invocation-local provenance 検証を通し、未知 ID / 非 ECN・DB・KB source を公開しない。さらに後段と同じ `validate_brief_claim_coverage()` を呼び、後段 S-3 が拒否する根拠 coverage 不足の数値・比較表現を reviewer に先行公開しない |
| `BriefPostProcessNode`（FunctionNode） | — | **preservation variant**: `risk_hypotheses`/`recommended_checks` の各 claim が (1) source ID を持ち、(2) その ID が当該実行の `evidence` 集合に属し、(3) claim 種別が要求する source 種別（ECN/DB/KB）を満たすことを検証。満たさない claim は reject。数値・比較表現の根拠 coverage は `BriefComposeNode` と共有する `validate_brief_claim_coverage()` で検証する。**`unknowns` はこの規則の対象外**（根拠なしの断定ではなく「不明の明示」のため。ただし query/未一致理由の記録は必須） |

> S-2 は allowlist 方式（denylist ではない — 未知キーを黙って通す向きに失敗するため）。S-3 は
> `FunctionNode` の `@final` gate を直 override せず `_extra_security_gate_output()` で拡張する。
> **S-2/S-3 は execute が書いた state に依存しない**（gate は execute の前後で走るため、
> execute が書いたキーを読むゲートは実行順序上そもそも成立しない）。

## Runtime Config 設計（`config/config.yaml`・**設計フェーズで作成済み**）

`config/agent.yaml`（静的 manifest）とは別に `config/config.yaml`（runtime）を作成する（manifest と runtime config の分離）。
**この runtime config は設計フェーズで実ファイルとしてコミット済み**（レビュー指摘への対応）— `hitl.enabled: true`
を宣言する以上、HITL ノードが scaffold default 設定のまま silent に走ることがないよう、runtime config を
文書だけでなく committed artifact として確定させる。実際の HITL ノードロジック（`interrupt()` + `hitl_allowed`
ガード）の実装は実装フェーズのハードゲート。

```yaml
max_retry: 3
timeout_s: 30
budget_usd: 1.50          # LLM コスト上限（Stage 4-5 の 1 回あたり）
memory_enabled: true      # MANDATORY — hitl.enabled: true の前提（checkpointer）
hitl:
  enabled: true
  max_hitl: 8             # 高リスク/低 confidence/データ不合格時の SQE レビュー interrupt 上限
```

- **`hitl_allowed==False`（GraphNode 経由の自動パイプライン）時の代替**: `interrupt()` を呼ばず、
  `requires_human_review=true` + review-required ブリーフを返して正常終了する（deadlock 回避のため必須）。
- **interrupt/resume 後の route**: `HitlStatus.APPROVED/CORRECTED`→post_process、`REJECTED`→finalize
  （framework の HITL routing 契約）。`hitl_draft` で resume 時の LLM 再実行を防ぐ。
- **`/resume` 境界**: `Operation.RESUME` により middleware identity のみ許可する。suspend 応答は
  `thread_id` と server 確定 `session_id` を返し、pending registry が scope/caller/session ownership と
  single-flight を強制する。外側/内側 checkpoint の pending 状態を再確認し、
  正常完了時に登録を削除するため、完了済み thread の再 resume と並行二重 resume は拒否される。
- PB-7（HITL interrupt propagation）は `hitl.enabled: true` のため **必須**（実装フェーズで
  `test_pb7_hitl_interrupt_propagation.py` の TODO を埋める）。

## 部品指定表（共通部品ライブラリからの写経）

**部品化ルール（parts/README.md）**: 再利用は「正本コピー焼き込み（写経）」。`apply_part.py` で src へコピー・
`# PART: <id> v<ver>` マーカー刻印、同梱テストは `tests/parts/<id>/` へ。編集は各部品の `adaptation_points`
シンボル内のみ（骨格を触ると `verify_part.py` が 🔴SUSPECT 判定）。`docs/03_test_spec.md` は `parts/TESTING.md`
の該当行（層2 接合部・層3 横断 X-1〜X-5）を引き写して具体化する。**HITL 有効のため X-5 / PB-7 必須。**

| 適用部品 (version) | 適用先（ノード/層） | 主な adaptation_points |
|---|---|---|
| **scoped-retrieval v0.3.0** | inner `PrecedentRecallNode`（Stage 3・過去 ECN 履歴 KB 意味検索） | `scope_keys=("tenant_id",)`（**1軸・per-request 必須・欠落 fail-closed=status error**）、`backend`（scope-aware store の `search_scoped` を包み、保存レコード由来 scope を hit に返す）、`normalize_query`、`top_k`、allowlist projection |
| **qdrant-scoped-backend v0.1.0** | `ServiceBundle.vector_store` と `ScopedVectorBackend` の間 | `QdrantScopedVectorStore.search_scoped()` が部品の `search()` へ委譲。`config/config.yaml` の opt-in 設定と `secrets_factory(...).get("AZURE_EMBEDDING_API_KEY")` が揃う場合だけ構築し、`AsyncServiceRuntime` と backend はプロセス終了時に close する。SDK 1.0.2 の `score_threshold=0.4` は固定で変更不能。また SDK は本文を payload に保存しないため、検索結果本文は payload の `snippet`（既定先頭 400 字）を運用する |
| **cat2-graph-skeleton v0.1.5** | outer `main` = `ECNImpactGraphNode(GraphNode)` | `_build_subgraph`/`subgraph_factory`、`_parent_config`、`extract_input`、`mode_specs`、`error_strategy="propagate"`、`propagate_hitl=True`。cold-start は部品の instance lock で単一構築 |
| **llm-draft-stage v0.2.0** | inner `ImpactAssessmentNode`（Stage 4）+ `BriefComposeNode`（Stage 5）の LLM 骨格 | `config["llm"]` 注入、prompt/fallback/evidence hooks。LLM 未設定だけ deterministic degradation、provider/config/timeout/runtime は `status=error` + stable `error_code` で fail-closed、例外自由文を state/trace に載せない |
| **evidence-gate v0.3.1** | 両 LLM stage の型付き射影 + `BriefPostProcessNode` の S-3 preservation | `FindingField` / `project_typed_finding(s)` で top-level と各 finding を一階層型検証。未知キー・必須欠落・型違い・direct list item 型・enum 外を拒否。provenance/strict coverage と source 種別検証も維持 |
| **output-envelope v0.1.2** | outer `post_process` = `BriefPostProcessNode`（Stage 6・整形） | `format_invoke`（impact_brief 出力形）、`required_fields_by_mode`（fail-open 防止の必須キー）、`gate_kind_by_mode`、`_sanitize_client_echoes`（**承認結論/BOM-AVL 更新指図/対外確定回答の混入 reject**）、`max_field_chars=None` 維持。`_extra_security_gate_output` から evidence-gate の provenance 検証を呼ぶ |
| **entry-adapter v0.4.0** | `src/api/server.py` の `/invoke` と `/resume` | `SCOPE_KEYS=("tenant_id",)`、`authenticate(request, operation=...)`。token/STG default は `READ_INVOKE` のみ、`RESUME` は caller_id 付き middleware identity 必須。**payload-store junction は本件では省く**（A-5） |

### 対象外部品と理由

| 対象外部品 | 理由 |
|---|---|
| ingest-file-sanitizer v0.1.0 | 入力は ECN envelope(JSON)+正規化アダプタ出力。生ファイル sniff/品質ゲートの ingest 経路が設計に無い（生 BOM 直結禁止） |
| ingest-row-pipeline v0.1.0 | 本テンプレは KB へ書き込む ingest モードを持たない（PrecedentRecall は既存 KB 検索のみ）。checkpoint scrubber も ECN 本文を state に載せる方針（A-5）で不要 |
| payload-store v0.2.0 | 設計が「ECN 本文を payload_ref 化せず allowlist 検証のみ」（承認済 A-5）を明示選択。payload_ref 化はオーバーヘッド増として却下済み＝部品前提と正面不整合 |

> **写経の忠実性**: 上記 adaptation_points 内の編集（scope_keys 変更・ProvenanceContract フィールド名変更等）は
> `verify_part.py` の 🟡ADAPT で通る。骨格（fail-closed 判定・allowlist projection・checkpointer 分離）を触ると
> 🔴SUSPECT。実装の push 前に部品検証ツールで SUSPECT 0 を確認する。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| スコープキー retrofit（A-4 更新） | `tenant_id` + `org_id` の 2 軸を維持 | `org_id` を外し `tenant_id` の 1 軸にする | **tenant_id のみ** | 2026-08-30 の方針変更。顧客ごとに DB/KB と資格情報を分離してデプロイするため、顧客分離は接続レベルで担保する。アプリ内の必須区画キーは `tenant_id` に統一し、その欠落、検索 hit の欠落・不一致は従来どおり fail-closed とする。本決定は承認済 A-4 の 2 軸判断を置き換える |
| PB-3b（実 Qdrant 区画分離）の CI 実行 | CI で Qdrant を provision し毎パイプラインで実行（**service 追加だけでは不足** — テストが `localhost:6333` を直書きしているため接続先を設定可能にする改修も要る） | CI では Qdrant を provision せず、ローカル実行の記録を証跡とする | **CI では provision しない（現状維持）** | 設計判断（2026-08-30）。区画分離は本 AG の中心的な安全性主張であり回帰検知の価値は認めるが、shared runner の service 対応が未検証で、未検証の CI 変更を同じ改版に混ぜない方針（Dockerfile を同じ改版で削除した判断と同じ）を優先した。標準的な隔離 runner では到達不能により PB-3b は明示 SKIP となるため、現時点の証跡はローカル実行のみ（コードが SKIP を保証するわけではなく、runner 環境の性質である点に注意）。証跡は docs/03 の実行結果と docs/07「Unwired at this revision」に明記し、隠さない |
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 終了は固定パイプライン。think→act の自律ループではない（Cat 2） |
| Composition pattern | Standalone FunctionNode in main | GraphNode + inner graph | **GraphNode + inner graph** | Stage 2-5 が DB→KB→LLM→brief の多段トポロジ。inner graph に隠蔽 |
| DB vs KB 責務 | 単一 store で両用 | DB=SQL 完全一致 / KB=vector 意味検索 を分離 | **分離** | 影響範囲は DB の完全一致で確定（推測禁止）。KB は失敗モード想起のみ（結論継承しない） |
| fail-closed の表現 | `status=error` | `requires_human_review=True` + `status=success` | **後者** | 未一致/品質不合格は「異常」ではなく「人間レビューへ委譲」。post/HITL で正常ルーティング |
| HITL | 無効 | `hitl.enabled: true` + `memory_enabled: true` | **有効・D6 interrupt** | 高リスク/低 confidence/データ不合格時に SQE レビューへ interrupt（`hitl_allowed` ガード付き）。同席レビュー（ターン間隔数分）想定。`hitl_allowed=False` 時は interrupt せず review-required ブリーフを返す（承認済 A-3） |
| KB データ区画統治 | tenant_id 単一 | tenant_id + org_id 両方 fail-closed | **tenant_id 単一** | `tenant_id` は単一顧客デプロイ内のデータ区画ディメンションであり、マルチテナント SaaS の顧客分離キーではない。顧客分離はデプロイごとの DB/KB 接続と資格情報で担保する。`tenant_id` をスコープ引数省略不可の必須引数にし、区画を跨ぐ混在を fail-closed で防ぐ（上記 retrofit 決定により A-4 を更新） |
| KB backend 障害 | store 未注入時と同じ `[]` 縮退 | 監査後に例外を送出して `status=error` | **Qdrant 注入時は fail-closed** | ユーザー裁定。従来の store 未注入時は `[]` と `degradation_reason` による R-4 縮退を維持する。一方、Qdrant を明示設定した環境の接続断・timeout は、部品が scope 値や例外文を含まない監査イベントを出した後に例外を再送出し、グラフを `status=error` にする。設定済み KB の障害を「類似事例なし」と誤認しないため |
| standalone runtime config | `Graph.__init__` のハードコード既定だけを使用 | `config/config.yaml` を `load_config` して Graph へ渡す | **設定ファイルを実配線** | 従来の `src/api/server.py` は `config/config.yaml` を一度もロードせず、`wire_services()` も `config=None` を渡していたため runtime config が飾りになっていた。本改版で scaffold と同じ `load_config` 経路を追加し、HITL・timeout・Qdrant opt-in を実構築へ反映する |
| 入力機密の扱い | ECN 本文を payload_ref 化 | allowlist 検証のみ（本文は state に載せる） | **allowlist のみ** | DB/KB 分離設計のため機密の主体は DB 側で境界管理。ECN 本文は機密だが個人 PII ではなく、payload_ref 化はオーバーヘッド増（承認済 A-5） |
