"""§8 Reference Data API: 全社標準コードの版管理."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.models import DataAsset, RefDataSet, RefDataVersion, to_naive_utc
from edgf.pagination import Page, page_params, paginate, paginate_list
from edgf.schemas import RefCodeIn, RefDataSetIn, RefDataSetOut, RefDataVersionIn
from edgf.services import audit, refdata
from edgf.services.audit import guard_payload
from edgf.services.refdata import RefDataError, RefDataStateError

router = APIRouter(prefix="/reference-data", tags=["reference-data"])


def _guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except RefDataStateError as e:
        raise HTTPException(409, str(e)) from e
    except RefDataError as e:
        raise HTTPException(422, str(e)) from e


def _set(db: Session, set_id: str) -> RefDataSet:
    data_set = db.get(RefDataSet, set_id)
    if data_set is None:
        raise HTTPException(404, f"reference set not found: {set_id}")
    return data_set


def _readable_set(db: Session, principal: Principal, set_id: str, action: str = "mdm.read") -> tuple[RefDataSet, DataAsset]:
    data_set = _set(db, set_id)
    return data_set, load_asset(db, principal, data_set.asset_id, action)


def _version(db: Session, set_id: str, number: int) -> RefDataVersion:
    version = refdata.get_version(db, set_id, number)
    if version is None:
        raise HTTPException(404, f"version not found: {set_id}@{number}")
    return version


def _code_dicts(codes: list[RefCodeIn]) -> list[dict]:
    data = [c.model_dump() for c in codes]
    # 区分値に個人情報が混ざらないよう、監査ログと同じガードをかける (§18.2 の例外の条件)
    guard_payload([{k: v for k, v in c.items() if isinstance(v, str)} for c in data], path="codes")
    return data


@router.post("/sets", response_model=RefDataSetOut, status_code=201)
def create_set(body: RefDataSetIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Reference Data Set を作る。public / internal かつ PII なしの reference 資産にだけ対応付けられる。"""
    asset = load_asset(db, principal, body.asset_id, "refdata.write")
    _guarded(refdata.check_asset, asset)
    if db.get(RefDataSet, body.id):
        raise HTTPException(409, f"reference set already exists: {body.id}")
    data_set = RefDataSet(id=body.id, asset_id=asset.id, name=body.name, description=body.description,
                          created_by=principal.subject)
    db.add(data_set)
    db.flush()
    audit.record(db, principal.audit_actor, "refdata.set_create", "reference_set", data_set.id, {"asset_id": asset.id})
    return data_set


@router.get("/sets", response_model=list[RefDataSetOut])
def list_sets(request: Request, response: Response, page: Page = Depends(page_params()),
              db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    readable = select(DataAsset.id).where(authz.readable_clause(principal))
    stmt = select(RefDataSet).where(RefDataSet.asset_id.in_(readable)).order_by(RefDataSet.id)
    return paginate(db, stmt, page, request, response)


@router.get("/sets/{set_id}", response_model=RefDataSetOut)
def get_set(set_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _readable_set(db, principal, set_id)[0]


@router.post("/sets/{set_id}/versions", status_code=201)
def create_version(set_id: str, body: RefDataVersionIn, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    """下書きの版を作る。"""
    data_set, asset = _readable_set(db, principal, set_id, "refdata.write")
    _guarded(refdata.check_asset, asset)
    version = _guarded(refdata.create_version, db, data_set, _code_dicts(body.codes), body.note, principal.subject)
    audit.record(db, principal.audit_actor, "refdata.version_create", "reference_set", set_id,
                 {"version": version.version, "codes": len(body.codes), "note": body.note})
    return refdata.version_view(db, version)


@router.get("/sets/{set_id}/versions")
def list_versions(set_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _readable_set(db, principal, set_id)
    versions = refdata.versions_of(db, set_id)
    return [refdata.version_view(db, v, with_codes=False) for v in paginate_list(versions, page, request, response)]


@router.get("/sets/{set_id}/latest")
def latest_version(set_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """最新の公開版。"""
    _readable_set(db, principal, set_id)
    version = refdata.latest_published(db, set_id)
    if version is None:
        raise HTTPException(404, "公開済みの版がありません")
    return refdata.version_view(db, version)


@router.get("/sets/{set_id}/as-of")
def version_as_of(set_id: str, at: datetime = Query(description="この時点で公開されていた版と、その時点で有効なコード値を返す"),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _readable_set(db, principal, set_id)
    when = to_naive_utc(at)
    version = refdata.published_as_of(db, set_id, when)
    if version is None:
        raise HTTPException(404, "指定した時点の公開版はありません")
    return refdata.version_view(db, version, at=when.date())


@router.get("/sets/{set_id}/diff")
def diff_versions(set_id: str, from_version: int = Query(ge=1), to_version: int = Query(ge=1),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """版どうしの差分 (追加・廃止・ラベル変更)。"""
    _readable_set(db, principal, set_id)
    return refdata.diff(db, _version(db, set_id, from_version), _version(db, set_id, to_version))


@router.get("/sets/{set_id}/versions/{number}")
def get_version(set_id: str, number: int, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _readable_set(db, principal, set_id)
    return refdata.version_view(db, _version(db, set_id, number))


@router.put("/sets/{set_id}/versions/{number}/codes")
def replace_codes(set_id: str, number: int, body: list[RefCodeIn], db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    """下書きのコード値を置き換える。公開済みの版は変更できない (409)。"""
    _, asset = _readable_set(db, principal, set_id, "refdata.write")
    version = _version(db, set_id, number)
    _guarded(refdata.update_draft, db, version, _code_dicts(body))
    audit.record(db, principal.audit_actor, "refdata.version_update", "reference_set", set_id,
                 {"version": number, "codes": len(body), "asset_id": asset.id})
    return refdata.version_view(db, version)


@router.post("/sets/{set_id}/versions/{number}/publish")
def publish_version(set_id: str, number: int, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """版を公開する (対象資産の Owner または Committee の承認)。下流資産の Owner に Issue で通知する。"""
    data_set, asset = _readable_set(db, principal, set_id, "refdata.publish")
    version = _version(db, set_id, number)
    result = _guarded(refdata.publish, db, data_set, version, asset, principal.subject, principal.audit_actor)
    audit.record(db, principal.audit_actor, "refdata.publish", "reference_set", set_id, {
        "version": number, "content_hash": version.content_hash, "issues_opened": result["issues_opened"]})
    return {**refdata.version_view(db, version, with_codes=False), **result}
