# 設計書と実装の対応表

設計書 `docs/design/enterprise-data-governance-framework.md` の 22 章と、実装、テスト、状態の対応です。Phase 14 の完了時点の内容です。

状態の意味は次のとおりです。

- **実装済み**: 設計書の内容を Control Plane の機能として実装し、テストで確認している
- **一部**: 主要な機能は実装したが、残りがある(「残り」の列を参照)
- **文書**: 設計の方針や運用の指針で、コードの対象ではない
- **対象外**: Control Plane の責務の外(Data Plane や組織の運用)

## 章ごとの対応

| 章 | 主な実装 | テスト | 状態 | 残り |
|---|---|---|---|---|
| §1 要件定義 | KPI(§1.8 の目標)、`edgf/services/kpi.py`、Catalog 登録率(`edgf/services/discovery.py`) | `test_lineage_policy.py`、`test_ui_api.py`、`test_discovery.py` | 実装済み | Catalog 登録率は Data Plane が送った資産一覧の範囲で計算する |
| §2 アーキテクチャ | Governance Control Plane と Data Plane の分離、`edgf/api`、`edgf/services` | 全テスト | 実装済み | |
| §3 Data Classification | `edgf/enums.py`、`edgf/api/catalog.py`、PII の推定 | `test_catalog.py`、`test_privacy_inventory.py` | 実装済み | |
| §4 Data Ownership | `edgf/authz.py`(権限表)、Owner / Steward / Custodian の割当、Unknown Owner の検出。Phase 17: UI のログインの BFF(`edgf/ui/bff.py`、機密クライアント)とセッション(`edgf/services/ui_sessions.py`)、Cookie の経路の CSRF の確認、承認行為(✓)のステップアップ。UI の操作も API と同じ権限表と職務分離で判定する | `test_authz.py`、`test_auth.py`、`test_security_authz.py`、`test_ui_bff.py`、`test_ui_csrf.py` | 実装済み | IdP 側のロールの変更は次のログインから反映 |
| §5 Data Quality | `edgf/services/quality.py`、`edgf/api/quality.py`。Phase 19: 実行の履歴からの異常の検知と劣化の予測(`quality_anomaly.py`) | `test_quality.py`、`test_quality_anomaly.py` | 実装済み | 検査の実行はレコードを受け取る方式。Data Plane での常時監視は対象外。異常の検知は季節性を扱わない |
| §6 Metadata | Catalog、Glossary、`edgf/api/glossary.py` | `test_catalog.py` | 実装済み | 自動収集は Data Plane の連携先が行う |
| §7 Data Catalog | 検索、Certification ゲート、UI(`edgf/ui`)、Discovery(資産一覧の差分、未登録の検出、スキーマの反映) | `test_catalog.py`、`test_ui_api.py`、`tests/e2e`、`test_discovery.py`、`tests/data_plane_e2e` | 実装済み | 自動スキャンは Data Plane が行い、Control Plane は差分と照合を担う |
| §8 MDM | `edgf/services/mdm.py`、`mdm_rules.py`、`refdata.py` | `test_mdm_*.py`、`test_refdata.py` | 一部 | MDM Hub の参照実装は無い(値は Data Plane) |
| §9 Data Lineage | `edgf/services/lineage.py`、影響分析、OpenLineage 互換の取込。Phase 16: カラム単位のリネージ(`column_lineage.py`、columnLineage facet 1-x、探索と影響分析、Contract check の `column_impact`)、信頼できる生成元による消去・AI の版・PII の継承の絞り込み(`lineage_trust.py`)、CDE のカラム化(`cde`、LIN-002、PRV-006、KPI `cde_column_lineage_rate`) | `test_lineage_policy.py`、`test_perf_queries.py`、`test_column_lineage.py`、`test_column_lineage_traversal.py`、`test_column_lineage_trust.py`、`test_cde_columns.py` | 実装済み | 自動収集は連携先が行う。信頼できる生成元の facet が完全かは検証しない(宣言の承認で担保) |
| §10 Data Lifecycle | `edgf/services/lifecycle.py`、`retention.py`、Legal Hold | `test_catalog.py`、`test_jobs.py`、`test_reviews.py` | 実装済み | 物理削除は Data Plane |
| §11 Data Security | ABAC(`readable_clause`)、閲覧の承認、定期棚卸 | `test_authz.py`、`test_security_leak.py`、`test_reviews.py` | 実装済み | 暗号化とマスキングの実行は Data Plane |
| §12 Data Privacy | `subject_ref`、同意、Right to Erasure、PII インベントリ | `test_privacy_*.py` | 実装済み | `EDGF_SUBJECT_KEY` のローテーションは未実装 |
| §13 Data Compliance | Policy as Code、ハッシュチェーン監査、アンカリング | `test_lineage_policy.py`、`test_policy_bundles.py`、`test_audit_anchor.py` | 実装済み | |
| §14 Data Governance & AI | `edgf/services/ai.py`、`ai_*.py`、Card、Lineage、RAG の判定、ModelRemediation | `test_ai_*.py` | 実装済み | 学習とモデルの本体、Unlearning の実行は Data Plane |
| §15 Data Mesh | `edgf/services/mesh.py`、`mesh_*.py`、Contract、DATSIS、利用登録、ODCS、CDM の取込 | `test_mesh_*.py` | 一部 | Data Mesh の実行基盤は無い。CDM の詳細定義は別文書(§1.10) |
| §16 Governance Process | 承認エンジン、Policy のライフサイクル、CloudEvents / Webhook、定期レビュー、ジョブ。Phase 17: 承認箱の全種類(新しいエンジンと既存の 8 種類)のアダプタに判断の API と入力の項目を追加し、UI から判断・公開できる。棚卸と証明、Issue の更新も UI から行える(監査に `channel: "ui"`) | `test_approvals.py`、`test_policy_bundles.py`、`test_outbox.py`、`test_reviews.py`、`test_jobs.py`、`test_ui_inbox.py`、`test_ui_reviews_issues.py`、`tests/e2e/test_ui_actions.py` | 実装済み | 会議体の運営は組織の運用。既存の API に却下の無い種類は UI でも却下できない |
| §17 KPI / Metrics | `edgf/services/kpi.py`、`kpi_snapshots`、ダッシュボード、利用実績(`edgf/services/usage.py`) | `test_ai_kpi.py`、`test_mesh_kpi.py`、`test_ui_api.py`、`test_usage.py` | 実装済み | Usage は Data Plane の集計値の申告で、Control Plane は検証しない。利用者の数だけで、Certified データの利用者の内訳は取らない |
| §18 Reference Architecture | 製品に依存しない構成、PostgreSQL / SQLite、OIDC、OTLP | `test_db_compat.py`、`tests/e2e_keycloak` | 一部 | 参照アーキテクチャのうち Data Plane 側の製品の組み合わせは対象外 |
| §19 非機能要件 | 下の表を参照 | `test_perf_*.py`、`test_security_*.py`、`test_audit_anchor.py`、`scripts/ops/restore_drill.py` | 一部 | 下の表を参照 |
| §20 Maturity Model | KPI による目安(Level 3 は Catalog 登録率)。Phase 18: ドメイン別の成熟度の評価(基準の宣言 `policies/maturity/criteria.yaml`、KPI のスナップショットによる自動の判定、自己評価、承認エンジンでの Committee の判定、`min(目安, 前回 + 1)` の上限で飛び級を防ぐ、全社の分布と最小値、参照ドメイン、年次の起票のジョブ)。Phase 19: 自律統治の基盤([ADR 0001](adr/0001-autonomous-remediation-scope.md)。自動化の主体 `system:autonomy`、プレイブックと操作のカタログ、dry_run、取り消し、停止スイッチと自動停止、品質の異常の検知と劣化の予測、メタデータからの分類の提案、統治の自己改善、Level 5 の KPI と基準) | `test_ui_api.py`、`test_discovery.py`、`test_maturity_criteria.py`、`test_maturity_evaluate.py`、`test_maturity_decide.py`、`test_maturity_enterprise.py`、`tests/e2e/test_ui_maturity.py`、`test_autonomy_catalog.py`、`test_autonomy_run.py`、`test_quality_anomaly.py`、`test_classification.py`、`test_governance_review.py`、`tests/e2e/test_ui_autonomy.py` | 一部 | 下の「成熟度」を参照(Level 5 は機能の基盤まで。到達は組織の運用と 90 日の継続の実績による) |
| §21 ベストプラクティス | §4、§5、§6、§7、§16 の実装で支える | 各章のテスト | 文書 | 年次サイクルの運営は組織の運用 |
| §22 アンチパターン | Unknown Owner の検出、Certification、Lineage の必須化などで対策 | 各章のテスト | 文書 | |

