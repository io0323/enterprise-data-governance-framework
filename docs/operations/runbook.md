# 運用手順書

対象は Governance Control Plane の運用担当者です。各手順は、バックアップと復元を除き、実行そのものを監査ログに残します。

## 1. 起動前の確認

本番(`EDGF_ENV=production`)では、次の不備があると起動を拒否します。

| 条件 | 必要な設定 |
|---|---|
| dev モードの禁止 | `EDGF_AUTH_MODE=jwt`(`dev` は起動を拒否) |
| OIDC | `EDGF_OIDC_ISSUER`、`EDGF_OIDC_AUDIENCE`、`EDGF_OIDC_JWKS_URL`(https) |
| subject_ref の鍵 | `EDGF_SUBJECT_KEY`(32 バイト以上) |
| アンカリング | `EDGF_ANCHOR_ENABLED=true` なら `EDGF_ANCHOR_SIGNING_KEY` |

### dev モードが無効であることの確認

```sh
curl -s https://<host>/health
# {"status":"ok","version":"...","auth_mode":"jwt"} の auth_mode が jwt であること
```

外形監視で `auth_mode` が `dev` になったら警報を出してください。次の手順で拒否されることも、切り替えの前に確認できます。

```sh
EDGF_ENV=production EDGF_AUTH_MODE=dev python -c "import edgf.main"
# AuthConfigError: EDGF_ENV=production では EDGF_AUTH_MODE=dev を使用できません
```

監査ログの actor に `dev:` で始まる行があれば、dev モードで操作された記録です。

## 2. バックアップと復元

### バックアップ

```sh
pg_dump -Fc -d edgf > edgf-$(date +%Y%m%d%H%M).dump
```

- DB には個人データ本体は入っていません(§18.2)。`subject_ref` と監査ログは入るので、DB と同じ機密度で保管してください
- 署名鍵と `EDGF_SUBJECT_KEY` はダンプに含まれません。鍵は別の場所で保管します(3 章)
- `alembic_version` もダンプに含まれるので、同じ版か新しい版のアプリケーションで復元します

### 復元

```sh
createdb edgf_restored
pg_restore --no-owner --exit-on-error -d edgf_restored edgf-YYYYmmddHHMM.dump
EDGF_DATABASE_URL=postgresql+psycopg://.../edgf_restored alembic upgrade head
```

アプリケーションを新しい DB に向けて起動し、**ジョブを動かす前に**確認します。

```sh
curl -s -H "Authorization: Bearer $TOKEN" https://<host>/api/v1/audit/verify
```

| `valid` | `anchor.status` | 意味 | 対応 |
|---|---|---|---|
| true | `ok` | チェーンが正しく、最新のアンカーと一致 | 復元の完了 |
| false | `tail_truncated` | バックアップより新しいアンカーがある。`head_seq` より後の監査イベントが失われた | 失われた範囲(`head_seq` からアンカーの `seq`、それ以降)を記録し、Committee に報告する |
| false | `mismatch` | アンカーの時点の行が違う | **バックアップの改ざんを疑う。** 別の世代を確認する |
| false | `signature_invalid` / `file_tampered` | DB かファイルのアンカーの署名が合わない | 公開鍵の設定を確認し、合っていれば改ざんとして扱う |
| true | `unverifiable` | 検証用の公開鍵が設定されていない | `EDGF_ANCHOR_VERIFY_KEYS` を設定する |
| true | `none` | アンカーがまだ無い | 古いバックアップならアンカーの導入前。導入済みならファイルの場所の設定を確認する |

古いバックアップに戻すと `valid: false` のままになります。**失われた範囲を隠さないための動作です。** 書き込みが再開して先頭が追いつくと status は `mismatch` に変わるので(同じ seq が別の内容になるため)、報告の前に再開しないでください。報告のあとは、古いアンカーファイルを保全して `EDGF_ANCHOR_FILE` を新しいファイルに切り替え、切り替えの経緯を記録に残します。

CI の `restore` ジョブが同じ手順を毎回実行します(`scripts/ops/restore_drill.py`)。手元でも実行できます。

```sh
EDGF_DATABASE_URL=postgresql+psycopg://edgf:...@localhost:5432/edgf_drill python scripts/ops/restore_drill.py
```

## 3. 鍵の保管と復旧

| 鍵 | 失ったとき | 漏れたとき |
|---|---|---|
| `EDGF_SUBJECT_KEY` | 既存の `subject_ref` と照合できなくなり、消去要求の対象を特定できない。**バックアップからしか復旧できない** | 識別子の候補から `subject_ref` を計算できる。ローテーションは未実装なので、Data Plane を含む全体の再計算の計画が必要 |
| `EDGF_ANCHOR_SIGNING_KEY` | 新しい鍵を作れば続行できる。過去のアンカーは公開鍵で検証できる | 偽のアンカーを作られる。鍵を更新し、漏えいより後のアンカーの信頼性を外部の控え(Outbox の受信側、TSA)で確認する |
| Webhook の秘密鍵(`secret_ref` の参照先) | 新しい鍵を作って受信側と揃える。解決できない間は配信せず、再試行する | 署名を偽造される。鍵を更新し、受信側で古い鍵を拒否する |
| OIDC の署名鍵(IdP 側) | IdP の手順に従う(未知の `kid` は JWKS を再取得するので追従する) | 同左 |

