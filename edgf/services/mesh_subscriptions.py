"""§15 / §4 Data Consumer: 利用登録.

利用登録は「誰がどの目的で使っているか」の統治の記録で、閲覧権限は与えない。
ABAC と restricted の閲覧承認 (AccessGrant) は、これまで通り資産ごとに判定する。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import ContractStatus, EscalationLevel, ProductStatus, SubscriptionStatus
from edgf.models import (ContractVersion, DataAsset, DataContract, DataProduct, MeshSubscription, OutputPort, PrivacyPurpose,
                         fit_identifier, utcnow)
from edgf.services import audit, issues, lineage
from edgf.services.mesh import MeshStateError, MeshValidationError

S = SubscriptionStatus
VALID = (S.PENDING.value, S.ACTIVE.value)  # 「有効な利用登録」(retire を止める)
OPEN = (*VALID, S.SUSPENDED.value)  # 同じ利用者の重複を認めない
APPROVAL_FROM = SENSITIVITY_LEVELS.index("confidential")


def _level(sensitivity: str) -> int:
    return SENSITIVITY_LEVELS.index(sensitivity)


def valid_count(db: Session, contract_id: str, major: int) -> int:
    return db.scalar(select(func.count()).select_from(MeshSubscription).where(
        MeshSubscription.contract_id == contract_id, MeshSubscription.major == major, MeshSubscription.status.in_(VALID))) or 0


def subscribers_of(db: Session, contract_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(MeshSubscription).where(MeshSubscription.contract_id == contract_id, MeshSubscription.status.in_(VALID))
                      .order_by(MeshSubscription.subscriber)).all()
    return [{"subscriber": r.subscriber, "major": r.major, "purpose_id": r.purpose_id, "status": r.status} for r in rows]


def _latest_of_major(db: Session, contract_id: str, major: int) -> ContractVersion | None:
    return db.scalar(select(ContractVersion).where(ContractVersion.contract_id == contract_id, ContractVersion.major == major,
                                                   ContractVersion.status == ContractStatus.PUBLISHED.value)
                     .order_by(ContractVersion.minor.desc(), ContractVersion.patch.desc()).limit(1))


def _current_major(db: Session, contract_id: str) -> int | None:
    return db.scalar(select(func.max(ContractVersion.major)).where(ContractVersion.contract_id == contract_id,
                                                                   ContractVersion.status == ContractStatus.PUBLISHED.value))


def _check_purpose(db: Session, purpose_id: str, version: ContractVersion) -> None:
    purpose = db.get(PrivacyPurpose, purpose_id)
    if purpose is None:
        raise MeshValidationError(f"unknown purpose: {purpose_id}")
    if purpose.withdrawn_at:
        raise MeshValidationError(f"目的 {purpose_id} は撤回されています")
    if version.purposes and purpose_id not in version.purposes:
        raise MeshValidationError(f"目的 {purpose_id} は Contract {version.version} の利用条件にありません: {version.purposes}")


def _needs_approval(asset: DataAsset, version: ContractVersion) -> bool:
    return max(_level(authz.effective_sensitivity(asset)), _level(version.sensitivity)) >= APPROVAL_FROM


def request(db: Session, *, contract: DataContract, port: OutputPort, product: DataProduct, asset: DataAsset, subscriber: str,
            purpose_id: str, major: int | None) -> MeshSubscription:
    if product.status != ProductStatus.PUBLISHED.value:
        raise MeshStateError(f"状態が {product.status} の Data Product には利用登録できません")
    target = major if major is not None else _current_major(db, contract.id)
    version = _latest_of_major(db, contract.id, target) if target is not None else None
    if version is None:
        raise MeshStateError("利用登録できるメジャー版がありません (公開済みで deprecated でない版が必要です)")
    _check_purpose(db, purpose_id, version)
    if db.scalar(select(MeshSubscription.id).where(MeshSubscription.subscriber == subscriber, MeshSubscription.output_port_id == port.id,
                                                   MeshSubscription.status.in_(OPEN))):
        raise MeshStateError("この Output Port への利用登録が既にあります")
    status = S.PENDING.value if _needs_approval(asset, version) else S.ACTIVE.value
    row = MeshSubscription(subscriber=subscriber, product_id=product.id, output_port_id=port.id, contract_id=contract.id,
                           major=version.major, purpose_id=purpose_id, status=status)
    db.add(row)
    db.flush()
    return row


def decide(db: Session, row: MeshSubscription, *, approve: bool, comment: str, actor: str) -> None:
    if row.status != S.PENDING.value:
        raise MeshStateError(f"状態が {row.status} の利用登録は決裁できません")
    row.status = S.ACTIVE.value if approve else S.REJECTED.value
    row.decided_by, row.decided_at, row.decision_comment = actor, utcnow(), comment
    db.flush()


def cancel(db: Session, row: MeshSubscription) -> None:
    if row.status not in OPEN:
        raise MeshStateError(f"状態が {row.status} の利用登録は取り消せません")
    row.status = S.CANCELLED.value
    db.flush()


def migrate(db: Session, row: MeshSubscription, *, contract: DataContract, asset: DataAsset, major: int, purpose_id: str | None) -> None:
    """メジャー版を付け替える。承認済みでも、機密度が上がるなら承認を取り直す。止まっていた登録もここで再開する。"""
    if row.status not in OPEN:
        raise MeshStateError(f"状態が {row.status} の利用登録は付け替えできません")
    target = _latest_of_major(db, contract.id, major)
    if target is None:
        raise MeshStateError(f"メジャー版 {major} には付け替えできません (公開済みで deprecated でない版が必要です)")
    purpose = purpose_id or row.purpose_id
    _check_purpose(db, purpose, target)
    current = db.scalar(select(ContractVersion).where(ContractVersion.contract_id == contract.id, ContractVersion.major == row.major)
                        .order_by(ContractVersion.minor.desc(), ContractVersion.patch.desc()).limit(1))
    raised = current is not None and _level(target.sensitivity) > _level(current.sensitivity)
    needs = _needs_approval(asset, target)
    if needs and (raised or row.decided_by is None):
        row.status, row.decided_by, row.decided_at = S.PENDING.value, None, None
    else:
        row.status = S.ACTIVE.value
    row.major, row.purpose_id, row.suspended_reason = target.major, purpose, None
    db.flush()


def _notify_subscriber(db: Session, actor: str, row: MeshSubscription, ref: str, title: str, description: str) -> str | None:
    """利用者の Issue は資産に紐付けない。閲覧権限が無い資産に紐付けると、本人が Issue を読めないため。"""
    issue = issues.open_issue(db, actor, asset=None, kind="contract", severity="high", source_ref=fit_identifier(ref, 100),
                              level=EscalationLevel.OWNER, title=title, description=description)
    if issue is None:
        return None
    issue.assignee = row.subscriber
    db.flush()
    return issue.id


def _suspend(db: Session, actor: str, row: MeshSubscription, reason: str, ref: str, title: str) -> None:
    row.status, row.suspended_reason = S.SUSPENDED.value, reason
    audit.record(db, actor, "mesh.subscription.suspend", "subscription", row.id, {"reason": reason, "purpose_id": row.purpose_id})
    _notify_subscriber(db, actor, row, ref, title, reason)


def suspend_for_purpose(db: Session, actor: str, purpose: PrivacyPurpose) -> list[str]:
    """目的そのものが撤回されたら、その目的の利用登録を止める。"""
    rows = db.scalars(select(MeshSubscription).where(MeshSubscription.purpose_id == purpose.id, MeshSubscription.status.in_(VALID))).all()
    for row in rows:
        product = db.get(DataProduct, row.product_id)
        _suspend(db, actor, row, f"目的 {purpose.id} が撤回されました", f"mesh-sub:{row.id}:purpose",
                 f"[SUBSCRIPTION] {product.name}: 目的 {purpose.id} の撤回により利用登録を停止しました")
    db.flush()
    return [r.id for r in rows]


def suspend_for_narrowed_usage(db: Session, actor: str, contract: DataContract, version: ContractVersion, product: DataProduct) -> list[str]:
    """利用条件の縮小に合わせて、新しい利用条件から外れた目的の利用登録を止める。"""
    if not version.purposes:
        return []
    rows = db.scalars(select(MeshSubscription).where(MeshSubscription.contract_id == contract.id, MeshSubscription.status.in_(VALID),
                                                     MeshSubscription.purpose_id.not_in(version.purposes))).all()
    for row in rows:
        _suspend(db, actor, row, f"Contract {version.version} の利用条件に目的 {row.purpose_id} がありません",
                 f"mesh-sub:{row.id}:usage", f"[SUBSCRIPTION] {product.name}: 利用条件の縮小により利用登録を停止しました")
    db.flush()
    return [r.id for r in rows]


def valid_for_product(db: Session, product_id: str) -> list[MeshSubscription]:
    return list(db.scalars(select(MeshSubscription).where(MeshSubscription.product_id == product_id, MeshSubscription.status.in_(VALID))))


def notify_product_deprecated(db: Session, actor: str, product: DataProduct) -> list[str]:
    opened = []
    for row in valid_for_product(db, product.id):
        issue_id = _notify_subscriber(db, actor, row, f"mesh-product:{product.id}:{row.id}",
                                      f"[PRODUCT] {product.name}: Data Product が非推奨になりました",
                                      "代替の Data Product に移行し、利用登録を取り消してください (§15)。")
        if issue_id:
            opened.append(issue_id)
    return opened


def notify_new_major(db: Session, actor: str, contract: DataContract, version: ContractVersion, product: DataProduct, asset: DataAsset,
                     sunset: datetime) -> list[str]:
    """旧版の利用者と下流の Owner に、新しいメジャー版と併存期間の満了を知らせる。"""
    message = f"Contract {version.version} を公開しました。旧版は {sunset.date().isoformat()} に満了します。移行してください (§15)。"
    opened = []
    rows = db.scalars(select(MeshSubscription).where(MeshSubscription.contract_id == contract.id, MeshSubscription.status.in_(VALID),
                                                     MeshSubscription.major < version.major)).all()
    for row in rows:
        issue_id = _notify_subscriber(db, actor, row, f"mesh-major:{version.id}:{row.id}",
                                      f"[CONTRACT] {product.name}: 新しいメジャー版 {version.version} への移行が必要です", message)
        if issue_id:
            opened.append(issue_id)
    impact = lineage.impact_analysis(db, asset.qualified_name)
    for item in impact["impacted"]:
        if not item["registered"] or not item["owner"]:
            continue
        issue = issues.open_issue(
            db, actor, asset=db.get(DataAsset, item["asset_id"]), kind="contract", severity="high",
            source_ref=f"mesh-major:{version.id}", level=EscalationLevel.OWNER,
            title=f"[CONTRACT] {item['qualified_name']}: 上流の {asset.qualified_name} に新しいメジャー版 {version.version} が公開されました",
            description=message)
        if issue:
            opened.append(issue.id)
    return opened
