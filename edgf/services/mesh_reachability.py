"""§15 Addressable: Output Port の到達の記録と MESH-007 の判定材料."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ProductStatus
from edgf.models import DataProduct, OutputPort, ReachabilityReport, to_naive_utc, utcnow
from edgf.services import audit, probe

PROBE_ACTOR = "system:probe"


def record(db: Session, actor: str, port: OutputPort, *, reachable: bool, method: str, detail: str = "", observed_at: datetime | None = None) -> ReachabilityReport:
    now = utcnow()
    observed = min(to_naive_utc(observed_at), now) if observed_at else now  # 未来の時刻で有効期間を延ばさせない
    report = ReachabilityReport(port_id=port.id, reachable=reachable, method=method, observed_at=observed, detail=detail, reporter=actor)
    db.add(report)
    db.flush()
    audit.record(db, actor, "mesh.port.reachability", "output_port", port.id, {"reachable": reachable, "method": method, "detail": detail})
    return report


def latest(db: Session, port_ids: Sequence[str]) -> dict[str, ReachabilityReport]:
    if not port_ids:
        return {}
    newest = select(ReachabilityReport.port_id, func.max(ReachabilityReport.observed_at).label("at")) \
        .where(ReachabilityReport.port_id.in_(list(port_ids))).group_by(ReachabilityReport.port_id).subquery()
    rows = db.scalars(select(ReachabilityReport).join(newest, (ReachabilityReport.port_id == newest.c.port_id)
                                                      & (ReachabilityReport.observed_at == newest.c.at)))
    return {r.port_id: r for r in rows}


def problem(report: ReachabilityReport | None, now: datetime | None = None) -> str | None:
    """MESH-007 の不合格の理由 (合格なら None)。"""
    if report is None:
        return "到達の報告がありません"
    if not report.reachable:
        return f"到達できません ({report.detail or report.method})"
    if (now or utcnow()) - report.observed_at > timedelta(hours=settings.reachability_max_age_hours):
        return f"到達の報告が {settings.reachability_max_age_hours} 時間より古いです"
    return None


def probe_all(db: Session, actor: str = PROBE_ACTOR) -> dict[str, int]:
    """公開済みと非推奨の Product のポートを確かめる (EDGF_PROBE_ENABLED のときだけ。無効なら外部に接続しない)。"""
    if not settings.probe_enabled:
        return {"probed": 0, "refused": 0}
    ports = db.scalars(select(OutputPort).join(DataProduct, OutputPort.product_id == DataProduct.id)
                       .where(DataProduct.status.in_([ProductStatus.PUBLISHED.value, ProductStatus.DEPRECATED.value]))).all()
    probed = refused = 0
    for port in ports:
        try:
            result = probe.probe(port.address_uri)
        except probe.ProbeRefused as e:
            refused += 1
            record(db, actor, port, reachable=False, method="probe", detail=f"refused:{e.code}")
            continue
        probed += 1
        record(db, actor, port, reachable=result.reachable, method="probe", detail=result.detail)
    return {"probed": probed, "refused": refused}
