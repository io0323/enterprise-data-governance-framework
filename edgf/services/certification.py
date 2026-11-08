"""§7 Certification gate: Certified = Owner/Steward + 品質SLA充足 + 高重大度ポリシー準拠."""
from __future__ import annotations

from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.models import DataAsset


def certification_blockers(db: Session, asset: DataAsset) -> list[str]:
    from edgf.services.policy import evaluate_asset
    from edgf.services.quality import latest_run

    blockers: list[str] = []
    if not asset.owner or not asset.steward:
        blockers.append("Owner/Stewardが未割当です (§4)")
    if asset.lifecycle_state != "active":
        blockers.append(f"lifecycle_stateがactiveではありません: {asset.lifecycle_state}")
    run = latest_run(db, asset.id)
    if run is None:
        blockers.append("品質検査の実行実績がありません (§5)")
    elif run.score < settings.certification_min_quality:
        blockers.append(f"Quality Score {run.score:.4f} < 基準 {settings.certification_min_quality} (§5)")
    for r in evaluate_asset(db, asset):
        if r["status"] == "fail" and r["severity"] in ("high", "critical") and not r["policy_id"].startswith("CAT-"):
            blockers.append(f"ポリシー違反 {r['policy_id']}: {r['message']}")
    return blockers