## §19 非機能要件

| 要件 | 設計基準 | 実装と確認の方法 | 状態 |
|---|---|---|---|
| Availability | 99.9%、認可は fail-close | fail-close はテストで確認。手順書と復元の訓練を CI で実行。**99.9% の実測はしていない**(運用環境が必要) | 一部 |
| Integrity | 追記型・署名 | ハッシュチェーン + Ed25519 のアンカー。末尾の削除と計算し直しを検知。Policy は版管理 | 実装済み |
| Security | Restricted 相当で保護、管理者操作の全記録 | 認可の総当たり、ABAC の漏洩、JWT への攻撃、入力の異常、pip-audit、bandit を CI で実行 | 実装済み |
| Scalability | 資産数百万件、毎秒万件級 | **資産 10 万件を開発機と CI で実測。参照環境は未測定で、目標の判定は保留。100 万件は未検証。毎秒万件級の収集と水平拡張は未検証**(監査チェーンの書き込みは直列なので、上限は別途の設計が必要) | 一部 |
| Auditability | 5W1H の再構成 | `request_id`、監査の payload のガード、`/audit/events` の絞り込み | 実装済み |
| Maintainability | Policy のテスト、段階リリース | Bundle の影響評価と承認、Alembic の往復。役割定義の年次更新は組織の運用 | 実装済み |
| Observability | SLI/SLO の監視とアラート | 構造化ログ、OTLP、`/ready`、`/ops/jobs`。**SLO の定義とアラートの設定は未実装**(運用環境の監視基盤が必要) | 一部 |

