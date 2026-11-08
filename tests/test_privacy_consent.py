"""Phase 8b (§12 Consent / §18.2): subject_ref、監査ガード、Consent Registry."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from edgf.auth import normalize_roles, resolve_clearance
from edgf.config import Settings
from edgf.db import SessionLocal, engine
from edgf.main import create_app
from edgf.services import audit
from edgf.services.subject_ref import (DEV_SUBJECT_KEY, SubjectKeyError, to_subject_ref, validate_subject_key)
from tests.conftest import dump_database

API = "/api/v1"
COMMITTEE = {"X-Actor": "chair", "X-Roles": "committee", "X-Domains": ""}
ADMIN = {"X-Actor": "root", "X-Roles": "admin", "X-Domains": ""}
COLLECTOR = {"X-Actor": "cmp-web", "X-Roles": "consent_collector", "X-Domains": ""}
RAW_EMAIL = "Hanako.Raw@Example.COM"
RAW_ID = "customer-raw-998877"


def future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture()
def registry(client):
    for d in ("sales", "hr"):
        client.post(f"{API}/domains", headers=COMMITTEE, json={"name": d})
    for pid, basis, dom in (("marketing", "consent", "sales"), ("billing", "contract", "sales"), ("global", "consent", None)):
        r = client.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={
            "id": pid, "description": f"{pid} の目的", "default_legal_basis": basis, "retention_days": 365, "domain": dom})
        assert r.status_code == 201, r.text
    return client


def consent(client, status="granted", who=COLLECTOR, identifier=RAW_EMAIL, itype="email", purpose="marketing", **kw):
    return client.post(f"{API}/privacy/consents", headers=who, json={
        "identifier_type": itype, "identifier": identifier, "purpose_id": purpose, "status": status, **kw})


def check(client, who=COLLECTOR, identifier=RAW_EMAIL, purpose="marketing"):
    r = client.post(f"{API}/privacy/consents/check", headers=who, json={
        "identifier_type": "email", "identifier": identifier, "purpose_id": purpose})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- subject_ref
def test_subject_ref_is_deterministic_hmac():
    ref = to_subject_ref("email", RAW_EMAIL)
    assert ref == to_subject_ref("email", RAW_EMAIL) and len(ref) == 64
    # 公開仕様どおりに Data Plane 側で再計算できる
    expected = hmac.new(DEV_SUBJECT_KEY.encode(), "email\x1fhanako.raw@example.com".encode(), hashlib.sha256).hexdigest()
    assert ref == expected
    assert to_subject_ref("email", "  hanako.raw@example.com ") == ref  # 正規化
    assert to_subject_ref("customer_id", "A1") != to_subject_ref("customer_id", "a1")  # email 以外は大小を区別
    assert to_subject_ref("customer_id", "A1") != to_subject_ref("account_id", "A1")  # 種別で分離
    assert to_subject_ref("customer_id", "A1", key="k" * 32) != to_subject_ref("customer_id", "A1")
    with pytest.raises(ValueError):
        to_subject_ref("email", "   ")


def _prod(**kw) -> Settings:
    base = Settings(env="production", auth_mode="jwt", oidc_issuer="https://i", oidc_audience="a",
                    oidc_jwks_url="https://i/jwks", subject_key="")
    return replace(base, **kw)


def test_production_without_subject_key_refuses_to_start():
    with pytest.raises(SubjectKeyError, match="EDGF_SUBJECT_KEY"):
        create_app(_prod())
    with pytest.raises(SubjectKeyError, match="32"):
        validate_subject_key(_prod(subject_key="short"))
    validate_subject_key(_prod(subject_key="k" * 32))
    assert create_app(_prod(subject_key="k" * 32)).state.auth_mode == "jwt"


def test_dev_uses_fixed_key_with_warning(edgf_logs):
    validate_subject_key(Settings(env="test", subject_key=""))
    assert "固定のテスト鍵" in edgf_logs.text()


def test_raw_identifiers_never_persist_or_log(registry, edgf_logs):
    registry.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": "sales.crm", "name": "crm", "domain": "sales", "data_class": "master",
        "sensitivity": "confidential", "owner": "o1",
        "schema": [{"name": "email", "identifier_type": "email", "processing_purposes": ["marketing"], "legal_basis": "consent"}]})
    responses = []
    for raw, itype in ((RAW_EMAIL, "email"), (RAW_ID, "customer_id")):
        responses.append(consent(registry, identifier=raw, itype=itype))
        responses.append(consent(registry, "withdrawn", identifier=raw, itype=itype))
        responses.append(registry.post(f"{API}/privacy/consents/check", headers=COLLECTOR, json={
            "identifier_type": itype, "identifier": raw, "purpose_id": "marketing"}))
    # 検証エラーでも入力値を応答に返さない
    responses.append(registry.post(f"{API}/privacy/consents", headers=COLLECTOR, json={
        "identifier_type": "Bad Type", "identifier": RAW_ID, "purpose_id": "marketing", "status": "granted"}))
    responses.append(registry.post(f"{API}/privacy/consents", headers=COLLECTOR, json={
        "identifier_type": "email", "identifier": RAW_EMAIL, "purpose_id": "marketing", "status": "bad"}))
    assert responses[0].status_code == 201 and responses[-1].status_code == 422
    db_dump = dump_database()
    assert to_subject_ref("email", RAW_EMAIL) in db_dump  # 走査が機能していることの確認
    for needle in (RAW_EMAIL.lower(), RAW_ID.lower()):
        assert needle not in db_dump
        assert needle not in edgf_logs.text()
        for r in responses:
            assert needle not in r.text.lower()
    # 監査ログには subject_ref も残さない (消去できないため)
    with engine.connect() as conn:
        payloads = [str(row[0]) for row in conn.execute(text("SELECT payload FROM audit_events"))]
    assert not any(to_subject_ref("email", RAW_EMAIL) in p for p in payloads)


# ---------------------------------------------------------------- 監査ガード
@pytest.mark.parametrize("payload, kind", [
    ({"email": "x"}, "forbidden_key"),
    ({"nested": [{"Subject_ID": "x"}]}, "forbidden_key"),
    ({"identifier": "abc"}, "forbidden_key"),
    ({"note": "連絡先は hanako@example.com です"}, "email"),
    ({"a": {"b": ["+818012345678"]}}, "phone"),
    ({"note": "03-1234-5678"}, "phone"),
    ({"note": "4111 1111 1111 1111"}, "card_number"),
])
def test_audit_guard_rejects_pii(client, payload, kind):
    with SessionLocal() as s:
        with pytest.raises(audit.AuditPayloadError) as e:
            audit.record(s, "system", "x.y", "t", "1", payload)
        assert e.value.extensions["kind"] == kind and e.value.status_code == 422
        s.rollback()


@pytest.mark.parametrize("payload", [
    {"owner": "hanako@example.com", "steward": "s@example.com"},  # 業務担当者はデータ主体ではない
    {"before": {"owner": "a@example.com"}, "after": {"owner": "b@example.com"}},
    {"subject": "analyst@example.com", "purpose": "p"},
    {"id": "550e8400-e29b-41d4-a716-446655440000", "ts": "2026-11-08T04:34:45", "date": "2026-11-08"},
    {"identifier_type": "email", "hash": "a" * 64, "records": 12345678901234},
    {"schema_": [{"name": "email", "pii": True}]},
])
def test_audit_guard_allows_operational_values(client, payload):
    with SessionLocal() as s:
        audit.record(s, "system", "x.y", "t", "1", payload)
        s.rollback()


def test_audit_guard_rejects_request_with_pii_in_free_text(sales_asset, client):
    r = client.post(f"{API}/assets/{sales_asset['id']}/legal-hold", json={"legal_hold": True, "reason": "taro@example.com の訴訟"})
    assert r.status_code == 422
    body = r.json()
    assert body["type"] == "urn:edgf:problem:audit-payload-rejected" and body["kind"] == "email"
    assert "taro@example.com" not in r.text
    # トランザクションごと巻き戻される
    assert client.get(f"{API}/assets/{sales_asset['id']}").json()["legal_hold"] is False


# ---------------------------------------------------------------- Purpose カタログ
def test_purpose_catalog(registry):
    r = registry.get(f"{API}/privacy/purposes", headers=COLLECTOR)
    assert [p["id"] for p in r.json()] == ["billing", "global", "marketing"] and r.headers["x-total-count"] == "3"
    assert registry.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={
        "id": "marketing", "description": "d", "default_legal_basis": "consent"}).status_code == 409
    assert registry.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={
        "id": "x", "description": "d", "default_legal_basis": "consent", "domain": "nope"}).status_code == 422
    assert registry.post(f"{API}/privacy/purposes", headers=COMMITTEE, json={
        "id": "bad id", "description": "d", "default_legal_basis": "consent"}).status_code == 422
    assert registry.post(f"{API}/privacy/purposes", headers=COLLECTOR, json={
        "id": "x", "description": "d", "default_legal_basis": "consent"}).status_code == 403
    r = registry.patch(f"{API}/privacy/purposes/billing", headers=ADMIN, json={"retention_days": 90})
    assert r.json()["retention_days"] == 90
    assert registry.get(f"{API}/privacy/purposes/nope", headers=COLLECTOR).status_code == 404
    assert registry.patch(f"{API}/privacy/purposes/billing", headers=COLLECTOR, json={"retention_days": 1}).status_code == 403
    assert registry.get(f"{API}/privacy/purposes/billing", headers=COLLECTOR).json()["retention_days"] == 90


# ---------------------------------------------------------------- Consent
def test_consent_history_is_append_only_and_latest_wins(registry):
    assert check(registry)["status"] == "none"
    r = consent(registry, source="web-form-v2")
    assert r.status_code == 201
    ref = r.json()["subject_ref"]
    assert ref == to_subject_ref("email", RAW_EMAIL) and "identifier" not in r.json()
    assert check(registry)["allowed"] is True
    assert consent(registry, "withdrawn").status_code == 201
    res = check(registry)
    assert res["allowed"] is False and res["status"] == "withdrawn" and res["consent_required"] is True
    assert consent(registry).status_code == 201  # 再取得
    assert check(registry)["allowed"] is True
    r = registry.get(f"{API}/privacy/consents", headers=COLLECTOR, params={"subject_ref": ref})
    assert [c["status"] for c in r.json()] == ["granted", "withdrawn", "granted"]
    assert r.headers["x-total-count"] == "3" and r.json()[0]["source"] == "web-form-v2"
    assert registry.get(f"{API}/privacy/consents", headers=COLLECTOR, params={"subject_ref": "bad"}).status_code == 422
    assert registry.get(f"{API}/privacy/consents", headers=COLLECTOR).status_code == 422  # 全件列挙は不可
    assert registry.get(f"{API}/privacy/consents", headers=COLLECTOR,
                        params={"subject_ref": ref, "purpose_id": "billing"}).json() == []
    ev = registry.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "consent.record"}).json()
    assert len(ev) == 3 and ev[0]["actor"] == "dev:cmp-web" and "subject_ref" not in ev[0]["payload"]


def test_check_without_consent_requirement_returns_reason(registry):
    res = check(registry, purpose="billing")
    assert res["consent_required"] is False and res["allowed"] is True and res["legal_basis"] == "contract"
    assert "contract" in res["reason"]


def test_consent_expiry(registry):
    assert consent(registry, expires_at=future(1)).status_code == 201
    assert check(registry)["allowed"] is True
    with engine.begin() as conn:
        conn.execute(text("UPDATE consents SET expires_at = '2000-01-01 00:00:00'"))
    res = check(registry)
    assert res["status"] == "expired" and res["allowed"] is False
    assert consent(registry, "expired").json()["status"] == "expired"
    assert consent(registry, expires_at="2000-01-01T00:00:00Z").status_code == 422
    assert consent(registry, "withdrawn", expires_at=future()).status_code == 422
    assert consent(registry, purpose="nope").status_code == 404
    assert registry.post(f"{API}/privacy/consents", headers=COLLECTOR, json={
        "identifier_type": "email", "identifier": "   ", "purpose_id": "marketing", "status": "granted"}).status_code == 422


def _crm_assets(client):
    base = {"domain": "sales", "data_class": "master", "sensitivity": "confidential", "owner": "o1", "steward": "s1"}
    col = {"name": "email", "pii_category": "contact", "identifier_type": "email"}
    assets = {}
    for qn, extra in (("sales.crm", {"processing_purposes": ["marketing"], "legal_basis": "consent"}),
                      ("sales.mail", {"processing_purposes": ["marketing"]}),  # 法的根拠は Purpose の既定 (consent)
                      ("sales.contract", {"processing_purposes": ["marketing"], "legal_basis": "contract"}),
                      ("sales.bill", {"processing_purposes": ["billing"], "legal_basis": "contract"})):
        r = client.post(f"{API}/assets", headers=ADMIN, json={**base, "qualified_name": qn, "name": qn,
                                                              "schema": [{**col, **extra}]})
        assert r.status_code == 201, r.text
        assets[qn] = r.json()["id"]
    return assets


def test_withdrawal_opens_deduplicated_issues_for_owner(registry):
    assets = _crm_assets(registry)
    assert consent(registry).json()["issues_opened"] == []  # 取得では起票しない
    opened = consent(registry, "withdrawn").json()["issues_opened"]
    assert len(opened) == 2
    issues = registry.get(f"{API}/issues", headers=COMMITTEE, params={"kind": "privacy"}).json()
    assert {i["asset_id"] for i in issues} == {assets["sales.crm"], assets["sales.mail"]}
    assert {i["assignee"] for i in issues} == {"o1"} and {i["escalation_level"] for i in issues} == {"owner"}
    assert RAW_EMAIL.lower() not in str(issues).lower()
    # 別の主体が撤回しても、未解決の間は重複起票しない
    assert consent(registry, "withdrawn", identifier="other@example.com").json()["issues_opened"] == []
    registry.patch(f"{API}/issues/{opened[0]}", headers=COMMITTEE, json={"status": "resolved"})
    assert len(consent(registry, "withdrawn", identifier="third@example.com").json()["issues_opened"]) == 1


# ---------------------------------------------------------------- 認可
def test_consent_authorization(registry):
    steward_sales = {"X-Actor": "st", "X-Roles": "steward", "X-Domains": "sales"}
    steward_hr = {"X-Actor": "st2", "X-Roles": "steward", "X-Domains": "hr"}
    for who in (COLLECTOR, COMMITTEE, ADMIN, steward_sales):
        assert consent(registry, who=who).status_code == 201
    assert check(registry, who=steward_sales)["allowed"] is True
    ref = to_subject_ref("email", RAW_EMAIL)
    assert len(registry.get(f"{API}/privacy/consents", headers=steward_sales, params={"subject_ref": ref}).json()) == 4
    # ドメイン外の steward、ドメインに属さない Purpose
    assert registry.get(f"{API}/privacy/consents", headers=steward_hr, params={"subject_ref": ref}).json() == []
    for r in (consent(registry, who=steward_hr), consent(registry, who=steward_sales, purpose="global")):
        assert r.status_code == 403 and r.json()["action"] == "privacy.consent.write"
    for who in ({"X-Actor": "analyst", "X-Roles": "", "X-Domains": ""},
                {"X-Actor": "c1", "X-Roles": "custodian", "X-Domains": "*"},
                {"X-Actor": "o1", "X-Roles": "owner", "X-Domains": "*"}):
        assert consent(registry, who=who).status_code == 403
        r = registry.post(f"{API}/privacy/consents/check", headers=who, json={
            "identifier_type": "email", "identifier": RAW_EMAIL, "purpose_id": "marketing"})
        assert r.status_code == 403 and r.json()["action"] == "privacy.consent.check"
        assert registry.get(f"{API}/privacy/consents", headers=who, params={"subject_ref": ref}).status_code == 403


def test_consent_collector_is_least_privilege(registry):
    assert normalize_roles("consent_collector") == {"consent_collector"}
    assert normalize_roles("consent_collector,steward") == {"consent_collector", "steward", "consumer"}
    assert resolve_clearance(None, {"consent_collector"}) == "public"
    for sens, qn in (("public", "sales.pub"), ("internal", "sales.int")):
        registry.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": qn, "name": qn, "domain": "sales",
                                                            "data_class": "master", "sensitivity": sens})
    assert [a["qualified_name"] for a in registry.get(f"{API}/assets", headers=COLLECTOR).json()] == ["sales.pub"]
    aid = registry.get(f"{API}/assets/by-name/sales.pub", headers=COLLECTOR).json()["id"]
    for method, path, body in (
        ("post", "/domains", {"name": "x"}),
        ("post", "/assets", {"qualified_name": "sales.z", "name": "z", "domain": "sales", "data_class": "master", "sensitivity": "public"}),
        ("patch", f"/assets/{aid}", {"description": "x"}),
        ("post", "/lineage/edges", {"source": "a", "target": "b"}),
        ("post", "/glossary/terms", {"name": "t", "definition": "d"}),
        ("post", f"/quality/assets/{aid}/rules", {"name": "r", "dimension": "completeness", "rule_type": "not_null", "column": "a"}),
        ("post", "/privacy/purposes", {"id": "z", "description": "d", "default_legal_basis": "consent"}),
        ("get", "/audit/events", None),
        ("get", "/privacy/inventory", None),
    ):
        r = getattr(registry, method)(f"{API}{path}", headers=COLLECTOR, **({"json": body} if body else {}))
        assert r.status_code == 403, (path, r.text)
    from edgf import authz
    with pytest.raises(ValueError, match="サービスアカウント"):
        authz.validate_permissions({"x": authz.rule(roles=("consent_collector",), approval=True)})


def test_jwt_style_client_cannot_use_app_without_auth():
    with TestClient(create_app()) as c:
        assert c.post(f"{API}/privacy/consents/check", json={}).status_code == 401
