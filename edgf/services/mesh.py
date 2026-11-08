"""§15 Data Governance & Data Mesh: Data Product と Output Port の定義.

Meshが実行、Governanceが統制 (§15.1)。ここで扱うのは統治のための定義だけで、
配信基盤、ストレージ、セルフサービス基盤、パイプラインの実行は実装しない。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import LifecycleState, ProductStatus
from edgf.models import DataAsset, DataProduct, Domain, GlossaryTerm, OutputPort

RETIRED = ProductStatus.RETIRED.value


class MeshValidationError(ValueError):
    """入力の不備 (422)。"""


class MeshStateError(ValueError):
    """現在の状態では実行できない操作 (409)。"""


def is_domain_data_owner(db: Session, domain: Domain, subject: str) -> bool:
    """ドメインの Data Owner か。§4 / §15 の Owner は、ドメインの Owner か、そのドメインの資産の Owner。"""
    if domain.owner == subject:
        return True
    return db.scalar(select(DataAsset.id).where(DataAsset.domain == domain.name, DataAsset.owner == subject).limit(1)) is not None


def _check_owner(db: Session, domain_name: str, owner: str) -> None:
    domain = db.get(Domain, domain_name)
    if domain is None:
        raise MeshValidationError(f"unknown domain: {domain_name}")
    if not is_domain_data_owner(db, domain, owner):
        raise MeshValidationError(f"{owner} はドメイン {domain_name} の Data Owner ではありません (§15 Domain Ownership)")


def _terms(db: Session, term_ids: Sequence[str]) -> list[GlossaryTerm]:
    terms = []
    for term_id in dict.fromkeys(term_ids):
        term = db.get(GlossaryTerm, term_id)
        if term is None:
            raise MeshValidationError(f"Glossary の用語が見つかりません: {term_id}")
        terms.append(term)
    return terms


def create_product(db: Session, *, name: str, domain: str, owner: str, description: str, term_ids: Sequence[str],
                   actor: str) -> DataProduct:
    _check_owner(db, domain, owner)
    if db.scalar(select(DataProduct.id).where(DataProduct.domain == domain, DataProduct.name == name)):
        raise MeshStateError(f"Data Product は登録済みです: {domain}/{name}")
    product = DataProduct(name=name, domain=domain, owner=owner, description=description, created_by=actor,
                          status=ProductStatus.DRAFT.value, terms=_terms(db, term_ids))
    db.add(product)
    db.flush()
    return product


def update_product(db: Session, product: DataProduct, changes: Mapping[str, Any]) -> None:
    if product.status == RETIRED:
        raise MeshStateError("retired の Data Product は変更できません")
    if changes.get("owner") is not None:
        _check_owner(db, product.domain, changes["owner"])
        product.owner = changes["owner"]
    if changes.get("description") is not None:
        product.description = changes["description"]
    if changes.get("term_ids") is not None:
        product.terms = _terms(db, changes["term_ids"])
    db.flush()


def add_port(db: Session, product: DataProduct, *, name: str, asset_id: str, port_type: str, address_uri: str | None) -> OutputPort:
    if product.status == RETIRED:
        raise MeshStateError("retired の Data Product には Output Port を追加できません")
    asset = db.get(DataAsset, asset_id)
    if asset is None or asset.lifecycle_state == LifecycleState.DELETED.value:
        raise MeshValidationError(f"Output Port の資産が Catalog にありません: {asset_id} (§6.3 Metadata First)")
    if asset.domain != product.domain:
        raise MeshValidationError(f"Output Port の資産は Product と同じドメインに属する必要があります: {asset.domain} != {product.domain}")
    if db.scalar(select(OutputPort.id).where(OutputPort.asset_id == asset.id)):
        raise MeshStateError(f"この資産は既に Output Port に対応付けられています: {asset.qualified_name}")
    if db.scalar(select(OutputPort.id).where(OutputPort.product_id == product.id, OutputPort.name == name)):
        raise MeshStateError(f"Output Port の名前が重複しています: {name}")
    port = OutputPort(product_id=product.id, asset_id=asset.id, name=name, port_type=port_type, address_uri=address_uri)
    db.add(port)
    db.flush()
    return port
