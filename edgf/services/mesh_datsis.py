"""§15 Data Product の出荷基準 (DATSIS) を Policy as Code で判定する.

Discoverable / Addressable / Trustworthy / Self-describing / Interoperable / Secure。
ポリシーの定義は policies/global.yaml (MESH-001〜006)。Domain Policy は基準を追加できるが、
同じ ID での上書きはロード時に拒否する (緩和不可。§15 Federated Governance)。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from edgf.enums import Certification, LifecycleState, ProductStatus
from edgf.errors import ProblemError
from edgf.models import ContractVersion, DataAsset, DataContract, DataProduct, OutputPort, PolicyException, QualityRun, RefDataSet, utcnow
from edgf.services import mesh_contracts, mesh_subscriptions, refdata
from edgf.services.mesh import MeshStateError
from edgf.services.policy import Policy, evaluate_asset, evaluate_product, get_policies

REQUIRED_POLICIES = tuple(f"MESH-00{i}" for i in range(1, 7))
P = ProductStatus
BLOCKING_SEVERITIES = ("high", "critical")
Result = tuple[bool, str]
CHUNK = 500
SNAPSHOT_KEY = "datsis_snapshot"


@dataclass
class Snapshot:
    """1 回の一覧のためにまとめて読み込んだ判定の材料。リクエストの中だけで使い、持ち越さない (古い判定を返さない)。"""
    policies: list[Policy]
    assets: dict[str, DataAsset] = field(default_factory=dict)
    versions: dict[str, ContractVersion | None] = field(default_factory=dict)  # Output Port → 最新の公開版
    runs: dict[str, QualityRun] = field(default_factory=dict)  # 資産 → 最新の品質検査
    exceptions: dict[str, set[str]] = field(default_factory=dict)  # 資産 → 有効な例外


def _chunks(values: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def load_snapshot(db: Session, products: Sequence[DataProduct]) -> Snapshot:
    snapshot = Snapshot(policies=get_policies(db=db))
    ports = [p for product in products for p in product.ports]
    asset_ids = [p.asset_id for p in ports]
    for chunk in _chunks(asset_ids):
        snapshot.assets.update({a.id: a for a in db.scalars(select(DataAsset).where(DataAsset.id.in_(chunk)))})
        newest = select(QualityRun.asset_id, func.max(QualityRun.executed_at).label("at")).where(QualityRun.asset_id.in_(chunk)).group_by(QualityRun.asset_id).subquery()
        snapshot.runs.update({r.asset_id: r for r in db.scalars(select(QualityRun).join(newest, and_(QualityRun.asset_id == newest.c.asset_id, QualityRun.executed_at == newest.c.at)))})
        for asset_id, policy_id in db.execute(select(PolicyException.asset_id, PolicyException.policy_id).where(
                PolicyException.asset_id.in_(chunk), PolicyException.status == "approved", PolicyException.expires_at > utcnow())):
            snapshot.exceptions.setdefault(asset_id, set()).add(policy_id)
    contracts: dict[str, str] = {}
    for chunk in _chunks([p.id for p in ports]):
        contracts.update({c.id: c.output_port_id for c in db.scalars(select(DataContract).where(DataContract.output_port_id.in_(chunk)))})
    snapshot.versions = dict.fromkeys((p.id for p in ports), None)
    for chunk in _chunks(list(contracts)):
        rows = db.scalars(select(ContractVersion).where(ContractVersion.contract_id.in_(chunk), ContractVersion.status.in_(mesh_contracts.LIVE))
                          .order_by(ContractVersion.major, ContractVersion.minor, ContractVersion.patch))
        snapshot.versions.update({contracts[v.contract_id]: v for v in rows})  # 版の昇順なので最後の値が最新になる
    return snapshot


def _snapshot(db: Session) -> Snapshot | None:
    return db.info.get(SNAPSHOT_KEY)


def _ports(db: Session, product: DataProduct) -> list[tuple[OutputPort, DataAsset]]:
    snapshot = _snapshot(db)
    if snapshot is not None:
        return [(p, snapshot.assets[p.asset_id]) for p in product.ports]
    return [(p, db.get(DataAsset, p.asset_id)) for p in product.ports]


def _latest(db: Session, port: OutputPort) -> ContractVersion | None:
    snapshot = _snapshot(db)
    if snapshot is not None:
        return snapshot.versions.get(port.id)
    contract = db.scalar(select(DataContract).where(DataContract.output_port_id == port.id))
    return mesh_contracts.latest_published(db, contract) if contract else None


def _quality(db: Session, asset: DataAsset, version: ContractVersion) -> dict[str, Any]:
    snapshot = _snapshot(db)
    if snapshot is not None:
        return mesh_contracts.quality_status(db, asset, version.quality_sla, snapshot.runs.get(asset.id))
    return mesh_contracts.quality_status(db, asset, version.quality_sla)


def _asset_results(db: Session, asset: DataAsset) -> list[dict[str, Any]]:
    snapshot = _snapshot(db)
    if snapshot is not None:
        return evaluate_asset(db, asset, snapshot.policies, excepted=snapshot.exceptions.get(asset.id, set()))
    return evaluate_asset(db, asset)


def _outcome(problems: list[str]) -> Result:
    return not problems, "ok" if not problems else "; ".join(problems)


def _no_ports(product: DataProduct) -> list[str]:
    return [] if product.ports else ["Output Port がありません"]


def discoverable(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    problems = _no_ports(product)
    if not product.description.strip():
        problems.append("Product の説明がありません")
    if not product.terms:
        problems.append("Glossary の用語がありません")
    for port, asset in _ports(db, product):
        if asset.lifecycle_state == LifecycleState.DELETED.value:
            problems.append(f"{port.name}: 資産が Catalog から削除されています")
        elif asset.domain != product.domain:
            problems.append(f"{port.name}: 資産のドメインが Product と異なります ({asset.domain})")
    return _outcome(problems)


def addressable(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    return _outcome(_no_ports(product) + [f"{p.name}: アドレス URI がありません" for p in product.ports if not p.address_uri])


def trustworthy(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    problems = _no_ports(product)
    for port, asset in _ports(db, product):
        version = _latest(db, port)
        if version is None or version.status != "published":
            problems.append(f"{port.name}: 公開済みの Contract がありません")
        else:
            quality = _quality(db, asset, version)
            if not quality["declared"]:
                problems.append(f"{port.name}: 品質 SLA が宣言されていません")
            problems.extend(f"{port.name}: {b}" for b in quality["breaches"])
        if asset.certification != Certification.CERTIFIED.value:
            problems.append(f"{port.name}: 資産が Certified ではありません ({asset.certification})")
    return _outcome(problems)


def self_describing(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    problems = _no_ports(product)
    if not product.description.strip():
        problems.append("Product の説明がありません")
    if not product.owner:
        problems.append("Owner がいません")
    for port in product.ports:
        version = _latest(db, port)
        if version is None:
            problems.append(f"{port.name}: 公開済みの Contract がありません")
            continue
        missing = [c["name"] for c in version.schema_ if not (c.get("description") or "").strip()]
        if missing:
            problems.append(f"{port.name}: 説明の無いカラムがあります: {missing}")
    return _outcome(problems)


def _reference_problem(db: Session, ref: str) -> str | None:
    try:
        set_id, number = refdata.parse_reference(ref)
    except (refdata.RefDataError, ValueError) as e:
        return f"Reference Data の参照の形式が不正です: {ref} ({e})"
    if db.get(RefDataSet, set_id) is None:
        return f"Reference Data がありません: {ref}"
    version = refdata.get_version(db, set_id, number) if number is not None else refdata.latest_published(db, set_id)
    if version is None or version.status != refdata.PUBLISHED:
        return f"公開済みの Reference Data がありません: {ref}"
    return None


def interoperable(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    problems = _no_ports(product)
    pattern = re.compile(check.get("column_name_pattern") or ".*")
    for port, asset in _ports(db, product):
        version = _latest(db, port)
        if version is None:
            problems.append(f"{port.name}: 公開済みの Contract がありません")
            continue
        result = mesh_contracts.conformance(db, mesh_contracts.view(version))
        if result and not result["ok"]:
            problems.append(f"{port.name}: {result['conforms_to']} に準拠していません")
        catalog = {c.get("name"): c for c in asset.schema_ or []}
        for column in version.schema_:
            name = column["name"]
            if not pattern.match(name):
                problems.append(f"{port.name}.{name}: カラム名が命名の標準に沿っていません")
            declared = (catalog.get(name) or {}).get("identifier_type")
            if column.get("identifier_type") and declared and column["identifier_type"] != declared:
                problems.append(f"{port.name}.{name}: ID の種別が Catalog と異なります ({column['identifier_type']} != {declared})")
            if column.get("reference_set"):
                problem = _reference_problem(db, column["reference_set"])
                if problem:
                    problems.append(f"{port.name}.{name}: {problem}")
    return _outcome(problems)


def secure(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    problems = _no_ports(product)
    for port, asset in _ports(db, product):
        problems.extend(f"{port.name}: {r['policy_id']} に違反しています" for r in _asset_results(db, asset)
                        if r["status"] == "fail" and r["severity"] in BLOCKING_SEVERITIES)
    return _outcome(problems)


_CHECKS: Mapping[str, Callable[[Session, DataProduct, Mapping[str, Any]], Result]] = {
    "mesh_discoverable": discoverable, "mesh_addressable": addressable, "mesh_trustworthy": trustworthy,
    "mesh_self_describing": self_describing, "mesh_interoperable": interoperable, "mesh_secure": secure,
}


def run(db: Session, product: DataProduct, check: Mapping[str, Any]) -> Result:
    return _CHECKS[check["type"]](db, product, check)


def evaluate(db: Session, product: DataProduct) -> tuple[list[dict[str, Any]], list[str]]:
    """全基準の結果と、公開を止める理由。必須のポリシーが定義から消えていても公開させない (fail-close)。"""
    snapshot = _snapshot(db)
    results = evaluate_product(db, product, snapshot.policies if snapshot else None)
    blockers = [f"{r['policy_id']}: {r['message']}" for r in results if r["status"] == "fail"]
    present = {r["policy_id"] for r in results}
    blockers.extend(f"{pid}: 出荷基準のポリシーが定義されていません" for pid in REQUIRED_POLICIES if pid not in present)
    return results, blockers


def overview(db: Session, products: Sequence[DataProduct]) -> Iterator[tuple[DataProduct, list[dict[str, Any]], list[str], Snapshot]]:
    """複数の Product をまとめて判定する。判定の内容は evaluate と同じで、材料の読み方だけが違う。"""
    snapshot = load_snapshot(db, products)
    db.info[SNAPSHOT_KEY] = snapshot
    try:
        for product in products:
            results, blockers = evaluate(db, product)
            yield product, results, blockers, snapshot
    finally:
        db.info.pop(SNAPSHOT_KEY, None)


def passes(db: Session, product: DataProduct) -> bool:
    return not evaluate(db, product)[1]


def publish(db: Session, product: DataProduct, publisher: str) -> None:
    if product.status != P.DRAFT.value:
        raise MeshStateError(f"状態が {product.status} の Data Product は公開できません")
    _, blockers = evaluate(db, product)
    if blockers:
        raise ProblemError(409, "Data Product の出荷基準を満たしていません (§15 DATSIS)", type_="product-publish-blocked",
                           blockers=blockers)
    product.status, product.published_by, product.published_at = P.PUBLISHED.value, publisher, utcnow()
    db.flush()


def deprecate(db: Session, product: DataProduct) -> None:
    if product.status != P.PUBLISHED.value:
        raise MeshStateError(f"状態が {product.status} の Data Product は非推奨にできません")
    product.status, product.deprecated_at = P.DEPRECATED.value, utcnow()
    db.flush()


def retire(db: Session, product: DataProduct) -> None:
    if product.status != P.DEPRECATED.value:
        raise MeshStateError(f"状態が {product.status} の Data Product は廃止できません (先に非推奨にしてください)")
    remaining = len(mesh_subscriptions.valid_for_product(db, product.id))
    if remaining:
        raise MeshStateError(f"有効な利用登録が {remaining} 件残っています")
    product.status, product.retired_at = P.RETIRED.value, utcnow()
    db.flush()
