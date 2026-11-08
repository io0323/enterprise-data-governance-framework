from tests.conftest import H

API = "/api/v1"
RECORDS = [
    {"order_id": "A1", "email": "a@x.com", "amount": 100, "start": "2026-01-01", "end": "2026-01-02", "status": "open"},
    {"order_id": "A2", "email": "bad-mail", "amount": -5, "start": "2026-01-03", "end": "2026-01-01", "status": "zzz"},
    {"order_id": "A2", "email": None, "amount": 50, "start": "2026-01-01", "end": "2026-01-05", "status": "closed"},
    {"order_id": "A4", "email": "d@x.com", "amount": 70, "start": "2026-01-01", "end": "2026-01-05", "status": "open"},
]


def _rule(client, aid, **kw):
    r = client.post(f"{API}/quality/assets/{aid}/rules", headers=H(), json=kw)
    assert r.status_code == 201, r.text
    return r.json()


def test_seven_dimensions_and_issue(client, sales_asset):
    aid = sales_asset["id"]
    _rule(client, aid, name="email必須", dimension="completeness", rule_type="not_null", column="email", threshold=0.9, severity="high")
    _rule(client, aid, name="order_id一意", dimension="uniqueness", rule_type="unique", column="order_id", severity="critical")
    _rule(client, aid, name="email形式", dimension="validity", rule_type="regex", column="email", params={"pattern": r"[^@]+@[^@]+\.\w+"})
    _rule(client, aid, name="金額範囲", dimension="validity", rule_type="range", column="amount", params={"min": 0})
    _rule(client, aid, name="期間整合", dimension="consistency", rule_type="comparison", params={"left": "end", "op": ">=", "right": "start"})
    _rule(client, aid, name="状態コード", dimension="integrity", rule_type="referential", column="status", params={"reference_values": ["open", "closed"]})
    _rule(client, aid, name="金額照合", dimension="accuracy", rule_type="reference_match", column="amount", params={"key": "order_id", "reference": {"A1": 100, "A4": 70}})
    run = client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": RECORDS}).json()
    by = {r["name"]: r for r in run["results"]}
    assert by["email必須"]["pass_rate"] == 0.75
    assert by["order_id一意"]["pass_rate"] == 0.5
    assert by["email形式"]["evaluated"] == 3  # null は completeness 側で評価
    assert by["期間整合"]["passed"] == 3
    assert by["金額照合"]["pass_rate"] == 1.0
    assert set(run["dimension_scores"]) == {"completeness", "uniqueness", "validity", "consistency", "integrity", "accuracy"}
    assert 0 < run["score"] < 1
    assert len(run["issues_opened"]) == 6  # accuracy 以外は閾値割れ
    # 再実行しても重複起票しない
    run2 = client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": RECORDS}).json()
    assert run2["issues_opened"] == []
    issue = client.get(f"{API}/issues", params={"kind": "quality"}).json()[0]
    assert issue["assignee"] == "steward1" and issue["escalation_level"] == "steward"


def test_freshness_rule(client, sales_asset):
    aid = sales_asset["id"]
    _rule(client, aid, name="鮮度", dimension="timeliness", rule_type="freshness", column="ts", params={"max_age_hours": 1})
    run = client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": [{"ts": "2020-01-01T00:00:00Z"}]}).json()
    assert run["score"] == 0.0


def test_invalid_rule_rejected(client, sales_asset):
    r = client.post(f"{API}/quality/assets/{sales_asset['id']}/rules", headers=H(),
                    json={"name": "x", "dimension": "validity", "rule_type": "regex", "column": "a"})
    assert r.status_code == 422


def test_escalation_flow(client, sales_asset):
    i = client.post(f"{API}/issues", headers=H(), json={"asset_id": sales_asset["id"], "title": "定義不一致"}).json()
    i = client.post(f"{API}/issues/{i['id']}/escalate", headers=H()).json()
    assert i["escalation_level"] == "owner" and i["assignee"] == "owner1"
    i = client.post(f"{API}/issues/{i['id']}/escalate", headers=H()).json()
    assert i["escalation_level"] == "committee"
    assert client.post(f"{API}/issues/{i['id']}/escalate", headers=H()).status_code == 409
    i = client.patch(f"{API}/issues/{i['id']}", headers=H(), json={"status": "resolved"}).json()
    assert i["resolved_at"] is not None
