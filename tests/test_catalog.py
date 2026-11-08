from tests.conftest import H

API = "/api/v1"


def test_register_and_pii_inference(client, sales_asset):
    assert sales_asset["contains_pii"] is True  # schema の pii フラグから推定
    assert sales_asset["lifecycle_state"] == "draft"
    r = client.post(f"{API}/assets", headers=H(), json={**{"qualified_name": "sales.orders"},
                    "name": "x", "data_class": "master", "sensitivity": "internal"})
    assert r.status_code == 409


def test_unknown_domain_rejected(client):
    r = client.post(f"{API}/assets", headers=H(), json={"qualified_name": "x.y", "name": "y", "domain": "nope",
                                                         "data_class": "master", "sensitivity": "internal"})
    assert r.status_code == 422


def test_search_and_orphan(client, sales_asset):
    client.post(f"{API}/assets", headers=H(), json={"qualified_name": "hr.misc", "name": "misc",
                                                     "data_class": "operational", "sensitivity": "internal",
                                                     "tags": ["legacy"]})
    assert [a["qualified_name"] for a in client.get(f"{API}/assets", params={"orphan": True}).json()] == ["hr.misc"]
    assert len(client.get(f"{API}/assets", params={"q": "受注"}).json()) == 1
    assert [a["qualified_name"] for a in client.get(f"{API}/assets", params={"q": "legacy"}).json()] == ["hr.misc"]


def test_glossary_search(client, sales_asset):
    t = client.post(f"{API}/glossary/terms", headers=H(), json={"name": "受注金額", "definition": "税抜の受注確定額"}).json()
    client.put(f"{API}/glossary/terms/{t['id']}/assets/{sales_asset['id']}", headers=H())
    hits = client.get(f"{API}/assets", params={"q": "税抜"}).json()
    assert [a["id"] for a in hits] == [sales_asset["id"]]
    assert client.post(f"{API}/glossary/terms", headers=H(), json={"name": "受注金額", "definition": "dup"}).status_code == 409


def test_lifecycle_and_legal_hold(client, sales_asset):
    aid = sales_asset["id"]
    assert client.post(f"{API}/assets/{aid}/lifecycle", headers=H(), json={"target_state": "archived"}).status_code == 409
    for s in ("active", "archived"):
        assert client.post(f"{API}/assets/{aid}/lifecycle", headers=H(), json={"target_state": s}).status_code == 200
    client.post(f"{API}/assets/{aid}/legal-hold", headers=H(), json={"legal_hold": True, "reason": "訴訟"})
    assert client.post(f"{API}/assets/{aid}/lifecycle", headers=H(), json={"target_state": "deleted"}).status_code == 409
    client.post(f"{API}/assets/{aid}/legal-hold", headers=H(), json={"legal_hold": False, "reason": "解除"})
    r = client.post(f"{API}/assets/{aid}/lifecycle", headers=H(), json={"target_state": "deleted"})
    assert r.status_code == 200 and r.json()["lifecycle_state"] == "deleted"
    assert client.get(f"{API}/assets").json() == []
    actions = [e["action"] for e in client.get(f"{API}/audit/events", params={"entity_id": aid}).json()]
    assert "lifecycle.delete_requested" in actions


def test_ownership_change_audited(client, sales_asset):
    aid = sales_asset["id"]
    r = client.put(f"{API}/assets/{aid}/ownership", headers=H("committee"), json={"owner": "owner2"})
    assert r.json()["owner"] == "owner2" and r.json()["steward"] == "steward1"
    ev = client.get(f"{API}/audit/events", params={"action": "asset.ownership_change"}).json()[0]
    assert ev["payload"]["before"]["owner"] == "owner1" and ev["actor"] == "committee"
