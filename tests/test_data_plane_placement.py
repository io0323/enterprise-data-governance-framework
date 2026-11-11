"""Phase 15: 置き場所の保証。資産一覧と利用実績を送っても、値や利用者を特定する情報が Control Plane に残らない (§18.2)."""
from __future__ import annotations

from tests.conftest import dump_database
from tests.process_helpers import ADMIN, API, U, asset

MARK = "zzplacementmark"  # 値、サンプル、統計、利用者の ID に入れる目印
DP = U("dp-sales", "custodian", "sales")


def test_values_and_user_identifiers_leave_no_trace(client, edgf_logs):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales"})
    asset(client, "sales.orders", domain="sales")
    rejected = [
        (f"{API}/discovery/snapshots", {"source": "warehouse.sales", "assets": [{"qualified_name": "sales.orders", "sample": [{"email": f"{MARK}@example.com"}]}]}),
        (f"{API}/discovery/snapshots", {"source": "warehouse.sales", "assets": [{"qualified_name": "sales.orders", "schema": [
            {"name": "email", "type": "string", "min": f"{MARK}-min", "max": f"{MARK}-max", "top_values": [MARK]}]}]}),
        (f"{API}/discovery/snapshots", {"source": "warehouse.sales", "assets": [{"qualified_name": "sales.orders", "statistics": {"note": MARK}}]}),
        (f"{API}/usage/daily", {"records": [{"qualified_name": "sales.orders", "day": "2026-11-01", "query_count": 3, "user_count": 1, "user_ids": [MARK]}]}),
        (f"{API}/usage/daily", {"records": [{"qualified_name": "sales.orders", "day": "2026-11-01", "query_count": 3, "user_count": 1, "query_text": f"select '{MARK}'"}]}),
    ]
    for path, body in rejected:
        for headers in (DP, {**DP, "Idempotency-Key": f"key-{len(path)}-{len(str(body))}"}):
            r = client.post(path, headers=headers, json=body)
            assert r.status_code == 422 and MARK not in r.text
    # 受け付けた送信も、メタデータと集計値だけが残る
    assert client.post(f"{API}/discovery/snapshots", headers=DP, json={"source": "warehouse.sales", "assets": [
        {"qualified_name": "sales.orders", "schema": [{"name": "email", "type": "string", "nullable": True}]}]}).status_code == 201
    assert client.post(f"{API}/usage/daily", headers=DP, json={"records": [
        {"qualified_name": "sales.orders", "day": "2026-11-01", "query_count": 3, "user_count": 1}]}).status_code == 200
    dump = dump_database()
    audit = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).text.lower()
    logs = edgf_logs.text()
    assert MARK not in dump and MARK not in audit and MARK not in logs
    assert "sales.orders" in dump  # 検査が空振りしていないこと
