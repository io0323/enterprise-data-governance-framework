"""Phase 18 のテストの共通部品."""
from __future__ import annotations

from datetime import date, timedelta

from edgf.db import SessionLocal
from edgf.models import KpiSnapshot, MaturityAssessment, utcnow
from tests.process_helpers import ADMIN, API, U

MAT = f"{API}/maturity"
CHAIR = U("chair", "committee")
# 各レベルの自動の指標を満たす値 (初期の基準)
LEVEL1 = {"owner_assignment_rate": 0.5}
LEVEL2 = {"owner_assignment_rate": 0.8, "quality_score_avg": 0.9}
LEVEL3 = {**LEVEL2, "catalog_registration_rate": 0.95, "compliance_rate": 0.9}
LEVEL4 = {**LEVEL3, "certified_rate": 0.5, "compliance_rate": 0.98}


def domain(client, name="sales", owner="dom-owner"):
    r = client.post(f"{API}/domains", headers=ADMIN, json={"name": name, "owner": owner})
    assert r.status_code == 201, r.text


def snapshots(name: str, metrics: dict, *, days: int = 1, end: date | None = None, skip: tuple[int, ...] = (),
              override: dict[int, dict] | None = None) -> list[int]:
    """end から過去 days 日分のドメイン別のスナップショットを入れる。skip は欠けさせる日 (end からの日数)。"""
    end = end or utcnow().date()
    ids = []
    with SessionLocal() as db:
        for offset in range(days):
            if offset in skip:
                continue
            row = KpiSnapshot(day=end - timedelta(days=offset), scope_type="domain", scope_name=name,
                              metrics={**metrics, **((override or {}).get(offset, {}))})
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
    return ids


def create(client, name="sales", who=CHAIR) -> dict:
    r = client.post(f"{MAT}/assessments", headers=who, json={"domain": name})
    assert r.status_code == 201, r.text
    return r.json()


def evaluate(client, assessment_id, who=CHAIR) -> dict:
    r = client.post(f"{MAT}/assessments/{assessment_id}/evaluate", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def decided(name: str, level: int) -> str:
    """前回の判定 (テストの前提を作るために DB に直接入れる)。"""
    now = utcnow()
    with SessionLocal() as db:
        row = MaturityAssessment(domain=name, cycle=str(now.year - 1), status="decided", created_by="chair", created_at=now - timedelta(days=400),
                                 due_at=now, criteria={}, auto_results=[], self_assessment=[], decision={"level": level}, cancel_reason="",
                                 decided_level=level, decided_by="chair", decided_at=now - timedelta(days=300))
        db.add(row)
        db.commit()
        return row.id
