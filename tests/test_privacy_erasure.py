"""Phase 8c (§12 Right to Erasure / §10 Delete): 消去要求のワークフロー."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from edgf.db import engine
from edgf.services.subject_ref import to_subject_ref
from tests.conftest import dump_database

API = "/api/v1/privacy"
CAT = "/api/v1"
RAW = "Erasure.Raw.Person@Example.com"
HASH = "ab" * 32


def U(actor: str, roles: str = "", domains: str = "") -> dict:
    return {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}


COMMITTEE, ADMIN = U("chair", "committee"), U("root", "admin")
COMMITTEE2 = U("vice-chair", "committee")


def _asset(client, qn, *, owner, custodian, pii=True, identifier=False, sensitivity="confidential", **kw):
    col = {"name": "email", "pii": pii}
    if identifier:
        col["identifier_type"] = "email"
    r = client.post(f"{CAT}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "data_class": "master", "sensitivity": sensitivity, "owner": owner,
        "steward": "st", "custodian": custodian, "schema": [col], **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _edge(client, src, dst):
    assert client.post(f"{CAT}/lineage/edges", headers=ADMIN, json={"source": src, "target": dst}).status_code == 201


@pytest.fixture()
def graph(client):
    """起点 → 下流 1 段 → 下流 2 段 (Legal Hold)。非 PII の中間資産と未登録の下流も含む。"""
    ids = {
        "src": _asset(client, "crm.customers", owner="o1", custodian="c1", identifier=True),
        "view": _asset(client, "crm.view", owner="o2", custodian="c2"),
        "held": _asset(client, "bi.mart", owner="o3", custodian="c3"),
        "mid": _asset(client, "bi.agg", owner="o4", custodian="c4", pii=False),
        "leaf": _asset(client, "bi.leaf", owner="o2", custodian="c2"),
        "other": _asset(client, "hr.unrelated", owner="o9", custodian="c9"),
    }
    _edge(client, "crm.customers", "crm.view")
    _edge(client, "crm.view", "bi.mart")
    _edge(client, "crm.customers", "bi.agg")
    _edge(client, "bi.agg", "bi.leaf")
    _edge(client, "crm.view", "ext.unregistered_feed")
    assert client.post(f"{CAT}/assets/{ids['held']}/legal-hold", headers=COMMITTEE,
                       json={"legal_hold": True, "reason": "訴訟対応"}).status_code == 200
    return ids


def receive(client, who=ADMIN, identifier=RAW, **kw):
    r = client.post(f"{API}/erasure-requests", headers=who, json={"identifier_type": "email", "identifier": identifier, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def step(client, rid, action, who=COMMITTEE, code=200, **kw):
    r = client.post(f"{API}/erasure-requests/{rid}/{action}", headers=who, **kw)
    assert r.status_code == code, r.text
    return r.json()


def to_dispatched(client, who=COMMITTEE):
    req = receive(client)
    rid = req["id"]
    step(client, rid, "scope")
    step(client, rid, "approve", who=who)
    return step(client, rid, "dispatch")


def tasks_for(client, who, **params):
    r = client.get(f"{API}/erasure-tasks", headers=who, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def answer(client, custodian, task_id, **body):
    assert client.post(f"{API}/erasure-tasks/{task_id}/ack", headers=U(custodian)).status_code == 200
    r = client.post(f"{API}/erasure-tasks/{task_id}/complete", headers=U(custodian),
                    json={"records_deleted": 3, "evidence_hash": HASH, **body})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- E2E (受け入れ基準)
def test_e2e_erasure_to_certificate(client, graph, edgf_logs):
    req = receive(client, reason="TICKET-1234")
    rid = req["id"]
    assert req["status"] == "received" and req["subject_ref"] == to_subject_ref("email", RAW)
    assert "identifier" not in req and req["tasks"] == []

    # 対象の特定: 起点 → 下流 2 段。非 PII の中間を経由した下流も含み、非 PII と無関係な資産は含まない
    scoped = step(client, rid, "scope")
    assert scoped["status"] == "scoped"
    by_name = {t["qualified_name"]: t for t in scoped["tasks"]}
    assert set(by_name) == {"crm.customers", "crm.view", "bi.mart", "bi.leaf"}
    assert (by_name["crm.customers"]["origin"], by_name["crm.customers"]["depth"]) == ("identifier_match", 0)
    assert (by_name["crm.view"]["origin"], by_name["crm.view"]["depth"]) == ("lineage", 1)
    assert (by_name["bi.mart"]["origin"], by_name["bi.mart"]["depth"]) == ("lineage", 2)
    assert by_name["crm.customers"]["columns"] == ["email"]
    # Legal Hold は withheld (§10.2)、未登録の下流は警告
    assert by_name["bi.mart"]["status"] == "withheld" and "Legal Hold" in by_name["bi.mart"]["withheld_reason"]
    assert scoped["unregistered_downstream"] == ["ext.unregistered_feed"]
    assert any("未登録" in w for w in scoped["warnings"])

    assert tasks_for(client, U("c1")) == []  # 発行前は Data Plane から見えない
    assert step(client, rid, "approve")["status"] == "approved"
    assert step(client, rid, "dispatch")["status"] == "dispatched"

    # Custodian が pull して応答する
    pending = tasks_for(client, U("c1"), status="pending")
    assert [t["qualified_name"] for t in pending] == ["crm.customers"]
    assert pending[0]["subject_ref"] == req["subject_ref"] and pending[0]["identifier_type"] == "email"
    assert sorted(t["qualified_name"] for t in tasks_for(client, U("c2"), status="pending")) == ["bi.leaf", "crm.view"]
    assert [t["status"] for t in tasks_for(client, U("c3"))] == ["withheld"]
    assert tasks_for(client, U("c3"), status="pending") == []

    assert answer(client, "c1", by_name["crm.customers"]["id"])["status"] == "completed"
    assert answer(client, "c2", by_name["crm.view"]["id"], records_deleted=7)["records_deleted"] == 7
    assert client.get(f"{API}/erasure-requests/{rid}", headers=COMMITTEE).json()["status"] == "dispatched"
    answer(client, "c2", by_name["bi.leaf"]["id"])

    # withheld があるので partially_completed
    done = client.get(f"{API}/erasure-requests/{rid}", headers=COMMITTEE).json()
    assert done["status"] == "partially_completed" and done["finished_at"]

    # 確認 → 消去証明 → クローズ
    assert step(client, rid, "verify")["status"] == "verified"
    r = client.get(f"{API}/erasure-requests/{rid}/certificate", headers=ADMIN)
    assert r.status_code == 200
    cert = r.json()
    assert cert["erasure_request_id"] == rid and cert["status"] == "verified" and cert["verified_by"] == "chair"
    assert cert["request_id"] == r.headers["x-request-id"] and len(cert["certificate_hash"]) == 64
    results = {t["qualified_name"]: t for t in cert["tasks"]}
    assert results["crm.view"]["records_deleted"] == 7 and results["crm.view"]["evidence_hash"] == HASH
    assert results["crm.view"]["request_id"] and results["crm.view"]["completed_at"]
    assert results["bi.mart"]["status"] == "withheld" and results["bi.mart"]["completed_at"] is None
    assert step(client, rid, "close")["status"] == "closed"
    assert client.get(f"{API}/erasure-requests/{rid}/certificate", headers=COMMITTEE).json()["status"] == "closed"

    # 生の識別子はどこにも残らない
    needle = RAW.lower()
    assert needle not in dump_database() and needle not in edgf_logs.text() and needle not in r.text.lower()
    # 監査の証跡
    actions = [e["action"] for e in client.get(f"{CAT}/audit/events", headers=COMMITTEE, params={"entity_id": rid}).json()]
    for a in ("erasure.receive", "erasure.scope", "erasure.approve", "erasure.dispatch", "erasure.verify",
              "erasure.certificate", "erasure.close"):
        assert a in actions
    assert client.get(f"{CAT}/audit/verify", headers=COMMITTEE).json()["valid"]
    with engine.connect() as conn:
        payloads = " ".join(str(p[0]) for p in conn.execute(text("SELECT payload FROM audit_events")))
    assert req["subject_ref"] not in payloads


def test_clean_completion_without_withheld(client):
    aid = _asset(client, "crm.only", owner="o1", custodian="c1", identifier=True)
    req = to_dispatched(client)
    t = tasks_for(client, U("c1"), status="pending")[0]
    assert t["asset_id"] == aid and t["request_id"] == req["id"]
    answer(client, "c1", t["id"])
    assert client.get(f"{API}/erasure-requests/{req['id']}", headers=COMMITTEE).json()["status"] == "completed"


# ---------------------------------------------------------------- Custodian だけが応答できる
@pytest.mark.parametrize("who", [U("c2", "custodian", "*"), ADMIN, COMMITTEE, U("o1", "owner", "*"), U("x")])
def test_only_target_custodian_can_respond(client, graph, who):
    to_dispatched(client)
    t = tasks_for(client, U("c1"), status="pending")[0]
    for path, body, action in (("ack", None, "privacy.erasure.task_ack"),
                               ("complete", {"evidence_hash": HASH}, "privacy.erasure.task_complete")):
        r = client.post(f"{API}/erasure-tasks/{t['id']}/{path}", headers=who, json=body)
        assert r.status_code == 403 and r.json()["action"] == action
    ev = client.get(f"{CAT}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()[0]
    assert ev["payload"]["action"] == "privacy.erasure.task_complete" and ev["entity_id"] == t["id"]
    assert tasks_for(client, U("c1"), status="pending")[0]["id"] == t["id"]  # 状態は変わらない


def test_task_state_rules_and_validation(client, graph):
    to_dispatched(client)
    t = tasks_for(client, U("c1"), status="pending")[0]
    complete = lambda **b: client.post(f"{API}/erasure-tasks/{t['id']}/complete", headers=U("c1"), json=b)  # noqa: E731
    assert complete(evidence_hash=HASH).status_code == 409  # ack が先
    assert client.post(f"{API}/erasure-tasks/{t['id']}/ack", headers=U("c1")).status_code == 200
    assert client.post(f"{API}/erasure-tasks/{t['id']}/ack", headers=U("c1")).status_code == 409
    assert complete().status_code == 422  # 証跡は必須
    assert complete(evidence_hash="not-a-hash").status_code == 422
    assert complete(evidence_hash=HASH, records_deleted=-1).status_code == 422
    r = complete(evidence_hash=HASH, note="連絡先 raw@example.com")  # note の PII は監査ガードで拒否
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:audit-payload-rejected"
    assert "raw@example.com" not in dump_database()
    assert complete(evidence_hash=HASH).status_code == 200
    assert complete(evidence_hash=HASH).status_code == 409
    assert client.post(f"{API}/erasure-tasks/nope/ack", headers=U("c1")).status_code == 404


def test_failed_task_leads_to_partially_completed(client):
    _asset(client, "crm.only", owner="o1", custodian="c1", identifier=True)
    req = to_dispatched(client)
    t = tasks_for(client, U("c1"))[0]
    client.post(f"{API}/erasure-tasks/{t['id']}/ack", headers=U("c1"))
    r = client.post(f"{API}/erasure-tasks/{t['id']}/complete", headers=U("c1"), json={"outcome": "failed", "note": "storage error"})
    assert r.json()["status"] == "failed"
    assert client.get(f"{API}/erasure-requests/{req['id']}", headers=COMMITTEE).json()["status"] == "partially_completed"


# ---------------------------------------------------------------- Legal Hold の優先 (§10.2)
def test_legal_hold_set_after_scope_or_dispatch_withholds(client):
    a = _asset(client, "crm.a", owner="o1", custodian="c1", identifier=True)
    b = _asset(client, "crm.b", owner="o2", custodian="c2", identifier=True)
    req = receive(client)
    rid = req["id"]
    step(client, rid, "scope")
    step(client, rid, "approve")
    hold = {"legal_hold": True, "reason": "調査"}
    client.post(f"{CAT}/assets/{a}/legal-hold", headers=COMMITTEE, json=hold)  # スコープ後の Hold
    dispatched = step(client, rid, "dispatch")
    assert {t["qualified_name"]: t["status"] for t in dispatched["tasks"]} == {"crm.a": "withheld", "crm.b": "pending"}
    client.post(f"{CAT}/assets/{b}/legal-hold", headers=COMMITTEE, json=hold)  # 発行後の Hold
    t = tasks_for(client, U("c2"), status="pending")[0]
    r = client.post(f"{API}/erasure-tasks/{t['id']}/ack", headers=U("c2"))
    assert r.json()["status"] == "withheld"
    assert client.get(f"{API}/erasure-requests/{rid}", headers=COMMITTEE).json()["status"] == "partially_completed"


def test_all_withheld_finishes_on_dispatch(client):
    a = _asset(client, "crm.a", owner="o1", custodian="c1", identifier=True)
    client.post(f"{CAT}/assets/{a}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "x"})
    assert to_dispatched(client)["status"] == "partially_completed"


def test_completion_reported_after_hold_is_recorded_with_warning(client):
    a = _asset(client, "crm.a", owner="o1", custodian="c1", identifier=True)
    req = to_dispatched(client)
    t = tasks_for(client, U("c1"))[0]
    client.post(f"{API}/erasure-tasks/{t['id']}/ack", headers=U("c1"))
    client.post(f"{CAT}/assets/{a}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "x"})
    client.post(f"{API}/erasure-tasks/{t['id']}/complete", headers=U("c1"), json={"evidence_hash": HASH})
    detail = client.get(f"{API}/erasure-requests/{req['id']}", headers=COMMITTEE).json()
    assert detail["status"] == "completed" and any("Legal Hold" in w for w in detail["warnings"])


# ---------------------------------------------------------------- 承認・却下
def test_owner_approves_only_own_assets(client, graph):
    rid = receive(client)["id"]
    step(client, rid, "scope")
    for who in (U("o9", "owner", "*"), U("c1", "custodian", "*"), ADMIN):
        assert step(client, rid, "approve", who=who, code=403)["action"] == "privacy.erasure.approve"
    r = step(client, rid, "approve", who=U("o1"))
    assert r["status"] == "scoped"
    assert {t["qualified_name"]: t["approved_by"] for t in r["tasks"] if t["approved_by"]} == {"crm.customers": "o1"}
    step(client, rid, "approve", who=U("o2"))
    # bi.mart は withheld なので承認不要。残りが揃うと approved
    assert client.get(f"{API}/erasure-requests/{rid}", headers=COMMITTEE).json()["status"] == "approved"


def test_requester_cannot_approve_and_executor_cannot_verify(client):
    _asset(client, "crm.only", owner="o1", custodian="c1", identifier=True)
    rid = receive(client, who=COMMITTEE)["id"]
    step(client, rid, "scope")
    assert "職務分離" in step(client, rid, "approve", code=403)["detail"]
    step(client, rid, "approve", who=COMMITTEE2)
    step(client, rid, "dispatch")
    t = tasks_for(client, U("c1"))[0]
    answer(client, "c1", t["id"])
    assert "職務分離" in step(client, rid, "verify", who=U("c1", "committee"), code=403)["detail"]
    step(client, rid, "verify")


def test_reject_is_committee_only_with_reason(client, graph):
    rid = receive(client)["id"]
    step(client, rid, "scope")
    assert step(client, rid, "reject", who=ADMIN, code=403, json={"reason": "x"})["action"] == "privacy.erasure.reject"
    assert step(client, rid, "reject", who=U("o1"), code=403, json={"reason": "x"})["action"] == "privacy.erasure.reject"
    step(client, rid, "reject", code=422, json={"reason": ""})
    step(client, rid, "reject", code=422, json={})
    r = step(client, rid, "reject", json={"reason": "税法上の保持義務"})
    assert r["status"] == "rejected" and r["decision_reason"] == "税法上の保持義務" and r["decided_by"] == "chair"
    assert {t["status"] for t in r["tasks"]} == {"cancelled"}
    assert tasks_for(client, U("c1")) == []
    step(client, rid, "dispatch", code=409)
    assert client.get(f"{API}/erasure-requests/{rid}/certificate", headers=COMMITTEE).status_code == 409
    # 却下後は同じ主体の新しい要求を受け付ける
    receive(client)


def test_cannot_reject_after_dispatch_and_state_order(client, graph):
    rid = receive(client)["id"]
    step(client, rid, "approve", code=409)
    step(client, rid, "dispatch", code=409)
    step(client, rid, "verify", code=409)
    step(client, rid, "close", code=409)
    assert client.get(f"{API}/erasure-requests/{rid}/certificate", headers=COMMITTEE).status_code == 409
    step(client, rid, "scope")
    step(client, rid, "scope")  # 承認前は再スコープできる
    step(client, rid, "approve")
    step(client, rid, "scope", code=409)
    step(client, rid, "dispatch")
    step(client, rid, "reject", code=409, json={"reason": "遅い"})


def test_rescope_picks_up_changes(client, graph):
    rid = receive(client)["id"]
    assert len(step(client, rid, "scope")["tasks"]) == 4
    client.post(f"{CAT}/assets/{graph['held']}/legal-hold", headers=COMMITTEE, json={"legal_hold": False, "reason": "解除"})
    _edge(client, "crm.customers", "hr.unrelated")
    tasks = {t["qualified_name"]: t["status"] for t in step(client, rid, "scope")["tasks"]}
    assert len(tasks) == 5 and tasks["bi.mart"] == "planned" and tasks["hr.unrelated"] == "planned"


def test_duplicate_open_request_and_no_targets(client):
    req = receive(client)
    r = client.post(f"{API}/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": RAW.upper()})
    assert r.status_code == 409  # 正規化後に同じ主体
    scoped = step(client, req["id"], "scope")
    assert scoped["tasks"] == [] and any("見つかりません" in w for w in scoped["warnings"])
    step(client, req["id"], "approve")
    assert step(client, req["id"], "dispatch")["status"] == "completed"


def test_missing_custodian_is_warned_and_deleted_assets_skipped(client):
    _asset(client, "crm.nocust", owner="o1", custodian=None, identifier=True)
    gone = _asset(client, "crm.gone", owner="o1", custodian="c1", identifier=True)
    client.post(f"{CAT}/assets/{gone}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"})
    r = step(client, receive(client)["id"], "scope")
    assert [t["qualified_name"] for t in r["tasks"]] == ["crm.nocust"]
    assert any("Custodian" in w and "crm.nocust" in w for w in r["warnings"])


# ---------------------------------------------------------------- 認可
def test_request_permissions_and_visibility(client, graph):
    for who in (U("o1", "owner", "*"), U("st", "steward", "*"), U("c1", "custodian", "*"), U("x")):
        r = client.post(f"{API}/erasure-requests", headers=who, json={"identifier_type": "email", "identifier": RAW})
        assert r.status_code == 403 and r.json()["action"] == "privacy.erasure.request"
    rid = receive(client)["id"]
    assert step(client, rid, "scope", who=U("o1", "owner", "*"), code=403)["action"] == "privacy.erasure.request"
    step(client, rid, "scope")
    # Owner は自分の資産が対象の要求だけを見られる
    assert [r["id"] for r in client.get(f"{API}/erasure-requests", headers=U("o1")).json()] == [rid]
    assert client.get(f"{API}/erasure-requests", headers=U("o9")).json() == []
    assert client.get(f"{API}/erasure-requests", headers=U("x")).json() == []
    assert client.get(f"{API}/erasure-requests", headers=ADMIN, params={"status": "closed"}).json() == []
    assert client.get(f"{API}/erasure-requests", headers=ADMIN, params={"status": "scoped"}).headers["x-total-count"] == "1"
    assert client.get(f"{API}/erasure-requests/{rid}", headers=U("o9")).status_code == 403
    assert client.get(f"{API}/erasure-requests/nope", headers=ADMIN).status_code == 404
    # 証明は admin / committee のみ
    assert client.get(f"{API}/erasure-requests/{rid}/certificate", headers=U("o1")).status_code == 403
    assert len(client.get(f"{API}/erasure-tasks", headers=ADMIN).json()) == 4  # planned も見える
    assert client.get(f"{API}/erasure-tasks", headers=ADMIN, params={"limit": 1}).headers["x-total-count"] == "4"
    assert tasks_for(client, U("x")) == []


def test_owner_view_hides_tasks_on_unreadable_assets(client):
    _asset(client, "crm.a", owner="o1", custodian="c1", identifier=True)
    _asset(client, "crm.secret", owner="o2", custodian="c2", identifier=True, sensitivity="restricted")
    rid = receive(client)["id"]
    step(client, rid, "scope")
    r = client.get(f"{API}/erasure-requests/{rid}", headers=U("o1")).json()
    assert [t["qualified_name"] for t in r["tasks"]] == ["crm.a"] and r["hidden_task_count"] == 1
    assert client.get(f"{API}/erasure-requests/{rid}", headers=COMMITTEE).json()["hidden_task_count"] == 0


# ---------------------------------------------------------------- 期限超過
def _expire(rid: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE erasure_requests SET due_at = '2000-01-01 00:00:00' WHERE id = :i"), {"i": rid})


def test_overdue_opens_issues_and_escalates(client, graph):
    rid = to_dispatched(client)["id"]
    sweep = lambda who=COMMITTEE: client.post(f"{API}/erasure-requests/escalate-overdue", headers=who)  # noqa: E731
    assert sweep().json() == {"overdue_requests": [], "issues_opened": [], "issues_escalated": []}
    assert sweep(U("o1", "owner", "*")).status_code == 403
    _expire(rid)
    r = sweep().json()
    assert r["overdue_requests"] == [rid] and len(r["issues_opened"]) == 3  # 未完了タスクの資産ごと (withheld を除く)
    issues = client.get(f"{CAT}/issues", headers=COMMITTEE, params={"kind": "privacy"}).json()
    assert {i["escalation_level"] for i in issues} == {"steward"} and all(rid in i["title"] for i in issues)
    assert to_subject_ref("email", RAW) not in str(issues)
    again = sweep().json()
    assert again["issues_opened"] == [] and again["issues_escalated"] == []  # 重複起票しない
    # Issue の期限も過ぎたらエスカレーションする
    with engine.begin() as conn:
        conn.execute(text("UPDATE issues SET due_at = '2000-01-01 00:00:00' WHERE kind = 'privacy'"))
    third = sweep().json()
    assert len(third["issues_escalated"]) == 3
    levels = {i["escalation_level"] for i in client.get(f"{CAT}/issues", headers=COMMITTEE, params={"kind": "privacy"}).json()}
    assert levels == {"owner"}


def test_overdue_without_tasks_opens_unassigned_issue(client):
    rid = receive(client)["id"]
    _expire(rid)
    r = client.post(f"{API}/erasure-requests/escalate-overdue", headers=COMMITTEE).json()
    assert len(r["issues_opened"]) == 1
    issue = client.get(f"{CAT}/issues", headers=COMMITTEE, params={"kind": "privacy"}).json()[0]
    assert issue["asset_id"] is None and rid in issue["title"]


def test_sla_days_setting(client):
    req = receive(client)
    from datetime import datetime, timedelta
    delta = datetime.fromisoformat(req["due_at"]) - datetime.fromisoformat(req["created_at"])
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30, seconds=5)
