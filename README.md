# Enterprise Data Governance Framework — Governance Control Plane

「Enterprise Data Governance Framework 設計書」の実装です。製品に依存しない(Cloud Agnostic)**Governance Control Plane** として、
メタデータ・統制の状態・証跡だけを持ち、**データ本体は持ちません**(設計書 §18.2)。

## 変更点(Phase 17)

**UI のログインを BFF に置き換え、UI から書き込めるようにしました。** Phase 13 のブラウザ側の OIDC(Authorization Code + PKCE)は廃止しました。サーバが OIDC の機密クライアントとしてログインを処理し、ブラウザにはトークンを渡しません。

| 対象 | 変更 |
|---|---|
| IdP のクライアントの登録 | **公開クライアントから機密クライアントに変わります。** リダイレクト URI は `<UI のオリジン>/ui/auth/callback` です(`callback.html` は廃止)。秘密は `EDGF_UI_CLIENT_SECRET` で渡します(production で UI を有効にするなら必須で、無ければ起動しません)。設定の手順は「[OIDC のクライアントの設定](#oidc-のクライアントの設定)」 |
| `/ui/config.json` | `{"configured": true/false}` だけを返します(issuer、client_id、scopes、audience は返さなくなりました。UI の内部の API です) |
| 新規 | `GET /ui/auth/login`、`GET /ui/auth/callback`、`POST /ui/auth/logout`、`GET /ui/session` |
| `/api/v1` | Bearer に加えて、セッションの Cookie(`__Host-edgf_session`)を受け付けます。両方が付いていたら 400(`ambiguous-credentials`)。Cookie の経路の GET 以外は CSRF を確認します。Bearer の経路の挙動は変わりません |
| `POST /policies/exceptions/{id}/decision` | **却下(`approve: false`)には `comment` が必要になりました**(空なら 422)。他の判断の API と揃えました(承認済みの挙動の変更です) |
| `GET /approvals/inbox` | 各案件に `operations`(判断を送る先の既存の API と入力の項目)を追加しました。承認を終えて公開を待つ Policy の変更も、Committee に `approval` の案件として出します |
| 監査 | UI の経路の操作は、payload に `channel: "ui"` とセッション ID のハッシュの先頭(`session`)が入ります。`ui.login` と `ui.logout` を記録します |
| ジョブ | `ui_session_cleanup`(1 時間ごと)を追加しました |
| CSP | `connect-src 'self'`、`form-action 'self' <issuer のオリジン>` |
| マイグレーション | 0035(`ui_sessions`、`ui_login_states`) |

既存の環境で UI を使うには、IdP のクライアントを機密クライアントに変更し、`EDGF_UI_CLIENT_SECRET` を設定してから起動してください。UI を使わない(`EDGF_UI_OIDC_CLIENT_ID` が空の)環境は、`exception` の却下の理由のほかに影響はありません。

## 変更点(Phase 16)

**カラム単位のリネージを追加しました。** 既存の API の応答の項目は変えず、項目と API を追加しただけです。columnLineage facet を含まない OpenLineage のイベントの結果は従来と同じです。

| 対象 | 変更 |
|---|---|
| `POST /lineage/openlineage` | 出力の `facets.columnLineage` を取り込みます(受け付け範囲は「[カラム単位のリネージ](#カラム単位のリネージ9)」)。facet があるときだけ `lineage.column_write` が必要で、応答に `column_edges` と `warnings` が加わります |
| 新規 API | `POST /lineage/column-edges`、`GET /lineage/{name}/columns`、`/columns/{column}/upstream`、`/downstream`、`/impact` |
| `POST /mesh/contracts/{id}/check` | `column_impact` を追加(既存の項目は変えていません) |
| Catalog | スキーマのカラムに `cde`(既定 false)。`is_cde` は「CDE のカラムを含む」で、`cde` のカラムがあれば true になります。`cde` のカラムがあるのに `is_cde: false` を指定すると 422 |
| Contract / ODCS | カラムに `cde`。ODCS の `criticalDataElement` で取り込み・書き出しします |
| 消去 | 要求の詳細と消去証明に `lineage_exclusions`、`ai_impact` に `lineage_excluded_asset_ids`(どちらも信頼できる生成元の宣言が無ければ空) |
| AI の継承 | `GET /ai/assets/{id}/inheritance` に `pii_columns` と `pii_excluded`。保存値(`inheritance`)には、値があるときだけ加わります |
| KPI | `cde_column_lineage_rate`(ダッシュボードの目標 100%) |
| Policy | LIN-002、PRV-006 と `policies/lineage/trusted_producers.yaml`(既定は空)。**既存の環境では、Phase 12 の Policy 変更の承認(`POST /policies/reload` の提案 → Committee の承認 → 公開)を経て適用してください。** 承認されるまで、LIN-002 と PRV-006 は評価されず、消去・AI の版・PII の継承は従来どおりデータセット単位です |
| マイグレーション | 0033(`column_lineage_edges`)、0034(取り込みの経路と主体、消去で外した資産の記録) |

## 変更点(Phase 15)

**成熟度 Level 3 の判定の指標を変えました(確定事項の変更です)。** これまでは Catalog 登録率を計測できなかったので、メタデータ完備率で代替していました。Phase 15 から Data Plane の資産一覧で未登録の資産を検出できるので、**Catalog 登録率(登録済み ÷ 検出された資産)が 95% 以上**であることを条件にします(§1.8、§20)。

資産一覧が届いていない環境では Catalog 登録率は `null` で、**Level 3 には上がりません**(これまで Level 3 だった環境も Level 2 になります)。Data Plane から資産一覧を送ると判定できるようになります。

| 対象 | 変更 |
|---|---|
| `GET /kpi`, `/kpi/domains`, `/kpi/history` | `catalog_registration_rate`、`discovered_asset_count`、`unregistered_asset_count`、`usage_active_assets_30d`、`usage_query_count_30d`、`unused_asset_count` を追加。`governance_activity_assets_30d` は互換性のために残します |
| 成熟度(`maturity`) | Level 3 の条件を `metadata_completeness` から `catalog_registration_rate` に変更 |
| ダッシュボード | Catalog 登録率を表示し、Usage を利用実績の指標に切り替え |
| Policy | `MESH-007`(Addressable: 到達の報告)を `policies/global.yaml` に追加。**既存の環境では、Phase 12 の Policy 変更の承認(`POST /policies/reload` の提案 → Committee の承認 → 公開)を経て適用してください。** 新規の環境では最初の取り込みで有効になります。必須の出荷基準(MESH-001〜006)には含めないので、承認の前に Product の公開は止まりません |
| 全 API | `Idempotency-Key` ヘッダを受け付けます(送信系の再送の重複の防止)。付けなければ従来どおりです |

## 変更点(Phase 14)

**ABAC の漏れを修正しました(セキュリティの修正として承認済みの挙動の変更です)。** 閲覧できない資産の**名前**は、Lineage 系のすべての API で返さなくなりました。

| API | 変更 |
|---|---|
| `GET /lineage/{name}/upstream`, `/downstream` | 閲覧できない資産の名前を不透明なラベル(`redacted-1` など)に置き換え、接するエッジの `job` と `transformation` を `null` にします。項目は削っていません。**非推奨**で、`Deprecation` と `Link`(`rel="successor-version"`)ヘッダで `/graph` を案内します |
| `GET /lineage/{name}/graph`, `/impact`、`POST /mesh/contracts/{id}/check` の `impact` | 名前と深さではなく、ラベルと深さだけを返します。通知先の Owner(`owners_to_notify`)とリスクは引き続き返します |
| `GET /ai/lineage/models/{id}`, `/ai/lineage/assets/{id}/models` | 同様にラベルに置き換えます。`origins` と `upstream_edges` で名前が出ていたのも修正しました |
| `GET /kpi/domains` | 閲覧できる資産があるか所属するドメインだけを返し、集計も閲覧できる資産の範囲で行います |

ラベルは応答ごとに振り直す番号で、応答の外では意味を持ちません。

次の変更は、不正な入力の拒否と追加の情報だけで、正常な利用には影響しません。

| 対象 | 変更 |
|---|---|
| 全 API | 本文が `EDGF_MAX_BODY_BYTES`(既定 10 MiB)を超えると 413(`payload-too-large`)。JSON の入れ子が 64 段を超えると 422(`json-too-deep`。これまでは応答の直列化で 500 になることがありました) |
| jwt モード | 16 KiB を超えるトークンは署名の検証の前に 401 |
| `GET /audit/verify` | アンカリングを使っているときだけ、`audit.read` を持つ利用者に `chain_valid` と `anchor` を追加で返します。`valid` はアンカーとの不一致でも false になります |
| `GET /audit/anchors` | 新規(`audit.read`) |
| ジョブ | `audit_anchor` を追加しました(既定で 1 時間ごと。`EDGF_ANCHOR_ENABLED=true` のときだけ動作します) |

## 変更点(Phase 12)

**`POST /policies/reload` の意味が変わりました(承認済みの挙動の変更です)。** これまではディスクの内容を即時に反映していましたが、**提案を作るだけ**になりました(`202`、応答は `proposal_version` と `approval_id`)。承認と公開を経るまで、実行時の評価は変わりません。**Policy を即時に反映する手段はありません。** 詳しくは「[Governance Process](#governance-process16)」を参照してください。

## 実装範囲(成熟度 Level 2〜3 相当)

| 設計書 | 機能 | API (`/api/v1`) |
|---|---|---|
| §3 / §6 / §7 | Catalog登録・横断検索(用語・タグ含む)・分類・機密度・PII自動推定 | `/assets`, `/domains` |
| §4 | Owner / Steward / Custodian の割当、Owner不在(Unknown Owner)の検出 | `/assets/{id}/ownership`, `/assets?orphan=true` |
| §6.2 | Business Glossary(用語とデータ資産の紐付け) | `/glossary/terms` |
| §7 | Certificationゲート(Owner・品質SLA・高重大度ポリシーを満たすこと) | `/assets/{id}/certification[/check]` |
| §10 | ライフサイクルの状態遷移、Legal Hold、保持期限切れの検出 | `/assets/{id}/lifecycle`, `/assets/retention-due` |
| §5 | 7ディメンションの品質ルール・Quality Score・閾値割れ時のIssue自動起票 | `/quality/...` |
| §16 | Issue管理・エスカレーション(Steward→Owner→Committee)・SLA超過時の自動エスカレーション | `/issues` |
| §9 | OpenLineage互換の取込(columnLineage facet を含む)・上流/下流トレース・影響分析(通知先Owner・リスク判定)・カラム単位のリネージと影響分析・CDE のカラム化(LIN-002) | `/lineage/...` |
| §13 / §15 | Policy as Code(Global/Domain YAML、上書き禁止)・自動監査・期限付き例外の承認(職務分離あり) | `/policies/...` |
| §13 / §19 | ハッシュチェーン監査ログ・改ざんの検知・Ed25519 のアンカリング | `/audit/events`, `/audit/verify`, `/audit/anchors` |
| §17 / §20 | KPI(全社・ドメイン別)・成熟度の目安 | `/kpi`, `/kpi/domains` |
| §19 | スキーマ版管理(Alembic)・統一エラーモデル(RFC 9457)・ページネーション・構造化ログ/request_id・liveness/readiness | `/health`, `/ready` |
| §12 / §10 | PII Inventory(カラム単位)・Consent Registry・Right to Erasure ワークフロー・匿名化/仮名化の管理(PRV-003〜005) | `/privacy/...` |
| §8 | Golden Record(値は持たず、xref・由来・版の情報のみ)、2 段階のマッチング、Survivorship、裁定キュー、as-of、Reference Data の版管理 | `/mdm/...`, `/reference-data/...` |
| §14 | AI 資産(Dataset / Model Card、版の情報)、学習利用の承認、AI Lineage、統制属性の継承、RAG の判定、消去の伝播と是正、インシデント | `/ai/...` |
| §15 | Data Product、Output Port、Data Contract(互換性の判定、メジャー版の更新)、利用登録、CDM の取り込みと準拠の検証、出荷基準(DATSIS)、ODCS の取り込みと書き出し | `/mesh/...` |
| §16 | 汎用の承認エンジンと共通の承認箱、Policy の変更のワークフロー、Outbox と Webhook(CloudEvents)、定期レビュー、ジョブ基盤と保持期限の自動執行 | `/approvals`, `/policies/bundles`, `/webhooks`, `/outbox`, `/reviews`, `/ops/jobs` |
| §7 / §6.3 / §17 / §15 | Data Plane 連携: 資産一覧の取り込みと差分の検出(未登録・消失・スキーマの変化)、Catalog 登録率、利用実績、Output Port の到達の報告と確認(MESH-007)、SDK(`clients/python`)、MDM Hub の参照実装 | `/discovery/...`, `/usage/daily`, `/mesh/ports/{id}/reachability` |
| §7 / §9 / §16 / §17 / §20 | UI(Catalog、Lineage、KPI、成熟度の閲覧。承認箱での判断、棚卸と証明、Issue の更新)、BFF のログインとセッション、KPI の推移 | `/ui/`, `/ui/auth/...`, `/ui/session`, `/kpi/history`, `/lineage/{name}/graph`, `/mesh/products/overview` |
| §4 / §11 | 認証(OIDC JWT / 開発用 dev モード)・RBAC・資産ごとの Owner/Steward 判定・ABAC(機密度×clearance)・閲覧承認・拒否の証跡 | 全 API、`/assets/{id}/access-grants` |

## 起動

```bash
# ローカル (SQLite, dev モード)
pip install -e ".[dev]"
alembic upgrade head                  # スキーマ作成/更新 (EDGF_DATABASE_URL の DB に対して)
EDGF_AUTH_MODE=dev uvicorn edgf.main:app --reload --no-access-log   # http://localhost:8000/docs
./scripts/demo.sh                     # サンプルデータの投入 (dev モード前提)
pytest -q --cov=edgf

# テストとマイグレーションの往復を PostgreSQL で実行 (テストは全テーブルを削除するので専用の DB を使う)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait db   # 127.0.0.1:55432 に公開
docker compose exec -T db psql -U edgf -d edgf -c "CREATE DATABASE edgf_test"
export PG_TEST=postgresql+psycopg://edgf:edgf-local-only@127.0.0.1:55432/edgf_test
EDGF_DATABASE_URL=$PG_TEST python scripts/verify_migrations.py
EDGF_TEST_DATABASE_URL=$PG_TEST pytest -q

# PostgreSQL (docker compose)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build   # 開発: dev モード
docker compose up --build             # 既定: jwt モード (EDGF_OIDC_* が必要。未設定なら起動しない)
```

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `EDGF_DATABASE_URL` | `sqlite:///./edgf.db` | SQLAlchemy URL(例: `postgresql+psycopg://...`) |
| `EDGF_POLICY_DIR` | `./policies` | Policy as Code の YAML |
| `EDGF_CERT_MIN_QUALITY` | `0.99` | Certification に必要な Quality Score |
| `EDGF_ISSUE_SLA_DAYS` | `30` | Issue 是正 SLA(日) |
| `EDGF_AUTO_MIGRATE` | `false` | 起動時に `alembic upgrade head` を実行する(開発用。本番はデプロイ手順で明示実行) |
| `EDGF_LOG_LEVEL` / `EDGF_LOG_FORMAT` | `INFO` / `json` | ログレベル / `json` または `text` |
| `EDGF_OTEL_ENABLED` | `false` | OpenTelemetry トレーシング(`pip install ".[otel]"` が必要。送信先は `OTEL_EXPORTER_OTLP_ENDPOINT` 等の OTel 標準変数、サービス名は `OTEL_SERVICE_NAME`) |
| `EDGF_ENV` | `development`(Docker では `production`) | `production` のとき dev モードと非 https の JWKS を拒否する |
| `EDGF_AUTH_MODE` | `jwt` | `jwt`(OIDC)または `dev`(開発専用)。後述の「認証」を参照 |
| `EDGF_SUBJECT_KEY` | (production では必須) | `subject_ref` の HMAC 鍵。32 バイト以上。後述の「Data Privacy」を参照 |
| `EDGF_ERASURE_SLA_DAYS` | `30` | 消去要求の対応期限(日) |
| `EDGF_MDM_REVIEW_SLA_DAYS` | `7` | MDM の裁定キューの対応期限(日) |
| `EDGF_MIN_K_ANONYMITY` | `5` | `anonymized` と宣言するために必要な k-匿名性の下限(PRV-003)。継承の解除の基準にもなる |
| `EDGF_AI_REMEDIATION_SLA_DAYS` | `30` | ModelRemediation の判断と実施の期限(日) |
| `EDGF_CONTRACT_DEPRECATION_DAYS` | `90` | 新しいメジャー版の公開後、旧版を併存させる期間(日) |
| `EDGF_UI_OIDC_CLIENT_ID` | (なし) | UI の OIDC クライアント ID。未設定だと UI はログインできません |
| `EDGF_UI_OIDC_ISSUER` / `EDGF_UI_OIDC_AUDIENCE` | `EDGF_OIDC_ISSUER` / `EDGF_OIDC_AUDIENCE` | UI が使う issuer と audience |
| `EDGF_UI_OIDC_SCOPES` | `openid,profile` | 要求するスコープ(カンマ区切り) |
| `EDGF_REVIEW_DUE_DAYS` | `30` | レビューの対応期限(日) |
| `EDGF_ACCESS_REVIEW_GRACE_DAYS` | `14` | 棚卸の期限を過ぎてから権限を自動で取り消すまでの猶予(日) |
| `EDGF_RETENTION_REJECT_COOLDOWN_DAYS` | `90` | 保持期限の削除の申請を却下されたあと、次に申請するまでの期間(日) |
| `EDGF_WEBHOOK_MAX_ATTEMPTS` | `8` | Webhook の最大試行回数。超えるとデッドレターに移す |
| `EDGF_WEBHOOK_BACKOFF_SECONDS` / `EDGF_WEBHOOK_BACKOFF_MAX_SECONDS` | `60` / `21600` | 再試行の間隔(指数バックオフ)の基準と上限 |
| `EDGF_WEBHOOK_TIMEOUT_SECONDS` | `10` | Webhook の送信のタイムアウト |
| `EDGF_JOB_LEASE_SECONDS` | `900` | ジョブのリースの有効期間 |
| `EDGF_JOB_<NAME>_ENABLED` / `EDGF_JOB_<NAME>_INTERVAL_SECONDS` | `true` / ジョブごと | ジョブの有効・無効と間隔(`<NAME>` は大文字。例: `EDGF_JOB_OUTBOX_DELIVER_ENABLED`) |

### スキーマ変更(Alembic)

```bash
alembic revision --autogenerate -m "add xxx"   # モデル変更後に生成し、内容をレビューしてコミット
alembic upgrade head
```

`tests/test_platform.py` はモデルとマイグレーションの差分がゼロであることを検査します(マイグレーションの作り忘れを CI で検出するため)。
MVP(v0.1, `create_all`)で作成済みの既存 DB は、スキーマが同一であれば `alembic stamp 0001` で管理下に移せます。ただし PostgreSQL では制約名が自動命名(例: `audit_events_prev_hash_key`)のままになるため、以後のマイグレーションで制約を変更する前に命名規約(`uq_<table>_<column>` など)へリネームしてください。

## 認証(§4 / §11)

`EDGF_AUTH_MODE` で方式を切り替えます。`/health` と `/ready` 以外の API はすべて認証が必要です。

| モード | 操作者の特定 | 用途 |
|---|---|---|
| `jwt`(既定) | `Authorization: Bearer <JWT>`。署名・`iss`・`aud`・`exp`・`nbf` を検証します。トークンが無い・不正なら 401 で、`X-Actor` は無視します。UI はセッションの Cookie(「[セッションと CSRF](#セッションと-csrf)」)で、ログイン時に同じ検証をしたアクセストークンから Principal を作ります | 本番・検証環境 |
| `dev` | `X-Actor`(必須)、`X-Roles` / `X-Domains` / `X-Clearance`(任意)ヘッダ | 開発・テスト専用(恒久的な機能) |

### jwt モードの設定(IdP 製品に依存しない)

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `EDGF_OIDC_ISSUER` / `EDGF_OIDC_AUDIENCE` | (必須) | 期待する `iss` / `aud` |
| `EDGF_OIDC_JWKS_URL` | (必須) | IdP の JWKS エンドポイント。`EDGF_ENV=production` では https のみ |
| `EDGF_OIDC_ALGORITHMS` | `RS256` | 許可する署名アルゴリズム(カンマ区切り)。非対称鍵(RS/PS/ES/EdDSA)のみ指定可 |
| `EDGF_JWT_LEEWAY_SECONDS` | `30` | `exp`/`nbf` の時計ずれ許容 |
| `EDGF_JWKS_CACHE_SECONDS` / `EDGF_JWKS_REFRESH_COOLDOWN_SECONDS` | `300` / `30` | JWKS のキャッシュ期間と、未知の `kid` による再取得の最短間隔(鍵ローテーション対応) |
| `EDGF_CLAIM_SUBJECT` | `sub` | 操作者 ID にする claim。資産の `owner`/`steward` と照合されます(例: `preferred_username`, `email`) |
| `EDGF_CLAIM_ROLES` / `EDGF_CLAIM_DOMAINS` / `EDGF_CLAIM_CLEARANCE` | `roles` / `domains` / `clearance` | ドット区切りでネストした claim も指定できます(例: `realm_access.roles`)。値は配列か、空白/カンマ区切りの文字列 |
| `EDGF_ROLE_MAP` | (なし) | IdP のロール/グループ名 → edgf ロール(例: `dg-committee=committee,dg-admins=admin`) |

設定が足りない、または対称鍵アルゴリズム(HS256 など)を指定した場合は、起動時に停止します。JWKS に到達できないときは、認可を fail-close にするため 503 を返します。

```bash
TOKEN=$(...)   # IdP から取得したアクセストークン (aud = EDGF_OIDC_AUDIENCE)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/assets
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -X POST http://localhost:8000/api/v1/assets -d '{"qualified_name":"sales.orders", ...}'
```

トークンの claim の例(既定の claim 名の場合):
`{"iss": "...", "aud": "edgf-control-plane", "sub": "hanako", "exp": ..., "roles": ["steward"], "domains": ["sales"], "clearance": "confidential"}`

### dev モードの安全策

dev モードは**開発・テスト専用の恒久的な機能**です(以前は移行期間の措置としていました)。API のテストや `curl` での確認を手軽にするために残し、次の安全策を維持します。UI は dev モードに対応しません。UI を手元で動かすときは `docker-compose.idp.yml` を使ってください。

- `EDGF_ENV=production` と同時に指定すると、起動時に例外を出して停止します。
- 起動時に WARNING ログを出し、`/health` の応答に `"auth_mode": "dev"` を含めます(外形監視で検知できます)。
- 監査ログの actor は `dev:<名前>` で記録し、JWT 由来の操作と区別します。
- Dockerfile と `docker-compose.yml` の既定は jwt です。dev モードは `docker-compose.dev.yml`(override)でだけ有効になります。

```bash
curl -H 'X-Actor: steward1' -H 'X-Roles: steward' -H 'X-Domains: sales' http://localhost:8000/api/v1/assets
```

## 認可(§4 / §11)

認可の判定は [edgf/authz.py](edgf/authz.py) に集約し、権限表 `PERMISSIONS` で宣言的に定義しています。各操作は、次のいずれかを満たせば許可されます。

- **ロール**(RBAC): `admin` / `committee` / `owner` / `steward` / `custodian` / `consumer` / `consent_collector`。認証済みの全員に `consumer` が付きます。ただし `consent_collector` だけを持つ主体は除きます。これは同意取得システムのサービスアカウント専用で、`privacy.consent.*` だけを許可する最小権限のロールです(既定の clearance は public)
- **ドメイン内ロール**: 対象資産のドメインが Principal の `domains` に含まれる場合だけ有効なロール(`*` は全ドメイン)
- **対象との関係**: 資産の `owner` / `steward` / `custodian` が JWT の subject と一致すること(Issue は `assignee`)

承認行為(✓)に Custodian を含めることはできず、そのような定義はロード時にエラーになります(§4 Business Ownership)。権限が無い操作は 403 になり、`authz.denied` として監査ログに記録します(リクエスト自体は巻き戻し、拒否の証跡だけを確定させます)。

| アクション | 内容 | 許可される主体 | 承認 |
|---|---|---|---|
| `ai.card.write` | Dataset Card / Model Card の登録・更新 | ドメイン内ロール: steward<br>対象の owner / steward |  |
| `ai.dataset_version.register` | Dataset Version の登録 | 対象の custodian / owner / steward |  |
| `ai.incident.create` | データ汚染・リコールの起票 | ロール: committee<br>ドメイン内ロール: owner, steward |  |
| `ai.inheritance.recompute` | 継承した統制属性の全件再計算 | ロール: admin, committee |  |
| `ai.model.approve` | Model Version の承認と再開 | 対象の owner | ✓ |
| `ai.model.suspend` | Model Version の停止と廃止 | ロール: committee<br>対象の owner |  |
| `ai.model_version.register` | Model Version の登録、デプロイと利用先の記録 | 対象の custodian / owner / steward |  |
| `ai.rag.authorize` | RAG の検索結果を利用者の権限で絞り込む判定 | 対象の custodian |  |
| `ai.remediation.complete` | 是正の完了報告 | 対象の custodian / owner |  |
| `ai.remediation.decide` | 消去の影響を受けたモデル版の是正の判断 (retrain / unlearn / accept) | ロール: committee | ✓ |
| `ai.remediation.escalate` | 是正の期限超過の処理 | ロール: admin, committee |  |
| `ai.training.approve` | 学習利用の承認 (構成元の資産の Owner と Committee) | ロール: committee<br>対象の owner | ✓ |
| `ai.training.request` | 学習利用の承認の申請と取り下げ | 対象の owner / steward |  |
| `approval.cancel` | 承認の申請の取り消し (申請者本人、admin) | ロール: admin<br>対象の requester |  |
| `approval.decide` | 承認の判断 (種別とステップごとのルールで判定する) | ロール: committee<br>ドメイン内ロール: owner<br>対象の owner | ✓ |
| `approval.read` | 全案件の参照 (ABAC で絞り込む) | ロール: admin, committee |  |
| `approval.request` | 承認の申請 (種別ごとの申請条件は approval_types で定義する) | 認証済みの全員 |  |
| `asset.access_grant` | 閲覧の承認 (§4.2 アクセス承認 A/R) | 対象の owner | ✓ |
| `asset.access_grant.read` | 閲覧承認の一覧 | ロール: admin, committee<br>対象の owner / steward |  |
| `asset.access_revoke` | 閲覧承認の取消 | ロール: committee<br>対象の owner |  |
| `asset.certify` | Certification の変更 | 対象の owner / steward | ✓ |
| `asset.delete` | 削除指示 (§10.1 Delete) | ロール: committee<br>対象の owner | ✓ |
| `asset.legal_hold` | Legal Hold の設定/解除 | ロール: committee<br>対象の owner | ✓ |
| `asset.ownership_change` | Owner/Steward/Custodian の割当 | ロール: admin, committee | ✓ |
| `asset.refresh_notify` | Data Plane からの更新通知 | ロール: admin<br>ドメイン内ロール: custodian, owner, steward<br>対象の custodian / owner / steward |  |
| `asset.register` | 資産の登録 | ロール: admin<br>ドメイン内ロール: owner, steward |  |
| `asset.update` | 資産メタデータの更新・ライフサイクル遷移 (削除以外) | ロール: admin<br>ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `audit.read` | 監査ログの参照 | ロール: admin, committee |  |
| `cdm.import` | 外部の CDM 定義の版の取り込み (作成・編集の機能は持たない) | ロール: admin, committee | ✓ |
| `discovery.apply_breaking` | 破壊的なスキーマの変化を確認して Catalog に反映 | 対象の owner / steward | ✓ |
| `discovery.push` | 資産一覧のスナップショットの送信 (ソースのドメイン) | ロール: admin<br>ドメイン内ロール: custodian |  |
| `discovery.read` | 検出された資産と保留のスキーマの変化の参照 | ロール: admin, committee<br>ドメイン内ロール: owner, steward |  |
| `domain.create` | ドメインの新設 | ロール: admin, committee |  |
| `exception.decide` | ポリシー例外の承認/却下 | ロール: admin, committee | ✓ |
| `exception.request` | ポリシー例外の申請 | ロール: admin<br>ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `glossary.link` | 用語と資産の紐付け | ロール: admin<br>ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `glossary.write` | 用語の作成 | ロール: admin, owner, steward |  |
| `issue.create` | Issue の起票 (Consumer の品質報告を含む) | 認証済みの全員 |  |
| `issue.escalate` | Issue のエスカレーション | ロール: admin, committee<br>対象の assignee / owner / steward |  |
| `issue.escalate_overdue` | SLA 超過 Issue の一括エスカレーション | ロール: admin, committee |  |
| `issue.update` | Issue の更新 | ロール: admin, committee<br>対象の assignee / owner / steward |  |
| `jobs.read` | ジョブの実行状況の参照 | ロール: admin, committee |  |
| `jobs.run` | ジョブの手動実行 (通常は外部の cron や CronJob から実行する) | ロール: admin |  |
| `lineage.column_write` | カラム単位のリネージの登録・取込 (columnLineage facet) | ロール: admin, custodian, owner, steward |  |
| `lineage.write` | リネージの登録・取込 | ロール: admin, custodian, owner, steward |  |
| `mdm.compose` | Golden Record の生成と内容ハッシュの検証 | ドメイン内ロール: steward<br>対象の custodian |  |
| `mdm.match` | ソースレコードのマッチング | ドメイン内ロール: steward<br>対象の custodian |  |
| `mdm.read` | MDM のメタデータの参照 (ABAC で絞り込む。値は返さない) | 認証済みの全員 |  |
| `mdm.review.decide` | 裁定 (merge / not_match / split) と属性値の指定 | 対象の owner / steward | ✓ |
| `mdm.review.escalate` | 裁定キューの期限超過の処理 | ロール: admin, committee |  |
| `mdm.rule.approve` | Golden Record 生成規則の承認 (§8.2) | 対象の owner | ✓ |
| `mdm.rule.write` | エンティティ定義の再読込 (公開) | ロール: admin, committee | ✓ |
| `mesh.contract.check` | Contract の案の互換性の判定と影響の確認 | 対象の custodian / owner / steward |  |
| `mesh.contract.expedite_retire` | 旧版の併存期間の短縮 (理由必須) | ロール: committee | ✓ |
| `mesh.contract.publish` | Data Contract の版の公開と旧版の retire (対象 Product の Owner) | 対象の owner | ✓ |
| `mesh.contract.write` | Data Contract の下書きの作成・更新 | ドメイン内ロール: owner, steward |  |
| `mesh.product.publish` | Data Product の公開・非推奨・廃止 (対象 Product の Owner) | 対象の owner | ✓ |
| `mesh.product.write` | Data Product と Output Port の登録・更新 | ドメイン内ロール: owner, steward |  |
| `mesh.subscription.approve` | 利用登録の承認 (対象 Product の Owner) | 対象の owner | ✓ |
| `mesh.subscription.read` | 全利用者の利用登録の参照 | ロール: admin, committee |  |
| `mesh.subscription.request` | 利用登録の申請と自分の登録の変更 (閲覧権限は与えない) | 認証済みの全員 |  |
| `outbox.redeliver` | デッドレターの参照と手動の再送 | ロール: admin |  |
| `policy.evaluate_all` | 全資産の自動監査と Issue 化 | ロール: admin, committee |  |
| `policy.propose` | Policy の変更の提案 (ドメイン内の Owner は Domain Policy だけ) | ロール: admin, committee<br>ドメイン内ロール: owner |  |
| `policy.publish` | 承認済みの Policy の公開 | ロール: committee | ✓ |
| `policy.reload` | ディスクの内容から Policy の変更を提案する (即時には反映しない) | ロール: admin, committee |  |
| `policy.review` | Policy の年次見直しの記録 | ロール: committee | ✓ |
| `privacy.consent.check` | 目的内利用の判定と同意履歴の参照 | ロール: admin, committee, consent_collector<br>ドメイン内ロール: steward |  |
| `privacy.consent.write` | 同意の取得・撤回の記録 (ドメインは Purpose の所属で判定) | ロール: admin, committee, consent_collector<br>ドメイン内ロール: steward |  |
| `privacy.erasure.approve` | 消去の対象範囲の承認 (Owner は自分の資産の分のみ) | ロール: committee<br>対象の owner | ✓ |
| `privacy.erasure.certificate` | 消去証明の発行 | ロール: admin, committee |  |
| `privacy.erasure.read` | 消去要求の参照 | ロール: admin, committee<br>対象の owner |  |
| `privacy.erasure.reject` | 消去要求の却下 (理由必須) | ロール: committee | ✓ |
| `privacy.erasure.request` | 消去要求の受付・対象特定・削除指示の発行・期限超過の処理 | ロール: admin, committee |  |
| `privacy.erasure.task_ack` | 消去タスクの受領 | 対象の custodian |  |
| `privacy.erasure.task_complete` | 消去タスクの完了報告 | 対象の custodian |  |
| `privacy.erasure.task_read` | 消去タスクの取得 (Custodian は自分の資産の分のみ) | ロール: admin, committee<br>対象の custodian |  |
| `privacy.erasure.verify` | 消去の完了確認とクローズ | ロール: committee<br>対象の owner | ✓ |
| `privacy.inventory.read` | PII インベントリの参照 | ロール: admin, committee<br>ドメイン内ロール: owner, steward |  |
| `privacy.purpose.write` | Purpose カタログの登録・更新 | ロール: admin, committee |  |
| `probe.configure` | Control Plane からの到達の確認の実行と設定の参照 | ロール: admin |  |
| `quality.rule_write` | 品質ルールの追加・削除 | ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `quality.run` | 品質検査の実行 (パイプラインからの送信を含む) | ドメイン内ロール: custodian, owner, steward<br>対象の custodian / owner / steward |  |
| `reachability.report` | Output Port の到達の報告 (ポートの資産の Custodian) | 対象の custodian |  |
| `refdata.publish` | Reference Data の版の公開 | ロール: committee<br>対象の owner | ✓ |
| `refdata.write` | Reference Data の下書きの作成 | ドメイン内ロール: owner, steward |  |
| `review.decide` | 棚卸と証明の判断 (対象資産の Owner) | 対象の owner | ✓ |
| `review.read` | キャンペーンと全項目の参照 | ロール: admin, committee |  |
| `usage.push` | 利用実績の集計値の送信 (資産のドメイン) | ロール: admin<br>ドメイン内ロール: custodian |  |
| `webhook.manage` | Webhook の配信先の登録・更新・参照 | ロール: admin |  |

これ以外の参照系 API(資産・Issue・KPI・ポリシー・リネージなど)は、認証済みの全員が使えます。資産を含む結果は、次の ABAC で絞り込みます。

### ABAC(資産の閲覧)

| 資産の機密度 | 閲覧できる主体 |
|---|---|
| public / internal / confidential | clearance がその機密度以上の利用者、資産の当事者(Owner/Steward/Custodian)、`committee`、`admin` |
| restricted | 資産の当事者、`committee`、`admin`、**Owner が閲覧を承認した利用者**(clearance だけでは閲覧不可) |

- AI 資産は、宣言値ではなく上流から継承した**実効機密度**で判定します(後述の「統制属性の継承」)。
- clearance は claim(`EDGF_CLAIM_CLEARANCE`)で与えます。無い場合はロールから導きます(admin/committee=restricted、owner/steward/custodian=confidential、consumer=internal、consent_collector=public)。
- 閲覧できない資産は、一覧(資産・保持期限・Issue・ポリシー例外・用語の紐付け先)から件数を含めて除外し、個別に取得しようとすると 403 になります。影響分析とリネージでは名前をラベルに置き換えて深さだけを返し、詳細を伏せます(通知先 Owner は変更管理に必要なので返します)。
- Owner による閲覧承認: `POST /assets/{id}/access-grants`(`subject`, `purpose`, `expires_at`。期限は最大 365 日)。取り消しは `DELETE /assets/{id}/access-grants/{grant_id}` で、Owner と Committee が行えます。

## カラム単位のリネージ(§9)

データセット単位の `lineage_edges` に加えて、カラムの間の辺を `column_lineage_edges` に持ちます。カラムの辺を登録すると、対応するデータセット単位の辺も作るので、既存の影響分析、LIN-001、AI Lineage はそのまま使えます。データセットの識別は既存と同じで、OpenLineage の dataset の `name` を `qualified_name` として扱います(`namespace` は識別に使いません)。

### columnLineage facet の受け付け範囲

| 項目 | 扱い |
|---|---|
| `_schemaURL` | メジャー版 1(`…/1-x-y/ColumnLineageDatasetFacet.json`)だけ。それ以外と欠落は 422(`column-lineage-invalid`)。1 つでも不正ならイベント全体を取り込みません |
| `fields.<出力カラム>.inputFields[]` | `name`(データセット)と `field`(カラム)を保存します。`namespace` は保存しません |
| `transformations[].type` | `DIRECT` / `INDIRECT`。それ以外は `DIRECT` として保存し、`warnings` に `unknown_type` を返します(値が流れる側に倒す) |
| `transformations[].subtype` | `IDENTITY`、`TRANSFORMATION`、`AGGREGATION`、`JOIN`、`GROUP_BY`、`FILTER`、`SORT`、`WINDOW`、`CONDITIONAL`。それ以外は `OTHER` として保存し、`unknown_subtype` を返します(拒否しません) |
| `transformations[].masking` | 保存します。**masking=true でも PII の継承と消去の対象は止めません** |
| `transformations[].description`、`transformationDescription` | **保存しません**(SQL のリテラルなどの値を含みうるため。DB、ログ、監査のいずれにも残しません) |
| 廃止予定の `transformationType`(field 単位) | `transformations` が無いときだけ `type` に読み替えます(`MASKED` は `DIRECT` / `TRANSFORMATION` / masking=true) |
| 種類の記録が無い入力 | `DIRECT` として扱います |
| `dataset[]` | データセット全体に効く入力として、出力カラムを NULL にした `INDIRECT` の辺で保存します |
| Catalog に無いカラム | 拒否せず保存し、`warnings` に `unregistered_column` を返します |
| 同じデータセットの中の辺 | 保存せず `same_dataset` を返します(データセット単位の辺が自己ループになるため) |
| 件数 | 1 つの facet で 20,000 辺まで |

### 探索と影響分析

| API | 内容 |
|---|---|
| `GET /lineage/{name}/columns` | 資産の各カラムの 1 段の上流と下流(資産の詳細の画面で使います) |
| `GET /lineage/{name}/columns/{column}/upstream`、`/downstream` | カラムの上流・下流。`depth` は既定 10、上限 50(既存と同じ) |
| `GET /lineage/{name}/columns/{column}/impact` | 影響を受けるカラム、深さ、経路の種類(`path`)、Owner、CDE のカラムへの影響(`cde_column`、`cde_impacted`)、リスク |

- 影響分析は DIRECT と INDIRECT の両方を辿ります。`path` は起点からの経路の種類で、途中がすべて値の流れる辺(`DIRECT`、または `AGGREGATION`)なら `DIRECT`、そうでなければ `INDIRECT` です
- 閲覧できない資産は、名前を `redacted-N` に置き換え、カラム名と `job` も返しません(Phase 14 と同じ。漏洩のテストもカラム単位の API に適用しています)。Owner とリスクは全資産で算出します
- 探索は既存と同じく段ごとの一括取得です(`WITH RECURSIVE` にしない判断と `EXPLAIN` は [docs/operations/performance.md](docs/operations/performance.md))
- UI(Phase 16 の時点では読み取り専用)の資産の詳細に「カラムのリネージ」、Lineage の画面にカラムの入力欄とカラム単位のグラフと影響の表を追加しました

### 広げる方向と絞る方向

**機密度・PII の継承と消去の対象の特定は、値の流れる経路(`DIRECT` と `AGGREGATION`)だけを見ます。** 方向によって、使える辺が違います。

| 方向 | 例 | 使える辺 |
|---|---|---|
| 広げる | 影響分析の詳細化、PII のカラムの追加(継承の `pii_columns`、消去のタスクのカラム、PRV-006)、Contract check の `column_impact` | すべての辺 |
| 絞る | 消去の対象から外す、AI の Dataset Version を superseded にしない、AI 資産の PII の継承を外す | 信頼できる生成元の辺だけ |

データセットの辺の組(A → B)が「信頼できる」のは、次をすべて満たすときです。満たさない組は、従来どおりデータセット単位で扱います(A のどこかに PII があれば B 全体)。

- A → B のデータセット単位の辺の job が、すべて `trusted_producers.yaml` で宣言されていること
- その job ごとに A → B のカラムの辺があり、すべて **OpenLineage で、宣言された主体から**取り込まれていること。手動の登録(`POST /lineage/column-edges`)と、別の主体が同じ job の名前で送った辺は信頼しません。信頼できる辺を別の主体が書き直すと、その辺は信頼できなくなります

起点(消去では identifier_type の一致した資産の **すべての** PII のカラム、継承では上流の PII のカラム)から値の流れる経路が無い資産だけを外します。外した資産と理由(`no_direct_path_from_pii_columns`、経由した資産、信頼できる生成元の job)は、消去証明の `lineage_exclusions` と継承の結果の `pii_excluded` に残します。機密度の継承はデータセット単位のままです。

### 信頼できる生成元の承認

`policies/lineage/trusted_producers.yaml` は Policy Bundle に含まれ、**変更には Committee の承認と公開(`policy.publish`)が必要です。** 承認されるまで、ディスクの変更は反映しません。

```yaml
producers:
  - job_namespace: dbt-prod          # OpenLineage の job.namespace
    jobs: [orders_mart]              # 省略すると名前空間のすべての job
    principals: [svc-dbt-prod]       # イベントを送る主体 (JWT の sub)。必須
    reason: 本番の dbt。facet の正しさをレビューで確認済み
```

公開すると、保存済みの AI 資産の継承を計算し直します。手順は [docs/operations/runbook.md](docs/operations/runbook.md) にあります。

### CDE のカラム単位化

| 項目 | 内容 |
|---|---|
| `cde`(スキーマのカラム) | Critical Data Element のカラム。`is_cde` は「CDE のカラムを含む」として残し、`cde` のカラムがあれば自動で true にします。`cde` のカラムが無い `is_cde: true`(データセット単位の CDE)も残せます |
| LIN-002(high) | 上流を持つ資産の CDE のカラムは、カラム単位のリネージを持つこと。上流の無い資産(源泉)は対象外 |
| PRV-006(high) | DIRECT の経路で上流の PII のカラムが届くカラムは `pii: true` を宣言すること(masking=true の経路も含む)。基準を満たす匿名化の資産の先は辿りません。カラム単位のリネージの無い資産は対象外 |
| KPI `cde_column_lineage_rate` | 上流を持つ資産の CDE のカラムのうち、カラム単位のリネージを持つ割合(LIN-002 と同じ対象)。対象が無ければ `null` |
| ODCS | `criticalDataElement: true` を `cde` として取り込み、書き出します。true / false 以外は警告にして false とします |

### 権限

| 操作 | 権限 |
|---|---|
| カラムの辺の登録、facet を含むイベントの取り込み | `lineage.column_write`(`lineage.write` と同じ主体: admin、custodian、owner、steward) |
| 信頼できる生成元の変更 | Policy Bundle の提案と公開(`policy.publish`。Committee の承認が必要) |
| 参照(探索、影響分析、カラムの一覧) | 認証済みの全員。ABAC で名前とカラム名を伏せます |

### 既知の制約(カラム単位のリネージ)

- 信頼できる生成元の facet が出力のすべてのカラムを記述しているかは検証しません。宣言の承認の前に確認してください(記述の漏れたカラムは「値が流れない」とみなされます)
- カラムの辺の削除 API はありません。facet から消えた辺も残ります(残った辺は値の流れる側に倒れるので、絞り込みは保守的になります)
- 消去の `scope` の結果は、その時点の宣言で決まります。宣言を変えた後は `scope` をやり直してください。AI の版の superseded は `dispatch` の時点で計算し直します
- 列の名前の変更はカラムの辺に反映しません。Contract check の `column_impact` は、変更前の名前で影響を探します

## Data Privacy(§12 / §10)

### subject_ref と鍵の運用

Control Plane はデータ主体の識別子を保持しません(§18.2)。API は生の識別子を受け取りますが、受け取った時点で `subject_ref` に変換して破棄します。識別子は URL に載らないよう POST の本文でだけ受け取り、DB・ログ・応答には残しません。

```
subject_ref = hex(HMAC-SHA256(EDGF_SUBJECT_KEY, utf8(identifier_type + "\x1f" + normalized_value)))
normalized_value = NFKC 正規化 + 前後の空白除去(identifier_type が email のときは小文字化も行う)
```

- **鍵の管理**: `EDGF_SUBJECT_KEY` は KMS などで管理します。`EDGF_ENV=production` で未設定または 32 バイト未満なら起動を拒否します。それ以外の環境では固定のテスト鍵を使い、起動時に WARNING を出します。
- **Data Plane での照合**: Data Plane も同じ鍵(KMS 経由)と上記の正規化で自分の識別子の HMAC を計算し、タスクの `subject_ref` と突き合わせて対象レコードを特定します。Control Plane から元の識別子を復元することはできません。
- **鍵の変更**: 鍵を変更すると既存の `subject_ref` と照合できなくなります。ローテーションの仕組みは未実装なので、鍵は変更しない前提で運用してください。

監査ログはハッシュチェーンのため消去できません。そのため `audit.record()` は payload の禁止キー(`identifier`, `email` など)と PII パターン(メールアドレス、電話番号、カード番号)を検査し、該当すればトランザクションごと 422(`audit-payload-rejected`)で拒否します。業務担当者のキー(`owner`, `steward` など)の値は、JWT の subject がメールアドレスである運用を考慮して検査の対象外です。監査 payload には `subject_ref` も記録しません。

### API(`/api/v1/privacy`)

| API | 内容 |
|---|---|
| `GET /inventory`, `GET /inventory/summary` | PII 資産・カラムの一覧と、カテゴリ・目的・法的根拠ごとの集計(ABAC + ドメイン範囲) |
| `POST /purposes`, `GET /purposes[/{id}]`, `PATCH /purposes/{id}` | Purpose カタログ(目的 ID、説明、既定の法的根拠、保持期間、所属ドメイン) |
| `POST /purposes/{id}/withdraw` | 目的そのものの撤回(理由必須)。新しい同意と利用登録を止め、その目的の利用登録を suspended にする。主体ごとの同意の撤回とは別です |
| `POST /consents` | 同意の取得・撤回の記録(追記のみ)。撤回時は、同意に基づいて処理している資産の Owner に Issue を起票(資産 × 目的で重複なし) |
| `POST /consents/check` | 目的内利用の判定。法的根拠が consent 以外の目的は同意不要として理由を返す |
| `GET /consents?subject_ref=...` | 同意の履歴(`subject_ref` は必須) |
| `POST /erasure-requests`, `GET /erasure-requests[/{id}]` | 消去要求の受付と参照 |
| `POST /erasure-requests/{id}/scope` / `approve` / `reject` / `dispatch` / `verify` / `close` | 状態遷移 |
| `GET /erasure-requests/{id}/certificate` | 消去証明(対象資産、各タスクの結果、時刻、request_id。生の識別子なし) |
| `POST /erasure-requests/escalate-overdue` | 期限超過の Issue 起票とエスカレーション(定期実行する想定) |
| `GET /erasure-tasks?status=pending`, `POST /erasure-tasks/{id}/ack`, `.../complete` | Data Plane 向けの pull 型タスク。応答は対象資産の Custodian のみ |

カラムの `nullable` は任意で、指定すると Data Contract とのずれの検出に使います。カラムの PII メタデータは `DataAsset.schema` の各要素で指定します(`pii_category`, `identifier_type`, `processing_purposes`, `legal_basis`, `privacy_technique`, `privacy_params`)。すべて任意で、既存の `pii` フラグと互換です。

### Right to Erasure の状態遷移

```mermaid
stateDiagram-v2
    [*] --> received: POST /erasure-requests
    received --> scoped: scope
    scoped --> scoped: scope(再実行) / approve(Owner の一部承認)
    scoped --> approved: approve(全タスク承認)
    approved --> dispatched: dispatch
    dispatched --> completed: 全タスク完了
    dispatched --> partially_completed: withheld / failed あり
    completed --> verified: verify
    partially_completed --> verified: verify
    verified --> closed: close
    received --> rejected: reject(Committee、理由必須)
    scoped --> rejected: reject
    approved --> rejected: reject
    closed --> [*]
    rejected --> [*]
```

- **対象の特定**: `identifier_type` が一致するカラムを持つ資産を起点に、Lineage の下流のうち `contains_pii` の資産と、AI 資産(Embedding、ベクトル、RAG コーパス、学習データ)を加えます(非 PII の資産を経由した下流も含みます)。モデルにはタスクを発行せず、[ModelRemediation](#消去の伝播と-modelremediation) で扱います。未登録の下流は `unregistered_downstream` として警告します。
- **Legal Hold**: 対象の特定時、発行時、ack 時に判定し、該当すれば `withheld`(理由を記録)にして削除しません(§10.2)。ack の後に Hold が設定された場合、実際に起きた削除の報告は証跡として記録し、警告を残します。
- **承認**: Committee は全タスク、Owner は自分の資産のタスクだけを承認でき、全タスクが揃うと `approved` になります。受付者本人の承認と、削除を実行した本人の完了確認は職務分離で拒否します。
- **期限超過**: 未完了タスクの資産ごとに Issue を起票します。Issue の期限も過ぎると、次の実行でエスカレーションします。

### 匿名化・仮名化(PRV-003〜005)

| ポリシー | 内容 | 重大度 |
|---|---|---|
| PRV-003 | `anonymized` と宣言するなら `privacy_params.k >= EDGF_MIN_K_ANONYMITY` | critical |
| PRV-004 | `pseudonymized` なら `privacy_params.key_vault_ref`(鍵の分離保管先)が必須で、`contains_pii` は true のまま | critical |
| PRV-005 | PII カラムには `processing_purposes` と `legal_basis` が必須 | medium |

`contains_pii` を自動で false にできるのは PRV-003 を満たす `anonymized` だけです。仮名化されたデータは常に true で、基準を割ると true に戻ります。PII カラムがある間は、手作業でも false にできません。PRV-005 は、既存資産の Certification を止めないよう medium にしています。

## Master Data Management(§8)

### Control Plane と Data Plane の境界(§18.2)

| 対象 | 置き場所 | 内容 |
|---|---|---|
| Golden Record の属性値、時点ごとの値の履歴 | **Data Plane(MDM Hub)** | Control Plane には永続化しない |
| 比較対象の候補の絞り込み(blocking) | **Data Plane(MDM Hub)** | 候補の値を `match` の `candidates` で渡す |
| マッチングと Survivorship のルール | Control Plane | `policies/mdm/*.yaml`。生成ロジックとして公開(`GET /mdm/entities`) |
| 相互参照(xref) | Control Plane | ソースのキーは HMAC の `source_ref` でのみ保持 |
| 属性ごとの由来 | Control Plane | 勝ったソースと適用したルール。値は持たない |
| 版の情報 | Control Plane | 版番号、有効期間、内容ハッシュ、構成する `source_ref`、変更の理由 |
| 裁定キュー | Control Plane | 候補の参照、総合スコア、属性ごとの比較結果だけ |
| Reference Data の値 | Control Plane(唯一の例外) | reference 資産で、public / internal かつ `contains_pii=false` の場合だけ。違反は 422 |

マッチングと生成は品質エンジンと同じくステートレスです。受け取った値は評価後に破棄し、DB・ログ・監査には残しません。値を返すのは `compose` の応答だけで、Data Plane がそれを保存します。

### エンティティ定義

`policies/mdm/*.yaml` で、属性、ソースの信頼度、マッチング(第1段: 完全一致キー、第2段: 類似度の重み付き合計。閾値は `auto_merge` と `review`)、Survivorship(`source_priority` / `most_recent` / `most_complete` / `most_frequent`)を定義します(例: [policies/mdm/party.yaml](policies/mdm/party.yaml))。閾値の大小関係、未定義の属性の参照、ルール種別、全属性の Survivorship はロード時に検証します。

- 各定義は Catalog の master 資産と対応付けます。資産が未登録なら MDM の操作は 409 です(Metadata First)。
- `identifier_type` を持つ属性は、Phase 8 の `subject_ref` と対応付けます(人を表すエンティティの消去の対象特定に使います)。
- 反映(`POST /mdm/entities/reload`)は admin / committee が行い、生成規則の承認(`POST /mdm/entities/{type}/approve`)は対象資産の Owner が行います(§8.2)。承認はルール本文のハッシュに紐付くので、ルールを変更すると再承認が必要です。

### match → compose → verify

```mermaid
sequenceDiagram
    participant Hub as MDM Hub (Data Plane)
    participant CP as Control Plane
    participant St as Steward / Owner

    Hub->>CP: POST /mdm/party/match (record, candidates の値)
    Note over CP: 完全一致 → 類似度で判定し、値は破棄
    alt 新規 / 既存に紐付く
        CP-->>Hub: decision=new|linked, golden_id, source_ref
    else 閾値の間
        CP-->>Hub: decision=review, review_ids (比較結果のみ)
        St->>Hub: 属性値を直接参照
        St->>CP: POST /mdm/party/reviews/{id}/decision
        Hub->>CP: POST /mdm/party/match (再実行で golden_id を取得)
    end
    Hub->>CP: POST /mdm/party/golden-records/{id}/compose (構成ソースの値)
    Note over CP: Survivorship で値を決定。由来・内容ハッシュ・版だけを保存
    CP-->>Hub: attributes (値), provenance, content_hash, version
    Note over Hub: 値を保存する
    Hub->>CP: POST /mdm/party/golden-records/{id}/verify (保持している値)
    Note over CP: 内容ハッシュと照合し、値は破棄
    CP-->>Hub: match=true|false
```

### API(`/api/v1/mdm`)

| API | 内容 |
|---|---|
| `GET /entities[/{type}]`, `POST /entities/reload`, `POST /entities/{type}/approve` | エンティティ定義の参照、反映、Owner の承認 |
| `POST /{type}/match` | 行き先の判定(新規 / 既存に紐付く / 裁定)。値は応答にも含めない |
| `POST /{type}/golden-records/{id}/compose` | Survivorship で値を決めて返す。`overrides`(属性 → source_system)は裁定の権限が必要 |
| `GET /{type}/golden-records[/{id}]`, `.../versions` | 版の情報。`?as_of=` でその時点の版を返す。値は含まない |
| `POST /{type}/golden-records/{id}/verify` | Data Plane の値と内容ハッシュの照合 |
| `POST /{type}/golden-records/{id}/split` | 誤マージの分割(自動マージを含む) |
| `GET /{type}/reviews[/{id}]`, `POST /{type}/reviews/{id}/decision` | 裁定キューと裁定(`merge` / `not_match` / `split`) |
| `POST /reviews/escalate-overdue` | 期限超過の Issue 起票(定期実行する想定) |
| `GET /xref?source_system=&key=`, `POST /xref/lookup` | キーを HMAC に変換して golden_id を引く。`GET` はキーが URL に載るので、プロキシのログなどに残る環境では `POST` を使ってください |

### 裁定キューの状態遷移

```mermaid
stateDiagram-v2
    [*] --> pending: match (auto_merge と review の間)
    pending --> merged: merge
    pending --> not_match: not_match (同じペアは再起票しない)
    pending --> superseded: 別の候補に統合 / 消去
    merged --> split: split (誤マージの分割)
    not_match --> [*]
    superseded --> [*]
    split --> [*]
```

- 裁定は対象マスタ資産の Steward / Owner だけが行えます。Steward は属性値を Data Plane から直接参照します。Control Plane の API は値を返しません。
- 裁定待ちの間は新規の Golden Record を作りません。全候補が `not_match` になると新規を作ります。
- `split` は版の履歴を保ったまま xref を付け替え、Data Plane に再構成を求めます(`recompose_required`)。分割前の構成は `?as_of=` で照会できます。
- 期限(`EDGF_MDM_REVIEW_SLA_DAYS`、既定 7 日)を過ぎると Issue を起票し、Issue の期限も過ぎるとエスカレーションします。

### 内容ハッシュ

`SHA-256(正規化した JSON)` で計算します。JSON には Golden Record ごとのソルトを含めます。値の種類が少ない PII は、素の SHA-256 だと辞書攻撃で逆算できるためです。Right to Erasure の完了時に、xref と版の情報を tombstone にしてソルトと `subject_ref` の対応を破棄します。内容ハッシュと `source_ref` は消去の証跡として残りますが、値と突き合わせることはできなくなります。消去済みのソースを再度取り込もうとすると 409 になります。

## Reference Data(§8)

API は `/api/v1/reference-data` です。

| API | 内容 |
|---|---|
| `POST /sets`, `GET /sets[/{id}]` | Reference Data Set(Catalog の reference 資産と対応付ける) |
| `POST /sets/{id}/versions`, `PUT /sets/{id}/versions/{n}/codes` | 下書きの作成と編集 |
| `POST /sets/{id}/versions/{n}/publish` | 公開(対象資産の Owner または Committee)。公開後は変更できない(409)。Lineage の下流資産の Owner に Issue で通知する |
| `GET /sets/{id}/latest`, `/versions/{n}`, `/as-of?at=`, `/diff?from_version=&to_version=` | 最新の公開版、版指定、時点指定、差分(追加・廃止・ラベル変更) |

コード値は `code`, `label`, `valid_from`, `valid_to`, `standard`(外部標準の出典), `standard_version` を持ちます。品質ルールの `allowed_values` / `referential` からは `params.reference_set: <id>[@version]` で参照できます。版を省略すると最新の公開版のうち有効なコードで評価します。既存の `reference_values` / `values` の指定もそのまま使えます。

## Data Governance & AI(§14)

### AI 資産の境界(§18.2)

| 対象 | 置き場所 | 内容 |
|---|---|---|
| 学習データ、Embedding、ベクトル、RAG コーパスの本文、モデルの重み、レジストリ本体 | **Data Plane** | Control Plane には保存しない。値を送ると 422 で拒否する(黙って捨てない) |
| Dataset Card / Model Card | Control Plane | 目的、収集元、ライセンス、同意の根拠、既知のバイアス、意図した用途と用途外、評価指標(スカラー値のみ)、制約 |
| 版の情報 | Control Plane | Data Plane が計算した manifest のハッシュ、件数、期間、構成元の資産とその版、外部参照の URI |
| AI Lineage | Control Plane | 構成元(版)→ Dataset Version → Model Version → 利用先 |
| 承認と是正判断の記録 | Control Plane | TrainingApproval、ModelRemediation、インシデント |
| 継承した統制属性 | Control Plane | 実効機密度、実効の `contains_pii`、それを決めた上流の名前 |

AI の API は未定義の項目を受け付けません(`extra="forbid"`)。`external_uri` は `data:` と認証情報を含む URI を拒否します。検証エラーの応答とログに値は含まれません。

`data_class=ai` の資産には `ai_kind` を指定します: `training_dataset` / `embedding_set` / `vector_index` / `rag_corpus` / `model`。Card や版を登録した資産の種別は変更できません。Purpose `ai_training` は標準で用意しています(マイグレーション 0012。同じ ID が既にあれば定義を尊重します)。

### AI Lineage

```mermaid
flowchart LR
    S1["構成元の資産(版)"] --> DV["Dataset Version"]
    S2["MDM / Reference Data の版"] --> DV
    DV0["別の Dataset Version"] --> DV
    DV --> MV["Model Version"]
    MV --> U1["デプロイ"]
    MV --> U2["RAG / 用途"]
    S0["上流の資産"] -. lineage_edges .-> S1
    S1 -. "データセット単位のエッジ" .-> DA["学習データの資産"]
    DA -. "データセット単位のエッジ" .-> MA["モデルの資産"]
```

- 版を登録すると `lineage_edges` にもデータセット単位のエッジを反映するので、既存の `impact_analysis` と LIN-001 がそのまま使えます。
- 影響の特定は版単位です。ただし、手動登録や OpenLineage のエッジで繋がる資産は、どの版が影響を受けるか分からないので全版を対象にします(消去と汚染の影響範囲から漏らさないため)。

### 学習利用の承認(§14 Training Data)

```mermaid
stateDiagram-v2
    [*] --> draft: Dataset Version の登録
    draft --> approved: 全 Owner (必要なら Committee) の承認
    draft --> superseded: 消去の発行
    approved --> superseded: 消去の発行
    superseded --> [*]
```

- **AI-001 は承認の記録で判定します**。`controls` に `training_approval` と書いても合格になりません。`training_dataset` は承認の記録がある approved の版、`model` は承認の記録がある学習データだけを使った版が必要です。Embedding、ベクトル、RAG は対象外で、`ai_kind` が未設定なら不合格です。
- **申請の前提**(§14.1 の必須化。満たさなければ 409 `training-prerequisites-failed`): ライセンスの確認結果が `verified`、バイアス評価の参照、構成元の品質検査の実績(Reference Data と Dataset Version を除く)、全構成元の Owner、構成元の PII カラムの `processing_purposes` に `ai_training` があるか同意以外の法的根拠があること。同意と品質は Control Plane が計算して記録し、各承認の時点で計算し直します。
- **承認者**: 構成元の全 Owner(1 人 1 枠)。構成元またはその上流に restricted か PII があれば Committee も必要です。却下は 1 件で確定し、理由が必須です。Committee の枠は、申請者と Owner として決裁した本人には開きません(職務分離)。申請後に Owner が交代したら、取り下げて申請し直します。
- **Model Version**: `draft → approved → deployed`、`approved / deployed ⇄ suspended`、いずれからも `retired`。approved でない Dataset Version を使う版は承認も再開もできません(409 `model-approval-blocked`)。

### 統制属性の継承(§14 Embedding / Vector / RAG)

- 実効機密度 = 自身と Lineage の全上流の最大値、実効の `contains_pii` = 自身と全上流の論理和です。ABAC は実効機密度で判定します。
- Phase 16 から、信頼できる生成元の辺だけを経由して PII のカラムからの DIRECT の経路が無い上流は、PII の継承から外します(`pii_excluded` に理由)。DIRECT の経路で届くカラムは `pii_columns` に示します。
- 解除できるのは、上流で PRV-003 を満たす `anonymized` を経た場合だけです。その資産より先は継承しません。すべての経路で匿名化を経る必要があり、仮名化では解除しません。
- 上流の分類や保護手法の変更、資産の登録、Lineage の追加、版の登録で再計算します。実効値が宣言値を上回ると Owner 宛ての Issue(`policy:AI-002`)を起票し、Certified なら draft に戻します。

| ポリシー | 内容 | 重大度 |
|---|---|---|
| AI-001 | 学習データに承認の記録があること | critical |
| AI-002 | 実効の統制属性が宣言値以下であること | critical |
| AI-003 | `rag_corpus` の構成元は Certified の資産だけ(Embedding とベクトルは透過して元の資産を見る。未登録の構成元や構成元なしは不合格) | high |
| AI-004 | `vector_index` / `embedding_set` の上流に PII があれば `privacy_technique` が `pseudonymized` 以上(`pseudonymized` / `anonymized` / `aggregated`) | critical |

### RAG の判定(§14 RAG Data)

`POST /ai/rag/authorize` は RAG サービスが検索のたびに呼ぶ判定 API です。利用者(`subject`, `roles`, 任意の `clearance`)、コーパス、ヒットした構成元の資産 ID を受け取り、ABAC を通過した資産だけを `allowed_asset_ids` に返します。除外の理由は `denied` に入ります(`clearance` / `restricted_not_granted` / `not_in_corpus` / `unknown_asset` / `deleted`)。

- **ステートレスで、呼び出し頻度が高いので監査には記録しません**(`POST /privacy/consents/check` と同じ扱い)。権限が無い呼び出しの拒否だけは `authz.denied` として記録します。
- 検索語、チャンクの本文、Embedding は受け取りません(送ると 422)。コーパスの構成元でない資産は除外します(fail-close)。
- 判定はコーパスではなく、ヒットした資産ごとに行います。呼び出せるのはコーパスの Custodian だけです。利用者の属性は呼び出し元の申告を信頼します。

### 消去の伝播と ModelRemediation

| 対象 | 動作 |
|---|---|
| `vector_index` / `rag_corpus` / `embedding_set` / `training_dataset` | `contains_pii` の宣言に関わらず ErasureTask を発行する |
| `training_dataset` の該当版 | 発行時に superseded にする。以降の学習には使えない(Legal Hold でデータを残す場合も同じ) |
| `model` | 自動では何もしない。影響を受けたモデル版(`retired` を除く)ごとに未決の ModelRemediation を作る |

対象の特定(`scope`)では `ai_impact` で影響の予定を示すだけで、版と Remediation は承認後の `dispatch` で確定します(却下される可能性があり、superseded は元に戻せないため)。`scope` の後に登録された版も `dispatch` で含めます。

```mermaid
stateDiagram-v2
    [*] --> pending: 消去の発行
    pending --> retrain: Committee の判断
    pending --> unlearn: Committee の判断
    pending --> accepted: Committee の判断(理由と期限が必須、最大 365 日)
    accepted --> pending: 期限切れ(Issue を起票)
    accepted --> retrain: 判断の変更
    accepted --> unlearn: 判断の変更
    retrain --> completed: 再学習した版を報告
    unlearn --> completed: 証跡のハッシュを報告
    completed --> [*]
```

- 消去証明には `ai_assets`(タスクの結果)、`dataset_versions`、`model_remediations`、`model_remediation_open` を含めます。データの消去が完了しても、未完了の是正があれば `model_remediation_open` が true になります。
- 判断は完了するまで変更できます(図は主な遷移です)。`retrain` の完了には、同じモデルの別の版(approved か deployed で、superseded の学習データを使わないもの)が必要です。
- 期限超過は `POST /ai/remediations/escalate-overdue` で Issue を起票します(未決は Committee、実施中は Owner 宛て)。Issue の期限も過ぎるとエスカレーションします。
- `ai_training` の同意が撤回されると、Card の同意の根拠がこの目的の資産と、この目的の PII カラムを持つ資産の下流にある AI 資産の Owner に Issue を起票します。

### API(`/api/v1/ai`)

| API | 内容 |
|---|---|
| `PUT/GET /assets/{id}/dataset-card`, `/model-card` | Card |
| `POST/GET /assets/{id}/dataset-versions`, `GET /dataset-versions/{id}` | Dataset Version(版は変更できない) |
| `POST/GET /assets/{id}/model-versions`, `GET /model-versions/{id}` | Model Version(学習に使った Dataset Version は必須) |
| `POST /model-versions/{id}/approve` / `deploy` / `suspend` / `resume` / `retire` | 状態遷移 |
| `POST/GET /model-versions/{id}/usages` | 利用先(デプロイ、RAG、用途) |
| `POST/GET /dataset-versions/{id}/training-approvals`, `GET /training-approvals/{id}`, `POST .../decision`, `.../withdraw` | 学習利用の承認 |
| `GET /lineage/models/{model_version_id}` | モデル版から起点の資産まで遡る |
| `GET /lineage/assets/{asset_id}/models` | その資産の影響を受けるモデル版 |
| `GET /assets/{id}/inheritance`, `POST /inheritance/recompute` | 実効値の参照と全件再計算(既存データの移行用) |
| `POST /rag/authorize` | RAG の判定 |
| `GET /remediations[/{id}]`, `POST .../decision`, `.../complete`, `POST /remediations/escalate-overdue` | ModelRemediation |
| `POST/GET /incidents`, `GET /incidents/{id}` | データ汚染・リコール。影響するモデル版を特定し、Owner と Committee 宛ての Issue を起票する。任意でモデル版を停止する |

KPI(`/kpi`)に `ai_training_approved_rate`(superseded を除く版のうち approved の比率)、`ai_remediation_pending_count`(期限切れの accept を含む)、`ai_card_complete_rate`、`ai_inheritance_violation_count`、`ai_asset_count` を追加しました。成熟度の判定(§20)には使いません。

## Data Mesh 連携(§15)

### 役割分担

Mesh が実行し、Governance が統制します(§15.1)。

| 対象 | 担当 | 内容 |
|---|---|---|
| 配信基盤、ストレージ、セルフサービス基盤、パイプラインの実行 | **Data Mesh / Data Plane** | 本リポジトリでは実装しない |
| Data Product、Output Port | Control Plane | 統治のための定義だけ。Output Port は Catalog 資産への参照(1 対 1)とアドレス URI を持つ |
| Data Contract | Control Plane | スキーマ、品質と鮮度の SLA、機密度、利用条件の版。値やサンプルは持たない |
| 利用登録 | Control Plane | 誰がどの目的で使っているかの記録。**閲覧権限は与えない**(ABAC と AccessGrant はこれまで通り) |
| CDM の定義 | 外部で定義、Control Plane は写しだけ | JSON Schema の一部として版を取り込み、準拠を検証する。作成・編集・設計の機能は持たない |
| Global / Domain Policy | Control Plane | 出荷基準(DATSIS)。Domain Policy は基準を追加できるが緩められない |

Product の `owner` は単一で、そのドメインの Data Owner(ドメインの Owner、またはそのドメインの資産の Owner)である必要があります。Output Port の資産は Product と同じドメインに属します。提供形態は `table` / `api` / `stream` / `file` です。

### Contract のライフサイクル

```mermaid
stateDiagram-v2
    [*] --> draft: 版の作成 / ODCS の取り込み
    draft --> published: 公開(Product の Owner)
    note right of draft
        足りない版の上げ方は 409
        CDM に準拠しなければ 409
    end note
    published --> deprecated: 新しいメジャー版の公開
    deprecated --> retired: 期間の満了 かつ 有効な利用登録なし
    retired --> [*]
```

公開した版は変更できません。同じメジャー版の中では新しい版が古い版を置き換え、古い版は `published` のまま履歴として残ります。

### メジャー版の更新

```mermaid
sequenceDiagram
    participant CI as Data Plane の CI
    participant CP as Control Plane
    participant PO as Product の Owner
    participant Sub as 利用者と下流の Owner
    participant Com as Committee

    CI->>CP: POST /mesh/contracts/{id}/check (案)
    CP-->>CI: 破壊的な変更、必要な版の上げ方、影響する下流と利用者
    PO->>CP: POST /mesh/contract-versions/{id}/publish (2.0.0)
    Note over CP: 旧版を deprecated にし、満了日時を設定する
    CP->>Sub: Issue で通知、利用条件から外れた登録は suspended
    Sub->>CP: POST /mesh/subscriptions/{id}/migrate
    opt 期間を短縮する場合
        Com->>CP: POST .../majors/1/expedite-retire (理由必須)
    end
    PO->>CP: POST .../majors/1/retire
    Note over CP: 期間中、または有効な利用登録が残っていれば 409
```

Committee が短縮できるのは期間だけです。有効な利用登録(`pending` / `active`)が残っていれば、短縮しても retire できません。

### 互換性の判定表

利用者から見た判定です。定義は [mesh_compat.py](edgf/services/mesh_compat.py) の `COMPAT_RULES` と `TYPE_WIDENING` にあります。足りない版の上げ方で公開しようとすると 409(`contract-version-bump-required`)になり、拒否の証跡を監査に残します。

| 変更 | コード | 必要な版の上げ方 |
|---|---|---|
| カラムの削除 | `column_removed` | major |
| カラムの名前の変更(`renamed_from` で明示。削除と追加が 1 件ずつで型が同じ場合も該当) | `column_renamed` | major |
| 非 null のカラムの追加 | `column_added_required` | major |
| 互換性の無い型の変更 | `type_incompatible` | major |
| 非 null の保証の撤回(非 null → nullable) | `nullable_relaxed` | major |
| 機密度の引き上げ | `sensitivity_raised` | major |
| PII の追加 | `pii_added` | major |
| 品質 SLA の引き下げ(最低スコア、ルールの削除) | `quality_sla_lowered` | major |
| 鮮度 SLA の引き下げ | `freshness_sla_lowered` | major |
| 利用条件の縮小 | `usage_narrowed` | major |
| 準拠する CDM の変更または削除 | `conforms_to_changed` | major |
| nullable なカラムの追加 | `column_added_nullable` | minor |
| 型の拡大 | `type_widened` | minor |
| 非 null の保証の追加 | `nullable_tightened` | minor |
| 機密度の引き下げ | `sensitivity_lowered` | minor |
| PII の削除 | `pii_removed` | minor |
| 品質 SLA の引き上げ | `quality_sla_raised` | minor |
| 鮮度 SLA の引き上げ | `freshness_sla_raised` | minor |
| 利用条件の拡大 | `usage_widened` | minor |
| 準拠する CDM の宣言の追加 | `conforms_to_added` | minor |
| ID や Reference Data の参照の変更 | `column_reference_changed` | minor |
| 説明の変更 | `description_changed` | patch |

型の拡大は `tinyint → smallint → int → bigint → decimal`、`int → double`、`float → double`、`date → timestamp`、`char → varchar → string` と、長さや精度の拡大(`varchar(10) → varchar(20)`)です。`bigint → double` は桁落ちするので含めません。

### スキーマのずれ

Catalog の `schema` や機密度を更新すると、最新の公開版と比較します。ずれていても**更新は拒否せず**、Product の Owner 宛ての Issue(`mesh-drift:<contract_id>`)を起票します。説明の違いはずれに含めません。Catalog 側で `nullable` を宣言していないカラムは、nullable を比較しません。

### 利用登録

- `POST /mesh/subscriptions` は認証済みの全員が使えます。実効機密度か Contract の機密度が confidential 以上なら Product の Owner の承認が必要で、それ以外はすぐに `active` になります。自分の申請は決裁できません(職務分離)。
- 目的は Contract の利用条件に含まれている必要があります(利用条件が空なら、有効な Purpose なら何でも可)。
- `suspended` になるのは、目的そのものの撤回(`POST /privacy/purposes/{id}/withdraw`)と、新しいメジャー版の利用条件から目的が外れたときです。**主体ごとの同意の撤回では止めません**(1 人の撤回で全利用者を止めないため)。`migrate` で別の目的か版に付け替えると再開できます。
- 付け替えで機密度が上がる場合は、承認を取り直します。
- 利用者宛ての Issue は資産に紐付けません。閲覧権限が無い資産に紐付けると、本人が Issue を読めないためです。タイトルには Product の名前だけを含めます。

### CDM の取り込みと準拠の検証

`POST /mesh/cdm/imports`(Committee / admin)で、`data_class=metadata` の Catalog 資産に対応付けて版を取り込みます。取り込んだ版は変更できません。

- **対応する JSON Schema**: `type: object`、`properties`(属性ごとの `type` と `format`。`type` は 1 つの型か、それと `null` の組)、`required`。`$ref` や `allOf` など検証の意味を変えるキーワードは 422 で拒否し、`maxLength` などの制約は警告を返して無視します。
- **検証**: 対応付け(`cdm_mappings`: CDM の属性 → カラム)が正しいか、必須の属性があるか、null を許さない必須の属性のカラムが nullable でないか、型に互換性があるか。不合格なら公開を 409(`contract-not-conforming`)で拒否します。
- **新しい版の取り込み**: そのエンティティに準拠している Contract の最新の公開版を再検証して結果を返し、不合格の Contract の Owner に Issue を起票します。Contract は宣言した版に準拠したままなので、状態は変えません。

### 出荷基準(DATSIS)

`target: product` のポリシーで判定します。すべて合格しないと Product を `published` にできません(409 `product-publish-blocked`)。判定は `GET /mesh/products/{id}/datsis` で確認できます。

| ポリシー | 基準 | 内容 |
|---|---|---|
| MESH-001 | Discoverable | Output Port があり、資産が Catalog に登録済みで同じドメインにある。説明と Glossary の用語がある |
| MESH-002 | Addressable | すべての Output Port にアドレス URI がある |
| MESH-003 | Trustworthy | Contract が公開済みで deprecated でない。品質 SLA が宣言されて満たされている。資産が Certified である |
| MESH-004 | Self-describing | Contract の全カラムと Product に説明があり、Owner がいる |
| MESH-005 | Interoperable | `conforms_to` があれば準拠している。カラム名が命名の標準(`column_name_pattern`)に沿う。`identifier_type` が Catalog と一致する。`reference_set` が公開済みの Reference Data を指す |
| MESH-006 | Secure | Output Port の資産が重大度 high / critical のポリシーに違反していない |

Domain Policy は `target: product` で基準を追加できます(`required_fields` / `field_in`)。同じ ID での上書きはロード時に拒否し、MESH-001〜006 が定義から消えていても公開させません(fail-close)。ポリシー例外は Product の判定には適用しません。

### ODCS の対応表

Contract は [ODCS](https://bitol-io.github.io/open-data-contract-standard/latest/fundamentals) v3 の主要部分で取り込み(`POST /mesh/contracts/import?output_port_id=`、本文は YAML)、書き出します(`GET /mesh/contracts/{id}/export?format=odcs`)。取り込んだ版は常に下書きで、公開には Product の Owner の承認が必要です。書き出しの `apiVersion` は `v3.1.0` です。

| ODCS | 内部のモデル | 備考 |
|---|---|---|
| `apiVersion`, `kind` | (検証のみ) | v3 系の `DataContract` だけを受け付ける |
| `id`, `name` | Contract の `external_id`, `name` | 書き出しの `id` は `external_id`。無ければ Contract の ID |
| `version` | 版 | セマンティックバージョンのみ |
| `status` | 書き出しのみ | `draft` / `active` / `deprecated` / `retired`。取り込みでは無視して警告 |
| `domain`, `dataProduct` | Product | Output Port の Product の値を使う。異なれば警告。`dataProduct` は v3.1.0 で非推奨ですが、対応付けのために書き出します |
| `description.purpose` | 説明 | |
| `schema[0].name`, `.properties[]` | Contract のスキーマ | Output Port と Contract は 1 対 1 なので 2 つ目以降は警告 |
| `properties[].name`, `.description` | カラム | |
| `properties[].physicalType`, `.logicalType` | `type` | 取り込みは `physicalType` を優先。`logicalType` は書き出し時に導出 |
| `properties[].required` | `nullable` の否定 | |
| `properties[].classification` | `pii` または機密度 | `pii` は PII。機密度の値は Contract の機密度に集約して警告 |
| `properties[].customProperties` の `pii`, `edgf.identifier_type`, `edgf.reference_set` | カラムの属性 | ODCS に PII 専用の項目が無いため |
| `schema[0].quality[].id` | 品質 SLA の `rule_ids` | 既存の品質ルールへの参照だけ |
| `slaProperties[]` の `latency`(`ly`, `freshness`) | 鮮度 SLA | 単位は時間か日。書き出しは時間 |
| `authoritativeDefinitions[]` の `type: cdm` | `conforms_to` | `url` は `urn:edgf:cdm:<entity>@<version>` |
| `customProperties` の `edgf.sensitivity`, `edgf.purposes`, `edgf.quality_min_score`, `edgf.cdm_mappings` | 機密度、利用条件、最低スコア、CDM の対応付け | ODCS に対応する項目が無いため |

**対応しないフィールド**(保存せず、項目名だけを `warnings` で返します): `servers`, `team`, `roles`, `support`, `price`, `tenant`, `tags`, `description.usage`, `description.limitations`, `schema[].description` ほかオブジェクトの属性、`properties[]` の `examples`, `primaryKey`, `unique`, `partitioned`, `quality` など、`availability` など鮮度以外の `slaProperties`、`type: cdm` 以外の `authoritativeDefinitions`、`edgf.*` 以外の `customProperties`。`examples` などにデータの値が含まれうるので、警告にも値は含めません。

### API(`/api/v1/mesh`)

| API | 内容 |
|---|---|
| `POST/GET /products`, `GET/PATCH /products/{id}` | Data Product |
| `GET /products/{id}/datsis`, `POST .../publish` / `deprecate` / `retire` | 出荷基準の判定と状態遷移。retire は有効な利用登録が残っていれば 409 |
| `POST /products/{id}/ports`, `GET/PATCH /ports/{id}` | Output Port。閲覧できない資産のポートは名前だけを返す |
| `POST/GET /contracts`, `GET /contracts/{id}` | Data Contract(Output Port ごとに 1 つ) |
| `POST/GET /contracts/{id}/versions`, `GET/PUT /contract-versions/{id}`, `POST .../publish` | 版。変更できるのは下書きだけ |
| `POST /contracts/{id}/check` | Data Plane の CI からデプロイ前に呼ぶ判定(ステートレス。監査には記録しない)。変更の一覧、必要な版の上げ方、品質 SLA と CDM の検証結果、`impact`(`/lineage/{name}/impact` と同じ)、`subscribers` を返す |
| `POST /contracts/{id}/majors/{major}/retire`, `.../expedite-retire` | 旧版の retire と期間の短縮 |
| `POST /contracts/import`, `GET /contracts/{id}/export` | ODCS |
| `POST/GET /subscriptions`, `GET /subscriptions/{id}`, `POST .../decision` / `migrate` / `cancel` | 利用登録 |
| `POST /cdm/imports`, `GET /cdm/entities[/{name}]`, `.../versions[/{version}]` | CDM(読み取りは ABAC) |

KPI(`/kpi`、ドメイン別は `/kpi/domains`)に `mesh_product_published_count`、`mesh_datsis_pass_rate`、`mesh_breaking_rejected_count`、`mesh_subscription_active_count`、`mesh_schema_drift_issue_count` を追加しました。成熟度の判定(§20)には使いません。

## Data Plane 連携(§7 / §17 / §15)

Control Plane はデータ本体を持たず(§18.2)、Data Plane が送るメタデータと集計値で統治します。

```mermaid
flowchart LR
  subgraph DP["Data Plane (各ドメインの基盤)"]
    WH[(ウェアハウス / レイク)]
    PIPE[パイプライン / CI]
    HUB[MDM Hub<br/>Golden Record の値]
    SDK[[edgf-client SDK]]
  end
  subgraph CP["Governance Control Plane (edgf)"]
    DISC[Discovery<br/>資産一覧の差分]
    CAT[Catalog / Policy / KPI]
    MESH[Data Mesh<br/>MESH-007]
    MDM[MDM<br/>由来・内容ハッシュ・版]
    PRIV[Right to Erasure]
  end
  IDP[(IdP<br/>Client Credentials)]
  WH -- 資産一覧 (メタデータだけ) --> SDK
  PIPE -- 品質検査・OpenLineage・更新の通知・Contract の判定 --> SDK
  PIPE -- 利用実績 (集計値) / 到達の報告 --> SDK
  HUB -- match / compose / verify --> SDK
  SDK -- 消去タスクの取得・完了 --> PRIV
  SDK -- トークン --> IDP
  SDK --> DISC & CAT & MESH & MDM
  CP -. 到達の確認 (既定は無効、許可したホストだけ) .-> WH
```

### SDK(`clients/python`、パッケージ `edgf-client`)

サーバとは別のパッケージで、依存は httpx だけです。

```python
from edgf_client import ClientCredentials, EdgfClient, ValidationFailed

credentials = ClientCredentials("https://idp.example.com/token", client_id="dp-sales", client_secret=secret)  # トークンはキャッシュし、期限の 60 秒前に更新
with EdgfClient("https://edgf.example.com", credentials) as edgf:
    edgf.push_inventory("warehouse.sales", [{"qualified_name": "sales.orders", "kind": "table",
                                              "schema": [{"name": "order_id", "type": "bigint", "nullable": False}]}])
    edgf.push_usage([{"qualified_name": "sales.orders", "day": "2026-11-10", "query_count": 120, "user_count": 8}])
    edgf.report_reachability(port_id, True, detail="http_200")
```

| 機能 | メソッド |
|---|---|
| 品質、リネージ、更新の通知 | `submit_quality_run`, `send_openlineage`, `notify_refreshed` |
| Right to Erasure | `erasure_tasks`, `ack_erasure_task`, `complete_erasure_task` |
| Data Contract(CI) | `check_contract` |
| MDM | `mdm_match`, `mdm_compose`, `mdm_verify` |
| RAG | `rag_authorize` |
| Data Plane 連携 | `push_inventory`, `push_usage`, `report_reachability` |

- **再試行**: 安全な操作(GET、判定)と、Idempotency-Key を付けた送信だけを再試行します(接続の失敗、429、502、503、504。`Retry-After` に従う)。鍵の無い送信は二重に処理されるおそれがあるので送り直しません
- **Idempotency-Key**: 送信系には呼び出しごとに鍵を付け、再試行では同じ鍵を使います。サーバは同じ操作者と鍵の要求を 1 回だけ処理し、2 回目以降は保存した応答を返します(`Idempotent-Replayed: true`)。保存するのは要求のハッシュと成功した応答だけで、期間は `EDGF_IDEMPOTENCY_TTL_HOURS`(既定 24 時間)です。**応答にデータの値を含む MDM の compose は保存の対象外で**、SDK は鍵を付けずに再送します(同じ入力なら同じ結果になる)
- **例外**: Problem Details を `Unauthenticated`、`Forbidden`、`NotFound`、`Conflict`、`ValidationFailed`、`PayloadTooLarge`、`RateLimited`、`ServerError`、`ServiceUnavailable` などに変換します。`slug`(`urn:edgf:problem:` を除いた種別)、`request_id`、拡張メンバを持ちます
- **401**: トークンを取り直して 1 回だけ送り直します

### 資産一覧の取り込み(`POST /discovery/snapshots`)

Data Plane はソース(namespace)ごとに**全量**のスナップショットを送ります。Control Plane は**同じソースの**前回と比べます。送られてこなかったソースの資産は消失と判定しません。

| 検出 | 扱い |
|---|---|
| 未登録(Catalog に無い) | 対応表のドメインの Steward に Issue(`discovery-unregistered:<名前>`、重複起票しない)。Catalog への登録は既存の `POST /assets` で行います |
| 消失(前回あって今回無い) | Catalog にあれば、その資産の Steward に Issue |
| スキーマの非破壊の変化 | Catalog の schema に自動で反映(actor は `system:discovery`)。既存の更新の経路を通すので、Certification のリセットと Contract とのずれの検出が働きます。説明や PII などの業務のメタデータは残します |
| スキーマの破壊的な変化 | 反映せず、保留にして Issue を起票。`POST /discovery/schema-changes/{id}/apply`(対象資産の Steward / Owner)で反映します |

変化の判定には Phase 11 の互換性の判定表を使います。ソースとドメインの対応は `policies/discovery/*.yaml` で宣言します(起動時に検証し、不正なら起動を止めます。Policy Bundle の承認の対象ではありません)。

```yaml
version: 1
sources:
  - {namespace: "warehouse.sales", domain: sales, steward: sales-steward}  # namespace はシェル形式のパターン。上から順に照合
```

**受け付けるのはメタデータだけです**: 資産は `qualified_name`、`kind`、`schema`、`updated_at`。カラムは `name`、`type`、`nullable` です。それ以外の項目は 422 で拒否し、値・サンプル・統計の項目(`sample`、`values`、`rows`、`min`、`max`、`top_values`、`statistics` など)はその理由を返します。エラーの応答に入力の値は含めません。本文の大きさは `EDGF_MAX_BODY_BYTES` に従います。

### 利用実績(`POST /usage/daily`)

受け取るのは **資産 × 日 × 問い合わせの件数 × 利用者の数** の集計値だけです。

- 利用者を特定する項目(`user_id`、`users`、`email`、`subject`、`client_ip`、`query_text`、`sql`、`session_id` など)と未知の項目は 422 で拒否します
- 同じ資産と日の再送は上書きします(Data Plane が集計し直して送り直せます)。Catalog に無い資産は保存せず、`unregistered` として返します
- 日付は今日から 400 日前まで、`user_count` は `query_count` 以下です
- KPI: `usage_active_assets_30d`(直近 30 日に問い合わせのあった資産)、`usage_query_count_30d`、`unused_asset_count`。利用実績が 1 件も無いうちは `null` です

### 到達の確認(MESH-007)

既定は **Data Plane からの報告(push)** です。`POST /mesh/ports/{id}/reachability`(ポートの資産の Custodian)で、`reachable` と短い `detail` を送ります。未来の時刻は受け取った時刻に丸めます。

Control Plane から確かめる方式は `EDGF_PROBE_ENABLED=false` が既定で、無効の間は外部に一切接続しません。有効にする場合の安全策:

- `EDGF_PROBE_ALLOWED_HOSTS`(許可するホストの一覧。`*.example.com` の形も可)が必須です。無ければ起動を拒否します
- https だけを使い、URI に認証情報があれば接続しません。証明書はホスト名で検証します
- 名前を解決したあと、解決したアドレスのどれかがプライベート、ループバック、リンクローカル、予約、マルチキャストなら拒否します
- 確認したアドレスにそのまま接続します(接続のときに解決し直さないので、DNS の再束縛で内部に向けられません)
- リダイレクトに従いません。タイムアウトは `EDGF_PROBE_TIMEOUT_SECONDS`(既定 5 秒)です

`MESH-007` は、全ポートに `EDGF_REACHABILITY_MAX_AGE_HOURS`(既定 24 時間)以内の到達の報告があることを求めます。

### MDM Hub の参照実装

`examples/mdm-hub/` に、SDK を使う最小の MDM Hub があります(サーバのパッケージには含めません)。Golden Record の値を自分の SQLite に持ち、match → compose → verify と消去タスクの処理を示します。

## Governance Process(§16)

### 承認エンジンと inbox の構成

```mermaid
flowchart LR
    subgraph 新しいエンジン
        T["approval_types.py<br>種別、ステップ、SLA、ハンドラ"] --> E["approvals.py<br>申請、判断 (追記のみ)、SLA"]
    end
    subgraph 既存の個別の承認
        S1[ポリシー例外]
        S2[MDM ルール]
        S3[Reference Data]
        S4[学習データ]
        S5[Contract]
        S6[利用登録]
        S7[CDM]
        S8[Remediation]
    end
    E --> A[ApprovalSource]
    S1 & S2 & S3 & S4 & S5 & S6 & S7 & S8 --> A
    A --> I["GET /approvals/inbox<br>判断できて閲覧できる案件"]
```

既存の承認の API は変えていません。アダプタは「今この人が判断できるもの」を読むだけで、各案件の `action` が判断する API を示します。

| 種別 | 内容 | 承認者 | SLA | 承認されたときの処理 |
|---|---|---|---|---|
| `access_request` | 利用者の閲覧の申請 | 対象資産の Owner | 3 営業日 | AccessGrant を作る(Owner が直接付与する API も残っています) |
| `external_share` | 社外提供(§10 Share) | Owner → Committee | 5 営業日 | 証跡とイベントを残す(提供は Data Plane) |
| `policy_change` | Policy の変更 | 範囲による(後述) | 10 営業日 | Bundle を approved にする |
| `retention_delete` | 保持期限による削除 | 対象資産の Owner | 5 営業日 | `lifecycle.delete_requested` |
| `access_review` | 権限の棚卸 | 対象資産の Owner | キャンペーンの期限 | 却下は取り消し |
| `ownership_attestation` | Owner などの証明 | 対象資産の Owner | キャンペーンの期限 | 却下は修正の Issue |

- 申請できるのは `access_request` と `external_share` だけです(`POST /approvals`)。他はシステムが作ります。
- ステップは承認者のルール(authz の `Rule`)と定足数の組です。判断の記録は追記のみです。
- **職務分離**: 申請者本人と対象資産の Custodian は承認できません。Custodian を含むルールはロード時に拒否します。1 人が同じ案件で判断できるのは 1 回だけです。
- SLA を超過すると Issue を起票し、Issue の期限も過ぎるとエスカレーションします。

CDM の取り込みは Committee の操作そのものが承認で、承認待ちの状態がありません。そのため inbox の CDM には、新しい版の再検証で不合格になった Contract の対応待ちを出します。

### Policy の変更のライフサイクル

```mermaid
stateDiagram-v2
    [*] --> active: 初回の起動 (policy.bootstrap)
    [*] --> proposed: POST /policies/bundles、/reload、/bundles/rollback
    note right of proposed
        提案の時点で検証し (不正は 422)
        影響評価 (dry-run) を記録する
    end note
    proposed --> approved: 全ステップの承認
    proposed --> rejected: 却下、取り消し
    approved --> active: POST .../publish (Committee)
    active --> superseded: 次の版の公開
    superseded --> [*]
    rejected --> [*]
```

- **実行時の評価は active の版だけを使います。** 公開されると内容ハッシュが変わるので、複数のプロセスでも追従します。
- **初回の起動**で active の版が無いときだけ、ディスクの内容を v1 として取り込みます。以後ディスクを変更しても評価は変わらず、`/ready` の `checks.policy_drift` に示します(503 にはしません)。
- **承認者**: Global Policy を含む変更は Committee。Domain Policy だけの変更は、そのドメインの Owner と Committee です。
- **提案できる人**: admin、Committee、ドメイン内の Owner(Domain Policy だけ。全ドメインに所属していても Global は提案できません)。
- **提案時の検証**: Global の上書き、未知の check type、YAML の不正、パスと宣言の不一致(`domains/<ドメイン>.yaml` は `scope: domain` と同じ `domain` が必須)。
- **影響評価**: `GET /policies/bundles/{version}/diff` で、Policy の定義の差分、新しく不合格になる資産、合格になる資産、ドメイン別の準拠率の変化を返します。閲覧できない資産は件数だけです。
- **公開の条件**: 提案の後に active の版が変わっていれば 409 です。影響評価をやり直すため、提案し直してください。
- **切り戻し**: `POST /policies/bundles/rollback` で 1 つ前の版の提案を作ります。切り戻しにも承認と公開が必要です。

### イベント(CloudEvents 1.0)

状態変更と同じトランザクションで `outbox_events` に書き込みます(巻き戻ればイベントも残りません)。`source` は `urn:edgf:control-plane` です。payload には監査と同じガードを適用し、PII と生の識別子を含むものは拒否します。

| `type` | 発生のタイミング | `data` の主な項目 |
|---|---|---|
| `edgf.issue.opened` / `.updated` / `.escalated` | Issue の起票、更新、エスカレーション | `issue_id`, `asset_id`, `severity`, `status`, `escalation_level` |
| `edgf.approval.requested` / `.approved` / `.rejected` / `.cancelled` / `.expired` | 承認の申請と確定 | `approval_id`, `type`, `target_id`, `asset_id`, `status`, `due_at` |
| `edgf.access.granted` / `.revoked` | 閲覧の付与と取り消し(棚卸を含む) | `grant_id` または `target_id`, `asset_id`, `subject` |
| `edgf.share.approved` | 社外提供の承認 | `approval_id`, `asset_id`, `recipient`, `expires_at` |
| `edgf.asset.delete_requested` | 削除の指示 | `asset_id`, `qualified_name` |
| `edgf.erasure.task.dispatched` | 消去タスクの発行 | `task_id`, `erasure_request_id`, `asset_id`, `ai_kind`, `due_at` |
| `edgf.consent.expired` | 保持期間による同意の失効 | `purpose_id`, `count` |
| `edgf.contract.published` | Contract の公開 | `contract_id`, `contract_version_id`, `version`, `breaking` |
| `edgf.policy.published` | Policy の公開 | `version`, `content_hash`, `superseded`, `rollback_of` |
| `edgf.review.campaign_created` | レビューキャンペーンの作成 | `campaign_id`, `kind`, `period`, `due_at` |

`edgf.erasure.task.dispatched` には `subject_ref` を載せません。受信側は既存の `GET /privacy/erasure-tasks` でタスクを取得します(pull 型の API はそのまま使えます)。

### Webhook

`POST /webhooks`(admin)で配信先を登録します。`event_types` は `edgf.issue.*` のように指定でき、空なら全種別です。

- **秘密鍵は保存しません。** `secret_ref: env:<環境変数名>` の外部参照だけを持ちます。解決できなければ送信しません(署名なしで送ることはありません)。KMS などを使う場合は `SecretResolver` を実装してください。
- **配信は at-least-once です。** 受信側はイベント ID(CloudEvents の `id`)で冪等にしてください。
- **再試行**は指数バックオフで、`EDGF_WEBHOOK_MAX_ATTEMPTS` 回で届かなければデッドレターに移します(`GET /outbox/dead-letters`、`POST /outbox/dead-letters/{id}/redeliver`)。
- **配信先の制限**: リダイレクトは追いません。本番では https だけで、内部ネットワークの宛先には配信しません(SSRF 対策)。

署名は `X-EDGF-Signature: t=<UNIX 時刻>,v1=<HMAC-SHA256>` で、対象は `<UNIX 時刻>.` と本文のバイト列です。

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    timestamp = int(parts["t"])
    if abs(time.time() - timestamp) > tolerance:   # 古い通知の再送を拒否する
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])

# 受信側の冪等性: event = json.loads(body); 処理済みの event["id"] なら 2xx だけを返す
```

### 定期レビュー

| 種別 | 周期 | 内容 | 期限と猶予を過ぎたとき |
|---|---|---|---|
| `access_review` | 四半期 | 有効な AccessGrant と利用登録を、対象資産の Owner が継続か取り消しか判断する | 期限で Issue、猶予を過ぎると**自動で取り消し**(`review.auto_revoke` を監査に記録) |
| `ownership_attestation` | 年次 | 各資産の Owner が、Owner・Steward・分類・保持期間が正しいことを証明する | 期限で Issue、猶予を過ぎると Committee にエスカレーション(権限は変えない) |
| `policy_review` | 年次 | 365 日以上見直されていない Policy の Issue を起票する | `POST /policies/{id}/review` で記録すると解決する |

判断は `POST /reviews/items/{id}/decision` または inbox の案件で行います。

### ジョブ

外部の依存を増やさず、`python -m edgf.jobs run <job>` を外部の cron や CronJob から呼びます。各ジョブは冪等で、DB のリースによって同時に 1 つしか実行されません(他で実行中なら `skipped` で正常終了します)。

| ジョブ | 既定の間隔 | 内容 |
|---|---|---|
| `issue_escalate` | 1 時間 | SLA 超過の Issue のエスカレーション |
| `erasure_escalate` | 1 時間 | 消去要求の期限超過の処理 |
| `mdm_review_escalate` | 1 時間 | MDM の裁定キューの期限超過の処理 |
| `ai_remediation_escalate` | 1 時間 | ModelRemediation の期限超過の処理 |
| `policy_evaluate` | 1 日 | 全資産の自動監査と Issue 化 |
| `retention_detect` | 1 日 | 保持期限の検出と削除の承認申請(Legal Hold 中は作らない) |
| `consent_expire` | 1 日 | 保持期間を過ぎた Consent の失効(`expired` の行を追記する) |
| `outbox_deliver` | 1 分 | Outbox の配信 |
| `approval_sla` | 1 時間 | 承認の SLA 超過の処理 |
| `review_campaigns` | 1 日 | レビューキャンペーンの作成と期限の処理 |
| `kpi_snapshot` | 1 日 | KPI の日次のスナップショット(同じ日に 2 回実行しても 1 日分) |
| `audit_anchor` | 1 時間 | 監査チェーンのアンカリング(`EDGF_ANCHOR_ENABLED=true` のときだけ動作) |
| `ui_session_cleanup` | 1 時間 | 期限切れの UI のセッションとログインの途中の状態を削除する(Phase 17) |
| `unused_assets` | 1 日 | `EDGF_UNUSED_DAYS`(既定 90 日)使われていない資産を、廃止の候補として Owner に Issue で知らせる(利用実績が届くまでは何もしない) |
| `reachability_probe` | 1 時間 | Control Plane からの Output Port の到達の確認(`EDGF_PROBE_ENABLED=true` のときだけ動作) |

```cron
# cron の例 (環境変数は EDGF_DATABASE_URL などを設定しておく)
* * * * *   python -m edgf.jobs run outbox_deliver
0 * * * *   python -m edgf.jobs run issue_escalate
5 * * * *   python -m edgf.jobs run erasure_escalate
10 * * * *  python -m edgf.jobs run mdm_review_escalate
15 * * * *  python -m edgf.jobs run ai_remediation_escalate
20 * * * *  python -m edgf.jobs run approval_sla
0 2 * * *   python -m edgf.jobs run policy_evaluate
10 2 * * *  python -m edgf.jobs run retention_detect
20 2 * * *  python -m edgf.jobs run consent_expire
30 2 * * *  python -m edgf.jobs run review_campaigns
40 2 * * *  python -m edgf.jobs run kpi_snapshot
25 * * * *  python -m edgf.jobs run audit_anchor
50 2 * * *  python -m edgf.jobs run unused_assets
35 * * * *  python -m edgf.jobs run reachability_probe
```

```yaml
# Kubernetes CronJob の例
apiVersion: batch/v1
kind: CronJob
metadata:
  name: edgf-outbox-deliver
spec:
  schedule: "* * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: job
              image: edgf:latest
              command: ["python", "-m", "edgf.jobs", "run", "outbox_deliver"]
              envFrom:
                - secretRef: {name: edgf-env}
```

cron を使わない場合は `python -m edgf.jobs serve` を常駐させます(標準ライブラリだけで動き、複数のプロセスで動かしてもリースで重複しません)。失敗したジョブの終了コードは 1 です。

実行の状況は `GET /ops/jobs` で確認できます。間隔の 3 倍を過ぎても成功していないジョブは、`/ready` の `checks.jobs_overdue` に示します(503 にはしません)。

### 保持期限の自動執行

- Purpose の `retention_days` を過ぎた Consent は `expired` になります。Consent は追記のみなので `expired` の行を追記し、監査には件数だけを記録します。
- 資産の保持期限が来ると、削除の承認申請を自動で作ります。承認されると `lifecycle.delete_requested` を記録します(`active` は状態遷移の規則に沿って `archived` を経由します)。
- Legal Hold 中の資産は申請を作らず、申請の後に Legal Hold が設定されたら取り消します。承認の時点でも 409 で止めます。Owner が却下した資産は、`EDGF_RETENTION_REJECT_COOLDOWN_DAYS` の間は申請を作り直しません。Owner が未割当なら Committee 宛ての Issue を起票します。

## UI(§7 / §9 / §16 / §17 / §20)

`/ui/` で配信する画面です。素の JavaScript(ES Modules)、HTML、CSS だけで作り、フレームワーク、ビルド工程、npm の依存、外部の CDN やフォントは使いません。データはすべて REST API から取り、認証、認可、ABAC、職務分離は API が判定します(UI 用の別の判定はありません)。

Phase 17 から、承認、棚卸と証明、Issue の更新を UI から行えます。資産のメタデータの編集はできません。

### 構成(BFF)

```mermaid
sequenceDiagram
    participant B as ブラウザ (UI)
    participant C as Control Plane (BFF + API)
    participant I as IdP
    B->>C: GET /ui/auth/login?return_to=#/inbox
    C->>C: state / nonce / code_verifier を保存 (10 分)
    C-->>B: 303 IdP へ (__Host-edgf_login: 参照だけ, SameSite=Lax)
    B->>I: 認可の要求 (PKCE S256, nonce)
    I-->>B: 303 /ui/auth/callback?code&state
    B->>C: GET /ui/auth/callback
    C->>I: トークンの要求 (code + code_verifier + client_secret)
    I-->>C: アクセストークン / ID トークン
    C->>C: 検証して Principal と auth_time だけをセッションに保存 (トークンは捨てる)
    C-->>B: 303 /ui/#/inbox (__Host-edgf_session: HttpOnly, Secure, SameSite=Strict)
    B->>C: GET /ui/session (CSRF のトークンを受け取る)
    B->>C: POST /api/v1/... (Cookie + X-CSRF-Token + Origin)
```

| パス | 内容 |
|---|---|
| `edgf/ui/bff.py` | ログイン(機密クライアント)、セッション、CSRF と Origin の確認 |
| `edgf/services/ui_sessions.py` | セッションと CSRF のトークン(ハッシュだけを保存)、失効、ステップアップの判定 |
| `edgf/ui/routes.py` | 静的ファイル、`/ui/config.json`、セキュリティヘッダ |
| `edgf/ui/static/lib/session.js` | ログイン中の利用者(`GET /ui/session` の応答)。メモリにだけ置く |
| `edgf/ui/static/lib/api.js` | REST API の読み取り(GET だけ) |
| `edgf/ui/static/lib/actions.js` | **書き込みの唯一の入口**。GET 以外の fetch はここからだけで、必ず `X-CSRF-Token` を付ける |
| `edgf/ui/static/lib/forms.js` | 判断のフォーム(承認箱、棚卸、Issue で共通) |
| `edgf/ui/static/pages/` | 各画面 |
| `edgf/ui/static/i18n/` | 文言の辞書(`ja` が既定、`en`) |

ブラウザの JavaScript はトークンにもセッション ID にも触れません(Cookie は HttpOnly)。ブラウザが IdP と通信するのはログインの画面遷移だけです。これは静的な検査(`tests/test_ui_server.py`)と、実ブラウザでのネットワークの監視(`tests/e2e/`)で確認しています。データの表示はすべて `textContent` 相当と DOM API で行い、`innerHTML` や `eval` は使いません。

### セッションと CSRF

| 項目 | 仕様 |
|---|---|
| セッションの Cookie | `__Host-edgf_session`。`Secure`、`HttpOnly`、`SameSite=Strict`、`Path=/`(`Domain` なし) |
| セッション ID | 256 ビットの乱数。DB には SHA-256 のハッシュだけを保存します。**ログインのたびに新しく発行**し、以前のセッションは消します(固定攻撃の対策) |
| 保存する内容 | ログイン時に検証した Principal の写し(subject、roles、clearance、domains)、表示名、`auth_time`。IdP のトークン(アクセス、リフレッシュ、ID)は**サーバにもブラウザにも保存しません** |
| 失効 | 無操作が `EDGF_SESSION_IDLE_MINUTES`(既定 30 分)、ログインから `EDGF_SESSION_MAX_HOURS`(既定 8 時間)、ログアウト。期限切れの行はジョブ `ui_session_cleanup` が削除します |
| ログインの途中の状態 | state、nonce、code_verifier はサーバに保存し、`__Host-edgf_login`(`SameSite=Lax`、10 分)には参照の乱数だけを入れます。1 回だけ使えます |
| CSRF の同期トークン | セッションごとに 1 つ(セッション ID から HMAC で導く)。`GET /ui/session` で返します。DB にはハッシュだけを保存し、照合は定数時間です |
| 確認の対象 | Cookie の経路の GET / HEAD / OPTIONS 以外のすべての要求。`X-CSRF-Token` が一致し、`Origin` が UI のオリジンと一致し、`Sec-Fetch-Site` があれば `same-origin` であること。どれかを満たさなければ 403(`csrf-failed`)。Bearer の経路は確認しません |
| 資格情報の重複 | `Authorization`(dev モードでは `X-Actor`)とセッションの Cookie の両方が付いていたら 400(`ambiguous-credentials`) |
| 戻り先 | `return_to` は UI の中の画面(`#/...`)だけ。外部の URL には転送しません |

UI のオリジンは `EDGF_UI_PUBLIC_URL` で指定します(省略時は要求の URL。TLS を終端するプロキシの後ろでは指定してください)。

### ステップアップ

権限表で ✓ の付いた操作(承認行為)を Cookie の経路で行うには、ログインから `EDGF_STEP_UP_MINUTES`(既定 15 分)以内であることが必要です。古ければ 401 の Problem(`step-up-required`)を返し、UI は `max_age=0` を付けて IdP で再ログインさせ、元の画面に戻します。

- 判定は認可のあとです。権限の無い利用者には従来どおり 403 を返します
- Bearer の経路(サービスの主体)には適用しません
- 読み取り(GET)には適用しません

### UI でできる操作

| 画面 | 操作 | 呼ぶ既存の API | 必要な権限 |
|---|---|---|---|
| 承認箱(`#/inbox`) | 新しい承認エンジンの承認・却下 | `POST /approvals/{id}/decision` | `approval.decide` ✓ |
| | Policy の変更の公開 | `POST /policies/bundles/{version}/publish` | `policy.publish` ✓ |
| | Policy 例外の承認・却下 | `POST /policies/exceptions/{id}/decision` | `exception.decide` ✓ |
| | 学習利用の承認・却下 | `POST /ai/training-approvals/{id}/decision` | `ai.training.approve` ✓ |
| | 利用登録の承認・却下 | `POST /mesh/subscriptions/{id}/decision` | `mesh.subscription.approve` ✓ |
| | MDM の生成規則の承認 | `POST /mdm/entities/{type}/approve` | `mdm.rule.approve` ✓ |
| | Reference Data の版の公開 | `POST /reference-data/sets/{id}/versions/{n}/publish` | `refdata.publish` ✓ |
| | Contract の版の公開 | `POST /mesh/contract-versions/{id}/publish` | `mesh.contract.publish` ✓ |
| | CDM の再検証の対応(Issue の更新) | `PATCH /issues/{id}` | `issue.update` |
| | モデルの是正の判断(retrain / unlearn / accept) | `POST /ai/remediations/{id}/decision` | `ai.remediation.decide` ✓ |
| 棚卸と証明(`#/reviews`) | 権限の継続・取り消し、Owner の証明・修正が必要 | `POST /reviews/items/{id}/decision` | `review.decide` ✓ |
| Issue(`#/issues`) | 状態、担当者、コメントの更新 | `PATCH /issues/{id}` | `issue.update` |

- **却下、取り消し、修正が必要には理由が必須です**(フォームと API の両方で確かめます)
- 承認箱の各案件は、アダプタが返す `operations`(送る先の API、固定の本文、入力の項目、ステップアップの有無)をそのまま使います。既存の API に却下の無い種類(MDM の生成規則、Reference Data、Contract、CDM、モデルの是正)は、既存の操作だけを出します
- 職務分離(申請者本人の承認の禁止、Custodian の禁止)と ABAC は API の判定のままです
- 操作はキーボードだけで行えます(フォームは最初の項目にフォーカスが移り、Esc で閉じて元のボタンに戻ります)

### 起動方法

```bash
docker compose -f docker-compose.yml -f docker-compose.idp.yml up --build
# http://localhost:8000/ui/ を開き、下のテストユーザーでログインする
```

Keycloak はローカル開発用のツールで、Control Plane の実行時の依存ではありません。設定は OIDC の標準項目だけなので、別の IdP にも同じ項目で接続できます。

| ユーザー | ロール | clearance | ドメイン |
|---|---|---|---|
| `dg-admin` | admin | restricted | `*` |
| `dg-committee` | committee | restricted | `*` |
| `dg-owner` | owner | confidential | `sales` |
| `dg-steward` | steward | confidential | `sales` |
| `dg-custodian` | custodian | confidential | `sales` |
| `dg-consumer` | consumer | internal | なし |

パスワードはすべて `edgf-dev-only` です(開発専用)。データは API で登録してください(例: `dg-admin` のトークンを使うか、dev モードの `./scripts/demo.sh`)。

### OIDC のクライアントの設定

IdP に、次の内容で**機密クライアント**を登録します(Phase 17 で公開クライアントから変わりました)。設定例は [docker/keycloak/edgf-realm.json](docker/keycloak/edgf-realm.json) です。

| 項目 | 値 |
|---|---|
| クライアントの種類 | 機密クライアント。認証の方式は `client_secret_post` |
| フロー | Authorization Code + PKCE(`S256`) |
| リダイレクト URI | `<UI のオリジン>/ui/auth/callback` |
| ログアウト後のリダイレクト URI | `<UI のオリジン>/ui/` |
| CORS | 不要(ブラウザは IdP の API を呼びません) |
| アクセストークンの claim | `aud` に `EDGF_OIDC_AUDIENCE`。ロール、ドメイン、clearance は `EDGF_CLAIM_*` で指定した claim |
| ID トークン | `aud` に client_id、`nonce`。`auth_time` があればステップアップに使います(無い IdP では、ログインの完了の時刻とみなします) |

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `EDGF_UI_OIDC_CLIENT_ID` | (空) | UI のクライアント。空なら UI のログインは無効 |
| `EDGF_UI_CLIENT_SECRET` | (空) | クライアントの秘密。環境変数からだけ読みます。production で UI を有効にするなら必須 |
| `EDGF_UI_OIDC_ISSUER` / `EDGF_UI_OIDC_AUDIENCE` / `EDGF_UI_OIDC_SCOPES` | API の設定 / `openid,profile` | 省略時は API の検証の設定を使います |
| `EDGF_UI_OIDC_DISCOVERY_URL` | `<issuer>/.well-known/openid-configuration` | ディスカバリの取得先。コンテナの中から IdP の名前が違う場合に指定します(issuer の一致は確かめます) |
| `EDGF_UI_PUBLIC_URL` | 要求の URL | ブラウザから見える UI のオリジン。リダイレクト URI と Origin の確認に使います。production では https |
| `EDGF_SESSION_IDLE_MINUTES` / `EDGF_SESSION_MAX_HOURS` / `EDGF_STEP_UP_MINUTES` | 30 / 8 / 15 | セッションの失効とステップアップ |

UI は dev モードに対応しません(dev モードでは `/ui/auth/login` は 404 です)。ログアウトは IdP の `end_session_endpoint` に `client_id` と `post_logout_redirect_uri` を付けて移動します(`id_token_hint` を持たないので、IdP によっては確認の画面が出ます)。

### セキュリティヘッダ

`/ui` のすべての応答に付けます。

| ヘッダ | 値 |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' <issuer のオリジン>; object-src 'none'` |
| `Referrer-Policy` | `no-referrer` |
| `X-Content-Type-Options` | `nosniff` |
| `Permissions-Policy` | `accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()` |

### 画面の一覧

| 画面 | パス | 使う API |
|---|---|---|
| [カタログの検索](docs/ui/catalog.png) | `#/catalog` | `/assets`, `/domains` |
| [資産の詳細](docs/ui/asset.png) | `#/assets/{id}` | `/assets/{id}`, `/quality/...`, `/policies/evaluate/...`, `/glossary/terms`, `/mesh/products?asset_id=`, `/ai/assets/...` |
| [用語集](docs/ui/glossary.png) | `#/glossary`, `#/glossary/{id}` | `/glossary/terms` |
| [リネージと影響分析](docs/ui/lineage.png) | `#/lineage/{name}?depth=` | `/lineage/{name}/graph`, `/lineage/{name}/impact` |
| [KPI と成熟度](docs/ui/dashboard.png) | `#/dashboard` | `/kpi`, `/kpi/history` |
| [Data Product](docs/ui/products.png) | `#/products` | `/mesh/products/overview` |
| [Issue](docs/ui/issues.png) | `#/issues` | `/issues`、更新は `PATCH /issues/{id}` |
| [承認箱](docs/ui/inbox.png) | `#/inbox` | `/approvals/inbox`。判断は案件ごとの `operations` の既存の API |
| 棚卸と証明 | `#/reviews` | `/reviews/items`、判断は `POST /reviews/items/{id}/decision` |
| [ジョブの状態](docs/ui/jobs.png) | `#/jobs` | `/ops/jobs` |

![KPI ダッシュボード](docs/ui/dashboard.png)

![リネージ](docs/ui/lineage.png)

### UI のために追加した API

| API | 内容 | 認可 |
|---|---|---|
| `GET /kpi/history?scope=&domain=&from=&to=` | KPI の推移(古い順、ページング)。`scope` は `enterprise` か `domains` | 認証済みの全員。ドメイン別は、閲覧できる資産があるか所属するドメインだけ |
| `GET /lineage/{name}/graph?depth=` | グラフの描画用のノードとエッジ | 認証済みの全員。閲覧できない資産は名前をラベルに置き換えて深さだけを返し、接するエッジの `job` も伏せる |
| `GET /mesh/products/overview` | DATSIS の合否、Contract の版(閲覧できる Output Port だけ)、有効な利用登録の数 | 認証済みの全員 |
| `GET /mesh/products?asset_id=` | 資産を Output Port に持つ Product(既存の API への絞り込みの追加) | 閲覧できない資産なら結果は空 |

`/kpi` に `privacy_pii_asset_count`、`privacy_erasure_open_count`、`privacy_erasure_overdue_count` を、`maturity` に `level_name` と `next_level`(次のレベルの条件)を追加しました。成熟度の判定そのものは変えていません。

### テスト

```bash
pip install -e ".[dev,e2e]" && playwright install chromium
EDGF_E2E=1 pytest -q tests/e2e                                       # IdP を模して実ブラウザで実行する
EDGF_E2E=1 EDGF_UI_SCREENSHOTS=docs/ui pytest -q tests/e2e           # スクリーンショットも撮る
EDGF_E2E_KEYCLOAK=http://localhost:8000 pytest -q tests/e2e_keycloak # docker-compose.idp.yml を起動しておく
```

CI では `playwright` ジョブが常に実行されます。Keycloak との E2E は手動で起動します(`.github/workflows/e2e-keycloak.yml`)。

## 性能

条件は PostgreSQL 16、資産 10 万件(restricted 5%)、ドメイン 50、Lineage 深さ 10 の分岐、Product 500、承認待ち 1,000 件です。手順、原因、判断の記録は [docs/operations/performance.md](docs/operations/performance.md) にあります。

| シナリオ | 目標 | 開発機の結果 | 参照環境 | CI の p95(回帰の検出用。20 回の範囲。書き込みの 2 シナリオは 2 回) |
|---|---|---|---|---|
| 検索 | p95 300ms | 達成(55ms。書き込みの直後は 90〜150ms) | 未測定 | 120〜250ms(`pg_trgm` の導入後の 7 回) |
| 資産詳細 | p95 200ms | 達成(6ms) | 未測定 | 6〜16ms |
| 影響分析(深さ 10) | p95 1s | 達成(590ms) | 未測定 | 620〜1,500ms(目標ちょうどの 1,000ms が 4 回) |
| カラム単位の影響分析(深さ 10、Phase 16) | p95 1s | 達成(320〜340ms) | 未測定 | 660〜900ms(2 回) |
| RAG 認可(50 ヒット) | p95 150ms | 達成(27ms) | 未測定 | 34〜83ms |
| Contract check | p95 1s | 達成(300ms) | 未測定 | 380〜850ms |
| inbox(1,000 件) | p95 500ms | 達成(280ms) | 未測定 | 330〜740ms |
| Product 一覧(500 件) | p95 2s | 達成(240ms) | 未測定 | 250〜360ms |
| 監査の書き込み | 200 件/秒以上 | 達成(277 件/秒) | 未測定 | CI では 141〜329 件/秒。ばらつきが大きく判定は保留 |
| 資産の更新 | 未設定 | 25〜32ms | 未測定 | 18〜26ms |
| 資産の登録 | 未設定 | 27〜43ms | 未測定 | 24〜36ms |

**目標(SLO)の判定は参照環境で行います。参照環境での測定はまだしていません。** CI は回帰の検出用で、目標の判定には使いません。役割の分担と参照環境の仕様は [docs/operations/performance.md](docs/operations/performance.md) にあります。

資産の更新と登録は、検索の索引による書き込みの回帰を監視するためのシナリオです。性能の目標は設定していません。

開発機は macOS と Docker 上の PostgreSQL で各 20 秒です。CI は GitHub Actions の `ubuntu-24.04` で各 30 秒で、負荷の発生、4 ワーカーの API、PostgreSQL が同じランナーに同居します。

**100 万件のプロファイルは用意しましたが、未検証です**(`perf.yml` を `profile=million` で手動実行します)。設計書 §19 の「資産数百万件、毎秒万件級」と水平拡張も未検証です。

性能の CI(`.github/workflows/perf.yml`)は手動と週次で動きます。基準値(`perf/baseline.json`)より 30% 以上悪化したシナリオだけを最大 2 回まで再計測し、3 回のうち最も良い p95(監査の書き込みは最も良い件数)でも悪化していれば失敗します。各回の結果は artifact と job summary に出ます。ランナーの種類で全体の速さが変わるので、小さな悪化は検知できません。基準値を更新するには、実行結果の artifact を `perf/baseline.json` として commit します。

## セキュリティテストの範囲

| 対象 | 内容 | テスト |
|---|---|---|
| ルートの網羅 | `app.routes` を列挙し、GET / HEAD / OPTIONS 以外のすべてが `PERMISSIONS` で保護されていること。無ければ失敗 | `tests/test_security_authz.py` |
| 認可の総当たり | `PERMISSIONS` × 6 ロール × ドメインの内外 × 関係で、許可と拒否が権限表どおりで、拒否が `authz.denied` で監査に残ること。全更新系ルートを無関係の利用者で呼ぶ | `tests/test_security_authz.py` |
| ABAC の漏洩 | restricted 資産に目印(名前、説明、カラム名、transformation など)を入れ、権限の無い利用者で全 GET ルートを呼び、本文とヘッダに無いこと。パラメータは自動で埋める | `tests/test_security_leak.py` |
| JWT への攻撃 | `alg=none`、HS256 / RS256 の取り違え、`kid` の差し替え、期限切れ、`nbf`、`iss` / `aud` の不一致、巨大なトークン、署名の改ざん | `tests/test_security_input.py` |
| 入力の異常 | 本文の上限、深い入れ子、不正な文字コード、制御文字、SQL のメタ文字 | `tests/test_security_input.py` |
| 秘密の混入 | トークン、秘密鍵、生の識別子がログと監査に出ないこと | `tests/test_security_input.py` |
| 依存関係 | `pip-audit`(既知の脆弱性はすべて失敗。深刻度を出力しないため) | CI の `security` |
| 静的解析 | `bandit`(深刻度 High 以上で失敗。誤検知は `# nosec <ID>` と理由で除外) | CI の `security` |

Product の名前と説明、ポートの名前は公開する設計(Phase 11)なので、ABAC の漏洩の検査の対象外です。ポートの裏の資産、アドレス、Contract のスキーマは検査します。

## 監査チェーンのアンカリング(§13 / §19 Integrity)

ハッシュチェーンだけでは、DB を書き換えられる者による末尾の削除や全行の計算し直しを検知できません。定期ジョブ(`audit_anchor`)がチェーンの先頭(seq、hash、時刻)に Ed25519 で署名し、DB の外に書き出します。

| 書き出し先 | 設定 | 内容 |
|---|---|---|
| ファイル | `EDGF_ANCHOR_FILE` | 追記のみの JSON Lines。**削除の防止はストレージの設定(WORM)に依存します** |
| Outbox | `EDGF_ANCHOR_OUTBOX`(既定 true) | `audit.anchor` イベント。受信側が保管すれば別の場所に控えが残ります |
| RFC 3161 | `EDGF_ANCHOR_TSA_URL`(任意) | TSA のトークンを記録します。失敗してもアンカーは残し、結果に記録します |

有効にするには `EDGF_ANCHOR_ENABLED=true` と `EDGF_ANCHOR_SIGNING_KEY`(32 バイトの base64)を設定します。production で有効なのに鍵が無ければ起動を拒否します。鍵の更新後は古い公開鍵を `EDGF_ANCHOR_VERIFY_KEYS` に残します。

`GET /audit/verify` はチェーンの検証に加えて、DB とファイルの**すべての**アンカーを照合します。最新のものだけでは、書き換えのあとに作られたアンカーが偽の先頭に署名するので足りません。

| `anchor.status` | 意味 | `valid` |
|---|---|---|
| `ok` | すべて一致 | チェーンが正しければ true |
| `none` | アンカーがまだ無い | チェーンが正しければ true |
| `unverifiable` | 公開鍵が設定されていない | チェーンが正しければ true |
| `tail_truncated` | チェーンの先頭の seq がアンカーより小さい(末尾の削除、古いバックアップからの復元) | false |
| `mismatch` | アンカーの seq のハッシュが違う(チェーンの計算し直しを含む、アンカー以前の改ざん) | false |
| `signature_invalid` | DB のアンカーの署名が合わない | false |
| `file_tampered` | ファイルが読めない、署名が合わない、DB の記録にある行が無い | false |

`valid` は認証済みの全員が見られます(従来どおり)。アンカーの詳細と `GET /audit/anchors` は `audit.read`(admin、committee)、手動の実行は `jobs.run`(admin)です。

外部の監査人は、公開鍵だけでファイルを検証できます。

```sh
python scripts/verify_anchors.py /worm/anchors.jsonl --public-key <base64>
```

**検知できないもの**: 最新のアンカーより後の行の改ざんは、次のアンカーまでチェーンの検証の範囲です(計算し直されると見つかりません)。署名鍵が漏れた場合と、ファイルと DB を同時に消された場合(WORM の設定が無い場合)も検知できません。RFC 3161 のトークンの証明書チェーンは、保管後に `openssl ts -verify` で検証します。

## 運用手順書

バックアップと復元、鍵の保管と復旧、ジョブの再開、デッドレターの処理、dev モードが無効であることの確認は [docs/operations/runbook.md](docs/operations/runbook.md) にあります。CI の `restore` ジョブが pg_dump から別の DB への復元、`/audit/verify` の valid、アンカーの一致を毎回確認します。

設計書の章と実装、テストの対応は [docs/traceability.md](docs/traceability.md) にあります。

## API の共通仕様

- **エラー**: すべて RFC 9457 Problem Details(`application/problem+json`)で返します。
  `{"type": "urn:edgf:problem:<slug>", "title", "status", "detail", "instance", "request_id", ...}`。
  クライアントは `type` で分岐してください(例: `not-found`, `conflict`, `unauthenticated`, `forbidden`(拡張 `action`, `required`), `auth-unavailable`, `validation-error`(拡張 `errors`), `certification-blocked`(拡張 `blockers`), `integrity-conflict`, `not-ready`(拡張 `checks`), `invalid-value`, `database-unavailable`, `audit-chain-contention`, `mdm-asset-missing`, `training-prerequisites-failed`(拡張 `blockers`), `contract-version-bump-required`(拡張 `required_bump`, `actual_bump`, `changes`), `contract-not-conforming`(拡張 `violations`), `contract-retire-blocked`(拡張 `blockers`), `product-publish-blocked`(拡張 `blockers`), `model-approval-blocked`(拡張 `blockers`), `audit-payload-rejected`(拡張 `field`, `kind`), `internal-error`)。500 の応答には内部情報を含めず、`request_id` でログと突き合わせます。
- **ページネーション**: 一覧 API はすべて `limit`(1〜1000、既定 100。品質実行履歴のみ既定 20)と `offset` を受け付けます。後方互換のため本文は配列のままで、総件数は `X-Total-Count`、前後ページは `Link`(`rel="next"`/`"prev"`)ヘッダで返します。
- **request_id**: `X-Request-ID` を受け取れば引き継ぎ(英数字と `._:-`、128 文字以内)、なければ採番してレスポンスヘッダとすべてのログ行に付けます。監査イベントにも記録し、ハッシュの対象に含めます(`GET /audit/events?request_id=...` で突き合わせできます)。
- **入力の上限**: 本文は `EDGF_MAX_BODY_BYTES`(既定 10 MiB)まで(超えると 413 `payload-too-large`)。JSON の入れ子は 64 段まで(超えると 422 `json-too-deep`)。
- **ヘルスチェック**: `/health` = liveness(プロセスの応答のみ)、`/ready` = readiness(DB 疎通、スキーマが head か、ポリシー構文が有効か)。検索の索引が無い場合は `checks.search_index: degraded` を警告として示します(503 にはしません)。準備ができていない場合は 503 の Problem で返し、`checks` に原因を含めます。

## アーキテクチャ上の判断

- **Control Plane / Data Plane の分離**: 品質検査ではData Planeからレコードを受け取って評価し、レコード自体は保存しません。物理削除もData Planeが行い、Control Planeは削除指示と証跡だけを記録します。
- **Policy as Code**: Policy の正は DB に記録した承認済みの Bundle で、`policies/` のファイルは提案と初回の取り込みの元です。`policies/global.yaml` は Committee が管理します。`policies/domains/*.yaml` はポリシーを追加することだけができ、同じIDで上書きしようとするとロード時にエラーになります(連邦型ガバナンスで緩和を禁止するため)。評価器は dict を返す単純な作りなので、OPA などに置き換えられます。
- **証跡**: SHA-256のハッシュチェーンにし、`prev_hash` にUNIQUE制約を付けて、同時書き込みでチェーンが分岐しないようにしています。衝突した書き込みはセーブポイントまで戻して先頭を読み直し、同じリクエストの他の変更を失わずに再試行します。書き込みはチェーン上で直列化されるので、監査対象の更新の同時実行数は DB のトランザクション時間で律速されます。分離レベルは READ COMMITTED が前提です。
- **ポータビリティ**: SQLAlchemy 2.0 を使い、SQLite と PostgreSQL のどちらでも動きます。メタデータの交換には OpenLineage 互換の形式を使います。マイグレーションは方言に依存しない DDL だけで書き、制約名は命名規約で固定しています。
- **Observability(§19)**: ログは標準ライブラリだけで JSON 化しています(ログ基盤製品に依存しないため)。トレーシングは OpenTelemetry(ベンダー中立の OSS 標準)を任意で有効化でき、未導入でも動作します。

### 追加した依存と理由

| パッケージ | 区分 | 理由 |
|---|---|---|
| `alembic` | 必須 | SQLAlchemy 公式のマイグレーションツール(MIT)。§19 Integrity/Maintainability のスキーマ版管理のため |
| `opentelemetry-sdk` / `-exporter-otlp-proto-http` / `-instrumentation-fastapi` | 任意(`otel`) | ベンダー中立のトレーシング標準(Apache-2.0)。§19 Observability。送信先は OTLP なので特定の製品に依存しない |
| `PyJWT[crypto]` | 必須 | JWT の検証と JWKS の取得・キャッシュ(MIT。暗号処理は `cryptography`)。依存が少なく、JWKS のキャッシュと未知の `kid` での再取得(鍵ローテーション)を標準で備え、特定の IdP SDK を使わずに OIDC 準拠の検証ができるため |
| (追加なし) | - | Phase 9 の類似度は標準ライブラリの `difflib` で計算しています。Phase 10 と Phase 11 でも依存は追加していません(ODCS の YAML は既存の `pyyaml` で処理します) |
| `pytest-cov` | 開発 | カバレッジ計測(services/ 95% 以上を CI で検査) |
| `edgf-client`(`clients/python`) | 別のパッケージ | Data Plane の SDK。依存は `httpx` だけです(サーバの開発依存として既に使っています)。サーバのパッケージには含めません |
| `cryptography` | 必須(直接指定に変更) | `PyJWT[crypto]` の依存として導入済み。Phase 14 で監査アンカーの Ed25519 署名に直接使い、既知の脆弱性を避けるため 50 以上を指定します |
| `locust` | 任意(`perf`) | 負荷試験(MIT)。Python でシナリオを書け、結果を CSV で比較できます |
| `pg_trgm`(PostgreSQL の contrib 拡張) | 任意(PostgreSQL のみ) | 検索の部分一致の GIN 索引。主要なマネージド PostgreSQL で使えるので Cloud Agnostic を保てます。使えなければ索引なしで動きます(結果は同じで、遅くなり、`/ready` に警告を出します) |
| `pip-audit` / `bandit` | 任意(`security`) | 依存関係の脆弱性と静的解析(Apache-2.0)。CI で実行します |

## 既知の制約と次フェーズ

- dev モード(`X-Actor`)は開発・テスト専用の恒久的な機能です。本番では起動を拒否します
- 閲覧の申請は承認エンジンの `access_request` で扱います。Owner が直接付与する API も残っています。SLA の営業日は土日だけを除きます(祝日は考慮しません)
- 棚卸はキャンペーンの作成時点の一覧で行います。作成後に付与された権限は次の四半期の対象です
- リネージの `upstream`/`downstream`/`impact` はオブジェクトで返すため、ページネーションの対象外です(`depth` 上限 50 で件数を抑えています)
- リネージはデータセットとカラムの 2 段です(Phase 16)。CDE のカラムは LIN-002 でカラム単位のリネージを求めます。絞る方向(消去の対象から外すなど)は、承認された信頼できる生成元の辺に限ります。詳しい制約は「[カラム単位のリネージ](#カラム単位のリネージ9)」の既知の制約を参照してください
- Right to Erasure は pull 型の API に加えて、タスクの発行を `edgf.erasure.task.dispatched` で通知します。バックアップを含む物理削除の実施は Data Plane の責務で、Control Plane は報告された件数と証跡ハッシュを記録するだけです(内容は検証しません)
- 発行後に Legal Hold で `withheld` になったタスクは、Hold が解除されても自動では再発行しません。新しい消去要求を出してください
- `subject_ref` は消去後も Consent と消去要求の記録に残ります(元の識別子には戻せず、消去の証跡として必要なため)。HMAC 鍵のローテーションは未実装です
- k-匿名性の `k` は申告値で、データ自体は検証しません(Control Plane はデータを持たないため)
- 監査ガードはキーとパターンによる検査です。形式に特徴の無い識別子(顧客 ID など)は検出できないので、生の識別子を payload に渡さない実装とテスト(全テーブルの走査)で担保しています
- `POST /consents/check` は高頻度の呼び出しを想定し、監査ログに記録しません。Purpose の `retention_days` は `consent_expire` ジョブで執行します(同意の記録の日時から数えます)
- **AI 資産の消去の伝播は実装済みです**が、Control Plane は主体がどの資産や版に含まれるかを知りません。下流の資産と該当しうる版をすべて対象にするので、実際には主体を含まない版も superseded になります。Data Plane は `subject_ref` で照合し、含まれなければ `records_deleted=0` で完了を報告してください
- 消去のタスクは、匿名化を経た下流の AI 資産にも発行します。継承は解除されますが、`k` は申告値なので消去の対象からは外しません(含まれなければ 0 件で完了します)
- 未完了の ModelRemediation があっても、消去要求の `verify` と `close` は止めません(是正は 30 日で終わらないことがあるため)。消去証明の `model_remediation_open` と Issue で追跡します
- superseded の学習データを使うモデル版は、`accept` の判断があっても停止からは再開できません(再学習が必要です)。`retired` のモデル版には Remediation を作りません
- インシデントは Issue の起票と任意の停止までで、ModelRemediation は作りません(是正の判断は消去の伝播に限っています)
- manifest のハッシュ、件数、`k`、Unlearning の証跡はすべて Data Plane の申告で、Control Plane は検証しません。Card の自由記述に貼られた値は検出できません(項目の長さの上限と運用で抑えています。監査ログには本文を残しません)
- 継承の計算は Lineage を都度辿ります。リネージの削除 API は無いので、DB を直接修正した場合は `POST /ai/inheritance/recompute` を実行してください。Phase 10 より前の `data_class=ai` の資産も、この API で実効値を作ります(それまで ABAC は宣言値で判定します)。未登録の上流は `unregistered_upstream` で示しますが、機密度が不明なので実効値には反映できません
- Phase 10 より前の `data_class=ai` の資産は、AI-001 が承認の記録を求めるようになったので不合格になります(`ai_kind` の設定と承認が必要です)
- 検索は `pg_trgm` の索引を使い、CI のランナーでも p95 120〜250ms です(参照環境では未測定)。ほぼ全行に一致する 3 文字以上の語は索引の効果が無く、開発機で 150〜215ms と、索引の導入前(約 95ms)より遅くなります。資産を大量に書き込んだ直後は、開発機で 90〜150ms に遅くなります
- 拡張を使えない環境では索引なしで同じ結果を返し、`/ready` の `checks` に `search_index: degraded` を出します。問い合わせの形は索引ありのときと同じなので、**導入前の速さには戻りません**(開発機で 220〜260ms。導入前は 150ms。条件は書き込みの直後)
- 索引による資産の書き込みの遅れは、開発機の API の p95 で更新が約 3ms で、登録はばらつきに隠れて差が見えません。SQL の単独の更新では約 3 倍です。CI で回帰を監視します
- 影響分析(620〜1,500ms)と inbox(330〜740ms)は CI で目標を超える回があります
- `POST /ai/rag/authorize` はヒットごとに ABAC を判定し、呼び出しごとに Lineage を辿ります。50 ヒットで p95 150ms の目標を満たしています(資産 10 万件で測定。100 万件は未検証)
- **PostgreSQL 16 で検証済みです**(CI で SQLite と PostgreSQL の両方について pytest とマイグレーションの往復を毎回実行します)。残っている差異は次のとおりです
  - `ilike` の非 ASCII: PostgreSQL は `É` と `é` を同一視しますが、SQLite は ASCII だけです。検索結果が DB で変わるのはこの場合だけです
  - 照合順序: 一覧の並びは DB の照合順序に従います。ページングは一意キーで安定していますが、`postgres:16-alpine` 以外(glibc の `en_US.UTF-8` など)では大文字小文字や記号の順序が変わることがあります
  - NUL 文字: PostgreSQL は 422 (`invalid-value`) で拒否しますが、SQLite は受け付けます
  - インメモリの SQLite は 1 接続を共有するので、並行書き込みのテストは PostgreSQL でだけ実行します
- デッドロック(リクエスト内で行ロックと監査チェーンの先頭を逆順で取り合う場合)は再試行せず、片方を 503(`database-unavailable`)で失敗させます。クライアントの再試行で回復します
- 同時実行で重複を防ぐ一意制約が無い検査があります。`POST /privacy/erasure-requests` の「同じ主体の未完了の要求」は、同時に受け付けると 2 件できる場合があります(業務上は重複して処理しても結果は同じです)
- **Data Mesh の実行基盤はありません**。アドレス URI の到達性、Data Plane が実際に Contract を守っているかは検証しません。Contract と Catalog の比較、品質検査の結果との照合までです
- スキーマのずれの Issue は、ずれが解消しても自動では閉じません。担当者が閉じてください。同じ Contract で未解決の Issue があれば重複して起票しません
- 判定表のうち名前の変更の推定は、削除と追加が 1 件ずつで型が同じ場合だけです。それ以外は `renamed_from` で明示してください(明示が無くても削除と追加で破壊的と判定されるので、見逃しにはなりません)。`array<int>` のような複合型は表記が同じ場合だけ互換とみなします
- 判定は型と制約の宣言だけを見ます。値の意味の変更(単位の変更など)は検出できません
- 公開後に Output Port の資産を別のドメインへ移しても拒否しません(Catalog の更新は止めません)。MESH-001 が不合格になり、次の判定で分かります。公開済みの Product を自動で非公開にはしません。DATSIS の結果は `/kpi` と `GET /mesh/products/{id}/datsis` で確認してください
- Product の Owner が自分で利用登録すると、自分では承認できないので `pending` のままです。Owner は自分の Product の利用登録を必要としない想定です
- `POST /mesh/contracts/{id}/check` は呼び出しごとに Lineage を辿ります。p95 1 秒の目標を満たしています(資産 10 万件で測定。100 万件は未検証)
- 拒否した破壊的変更の件数は監査ログから集計するので、KPI の計算は監査ログの件数に比例します
- **MDM Hub は最小の参照実装だけです**(`examples/mdm-hub/`)。候補の絞り込み、値の履歴、同時実行の制御、利用システムへの配信(§8 Synchronization、逆流更新の変更申請を含む)は Data Plane の責務で、参照実装には含みません
- Golden Record どうしのマージ(ソースを経由しない統合)は未実装です。裁定と自動マージはソース単位です
- 裁定待ちのソースは Golden Record に紐付いていないので、その段階の消去要求では特定できません。MDM Hub 側で別途対応してください
- `split` の直後は、どのソースの識別子か判定できないので、`subject_ref` の対応を両方の Golden Record に残します(再構成で正しくなります。消去の対象から漏らさないための措置です)
- Reference Data は公開後に資産の機密度が上がっても、公開済みの値を自動では削除しません(新しい版の作成と公開は拒否します)
- **UI のアクセシビリティ**は WCAG 2.2 AA の基本(キーボード操作、ランドマーク、ラベル、フォーカスの表示、配色)に沿って作り、キーボード操作と構造を E2E で確認しています。支援技術での確認と、適合の監査は行っていません
- **IdP 側でロールやドメインが変わっても、UI に反映されるのは次のログインからです**(セッションはログイン時に検証した Principal の写しを持ち、トークンを保存しないので、更新もしません)。最長で `EDGF_SESSION_MAX_HOURS`(既定 8 時間)古いままになります。急ぎの剥奪は、IdP で無効にしたうえで、DB の `ui_sessions` からその subject の行を削除してください
- ステップアップは ID トークンの `auth_time` で判定します。`auth_time` を返さない IdP では、ログインの完了の時刻とみなすので、`max_age=0` を無視する IdP では実際に再認証したかを確かめられません
- ログアウトは `id_token_hint` を持たないので、IdP によってはログアウトの確認の画面が出ます。IdP 側のシングルサインオンのセッションが残るかは IdP の設定によります
- 既存の API に却下の無い承認の種類(MDM の生成規則、Reference Data、Contract、CDM、モデルの是正)は、UI からも却下できません(既存の操作だけを出します)
- UI から資産のメタデータ(Owner / Steward の割当を含む)は編集できません(Phase 17 の範囲外。マスタープロンプトの Phase 17 の「Owner / Steward の割当」は、確定事項に従って含めていません)
- Cookie の経路では、要求ごとに DB でセッションを確かめます(最終操作の時刻の書き込みは 1 分に 1 回)。CSRF のトークンはセッションの間は同じ値です(要求ごとには変えません)
- `/kpi/history` のドメイン別の値は日次のスナップショットで、閲覧できない資産も含めた集計です(行そのものは ABAC で絞り込みます)。閲覧できる範囲で集計したい場合は `/kpi/domains` を使ってください
- Catalog 登録率は、Data Plane が送った資産一覧の範囲で計算します。**送られていないソースの資産は分母に入りません**。全ソースから送られているかは、Data Plane の側で確かめてください
- 資産一覧の差分は、ソースごとに前回のスナップショットと比べます。差分のたびに Catalog を 1 件ずつ照合するので、1 回の送信で数万件を超える場合は分けて送ってください(ソースの単位を細かくする)
- Usage の KPI は Data Plane の集計値の申告で、Control Plane は検証しません
- `/mesh/products/overview` は 500 件で p95 2 秒の目標を満たしています(1 リクエストの間だけのスナップショットを使い、キャッシュは持ちません)。件数が増えると比例して遅くなります
- **Policy の起案者は admin、Committee、ドメイン内の Owner です**。§16 の「起案(Steward/Owner)」のうち Steward は含みません
- Policy の影響評価は全資産を評価するので、資産の数に比例して提案に時間がかかります。評価の結果は提案の時点のものです(公開までに資産が変わっても再計算しません)
- inbox は案件を集めてからページングします。1,000 件で p95 500ms の目標を満たしています(案件の数に比例して遅くなります)
- 対象資産の Owner と Custodian が同じ人だと、その資産の承認を判断できません(Custodian は承認者になれないため)。Owner か Custodian を変更してください
- ジョブのリースは更新しません。`EDGF_JOB_LEASE_SECONDS` より長く動くジョブは、別のプロセスと重なることがあります(各ジョブが冪等なので結果は壊れません)。配信ジョブは 1 回に 200 件までです
- イベントの順序は保証しません。`outbox_events` と `webhook_deliveries` の削除(保持期間)は未実装です
- 内部ネットワークへの配信の拒否は、確認と送信の間に DNS が変わる場合を防げません。本番では外向きの通信も制限してください
- 秘密鍵の参照は `env:` だけに対応しています。KMS などは `SecretResolver` の実装が必要です
