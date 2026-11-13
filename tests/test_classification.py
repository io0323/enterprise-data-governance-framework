"""Phase 19d: メタデータからの分類の提案と確定 (区分 B。docs/adr/0001)."""
from __future__ import annotations

from datetime import timedelta

import pytest
import yaml
from sqlalchemy import select, update

from edgf import authz
from edgf.auth import Principal
from edgf.db import SessionLocal
from edgf.errors import ProblemError
from edgf.models import ApprovalRequest, ClassificationSuggestion, ColumnLineageEdge, DataAsset, utcnow
from edgf.services import approvals, autonomy_catalog, classification
from edgf.services.classification_rules import RULES_FILE, RulesInvalid, parse, tokens
from tests.autonomy_helpers import AUT, CHAIR, audits, playbook, run_now, runs, use_playbooks
from tests.conftest import dump_database
from tests.process_helpers import ADMIN, API, U, asset

STEWARD, OWNER = U("asset-steward"), U("asset-owner")


def cls_playbook(mode="active", min_confidence=0.5):
    return playbook("PB-CLS", trigger={"detector": "classification_scan"}, action="propose.classification", category="B", mode=mode,
                    undo_days=None, fp=None, condition={"min_confidence": min_confidence})


def suggestions(asset_id=None) -> list[ClassificationSuggestion]:
    with SessionLocal() as db:
        stmt = select(ClassificationSuggestion).order_by(ClassificationSuggestion.column, ClassificationSuggestion.created_at)
        if asset_id:
            stmt = stmt.where(ClassificationSuggestion.asset_id == asset_id)
        return db.scalars(stmt).all()


def suggest(asset_id) -> list[dict]:
    with SessionLocal() as db:
        return classification.suggest_for(db, db.get(DataAsset, asset_id))


def by_column(items) -> dict:
    return {s["column"] if isinstance(s, dict) else s.column: s for s in items}


# ---------------------------------------------------------------- 辞書規則の定義
def test_initial_rules_load_and_are_in_the_bundle(client):
    from pathlib import Path

    from edgf.config import settings
    rules = parse((Path(settings.policy_dir) / RULES_FILE).read_text(encoding="utf-8"))
    assert len(rules.items) >= 8
    active = client.get(f"{API}/policies/bundles", headers=ADMIN, params={"status": "active"}).json()[0]
    assert RULES_FILE in client.get(f"{API}/policies/bundles/{active['version']}", headers=ADMIN).json()["files"]


@pytest.mark.parametrize("rule,message", [
    ({"id": "X1", "patterns": ["a"], "suggest": {"pii": False}, "confidence": 0.5}, "true だけ"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"cde": False}, "confidence": 0.5}, "true だけ"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"pii": True, "pii_category": "secret"}, "confidence": 0.5}, "pii_category"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"pii_category": "contact"}, "confidence": 0.5}, "pii: true"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"sensitivity": "top"}, "confidence": 0.5}, "sensitivity"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"owner": "x"}, "confidence": 0.5}, "suggest"),
    ({"id": "X1", "patterns": [], "suggest": {"cde": True}, "confidence": 0.5}, "patterns"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"cde": True}, "confidence": 0}, "confidence"),
    ({"id": "x", "patterns": ["a"], "suggest": {"cde": True}, "confidence": 0.5}, "id"),
    ({"id": "X1", "patterns": ["a"], "suggest": {"cde": True}, "confidence": 0.5, "extra": 1}, "だけを持つ"),
])
def test_invalid_rules_are_rejected(rule, message):
    with pytest.raises(RulesInvalid, match=message):
        parse(yaml.safe_dump({"rules": [rule]}, allow_unicode=True))


def test_rules_document_shape():
    ok = {"id": "X1", "patterns": ["a"], "suggest": {"cde": True}, "confidence": 0.5}
    with pytest.raises(RulesInvalid, match="重複"):
        parse(yaml.safe_dump({"rules": [ok, ok]}))
    for bad in ("[1]", "rules: {}", "other: 1", ": : :"):
        with pytest.raises(RulesInvalid):
            parse(bad)
    assert parse("").items == ()


