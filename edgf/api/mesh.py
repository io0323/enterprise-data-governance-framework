"""§15 Data Governance & Data Mesh API."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.api.lineage import redacted_impact
from edgf.deps import get_principal, load_asset
from edgf.errors import ProblemError
from edgf.models import CdmEntity, CdmVersion, ContractVersion, DataAsset, DataContract, DataProduct, MeshSubscription, OutputPort
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import (CdmEntityOut, CdmImportIn, CdmVersionOut, ContractCheckIn, ContractIn, ContractOut, ContractVersionIn, ContractVersionOut, ExpediteRetireIn,
                          OutputPortIn, OutputPortOut, OutputPortUpdate, ProductIn, ProductOut, ProductUpdate,
                          SubscriptionDecisionIn, SubscriptionIn, SubscriptionMigrateIn, SubscriptionOut)
from edgf.services import audit, mesh, mesh_cdm, mesh_contracts, mesh_datsis, mesh_subscriptions
from edgf.services.mesh import MeshStateError, MeshValidationError

router = APIRouter(prefix="/mesh", tags=["mesh"])
T = TypeVar("T")


def _guard(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except MeshValidationError as e:
        raise HTTPException(422, str(e)) from e
    except MeshStateError as e:
        raise HTTPException(409, str(e)) from e


def _require_owner(db: Session, principal: Principal, action: str, product: DataProduct, entity_type: str, entity_id: str) -> None:
    """Product の Owner だけに許す操作 (Output Port の資産の Owner とは別の人でありうる)。"""
    authz.require(db, principal, action, relations=("owner",) if product.owner == principal.subject else (),
                  entity_type=entity_type, entity_id=entity_id)


def _get_product(db: Session, product_id: str) -> DataProduct:
    product = db.get(DataProduct, product_id)
    if product is None:
        raise HTTPException(404, f"data product not found: {product_id}")
    return product


def _get_port(db: Session, port_id: str) -> OutputPort:
    port = db.get(OutputPort, port_id)
    if port is None:
        raise HTTPException(404, f"output port not found: {port_id}")
    return port


def _port_out(db: Session, principal: Principal, port: OutputPort) -> OutputPortOut:
    """閲覧できない資産のポートは名前だけを返す (Product 自体は発見できるようにする)。"""
    if not authz.can_read(db, principal, db.get(DataAsset, port.asset_id)):
        return OutputPortOut(id=port.id, product_id=port.product_id, name=port.name, redacted=True)
    contract_id = db.scalar(select(DataContract.id).where(DataContract.output_port_id == port.id))
    return OutputPortOut(id=port.id, product_id=port.product_id, name=port.name, asset_id=port.asset_id,
                         port_type=port.port_type, address_uri=port.address_uri, contract_id=contract_id)


def _product_out(db: Session, principal: Principal, product: DataProduct) -> ProductOut:
    return ProductOut(id=product.id, name=product.name, domain=product.domain, owner=product.owner,
                      description=product.description, status=product.status, created_by=product.created_by,
                      created_at=product.created_at, updated_at=product.updated_at, published_by=product.published_by,
                      published_at=product.published_at, deprecated_at=product.deprecated_at, retired_at=product.retired_at,
                      term_ids=sorted(t.id for t in product.terms), ports=[_port_out(db, principal, p) for p in product.ports])


# ---------------- Data Product (§15) ----------------
@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "mesh.product.write", domain=body.domain, entity_type="data_product", entity_id=body.name)
    product = _guard(mesh.create_product, db, name=body.name, domain=body.domain, owner=body.owner,
                     description=body.description, term_ids=body.term_ids, actor=principal.subject)
    audit.record(db, principal.audit_actor, "mesh.product.create", "data_product", product.id,
                 {"name": product.name, "domain": product.domain, "owner": product.owner})
    return _product_out(db, principal, product)


@router.get("/products", response_model=list[ProductOut])
def list_products(request: Request, response: Response, domain: str | None = None, status: str | None = None,
                  owner: str | None = None, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    stmt = select(DataProduct).order_by(DataProduct.domain, DataProduct.name)
    for column, value in ((DataProduct.domain, domain), (DataProduct.status, status), (DataProduct.owner, owner)):
        if value is not None:
            stmt = stmt.where(column == value)
    return [_product_out(db, principal, p) for p in paginate(db, stmt, page, request, response)]


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _product_out(db, principal, _get_product(db, product_id))


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: str, body: ProductUpdate, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    product = _get_product(db, product_id)
    authz.require(db, principal, "mesh.product.write", domain=product.domain, entity_type="data_product", entity_id=product.id)
    changes = body.model_dump(exclude_unset=True)
    _guard(mesh.update_product, db, product, changes)
    audit.record(db, principal.audit_actor, "mesh.product.update", "data_product", product.id,
                 {k: v for k, v in changes.items() if k != "description"})
    return _product_out(db, principal, product)


@router.get("/products/{product_id}/datsis")
def get_datsis(product_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """出荷基準の判定結果。公開前に確認できる。"""
    product = _get_product(db, product_id)
    results, blockers = mesh_datsis.evaluate(db, product)
    return {"product_id": product.id, "publishable": not blockers, "results": results, "blockers": blockers}


@router.post("/products/{product_id}/publish", response_model=ProductOut)
def publish_product(product_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """MESH-001〜006 (と Domain Policy の追加基準) がすべて合格しないと公開できない (Certification と同じゲート)。"""
    product = _get_product(db, product_id)
    _require_owner(db, principal, "mesh.product.publish", product, "data_product", product.id)
    _guard(mesh_datsis.publish, db, product, principal.subject)
    audit.record(db, principal.audit_actor, "mesh.product.publish", "data_product", product.id, {"status": product.status})
    return _product_out(db, principal, product)


@router.post("/products/{product_id}/deprecate", response_model=ProductOut)
def deprecate_product(product_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """新しい利用登録を止め、利用者に知らせる。"""
    product = _get_product(db, product_id)
    _require_owner(db, principal, "mesh.product.publish", product, "data_product", product.id)
    _guard(mesh_datsis.deprecate, db, product)
    opened = mesh_subscriptions.notify_product_deprecated(db, principal.audit_actor, product)
    audit.record(db, principal.audit_actor, "mesh.product.deprecate", "data_product", product.id, {"issues_opened": opened})
    return _product_out(db, principal, product)


@router.post("/products/{product_id}/retire", response_model=ProductOut)
def retire_product(product_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """有効な利用登録が残っていれば 409。"""
    product = _get_product(db, product_id)
    _require_owner(db, principal, "mesh.product.publish", product, "data_product", product.id)
    _guard(mesh_datsis.retire, db, product)
    audit.record(db, principal.audit_actor, "mesh.product.retire", "data_product", product.id, {"status": product.status})
    return _product_out(db, principal, product)


# ---------------- Output Port ----------------
@router.post("/products/{product_id}/ports", response_model=OutputPortOut, status_code=201)
def add_port(product_id: str, body: OutputPortIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    product = _get_product(db, product_id)
    authz.require(db, principal, "mesh.product.write", domain=product.domain, entity_type="data_product", entity_id=product.id)
    port = _guard(mesh.add_port, db, product, name=body.name, asset_id=body.asset_id, port_type=body.port_type,
                  address_uri=body.address_uri)
    audit.record(db, principal.audit_actor, "mesh.port.add", "output_port", port.id, {
        "product_id": product.id, "asset_id": port.asset_id, "port_type": port.port_type, "address_uri": port.address_uri})
    return _port_out(db, principal, port)


@router.patch("/ports/{port_id}", response_model=OutputPortOut)
def update_port(port_id: str, body: OutputPortUpdate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    port = _get_port(db, port_id)
    product = db.get(DataProduct, port.product_id)
    authz.require(db, principal, "mesh.product.write", domain=product.domain, entity_type="output_port", entity_id=port.id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("port_type", "") is None:
        del changes["port_type"]
    for key, value in changes.items():
        setattr(port, key, value)
    db.flush()
    audit.record(db, principal.audit_actor, "mesh.port.update", "output_port", port.id, changes)
    return _port_out(db, principal, port)


@router.get("/ports/{port_id}", response_model=OutputPortOut)
def get_port(port_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _port_out(db, principal, _get_port(db, port_id))


# ---------------- Data Contract (§15 Standardization) ----------------
def _get_contract(db: Session, contract_id: str) -> DataContract:
    contract = db.get(DataContract, contract_id)
    if contract is None:
        raise HTTPException(404, f"data contract not found: {contract_id}")
    return contract


def _context(db: Session, contract: DataContract) -> tuple[DataProduct, OutputPort, DataAsset]:
    port = db.get(OutputPort, contract.output_port_id)
    return db.get(DataProduct, contract.product_id), port, db.get(DataAsset, port.asset_id)


def _get_version(db: Session, version_id: str) -> tuple[ContractVersion, DataContract]:
    version = db.get(ContractVersion, version_id)
    if version is None:
        raise HTTPException(404, f"contract version not found: {version_id}")
    return version, db.get(DataContract, version.contract_id)


def _content(body: ContractVersionIn) -> dict[str, Any]:
    return body.model_dump(by_alias=False, exclude={"version"}, mode="json")


@router.post("/contracts", response_model=ContractOut, status_code=201)
def create_contract(body: ContractIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    port = _get_port(db, body.output_port_id)
    product = db.get(DataProduct, port.product_id)
    authz.require(db, principal, "mesh.contract.write", domain=product.domain, entity_type="output_port", entity_id=port.id)
    contract = _guard(mesh_contracts.create_contract, db, port, name=body.name, actor=principal.subject)
    audit.record(db, principal.audit_actor, "mesh.contract.create", "data_contract", contract.id,
                 {"output_port_id": port.id, "product_id": product.id})
    return contract


@router.get("/contracts", response_model=list[ContractOut])
def list_contracts(request: Request, response: Response, product_id: str | None = None, page: Page = Depends(page_params()),
                   db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """閲覧できない Output Port の Contract は件数を含めて除外する (ABAC)。"""
    stmt = select(DataContract).join(OutputPort, DataContract.output_port_id == OutputPort.id) \
        .join(DataAsset, OutputPort.asset_id == DataAsset.id).where(authz.readable_clause(principal)) \
        .order_by(DataContract.created_at, DataContract.id)
    if product_id:
        stmt = stmt.where(DataContract.product_id == product_id)
    return paginate(db, stmt, page, request, response)


@router.get("/contracts/{contract_id}", response_model=ContractOut)
def get_contract(contract_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    contract = _get_contract(db, contract_id)
    _, port, _ = _context(db, contract)
    load_asset(db, principal, port.asset_id)
    return contract


@router.post("/contracts/{contract_id}/versions", response_model=ContractVersionOut, status_code=201)
def create_contract_version(contract_id: str, body: ContractVersionIn, db: Session = Depends(get_session),
                            principal: Principal = Depends(get_principal)):
    contract = _get_contract(db, contract_id)
    product, _, asset = _context(db, contract)
    authz.require(db, principal, "mesh.contract.write", domain=product.domain, entity_type="data_contract", entity_id=contract.id)
    version = _guard(mesh_contracts.create_version, db, contract, asset, version=body.version, content=_content(body),
                     actor=principal.subject)
    audit.record(db, principal.audit_actor, "mesh.contract.version_create", "contract_version", version.id,
                 {"contract_id": contract.id, "version": version.version})
    return version


@router.get("/contracts/{contract_id}/versions", response_model=list[ContractVersionOut])
def list_contract_versions(contract_id: str, request: Request, response: Response, page: Page = Depends(page_params()),
                           db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    contract = _get_contract(db, contract_id)
    _, port, _ = _context(db, contract)
    load_asset(db, principal, port.asset_id)
    stmt = select(ContractVersion).where(ContractVersion.contract_id == contract.id) \
        .order_by(ContractVersion.major, ContractVersion.minor, ContractVersion.patch, ContractVersion.id)
    return paginate(db, stmt, page, request, response)


@router.get("/contract-versions/{version_id}", response_model=ContractVersionOut)
def get_contract_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    version, contract = _get_version(db, version_id)
    _, port, _ = _context(db, contract)
    load_asset(db, principal, port.asset_id)
    return version


@router.put("/contract-versions/{version_id}", response_model=ContractVersionOut)
def update_contract_version(version_id: str, body: ContractVersionIn, db: Session = Depends(get_session),
                            principal: Principal = Depends(get_principal)):
    """下書きだけを変更できる。公開した版は変更できない (409)。"""
    version, contract = _get_version(db, version_id)
    product, _, asset = _context(db, contract)
    authz.require(db, principal, "mesh.contract.write", domain=product.domain, entity_type="contract_version", entity_id=version.id)
    _guard(mesh_contracts.update_draft, db, version, asset, version_label=body.version, content=_content(body))
    audit.record(db, principal.audit_actor, "mesh.contract.version_update", "contract_version", version.id,
                 {"contract_id": contract.id, "version": version.version})
    return version


@router.post("/contract-versions/{version_id}/publish", response_model=ContractVersionOut)
def publish_contract_version(version_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """Product の Owner の承認で公開する。前の版と比べて必要な版の上げ方に足りなければ 409 で拒否し、証跡を残す。"""
    version, contract = _get_version(db, version_id)
    product, port, asset = _context(db, contract)
    _require_owner(db, principal, "mesh.contract.publish", product, "contract_version", version.id)
    mesh_contracts.lock(db, contract)
    try:
        result = _guard(mesh_contracts.publish, db, principal.audit_actor, contract, version, product, port, asset,
                        principal.subject)
    except mesh_contracts.VersionBumpRequired as e:
        _reject_publish(db, principal, version_id, contract.id, product.domain, e)
    audit.record(db, principal.audit_actor, "mesh.contract.publish", "contract_version", version.id, {
        "contract_id": contract.id, "version": version.version, **result})
    return version


def _reject_publish(db: Session, principal: Principal, version_id: str, contract_id: str, domain: str,
                    error: mesh_contracts.VersionBumpRequired) -> None:
    """拒否も統制の実行なので証跡を確定させる (§13 Evidence。KPI の集計にも使う)。"""
    db.rollback()
    audit.record(db, principal.audit_actor, "mesh.contract.publish_rejected", "contract_version", version_id, {
        "contract_id": contract_id, "domain": domain, "required_bump": error.required, "actual_bump": error.actual,
        "breaking": error.breaking, "changes": [c["code"] for c in error.changes]})
    db.commit()
    raise ProblemError(409, str(error), type_="contract-version-bump-required", required_bump=error.required,
                       actual_bump=error.actual, changes=error.changes)


@router.post("/contracts/{contract_id}/check")
def check_contract(contract_id: str, body: ContractCheckIn, db: Session = Depends(get_session),
                   principal: Principal = Depends(get_principal)):
    """Data Plane の CI からデプロイ前に呼ぶ判定 (ステートレス。呼び出し頻度が高いので監査には記録しない)。"""
    contract = _get_contract(db, contract_id)
    product, port, asset = _context(db, contract)
    load_asset(db, principal, asset.id, "mesh.contract.check")
    proposal = body.model_dump(by_alias=False, exclude_unset=True, exclude={"version"}, mode="json")
    result = mesh_contracts.check(db, contract, asset, proposal, body.version)
    return {**result, "impact": redacted_impact(db, principal, asset.qualified_name),
            "subscribers": mesh_subscriptions.subscribers_of(db, contract.id)}


@router.post("/contracts/{contract_id}/majors/{major}/retire", response_model=list[ContractVersionOut])
def retire_major(contract_id: str, major: int, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """期間が満了し、かつ有効な利用登録が残っていない旧版だけを retired にできる (Product の Owner)。"""
    contract = _get_contract(db, contract_id)
    product, _, _ = _context(db, contract)
    _require_owner(db, principal, "mesh.contract.publish", product, "data_contract", contract.id)
    mesh_contracts.lock(db, contract)
    versions = _guard(mesh_contracts.retire_major, db, contract, major, principal.subject)
    audit.record(db, principal.audit_actor, "mesh.contract.retire", "data_contract", contract.id,
                 {"major": major, "versions": [v.version for v in versions]})
    return versions


@router.post("/contracts/{contract_id}/majors/{major}/expedite-retire")
def expedite_retire(contract_id: str, major: int, body: ExpediteRetireIn, db: Session = Depends(get_session),
                    principal: Principal = Depends(get_principal)):
    """Committee が併存期間を短縮する (理由必須)。retire は別途 Product の Owner が行い、利用登録の条件は残る。"""
    contract = _get_contract(db, contract_id)
    authz.require(db, principal, "mesh.contract.expedite_retire", entity_type="data_contract", entity_id=contract.id)
    mesh_contracts.lock(db, contract)
    sunset = _guard(mesh_contracts.expedite_retire, db, contract, major, reason=body.reason, sunset_at=body.sunset_at,
                    actor=principal.subject)
    audit.record(db, principal.audit_actor, "mesh.contract.expedite_retire", "data_contract", contract.id,
                 {"major": major, "reason": body.reason, "sunset_at": sunset})
    return {"contract_id": contract.id, "major": major, "sunset_at": sunset}


# ---------------- 利用登録 (§15 / §4 Data Consumer)。閲覧権限は与えない ----------------
def _get_subscription(db: Session, subscription_id: str) -> tuple[MeshSubscription, DataProduct]:
    row = db.get(MeshSubscription, subscription_id)
    if row is None:
        raise HTTPException(404, f"subscription not found: {subscription_id}")
    return row, db.get(DataProduct, row.product_id)


def _deny_subscription(db: Session, principal: Principal, row: MeshSubscription, reason: str) -> None:
    authz.deny(db, principal, "mesh.subscription.request", entity_type="subscription", entity_id=row.id, reason=reason)


def _require_party(db: Session, principal: Principal, row: MeshSubscription, product: DataProduct) -> None:
    if principal.subject not in (row.subscriber, product.owner):
        _deny_subscription(db, principal, row, "利用者本人または Product の Owner だけが変更できます")


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=201)
def request_subscription(body: SubscriptionIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """自分の利用登録を申請する。confidential 以上は Product の Owner の承認が必要。"""
    authz.require(db, principal, "mesh.subscription.request", entity_type="output_port", entity_id=body.output_port_id)
    port = _get_port(db, body.output_port_id)
    contract = db.scalar(select(DataContract).where(DataContract.output_port_id == port.id))
    if contract is None:
        raise HTTPException(409, "この Output Port には Contract がありません")
    mesh_contracts.lock(db, contract)
    row = _guard(mesh_subscriptions.request, db, contract=contract, port=port, product=db.get(DataProduct, port.product_id),
                 asset=db.get(DataAsset, port.asset_id), subscriber=principal.subject, purpose_id=body.purpose_id, major=body.major)
    audit.record(db, principal.audit_actor, "mesh.subscription.request", "subscription", row.id, {
        "output_port_id": port.id, "major": row.major, "purpose_id": row.purpose_id, "status": row.status})
    return row


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(request: Request, response: Response, status: str | None = None, output_port_id: str | None = None,
                       page: Page = Depends(page_params()), db: Session = Depends(get_session),
                       principal: Principal = Depends(get_principal)):
    """Committee / admin は全件。それ以外は自分の登録と、自分が Owner の Product への登録だけ。"""
    stmt = select(MeshSubscription).order_by(MeshSubscription.created_at, MeshSubscription.id)
    if not authz.is_allowed(principal, "mesh.subscription.read"):
        owned = select(DataProduct.id).where(DataProduct.owner == principal.subject)
        stmt = stmt.where(or_(MeshSubscription.subscriber == principal.subject, MeshSubscription.product_id.in_(owned)))
    for column, value in ((MeshSubscription.status, status), (MeshSubscription.output_port_id, output_port_id)):
        if value is not None:
            stmt = stmt.where(column == value)
    return paginate(db, stmt, page, request, response)


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
def get_subscription(subscription_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    row, product = _get_subscription(db, subscription_id)
    if principal.subject not in (row.subscriber, product.owner) and not authz.is_allowed(principal, "mesh.subscription.read"):
        authz.require(db, principal, "mesh.subscription.read", entity_type="subscription", entity_id=row.id)
    return row


@router.post("/subscriptions/{subscription_id}/decision", response_model=SubscriptionOut)
def decide_subscription(subscription_id: str, body: SubscriptionDecisionIn, db: Session = Depends(get_session),
                        principal: Principal = Depends(get_principal)):
    """Product の Owner の決裁。自分の申請は決裁できない (職務分離)。"""
    row, product = _get_subscription(db, subscription_id)
    _require_owner(db, principal, "mesh.subscription.approve", product, "subscription", row.id)
    if row.subscriber == principal.subject:
        authz.deny(db, principal, "mesh.subscription.approve", entity_type="subscription", entity_id=row.id,
                   reason="自分の利用登録は決裁できません (職務分離)")
    mesh_contracts.lock(db, db.get(DataContract, row.contract_id))
    _guard(mesh_subscriptions.decide, db, row, approve=body.approve, comment=body.comment, actor=principal.subject)
    audit.record(db, principal.audit_actor, "mesh.subscription.decision", "subscription", row.id,
                 {"approve": body.approve, "comment": body.comment, "status": row.status})
    return row


@router.post("/subscriptions/{subscription_id}/migrate", response_model=SubscriptionOut)
def migrate_subscription(subscription_id: str, body: SubscriptionMigrateIn, db: Session = Depends(get_session),
                         principal: Principal = Depends(get_principal)):
    """新しいメジャー版に付け替える。機密度が上がる場合は承認を取り直す。"""
    row, product = _get_subscription(db, subscription_id)
    _require_party(db, principal, row, product)
    contract = db.get(DataContract, row.contract_id)
    mesh_contracts.lock(db, contract)
    previous = row.major
    _guard(mesh_subscriptions.migrate, db, row, contract=contract, asset=db.get(DataAsset, db.get(OutputPort, row.output_port_id).asset_id),
           major=body.major, purpose_id=body.purpose_id)
    audit.record(db, principal.audit_actor, "mesh.subscription.migrate", "subscription", row.id,
                 {"from_major": previous, "to_major": row.major, "purpose_id": row.purpose_id, "status": row.status})
    return row


@router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut)
def cancel_subscription(subscription_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    row, product = _get_subscription(db, subscription_id)
    _require_party(db, principal, row, product)
    mesh_contracts.lock(db, db.get(DataContract, row.contract_id))
    _guard(mesh_subscriptions.cancel, db, row)
    audit.record(db, principal.audit_actor, "mesh.subscription.cancel", "subscription", row.id, {"status": row.status})
    return row


# ---------------- CDM の取り込み (外部の標準の写し。作成・編集・設計の機能は持たない) ----------------
@router.post("/cdm/imports", status_code=201)
def import_cdm(body: CdmImportIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """版を取り込む (Committee の承認)。取り込んだ版は変更できない。準拠している Contract の再検証の結果を返す。"""
    authz.require(db, principal, "cdm.import", entity_type="cdm", entity_id=f"{body.entity}@{body.version}")
    row, warnings = _guard(mesh_cdm.import_version, db, entity=body.entity, asset_id=body.asset_id, version=body.version,
                           json_schema=body.json_schema, source_uri=body.source_uri, actor=principal.subject)
    results = mesh_contracts.revalidate_for_cdm(db, principal.audit_actor, row.entity, row.version)
    audit.record(db, principal.audit_actor, "cdm.import", "cdm", f"{row.entity}@{row.version}", {
        "content_hash": row.content_hash, "source_uri": row.source_uri, "warnings": warnings,
        "failed_contracts": [r["contract_id"] for r in results if not r["ok"]]})
    return {"version": CdmVersionOut.model_validate(row), "warnings": warnings, "revalidation": results}


@router.get("/cdm/entities", response_model=list[CdmEntityOut])
def list_cdm_entities(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                      principal: Principal = Depends(get_principal)):
    stmt = select(CdmEntity).join(DataAsset, CdmEntity.asset_id == DataAsset.id).where(authz.readable_clause(principal)) \
        .order_by(CdmEntity.name)
    return paginate(db, stmt, page, request, response)


def _get_cdm_entity(db: Session, principal: Principal, name: str) -> CdmEntity:
    entity = db.get(CdmEntity, name)
    if entity is None:
        raise HTTPException(404, f"cdm entity not found: {name}")
    load_asset(db, principal, entity.asset_id)
    return entity


@router.get("/cdm/entities/{name}", response_model=CdmEntityOut)
def get_cdm_entity(name: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    return _get_cdm_entity(db, principal, name)


@router.get("/cdm/entities/{name}/versions", response_model=list[CdmVersionOut])
def list_cdm_versions(name: str, request: Request, response: Response, page: Page = Depends(page_params()),
                      db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _get_cdm_entity(db, principal, name)
    stmt = select(CdmVersion).where(CdmVersion.entity == name).order_by(CdmVersion.imported_at, CdmVersion.id)
    return paginate(db, stmt, page, request, response)


@router.get("/cdm/entities/{name}/versions/{version}", response_model=CdmVersionOut)
def get_cdm_version(name: str, version: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    _get_cdm_entity(db, principal, name)
    row = db.scalar(select(CdmVersion).where(CdmVersion.entity == name, CdmVersion.version == version))
    if row is None:
        raise HTTPException(404, f"cdm version not found: {name}@{version}")
    return row
