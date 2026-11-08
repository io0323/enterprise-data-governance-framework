"""§12 Data Privacy API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.enums import ConsentStatus, LegalBasis, LifecycleState, PiiCategory
from edgf.models import Consent, DataAsset, Domain, PrivacyPurpose, utcnow
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import (ConsentCheckIn, ConsentOut, ConsentIn, ConsentRecorded, PurposeIn, PurposeOut, PurposeUpdate,
                          SubjectIdentifier)
from edgf.services import audit, consent, privacy
from edgf.services.subject_ref import to_subject_ref

router = APIRouter(prefix="/privacy", tags=["privacy"])


# ---------------- PII Inventory (§12 PII) ----------------
def _inventory(db: Session, principal: Principal, domain: str | None, category: PiiCategory | None,
               purpose: str | None, legal_basis: LegalBasis | None) -> list[dict]:
    """権限のあるドメインかつ ABAC で閲覧できる資産に絞る。"""
    scope = authz.require_scope(db, principal, "privacy.inventory.read")
    stmt = select(DataAsset).where(scope, authz.readable_clause(principal), DataAsset.contains_pii.is_(True),
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


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


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


# ---------------- Consent Registry (§12 Consent) ----------------
@router.post("/consents", response_model=ConsentRecorded, status_code=201)
def record_consent(body: ConsentIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """同意の取得・撤回を記録する (追記のみ)。監査ログには subject_ref も残さず、行の seq で参照する。"""
    purpose = _get_purpose(db, body.purpose_id)
    authz.require(db, principal, "privacy.consent.write", domain=purpose.domain, entity_type="purpose", entity_id=purpose.id)
    expires = _naive_utc(body.expires_at) if body.expires_at else None
    if expires is not None and (body.status != ConsentStatus.GRANTED or expires <= utcnow()):
        raise HTTPException(422, "expires_at は status=granted のときに未来の日時で指定してください")
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
