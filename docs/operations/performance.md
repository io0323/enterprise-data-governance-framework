# 性能の測定と最適化の記録

対象は Phase 14 で確定した 8 シナリオです。条件は PostgreSQL 16、資産 10 万件(restricted 5%)、ドメイン 50、Lineage 深さ 10 の分岐、Product 500、承認待ち 1,000 件です。SQLite は測定しません。

## 実行方法

```sh
export EDGF_DATABASE_URL=postgresql+psycopg://...
python scripts/perf/seed.py --reset          # 乱数のシードが固定なので、同じ引数なら同じデータになる
sh scripts/perf/serve.sh &                   # 127.0.0.1:8100、4 ワーカー
python scripts/perf/run.py --host http://127.0.0.1:8100 --output perf/results/now.json
python scripts/perf/compare.py perf/results/now.json
```

`compare.py` は `perf/baseline.json` より p95 が 30% 以上悪化する(監査の書き込みは件数が 30% 以上低下する)か、エラー率が 1% を超えると失敗します。目標の未達は表示だけで、失敗にはしません。CI の実行環境と目標の前提が同じとは限らないためです。

CI(`.github/workflows/perf.yml`)は手動と週次で動き、Pull Request ごとには動きません。100 万件のプロファイルは手動の入力 `profile=million` でだけ動きます。**100 万件は未検証です。**

## 手元での測定結果(最適化の前後)

開発機(macOS、Docker 上の PostgreSQL)で各 20 秒。同時実行数は 5(Product 一覧は 2、監査の書き込みは 16)。エラーは前後とも 0 件です。CI の基準値は実行環境が違うので、この表とは別に `perf/baseline.json` に記録しています。

| シナリオ | 目標 | 前 | 後 | 1 リクエストの SQL 数(前→後) |
|---|---|---|---|---|
| 検索 | p95 300ms | 190ms | 180ms | 変更なし |
| 資産詳細 | p95 200ms | 7ms | 7ms | 変更なし |
| 影響分析(深さ 10) | p95 1s | 3,100ms | 570ms | 3,172 → 22 |
| RAG 認可(50 ヒット) | p95 150ms | 79ms | 26ms | → 14 |
| Contract check | p95 1s | 2,200ms | 300ms | 2,327 → 24 |
| inbox(1,000 件) | p95 500ms | 2,000ms | 260ms | 2,008 → 11 |
| Product 一覧(500 件) | p95 2s | 5,200ms | 240ms | 10,045 → 54 |
| 監査の書き込み | 200 件/秒以上 | 142 件/秒 | 279 件/秒 | ― |

## 原因

SQL の遅さではなく、速い SQL を何千回も発行していたこと(N+1)が主な原因でした。

- `EXPLAIN ANALYZE` で個々の SQL は問題ありません。検索の一覧は `qualified_name` の索引で約 1ms、`lineage_edges` は `source`/`target` の索引で 200 ノード分を約 13ms です。
- 検索の残りの時間は `X-Total-Count` の `count(*)` が占めます(9 万 5 千行の Seq Scan で約 60ms)。挙動に関わるので変更せず、目標内のため索引も足していません。
- 監査の書き込みは、チェーンの先頭を同時に読んだ書き込みが UNIQUE 違反で再試行を繰り返していました。

## 判断の記録

### Lineage: `WITH RECURSIVE` にせず、段ごとの一括取得にする

`lineage.traverse` は 1 段ごとに `IN` でまとめて辺を取ります(500 件ごとに分割)。SQL の数は深さに比例します。

`WITH RECURSIVE` を採らなかった理由は次の 3 つです。

- 出力の順序(幅優先、同じ段では登録順)と循環の扱いを既存と完全に揃えるには、結局 Python 側で並べ直す必要があること
- SQLite と PostgreSQL の両方で同じ結果を保証しやすいこと
- 深さ 10 で目標を満たしており、これ以上の複雑さに見合わないこと

### `readable_clause` に索引を足さない

`sensitivity` の条件に合う行は全体の約 95% で、選択性がありません。Planner は索引を使わず、足しても書き込みの負担が増えるだけです。

名前を伏せる処理(`NameRedactor`)が名前ごとに問い合わせていたのは、`preload` で先にまとめて読むようにしました。

### inbox の集約方式

判断できる案件の絞り込み自体は SQL にしていません。「同じ人が 2 回判断できない」「Custodian は判断できない」などの規則は `approvals` の Python 側にあり、SQL に重複させると規則がずれる危険があるためです。

代わりに `decidable_pending` が承認待ちを 500 件ずつ読み、関連する行をまとめて読み込んでから既存の判定を通します。SQL の数は案件の数に比例しません。

### DATSIS: TTL キャッシュを持たない

`/mesh/products/overview` は、リクエストの間だけ有効なスナップショット(`mesh_datsis.load_snapshot`)を使います。一覧の処理が終わると破棄します。

時間で失効するキャッシュを採らなかったのは、無効化の条件が多く(品質の実行、Contract、ポート、例外、Bundle、資産の属性)、古い判定を返すと Product の公開可否という振る舞いが変わるためです。性能目標は TTL キャッシュなしで満たしています。

### 監査チェーン: PostgreSQL では advisory lock で直列化する

`audit.record` は `pg_advisory_xact_lock` でチェーンの更新をトランザクションの終わりまで直列化します。

- 再試行の連鎖がなくなり、142 件/秒が 279 件/秒になりました
- チェーンの性質上、書き込みは 1 本に並びます。**監査を記録するトランザクションの長さが、そのまま全体の書き込みの上限になります**
- UNIQUE 制約と再試行は残しています(SQLite と、ロック外の経路に対する最後の防御です)

## 未検証・制約

- 100 万件のプロファイルは用意しただけで、実行していません
- 手元の結果は 20 秒の短い測定で、長時間の劣化(VACUUM の影響、テーブルの肥大)は見ていません
- 接続プールの枯渇、複数ノードでの動作は対象外です
