"""§12 Data Privacy API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_asset_or_404, get_principal
from edgf.enums import ConsentStatus, LegalBasis, LifecycleState, PiiCategory
from edgf.models import Consent, DataAsset, Domain, ErasureRequest, ErasureTask, PrivacyPurpose, to_naive_utc, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import (ConsentCheckIn, ConsentIn, ConsentOut, ConsentRecorded, ErasureRejectIn, ErasureRequestDetail,
                          ErasureRequestIn, ErasureRequestOut, ErasureTaskCompleteIn, ErasureTaskOut, ErasureWorkItem,
                          PurposeIn, PurposeOut, PurposeUpdate, PurposeWithdrawIn, SubjectIdentifier)
from edgf.services import audit, consent, erasure, mesh_subscriptions, privacy
from edgf.services.erasure import ErasureStateError
from edgf.services.subject_ref import to_subject_ref

router = APIRouter(prefix="/privacy", tags=["privacy"])


# ---------------- PII Inventory (§12 PII) ----------------
def _inventory(db: Session, principal: Principal, domain: str | None, category: PiiCategory | None,
               purpose: str | None, legal_basis: LegalBasis | None) -> list[dict]:
    """権限のあるドメインかつ ABAC で閲覧できる資産に絞る。"""
    scope = authz.require_scope(db, principal, "privacy.inventory.read")
    # 上流から PII を継承した AI 資産も PII の所在に含める (§14)
    stmt = select(DataAsset).where(scope, authz.readable_clause(principal),
                                   or_(DataAsset.contains_pii.is_(True), DataAsset.effective_contains_pii.is_(True)),
                                   DataAsset.lifecycle_state != LifecycleState.DELETED.value)
    if domain:
        stmt = stmt.where(DataAsset.domain == domain)
    assets = db.scalars(stmt.order_by(DataAsset.qualified_name)).all()
    return privacy.inventory_entries(assets, category=category.value if category else None, purpose=purpose,
                                     legal_basis=legal_basis.value if legal_basis else None)


@router.get("/inventory")
def list_inventory(request: Request, response: Response, domain: str | None = None,
                   category: PiiCategory | None = Query(None), purpose: str | None = None,
                   legal_basis: LegalBasis | None = Query(None), page: Page = Depends(page_params()),
                   db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """PII を含む資産とカラムの一覧。処理目的と法的根拠はカラム単位で返す。"""
    return paginate_list(_inventory(db, principal, domain, category, purpose, legal_basis), page, request, response)


@router.get("/inventory/summary")
def inventory_summary(domain: str | None = None, category: PiiCategory | None = Query(None),
                      purpose: str | None = None, legal_basis: LegalBasis | None = Query(None),
                      db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """カテゴリ・目的・法的根拠ごとのカラム数を集計する。"""
    return privacy.summarize(_inventory(db, principal, domain, category, purpose, legal_basis))


# ---------------- 共通 ----------------
def _subject_ref(body: SubjectIdentifier) -> str:
    """生の識別子を subject_ref に変換する。生の値はこの関数の外に出さない。"""
    try:
        return to_subject_ref(body.identifier_type, body.identifier.get_secret_value())
    except ValueError as e:
        raise HTTPException(422, "identifier が空です") from e


# ---------------- Purpose カタログ (§12) ----------------
def _get_purpose(db: Session, purpose_id: str) -> PrivacyPurpose:
    purpose = db.get(PrivacyPurpose, purpose_id)
    if purpose is None:
        raise HTTPException(404, f"purpose not found: {purpose_id}")
    return purpose


@router.post("/purposes", response_model=PurposeOut, status_code=201)
def create_purpose(body: PurposeIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "privacy.purpose.write", entity_type="purpose", entity_id=body.id)
    if db.get(PrivacyPurpose, body.id):
        raise HTTPException(409, f"purpose already exists: {body.id}")
    if body.domain and not db.get(Domain, body.domain):
        raise HTTPException(422, f"unknown domain: {body.domain}")
    purpose = PrivacyPurpose(**body.model_dump(mode="json"))
    db.add(purpose)
    db.flush()
    audit.record(db, principal.audit_actor, "privacy.purpose_create", "purpose", purpose.id, body.model_dump(mode="json"))
    return purpose


@router.get("/purposes", response_model=list[PurposeOut])
def list_purposes(request: Request, response: Response, page: Page = Depends(page_params()),
                  db: Session = Depends(get_session)):
    return paginate(db, select(PrivacyPurpose).order_by(PrivacyPurpose.id), page, request, response)


@router.get("/purposes/{purpose_id}", response_model=PurposeOut)
def get_purpose(purpose_id: str, db: Session = Depends(get_session)):
    return _get_purpose(db, purpose_id)


@router.patch("/purposes/{purpose_id}", response_model=PurposeOut)
def update_purpose(purpose_id: str, body: PurposeUpdate, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    purpose = _get_purpose(db, purpose_id)
    authz.require(db, principal, "privacy.purpose.write", entity_type="purpose", entity_id=purpose.id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    for k, v in changes.items():
        setattr(purpose, k, v)
    db.flush()
    audit.record(db, principal.audit_actor, "privacy.purpose_update", "purpose", purpose.id, changes)
    return purpose


@router.post("/purposes/{purpose_id}/withdraw")
def withdraw_purpose(purpose_id: str, body: PurposeWithdrawIn, db: Session = Depends(get_session),
                     principal: Principal = Depends(get_principal)):
    """目的そのものを撤回する (主体ごとの同意の撤回とは別)。その目的の利用登録を suspended にする (§15)。"""
    purpose = _get_purpose(db, purpose_id)
    authz.require(db, principal, "privacy.purpose.write", entity_type="purpose", entity_id=purpose.id)
    if purpose.withdrawn_at:
        raise HTTPException(409, f"purpose already withdrawn: {purpose.id}")
    purpose.withdrawn_at, purpose.withdrawn_by, purpose.withdrawn_reason = utcnow(), principal.subject, body.reason
    db.flush()
    suspended = mesh_subscriptions.suspend_for_purpose(db, principal.audit_actor, purpose)
    audit.record(db, principal.audit_actor, "privacy.purpose_withdraw", "purpose", purpose.id,
                 {"reason": body.reason, "subscriptions_suspended": suspended})
    return {"purpose": PurposeOut.model_validate(purpose), "subscriptions_suspended": suspended}


# ---------------- Consent Registry (§12 Consent) ----------------
@router.post("/consents", response_model=ConsentRecorded, status_code=201)
def record_consent(body: ConsentIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """同意の取得・撤回を記録する (追記のみ)。監査ログには subject_ref も残さず、行の seq で参照する。"""
    purpose = _get_purpose(db, body.purpose_id)
    authz.require(db, principal, "privacy.consent.write", domain=purpose.domain, entity_type="purpose", entity_id=purpose.id)
    expires = to_naive_utc(body.expires_at) if body.expires_at else None
    if expires is not None and (body.status != ConsentStatus.GRANTED or expires <= utcnow()):
        raise HTTPException(422, "expires_at は status=granted のときに未来の日時で指定してください")
    if purpose.withdrawn_at and body.status == ConsentStatus.GRANTED:
        raise HTTPException(409, f"目的 {purpose.id} は撤回されているため、新しい同意を記録できません")
    row = consent.record(db, subject_ref=_subject_ref(body), purpose_id=purpose.id, status=body.status.value,
                         recorded_by=principal.subject, source=body.source, expires_at=expires)
    opened: list[str] = []
    if body.status == ConsentStatus.WITHDRAWN:
        opened = consent.open_withdrawal_issues(db, principal.audit_actor, purpose)
    audit.record(db, principal.audit_actor, "consent.record", "consent", str(row.seq),
                 {"purpose_id": purpose.id, "status": row.status, "source": row.source, "issues_opened": opened})
    out = ConsentRecorded.model_validate(row)
    out.issues_opened = opened
    return out


@router.post("/consents/check")
def check_consent(body: ConsentCheckIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """目的内利用かどうかを判定する。POST にしているのは識別子を URL やログに残さないため。"""
    purpose = _get_purpose(db, body.purpose_id)
    authz.require(db, principal, "privacy.consent.check", domain=purpose.domain, entity_type="purpose", entity_id=purpose.id)
    return consent.check(db, _subject_ref(body), purpose)


@router.get("/consents", response_model=list[ConsentOut])
def consent_history(request: Request, response: Response,
                    subject_ref: str = Query(pattern=r"^[0-9a-f]{64}$", description="対象の subject_ref (必須)"),
                    purpose_id: str | None = None, page: Page = Depends(page_params()),
                    db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """同意の履歴 (古い順)。全件の列挙を防ぐため subject_ref を必須にしている。"""
    scope = authz.require_scope(db, principal, "privacy.consent.check", PrivacyPurpose.domain)
    stmt = select(Consent).join(PrivacyPurpose, Consent.purpose_id == PrivacyPurpose.id) \
        .where(scope, Consent.subject_ref == subject_ref).order_by(Consent.seq)
    if purpose_id:
        stmt = stmt.where(Consent.purpose_id == purpose_id)
    return paginate(db, stmt, page, request, response)


# ---------------- Right to Erasure (§12 / §10 Delete) ----------------
def _get_request(db: Session, request_id: str) -> ErasureRequest:
    req = db.get(ErasureRequest, request_id)
    if req is None:
        raise HTTPException(404, f"erasure request not found: {request_id}")
    return req


def _target_assets(db: Session, req: ErasureRequest) -> list[tuple[ErasureTask, DataAsset]]:
    return [(t, db.get(DataAsset, t.asset_id)) for t in erasure.tasks_of(db, req)]


def _require_on_any_target(db: Session, principal: Principal, req: ErasureRequest, action: str) -> list[tuple[ErasureTask, DataAsset]]:
    """全社ロール、または対象資産のいずれかとの関係 (Owner など) で判定する。権限が及ぶ (タスク, 資産) を返す。"""
    pairs = _target_assets(db, req)
    mine = [(t, a) for t, a in pairs if authz.is_allowed(principal, action, asset=a)]
    if not mine and not authz.is_allowed(principal, action):
        authz.require(db, principal, action, entity_type="erasure", entity_id=req.id)
    if authz.PERMISSIONS[action].approval:  # is_allowed で許可した経路でも、承認行為なら UI の経路でステップアップを求める
        authz.require_step_up(principal, action)
    return mine


def _detail(db: Session, principal: Principal, req: ErasureRequest) -> ErasureRequestDetail:
    pairs = _target_assets(db, req)
    visible = [t for t, a in pairs if authz.can_read(db, principal, a)]
    out = ErasureRequestDetail.model_validate(req)
    out.tasks = [ErasureTaskOut.model_validate(t) for t in visible]
    out.hidden_task_count = len(pairs) - len(visible)
    out.lineage_exclusions = _visible_exclusions(db, principal, (req.lineage_narrowing or {}).get("excluded", []))
    return out


def _visible_exclusions(db: Session, principal: Principal, excluded: list[dict]) -> list[dict]:
    """外した資産のうち閲覧できるものだけ。経由した資産の名前も、閲覧できないものは出さない (Phase 14 と同じ扱い)。"""
    if not excluded:
        return []
    names = {n for e in excluded for n in e.get("via", [])}
    readable = {a.qualified_name for a in db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(names)))
                if authz.can_read(db, principal, a)} if names else set()
    out = []
    for e in excluded:
        asset = db.get(DataAsset, e["asset_id"])
        if asset is not None and authz.can_read(db, principal, asset):
            out.append({**e, "via": [n for n in e.get("via", []) if n in readable]})
    return out


def _state_guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ErasureStateError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/erasure-requests", response_model=ErasureRequestDetail, status_code=201)
def create_erasure_request(body: ErasureRequestIn, db: Session = Depends(get_session),
                           principal: Principal = Depends(get_principal)):
    """消去要求を受け付ける。生の識別子は subject_ref に変換して破棄する。"""
    authz.require(db, principal, "privacy.erasure.request", entity_type="erasure", entity_id="new")
    req = _state_guard(erasure.create, db, subject_ref=_subject_ref(body), identifier_type=body.identifier_type,
                       reason=body.reason, requested_by=principal.subject)
    audit.record(db, principal.audit_actor, "erasure.receive", "erasure", req.id,
                 {"identifier_type": req.identifier_type, "reason": req.reason, "due_at": req.due_at})
    return _detail(db, principal, req)


@router.get("/erasure-requests", response_model=list[ErasureRequestOut])
def list_erasure_requests(request: Request, response: Response, status: str | None = None,
                          page: Page = Depends(page_params()), db: Session = Depends(get_session),
                          principal: Principal = Depends(get_principal)):
    """Committee / admin は全件、Owner は自分の資産が対象の要求だけ。"""
    stmt = select(ErasureRequest).order_by(ErasureRequest.created_at.desc(), ErasureRequest.id)
    if not authz.is_allowed(principal, "privacy.erasure.read"):
        owned = select(ErasureTask.request_id).join(DataAsset, ErasureTask.asset_id == DataAsset.id) \
            .where(DataAsset.owner == principal.subject)
        stmt = stmt.where(ErasureRequest.id.in_(owned))
    if status:
        stmt = stmt.where(ErasureRequest.status == status)
    return paginate(db, stmt, page, request, response)


@router.post("/erasure-requests/escalate-overdue")
def escalate_overdue_erasures(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """期限超過の要求を Issue 化する。スケジューラから定期実行する想定 (Phase 12)。"""
    authz.require(db, principal, "privacy.erasure.request", entity_type="erasure", entity_id="*")
    result = erasure.escalate_overdue(db, principal.audit_actor)
    audit.record(db, principal.audit_actor, "erasure.escalate_overdue", "erasure", "*", result)
    return result


@router.get("/erasure-requests/{request_id}", response_model=ErasureRequestDetail)
def get_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    req = _get_request(db, request_id)
    _require_on_any_target(db, principal, req, "privacy.erasure.read")
    return _detail(db, principal, req)


@router.post("/erasure-requests/{request_id}/scope", response_model=ErasureRequestDetail)
def scope_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """対象資産を特定する。承認前なら再実行できる (計画を作り直す)。"""
    req = _get_request(db, request_id)
    authz.require(db, principal, "privacy.erasure.request", entity_type="erasure", entity_id=req.id)
    tasks = _state_guard(erasure.scope, db, req)
    audit.record(db, principal.audit_actor, "erasure.scope", "erasure", req.id, {
        "assets": [t.qualified_name for t in tasks],
        "withheld": [t.qualified_name for t in tasks if t.status == "withheld"],
        "unregistered_downstream": req.unregistered_downstream, "ai_impact": req.ai_impact,
        "lineage_excluded": [e["qualified_name"] for e in (req.lineage_narrowing or {}).get("excluded", [])]})
    return _detail(db, principal, req)


@router.post("/erasure-requests/{request_id}/approve", response_model=ErasureRequestDetail)
def approve_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """対象範囲を承認する。Committee は全件、Owner は自分の資産の分だけ。全タスクが揃うと approved になる。"""
    req = _get_request(db, request_id)
    mine = _require_on_any_target(db, principal, req, "privacy.erasure.approve")
    if principal.subject == req.requested_by:
        authz.deny(db, principal, "privacy.erasure.approve", entity_type="erasure", entity_id=req.id,
                   reason="受付者本人は承認できません (職務分離)")
    tasks = [t for t, _ in mine]
    _state_guard(erasure.approve, db, req, tasks, principal.subject)
    audit.record(db, principal.audit_actor, "erasure.approve", "erasure", req.id,
                 {"assets": [t.qualified_name for t in tasks], "status": req.status})
    return _detail(db, principal, req)


@router.post("/erasure-requests/{request_id}/reject", response_model=ErasureRequestDetail)
def reject_erasure_request(request_id: str, body: ErasureRejectIn, db: Session = Depends(get_session),
                           principal: Principal = Depends(get_principal)):
    """却下は Committee のみ (法的義務による保持など)。理由は必須。"""
    req = _get_request(db, request_id)
    authz.require(db, principal, "privacy.erasure.reject", entity_type="erasure", entity_id=req.id)
    _state_guard(erasure.reject, db, req, principal.subject, body.reason)
    audit.record(db, principal.audit_actor, "erasure.reject", "erasure", req.id, {"reason": body.reason})
    return _detail(db, principal, req)


@router.post("/erasure-requests/{request_id}/dispatch", response_model=ErasureRequestDetail)
def dispatch_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """削除指示を発行する。発行後のタスクは Data Plane から GET /privacy/erasure-tasks で取得できる。"""
    req = _get_request(db, request_id)
    authz.require(db, principal, "privacy.erasure.request", entity_type="erasure", entity_id=req.id)
    _state_guard(erasure.dispatch, db, req, principal.audit_actor)
    audit.record(db, principal.audit_actor, "erasure.dispatch", "erasure", req.id, {
        "status": req.status, "dataset_version_ids": req.ai_impact.get("dataset_version_ids", []),
        "model_version_ids": req.ai_impact.get("model_version_ids", [])})
    return _detail(db, principal, req)


def _finish(db: Session, principal: Principal, request_id: str, fn, action: str) -> ErasureRequestDetail:
    req = _get_request(db, request_id)
    _require_on_any_target(db, principal, req, "privacy.erasure.verify")
    if principal.subject in {t.completed_by for t in erasure.tasks_of(db, req)}:
        authz.deny(db, principal, "privacy.erasure.verify", entity_type="erasure", entity_id=req.id,
                   reason="削除を実行した本人は完了確認できません (職務分離)")
    _state_guard(fn, db, req, principal)
    audit.record(db, principal.audit_actor, action, "erasure", req.id, {"status": req.status})
    return _detail(db, principal, req)


@router.post("/erasure-requests/{request_id}/verify", response_model=ErasureRequestDetail)
def verify_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _finish(db, principal, request_id, lambda d, r, p: erasure.verify(d, r, p.subject), "erasure.verify")


@router.post("/erasure-requests/{request_id}/close", response_model=ErasureRequestDetail)
def close_erasure_request(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _finish(db, principal, request_id, lambda d, r, p: erasure.close(d, r), "erasure.close")


@router.get("/erasure-requests/{request_id}/certificate")
def erasure_certificate(request_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """消去証明 (対象資産、各タスクの結果、時刻、request_id)。生の識別子は含まない。発行自体も監査に残す。"""
    req = _get_request(db, request_id)
    authz.require(db, principal, "privacy.erasure.certificate", entity_type="erasure", entity_id=req.id)
    cert = _state_guard(erasure.certificate, db, req)
    audit.record(db, principal.audit_actor, "erasure.certificate", "erasure", req.id,
                 {"certificate_hash": cert["certificate_hash"], "status": req.status})
    return cert


# ---------------- Data Plane 向け (pull 型。Phase 12 の Outbox までのつなぎ) ----------------
@router.get("/erasure-tasks", response_model=list[ErasureWorkItem])
def list_erasure_tasks(request: Request, response: Response, status: str | None = None,
                       page: Page = Depends(page_params()), db: Session = Depends(get_session),
                       principal: Principal = Depends(get_principal)):
    """削除タスクの取得。Custodian には自分の資産の、発行済みのタスクだけを返す。"""
    stmt = select(ErasureTask, ErasureRequest).join(ErasureRequest, ErasureTask.request_id == ErasureRequest.id) \
        .join(DataAsset, ErasureTask.asset_id == DataAsset.id) \
        .order_by(ErasureRequest.due_at, ErasureTask.id)
    if not authz.is_allowed(principal, "privacy.erasure.task_read"):
        stmt = stmt.where(DataAsset.custodian == principal.subject,
                          ErasureTask.status.not_in(["planned", "cancelled"]))
    if status:
        stmt = stmt.where(ErasureTask.status == status)
    rows = db.execute(stmt).all()
    page_rows = paginate_list(rows, page, request, response)
    return [ErasureWorkItem(**ErasureTaskOut.model_validate(t).model_dump(), subject_ref=r.subject_ref,
                            identifier_type=r.identifier_type, due_at=r.due_at) for t, r in page_rows]


def _load_task(db: Session, principal: Principal, task_id: str, action: str) -> tuple[ErasureTask, DataAsset]:
    task = db.get(ErasureTask, task_id)
    if task is None:
        raise HTTPException(404, f"erasure task not found: {task_id}")
    asset = get_asset_or_404(db, task.asset_id)
    authz.require(db, principal, action, asset=asset, entity_type="erasure_task", entity_id=task.id)
    return task, asset


@router.post("/erasure-tasks/{task_id}/ack", response_model=ErasureTaskOut)
def ack_erasure_task(task_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """受領の応答。発行後に Legal Hold が設定されていた場合は withheld を返す (削除しないこと)。"""
    task, asset = _load_task(db, principal, task_id, "privacy.erasure.task_ack")
    _state_guard(erasure.acknowledge, db, task, asset, principal.subject)
    audit.record(db, principal.audit_actor, "erasure.task_ack", "erasure_task", task.id,
                 {"request_id": task.request_id, "asset_id": asset.id, "status": task.status})
    return task


@router.post("/erasure-tasks/{task_id}/complete", response_model=ErasureTaskOut)
def complete_erasure_task(task_id: str, body: ErasureTaskCompleteIn, db: Session = Depends(get_session),
                          principal: Principal = Depends(get_principal)):
    """完了の応答 (削除件数とハッシュ化した証跡)。全タスクが終わると要求は completed / partially_completed になる。"""
    task, asset = _load_task(db, principal, task_id, "privacy.erasure.task_complete")
    _state_guard(erasure.complete, db, task, asset, actor=principal.subject, outcome=body.outcome,
                 records_deleted=body.records_deleted, evidence_hash=body.evidence_hash, note=body.note)
    audit.record(db, principal.audit_actor, "erasure.task_complete", "erasure_task", task.id, {
        "request_id": task.request_id, "asset_id": asset.id, "outcome": body.outcome,
        "records_deleted": body.records_deleted, "evidence_hash": body.evidence_hash, "note": body.note})
    return task
