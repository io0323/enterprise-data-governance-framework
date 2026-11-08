"""§12 Data Privacy API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.enums import LegalBasis, LifecycleState, PiiCategory
from edgf.models import DataAsset
from edgf.pagination import Page, page_params, paginate_list
from edgf.services import privacy

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