def test_invalid_rules_proposal_is_422(client):
    r = client.post(f"{API}/policies/bundles", headers=U("proposer", "committee"),
                    json={"changes": {RULES_FILE: "rules: [{id: X1, patterns: [a], suggest: {pii: false}, confidence: 0.5}]"}, "reason": "x"})
    assert r.status_code == 422 and "true だけ" in r.text


def test_tokens():
    assert tokens("customerEmailAddress") == ["customer", "email", "address"]
    assert tokens("BILLING-zip_code") == ["billing", "zip", "code"]


# ---------------------------------------------------------------- 根拠: 辞書規則 (日本語と英語)
def test_dictionary_rules_in_english_and_japanese(client):
    a = asset(client, "sales.customers", schema=[
        {"name": "customer_email", "type": "string"},
        {"name": "電話番号", "type": "string"},
        {"name": "contactMobilePhone", "type": "string"},
        {"name": "hotel_name", "type": "string"},  # "tel" を部分一致で拾わない
        {"name": "c1", "type": "string", "description": "顧客の住所 (都道府県から)"},
        {"name": "order_id", "type": "string"},
        {"name": "revenue", "type": "number"},
    ])
    found = by_column(suggest(a["id"]))
    assert set(found) == {"customer_email", "電話番号", "contactMobilePhone", "c1", "revenue"}
    email = found["customer_email"]
    assert email["proposal"] == {"pii": True, "pii_category": "contact", "sensitivity": "confidential"}
    assert email["current"] == {"pii": False, "pii_category": None, "sensitivity": "internal"}
    [ev] = email["evidence"]
    assert ev == {"source": "rule", "rule_id": "CLS-EMAIL", "field": "name", "pattern": "email", "confidence": 0.9,
                  "suggest": {"pii": True, "pii_category": "contact", "sensitivity": "confidential"}}
    assert found["電話番号"]["evidence"][0]["rule_id"] == "CLS-PHONE" and found["電話番号"]["evidence"][0]["pattern"] == "電話番号"
    assert found["contactMobilePhone"]["evidence"][0]["rule_id"] == "CLS-PHONE"
    address = found["c1"]["evidence"][0]
    assert address["field"] == "description" and address["confidence"] == pytest.approx(0.8 * 0.8)
    assert found["revenue"]["proposal"] == {"cde": True}


def test_only_raising_changes_are_suggested(client):
    a = asset(client, "sales.customers", sensitivity="restricted", schema=[
        {"name": "email", "type": "string", "pii": True, "pii_category": "other"},  # 人が決めたカテゴリは上書きしない
        {"name": "phone", "type": "string", "pii": True, "pii_category": "contact"},
    ])
    assert suggest(a["id"]) == []


# ---------------------------------------------------------------- 根拠: Lineage の DIRECT の経路
def _edge(source, source_column, target, target_column, *, type_="DIRECT", subtype="IDENTITY", masking=False):
    with SessionLocal() as db:
        db.add(ColumnLineageEdge(source=source, source_column=source_column, target=target, target_column=target_column, type=type_,
                                 subtype=subtype, masking=masking, via="manual", recorded_by="x"))
        db.commit()


