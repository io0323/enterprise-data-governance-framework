"""§15 Data Governance & Data Mesh API."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import DataAsset, DataProduct, OutputPort
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import OutputPortIn, OutputPortOut, OutputPortUpdate, ProductIn, ProductOut, ProductUpdate
from edgf.services import audit, mesh
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
    return OutputPortOut(id=port.id, product_id=port.product_id, name=port.name, asset_id=port.asset_id,
                         port_type=port.port_type, address_uri=port.address_uri)


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
