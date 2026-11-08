"""MDM テストの共通ヘルパー."""
from __future__ import annotations

API = "/api/v1"
MDM = f"{API}/mdm"


def U(actor: str, roles: str = "", domains: str = "") -> dict:
    return {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}


ADMIN, COMMITTEE = U("root", "admin"), U("chair", "committee")
HUB, OWNER, STEWARD = U("hub"), U("own"), U("stw")


def master_asset(client, qn="mdm.party", **kw) -> str:
    client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "master"})
    body = {"qualified_name": qn, "name": qn, "domain": "master", "data_class": "master", "sensitivity": "confidential",
            "owner": "own", "steward": "stw", "custodian": "hub", "description": "d", **kw}
    r = client.post(f"{API}/assets", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]
