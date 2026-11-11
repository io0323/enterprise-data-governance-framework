# MDM Hub の参照実装

Data Plane の側で Golden Record の値を持つ MDM Hub の最小の例です。Control Plane(edgf)には SDK(`clients/python`、`edgf-client`)で接続します。

- **値は Hub が持ちます**: Golden Record の値とソースレコードは Hub の SQLite に保存します。Control Plane は値を判定に使うだけで、由来、内容ハッシュ、版だけを保存します(設計書 §8、§18.2)
- **サーバのパッケージには含めません**: 参照用の例で、本番向けの機能(候補の絞り込み、同時実行の制御、利用システムへの配信)はありません

## 流れ

| 手順 | Hub の API | Control Plane の API(SDK) |
|---|---|---|
| ソースレコードの登録 | `POST /records` | `mdm_match` → `mdm_compose` → `mdm_verify` |
| Golden Record の参照 | `GET /golden/{id}` | (呼ばない。値は Hub にだけある) |
| 消去 | `POST /erasure/run` | `erasure_tasks` → `ack_erasure_task` → `complete_erasure_task` |

Hub のサービスの主体は、master 資産(例: `mdm.party`)の Custodian にします。

## 起動

```sh
pip install fastapi uvicorn -e clients/python
EDGF_URL=https://edgf.example.com EDGF_TOKEN_URL=https://idp.example.com/token \
EDGF_CLIENT_ID=mdm-hub EDGF_CLIENT_SECRET=... MDM_HUB_DB=hub.db \
uvicorn app:app --app-dir examples/mdm-hub
```

## 確認

CI の `data-plane-e2e` ジョブが、Control Plane を起動して match → compose → verify → 消去の完了までを確かめます(`tests/data_plane_e2e/test_mdm_hub_e2e.py`)。
