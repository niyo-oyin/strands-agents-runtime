# 機能ロードマップ

- 作成日: 2026-10-22、改訂: 2026-10-23（議論を反映）
- 前提: フェーズ 1（ローカル・Docker・実モデルでの確認、呼び出し契約の強化、CDK の `synth` まで）が完了し、本番投入はまだ先の開発初期にある。
- 根拠: 同系統ランタイムの機能調査（付録参照）、Strands SDK 1.57〜1.62 の変更履歴、AgentCore の各サービス、本ランタイムの実装状況。

---

## 1. 目的の再確認

本ランタイムの目的は最初から一つで、**同じエージェント定義（TOML + Markdown）と同じ成果物が、ローカルでも Docker でも AgentCore Runtime でも動くこと**である。
ロードマップはこの目的を損なわない範囲で、Strands SDK が既に持つ機能を定義ファイルから使えるようにし、呼び出し契約を運用に耐えるものにしていく。

調査で確認した周辺の存在は次のように位置づける。

- **AgentCore Harness**（AWS のマネージド機能、GA）: `harness.json` でモデル・プロンプト・ツール・スキル・メモリを宣言できる。AWS に閉じてよい用途なら選択肢になる。本ランタイムとは「どこでも動くか」で使い分ける関係であり、競合ではない。
- **`strands-harness`**: コーディングエージェント向けの既定（shell / read / write / edit、todos）を持つファクトリ。本ランタイムはモデル解決にだけ使い、既定の組み込みツールは持ち込まない。
- **LangSmith Deployment、Google ADK、OpenAI Agents SDK、Microsoft Agent Framework、Pydantic AI、Mastra、CrewAI、Letta**: 機能の参照点。特にジョブ（バックグラウンド実行）、同一スレッドの同時入力ポリシー、評価、長期記憶、耐久実行の扱いを参考にする。

---

## 2. 観点

1. **同系統の提供機能**: 上記ライブラリとプラットフォーム。
2. **Strands の未活用機能**: 1.57〜1.62 で追加され、本ランタイムが TOML から触れていないもの。
3. **潮流（2026）**: MCP 2026-07-28 仕様（ステートレス化、Tasks 拡張）、A2A 1.0、Agent Skills、コンテキストエンジニアリング、評価の常態化、ポリシー・アズ・コード、耐久実行。
4. **運用要件**: トークン数の計上、監査、リプレイ、版管理、PII 抑制、シークレット、ヘルスとグレースフルシャットダウン。

---

## 3. 現状と未活用機能の対応表

| 領域 | 本ランタイムの現状 | Strands / AgentCore にあるが未活用 |
|---|---|---|
| モデル | `provider/id` + effort、`openai-chat` 独自、`model.config` | `ModelRouter`（フォールバック・分類器）、`aux_model`、`CacheConfig`（TTL・キー） |
| ツール | 文字列参照、MCP（stdio / http）、Skills、vended | MCP OAuth、`make_mcp_router`、Gateway、Code Interpreter / Browser、`Sandbox`（Docker / SSH） |
| 制御 | `limits`（turns / tokens / timeout）、`interventions` のモジュール参照と `.cedar` | `HumanInTheLoop`（LLM 分類器付き）、steering、ミドルウェア |
| 記憶 | セッション（local / s3 / AgentCore Memory STM）、`context_manager` 文字列 | `MemoryManager` + ストア、AgentCore Memory の長期戦略、`agentic` コンテキスト管理、offloader、stash |
| 実行 | 同期・SSE、キャンセル、タイムアウト、同一セッション 409、HITL 再開 | `background_tasks`、checkpoint / snapshot による再開、`agent.shutdown()`、`idempotency_token` |
| マルチエージェント | agents-as-tools | `make_subagent`（presets / authority）、Graph、Swarm、A2A サーバー / クライアント、`multiagent_*` イベント |
| 観測 | OTel スパン（SDK）、`trace_attributes`、ログファイル、`metrics` | span redaction、`gen_ai_span_attributes_only`、baggage 伝播 |
| 構成管理 | Git + `validate`、Runtime ごとの `runtime.toml` | Configuration Bundles（版付き上書き、A/B） |

---

## 4. ロードマップ

優先度は「開発初期の日々の体験に効くか」「契約（ペイロードとイベント）に影響するため早く決めるべきか」「実装コスト」で付けた。AWS アカウントが要るものは B に分けた。

### A. AWS 非依存

**A1. モデルのフォールバックとキャッシュ設定**（実装済み 2026-10-23）

```toml
[model]
id = "openai-chat/deepseek/deepseek-v4.1-flash-1017"
fallbacks = ["openai-chat/xiaomi/mimo-v2.6-pro"]   # ModelRouter + FallbackStrategy
[model.cache]
ttl = "1h"                                          # CacheConfig
```

OpenRouter や互換サーバーは落ちることがあり、フォールバックは最初に欲しくなる。`ModelRouter` は `Agent(model=)` にそのまま渡せる。`effort` は候補ごとに `resolve_model` で解決する。

**A2. トークン数のテレメトリ**（実装済み 2026-10-23）

