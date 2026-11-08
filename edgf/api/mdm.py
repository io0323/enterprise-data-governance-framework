"""§8 Master Data Management API.

Golden Record の値は Data Plane (MDM Hub) の責務で、Control Plane には永続化しない (§18.2)。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal, load_asset
from edgf.errors import ProblemError
from sqlalchemy import select

from edgf.models import DataAsset, GoldenRecord, GoldenVersion, MdmReview, MdmRuleApproval
from edgf.pagination import paginate
from edgf.schemas import (MdmComposeIn, MdmMatchIn, MdmMatchOut, MdmReviewDecision, MdmReviewOut, MdmSourceRecord,
                          MdmSplitIn, MdmVerifyIn, MdmXrefLookupIn)
from edgf.pagination import Page, page_params, paginate_list
from edgf.services import audit, mdm, mdm_rules
from edgf.services.mdm import MdmInputError, MdmStateError, SourceRecord
from edgf.services.mdm_rules import Entity, MdmRuleError

router = APIRouter(prefix="/mdm", tags=["mdm"])


def _entity(entity_type: str) -> Entity:
    entity = mdm_rules.get_entities().get(entity_type)
    if entity is None:
        raise HTTPException(404, f"unknown entity type: {entity_type}")
    return entity


def _master_asset(db: Session, entity: Entity) -> DataAsset:
    """Metadata First (§6.3): 対応する master 資産が Catalog に無ければ MDM の操作を受け付けない。"""
    asset = mdm_rules.find_asset(db, entity)
    if asset is None:
        raise ProblemError(409, f"エンティティ {entity.entity_type} に対応する master 資産 {entity.asset} が Catalog にありません",
                           type_="mdm-asset-missing")
    return asset


def _entity_view(db: Session, entity: Entity, asset: DataAsset | None) -> dict:
    approval = mdm_rules.approval_of(db, entity)
    return {**entity.as_dict(), "asset_id": asset.id if asset else None, "asset_registered": asset is not None,
            "approved_by": approval.approved_by if approval else None,
            "approved_at": approval.approved_at if approval else None}


# ---------------- エンティティ定義 (9a) ----------------
@router.get("/entities")
def list_entities(request: Request, response: Response, page: Page = Depends(page_params()),
                  db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """エンティティ定義の一覧 (生成ロジックの公開: §8.1)。閲覧できない資産に対応するものは除外する。"""
    authz.require(db, principal, "mdm.read", entity_type="mdm", entity_id="*")
    views = []
    for entity in mdm_rules.get_entities().values():
        asset = mdm_rules.find_asset(db, entity)
        if asset is None or authz.can_read(db, principal, asset):
            views.append(_entity_view(db, entity, asset))
    return paginate_list(views, page, request, response)


@router.post("/entities/reload")
def reload_entities(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "mdm.rule.write", entity_type="mdm", entity_id="*")
    try:
        entities = mdm_rules.get_entities(reload=True)
    except MdmRuleError as e:
        raise HTTPException(422, str(e)) from e
    audit.record(db, principal.audit_actor, "mdm.rule_reload", "mdm", "*",
                 {"rules": {e.entity_type: e.rule_hash for e in entities.values()}})
    return {"loaded": len(entities)}


@router.get("/entities/{entity_type}")
def get_entity(entity_type: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    asset = mdm_rules.find_asset(db, entity)
    if asset is not None:
        authz.require_read(db, principal, asset)
    return _entity_view(db, entity, asset)


@router.post("/entities/{entity_type}/approve")
def approve_entity_rules(entity_type: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Golden Record の生成規則を対象資産の Owner が承認する (§8.2)。ルールを変更すると再承認が必要になる。"""
    entity = _entity(entity_type)
    asset = load_asset(db, principal, _master_asset(db, entity).id, "mdm.rule.approve")
    approval = mdm_rules.approval_of(db, entity)
    if approval is None:
        approval = MdmRuleApproval(entity_type=entity.entity_type, rule_hash=entity.rule_hash,
                                   approved_by=principal.subject)
        db.add(approval)
        db.flush()
        audit.record(db, principal.audit_actor, "mdm.rule_approve", "asset", asset.id,
                     {"entity_type": entity.entity_type, "rule_hash": entity.rule_hash})
    return _entity_view(db, entity, asset)


# ---------------- マッチングと生成 (9b): 値は保持しない ----------------
def _guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MdmStateError as e:
        raise HTTPException(409, str(e)) from e
    except MdmInputError as e:
        raise HTTPException(422, str(e)) from e


def _record(entity: Entity, r: MdmSourceRecord) -> SourceRecord:
    return _guarded(mdm.prepare_record, entity, r.source_system, r.key.get_secret_value(), r.attributes, r.updated_at)


def _golden(db: Session, entity: Entity, golden_id: str):
    golden = mdm.get_active_golden(db, entity, golden_id)
    if golden is None:
        raise HTTPException(404, f"golden record not found: {golden_id}")
    return golden


