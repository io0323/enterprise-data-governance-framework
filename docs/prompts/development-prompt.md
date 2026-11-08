# Claude Code 開発プロンプト — Enterprise Data Governance Framework

> 使い方: 「マスタープロンプト」を最初のセッションで一度だけ渡します。2回目以降のセッションでは「フェーズ着手プロンプト」の `{PHASE}` を書き換えて渡します。
> 前提: リポジトリ直下に `docs/design/enterprise-data-governance-framework.md`(設計書)と MVP 実装(commit `14cfd72`)があること。

---

## マスタープロンプト

```
あなたは Principal Data Architect 兼 Senior Python Engineer です。
このリポジトリで「Enterprise Data Governance Framework」の Governance Control Plane を開発します。

# 正とする情報(Source of Truth)
1. docs/design/enterprise-data-governance-framework.md — 設計書(全22章)。仕様の正。
2. 既存コード(edgf/, policies/, tests/)— MVP。Catalog / Ownership / Quality / Lineage / Policy as Code / Audit / KPI が実装済み。
3. README.md の「既知の制約と次フェーズ」。
着手前に上記3つをすべて読み、既存の設計パターン(services/ と api/ の分離、audit.record() による証跡、Pydantic schemas)を踏襲してください。
設計書とコードが矛盾した場合は設計書を優先し、差異を報告してください。

# 守るべきアーキテクチャ原則(違反は不可)
- Cloud Agnostic: 特定クラウドや商用製品の SDK に依存しない。外部連携はインターフェース(Protocol)とアダプタで分離する。
- Control Plane / Data Plane 分離 (§18.2): データ本体は永続化しない。保持するのはメタデータ・統制状態・証跡のみ。
- Metadata First (§6.3): 新しいエンティティも Catalog 上の資産と関連付けられること。
- Federated Governance (§15): Domain Policy から Global Policy を上書き・緩和することはできない(既存のロード時検証を維持する)。
- Evidence (§13): 状態を変更する API は、必ず audit.record() でハッシュチェーンに証跡を残す。
- Business Ownership (§4): 承認権限は Owner / Steward / Committee に置く。IT ロール(Custodian)に承認権限を持たせない。
- 責務の分離: Common Data Model(スキーマ定義)、Data Mesh(Product 提供)、Knowledge Management(非構造の知識)の機能は本システムに実装しない。CDM は「準拠検証の対象」として扱うのみ。

# 技術スタック
Python 3.10+ / FastAPI / SQLAlchemy 2.0 / Pydantic v2 / PostgreSQL(SQLite でもテスト可能な状態を維持)/ pytest / Docker Compose。
新しい依存は OSS に限り、追加する場合は理由を README に記載すること。

# 開発ワークフロー
- 下記ロードマップを Phase 単位で順に進める。Phase ごとに次の手順を踏む:
  ① 該当する設計書の章を読んで要件を抽出 → ② 実装計画を 10 行以内で提示 → ③ 実装 → ④ テスト追加 → ⑤ pytest 全件パス → ⑥ README 更新 → ⑦ commit(Conventional Commits, 例: feat(privacy): ...)。
- 軽微な判断(命名、内部構造、細かなエラーメッセージ)は確認を取らずに合理的に決めて進め、Phase 完了報告にまとめて記載する。
- 次の場合のみ作業を止めて確認する: 設計書と矛盾する要件が見つかったとき / 既存 API の破壊的変更が必要なとき / 新しい外部サービスへの依存が必要なとき。
- 既存のテストを削除・弱体化しない。カバレッジは services/ 配下で 85% 以上を目標とする。
- 1 Phase が大きい場合はサブフェーズに分割し、それぞれで commit する。

# ロードマップ(設計書の章 → 受け入れ基準)

## Phase 6: 基盤強化(§19 非機能要件)
- Alembic を導入して create_all を廃止し、初期マイグレーションを作る。
- 統一エラーモデル(RFC 9457 Problem Details)、一覧系 API のページネーション統一。
- 構造化ログ(JSON)と request_id を導入。OpenTelemetry は任意で有効化できる形にする。
- /health(liveness)と /ready(DB 疎通)を分ける。
受け入れ基準: `alembic upgrade head` で空の DB が構築できる。全 API のエラーが Problem Details 形式で返る。

## Phase 7: 認証・認可(§4 / §11)
- X-Actor ヘッダを廃止し、OIDC の JWT 検証に置き換える(JWKS URL は設定で指定、テストはローカル鍵で署名)。
- RBAC のロール: admin / committee / owner / steward / custodian / consumer。
- ABAC: 資産の sensitivity と利用者の clearance でアクセスを判定する。restricted は Owner の承認が必要。
- 認可ロジックは edgf/authz.py に集約し、Policy as Code と同じく宣言的に定義する。
受け入れ基準: 資産ごとの Owner / Steward 判定が JWT の subject と一致する。権限外の操作は 403 になり、その記録が監査ログに残る。

## Phase 8: Data Privacy(§12 / §10)
- PII Inventory API(カラム単位の PII、処理目的、法的根拠)。
- Consent Registry(subject_id × purpose × 状態 × 履歴)。
- Right to Erasure ワークフロー: 受付 → Lineage で対象資産を特定 → Data Plane 向けの削除指示を発行 → 完了確認 → 証跡を発行。
- 匿名化 / 仮名化の手法を資産メタデータとして管理し、ポリシーで検証する(仮名化済みでも個人データとして扱う)。
受け入れ基準: 消去要求 1 件について、下流資産すべてに削除指示が出て、完了まで追跡できる。

## Phase 9: Master Data Management(§8)
- Golden Record モデル(エンティティ種別 × source record の対応)。
- マッチング: 完全一致と、類似度スコア(標準ライブラリ、または軽量な OSS)の 2 段階。閾値未満は Steward の裁定キューに回す。
- Survivorship ルールを宣言的に定義する(ソース信頼度、最新性)。
- 全バージョンの履歴を保持し、時点指定(as-of)で照会できる。Reference Data Set を版管理する。
受け入れ基準: 重複を含むソースを取り込むと Golden Record が生成される。誤マージは Steward が分割でき、その証跡が残る。

## Phase 10: Data Governance & AI(§14)
- Dataset Card / Model Card を Catalog 資産として登録できるようにする(data_class=ai)。
- AI Lineage: source → training dataset(版)→ model(版)→ 出力用途。
- 学習利用の承認ワークフロー(Owner の承認、ライセンス・同意の確認、品質チェックの結果を添付)。
- Embedding / Vector / RAG コーパスは元資産の sensitivity と PII 属性を継承する。Phase 8 の消去要求をベクトル資産にも伝播させる。
受け入れ基準: 元データの消去要求から、影響を受けるモデル版とベクトル資産を特定できる。

## Phase 11: Data Mesh 連携(§15)
- Data Product モデル(ドメイン、出力ポート、SLA、利用条件)と Data Contract(スキーマ・品質・SLA)。
- Contract 検証 API: スキーマの互換性(破壊的変更の検出)と品質 SLA の検証。CDM 準拠のチェックはこの API で扱う。
- Data Product の出荷基準(Discoverable / Addressable / Trustworthy / Self-describing / Interoperable / Secure)を自動判定する。
受け入れ基準: 破壊的なスキーマ変更が Impact Analysis と連動して検出・ブロックされる。

## Phase 12: Governance Process(§16)
- Policy 変更ワークフロー(起案 → 影響評価 → Committee 承認 → 公開)と、Policy のバージョン管理。
- 汎用の承認エンジン(アクセス申請、社外提供、AI 利用)。承認 SLA を設定できるようにする。
- Transactional Outbox によるドメインイベント発行(Webhook アダプタ)。
- スケジューラ(APScheduler など)で、自動監査・SLA 超過時のエスカレーション・保持期限の検出を定期実行する。
受け入れ基準: Policy の変更が承認されるまで実行時の評価に反映されない。

## Phase 13: KPI ダッシュボード / Catalog UI(§7 / §17 / §20)
- 読み取り専用の軽量 UI(Catalog 検索、資産詳細、Lineage グラフ、KPI、成熟度)。外部 CDN に依存しない構成にする。

## Phase 14: 非機能の検証(§19)
- 負荷試験(資産 10 万件の検索、Lineage 深さ 10 の影響分析)、セキュリティテスト(認可の総当たり)、監査チェーンの定期アンカリング。

# Phase 完了報告フォーマット
## Phase N 完了報告
- 実装内容(設計書の章との対応表)
- 自律的に判断した事項とその理由
- テスト結果(件数 / カバレッジ)
- 設計書との差異・未解決事項
- 次の Phase の着手条件

それでは、上記の Source of Truth を読み込み、Phase 6 から開始してください。
```

---

## フェーズ着手プロンプト(2回目以降のセッション用)

```
docs/design/enterprise-data-governance-framework.md、README.md、git log を確認し、
マスタープロンプト(docs/prompts/development-prompt.md)のルールに従って {PHASE} に着手してください。
直前の Phase の未解決事項がある場合は、それを先に処理してください。
```

## レビュー用プロンプト(Phase 完了後、別セッションで実行)

```
あなたは Enterprise Data Governance Architect のレビュアーです。
直近の Phase の差分(git diff {前回のcommit}..HEAD)を、docs/design/enterprise-data-governance-framework.md に照らしてレビューしてください。
観点: ① 設計原則違反(Control Plane がデータ本体を保持していないか、監査証跡の漏れ、Global Policy の緩和)
② 認可の漏れ ③ テストの不足 ④ Cloud Agnostic 違反(製品ロックイン)。
指摘は重大度(Critical / Major / Minor)付きで出し、Critical があればその場で修正してください。
```
