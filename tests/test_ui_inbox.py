"""Phase 17c: 承認箱の操作 (確定事項 6 の ①)。新しい承認エンジンと既存の 8 種類のアダプタを、UI の経路で実行する.

UI は承認箱のアダプタが返す operations (判断を送る先の既存の API と入力の項目) をそのまま使う。ここでも同じ方法で呼ぶ。
既存の API に却下の無い種類 (mdm_rule / reference_data / contract / cdm / model_remediation) は、既存の操作を確かめる。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from edgf.db import SessionLocal
from edgf.models import AuditEvent
from tests.mesh_helpers import add_port, contract, domain, draft, port_asset, product
from tests.process_helpers import API, U
from tests.test_approvals import full_inbox  # noqa: F401 - フィクスチャ (新しいエンジンと 8 種類の案件)
from tests.ui_helpers import as_ui

OWNER_UI = ("po", "owner", "sales")
CHAIR_UI = ("chair", "committee", "")


def items(ui, source=None) -> list[dict]:
    r = ui.get(f"{API}/approvals/inbox", params={"source": source} if source else {})
    assert r.status_code == 200, r.text
    return r.json()


def run(ui, item, op_id, **fields):
    """アダプタの operation のとおりに、既存の API を呼ぶ (固定の body に入力の項目を足す)。"""
    op = next(o for o in item["operations"] if o["id"] == op_id)
    assert op["path"].startswith("/api/v1/") and item["action"] == f"{op['method']} {op['path']}"
    return ui.client.request(op["method"], op["path"], json={**op["body"], **fields}, headers=ui.headers())


def ui_audit(action) -> list[dict]:
    with SessionLocal() as db:
        return [e.payload for e in db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq))]


def test_every_item_has_operations_with_input_fields(full_inbox):
    app = full_inbox["client"].app
    found = {i["source"]: i for who in (OWNER_UI, CHAIR_UI) for i in items(as_ui(app, *who))}
    assert set(found) == {"approval", "policy_exception", "mdm_rule", "reference_data", "training_approval", "contract", "subscription",
                          "cdm", "model_remediation"}
    ops = {source: [o["id"] for o in item["operations"]] for source, item in found.items()}
    assert ops == {"approval": ["approve", "reject"], "policy_exception": ["approve", "reject"], "training_approval": ["approve", "reject"],
                   "subscription": ["approve", "reject"], "mdm_rule": ["approve"], "reference_data": ["publish"], "contract": ["publish"],
                   "cdm": ["update"], "model_remediation": ["retrain", "unlearn", "accept"]}
    for source in ("approval", "policy_exception", "training_approval", "subscription"):
        reject = found[source]["operations"][1]
        assert [f for f in reject["fields"] if f["required"]] and reject["step_up"] is True
    assert found["cdm"]["operations"][0]["step_up"] is False  # Issue の更新は承認行為ではない
    accept = found["model_remediation"]["operations"][2]
    assert {f["name"]: f["required"] for f in accept["fields"]} == {"reason": True, "accept_until": True}


# ---------------------------------------------------------------- 承認と却下のある 4 種類
APPROVE_REJECT = [("approval", OWNER_UI, "approval.decision"), ("policy_exception", CHAIR_UI, "exception.decision"),
                  ("training_approval", OWNER_UI, "ai.training.decision"), ("subscription", OWNER_UI, "mesh.subscription.decision")]


@pytest.mark.parametrize("source,who,audit_action", APPROVE_REJECT, ids=[c[0] for c in APPROVE_REJECT])
def test_approve_on_ui_path(full_inbox, source, who, audit_action):
    ui = as_ui(full_inbox["client"].app, *who)
    [item] = items(ui, source)
    r = run(ui, item, "approve", comment="確認しました")
    assert r.status_code == 200, r.text
    assert item["id"] not in {i["id"] for i in items(ui, source)}
    payload = ui_audit(audit_action)[-1]
    assert payload["channel"] == "ui" and len(payload["session"]) == 12


@pytest.mark.parametrize("source,who,audit_action", APPROVE_REJECT, ids=[c[0] for c in APPROVE_REJECT])
def test_reject_on_ui_path_requires_reason(full_inbox, source, who, audit_action):
    ui = as_ui(full_inbox["client"].app, *who)
    [item] = items(ui, source)
    assert run(ui, item, "reject").status_code == 422
    assert run(ui, item, "reject", comment="   ").status_code == 422
    r = run(ui, item, "reject", comment="根拠が不足しています")
    assert r.status_code == 200, r.text
    assert item["id"] not in {i["id"] for i in items(ui, source)}
    assert ui_audit(audit_action)[-1]["channel"] == "ui"


# ---------------------------------------------------------------- 既存の API に却下の無い 5 種類
def test_mdm_rule_approve_on_ui_path(full_inbox):
    ui = as_ui(full_inbox["client"].app, *OWNER_UI)
    [item] = items(ui, "mdm_rule")
    assert run(ui, item, "approve").status_code == 200
    assert items(ui, "mdm_rule") == [] and ui_audit("mdm.rule_approve")[-1]["channel"] == "ui"


def test_reference_data_publish_on_ui_path(full_inbox):
    ui = as_ui(full_inbox["client"].app, *OWNER_UI)
    [item] = items(ui, "reference_data")
    assert run(ui, item, "publish").status_code == 200
    assert items(ui, "reference_data") == [] and ui_audit("refdata.publish")[-1]["channel"] == "ui"


def test_contract_publish_on_ui_path(full_inbox):
    ui = as_ui(full_inbox["client"].app, *OWNER_UI)
    [item] = items(ui, "contract")
    r = run(ui, item, "publish")
    assert r.status_code == 200, r.text
    assert items(ui, "contract") == [] and ui_audit("mesh.contract.publish")[-1]["channel"] == "ui"


def test_cdm_issue_update_on_ui_path(full_inbox):
    ui = as_ui(full_inbox["client"].app, *CHAIR_UI)
    [item] = items(ui, "cdm")
    r = run(ui, item, "update", status="resolved", comment="Contract を CDM の 2 版に合わせる")
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert items(ui, "cdm") == [] and ui_audit("issue.update")[-1]["channel"] == "ui"


def test_model_remediation_on_ui_path(full_inbox):
    ui = as_ui(full_inbox["client"].app, *CHAIR_UI)
    [item] = items(ui, "model_remediation")
    assert run(ui, item, "accept", reason="影響は軽微").status_code == 422  # 期限が必須
    until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = run(ui, item, "accept", reason="影響は軽微", accept_until=until)
    assert r.status_code == 200, r.text
    assert ui_audit("ai.remediation.decide")[-1]["channel"] == "ui"


# ---------------------------------------------------------------- 職務分離と ABAC は API と同じ
def test_separation_of_duties_is_the_same_as_api(full_inbox):
    client = full_inbox["client"]
    req = full_inbox["new"]
    path = f"{API}/approvals/{req['id']}/decision"
    body = {"approve": True}
    cases = [("alice", "", ""), ("asset-custodian", "custodian", "sales")]  # 申請者本人、Custodian
    for who in cases:
        via_api = client.post(path, headers=U(*who), json=body)
        via_ui = as_ui(client.app, *who).post(path, json=body)
        assert via_api.status_code == via_ui.status_code == 403, (who, via_api.text, via_ui.text)


def test_abac_is_the_same_as_api(client):
    """読めない資産の案件は承認箱に出ない。直接呼んだときの結果は、同じ条件の案件を API で呼んだときと同じ。

    Contract の公開の API は Product の Owner であれば通す (既存の挙動)。UI の経路でも同じ判定になる。
    """
    domain(client)
    p = product(client)
    versions, assets = [], []
    for name in ("secret1", "secret2"):
        asset_row = port_asset(client, f"sales.{name}", sensitivity="restricted", owner="so")
        assets.append(asset_row["id"])
        port = add_port(client, p["id"], asset_row["id"], name=name)
        versions.append(draft(client, contract(client, port["id"])["id"], sensitivity="restricted"))
    who = ("po", "owner", "sales", "confidential")
    low = as_ui(client.app, *who)
    assert items(low) == []
    via_ui = low.post(f"{API}/mesh/contract-versions/{versions[0]['id']}/publish")
    via_api = client.post(f"{API}/mesh/contract-versions/{versions[1]['id']}/publish", headers={**U(*who[:3]), "X-Clearance": who[3]})
    assert via_ui.status_code == via_api.status_code, (via_ui.text, via_api.text)
    # 閲覧の API は ABAC で拒否する (UI の経路でも同じ)
    via_ui = low.get(f"{API}/assets/{assets[0]}")
    via_api = client.get(f"{API}/assets/{assets[0]}", headers={**U(*who[:3]), "X-Clearance": who[3]})
    assert via_ui.status_code == via_api.status_code == 403


# ---------------------------------------------------------------- Policy の変更: 承認から公開まで
def test_policy_bundle_approve_and_publish_on_ui_path(client):
    r = client.post(f"{API}/policies/bundles", headers=U("proposer", "committee"), json={"changes": {"extra.yaml": "policies: []\n"},
                                                                                         "reason": "テスト"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    chair = as_ui(client.app, *CHAIR_UI)
    [item] = [i for i in items(chair, "approval") if i["id"] == proposal["approval_id"]]
    assert run(chair, item, "approve").status_code == 200
    [publish] = [i for i in items(chair, "approval") if i["id"] == f"policy_bundle:{proposal['proposal_version']}"]
    assert [o["id"] for o in publish["operations"]] == ["publish"] and publish["operations"][0]["step_up"] is True
    assert run(chair, publish, "publish").status_code == 200
    assert not [i for i in items(chair, "approval") if i["id"].startswith("policy_bundle:")]
    assert ui_audit("policy.publish")[-1]["channel"] == "ui"
    # 公開の項目は policy.publish を持つ人にだけ出る
    assert not [i for i in items(as_ui(client.app, *OWNER_UI)) if i["id"].startswith("policy_bundle:")]