def test_lineage_direct_path_is_evidence(client):
    asset(client, "crm.contacts", sensitivity="restricted", schema=[{"name": "addr", "type": "string", "pii": True, "pii_category": "location"},
                                                                    {"name": "m", "type": "string", "pii": True}])
    a = asset(client, "sales.copy", schema=[{"name": "x1", "type": "string"}, {"name": "x2", "type": "string"},
                                            {"name": "x3", "type": "string"}, {"name": "x4", "type": "string"}])
    _edge("crm.contacts", "addr", "sales.copy", "x1")
    _edge("crm.contacts", "addr", "sales.copy", "x2", masking=True)  # マスキングを伴う経路は使わない
    _edge("crm.contacts", "addr", "sales.copy", "x3", type_="INDIRECT", subtype="FILTER")
    _edge("crm.contacts", "m", "sales.copy", "x4", subtype="TRANSFORMATION")
    found = by_column(suggest(a["id"]))
    assert set(found) == {"x1", "x4"}
    assert found["x1"]["proposal"] == {"pii": True, "pii_category": "location", "sensitivity": "restricted"}
    [ev] = found["x1"]["evidence"]
    assert ev["source"] == "lineage" and ev["upstream"] == "crm.contacts" and ev["upstream_column"] == "addr" and ev["confidence"] == 0.85
    assert found["x4"]["evidence"][0]["confidence"] == 0.75 and "pii_category" not in found["x4"]["proposal"]


# ---------------------------------------------------------------- 根拠: Glossary
def test_glossary_term_is_evidence(client):
    a = asset(client, "sales.leads", schema=[{"name": "contact_point", "type": "string"}, {"name": "lead_id", "type": "string"}])
    r = client.post(f"{API}/glossary/terms", headers=ADMIN, json={"name": "顧客連絡先", "definition": "顧客のメールアドレス",
                                                                  "synonyms": ["contact point"]})
    assert r.status_code == 201, r.text
    term = r.json()
    assert client.put(f"{API}/glossary/terms/{term['id']}/assets/{a['id']}", headers=ADMIN).status_code == 200
    [s] = suggest(a["id"])
    assert s["column"] == "contact_point"
    [ev] = s["evidence"]
    assert ev["source"] == "glossary" and ev["term"] == "顧客連絡先" and ev["rule_id"] == "CLS-EMAIL"
    assert ev["confidence"] == pytest.approx(0.9 * 0.9)


# ---------------------------------------------------------------- データの値は使わない
def test_data_values_are_never_used(client, autonomy_on):
    marker = "MARKER-CLS-b81d taro@example.com 090-1234-5678"
    a = asset(client, "sales.notes", schema=[{"name": "note", "type": "string"}, {"name": "id", "type": "string"}])
    qa = U("qa", "admin,owner,custodian", "*")
    assert client.post(f"{API}/quality/assets/{a['id']}/rules", headers=qa, json={
        "name": "nn", "dimension": "completeness", "rule_type": "not_null", "column": "note"}).status_code == 201
    assert client.post(f"{API}/quality/assets/{a['id']}/runs", headers=qa, json={"records": [{"note": marker, "id": "1"}]}).status_code == 201
    use_playbooks(cls_playbook())
    run_now()
    assert suggestions(a["id"]) == []  # 値にメールアドレスがあっても、カラム名と説明文に根拠が無ければ提案しない
    assert "marker-cls-b81d" not in dump_database() and "taro@example.com" not in dump_database()


# ---------------------------------------------------------------- 提案は承認エンジンに作られるだけ
def _customers(client, **kw):
    return asset(client, "sales.customers", schema=[{"name": "customer_email", "type": "string"}, {"name": "id", "type": "string"}], **kw)


def test_dry_run_creates_suggestions_but_no_approval(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook(mode="dry_run"))
    run_now()
    [s] = suggestions(a["id"])
    assert s.status == "pending" and s.approval_id is None
    [row] = runs()
    assert row.status == "dry_run" and row.after["proposal"] == s.proposal


