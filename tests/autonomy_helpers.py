"""Phase 19 のテストの共通部品."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import yaml
from sqlalchemy import func, select

from edgf.db import SessionLocal
from edgf.models import AccessGrant, AuditEvent, RemediationRun, utcnow
from edgf.services import autonomy, policy_bundles
from edgf.services.autonomy_catalog import PLAYBOOK_FILE
from tests.process_helpers import ADMIN, API, U

AUT = f"{API}/autonomy"
CHAIR = U("chair", "committee")


def playbook(pid: str, *, trigger: dict, action: str, category: str = "A", mode: str = "active", limit: int = 50,
             undo_days: int | None = 30, fp: float | None = 0.5, condition: dict | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": pid, "trigger": trigger, "action": action, "category": category, "mode": mode,
                            "max_actions_per_run": limit}
    if undo_days is not None:
        item["undo_days"] = undo_days
    if fp is not None:
        item["false_positive_threshold"] = fp
    if condition is not None:
        item["condition"] = condition
    return item


def stale(mode="active", **kw) -> dict:
    return playbook("PB-STALE", trigger={"detector": "stale_access"}, action="access.revoke_stale", mode=mode, **kw)


def recovered(mode="active", **kw) -> dict:
    return playbook("PB-RECOVERED", trigger={"detector": "quality_recovered"}, action="issue.close_recovered", mode=mode,
                    condition={"consecutive_passes": 3}, **kw)


def owners(mode="active", **kw) -> dict:
    return playbook("PB-OWNER", trigger={"detector": "unowned_assets"}, action="propose.owner_assignment", category="B", mode=mode,
                    undo_days=None, fp=None, **kw)


def use_playbooks(*items: dict) -> None:
    """active の Bundle のプレイブックを差し替える (承認と公開の流れは test_autonomy_catalog で確かめている)。"""
    with SessionLocal() as db:
        bundle = policy_bundles.active_bundle(db) or policy_bundles.bootstrap(db)
        bundle.files = {**bundle.files, PLAYBOOK_FILE: yaml.safe_dump({"playbooks": list(items)}, allow_unicode=True)}
        db.commit()


def run_now(now=None) -> dict:
    with SessionLocal() as db:
        result = autonomy.run(db, now=now)
        db.commit()
        return result


def runs(**where) -> list[RemediationRun]:
    with SessionLocal() as db:
        stmt = select(RemediationRun).order_by(RemediationRun.created_at, RemediationRun.id)
        for k, v in where.items():
            stmt = stmt.where(getattr(RemediationRun, k) == v)
        return db.scalars(stmt).all()


def grant(asset_id: str, subject: str = "alice", *, expired: bool = True) -> str:
    with SessionLocal() as db:
        row = AccessGrant(asset_id=asset_id, subject=subject, purpose="分析", granted_by="asset-owner",
                          expires_at=utcnow() + timedelta(days=-1 if expired else 30))
        db.add(row)
        db.commit()
        return row.id


def grant_row(grant_id: str) -> AccessGrant:
    with SessionLocal() as db:
        return db.get(AccessGrant, grant_id)


def audits(action: str) -> list[AuditEvent]:
    with SessionLocal() as db:
        return db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq)).all()


def audit_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(AuditEvent))


def domain(client, name="sales", owner="dom-owner"):
    r = client.post(f"{API}/domains", headers=ADMIN, json={"name": name, "owner": owner})
    assert r.status_code == 201, r.text


AUTONOMY_TABLES = {"remediation_runs", "autonomy_control", "autonomy_playbook_states", "job_runs", "job_leases"}


def governance_state() -> list[str]:
    """自律統治の記録 (と、ジョブの実行の記録) 以外の全テーブルの全行。dry_run で何も変わらないことの確認に使う。"""
    from edgf.db import Base, engine

    with engine.connect() as conn:
        return [f"{t.name}:{tuple(row)}" for t in Base.metadata.sorted_tables if t.name not in AUTONOMY_TABLES
                for row in conn.execute(t.select())]