共通の注意:

- 鍵は KMS やシークレット管理に置き、環境変数で渡します。DB、ログ、監査ログには保存しません(テストで確認しています)
- `EDGF_SUBJECT_KEY` と署名鍵は DB のバックアップと**別の場所**に、別の権限で保管します

### 署名鍵の作成と更新

```sh
python - <<'PY'
import base64, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
seed = os.urandom(32)
public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)
print("EDGF_ANCHOR_SIGNING_KEY=" + base64.b64encode(seed).decode())
print("public key: " + base64.b64encode(public).decode())
PY
```

更新の手順:

1. 古い公開鍵を `EDGF_ANCHOR_VERIFY_KEYS` に追加する(カンマ区切り)
2. 新しい鍵を `EDGF_ANCHOR_SIGNING_KEY` に設定して再起動する
3. `GET /audit/verify` の `anchor.status` が `ok` であることを確認する
4. 新しい公開鍵を外部の監査人に渡す

## 4. アンカーの検証

DB にも署名鍵にも触らずに、公開鍵だけでファイルを検証できます。

```sh
python scripts/verify_anchors.py /worm/anchors.jsonl --public-key <base64>
```

最後の行の `seq` と `hash` を、`GET /audit/verify` の `anchor.latest` と比べます。

ファイルは WORM(追記のみ、保持期間の間は削除不可)のストレージに置いてください。オブジェクトロックやイミュータブルな領域のマウントなどです。アプリケーションは追記だけを行いますが、**削除の防止はストレージの設定に依存します。**

RFC 3161 のトークンは `audit_anchors.sinks` に base64 で記録します。アプリケーションは応答の状態だけを確認するので、トークンの証明書チェーンの検証は次の手順で行います。

```sh
openssl ts -verify -in token.tsr -digest <署名対象の SHA-256> -CAfile tsa-ca.pem
```

## 5. ジョブの再開

状況は `GET /api/v1/ops/jobs`(admin、committee)で確認します。間隔の 3 倍を過ぎても成功していないジョブは `/ready` の `checks.jobs_overdue` に出ます。

- 全ジョブは冪等です。止まっていた分は、再開すれば次の実行でまとめて処理されます
- 異常終了すると、リースは `EDGF_JOB_LEASE_SECONDS`(既定 900 秒)で失効します。急ぐ場合でも `job_leases` を直接変更せず、失効を待ってください
- 手動の実行は `POST /api/v1/ops/jobs/{name}/run`(admin)か `python -m edgf.jobs run <name>` です
- 特定のジョブを止めるには `EDGF_JOB_<NAME>_ENABLED=false` を設定します

`consecutive_failures` が増えているときは、`last_error`(例外の型だけが入ります)を確認し、ログで `job failed` を検索します。

`audit_anchor` が失敗している間はアンカーが進みません。**アンカー以降の行の改ざんは、チェーンの検証の範囲に限られます。** `AnchorConfigError` は鍵の設定を、`OSError` はファイルの配置先を確認してください。TSA の失敗はジョブの失敗にせず、`sink_failures` に記録します。

## 6. デッドレターの処理

```sh
curl -H "Authorization: Bearer $TOKEN" https://<host>/api/v1/outbox/dead-letters
curl -X POST -H "Authorization: Bearer $TOKEN" https://<host>/api/v1/outbox/dead-letters/<id>/redeliver
```

1. `last_error` で原因を確認する。`secret_unavailable` は鍵の参照先の問題です
2. 受信側か鍵を直す
3. 再送する(admin のみ。監査ログに `outbox.redeliver` が残ります)

配信は at-least-once なので、再送は受信側で重複しても問題ありません。受信側はイベント ID で冪等にしてください。

## 7. 障害時の振る舞い

| 障害 | 振る舞い | 対応 |
|---|---|---|
| DB の停止 | `/ready` が 503。一時的なものは 503 `database-unavailable` | DB の復旧。アプリケーションの再起動は不要 |
| JWKS に届かない | 認証は 503 `auth-unavailable`(fail-close) | IdP の復旧。キャッシュ済みの鍵では動作を続ける |
| 監査チェーンの混雑 | 503(証跡なしでは確定しない) | 再試行。続くなら長いトランザクションを調べる |
| Policy が不正 | 起動時に停止 / `/ready` が 503 | 承認済みの Bundle の確認 |

Control Plane が止まってもデータ利用は Data Plane の側で継続します(§19)。ただし認可の判定は fail-close です。

## 8. 監査チェーンの改ざんを検知したら

1. **書き込みを止める。** `valid: false` になったら、ジョブを止めて調査の対象を固定します
2. `GET /audit/verify` の結果(`broken_at_seq`、`anchor`)を保全します
3. `pg_dump` で現状を保全し、バックアップの世代と比べます
4. Committee に報告します。証跡の信頼性の問題なので、調査の結果を残します
