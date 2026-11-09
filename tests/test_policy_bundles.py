"""Phase 12b (§16 Policy): Policy の変更のワークフロー."""
from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from sqlalchemy import text

from edgf.config import settings
from edgf.db import SessionLocal, engine
from edgf.services import policy, policy_bundles
from tests.process_helpers import ADMIN, API, COMMITTEE, COMMITTEE2, U, asset, audit_actions, decide, inbox

POL = f"{API}/policies"
CHAIR_PROPOSER = U("proposer", "committee")
NEW_POLICY = {"id": "TEST-001", "name": "保持期間の必須化", "severity": "low", "check": {"type": "required_fields", "fields": ["retention_days"]}}


def _active(client) -> dict:
    version = client.get(f"{POL}/bundles", headers=ADMIN, params={"status": "active"}).json()[0]["version"]
    return client.get(f"{POL}/bundles/{version}", headers=ADMIN).json()


def _global_with(client, *new_policies, drop=()) -> str:
    doc = yaml.safe_load(_active(client)["files"]["global.yaml"])
    doc["policies"] = [p for p in doc["policies"] if p["id"] not in drop] + list(new_policies)
    return yaml.safe_dump(doc, allow_unicode=True)


def _propose(client, changes, who=CHAIR_PROPOSER, code=202, reason="統制の追加"):
    r = client.post(f"{POL}/bundles", headers=who, json={"changes": changes, "reason": reason})
    assert r.status_code == code, r.text
    return r.json()


def _results(client, asset_id) -> dict:
    res = client.get(f"{POL}/evaluate/assets/{asset_id}", headers=ADMIN).json()["results"]
    return {r["policy_id"]: r["status"] for r in res}


def _publish(client, version, who=COMMITTEE, code=200):
    r = client.post(f"{POL}/bundles/{version}/publish", headers=who)
    assert r.status_code == code, r.text
    return r.json()


def _approved_and_published(client, changes) -> dict:
    p = _propose(client, changes)
    decide(client, p["approval_id"], COMMITTEE)
    _publish(client, p["proposal_version"])
    return p


# ---------------------------------------------------------------- 取り込み
def test_bootstrap_records_v1_once(client):
    active = _active(client)
    assert active["version"] == 1 and active["status"] == "active" and "global.yaml" in active["files"]
    assert len(active["content_hash"]) == 64
    events = [e for e in audit_actions(client) if e["action"] == "policy.bootstrap"]
    assert len(events) == 1 and events[0]["actor"] == "system:bootstrap" and events[0]["payload"]["version"] == 1
    # 2 回目の起動では取り込まない
    policy_bundles.bootstrap_on_startup()
    assert client.get(f"{POL}/bundles", headers=ADMIN).headers["X-Total-Count"] == "1"


def test_lazy_bootstrap_when_startup_could_not(client):
    """起動時にスキーマが無かった場合は、最初の評価で取り込む。"""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM policy_bundles"))
    with SessionLocal() as db:
        assert policy.get_policies(db=db)
        db.commit()
    assert _active(client)["version"] == 1


