# Enterprise Data Governance Framework — Governance Control Plane

「Enterprise Data Governance Framework 設計書」の実装です。製品に依存しない(Cloud Agnostic)**Governance Control Plane** として、
メタデータ・統制の状態・証跡だけを持ち、**データ本体は持ちません**(設計書 §18.2)。

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
| §9 | OpenLineage互換の取込・上流/下流トレース・影響分析(通知先Owner・リスク判定) | `/lineage/...` |
| §13 / §15 | Policy as Code(Global/Domain YAML、上書き禁止)・自動監査・期限付き例外の承認(職務分離あり) | `/policies/...` |
| §13 / §19 | ハッシュチェーン監査ログ・改ざんの検知 | `/audit/events`, `/audit/verify` |
| §17 / §20 | KPI(全社・ドメイン別)・成熟度の目安 | `/kpi`, `/kpi/domains` |
| §19 | スキーマ版管理(Alembic)・統一エラーモデル(RFC 9457)・ページネーション・構造化ログ/request_id・liveness/readiness | `/health`, `/ready` |
| §4 / §11 | 認証(OIDC JWT / 開発用 dev モード)・RBAC・資産ごとの Owner/Steward 判定・ABAC(機密度×clearance)・閲覧承認・拒否の証跡 | 全 API、`/assets/{id}/access-grants` |

## 起動