## 非対象範囲(§1.10)

次のものは設計書で非対象とされており、**実装していません。** 将来も Control Plane の責務に入れません。

- 非構造ナレッジ(文書、ノウハウ)の管理(Knowledge Management)
- アプリケーション機能の設計
- 個別の ETL の実装
- CDM のエンティティの詳細定義(外部の定義の版の取り込みだけを行い、作成と編集の機能は持たない)

§18.2 の Control Plane と Data Plane の分離により、次のものも保持しません。

- データ本体、個人データの生の識別子(`subject_ref` だけを持つ)
- MDM の値、学習データ、Embedding、モデルの重み

## 成熟度(§20)

設計書は 1 レベルずつ、ドメイン別に年次で評価することを求めています。成熟度は組織の状態なので、ソフトウェアの実装だけでは到達できません。ここでは、**機能が支えられる範囲**を整理します。

| Level | 到達条件 | 機能の対応 | 組織の側に残ること |
|---|---|---|---|
| 1 Basic Policy | Policy 承認、Owner 任命 | Policy Bundle の承認、Owner の割当 | Committee の設置 |
| 2 Managed | CDE 定義、Quality KPI | CDE、品質検査、KPI | 重要ドメインでの運用 |
| 3 Standardized | Catalog 登録率 95%、自動監査 | 自動監査のジョブ、全社標準の Policy、Catalog 登録率(Data Plane の資産一覧から) | 全ドメインへの適用、全ソースからの資産一覧の送信 |
| 4 Federated Governance | 連邦体制・Certification 流通の定着 | ドメイン別の成熟度の評価(Phase 18): Certified 率と遵守率の 90 日継続、手動の項目の自己評価と Committee の判断、飛び級の防止、参照ドメイン | ドメインの統治の体制と運用の実績、年次の評価の運用 |
| 5 Autonomous Governance | 品質検知・分類・是正の大部分の自動化、統治の自己改善 | 自律統治の基盤(Phase 19): ADR による範囲の承認、区分 A の自動の是正と取り消し、区分 B の提案、品質の異常の検知と予測、分類の提案、Policy の見直しの起票、Level 5 の KPI と基準 | ADR の承認、dry_run から active への切り替えの判断、自動是正率と受け入れ率の 90 日の継続、範囲の見直しの運用 |

### Level 4 Federated Governance に残ること

機能はあります(Domain Policy は Global を緩められない、Data Product、Certification)。定着の判定に次が必要です。

- ドメインごとの運用の実績(連邦体制の定着は期間を要する)。Phase 18 の評価では、Certified 率と遵守率を 90 日継続して満たすことを自動の指標にし、体制と Data Product の運用の定着は手動の項目(自己評価と Committee の判断)で確かめる
- Data Plane との連携のうち、Contract の実行時の強制(到達の報告と MESH-007 は Phase 15 で実装)
- ~~ドメイン別の成熟度評価の機能~~ → Phase 18 で実装(評価の記録、1 レベルずつの引き上げ、参照ドメイン)。評価を年次で回し、記録を積み上げるのは組織の運用

### Level 5 Autonomous Governance に残ること

**機能の基盤は Phase 19 で実装しました。** 自動で是正してよい範囲は [ADR 0001](adr/0001-autonomous-remediation-scope.md) にまとめ、プレイブックを含む Policy Bundle の承認をもって Committee の承認とします。承認の前(active の Bundle にプレイブックが無い間)は、自動の是正は動きません。

| 設計書の要素 | Phase 19 の対応 | 残ること |
|---|---|---|
| 是正の自動化と、その率の計測 | 区分 A の操作(Issue の起票、Certification の取り消し、解消した品質の Issue のクローズ、期限切れなどの閲覧の承認の取り消し)の自動の実行と取り消し。`autonomy_remediation_rate`、`autonomy_false_positive_rate` | 区分 B を区分 A に移す判断(ADR の改訂)。dry_run から active への切り替えの判断と、率の 90 日の継続 |
| 予測的な品質管理 | 実行の履歴からの外れ値・劣化の予測(Theil–Sen)・件数の急減・更新の遅れの検知。`anomaly_detected_count` | 季節性や外部の要因を扱う手法(履歴と Data Plane の情報が必要) |
| 分類の自動化 | メタデータ(辞書規則、Lineage の DIRECT の経路、Glossary)からの提案と、Owner / Steward による確定。`classification_suggestion_acceptance_rate` | 辞書規則の整備。データの値に基づく検出は Data Plane の責務(Control Plane は値を持たない) |
| 統治の自己改善 | 常に合格・一度も適用されない・例外が多い・不合格が続く Policy の見直しの Issue の起票 | 見直しの判断と Policy の変更(Committee) |

§4 Business Ownership との整合: 自動化の主体は承認(✓)の権限を一切持たず(ロード時の検証と承認エンジンの拒否)、業務の判断を含む変更は提案までで止めます。自動の操作は監査に残り、Owner と Committee が取り消せます。