@router.post("/{entity_type}/match", response_model=MdmMatchOut)
def match_record(entity_type: str, body: MdmMatchIn, db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    """ソースレコードの行き先 (新規 / 既存に紐付く / 裁定) を判定する。値は応答にも監査にも含めない。"""
    entity = _entity(entity_type)
    asset = load_asset(db, principal, _master_asset(db, entity).id, "mdm.match")
    record = _record(entity, body.record)
    candidates = [(c.golden_id, c.attributes) for c in body.candidates]
    result = _guarded(mdm.match, db, entity, asset, record, candidates, principal.audit_actor)
    audit.record(db, principal.audit_actor, "mdm.match", "mdm", result["golden_id"] or asset.id, {
        "entity_type": entity.entity_type, "source_system": record.source_system, "decision": result["decision"],
        "stage": result["stage"], "score": result["score"], "review_ids": result["review_ids"]})
    return result


@router.post("/{entity_type}/golden-records/{golden_id}/compose")
def compose_golden(entity_type: str, golden_id: str, body: MdmComposeIn, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    """構成ソースの値から Survivorship で Golden Record を生成して返す。

    値はこの応答で返すだけで、永続化するのは属性ごとの由来・内容ハッシュ・版だけ。Data Plane が値を保存する。
    """
    entity = _entity(entity_type)
    # 属性値の指定 (steward_override) は実行ではなく裁定なので、対象資産の Steward / Owner が行う
    action = "mdm.review.decide" if body.overrides else "mdm.compose"
    load_asset(db, principal, _master_asset(db, entity).id, action)
    golden = _golden(db, entity, golden_id)
    records = [_record(entity, r) for r in body.records]
    result = _guarded(mdm.compose, db, entity, golden, records, principal.audit_actor, body.overrides)
    audit.record(db, principal.audit_actor, "mdm.compose", "golden", golden.id, {
        "version": result["version"], "content_hash": result["content_hash"], "reason": result["reason"],
        "changed": result["changed"], "source_count": len(records)})
    return result


# ---------------- Steward の裁定キュー (9c): 比較結果だけを扱う ----------------
@router.post("/reviews/escalate-overdue")
def escalate_overdue_reviews(db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """期限超過の裁定を Issue 化する。スケジューラから定期実行する想定 (Phase 12)。"""
    authz.require(db, principal, "mdm.review.escalate", entity_type="mdm", entity_id="*")
    result = mdm.escalate_overdue_reviews(db, principal.audit_actor)
    audit.record(db, principal.audit_actor, "mdm.review_escalate_overdue", "mdm", "*", result)
    return result


@router.get("/{entity_type}/reviews", response_model=list[MdmReviewOut])
def list_reviews(entity_type: str, request: Request, response: Response, status: str | None = None,
                 page: Page = Depends(page_params()), db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.read")
    stmt = select(MdmReview).where(MdmReview.entity_type == entity.entity_type) \
        .order_by(MdmReview.due_at, MdmReview.id)
    if status:
        stmt = stmt.where(MdmReview.status == status)
    return paginate(db, stmt, page, request, response)


def _review(db: Session, entity: Entity, review_id: str) -> MdmReview:
    review = db.get(MdmReview, review_id)
    if review is None or review.entity_type != entity.entity_type:
        raise HTTPException(404, f"review not found: {review_id}")
    return review


@router.get("/{entity_type}/reviews/{review_id}", response_model=MdmReviewOut)
def get_review(entity_type: str, review_id: str, db: Session = Depends(get_session),
               principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.read")
    return _review(db, entity, review_id)


@router.post("/{entity_type}/reviews/{review_id}/decision", response_model=MdmReviewOut)
def decide_review(entity_type: str, review_id: str, body: MdmReviewDecision, db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    """裁定: merge / not_match (以後は同じペアを起票しない) / split (merge 済みの誤マージを分割)。"""
    entity = _entity(entity_type)
    asset = load_asset(db, principal, _master_asset(db, entity).id, "mdm.review.decide")
    review = _review(db, entity, review_id)
    actor = principal.subject
    if body.decision == "merge":
        _guarded(mdm.decide_merge, db, entity, review, actor, body.reason)
    elif body.decision == "not_match":
        _guarded(mdm.decide_not_match, db, entity, asset, review, actor, body.reason)
    else:
        _guarded(mdm.decide_split, db, entity, asset, review, actor, body.reason)
    audit.record(db, principal.audit_actor, f"mdm.review_{body.decision}", "mdm_review", review.id, {
        "golden_id": review.golden_id, "new_golden_id": review.new_golden_id, "score": review.score, "reason": body.reason})
    return review


@router.post("/{entity_type}/golden-records/{golden_id}/split")
def split_golden(entity_type: str, golden_id: str, body: MdmSplitIn, db: Session = Depends(get_session),
                 principal: Principal = Depends(get_principal)):
    """誤マージの分割 (自動マージを含む)。版の履歴を保ったまま xref を付け替え、Data Plane に再構成を求める。"""
    entity = _entity(entity_type)
    asset = load_asset(db, principal, _master_asset(db, entity).id, "mdm.review.decide")
    golden = _golden(db, entity, golden_id)
    new = _guarded(mdm.split_sources, db, entity, asset, golden, body.source_refs, principal.subject)
    audit.record(db, principal.audit_actor, "mdm.split", "golden", golden.id, {
        "new_golden_id": new.id, "moved": len(body.source_refs), "reason": body.reason})
    return {"golden_id": golden.id, "new_golden_id": new.id, "moved": sorted(set(body.source_refs)),
            "recompose_required": [golden.id, new.id]}


# ---------------- 版の管理と時点指定の照会 (9d): 値は返さない ----------------
def _xref_results(db: Session, principal: Principal, source_system: str, key: str) -> list[dict]:
    authz.require(db, principal, "mdm.read", entity_type="mdm", entity_id="xref")
    try:
        source_ref = mdm.make_source_ref(source_system, key)
    except ValueError as e:
        raise HTTPException(422, "key が空です") from e
    results = []
    for xref in mdm.lookup_xrefs(db, source_ref):
        golden = db.get(GoldenRecord, xref.golden_id)
        if authz.can_read(db, principal, db.get(DataAsset, golden.asset_id)):
            results.append({"entity_type": xref.entity_type, "golden_id": xref.golden_id, "source_ref": xref.source_ref,
                            "source_system": xref.source_system, "link_type": xref.link_type, "linked_at": xref.linked_at,
                            "status": mdm.TOMBSTONED if xref.tombstoned_at else mdm.ACTIVE})
    return results


@router.get("/xref")
def get_xref(request: Request, response: Response, source_system: str = Query(min_length=1, max_length=100),
             key: str = Query(min_length=1, max_length=512), page: Page = Depends(page_params()),
             db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """キーを HMAC に変換して、紐付く golden_id を引く (ABAC で絞り込む)。

    キーが URL に載るので、プロキシのログなどに残る環境では POST /mdm/xref/lookup を使うこと。
    """
    return paginate_list(_xref_results(db, principal, source_system, key), page, request, response)


@router.post("/xref/lookup")
def lookup_xref(body: MdmXrefLookupIn, request: Request, response: Response, page: Page = Depends(page_params()),
                db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """GET /mdm/xref と同じ。キーを URL に載せない。"""
    return paginate_list(_xref_results(db, principal, body.source_system, body.key.get_secret_value()), page, request, response)


@router.get("/{entity_type}/golden-records")
def list_golden_records(entity_type: str, request: Request, response: Response, status: str | None = None,
                        recompose_required: bool | None = None, page: Page = Depends(page_params()),
                        db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.read")
    stmt = select(GoldenRecord).where(GoldenRecord.entity_type == entity.entity_type) \
        .order_by(GoldenRecord.created_at, GoldenRecord.id)
    if status:
        stmt = stmt.where(GoldenRecord.status == status)
    if recompose_required is not None:
        stmt = stmt.where(GoldenRecord.recompose_required.is_(recompose_required))
    return [mdm.golden_view(g) for g in paginate(db, stmt, page, request, response)]


@router.get("/{entity_type}/golden-records/{golden_id}")
def get_golden_record(entity_type: str, golden_id: str, as_of: datetime | None = Query(None, description="この時点の版の情報を返す"),
                      db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """版の情報を返す。値は含まない (値は Data Plane が保持する)。"""
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.read")
    golden = _golden(db, entity, golden_id)
    if as_of is not None:
        at = as_of.astimezone(timezone.utc).replace(tzinfo=None) if as_of.tzinfo else as_of
        version = mdm.version_at(db, golden.id, at)
        if version is None:
            raise HTTPException(404, "指定した時点の版はありません")
    else:
        version = mdm.current_version(db, golden)
    sources = mdm.active_source_refs(db, golden.id)
    return {**mdm.golden_view(golden), "source_refs": sources, "version": mdm.version_view(version) if version else None}


@router.get("/{entity_type}/golden-records/{golden_id}/versions")
def list_golden_versions(entity_type: str, golden_id: str, request: Request, response: Response,
                         page: Page = Depends(page_params()), db: Session = Depends(get_session),
                         principal: Principal = Depends(get_principal)):
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.read")
    golden = _golden(db, entity, golden_id)
    stmt = select(GoldenVersion).where(GoldenVersion.golden_id == golden.id).order_by(GoldenVersion.version)
    return [mdm.version_view(v) for v in paginate(db, stmt, page, request, response)]


@router.post("/{entity_type}/golden-records/{golden_id}/verify")
def verify_golden(entity_type: str, golden_id: str, body: MdmVerifyIn, db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    """Data Plane が保持している値を、その版の内容ハッシュと照合する。値は検証後に破棄する。"""
    entity = _entity(entity_type)
    load_asset(db, principal, _master_asset(db, entity).id, "mdm.compose")
    golden = _golden(db, entity, golden_id)
    result = _guarded(mdm.verify, db, golden, body.attributes, body.version)
    audit.record(db, principal.audit_actor, "mdm.verify", "golden", golden.id,
                 {"version": result["version"], "match": result["match"]})
    return result