def test_proposal_does_not_change_state_until_decided(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    assert s.status == "proposed" and s.approval_id
    with SessionLocal() as db:
        asset_row = db.get(DataAsset, a["id"])
        assert asset_row.schema_[0].get("pii") is False and asset_row.sensitivity == "internal"
        req = db.get(ApprovalRequest, s.approval_id)
        assert req.type == "classification_suggestion" and req.requested_by == "system:autonomy:PB-CLS"
        with pytest.raises(ProblemError) as e:
            approvals.decide(db, autonomy_catalog.principal_for("PB-CLS"), req, approve=True, comment="")
        assert e.value.status_code == 403
    # 共通の判断の API ではなく、専用の API で判断する
    r = client.post(f"{API}/approvals/{s.approval_id}/decision", headers=STEWARD, json={"approve": True, "comment": ""})
    assert r.status_code == 409 and r.json()["decision_path"] == f"/api/v1/autonomy/suggestions/{s.id}/decision"
    for who in (U("asset-custodian"), U("someone", "steward", "*"), CHAIR, ADMIN):
        assert client.post(f"{AUT}/suggestions/{s.id}/decision", headers=who, json={"accept": True}).status_code == 403
    assert client.post(f"{AUT}/suggestions/{s.id}/decision", headers=U("system:autonomy:PB-CLS", "owner,steward", "*"),
                       json={"accept": True}).status_code == 403
    with SessionLocal() as db:
        assert db.get(DataAsset, a["id"]).schema_[0].get("pii") is False


def test_accept_goes_through_the_existing_update_path(client, autonomy_on):
    a = _customers(client)
    with SessionLocal() as db:
        db.execute(update(DataAsset).where(DataAsset.id == a["id"]).values(certification="certified"))
        db.commit()
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    r = client.post(f"{AUT}/suggestions/{s.id}/decision", headers=STEWARD, json={"accept": True, "reason": "メールアドレスです"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted" and r.json()["approval_status"] == "approved" and r.json()["decided_by"] == "asset-steward"
    with SessionLocal() as db:
        row = db.get(DataAsset, a["id"])
        email = next(c for c in row.schema_ if c["name"] == "customer_email")
        assert email["pii"] is True and email["pii_category"] == "contact"
        assert row.contains_pii is True and row.sensitivity == "confidential"
        assert row.certification == "draft"  # 既存の更新の経路の統制 (分類の変更で再認証) が発火する
    update_event = audits("asset.update")[-1]
    assert update_event.actor == "dev:asset-steward" and update_event.payload["certification_reset"] is True
    assert audits("classification.accept")[-1].payload["applied"] == ["schema_", "sensitivity"]
    assert client.post(f"{AUT}/suggestions/{s.id}/decision", headers=OWNER, json={"accept": True}).status_code == 409


def test_rejected_suggestion_is_not_repeated_unless_the_evidence_changes(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    assert client.post(f"{AUT}/suggestions/{s.id}/decision", headers=OWNER, json={"accept": False, "reason": " "}).status_code == 422
    r = client.post(f"{AUT}/suggestions/{s.id}/decision", headers=OWNER, json={"accept": False, "reason": "テスト用の架空の値"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decision_reason"] == "テスト用の架空の値"
    assert audits("classification.reject")[-1].payload["evidence_hash"] == s.evidence_hash
    run_now()
    run_now()
    assert [x.status for x in suggestions(a["id"])] == ["rejected"]  # 同じ根拠では二度と出さない
    # 説明文を足しても、同じ規則に名前で当たっている限り根拠は同じ (同じ規則は 1 件として数える)
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [
        {"name": "customer_email", "type": "string", "description": "連絡用のメールアドレス"}, {"name": "id", "type": "string"}]})
    assert r.status_code == 200, r.text
    run_now()
    assert [x.status for x in suggestions(a["id"])] == ["rejected"]
    # 根拠が変わる (上流の PII のカラムからの DIRECT の経路が加わる) と、新しい提案になる
    asset(client, "crm.contacts", schema=[{"name": "mail", "type": "string", "pii": True, "pii_category": "contact"}])
    _edge("crm.contacts", "mail", "sales.customers", "customer_email")
    run_now()
    statuses = sorted(x.status for x in suggestions(a["id"]))
    assert statuses == ["proposed", "rejected"]
    new = next(x for x in suggestions(a["id"]) if x.status == "proposed")
    assert new.evidence_hash != s.evidence_hash and {e["source"] for e in new.evidence} == {"rule", "lineage"}


def test_changed_evidence_supersedes_pending_suggestion(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook(mode="dry_run"))
    run_now()
    [old] = suggestions(a["id"])
    client.post(f"{API}/glossary/terms", headers=ADMIN, json={"name": "customer email", "definition": "メールアドレス"})
    term = client.get(f"{API}/glossary/terms", headers=ADMIN).json()[0]
    assert client.put(f"{API}/glossary/terms/{term['id']}/assets/{a['id']}", headers=ADMIN).status_code == 200
    run_now()
    rows = suggestions(a["id"])
    assert sorted(r.status for r in rows) == ["pending", "superseded"] and old.id in {r.id for r in rows if r.status == "superseded"}


def test_inbox_shows_dedicated_operations_with_step_up(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    items = client.get(f"{API}/approvals/inbox", headers=STEWARD).json()
    [item] = [i for i in items if i["id"] == s.approval_id]
    assert "sales.customers.customer_email" in item["title"]
    assert [(o["id"], o["path"], o["step_up"]) for o in item["operations"]] == [
        ("accept", f"/api/v1/autonomy/suggestions/{s.id}/decision", True), ("reject", f"/api/v1/autonomy/suggestions/{s.id}/decision", True)]
    assert client.get(f"{API}/approvals/inbox", headers=U("asset-custodian")).json() == []


def test_decision_needs_step_up_on_the_ui_path():
    assert authz.PERMISSIONS["classification.suggestion.decide"].approval is True
    old = utcnow() - timedelta(hours=2)
    ui = Principal("asset-steward", frozenset(), "confidential", frozenset(), channel="ui", auth_time=old, write_request=True)
    with SessionLocal() as db:
        with pytest.raises(ProblemError) as e:
            authz.require_step_up(ui, "classification.suggestion.decide")
        assert e.value.type_ == "step-up-required"
        del db


def test_suggestion_listing_scope(client, autonomy_on):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales", "owner": "o"})
    a = _customers(client, domain="sales")
    use_playbooks(cls_playbook(mode="dry_run"))
    run_now()
    [s] = suggestions(a["id"])
    assert len(client.get(f"{AUT}/suggestions", headers=U("s", "steward", "sales")).json()) == 1
    assert client.get(f"{AUT}/suggestions", headers=U("h", "steward", "hr")).json() == []
    assert client.get(f"{AUT}/suggestions/{s.id}", headers=U("h", "steward", "hr")).status_code == 404
    assert client.get(f"{AUT}/suggestions/{s.id}", headers=CHAIR).json()["evidence"][0]["rule_id"] == "CLS-EMAIL"
    assert client.get(f"{AUT}/suggestions/nope", headers=CHAIR).status_code == 404
    assert client.post(f"{AUT}/suggestions/nope/decision", headers=CHAIR, json={"accept": True}).status_code == 404
    assert client.post(f"{AUT}/suggestions/{s.id}/decision", headers=OWNER, json={"accept": True}).status_code == 409  # まだ案件ではない


def test_schema_change_after_proposal_conflicts(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"schema": [{"name": "id", "type": "string"}]})
    r = client.post(f"{AUT}/suggestions/{s.id}/decision", headers=OWNER, json={"accept": True})
    assert r.status_code == 409 and "存在しません" in r.json()["detail"]


def test_cancelled_approval_marks_the_suggestion(client, autonomy_on):
    a = _customers(client)
    use_playbooks(cls_playbook())
    run_now()
    [s] = suggestions(a["id"])
    assert client.post(f"{API}/approvals/{s.approval_id}/cancel", headers=ADMIN, json={"reason": "重複"}).status_code == 200
    assert suggestions(a["id"])[0].status == "cancelled"