Strands が集計する入出力トークン（キャッシュ読み書き含む）を、サブエージェント込みの合計とエージェント別内訳（使ったモデル、呼び出し回数）で `result.metrics` に載せ、同じ値と `actor_id` を OTel の現在のスパン属性に付ける。`model_failures` でフォールバックの発生も分かる。取れないプロバイダーの値は 0 のまま許容し、料金換算はしない。フォールバック候補ごとの内訳は Strands の `ModelRouter`（provisional）が served candidate を公開していないため未対応。

**A3. ジョブと進捗の契約設計**（中、契約に影響するため早く決める）

1 回の呼び出しが 1 時間以上走る可能性は否定できない。AgentCore の同期呼び出しは 15 分、ストリーミングでも 60 分が上限で、接続を保ち続ける設計は成り立たない。
一方、AgentCore の HTTP 契約は `/invocations` と `/ping` だけなので、非同期は `/invocations` のペイロードで表現するしかない。

設計の骨子（詳細は設計文書 §7.6）:

- **ジョブ = リクエストを超えて生きる呼び出し**。`{"mode": "job"}` のような指定で受け付け、即座にジョブ ID（= `session_id` に紐づく）を返す。
- 実行はプロセス内のバックグラウンドタスクで続け、`/ping` を `HealthyBusy` にしてセッション（AgentCore では microVM）を生かす。
- **進捗と結果はセッションストレージに書く**。同じ `session_id` への後続呼び出し（`{"mode": "status"}` / `{"mode": "result"}`）で取り出す。ローカルでもプロセスが生きていれば同じ契約で動く。
- 進捗の単位は既存のイベント（`tool_use`、`tool_result`、後述の `node` 系）をそのまま使い、同期ストリームと非同期ジョブで同じ語彙にする。
- 同一セッションの同時呼び出しは今は 409 固定。`status` / `result` / キャンセルの呼び出しは例外として通す。

**A4. マルチエージェントの `kind` と進捗イベント**（中、A3 の上に載せる）

- Graph / Swarm を `kind = "graph" | "swarm"` で宣言する（設計文書 §8 の予約どおり）。`SnapshotSessionManager` が Graph / Swarm にも対応したため、セッション永続化は共通化できる。
- Strands の `multiagent_node_start / node_stop / handoff` を `node_start` / `node_stop` / `handoff` イベントとして契約に加え、「フローのどの段階にいるか」を同期・非同期のどちらでも返す。
- 現在の agents-as-tools でも、子の実行は `tool_use` / `tool_result` に畳み込まれて見えない。ネストした実行の見せ方（親子関係を示す `parent` 属性など）を A3 と同時に決める。
- 動的な子生成（`make_subagent` の presets / authority）はその後。

**A5. ツール呼び出しの制限**（小）

```toml
[guard]
deny = ["shell"]                      # 定義上は存在しても呼ばせない
policy = "./policy.cedar"             # CedarAuthorization（既存の interventions を統合）
classifier = "llm"                    # 任意: 曖昧なケースを LLM が判定し、必要なら interrupt で人に確認する
```

人への確認は既存の `interrupt` / `interruptResponse` 契約に乗せ、誰が承認するかはクライアントの責務とする。
`classifier` を有効にする定義者は「人への確認が来る場合がある」ことを理解したうえで選ぶので、ランタイムはその経路（Strands の `HumanInTheLoop`）を提供するだけでよい。

**A6. 長期記憶とコンテキスト管理の宣言**（要求が具体化してから）

```toml
[memory]
store = "file"            # file | bedrock_kb | agentcore
namespace = "{actor_id}"
```

`MemoryManager` とストアの配線。`actor_id` を名前空間にする設計は AgentCore Memory の actor スコープと一致する。`context_manager` の offload / stash プリセットも同時に。

**A7. セッション保存先の拡張点**（小）

`[session] backend = "module"` と `storage = "pkg.mod:make_storage"` で、Strands の `Storage` 実装（Azure Blob、GCS、Redis など）をモジュール参照で差し込めるようにする。ECS や Azure など S3 を前提にできない基盤でセッションを永続化するために要る。

**A8. 運用の基礎**（必要になった順に）

- グレースフルシャットダウン: SIGTERM で新規受付を止め、進行中の呼び出しに `cancel_signal` を立て、`Agent.shutdown()` と MCP クライアントの終了を待つ。
- `/ping` の `HealthyBusy`（A3 の前提）。
- 監査ログ: ツール呼び出しと判定を 1 行 JSON で記録。
- トレースの秘匿: `gen_ai_span_attributes_only` と span redaction を `[telemetry]` から選ぶ（送信先ではなく「何を送るか」の方針なので TOML に置く）。
- セッションの保持期間: `local` / `s3` セッションの TTL と掃除コマンド。

**評価について**

プロンプトや定義の回帰テスト（入力と期待を実モデルで流して採点する）は、同系統のライブラリが一斉に持ち始めた機能だが、ランタイム本体には入れない。
必要になれば `strands-agents-evals` をそのまま使い、足りなければ別パッケージとして作る。ランタイム側で用意するのは、評価から呼びやすいクライアント（既存の `HttpClient`）だけでよい。