def test_startup_without_schema_only_validates_syntax(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Broken:
        def __enter__(self):
            raise OperationalError("x", {}, Exception("no such table"))

        def __exit__(self, *args):
            return False
    monkeypatch.setattr(policy_bundles, "SessionLocal", Broken)
    policy_bundles.bootstrap_on_startup()
    assert policy_bundles.drift_message() is None


# ---------------------------------------------------------------- 受け入れ基準
def test_evaluation_does_not_change_until_published(client):
    a = asset(client)
    assert "TEST-001" not in _results(client, a["id"])
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    assert p["status"] == "proposed" and p["proposal_version"] == 2
    assert "TEST-001" not in _results(client, a["id"])  # 提案だけでは変わらない
    assert "TEST-001" not in {x["id"] for x in client.get(POL, headers=ADMIN).json()}
    _publish(client, p["proposal_version"], code=409)  # 承認前は公開できない
    decide(client, p["approval_id"], COMMITTEE)
    got = client.get(f"{POL}/bundles/{p['proposal_version']}", headers=ADMIN).json()
    assert got["status"] == "approved" and got["approved_at"]
    assert "TEST-001" not in _results(client, a["id"])  # 承認されても公開までは変わらない
    _publish(client, p["proposal_version"], who=ADMIN, code=403)  # 公開は Committee だけ
    done = _publish(client, p["proposal_version"])
    assert done["status"] == "active" and done["published_by"] == "chair"
    assert _results(client, a["id"])["TEST-001"] == "fail"
    assert "TEST-001" in {x["id"] for x in client.get(POL, headers=ADMIN).json()}
    old = client.get(f"{POL}/bundles/1", headers=ADMIN).json()
    assert old["status"] == "superseded" and old["superseded_at"]
    _publish(client, p["proposal_version"], code=409)
    assert {"policy.propose", "policy.publish"} <= {e["action"] for e in audit_actions(client)}


def test_disk_change_does_not_affect_evaluation(client, tmp_path):
    """ディスクを変更しても評価は変わらない。違いは /ready に示すが 503 にはしない。"""
    a = asset(client)
    assert client.get("/ready").json()["checks"] == {"database": "ok", "schema": "ok", "policies": "ok"}
    original = settings.policy_dir
    shutil.copytree(original, tmp_path / "p")
    doc = yaml.safe_load((tmp_path / "p" / "global.yaml").read_text(encoding="utf-8"))
    doc["policies"].append(NEW_POLICY)
    (tmp_path / "p" / "global.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    object.__setattr__(settings, "policy_dir", tmp_path / "p")
    try:
        assert "TEST-001" not in _results(client, a["id"])
        r = client.get("/ready")
        assert r.status_code == 200 and "v1" in r.json()["checks"]["policy_drift"]
        # reload は提案を作るだけ (202)
        rel = client.post(f"{POL}/reload", headers=COMMITTEE)
        assert rel.status_code == 202 and rel.json()["status"] == "proposed" and rel.json()["approval_id"]
        assert "TEST-001" not in _results(client, a["id"])
        again = client.post(f"{POL}/reload", headers=ADMIN)  # 同じ内容の提案は増やさない
        assert again.status_code == 202 and again.json()["proposal_version"] == rel.json()["proposal_version"]
        decide(client, rel.json()["approval_id"], COMMITTEE2)
        _publish(client, rel.json()["proposal_version"])
        assert _results(client, a["id"])["TEST-001"] == "fail"
        assert "policy_drift" not in client.get("/ready").json()["checks"]
    finally:
        object.__setattr__(settings, "policy_dir", original)


def test_reload_with_same_content_or_invalid_disk(client, tmp_path):
    assert client.post(f"{POL}/reload", headers=COMMITTEE).status_code == 409
    original = settings.policy_dir
    (tmp_path / "global.yaml").write_text("policies:\n  - {id: X, name: x, check: {type: nope}}\n", encoding="utf-8")
    object.__setattr__(settings, "policy_dir", tmp_path)
    try:
        assert client.post(f"{POL}/reload", headers=COMMITTEE).status_code == 422
    finally:
        object.__setattr__(settings, "policy_dir", original)


def test_rejected_proposal_never_applies(client):
    a = asset(client)
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    decide(client, p["approval_id"], COMMITTEE, approve=False, comment="時期尚早")
    assert client.get(f"{POL}/bundles/{p['proposal_version']}", headers=ADMIN).json()["status"] == "rejected"
    _publish(client, p["proposal_version"], code=409)
    assert "TEST-001" not in _results(client, a["id"])
    # 取り消しでも rejected になり、同じ内容を提案し直せる
    p2 = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    assert p2["proposal_version"] != p["proposal_version"]
    assert client.post(f"{API}/approvals/{p2['approval_id']}/cancel", headers=CHAIR_PROPOSER).json()["status"] == "cancelled"
    assert client.get(f"{POL}/bundles/{p2['proposal_version']}", headers=ADMIN).json()["status"] == "rejected"


def test_proposer_cannot_approve_and_stale_proposal_is_rejected(client):
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    decide(client, p["approval_id"], CHAIR_PROPOSER, code=403)
    other = _propose(client, {"global.yaml": _global_with(client, {**NEW_POLICY, "id": "TEST-002"})})
    decide(client, p["approval_id"], COMMITTEE)
    decide(client, other["approval_id"], COMMITTEE)
    _publish(client, p["proposal_version"])
    # 提案の後に active の版が変わったので、影響評価をやり直すまで公開できない
    r = client.post(f"{POL}/bundles/{other['proposal_version']}/publish", headers=COMMITTEE)
    assert r.status_code == 409 and "提案し直して" in r.json()["detail"]
    assert client.post(f"{POL}/bundles/99/publish", headers=COMMITTEE).status_code == 404


# ---------------------------------------------------------------- 影響評価
def test_dry_run_matches_actual_results(client):
    a = asset(client, "sales.no_retention")
    b = asset(client, "sales.with_retention", retention_days=30)
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY, drop=["LIN-001"])})
    diff = client.get(f"{POL}/bundles/{p['proposal_version']}/diff", headers=ADMIN).json()
    assert diff["policies_added"] == ["TEST-001"] and diff["policies_removed"] == ["LIN-001"] and diff["files_changed"] == ["global.yaml"]
    impact = diff["impact"]
    assert [(i["qualified_name"], i["policy_ids"]) for i in impact["newly_failing"]] == [("sales.no_retention", ["TEST-001"])]
    before = {x: _results(client, x) for x in (a["id"], b["id"])}
    decide(client, p["approval_id"], COMMITTEE)
    _publish(client, p["proposal_version"])
    after = {x: _results(client, x) for x in (a["id"], b["id"])}
    # 実際の評価結果と一致する
    for asset_id, name in ((a["id"], "sales.no_retention"), (b["id"], "sales.with_retention")):
        failed_now = {k for k, v in after[asset_id].items() if v == "fail"} - {k for k, v in before[asset_id].items() if v == "fail"}
        recorded = next((i["policy_ids"] for i in impact["newly_failing"] if i["qualified_name"] == name), [])
        assert sorted(failed_now) == recorded
    assert impact["asset_count"] == 2
    rates = impact["domain_compliance"]["(none)"]
    compliant = sum(1 for r in after.values() for v in r.values() if v != "fail")
    total = sum(len(r) for r in after.values())
    assert rates["after"] == round(compliant / total, 6)
    assert rates["after"] != rates["before"]


