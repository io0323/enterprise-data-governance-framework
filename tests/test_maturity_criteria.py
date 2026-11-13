"""Phase 18a: 成熟度の評価の基準の定義、ロード時の検証、Policy Bundle への組み込み."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.services import kpi, maturity_criteria
from edgf.services.maturity_criteria import CRITERIA_FILE, CriteriaInvalid, parse
from tests.process_helpers import API, COMMITTEE, U, decide

POL = f"{API}/policies"
DISK = (Path(settings.policy_dir) / CRITERIA_FILE).read_text(encoding="utf-8")


def _doc(**level_overrides) -> dict:
    doc = yaml.safe_load(DISK)
    for number, changes in level_overrides.items():
        doc["levels"][int(number[1:]) - 1].update(changes)
    return doc


def test_initial_criteria_cover_levels_1_to_5():
    criteria = parse(DISK)
    assert [lv.level for lv in criteria.levels] == [1, 2, 3, 4, 5]
    assert [lv.name for lv in criteria.levels] == ["Basic Policy", "Managed", "Standardized", "Federated Governance", "Autonomous Governance"]
    assert all(a.days >= 90 for lv in criteria.levels if lv.level >= 4 for a in lv.automatic)
    assert not any(a.measurable for a in criteria.level(5).automatic)  # Phase 19 まで計測できない
    assert all(lv.manual for lv in criteria.levels)


def test_thresholds_match_the_existing_indicator():
    """自動の指標は GET /kpi の maturity (目安) の閾値と一致させる。"""
    criteria = parse(DISK)
    for number, conditions in kpi.LEVEL_CONDITIONS.items():
        auto = {(a.metric, None if a.op == "measured" else a.threshold) for a in criteria.level(number).automatic}
        assert auto == set(conditions), number


def test_kpi_metric_names_match_compute(client):
    with SessionLocal() as db:
        assert maturity_criteria.KPI_METRICS == set(kpi.compute(db, None)) - {"scope"}


@pytest.mark.parametrize("change,message", [
    ({"L1": {"automatic": [{"id": "X", "metric": "no_such_kpi", "op": ">=", "threshold": 1}]}}, "存在しない KPI"),
    ({"L1": {"level": 2}}, "level は 1"),
    ({"L4": {"automatic": [{"id": "X", "metric": "certified_rate", "op": ">=", "threshold": 0.5}]}}, "days"),
    ({"L4": {"automatic": [{"id": "X", "metric": "certified_rate", "op": ">=", "threshold": 0.5, "days": 0}]}}, "days"),
    ({"L5": {"automatic": [{"id": "X", "metric": "auto_remediation_rate", "op": ">=", "threshold": 0.5}]}}, "days"),
    ({"L1": {"automatic": [{"id": "X", "metric": "certified_rate", "op": "==", "threshold": 1}]}}, "op"),
    ({"L1": {"automatic": [{"id": "X", "metric": "certified_rate", "op": ">="}]}}, "threshold"),
    ({"L1": {"automatic": [{"id": "X", "metric": "certified_rate", "op": "measured", "threshold": 1}]}}, "measured"),
    ({"L1": {"automatic": [{"id": "L2-CDE", "metric": "certified_rate", "op": ">=", "threshold": 1}]}}, "重複"),
    ({"L1": {"automatic": [], "manual": []}}, "基準がありません"),
    ({"L1": {"extra": 1}}, "使えないキー"),
    ({"L2": {"manual": [{"id": "M", "description": "d"}]}}, "evidence"),
])
def test_invalid_criteria_are_rejected(change, message):
    with pytest.raises(CriteriaInvalid, match=message):
        parse(yaml.safe_dump(_doc(**change), allow_unicode=True))


def test_level_order_and_count():
    doc = _doc()
    with pytest.raises(CriteriaInvalid, match="level は 3"):
        parse(yaml.safe_dump({"levels": [doc["levels"][0], doc["levels"][1], doc["levels"][3]]}, allow_unicode=True))
    with pytest.raises(CriteriaInvalid, match="5 まで"):
        parse(yaml.safe_dump({"levels": doc["levels"] + [{**doc["levels"][4], "level": 6, "name": "x", "automatic": [],
                                                           "manual": [{"id": "Z", "description": "d", "evidence": "e"}]}]}, allow_unicode=True))
    for bad in ("[1]", "levels: {}", "other: 1", ": : :"):
        with pytest.raises(CriteriaInvalid):
            parse(bad)


# ---------------------------------------------------------------- Policy Bundle
def _active_criteria():
    with SessionLocal() as db:
        return maturity_criteria.active(db)


def test_criteria_are_part_of_the_bundle(client):
    active = client.get(f"{POL}/bundles", headers=U("root", "admin"), params={"status": "active"}).json()[0]
    files = client.get(f"{POL}/bundles/{active['version']}", headers=U("root", "admin")).json()["files"]
    assert CRITERIA_FILE in files and _active_criteria().digest == parse(files[CRITERIA_FILE]).digest


def test_change_is_reflected_only_after_approval(client):
    doc = _doc(L1={"automatic": [{"id": "L1-OWNER-RATE", "metric": "owner_assignment_rate", "op": ">=", "threshold": 0.7}]})
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"),
                    json={"changes": {CRITERIA_FILE: yaml.safe_dump(doc, allow_unicode=True)}, "reason": "Level 1 を厳しくする"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    req = client.get(f"{API}/approvals/{proposal['approval_id']}", headers=COMMITTEE).json()
    assert [s["name"] for s in req["steps"]] == ["committee"]
    assert _active_criteria().level(1).automatic[0].threshold == 0.5  # 承認前は従来の基準
    decide(client, proposal["approval_id"], U("vice-chair", "committee"))
    assert _active_criteria().level(1).automatic[0].threshold == 0.5  # 公開前も同じ
    assert client.post(f"{POL}/bundles/{proposal['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    assert _active_criteria().level(1).automatic[0].threshold == 0.7


def test_invalid_criteria_proposal_is_422(client):
    doc = _doc(L4={"automatic": [{"id": "X", "metric": "certified_rate", "op": ">=", "threshold": 0.5}]})
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"),
                    json={"changes": {CRITERIA_FILE: yaml.safe_dump(doc, allow_unicode=True)}, "reason": "x"})
    assert r.status_code == 422 and "days" in r.text
    r = client.post(f"{POL}/bundles", headers=U("proposer", "committee"), json={"changes": {"maturity/other.yaml": "levels: []"}, "reason": "x"})
    assert r.status_code == 422


def test_no_bundle_file_means_no_criteria(client):
    """既存の環境で基準を承認する前は、基準は空 (目安は 0)。"""
    with SessionLocal() as db:
        from edgf.services import policy_bundles
        bundle = policy_bundles.active_bundle(db)
        bundle.files = {k: v for k, v in bundle.files.items() if k != CRITERIA_FILE}
        db.commit()
    assert _active_criteria().levels == ()
