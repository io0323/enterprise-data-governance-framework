"""§19 Availability: liveness (/health) と readiness (/ready) の分離.

- /health: プロセスが応答できるか (依存先を見ない。再起動判定用)
- /ready : トラフィックを受けられるか (DB 疎通・スキーマが最新・ポリシー構文が有効)。不可なら 503
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

from edgf import __version__, db, jobs, migrate
from edgf.errors import ProblemError
from edgf.services.mdm_rules import get_entities
from edgf.services.policy import get_policies
from edgf.services.policy_bundles import drift_message

router = APIRouter(tags=["ops"])
log = logging.getLogger("edgf.ops")


@router.get("/health")
def health(request: Request):
    # auth_mode を返し、dev モードでの稼働を外形監視で検知できるようにする
    return {"status": "ok", "version": __version__, "auth_mode": request.app.state.auth_mode}


@router.get("/ready")
def ready():
    checks: dict[str, str] = {}
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            current, head = migrate.current_revision(conn), migrate.head_revision()
            checks["schema"] = "ok" if current == head else f"outdated (current={current}, head={head})"
    except Exception as e:  # noqa: BLE001 - 依存先の障害種別を問わず not ready とする
        log.warning("readiness check failed: database", extra={"fields": {"error": type(e).__name__}})
        checks["database"] = "unavailable"
        checks.setdefault("schema", "unknown")
    try:
        get_policies()
        get_entities()
        checks["policies"] = "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("readiness check failed: policies", extra={"fields": {"error": str(e)}})
        checks["policies"] = "invalid"
    failed = any(v != "ok" for v in checks.values())
    # 503 にしない注意事項。該当するときだけ checks に現れる
    drift = drift_message()
    if drift:
        checks["policy_drift"] = drift
    overdue = jobs.overdue_jobs()
    if overdue:
        checks["jobs_overdue"] = ", ".join(overdue)
    if failed:
        raise ProblemError(503, "依存先が利用できないため、リクエストを受け付けられません", type_="not-ready", checks=checks)
    return {"status": "ready", "version": __version__, "checks": checks}