### B. AWS 連携（アカウント取得後）

- **B1. AgentCore Identity による外部資格情報**: `api_key_env` に加え `api_key_provider` を受け、`@requires_api_key` / `@requires_access_token` で取得したトークンを MCP ヘッダーやモデルクライアントに渡す。環境変数のサイズ制限（V2 で 1.5〜2.5 KB）を根本的に解く。
- **B2. Direct Code Deploy と Runtime V2**: 設計文書 §11.4 / §11.6。実機で確認して既定を決める。
- **B3. Gateway をツールソースとして**: `[mcp_servers.gateway] url = ...` と B1 の認証で接続。意味検索・レート制限・Policy は Gateway に委ねる。
- **B4. AgentCore Memory の長期戦略**: A6 の `store = "agentcore"` と CDK の `Memory` 戦略。
- **B5. Configuration Bundles**: リクエストの `baggage` で指定された版を取得し、TOML が「上書き可能」と宣言したキーにだけ適用する。クライアントによる上書き禁止と矛盾しない運用者の経路。

### C. プロトコル面

- **C1. A2A サーバーモード**: `[server] protocol = "a2a"`。Runtime を分割する運用が始まるときのエージェント間連携の正規ルート。
- **C2. MCP 2026-07-28 への追従**: Strands 側で対応済み。`validate --mcp` と結合テストで確認する。
- **C3. 双方向ストリーミング（音声）**: 要求が出た時点で `kind = "bidi"` として別設計。

---

## 5. やらないこと

- コーディングエージェント向け組み込みツールの同梱（`strands-harness` の領分。必要なら `strands.vended_tools` を `tools` に書く）。
- 独自の評価フレームワーク、独自のサンドボックス、独自のゲートウェイ。
- UI（対話画面、管理画面）。
- 料金換算。
- ランタイム内のスケジューラ（cron は EventBridge 等に任せる）。
- 環境変数による設定値の上書き（ADR-6）。

---

## 6. 推奨する着手順

1. ~~A1 フォールバック + A2 トークン数~~（実装済み）。
2. **A3 ジョブと進捗の契約設計**。実装の前に設計文書で契約を確定する。長時間実行とマルチエージェントの両方がここに依存する。
3. **A4 Graph / Swarm と進捗イベント**。A3 の契約の上に載せる。
4. **A5 ツール制限、A6 長期記憶、A7 セッション保存先の拡張点、A8 運用の基礎**は要求が具体化した順に（ECS や Azure に載せる話が出たら A7 を先に）。
5. AWS アカウント取得後に **B1 → B2 → B3 → B4 → B5**。
6. **C1 A2A** は Runtime を分割する運用が始まるときに。

---

## 付録: 調査の要点（2026-10-22）

- **AgentCore Harness**: マネージドな宣言的 Strands エージェント（GA）。`maxIterations`（既定 75）、`timeoutSeconds`、`maxTokens`、不変バージョンと名前付きエンドポイント、コスト配分タグ、Step Functions からの `InvokeHarness`。双方向ストリーミング、Graph / ワークフロー、フレームワーク選択は不可（AWS の比較ページ）。
- **LangSmith Deployment**: assistants / threads / runs、バックグラウンド実行と cron、同一スレッドの同時入力ポリシー（reject / enqueue / interrupt / rollback）、HITL と time travel、TTL、MCP / A2A での公開、Prompt / Context Hub。
- **Google ADK**: YAML 設定は実験的で Gemini 限定。評価（ユーザー・環境シミュレーション）、Memory Bank（非同期抽出、TTL）。
- **OpenAI Agents SDK**: ガードレール、ハンドオフ、HITL、サンドボックスエージェントとマニフェスト。
- **Microsoft Agent Framework 1.0**: YAML の宣言的エージェントとワークフロー（ループ、HITL 承認、チェックポイント再開）。
- **Pydantic AI**: Temporal / DBOS / Prefect の耐久実行、Evals（トラジェクトリ評価）、Gateway（フェイルオーバー、支出上限）。
- **Mastra**: suspend / resume、スケジュール、評価の gates、JWT / FGA 認証。
- **Strands 1.57〜1.62**: ミドルウェア公開、`agent.cancel()`、`Agent.shutdown()`、使用量集計、MCP 2.x 対応、`make_subagent`、vended `swarm`、BidiAgent GA、baggage 伝播、Strands Box（`box.toml`、Dogwood ポリシー）。
- **MCP 2026-07-28**: ステートレス（`server/discover`）、MRTR、Tasks 拡張、`ttlMs` 付きの一覧キャッシュと決定的なツール順序、`_meta` での `traceparent` / `baggage`。
- **A2A 1.0**（2026-04-09）: マルチテナンシー、署名付き Agent Card、150 超の組織。
- **運用機能の提供元**: トークン計上（Harness のタグ、Pydantic AI Gateway）、レート制限（AgentCore Gateway）、リプレイ（LangSmith、Mastra）、版管理と A/B（LangSmith Hub、Configuration Bundles）、評価ゲート（Mastra、Strands Evals CLI）、保持期間 TTL（LangSmith、Memory Bank）。