```bash
# ローカル (SQLite, dev モード)
pip install -e ".[dev]"
alembic upgrade head                  # スキーマ作成/更新 (EDGF_DATABASE_URL の DB に対して)
EDGF_AUTH_MODE=dev uvicorn edgf.main:app --reload --no-access-log   # http://localhost:8000/docs
./scripts/demo.sh                     # サンプルデータの投入 (dev モード前提)
pytest -q --cov=edgf

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
| `jwt`(既定) | `Authorization: Bearer <JWT>`。署名・`iss`・`aud`・`exp`・`nbf` を検証します。トークンが無い・不正なら 401 で、`X-Actor` は無視します | 本番・検証環境 |
| `dev` | `X-Actor`(必須)、`X-Roles` / `X-Domains` / `X-Clearance`(任意)ヘッダ | ローカル開発・移行期間 |

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

- `EDGF_ENV=production` と同時に指定すると、起動時に例外を出して停止します。
- 起動時に WARNING ログを出し、`/health` の応答に `"auth_mode": "dev"` を含めます(外形監視で検知できます)。
- 監査ログの actor は `dev:<名前>` で記録し、JWT 由来の操作と区別します。
- Dockerfile と `docker-compose.yml` の既定は jwt です。dev モードは `docker-compose.dev.yml`(override)でだけ有効になります。

```bash
curl -H 'X-Actor: steward1' -H 'X-Roles: steward' -H 'X-Domains: sales' http://localhost:8000/api/v1/assets
```

## 認可(§4 / §11)

認可の判定は [edgf/authz.py](edgf/authz.py) に集約し、権限表 `PERMISSIONS` で宣言的に定義しています。各操作は、次のいずれかを満たせば許可されます。

- **ロール**(RBAC): `admin` / `committee` / `owner` / `steward` / `custodian` / `consumer`(認証済みの全員に `consumer` が付きます)
- **ドメイン内ロール**: 対象資産のドメインが Principal の `domains` に含まれる場合だけ有効なロール(`*` は全ドメイン)
- **対象との関係**: 資産の `owner` / `steward` / `custodian` が JWT の subject と一致すること(Issue は `assignee`)

承認行為(✓)に Custodian を含めることはできず、そのような定義はロード時にエラーになります(§4 Business Ownership)。権限が無い操作は 403 になり、`authz.denied` として監査ログに記録します(リクエスト自体は巻き戻し、拒否の証跡だけを確定させます)。

| アクション | 内容 | 許可される主体 | 承認 |
|---|---|---|---|
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
| `domain.create` | ドメインの新設 | ロール: admin, committee |  |
| `exception.decide` | ポリシー例外の承認/却下 | ロール: admin, committee | ✓ |
| `exception.request` | ポリシー例外の申請 | ロール: admin<br>ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `glossary.link` | 用語と資産の紐付け | ロール: admin<br>ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `glossary.write` | 用語の作成 | ロール: admin, owner, steward |  |
| `issue.create` | Issue の起票 (Consumer の品質報告を含む) | 認証済みの全員 |  |
| `issue.escalate` | Issue のエスカレーション | ロール: admin, committee<br>対象の assignee / owner / steward |  |
| `issue.escalate_overdue` | SLA 超過 Issue の一括エスカレーション | ロール: admin, committee |  |
| `issue.update` | Issue の更新 | ロール: admin, committee<br>対象の assignee / owner / steward |  |
| `lineage.write` | リネージの登録・取込 | ロール: admin, custodian, owner, steward |  |
| `policy.evaluate_all` | 全資産の自動監査と Issue 化 | ロール: admin, committee |  |
| `policy.reload` | ポリシーの再読込 (公開) | ロール: admin, committee | ✓ |
| `quality.rule_write` | 品質ルールの追加・削除 | ドメイン内ロール: owner, steward<br>対象の owner / steward |  |
| `quality.run` | 品質検査の実行 (パイプラインからの送信を含む) | ドメイン内ロール: custodian, owner, steward<br>対象の custodian / owner / steward |  |

これ以外の参照系 API(資産・Issue・KPI・ポリシー・リネージなど)は、認証済みの全員が使えます。資産を含む結果は、次の ABAC で絞り込みます。

### ABAC(資産の閲覧)

| 資産の機密度 | 閲覧できる主体 |
|---|---|
| public / internal / confidential | clearance がその機密度以上の利用者、資産の当事者(Owner/Steward/Custodian)、`committee`、`admin` |
| restricted | 資産の当事者、`committee`、`admin`、**Owner が閲覧を承認した利用者**(clearance だけでは閲覧不可) |

- clearance は claim(`EDGF_CLAIM_CLEARANCE`)で与えます。無い場合はロールから導きます(admin/committee=restricted、owner/steward/custodian=confidential、consumer=internal)。
- 閲覧できない資産は、一覧(資産・保持期限・Issue・ポリシー例外・用語の紐付け先)から件数を含めて除外し、個別に取得しようとすると 403 になります。影響分析では名前と深さだけを返し、詳細を伏せます(通知先 Owner は変更管理に必要なので返します)。
- Owner による閲覧承認: `POST /assets/{id}/access-grants`(`subject`, `purpose`, `expires_at`。期限は最大 365 日)。取り消しは `DELETE /assets/{id}/access-grants/{grant_id}` で、Owner と Committee が行えます。

## API の共通仕様

- **エラー**: すべて RFC 9457 Problem Details(`application/problem+json`)で返します。
  `{"type": "urn:edgf:problem:<slug>", "title", "status", "detail", "instance", "request_id", ...}`。
  クライアントは `type` で分岐してください(例: `not-found`, `conflict`, `unauthenticated`, `forbidden`(拡張 `action`, `required`), `auth-unavailable`, `validation-error`(拡張 `errors`), `certification-blocked`(拡張 `blockers`), `integrity-conflict`, `not-ready`(拡張 `checks`), `internal-error`)。500 の応答には内部情報を含めず、`request_id` でログと突き合わせます。
- **ページネーション**: 一覧 API はすべて `limit`(1〜1000、既定 100。品質実行履歴のみ既定 20)と `offset` を受け付けます。後方互換のため本文は配列のままで、総件数は `X-Total-Count`、前後ページは `Link`(`rel="next"`/`"prev"`)ヘッダで返します。
- **request_id**: `X-Request-ID` を受け取れば引き継ぎ(英数字と `._:-`、128 文字以内)、なければ採番してレスポンスヘッダとすべてのログ行に付けます。監査イベントにも記録し、ハッシュの対象に含めます(`GET /audit/events?request_id=...` で突き合わせできます)。
- **ヘルスチェック**: `/health` = liveness(プロセスの応答のみ)、`/ready` = readiness(DB 疎通、スキーマが head か、ポリシー構文が有効か)。準備ができていない場合は 503 の Problem で返し、`checks` に原因を含めます。

## アーキテクチャ上の判断

- **Control Plane / Data Plane の分離**: 品質検査ではData Planeからレコードを受け取って評価し、レコード自体は保存しません。物理削除もData Planeが行い、Control Planeは削除指示と証跡だけを記録します。
- **Policy as Code**: `policies/global.yaml` は Committee が管理します。`policies/domains/*.yaml` はポリシーを追加することだけができ、同じIDで上書きしようとするとロード時にエラーになります(連邦型ガバナンスで緩和を禁止するため)。評価器は dict を返す単純な作りなので、OPA などに置き換えられます。
- **証跡**: SHA-256のハッシュチェーンにし、`prev_hash` にUNIQUE制約を付けて、同時書き込みでチェーンが分岐しないようにしています。
- **ポータビリティ**: SQLAlchemy 2.0 を使い、SQLite と PostgreSQL のどちらでも動きます。メタデータの交換には OpenLineage 互換の形式を使います。マイグレーションは方言に依存しない DDL だけで書き、制約名は命名規約で固定しています。
- **Observability(§19)**: ログは標準ライブラリだけで JSON 化しています(ログ基盤製品に依存しないため)。トレーシングは OpenTelemetry(ベンダー中立の OSS 標準)を任意で有効化でき、未導入でも動作します。

### 追加した依存と理由

| パッケージ | 区分 | 理由 |
|---|---|---|
| `alembic` | 必須 | SQLAlchemy 公式のマイグレーションツール(MIT)。§19 Integrity/Maintainability のスキーマ版管理のため |
| `opentelemetry-sdk` / `-exporter-otlp-proto-http` / `-instrumentation-fastapi` | 任意(`otel`) | ベンダー中立のトレーシング標準(Apache-2.0)。§19 Observability。送信先は OTLP なので特定の製品に依存しない |
| `PyJWT[crypto]` | 必須 | JWT の検証と JWKS の取得・キャッシュ(MIT。暗号処理は `cryptography`)。依存が少なく、JWKS のキャッシュと未知の `kid` での再取得(鍵ローテーション)を標準で備え、特定の IdP SDK を使わずに OIDC 準拠の検証ができるため |
| `pytest-cov` | 開発 | カバレッジ計測(services/ 85% 以上を目標) |

## 既知の制約と次フェーズ

- dev モード(`X-Actor`)は移行期間の措置です。移行が完了したら廃止します
- 閲覧承認(AccessGrant)は単独の承認です。申請→承認のワークフローと承認 SLA は Phase 12 の汎用承認エンジンで扱います
- 権限の定期棚卸(§11 四半期)はまだ実装していません。監査ログ(`access.grant` / `authz.denied`)から集計できます
- リネージの `upstream`/`downstream`/`impact` はオブジェクトで返すため、ページネーションの対象外です(`depth` 上限 50 で件数を抑えています)
- リネージはデータセット単位です → CDE はカラム単位にする(§9)
- 未着手: Right to Erasure のワークフロー(§12)、Golden Record/MDM(§8)、AI資産(Model/Dataset Card、§14)、Catalog UI、スケジューラとの連携(`/issues/escalate-overdue` と `/policies/evaluate` の定期実行)
