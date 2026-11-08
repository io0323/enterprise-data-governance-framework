"""Phase 10 のテストの共通部品。"""
from __future__ import annotations

from tests.conftest import dump_database  # noqa: F401

API = "/api/v1"
HASH = "ab" * 32
CONTROLS = ["encryption_at_rest", "encryption_in_transit", "masking"]


def U(actor: str, roles: str = "", domains: str = "", clearance: str | None = None) -> dict:
    h = {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}
    if clearance:
        h["X-Clearance"] = clearance
    return h


ADMIN, COMMITTEE = U("root", "admin"), U("chair", "committee")
OWNER, STEWARD, CUSTODIAN = U("ai-owner", "owner"), U("ai-steward", "steward"), U("ai-custodian", "custodian")


def asset(client, qn, *, data_class="analytical", sensitivity="internal", owner="src-owner", custodian="src-custodian",
          steward="src-steward", **kw) -> dict:
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "description": "d", "data_class": data_class, "sensitivity": sensitivity,
        "owner": owner, "steward": steward, "custodian": custodian, "controls": CONTROLS, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def ai_asset(client, qn, kind, **kw) -> dict:
    kw = {"owner": "ai-owner", "steward": "ai-steward", "custodian": "ai-custodian", **kw}
    return asset(client, qn, data_class="ai", ai_kind=kind, **kw)


def edge(client, source, target) -> None:
    assert client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": source, "target": target}).status_code == 201


def dataset_version(client, dataset_id, source_ids, version="v1", **kw) -> dict:
    body = {"version": version, "manifest_hash": HASH, "record_count": 10,
            "sources": [{"asset_id": s, "version": "2026-01"} for s in source_ids], **kw}
    r = client.post(f"{API}/ai/assets/{dataset_id}/dataset-versions", headers=CUSTODIAN, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def model_version(client, model_id, dataset_version_ids, version="m1", **kw) -> dict:
    body = {"version": version, "dataset_version_ids": dataset_version_ids, **kw}
    r = client.post(f"{API}/ai/assets/{model_id}/model-versions", headers=CUSTODIAN, json=body)
    assert r.status_code == 201, r.text
    return r.json()
