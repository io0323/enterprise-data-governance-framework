"""§16 Approval: 共通の承認箱 (inbox).

新しいエンジンの案件と、既存の個別の承認を、ApprovalSource の Protocol を通して 1 か所に集める。
既存の API は変えず、ここは「今この人が判断できるもの」を読むだけ。判断は各 API (item の action) で行う。

Phase 17: 各案件に operations (判断を送る先の既存の API と、入力の項目) を付ける。UI はこれを見て既存の API を呼ぶ。
認可と職務分離は各 API が判定するので、ここで UI 用の判定は作らない。却下のある API では、却下の理由を必須の項目にする。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import COMMITTEE, Principal
from edgf.enums import BundleStatus, ContractStatus, IssueStatus, RemediationStatus, SubscriptionStatus, TrainingApprovalStatus
from edgf.models import (ContractVersion, DataAsset, DataContract, DataProduct, DatasetVersion, Issue, MeshSubscription, ModelRemediation,
                         ModelVersion, OutputPort, PolicyBundle, PolicyException, RefDataSet, RefDataVersion, TrainingApproval, utcnow)
from edgf.services import ai_training, approvals, issues
from edgf.services.mdm_rules import approval_of, find_asset, get_entities


def field_(name: str, *, kind: str = "text", required: bool = False, max_length: int | None = 1000,
           options: Sequence[str] = ()) -> dict[str, Any]:
    """入力の項目。kind は text (複数行) / select / datetime。"""
    out: dict[str, Any] = {"name": name, "kind": kind, "required": required}
    if max_length is not None and kind == "text":
        out["max_length"] = max_length
    if options:
        out["options"] = list(options)
    return out


def operation(op_id: str, method: str, path: str, permission: str, *, body: dict[str, Any] | None = None,
              fields: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """判断の操作。step_up は承認行為 (✓) かどうかで、UI の経路では最近のログインが必要になる。"""
    return {"id": op_id, "method": method, "path": path, "body": body or {}, "fields": list(fields),
            "step_up": authz.PERMISSIONS[permission].approval}


def approve_reject(path: str, permission: str, *, approve_key: str = "approve", reason_key: str = "comment") -> tuple[dict, ...]:
    """承認と却下のある API。却下には理由を必須にする。"""
    return (operation("approve", "POST", path, permission, body={approve_key: True}, fields=[field_(reason_key)]),
            operation("reject", "POST", path, permission, body={approve_key: False}, fields=[field_(reason_key, required=True)]))


@dataclass(frozen=True)
class InboxItem:
    source: str
    id: str
    title: str
    action: str
    status: str = "pending"
    asset_id: str | None = None
    requested_by: str | None = None
    due_at: datetime | None = None
    created_at: datetime | None = None
    operations: tuple[dict[str, Any], ...] = ()


class ApprovalSource(Protocol):
    """承認の案件の出どころ。今この人が判断でき、かつ閲覧できる案件を返す。"""
    kind: str

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]: ...


class EngineSource:
    kind = "approval"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for req, asset in approvals.decidable_pending(db, principal):
            if asset is None or authz.can_read(db, principal, asset):
                path = f"/api/v1/approvals/{req.id}/decision"
                yield InboxItem(self.kind, req.id, f"{req.type}: {req.target_id}", f"POST {path}",
                                asset_id=req.asset_id, requested_by=req.requested_by, due_at=req.due_at, created_at=req.created_at,
                                operations=approve_reject(path, "approval.decide"))
        if authz.is_allowed(principal, "policy.publish"):
            # 承認を終えた Policy の変更は、公開して有効になる (§16 Policy)。公開も Committee の操作なので、ここに並べる
            for bundle in db.scalars(select(PolicyBundle).where(PolicyBundle.status == BundleStatus.APPROVED.value).order_by(PolicyBundle.version)):
                path = f"/api/v1/policies/bundles/{bundle.version}/publish"
                yield InboxItem(self.kind, f"policy_bundle:{bundle.version}", f"Policy の公開: v{bundle.version}", f"POST {path}",
                                status=bundle.status, requested_by=bundle.proposed_by, created_at=bundle.created_at,
                                operations=(operation("publish", "POST", path, "policy.publish"),))


class ExceptionSource:
    kind = "policy_exception"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        if not authz.is_allowed(principal, "exception.decide"):
            return
        for row in db.scalars(select(PolicyException).where(PolicyException.status == "pending", PolicyException.requested_by != principal.subject)):
            asset = db.get(DataAsset, row.asset_id)
            if authz.can_read(db, principal, asset):
                path = f"/api/v1/policies/exceptions/{row.id}/decision"
                yield InboxItem(self.kind, row.id, f"{row.policy_id}: {asset.qualified_name}", f"POST {path}", asset_id=asset.id,
                                requested_by=row.requested_by, created_at=row.created_at,
                                operations=approve_reject(path, "exception.decide"))


class MdmRuleSource:
    kind = "mdm_rule"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for entity in get_entities().values():
            asset = find_asset(db, entity)
            if asset is None or approval_of(db, entity) is not None or not authz.is_allowed(principal, "mdm.rule.approve", asset=asset):
                continue
            path = f"/api/v1/mdm/entities/{entity.entity_type}/approve"
            # 既存の API は承認だけ (却下の状態は無い。規則を直すと再承認になる)
            yield InboxItem(self.kind, entity.entity_type, f"生成規則: {entity.entity_type}", f"POST {path}", asset_id=asset.id,
                            operations=(operation("approve", "POST", path, "mdm.rule.approve"),))


class RefDataSource:
    kind = "reference_data"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(RefDataVersion, RefDataSet).join(RefDataSet, RefDataVersion.set_id == RefDataSet.id)
                          .where(RefDataVersion.status == "draft")).all()
        for version, data_set in rows:
            asset = db.get(DataAsset, data_set.asset_id)
            if authz.is_allowed(principal, "refdata.publish", asset=asset) and authz.can_read(db, principal, asset):
                path = f"/api/v1/reference-data/sets/{data_set.id}/versions/{version.version}/publish"
                yield InboxItem(self.kind, f"{data_set.id}@{version.version}", f"公開: {data_set.id}@{version.version}", f"POST {path}",
                                status="draft", asset_id=asset.id, requested_by=version.created_by, created_at=version.created_at,
                                operations=(operation("publish", "POST", path, "refdata.publish"),))


class TrainingSource:
    kind = "training_approval"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        for approval in db.scalars(select(TrainingApproval).where(TrainingApproval.status == TrainingApprovalStatus.PENDING.value)):
            version = db.get(DatasetVersion, approval.dataset_version_id)
            title = f"学習データ: {db.get(DataAsset, version.asset_id).qualified_name}@{version.version}"
            path = f"/api/v1/ai/training-approvals/{approval.id}/decision"
            action, ops = f"POST {path}", approve_reject(path, "ai.training.approve")
            slot = ai_training.owner_slot_for(db, approval, principal.subject)
            if slot is not None:  # Owner は自分の構成元の資産について判断する (AI 資産自体の当事者とは限らない)
                yield InboxItem(self.kind, approval.id, title, action, asset_id=slot.asset_ids[0],
                                requested_by=approval.requested_by, created_at=approval.requested_at, operations=ops)
            elif principal.has_role(COMMITTEE) and ai_training.committee_slot(approval) is not None \
                    and principal.subject != approval.requested_by \
                    and principal.subject not in {s.decided_by for s in approval.decisions if s.required_subject}:
                yield InboxItem(self.kind, approval.id, title, action, asset_id=version.asset_id,
                                requested_by=approval.requested_by, created_at=approval.requested_at, operations=ops)


class ContractSource:
    kind = "contract"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(ContractVersion, DataContract, DataProduct)
                          .join(DataContract, ContractVersion.contract_id == DataContract.id)
                          .join(DataProduct, DataContract.product_id == DataProduct.id)
                          .where(ContractVersion.status == ContractStatus.DRAFT.value, DataProduct.owner == principal.subject)).all()
        for version, contract, product in rows:
            asset = db.get(DataAsset, db.get(OutputPort, contract.output_port_id).asset_id)
            if authz.can_read(db, principal, asset):
                path = f"/api/v1/mesh/contract-versions/{version.id}/publish"
                yield InboxItem(self.kind, version.id, f"Contract の公開: {product.name} {version.version}", f"POST {path}",
                                status="draft", asset_id=asset.id, requested_by=version.created_by, created_at=version.created_at,
                                operations=(operation("publish", "POST", path, "mesh.contract.publish"),))


class SubscriptionSource:
    kind = "subscription"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        rows = db.execute(select(MeshSubscription, DataProduct).join(DataProduct, MeshSubscription.product_id == DataProduct.id)
                          .where(MeshSubscription.status == SubscriptionStatus.PENDING.value, DataProduct.owner == principal.subject,
                                 MeshSubscription.subscriber != principal.subject)).all()
        for row, product in rows:
            asset = db.get(DataAsset, db.get(OutputPort, row.output_port_id).asset_id)
            if authz.can_read(db, principal, asset):
                path = f"/api/v1/mesh/subscriptions/{row.id}/decision"
                yield InboxItem(self.kind, row.id, f"利用登録: {product.name} ({row.purpose_id})", f"POST {path}", asset_id=asset.id,
                                requested_by=row.subscriber, created_at=row.created_at,
                                operations=approve_reject(path, "mesh.subscription.approve"))


class CdmSource:
    """CDM の取り込みは Committee の操作そのものが承認で、承認待ちの状態を持たない。
    そのため、新しい版の再検証で不合格になった Contract の対応待ち (Owner の判断) をここに出す。
    """
    kind = "cdm"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        stmt = select(Issue).where(Issue.source_ref.like("mesh-cdm:%"), Issue.status.in_(issues.OPEN_STATES))
        if not principal.has_role(COMMITTEE):
            stmt = stmt.where(Issue.assignee == principal.subject)
        for issue in db.scalars(stmt):
            asset = db.get(DataAsset, issue.asset_id) if issue.asset_id else None
            if asset is None or authz.can_read(db, principal, asset):
                path = f"/api/v1/issues/{issue.id}"
                yield InboxItem(self.kind, issue.id, issue.title, f"PATCH {path}", status=issue.status,
                                asset_id=issue.asset_id, due_at=issue.due_at, created_at=issue.created_at,
                                operations=(issue_update_operation(path),))


class RemediationSource:
    kind = "model_remediation"

    def pending_for(self, db: Session, principal: Principal) -> Iterable[InboxItem]:
        if not authz.is_allowed(principal, "ai.remediation.decide"):
            return
        now = utcnow()
        for row in db.scalars(select(ModelRemediation).where(ModelRemediation.status.in_((RemediationStatus.PENDING.value, RemediationStatus.ACCEPTED.value)))):
            if row.status == RemediationStatus.ACCEPTED.value and row.accept_until > now:
                continue
            asset = db.get(DataAsset, row.asset_id)
            if authz.can_read(db, principal, asset):
                model = db.get(ModelVersion, row.model_version_id)
                path = f"/api/v1/ai/remediations/{row.id}/decision"
                yield InboxItem(self.kind, row.id, f"是正の判断: {asset.qualified_name}@{model.version}", f"POST {path}",
                                status=row.status, asset_id=asset.id, due_at=row.due_at, created_at=row.created_at,
                                operations=remediation_operations(path))


def issue_update_operation(path: str) -> dict[str, Any]:
    """Issue の状態と担当者の更新 (PATCH /issues/{id})。CDM の再検証の不合格の対応と、Issue の画面で共通。"""
    return operation("update", "PATCH", path, "issue.update", fields=[
        field_("status", kind="select", options=[s.value for s in IssueStatus]), field_("assignee", max_length=200), field_("comment")])


def remediation_operations(path: str) -> tuple[dict[str, Any], ...]:
    """是正の判断は retrain / unlearn / accept の 3 択 (却下は無い)。accept は理由と期限が必須。"""
    return (operation("retrain", "POST", path, "ai.remediation.decide", body={"decision": "retrain"}, fields=[field_("reason")]),
            operation("unlearn", "POST", path, "ai.remediation.decide", body={"decision": "unlearn"}, fields=[field_("reason")]),
            operation("accept", "POST", path, "ai.remediation.decide", body={"decision": "accept"},
                      fields=[field_("reason", required=True), field_("accept_until", kind="datetime", required=True)]))


SOURCES: Sequence[ApprovalSource] = (EngineSource(), ExceptionSource(), MdmRuleSource(), RefDataSource(), TrainingSource(),
                                     ContractSource(), SubscriptionSource(), CdmSource(), RemediationSource())


def inbox(db: Session, principal: Principal, source: str | None = None) -> list[dict[str, Any]]:
    """期限の近いものから返す (期限の無い案件は後ろ)。"""
    items = [item for s in SOURCES if source in (None, s.kind) for item in s.pending_for(db, principal)]
    items.sort(key=lambda i: (i.due_at is None, i.due_at or datetime.max, i.created_at or datetime.max, i.source, i.id))
    return [asdict(i) for i in items]
