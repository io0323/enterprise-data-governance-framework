"""資産のメタデータの更新 (PATCH /assets/{id} と、Discovery によるスキーマの反映で共通の経路).

権限の判定は呼び出し側で行う。ここでは、更新に伴う統制 (匿名化の扱い、Certification のリセット、監査、
AI 資産の統制属性の継承、Contract とのずれの検出) をまとめて行う。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from edgf.enums import Certification
from edgf.models import DataAsset
from edgf.services import ai_inherit, audit, mesh_contracts, privacy

# 下流の AI 資産の実効値に影響する項目 (§14)
INHERITANCE_FIELDS = {"sensitivity", "contains_pii", "schema_", "privacy_technique", "privacy_params", "data_class", "ai_kind"}
CONTRACT_DRIFT_FIELDS = {"schema_", "sensitivity"}
RECERTIFY_FIELDS = {"schema_", "data_class", "sensitivity", "privacy_technique"}


def apply(db: Session, asset: DataAsset, changes: dict[str, Any], actor: str, *, was_anonymized: bool | None = None) -> dict[str, Any]:
    """変更を反映し、監査に記録した内容 (Certification のリセットなどを含む) を返す。"""
    if was_anonymized is None:
        was_anonymized = privacy.is_anonymized(asset.privacy_technique, asset.privacy_params)
    for k, v in changes.items():
        setattr(asset, k, v)
    if "schema_" in changes and not asset.is_cde and any(c.get("cde") for c in asset.schema_ or []):
        asset.is_cde = changes["is_cde"] = True  # is_cde は「CDE のカラムを含む」(Phase 16)
    if set(changes) & {"schema_", "contains_pii", "privacy_technique", "privacy_params"}:
        # 基準を満たす匿名化でなくなったら、個人データとして扱いに戻す (§12)
        lost_anonymity = was_anonymized and not privacy.is_anonymized(asset.privacy_technique, asset.privacy_params)
        declared = asset.contains_pii or lost_anonymity
        effective = privacy.effective_contains_pii(declared, asset.schema_, asset.privacy_technique, asset.privacy_params)
        if effective != asset.contains_pii:
            asset.contains_pii = effective
            changes["contains_pii"] = effective
    # 認証済み資産の構造・分類変更は再認証を要求する
    if asset.certification == Certification.CERTIFIED.value and set(changes) & RECERTIFY_FIELDS:
        asset.certification = Certification.DRAFT.value
        changes["certification_reset"] = True
    db.flush()
    audit.record(db, actor, "asset.update", "asset", asset.id, changes)
    if set(changes) & INHERITANCE_FIELDS:
        ai_inherit.refresh(db, actor, [asset.qualified_name])
    if set(changes) & CONTRACT_DRIFT_FIELDS:  # Contract とずれても更新は拒否しない。Issue で知らせる (§15)
        mesh_contracts.check_drift(db, actor, asset)
    return changes
