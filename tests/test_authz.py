"""Phase 7 (§4 / §11): 認可 — RBAC / 資産ごとの Owner・Steward 判定 / ABAC / 拒否の証跡."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from edgf import authz
from edgf.db import engine
from tests.test_auth import bearer, jwks, jwt_client, keys, token  # noqa: F401  (fixtures)

API = "/api/v1"
COMMITTEE = {"X-Actor": "chair", "X-Roles": "committee"}
ADMIN = {"X-Actor": "root", "X-Roles": "admin"}


def U(actor: str, roles: str = "", domains: str = "", clearance: str | None = None) -> dict:
    h = {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}
    if clearance:
        h["X-Clearance"] = clearance
    return h


CONSUMER = U("analyst")  # ロール無し = consumer (clearance internal)


def future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture()
def world(client):
    """sales / hr ドメインと、機密度の異なる 3 資産 (Owner=o1, Steward=s1, Custodian=c1)。"""
    for d in ("sales", "hr"):
        assert client.post(f"{API}/domains", headers=COMMITTEE, json={"name": d}).status_code == 201
    base = {"domain": "sales", "data_class": "transactional", "owner": "o1", "steward": "s1", "custodian": "c1",
            "description": "d", "controls": ["encryption_at_rest", "encryption_in_transit", "masking"]}
    ids = {}
    for qn, sens in (("sales.payroll", "restricted"), ("sales.orders", "confidential"), ("sales.kpi", "internal")):
        r = client.post(f"{API}/assets", headers=ADMIN, json={**base, "qualified_name": qn, "name": qn, "sensitivity": sens})
        assert r.status_code == 201, r.text
        ids[qn] = r.json()["id"]
    return ids


def denied_events(client, action: str | None = None) -> list[dict]:
    evs = client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()
    return [e for e in evs if action is None or e["payload"]["action"] == action]


def assert_403(client, r, action: str, actor: str) -> None:
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["type"] == "urn:edgf:problem:forbidden" and body["action"] == action
    ev = denied_events(client, action)[0]  # 最新の拒否イベント
    assert ev["actor"] == f"dev:{actor}" and ev["request_id"] == r.headers["x-request-id"]


# ---------------------------------------------------------------- RBAC: ロール別の 403 と証跡
@pytest.mark.parametrize("who, method, path, body, action", [
    (CONSUMER, "post", "/domains", {"name": "x"}, "domain.create"),
    (CONSUMER, "post", "/assets", {"qualified_name": "sales.x", "name": "x", "domain": "sales",
                                   "data_class": "master", "sensitivity": "internal"}, "asset.register"),
    # ドメインロールは所属ドメインの資産にしか効かない (§15 Domain Ownership)
    (U("hr-st", "steward", "hr"), "post", "/assets", {"qualified_name": "sales.y", "name": "y", "domain": "sales",
                                                      "data_class": "master", "sensitivity": "internal"},
     "asset.register"),
    (U("st", "steward", "sales"), "post", "/policies/reload", None, "policy.reload"),
    (U("own", "owner", "sales"), "post", "/policies/exceptions/none/decision", {"approve": True}, None),
    (CONSUMER, "get", "/audit/events", None, "audit.read"),
    (CONSUMER, "post", "/lineage/edges", {"source": "a.b", "target": "c.d"}, "lineage.write"),
    (CONSUMER, "post", "/glossary/terms", {"name": "t", "definition": "d"}, "glossary.write"),
    (CONSUMER, "post", "/issues/escalate-overdue", None, "issue.escalate_overdue"),
    (U("st", "steward", "*"), "post", "/policies/evaluate", None, "policy.evaluate_all"),
])
def test_role_denials_are_403_and_audited(client, world, who, method, path, body, action):
    r = getattr(client, method)(f"{API}{path}", headers=who, **({"json": body} if body is not None else {}))
    if action is None:  # 存在しない例外: 404 (権限判定より先に対象の存在を確認する)
        assert r.status_code == 404
        return
    assert_403(client, r, action, who["X-Actor"])


@pytest.mark.parametrize("who, method, path_tpl, body, action", [
    # 所有権の変更・承認系は Committee/admin。Custodian (IT) には承認権限が無い (§4 Business Ownership)
    (U("c1", "custodian", "*"), "put", "/assets/{orders}/ownership", {"owner": "c1"}, "asset.ownership_change"),
    (U("c1", "custodian", "*"), "post", "/assets/{orders}/certification", {"certification": "certified"},
     "asset.certify"),
    (U("c1", "custodian", "*"), "post", "/assets/{orders}/legal-hold", {"legal_hold": True, "reason": "x"},
     "asset.legal_hold"),
    (U("c1", "custodian", "*"), "post", "/assets/{orders}/access-grants",
     {"subject": "x", "purpose": "p", "expires_at": "__future__"}, "asset.access_grant"),
    # Certification はその資産の Owner/Steward だけ。owner ロールや admin でも他人の資産は不可
    (U("other-owner", "owner", "sales"), "post", "/assets/{orders}/certification", {"certification": "certified"},
     "asset.certify"),
    (ADMIN, "post", "/assets/{orders}/certification", {"certification": "certified"}, "asset.certify"),
    # 削除指示と Legal Hold は資産の Owner か Committee
    (U("s1", "steward", "sales"), "post", "/assets/{orders}/lifecycle", {"target_state": "deleted"}, "asset.delete"),
    (ADMIN, "post", "/assets/{orders}/legal-hold", {"legal_hold": True, "reason": "x"}, "asset.legal_hold"),
    # 品質ルールは資産の Steward/Owner (またはドメイン内の steward/owner ロール)
    (U("hr-st", "steward", "hr", "confidential"), "post", "/quality/assets/{orders}/rules",
     {"name": "r", "dimension": "completeness", "rule_type": "not_null", "column": "a"}, "quality.rule_write"),
    (U("st2", "steward", "sales"), "patch", "/assets/{orders}", {"domain": "hr"}, "asset.update"),  # 移管先の権限なし
    (U("c2", "custodian", "*"), "post", "/assets/{orders}/access-grants",
     {"subject": "x", "purpose": "p", "expires_at": "__future__"}, "asset.access_grant"),
    (COMMITTEE, "post", "/assets/{orders}/access-grants",  # 閲覧承認は Owner のみ (§4.2 アクセス承認 A/R)
     {"subject": "x", "purpose": "p", "expires_at": "__future__"}, "asset.access_grant"),
])
def test_asset_level_denials(client, world, who, method, path_tpl, body, action):
    path = path_tpl.format(orders=world["sales.orders"])
    if body and body.get("expires_at") == "__future__":
        body = {**body, "expires_at": future()}
    r = getattr(client, method)(f"{API}{path}", headers=who, json=body)
    assert_403(client, r, action, who["X-Actor"])


def test_denial_rolls_back_but_keeps_evidence(client, world):
    before = len(client.get(f"{API}/domains", headers=CONSUMER).json())
    r = client.post(f"{API}/domains", headers=CONSUMER, json={"name": "shadow"})
    assert r.status_code == 403
    assert len(client.get(f"{API}/domains", headers=CONSUMER).json()) == before
    ev = denied_events(client, "domain.create")[0]
    assert ev["entity_id"] == "shadow" and ev["payload"]["roles"] == ["consumer"]
    assert client.get(f"{API}/audit/verify", headers=CONSUMER).json()["valid"]


def test_separation_of_duties_denial_is_audited(client, world):
    ex = client.post(f"{API}/policies/exceptions", headers=U("o1"), json={
        "policy_id": "LIN-001", "asset_id": world["sales.kpi"], "reason": "移行中", "expires_at": future()}).json()
    r = client.post(f"{API}/policies/exceptions/{ex['id']}/decision", headers=U("o1", "committee"), json={"approve": True})
    assert_403(client, r, "exception.decide", "o1")
    assert client.post(f"{API}/policies/exceptions/{ex['id']}/decision", headers=COMMITTEE,
                       json={"approve": True}).json()["status"] == "approved"


# ---------------------------------------------------------------- 許可されるべき操作
def test_business_roles_can_do_their_jobs(client, world):
    orders = world["sales.orders"]
    st = U("sales-st", "steward", "sales")
    assert client.post(f"{API}/assets", headers=st, json={"qualified_name": "sales.new", "name": "n", "domain": "sales",
                                                          "data_class": "master", "sensitivity": "internal"}).status_code == 201
    # 資産の Steward (subject 一致) はロール無しでも品質ルールを管理できる
    assert client.post(f"{API}/quality/assets/{orders}/rules", headers=U("s1"), json={
        "name": "r", "dimension": "completeness", "rule_type": "not_null", "column": "a"}).status_code == 201
    # Custodian はパイプラインの技術的な操作 (更新通知・品質検査・リネージ) を行える
    assert client.post(f"{API}/assets/{orders}/refreshed", headers=U("c1")).status_code == 200
    assert client.post(f"{API}/quality/assets/{orders}/runs", headers=U("c1"), json={"records": [{"a": 1}]}).status_code == 201
    assert client.post(f"{API}/lineage/edges", headers=U("etl", "custodian"),
                       json={"source": "sales.orders", "target": "sales.kpi"}).status_code == 201
    # 資産の Owner は削除指示と Legal Hold を出せる。Committee も可
    assert client.post(f"{API}/assets/{orders}/legal-hold", headers=U("o1"),
                       json={"legal_hold": True, "reason": "訴訟"}).status_code == 200
    assert client.post(f"{API}/assets/{orders}/legal-hold", headers=COMMITTEE,
                       json={"legal_hold": False, "reason": "解除"}).status_code == 200
    assert client.post(f"{API}/assets/{orders}/lifecycle", headers=U("o1"),
                       json={"target_state": "deleted"}).status_code == 200
    actions = [e["action"] for e in client.get(f"{API}/audit/events", headers=COMMITTEE,
                                               params={"entity_id": orders}).json()]
    assert "asset.refreshed" in actions and "lifecycle.delete_requested" in actions


def test_issue_workflow_permissions(client, world):
    i = client.post(f"{API}/issues", headers=CONSUMER, json={"asset_id": world["sales.kpi"], "title": "値が不正"})
    assert i.status_code == 201  # Consumer も品質問題を報告できる (§4.1)
    iid = i.json()["id"]
    assert i.json()["assignee"] == "s1"
    r = client.patch(f"{API}/issues/{iid}", headers=U("bystander"), json={"status": "resolved"})
    assert_403(client, r, "issue.update", "bystander")
    assert client.patch(f"{API}/issues/{iid}", headers=U("s1"), json={"assignee": "s2"}).status_code == 200
    assert client.post(f"{API}/issues/{iid}/escalate", headers=U("s2")).status_code == 200  # 新しい担当者
    # 閲覧できない資産の Issue は起票できない
    assert client.post(f"{API}/issues", headers=CONSUMER,
                       json={"asset_id": world["sales.payroll"], "title": "x"}).status_code == 403


# ---------------------------------------------------------------- ABAC
def _names(r) -> list[str]:
    return [a["qualified_name"] for a in r.json()]


def test_restricted_assets_hidden_from_consumers_until_owner_approves(client, world):
    payroll = world["sales.payroll"]
    r = client.get(f"{API}/assets", headers=CONSUMER)
    assert _names(r) == ["sales.kpi"] and r.headers["x-total-count"] == "1"
    assert_403(client, client.get(f"{API}/assets/{payroll}", headers=CONSUMER), "asset.read", "analyst")
    # clearance だけでは restricted は見えない
    assert "sales.payroll" not in _names(client.get(f"{API}/assets", headers=U("x", clearance="restricted")))

    g = client.post(f"{API}/assets/{payroll}/access-grants", headers=U("o1"),
                    json={"subject": "analyst", "purpose": "給与分析", "expires_at": future()})
    assert g.status_code == 201, g.text
    assert _names(client.get(f"{API}/assets", headers=CONSUMER)) == ["sales.kpi", "sales.payroll"]
    assert client.get(f"{API}/assets/{payroll}", headers=CONSUMER).status_code == 200
    assert [x["subject"] for x in client.get(f"{API}/assets/{payroll}/access-grants", headers=U("s1")).json()] == ["analyst"]

    # Committee は取り消せる (保護方向の操作)
    assert client.delete(f"{API}/assets/{payroll}/access-grants/{g.json()['id']}", headers=COMMITTEE).status_code == 200
    assert _names(client.get(f"{API}/assets", headers=CONSUMER)) == ["sales.kpi"]
    assert client.get(f"{API}/assets/{payroll}/access-grants", headers=U("o1")).json() == []
    assert len(client.get(f"{API}/assets/{payroll}/access-grants", headers=U("o1"),
                          params={"include_inactive": True}).json()) == 1


def test_expired_grant_no_longer_applies(client, world):
    payroll = world["sales.payroll"]
    client.post(f"{API}/assets/{payroll}/access-grants", headers=U("o1"),
                json={"subject": "analyst", "purpose": "p", "expires_at": future(1)})
    assert client.get(f"{API}/assets/{payroll}", headers=CONSUMER).status_code == 200
    with engine.begin() as conn:
        conn.execute(text("UPDATE access_grants SET expires_at = '2000-01-01 00:00:00'"))
    assert client.get(f"{API}/assets/{payroll}", headers=CONSUMER).status_code == 403


def test_grant_validation(client, world):
    payroll = world["sales.payroll"]
    for exp in (future(400), "2000-01-01T00:00:00Z"):
        assert client.post(f"{API}/assets/{payroll}/access-grants", headers=U("o1"),
                           json={"subject": "a", "purpose": "p", "expires_at": exp}).status_code == 422
    assert client.delete(f"{API}/assets/{payroll}/access-grants/nope", headers=U("o1")).status_code == 404


def test_clearance_and_parties(client, world):
    assert _names(client.get(f"{API}/assets", headers=U("x", "steward", "hr"))) == ["sales.kpi", "sales.orders"]
    assert _names(client.get(f"{API}/assets", headers=CONSUMER)) == ["sales.kpi"]
    for who in (COMMITTEE, ADMIN, U("o1"), U("s1"), U("c1")):  # 監督者と資産の当事者は restricted も見える
        assert "sales.payroll" in _names(client.get(f"{API}/assets", headers=who))
    assert client.get(f"{API}/assets/by-name/sales.payroll", headers=CONSUMER).status_code == 403


def test_related_lists_are_filtered(client, world):
    payroll, kpi = world["sales.payroll"], world["sales.kpi"]
    client.post(f"{API}/issues", headers=COMMITTEE, json={"asset_id": payroll, "title": "secret issue"})
    client.post(f"{API}/issues", headers=COMMITTEE, json={"asset_id": kpi, "title": "public issue"})
    assert [i["title"] for i in client.get(f"{API}/issues", headers=CONSUMER).json()] == ["public issue"]

    t = client.post(f"{API}/glossary/terms", headers=COMMITTEE | {"X-Roles": "admin"},
                    json={"name": "給与", "definition": "d"}).json()
    for aid in (payroll, kpi):
        client.put(f"{API}/glossary/terms/{t['id']}/assets/{aid}", headers=U("s1"))
    assert client.get(f"{API}/glossary/terms/{t['id']}", headers=CONSUMER).json()["asset_ids"] == [kpi]

    client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": "sales.kpi", "target": "sales.payroll"})
    imp = client.get(f"{API}/lineage/sales.kpi/impact", headers=CONSUMER).json()
    assert imp["impacted"] == [{"qualified_name": "redacted-1", "depth": 1, "registered": True, "redacted": True}]
    assert imp["owners_to_notify"] == ["o1"]  # 変更管理の通知先は伏せない
    assert client.get(f"{API}/lineage/sales.kpi/impact", headers=COMMITTEE).json()["impacted"][0]["owner"] == "o1"

    client.post(f"{API}/policies/exceptions", headers=U("o1"), json={
        "policy_id": "LIN-001", "asset_id": payroll, "reason": "r", "expires_at": future()})
    assert client.get(f"{API}/policies/exceptions", headers=CONSUMER).json() == []
    assert len(client.get(f"{API}/policies/exceptions", headers=COMMITTEE).json()) == 1


# ---------------------------------------------------------------- JWT の subject による Owner/Steward 判定
def test_ownership_matches_jwt_subject(jwt_client, keys):  # noqa: F811
    committee = bearer(token(keys, sub="u-chair", roles=["committee"]))
    jwt_client.post(f"{API}/domains", headers=committee, json={"name": "sales"})
    admin = bearer(token(keys, sub="u-root", roles=["admin"]))
    aid = jwt_client.post(f"{API}/assets", headers=admin, json={
        "qualified_name": "sales.x", "name": "x", "domain": "sales", "data_class": "master",
        "sensitivity": "restricted", "owner": "u-owner", "steward": "u-steward"}).json()["id"]
    owner = bearer(token(keys, sub="u-owner", roles=[]))  # ロール無しでも subject 一致で Owner
    assert jwt_client.get(f"{API}/assets/{aid}", headers=owner).status_code == 200
    r = jwt_client.post(f"{API}/assets/{aid}/certification", headers=owner, json={"certification": "certified"})
    assert r.status_code == 409 and r.json()["type"] == "urn:edgf:problem:certification-blocked"  # 認可は通過
    other = bearer(token(keys, sub="u-mallory", roles=["owner"], domains=["sales"], clearance="restricted"))
    assert jwt_client.get(f"{API}/assets/{aid}", headers=other).status_code == 403
    r = jwt_client.post(f"{API}/assets/{aid}/legal-hold", headers=other, json={"legal_hold": True, "reason": "x"})
    assert r.status_code == 403
    ev = jwt_client.get(f"{API}/audit/events", headers=committee, params={"action": "authz.denied"}).json()
    assert {e["actor"] for e in ev} == {"u-mallory"} and {e["payload"]["auth_mode"] for e in ev} == {"jwt"}


# ---------------------------------------------------------------- 権限表の整合性
def test_custodian_never_has_approval_authority():
    for action, r in authz.PERMISSIONS.items():
        if r.approval:
            assert "custodian" not in r.roles | r.domain_roles | r.relations, action
    with pytest.raises(ValueError, match="Custodian"):
        authz.validate_permissions({"x": authz.rule(roles=("custodian",), approval=True)})
    with pytest.raises(ValueError, match="Custodian"):
        authz.validate_permissions({"x": authz.rule(relations=("custodian",), approval=True)})
    with pytest.raises(ValueError, match="全員"):
        authz.validate_permissions({"x": authz.rule(authenticated=True, approval=True)})
    with pytest.raises(ValueError, match="unknown roles"):
        authz.validate_permissions({"x": authz.rule(roles=("superuser",))})
    with pytest.raises(ValueError, match="unknown relations"):
        authz.validate_permissions({"x": authz.rule(relations=("friend",))})


def test_permission_matrix_is_exported():
    m = authz.permission_matrix()
    assert m["asset.certify"] == {"roles": [], "domain_roles": [], "relations": ["owner", "steward"],
                                  "approval": True, "authenticated": False,
                                  "description": "Certification の変更"}


def test_rule_delete_and_glossary_unlink_are_authorized(client, world):
    orders = world["sales.orders"]
    rule = client.post(f"{API}/quality/assets/{orders}/rules", headers=U("s1"), json={
        "name": "r", "dimension": "completeness", "rule_type": "not_null", "column": "a"}).json()
    assert_403(client, client.delete(f"{API}/quality/rules/{rule['id']}", headers=U("x", "steward", "hr")),
               "quality.rule_write", "x")
    assert client.delete(f"{API}/quality/rules/{rule['id']}", headers=U("o1")).status_code == 204
    t = client.post(f"{API}/glossary/terms", headers=U("s1", "steward"), json={"name": "受注", "definition": "d"}).json()
    client.put(f"{API}/glossary/terms/{t['id']}/assets/{orders}", headers=U("s1"))
    assert_403(client, client.delete(f"{API}/glossary/terms/{t['id']}/assets/{orders}",
                                     headers=U("y", clearance="confidential")), "glossary.link", "y")
    assert client.delete(f"{API}/glossary/terms/{t['id']}/assets/{orders}", headers=U("o1")).json()["asset_ids"] == []


def test_readme_permission_table_lists_every_action():
    """権限表と README の食い違いを検出する。"""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    missing = [action for action in authz.PERMISSIONS if f"| `{action}` |" not in readme]
    assert missing == []
