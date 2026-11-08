# Enterprise Data Governance Framework — Governance Control Plane (MVP)

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

## 起動

```bash
# ローカル (SQLite)
pip install -e ".[dev]"
uvicorn edgf.main:app --reload        # http://localhost:8000/docs
pytest -q

# PostgreSQL (docker compose)
docker compose up --build
./scripts/demo.sh                     # サンプルデータの投入
```

環境変数: `EDGF_DATABASE_URL` / `EDGF_POLICY_DIR` / `EDGF_CERT_MIN_QUALITY`(既定 0.99)/ `EDGF_ISSUE_SLA_DAYS`(既定 30)

## アーキテクチャ上の判断

- **Control Plane / Data Plane の分離**: 品質検査ではData Planeからレコードを受け取って評価し、レコード自体は保存しません。物理削除もData Planeが行い、Control Planeは削除指示と証跡だけを記録します。
- **Policy as Code**: `policies/global.yaml` は Committee が管理します。`policies/domains/*.yaml` はポリシーを追加することだけができ、同じIDで上書きしようとするとロード時にエラーになります(連邦型ガバナンスで緩和を禁止するため)。評価器は dict を返す単純な作りなので、OPA などに置き換えられます。
- **証跡**: SHA-256のハッシュチェーンにし、`prev_hash` にUNIQUE制約を付けて、同時書き込みでチェーンが分岐しないようにしています。
- **ポータビリティ**: SQLAlchemy 2.0 を使い、SQLite と PostgreSQL のどちらでも動きます。メタデータの交換には OpenLineage 互換の形式を使います。

## 既知の制約と次フェーズ

- 認証は `X-Actor` ヘッダで代用しています → OIDC/IdP 連携と RBAC/ABAC を入れる(§11)
- スキーマはまだ `create_all` で作っています → Alembic のマイグレーションに移行する
- リネージはデータセット単位です → CDE はカラム単位にする(§9)
- 未着手: Right to Erasure のワークフロー(§12)、Golden Record/MDM(§8)、AI資産(Model/Dataset Card、§14)、Catalog UI、スケジューラとの連携(`/issues/escalate-overdue` と `/policies/evaluate` の定期実行)
