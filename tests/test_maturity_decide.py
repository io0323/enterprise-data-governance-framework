"""Phase 18c: 自己評価、Committee の判定 (承認エンジンの種別)、職務分離、参照ドメイン."""
from __future__ import annotations

import socket
import urllib.request

import pytest

from tests.conftest import dump_database
from tests.maturity_helpers import CHAIR, LEVEL2, MAT, create, decided, domain, evaluate, snapshots
from tests.process_helpers import API, U, audit_actions
from tests.ui_helpers import as_ui

OWNER = U("dom-owner", "owner", "sales")
EVIDENCE = [{"uri": "https://docs.example.com/governance/committee", "description": "Committee の設置の議事録"}]
L1_MANUAL = ["L1-COMMITTEE", "L1-OWNER-APPOINT"]


def items(*ids, achieved=True):
    return [{"criterion_id": cid, "achieved": achieved, "evidence": EVIDENCE} for cid in ids]


def self_assess(client, assessment_id, body_items, who=OWNER, code=200):
    r = client.put(f"{MAT}/assessments/{assessment_id}/self-assessment", headers=who, json={"items": body_items})
    assert r.status_code == code, r.text
    return r.json()


def decide(client, assessment_id, level, manual=None, reason="", who=CHAIR, code=200):
    r = client.post(f"{MAT}/assessments/{assessment_id}/decision", headers=who,
                    json={"level": level, "manual": manual if manual is not None else dict.fromkeys(L1_MANUAL, "accepted"), "reason": reason})
    assert r.status_code == code, r.text
    return r.json()


@pytest.fixture()
def assessed(client):
    """sales (Owner: dom-owner) を Level 2 の指標で自動判定し、Level 1 の手動の基準を自己評価した状態。"""
    domain(client)
    snapshots("sales", LEVEL2)
    a = evaluate(client, create(client)["id"])
    return client, self_assess(client, a["id"], items(*L1_MANUAL))


# ---------------------------------------------------------------- 自己評価
def test_self_assessment_opens_an_approval(assessed):
    client, a = assessed
    assert a["status"] == "self_assessed" and a["self_assessed_by"] == "dom-owner" and a["approval_id"]
    req = client.get(f"{API}/approvals/{a['approval_id']}", headers=CHAIR).json()
    assert req["type"] == "maturity_assessment" and req["requested_by"] == "dom-owner" and req["target_id"] == a["id"]
    again = self_assess(client, a["id"], items(*L1_MANUAL, "L2-CDE"))  # 判定の前なら出し直せる。案件は 1 つのまま
    assert again["approval_id"] == a["approval_id"] and len(again["self_assessment"]) == 3


@pytest.mark.parametrize("who", [U("s", "steward", "sales"), U("o2", "owner", "hr"), U("chair", "committee"), U("c", "custodian", "sales")])
def test_only_the_domain_owner_self_assesses(client, who):
    domain(client)
    a = evaluate(client, create(client)["id"])
    self_assess(client, a["id"], items("L1-COMMITTEE"), who=who, code=403)


def test_self_assessment_needs_auto_evaluation(client):
    domain(client)
    a = create(client)
    self_assess(client, a["id"], items("L1-COMMITTEE"), code=409)


@pytest.mark.parametrize("bad", [
    [{"criterion_id": "L1-OWNER-RATE", "achieved": True}],  # 自動の指標
    [{"criterion_id": "NOPE", "achieved": True}],
    items("L1-COMMITTEE") + items("L1-COMMITTEE"),
])
def test_self_assessment_validation(client, bad):
    domain(client)
    a = evaluate(client, create(client)["id"])
    self_assess(client, a["id"], bad, code=422)


@pytest.mark.parametrize("uri", ["http://docs.example.com/x", "ftp://docs.example.com/x", "file:///etc/passwd", "https://", "https://a b.com/x",
                                 "javascript:alert(1)"])
def test_evidence_uri_must_be_https(client, uri):
    domain(client)
    a = evaluate(client, create(client)["id"])
    self_assess(client, a["id"], [{"criterion_id": "L1-COMMITTEE", "achieved": True, "evidence": [{"uri": uri}]}], code=422)


