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


def run_quality(client, asset_id, score_ok=True) -> None:
    """構成元の品質検査の実績を作る (値は評価後に破棄される)。"""
    qa = U("qa", "admin,owner,custodian", "*")
    r = client.post(f"{API}/quality/assets/{asset_id}/rules", headers=qa, json={
        "name": "not-null", "dimension": "completeness", "rule_type": "not_null", "column": "id"})
    assert r.status_code == 201, r.text
    r = client.post(f"{API}/quality/assets/{asset_id}/runs", headers=qa,
                    json={"records": [{"id": 1 if score_ok else None}]})
    assert r.status_code == 201, r.text


def request_approval(client, dataset_version_id, **kw):
    body = {"license_status": "verified", "license_ref": "lic://1", "bias_evaluation_ref": "eval://1", **kw}
    return client.post(f"{API}/ai/dataset-versions/{dataset_version_id}/training-approvals", headers=OWNER, json=body)


def approve_dataset(client, dataset_version_id, source_owners=("src-owner",), committee=False) -> dict:
    """全 Owner (と必要なら Committee) の承認を経て Dataset Version を approved にする。"""
    r = request_approval(client, dataset_version_id)
    assert r.status_code == 201, r.text
    approval = r.json()
    for owner in source_owners:
        r = client.post(f"{API}/ai/training-approvals/{approval['id']}/decision", headers=U(owner), json={"approve": True})
        assert r.status_code == 200, r.text
    if committee:
        r = client.post(f"{API}/ai/training-approvals/{approval['id']}/decision", headers=COMMITTEE, json={"approve": True})
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved", r.text
    return r.json()


def certify(client, asset_id, owner="src-owner") -> None:
    """Certification のゲートを通して Certified にする (品質の実績と active が必要)。"""
    run_quality(client, asset_id)
    assert client.post(f"{API}/assets/{asset_id}/lifecycle", headers=ADMIN, json={"target_state": "active"}).status_code == 200
    r = client.post(f"{API}/assets/{asset_id}/certification", headers=U(owner), json={"certification": "certified"})
    assert r.status_code == 200, r.text


def policy_result(client, asset_id, policy_id):
    results = client.get(f"{API}/policies/evaluate/assets/{asset_id}", headers=ADMIN).json()["results"]
    return next((r for r in results if r["policy_id"] == policy_id), None)