def test_dry_run_reports_newly_passing_and_domain_rates(client):
    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales", "owner": "o"}).status_code == 201
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "sales.report", "name": "r", "description": "d", "domain": "sales", "data_class": "analytical",
                                                          "sensitivity": "internal", "owner": "o", "steward": "s", "controls": ["encryption_at_rest", "encryption_in_transit"]})
    assert r.status_code == 201
    p = _propose(client, {"global.yaml": _global_with(client, drop=["LIN-001"])})
    impact = client.get(f"{POL}/bundles/{p['proposal_version']}/diff", headers=ADMIN).json()["impact"]
    assert [(i["qualified_name"], i["policy_ids"], i["domain"]) for i in impact["newly_passing"]] == [("sales.report", ["LIN-001"], "sales")]
    rates = impact["domain_compliance"]["sales"]
    assert rates["after"] > rates["before"]


def test_diff_hides_unreadable_assets(client):
    asset(client, "sales.secret", sensitivity="restricted")
    asset(client, "sales.open")
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    diff = client.get(f"{POL}/bundles/{p['proposal_version']}/diff", headers=U("reader", clearance="internal")).json()
    assert [i["qualified_name"] for i in diff["impact"]["newly_failing"]] == ["sales.open"]
    assert diff["impact"]["newly_failing_hidden_count"] == 1
    assert client.get(f"{POL}/bundles/99/diff", headers=ADMIN).status_code == 404
    assert client.get(f"{POL}/bundles/99", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------- 提案の検証
@pytest.mark.parametrize("changes", [
    {"domains/sales.yaml": "scope: domain\ndomain: sales\npolicies:\n  - {id: GOV-001, name: weaker, check: {type: glossary_linked}}\n"},  # 上書き
    {"global.yaml": "policies:\n  - {id: X, name: x, check: {type: nope}}\n"},  # 未知の check type
    {"global.yaml": "policies: [\n"},  # YAML の不正
    {"global.yaml": "- a\n- b\n"},
    {"global.yaml": "policies:\n  - {name: x}\n"},
    {"../evil.yaml": "policies: []\n"},  # パス
    {"domains/a/b.yaml": "policies: []\n"},
    {"other.txt": "x"},
    {"domains/sales.yaml": "scope: global\npolicies: []\n"},  # 置き場所と宣言の不一致
    {"domains/sales.yaml": "scope: domain\ndomain: finance\npolicies: []\n"},
    {"extra.yaml": "scope: domain\ndomain: sales\npolicies: []\n"},
])
def test_invalid_proposals_are_rejected(client, changes):
    _propose(client, changes, code=422)
    assert client.get(f"{POL}/bundles", headers=ADMIN).headers["X-Total-Count"] == "1"


def test_same_content_cannot_be_proposed(client):
    _propose(client, {}, code=409)


def test_oversized_bundle_is_rejected(client):
    _propose(client, {"big.yaml": "# " + "a" * 2_000_001}, code=422)


# ---------------------------------------------------------------- 承認者
DOMAIN_FILE = "scope: domain\ndomain: sales\npolicies:\n  - {id: SALES-002, name: 追加, severity: low, check: {type: required_fields, fields: [freshness_sla_hours]}}\n"
SALES_OWNER = U("sales-owner", "owner", "sales")


def test_global_change_needs_committee_only(client):
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    steps = client.get(f"{API}/approvals/{p['approval_id']}", headers=ADMIN).json()["steps"]
    assert [s["name"] for s in steps] == ["committee"]
    assert client.get(f"{POL}/bundles/{p['proposal_version']}", headers=ADMIN).json()["scope"] == "global"
    assert p["approval_id"] in {i["id"] for i in inbox(client, COMMITTEE)}


def test_domain_only_change_needs_domain_owner_and_committee(client):
    p = _propose(client, {"domains/sales.yaml": DOMAIN_FILE}, who=U("proposer", "owner", "sales"))
    req = client.get(f"{API}/approvals/{p['approval_id']}", headers=ADMIN).json()
    assert [(s["name"], s["domain"]) for s in req["steps"]] == [("owner:sales", "sales"), ("committee", None)]
    decide(client, p["approval_id"], COMMITTEE, code=403)  # まずドメインの Owner
    decide(client, p["approval_id"], U("other-owner", "owner", "finance"), code=403)
    assert p["approval_id"] in {i["id"] for i in inbox(client, SALES_OWNER)}
    assert decide(client, p["approval_id"], SALES_OWNER)["status"] == "pending"
    assert decide(client, p["approval_id"], COMMITTEE)["status"] == "approved"
    _publish(client, p["proposal_version"])
    assert "SALES-002" in {x["id"] for x in client.get(POL, headers=ADMIN).json()}


@pytest.mark.parametrize("who,changes_key,code", [
    (U("o", "owner", "sales"), "global", 403),  # Domain Owner は Global を提案できない
    (U("o", "owner", "*"), "global", 403),      # 全ドメインに所属していても同じ
    (U("o", "owner", "finance"), "domain", 403),  # 他のドメイン
    (U("s", "steward", "sales"), "domain", 403),
    (U("c", "custodian", "sales"), "domain", 403),
    (U("x"), "domain", 403),
    (ADMIN, "global", 202),
    (U("o", "owner", "*"), "domain", 202),
])
def test_propose_permissions(client, who, changes_key, code):
    changes = {"global.yaml": _global_with(client, NEW_POLICY)} if changes_key == "global" else {"domains/sales.yaml": DOMAIN_FILE}
    _propose(client, changes, who=who, code=code)


def test_mixed_change_is_global_and_file_removal_counts(client):
    p = _propose(client, {"global.yaml": _global_with(client, NEW_POLICY), "domains/sales.yaml": None}, who=ADMIN)
    assert client.get(f"{POL}/bundles/{p['proposal_version']}", headers=ADMIN).json()["scope"] == "global"
    domain_only = _propose(client, {"domains/sales.yaml": None}, who=U("o", "owner", "sales"))
    assert client.get(f"{POL}/bundles/{domain_only['proposal_version']}", headers=ADMIN).json()["domains"] == ["sales"]


def test_reload_permission_stays_with_committee_and_admin(client):
    assert client.post(f"{POL}/reload", headers=U("o", "owner", "*")).status_code == 403


# ---------------------------------------------------------------- 切り戻し
def test_rollback_needs_approval(client):
    a = asset(client)
    url = f"{POL}/bundles/rollback"
    assert client.post(url, headers=COMMITTEE, json={"reason": "r"}).status_code == 409  # 戻せる版が無い
    _approved_and_published(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    assert _results(client, a["id"])["TEST-001"] == "fail"
    assert client.post(url, headers=U("o", "owner", "*"), json={"reason": "r"}).status_code == 403
    assert client.post(url, headers=COMMITTEE, json={}).status_code == 422
    r = client.post(url, headers=CHAIR_PROPOSER, json={"reason": "誤検知が多い"})
    assert r.status_code == 202
    rb = r.json()
    got = client.get(f"{POL}/bundles/{rb['proposal_version']}", headers=ADMIN).json()
    assert got["rollback_of"] == 1 and got["status"] == "proposed" and got["files"] == client.get(f"{POL}/bundles/1", headers=ADMIN).json()["files"]
    # 承認されるまでは戻らない
    assert _results(client, a["id"])["TEST-001"] == "fail"
    _publish(client, rb["proposal_version"], code=409)
    decide(client, rb["approval_id"], COMMITTEE)
    assert _results(client, a["id"])["TEST-001"] == "fail"
    _publish(client, rb["proposal_version"])
    assert "TEST-001" not in _results(client, a["id"])
    assert _active(client)["version"] == rb["proposal_version"]


# ---------------------------------------------------------------- キャッシュ
def test_new_process_follows_published_bundle(client):
    """別のプロセスが公開しても、内容ハッシュで追従する。"""
    a = asset(client)
    _results(client, a["id"])
    _approved_and_published(client, {"global.yaml": _global_with(client, NEW_POLICY)})
    with engine.begin() as conn:  # 別のプロセスの公開を模して、DB だけ前の版に戻す
        conn.execute(text("UPDATE policy_bundles SET status = 'superseded' WHERE version = 2"))
        conn.execute(text("UPDATE policy_bundles SET status = 'active' WHERE version = 1"))
    assert "TEST-001" not in _results(client, a["id"])
    assert policy.get_policies()  # DB を指定しない経路も同じ


@pytest.mark.skipif(engine.dialect.name == "sqlite", reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")
def test_concurrent_publish_leaves_one_active(client):
    proposals = [_propose(client, {"global.yaml": _global_with(client, {**NEW_POLICY, "id": f"TEST-{i}"})}) for i in range(4)]
    for p in proposals:
        decide(client, p["approval_id"], COMMITTEE)
    barrier = threading.Barrier(len(proposals))

    def go(p):
        barrier.wait()
        return client.post(f"{POL}/bundles/{p['proposal_version']}/publish", headers=COMMITTEE).status_code

    with ThreadPoolExecutor(len(proposals)) as pool:
        codes = sorted(pool.map(go, proposals))
    assert codes == [200, 409, 409, 409]
    assert client.get(f"{POL}/bundles", headers=ADMIN, params={"status": "active"}).headers["X-Total-Count"] == "1"