def test_evidence_description_is_guarded_and_not_stored(client):
    domain(client)
    a = evaluate(client, create(client)["id"])
    marker = "maturity.marker.7c1@example.com"
    r = client.put(f"{MAT}/assessments/{a['id']}/self-assessment", headers=OWNER, json={"items": [
        {"criterion_id": "L1-COMMITTEE", "achieved": True, "evidence": [{"uri": "https://docs.example.com/x", "description": f"連絡先 {marker}"}]}]})
    assert r.status_code == 422 and r.json()["type"].endswith("audit-payload-rejected") and marker not in r.text
    assert marker not in dump_database()
    long = "x" * 1001
    r = client.put(f"{MAT}/assessments/{a['id']}/self-assessment", headers=OWNER, json={"items": [
        {"criterion_id": "L1-COMMITTEE", "achieved": True, "evidence": [{"uri": "https://docs.example.com/x", "description": long}]}]})
    assert r.status_code == 422


def test_control_plane_does_not_connect_to_evidence(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("証拠の URI に接続しようとしました")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    domain(client)
    a = evaluate(client, create(client)["id"])
    assert self_assess(client, a["id"], items(*L1_MANUAL))["status"] == "self_assessed"


# ---------------------------------------------------------------- 判定の上限
def test_first_decision_is_capped_at_1(assessed):
    client, a = assessed
    assert (a["indicative_level"], a["level_cap"]) == (2, 1)
    r = client.post(f"{MAT}/assessments/{a['id']}/decision", headers=CHAIR, json={"level": 2, "manual": dict.fromkeys(L1_MANUAL, "accepted")})
    assert r.status_code == 422 and r.json()["level_cap"] == 1 and "飛び級" in r.json()["detail"]
    body = decide(client, a["id"], 1, reason="手動の基準を確認した")
    assert body["status"] == "decided" and body["decided_level"] == 1 and body["decided_by"] == "chair"
    assert body["decision"] == {"indicative_level": 2, "previous_level": 0, "level_cap": 1, "level": 1,
                                "manual": dict.fromkeys(L1_MANUAL, "accepted"), "reason": "手動の基準を確認した"}
    assert client.get(f"{API}/approvals/{a['approval_id']}", headers=CHAIR).json()["status"] == "approved"
    event = [e for e in audit_actions(client) if e["action"] == "maturity.decide"][-1]
    assert event["payload"]["level"] == 1 and event["payload"]["level_cap"] == 1


def test_manual_criteria_must_be_accepted_up_to_the_level(assessed):
    client, a = assessed
    decide(client, a["id"], 1, manual={"L1-COMMITTEE": "accepted", "L1-OWNER-APPOINT": "rejected"}, code=422)
    decide(client, a["id"], 1, manual={"L1-COMMITTEE": "accepted"}, code=422)
    decide(client, a["id"], 1, manual={**dict.fromkeys(L1_MANUAL, "accepted"), "NOPE": "accepted"}, code=422)
    decide(client, a["id"], 1, manual={**dict.fromkeys(L1_MANUAL, "accepted"), "L2-CDE": "maybe"}, code=422)
    assert decide(client, a["id"], 0, manual={"L1-COMMITTEE": "rejected"})["decided_level"] == 0  # 0 なら手動の基準は不要


def test_lowering_requires_a_reason(client):
    domain(client)
    decided("sales", 2)
    snapshots("sales", {"owner_assignment_rate": 0.5})  # 目安は 1 に下がった
    a = evaluate(client, create(client)["id"])
    assert (a["previous_level"], a["indicative_level"], a["level_cap"]) == (2, 1, 1)
    self_assess(client, a["id"], items(*L1_MANUAL))
    decide(client, a["id"], 1, code=422)
    assert decide(client, a["id"], 1, reason="Owner の割当率が下がった")["decided_level"] == 1


def test_decided_record_is_immutable(assessed):
    client, a = assessed
    decide(client, a["id"], 1)
    decide(client, a["id"], 0, reason="やり直し", code=409)
    self_assess(client, a["id"], items(*L1_MANUAL), code=409)
    assert client.post(f"{MAT}/assessments/{a['id']}/cancel", headers=CHAIR, json={"reason": "x"}).status_code == 409
    assert client.post(f"{MAT}/assessments/{a['id']}/evaluate", headers=CHAIR).status_code == 409


def test_generic_approval_api_cannot_decide(assessed):
    client, a = assessed
    r = client.post(f"{API}/approvals/{a['approval_id']}/decision", headers=CHAIR, json={"approve": True})
    assert r.status_code == 409 and r.json()["decision_path"] == f"/api/v1/maturity/assessments/{a['id']}/decision"


# ---------------------------------------------------------------- 職務分離
@pytest.mark.parametrize("who", [
    U("dom-owner", "committee,owner", "sales"),  # 申請者本人で、対象ドメインの Owner
    U("other-owner", "committee,owner", "sales"),  # 対象ドメインの Owner (ドメイン内のロール)
    U("dom-steward", "committee,steward", "sales"),  # 対象ドメインの Steward
    U("global-owner", "committee,owner", "*"),  # 全ドメインの Owner
])
def test_domain_owner_and_steward_cannot_decide_even_as_committee(assessed, who):
    client, a = assessed
    r = client.post(f"{MAT}/assessments/{a['id']}/decision", headers=who, json={"level": 1, "manual": dict.fromkeys(L1_MANUAL, "accepted")})
    assert r.status_code == 403
    denied = [e for e in audit_actions(client) if e["action"] == "authz.denied" and e["entity_id"] == a["id"]]
    assert denied and denied[-1]["payload"]["action"] == "maturity.decide"
    assert a["approval_id"] not in {i["id"] for i in client.get(f"{API}/approvals/inbox", headers=who).json()}


def test_domain_owner_by_domain_record_is_excluded(assessed):
    """ドメインの記録の Owner (dom-owner) は、ロールに owner が無くても加われない。"""
    client, a = assessed
    r = client.post(f"{MAT}/assessments/{a['id']}/decision", headers=U("dom-owner", "committee"),
                    json={"level": 1, "manual": dict.fromkeys(L1_MANUAL, "accepted")})
    assert r.status_code == 403


@pytest.mark.parametrize("who", [U("c", "custodian", "sales"), U("o", "owner", "hr"), U("x")])
def test_non_committee_cannot_decide(assessed, who):
    client, a = assessed
    decide(client, a["id"], 1, who=who, code=403)


def test_owner_of_another_domain_on_the_committee_can_decide(assessed):
    client, a = assessed
    assert decide(client, a["id"], 1, who=U("hr-owner", "committee,owner", "hr"))["decided_level"] == 1


# ---------------------------------------------------------------- 承認箱とステップアップ
def test_inbox_item_for_committee(assessed):
    client, a = assessed
    [item] = [i for i in client.get(f"{API}/approvals/inbox", headers=CHAIR).json() if i["id"] == a["approval_id"]]
    assert item["title"].startswith("成熟度の判定: sales")
    [op] = item["operations"]
    assert op["path"] == f"/api/v1/maturity/assessments/{a['id']}/decision" and op["step_up"] is True
    fields = {f["name"]: f for f in op["fields"]}
    assert fields["level"]["options"] == ["0", "1"] and fields["level"]["required"]
    assert {"manual.L1-COMMITTEE", "manual.L1-OWNER-APPOINT", "reason"} <= set(fields)


def test_decision_on_ui_path_needs_step_up(assessed):
    client, a = assessed
    body = {"level": 1, "manual": dict.fromkeys(L1_MANUAL, "accepted")}
    stale = as_ui(client.app, "chair", "committee", auth_age_minutes=60)
    r = stale.post(f"{MAT}/assessments/{a['id']}/decision", json=body)
    assert r.status_code == 401 and r.json()["type"].endswith("step-up-required")
    fresh = as_ui(client.app, "chair", "committee")
    r = fresh.post(f"{MAT}/assessments/{a['id']}/decision", json={**body, "level": "1"})  # フォームの値は文字列
    assert r.status_code == 200 and r.json()["decided_level"] == 1


# ---------------------------------------------------------------- 参照ドメイン
def test_reference_domains(assessed):
    client, a = assessed
    r = client.post(f"{MAT}/references", headers=CHAIR, json={"domain": "sales", "reason": "先行ドメイン"})
    assert r.status_code == 422  # 判定済みのドメインだけ
    decide(client, a["id"], 1)
    assert client.post(f"{MAT}/references", headers=OWNER, json={"domain": "sales"}).status_code == 403
    r = client.post(f"{MAT}/references", headers=CHAIR, json={"domain": "sales", "reason": "先行ドメイン"})
    assert r.status_code == 201 and r.json()["level"] == 1 and r.json()["designated_by"] == "chair"
    assert [x["domain"] for x in client.get(f"{MAT}/references", headers=U("anyone")).json()] == ["sales"]
    assert client.delete(f"{MAT}/references/sales", headers=CHAIR).status_code == 204
    assert client.delete(f"{MAT}/references/sales", headers=CHAIR).status_code == 404
    assert client.get(f"{MAT}/references", headers=U("anyone")).json() == []
