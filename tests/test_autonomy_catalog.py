"""Phase 19a: 自動化の主体、プレイブックの定義とロード時の検証、操作のカタログ (docs/adr/0001)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from edgf import authz
from edgf.auth import AUTONOMY, ROLES, normalize_roles
from edgf.config import settings
from edgf.db import SessionLocal
from edgf.services import approval_types, autonomy_catalog
from edgf.services.autonomy_catalog import A, B, CATALOG, FORBIDDEN, PLAYBOOK_FILE, PlaybookInvalid, parse
from tests.process_helpers import API, COMMITTEE, U, decide

POL = f"{API}/policies"
DISK = (Path(settings.policy_dir) / PLAYBOOK_FILE).read_text(encoding="utf-8")
ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0001-autonomous-remediation-scope.md"


def _doc() -> dict:
    return yaml.safe_load(DISK)


def _with(index: int, **changes) -> str:
    doc = _doc()
    doc["playbooks"][index].update(changes)
    for key in [k for k, v in changes.items() if v is None]:
        doc["playbooks"][index].pop(key)
    return yaml.safe_dump(doc, allow_unicode=True)


def _index(playbook_id: str) -> int:
    return next(i for i, p in enumerate(_doc()["playbooks"]) if p["id"] == playbook_id)


# ---------------------------------------------------------------- ADR
def test_adr_exists_and_defines_the_three_categories():
    text = ADR.read_text(encoding="utf-8")
    for heading in ("背景", "§4 Business Ownership", "区分", "安全装置", "dry-run", "Committee の承認の方法", "見直しの条件"):
        assert heading in text, heading
    for category in ("| A |", "| B |", "| C |"):
        assert category in text


# ---------------------------------------------------------------- 自動化の主体
def test_autonomy_role_cannot_hold_approval_permissions():
    with pytest.raises(ValueError, match="system:autonomy"):
        authz.validate_permissions({"x.approve": authz.rule(roles=(AUTONOMY,), approval=True)})
    with pytest.raises(ValueError, match="system:autonomy"):
        authz.validate_permissions({"x.approve": authz.rule(domain_roles=(AUTONOMY,), approval=True)})
    with pytest.raises(ValueError, match="system:autonomy"):
        approval_types.add_rule("autonomy_approver", authz.rule(roles=(AUTONOMY,), approval=True))
    assert "autonomy_approver" not in approval_types.APPROVER_RULES
    authz.validate_permissions({"x.run": authz.rule(roles=(AUTONOMY,))})  # 承認でない権限は持てる


def test_no_approval_permission_is_granted_to_autonomy():
    rules = {**authz.PERMISSIONS, **approval_types.APPROVER_RULES}
    assert [a for a, r in rules.items() if r.approval and AUTONOMY in r.roles | r.domain_roles] == []
    principal = autonomy_catalog.principal_for("PB-X")
    assert principal.subject == "system:autonomy:PB-X" and principal.audit_actor == "system:autonomy:PB-X"
    assert [a for a, r in authz.PERMISSIONS.items() if r.approval and authz.is_allowed(principal, a)] == []


def test_autonomy_role_cannot_be_obtained_from_the_idp():
    assert AUTONOMY not in ROLES
    assert AUTONOMY not in normalize_roles(["autonomy", "committee"]) and "committee" in normalize_roles(["autonomy", "committee"])
    assert AUTONOMY not in normalize_roles(["x"], {"x": "autonomy"})


# ---------------------------------------------------------------- カタログ
def test_catalog_is_only_categories_a_and_b():
    assert {op.category for op in CATALOG.values()} == {A, B}
    assert not set(CATALOG) & set(FORBIDDEN)
    # 区分 A は Issue の起票を除き取り消せる (ADR §3)
    assert [op.id for op in CATALOG.values() if op.category == A and not op.undoable] == ["issue.open"]
    assert not any(op.undoable for op in CATALOG.values() if op.category == B)


def test_initial_playbooks_are_all_dry_run():
    playbooks = parse(DISK)
    assert playbooks.items and {p.mode for p in playbooks.items} == {"dry_run"}
    assert {p.action for p in playbooks.items} == set(CATALOG)  # カタログの操作をすべて使う
    for p in playbooks.items:
        assert p.category == CATALOG[p.action].category
        assert (p.undo_days > 0) == p.operation.undoable


# ---------------------------------------------------------------- ロード時の検証
@pytest.mark.parametrize("action", sorted(FORBIDDEN))
def test_category_c_operations_cannot_be_declared(action):
    with pytest.raises(PlaybookInvalid, match="区分 C"):
        parse(_with(_index("PB-ACCESS-STALE"), action=action))


def test_category_c_cannot_be_declared_as_a_category():
    with pytest.raises(PlaybookInvalid, match="区分 C"):
        parse(_with(_index("PB-ACCESS-STALE"), category="C"))


@pytest.mark.parametrize("playbook,category", [("PB-ACCESS-STALE", "B"), ("PB-ANOMALY-ISSUE", "B"), ("PB-CLASSIFICATION", "A")])
def test_disguised_category_is_rejected(playbook, category):
    with pytest.raises(PlaybookInvalid, match="偽る"):
        parse(_with(_index(playbook), category=category))


@pytest.mark.parametrize("playbook,changes,message", [
    ("PB-ACCESS-STALE", {"action": "access.revoke_all"}, "カタログに無い"),
    ("PB-ACCESS-STALE", {"trigger": {"detector": "quality_anomaly"}}, "受け付けません"),
    ("PB-ACCESS-STALE", {"trigger": {"detector": "nope"}}, "存在しない契機"),
    ("PB-ACCESS-STALE", {"trigger": {"event": "issue.opened"}}, "存在しない契機"),
    ("PB-ACCESS-STALE", {"trigger": {"detector": "stale_access", "event": "quality.anomaly_detected"}}, "どちらか 1 つ"),
    ("PB-ACCESS-STALE", {"mode": "live"}, "mode"),
    ("PB-ACCESS-STALE", {"max_actions_per_run": 0}, "max_actions_per_run"),
    ("PB-ACCESS-STALE", {"max_actions_per_run": 5000}, "max_actions_per_run"),
    ("PB-ACCESS-STALE", {"undo_days": None}, "undo_days"),
    ("PB-ACCESS-STALE", {"undo_days": 400}, "undo_days"),
    ("PB-ACCESS-STALE", {"false_positive_threshold": None}, "誤って実行した率"),
    ("PB-ACCESS-STALE", {"false_positive_threshold": 0}, "0 より大きく"),
    ("PB-ACCESS-STALE", {"false_positive_threshold": 1.5}, "false_positive_threshold"),
    ("PB-ACCESS-STALE", {"condition": {"consecutive_passes": 3}}, "使えない条件"),
    ("PB-ACCESS-STALE", {"condition": {"expired": "yes"}}, "true / false"),
    ("PB-ACCESS-STALE", {"condition": []}, "オブジェクト"),
    ("PB-ANOMALY-ISSUE", {"undo_days": 10}, "取り消せない操作"),
    ("PB-ANOMALY-ISSUE", {"condition": {"kinds": ["seasonal"]}}, "kinds"),
    ("PB-ANOMALY-ISSUE", {"condition": {"kinds": []}}, "kinds"),
    ("PB-CLASSIFICATION", {"condition": {"min_confidence": 2}}, "min_confidence"),
    ("PB-POLICY-REVIEW", {"condition": {"failing_days": 0}}, "failing_days"),
    ("PB-ACCESS-STALE", {"id": "lower"}, "id"),
    ("PB-ACCESS-STALE", {"description": "x" * 501}, "description"),
    ("PB-ACCESS-STALE", {"extra": 1}, "未知の項目"),
])
def test_invalid_playbooks_are_rejected(playbook, changes, message):
    with pytest.raises(PlaybookInvalid, match=message):
        parse(_with(_index(playbook), **changes))


def test_document_shape_and_duplicates():
    doc = _doc()
    doc["playbooks"].append(copy.deepcopy(doc["playbooks"][0]))
    with pytest.raises(PlaybookInvalid, match="重複"):
        parse(yaml.safe_dump(doc, allow_unicode=True))
    for bad in ("[1]", "playbooks: {}", "other: 1", ": : :", "playbooks: [1]"):
        with pytest.raises(PlaybookInvalid):
            parse(bad)
    assert parse("playbooks: []").items == ()
    assert parse("").items == ()


def test_defaults_are_filled_in():
    pb = parse(_with(_index("PB-ACCESS-STALE"), condition=None)).get("PB-ACCESS-STALE")
    assert pb.condition == {"expired": True, "asset_gone": True}
    assert pb.as_dict()["trigger"] == {"detector": "stale_access"} and pb.as_dict()["undoable"] is True
    decertify = parse(DISK).get("PB-ANOMALY-DECERTIFY")
    assert decertify.trigger_type == "event" and decertify.target_kind == "anomaly"
    assert parse(DISK).get("nope") is None


# ---------------------------------------------------------------- Policy Bundle
def _active():
    with SessionLocal() as db:
        return autonomy_catalog.active(db)


def test_playbooks_are_part_of_the_bundle(client):
    active = client.get(f"{POL}/bundles", headers=U("root", "admin"), params={"status": "active"}).json()[0]
    files = client.get(f"{POL}/bundles/{active['version']}", headers=U("root", "admin")).json()["files"]
    assert PLAYBOOK_FILE in files and _active().digest == parse(files[PLAYBOOK_FILE]).digest


def test_switch_to_active_needs_committee_approval_and_publication(client):
    index = _index("PB-ACCESS-STALE")
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"),
                    json={"changes": {PLAYBOOK_FILE: _with(index, mode="active")}, "reason": "dry_run の結果を確認した"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    req = client.get(f"{API}/approvals/{proposal['approval_id']}", headers=COMMITTEE).json()
    assert [s["name"] for s in req["steps"]] == ["committee"]
    assert _active().get("PB-ACCESS-STALE").mode == "dry_run"
    decide(client, proposal["approval_id"], U("vice-chair", "committee"))
    assert _active().get("PB-ACCESS-STALE").mode == "dry_run"  # 公開の前
    assert client.post(f"{POL}/bundles/{proposal['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    assert _active().get("PB-ACCESS-STALE").mode == "active"


def test_invalid_playbook_proposal_is_422(client):
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"),
                    json={"changes": {PLAYBOOK_FILE: _with(_index("PB-ACCESS-STALE"), action="asset.delete")}, "reason": "x"})
    assert r.status_code == 422 and "区分 C" in r.text
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"),
                    json={"changes": {"remediation/other.yaml": "playbooks: []"}, "reason": "x"})
    assert r.status_code == 422


def test_no_bundle_file_means_no_playbooks(client):
    """既存の環境でプレイブックを承認する前は、自動の是正は何も動かない (ADR §6)。"""
    with SessionLocal() as db:
        from edgf.services import policy_bundles
        bundle = policy_bundles.active_bundle(db)
        bundle.files = {k: v for k, v in bundle.files.items() if k != PLAYBOOK_FILE}
        db.commit()
    assert _active().items == ()
