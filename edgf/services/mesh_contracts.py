"""§15 Standardization: Data Contract の版の管理、公開、スキーマのずれの検出."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import SENSITIVITY_LEVELS
from edgf.config import settings
from edgf.enums import ContractStatus, EscalationLevel, ProductStatus
from edgf.errors import ProblemError
from edgf.models import (ContractVersion, DataAsset, DataContract, DataProduct, OutputPort, PrivacyPurpose, QualityRule,
                         fit_identifier, to_naive_utc, utcnow)
from edgf.services import audit, issues, mesh_cdm, mesh_compat, mesh_subscriptions, outbox
from edgf.services.mesh import MeshStateError, MeshValidationError
from edgf.services.mesh_compat import Bump
from edgf.services.quality import latest_run

C = ContractStatus
LIVE = (C.PUBLISHED.value, C.DEPRECATED.value)
CONTENT_KEYS = ("description", "schema_", "quality_sla", "freshness_sla_hours", "sensitivity", "purposes", "conforms_to", "cdm_mappings")


class VersionBumpRequired(Exception):
    """前の版と比べて、版の上げ方が足りない (409)。"""

    def __init__(self, message: str, *, required: str, actual: str | None, changes: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.required, self.actual, self.changes = required, actual, changes
        self.breaking = [c["code"] for c in changes if c["breaking"]]


def lock(db: Session, contract: DataContract) -> None:
    """公開、利用登録、retire を Contract の行ロックで直列化する (同時に実行すると版の順序や条件の判定が崩れる)。"""
    db.refresh(contract, with_for_update=True)


def _require_open_product(db: Session, product_id: str) -> None:
    status = db.get(DataProduct, product_id).status
    if status == ProductStatus.RETIRED.value:
        raise MeshStateError("retired の Data Product の Contract は変更できません")


def create_contract(db: Session, port: OutputPort, *, name: str | None, actor: str) -> DataContract:
    _require_open_product(db, port.product_id)
    if db.scalar(select(DataContract.id).where(DataContract.output_port_id == port.id)):
        raise MeshStateError("この Output Port の Contract は登録済みです")
    contract = DataContract(product_id=port.product_id, output_port_id=port.id, name=name or port.name,
                            schema_object=port.name, created_by=actor)
    db.add(contract)
    db.flush()
    return contract


def _validate_content(db: Session, asset: DataAsset, content: Mapping[str, Any]) -> None:
    for purpose_id in content.get("purposes") or []:
        purpose = db.get(PrivacyPurpose, purpose_id)
        if purpose is None:
            raise MeshValidationError(f"unknown purpose: {purpose_id}")
        if purpose.withdrawn_at:
            raise MeshValidationError(f"目的 {purpose_id} は撤回されています")
    rule_ids = (content.get("quality_sla") or {}).get("rule_ids") or []
    if rule_ids:
        known = set(db.scalars(select(QualityRule.id).where(QualityRule.asset_id == asset.id)))
        missing = sorted(set(rule_ids) - known)
        if missing:
            raise MeshValidationError(f"Output Port の資産に無い品質ルールです: {missing}")


def _set_label(row: ContractVersion, label: str) -> None:
    row.version = label
    row.major, row.minor, row.patch = mesh_compat.parse_version(label)


def create_version(db: Session, contract: DataContract, asset: DataAsset, *, version: str, content: Mapping[str, Any], actor: str) -> ContractVersion:
    _require_open_product(db, contract.product_id)
    _validate_content(db, asset, content)
    if db.scalar(select(ContractVersion.id).where(ContractVersion.contract_id == contract.id, ContractVersion.version == version)):
        raise MeshStateError(f"版 {version} は登録済みです")
    row = ContractVersion(contract_id=contract.id, status=C.DRAFT.value, created_by=actor, **content)
    _set_label(row, version)
    db.add(row)
    db.flush()
    return row


def update_draft(db: Session, row: ContractVersion, asset: DataAsset, *, version_label: str, content: Mapping[str, Any]) -> None:
    if row.status != C.DRAFT.value:
        raise MeshStateError(f"状態が {row.status} の版は変更できません (公開した版は不変です)")
    _validate_content(db, asset, content)
    if version_label != row.version and db.scalar(select(ContractVersion.id).where(
            ContractVersion.contract_id == row.contract_id, ContractVersion.version == version_label)):
        raise MeshStateError(f"版 {version_label} は登録済みです")
    _set_label(row, version_label)
    for key, value in content.items():
        setattr(row, key, value)
    db.flush()


def latest_published(db: Session, contract: DataContract) -> ContractVersion | None:
    return db.scalar(select(ContractVersion).where(ContractVersion.contract_id == contract.id, ContractVersion.status.in_(LIVE))
                     .order_by(ContractVersion.major.desc(), ContractVersion.minor.desc(), ContractVersion.patch.desc()).limit(1))


def view(row: ContractVersion) -> dict[str, Any]:
    return {k: getattr(row, k) for k in CONTENT_KEYS}


def quality_status(db: Session, asset: DataAsset, sla: Mapping[str, Any] | None) -> dict[str, Any]:
    """品質 SLA を最新の品質検査の結果と照合する。値は扱わず、スコアとルールの結果だけを見る。"""
    sla = sla or {}
    min_score, rule_ids = sla.get("min_score"), sla.get("rule_ids") or []
    declared = min_score is not None or bool(rule_ids)
    run = latest_run(db, asset.id)
    breaches = []
    if declared and run is None:
        breaches.append("品質検査の実績がありません")
    elif run is not None:
        if min_score is not None and run.score < min_score:
            breaches.append(f"Quality Score {run.score} < {min_score}")
        results = {r["rule_id"]: r for r in run.results or []}
        for rule_id in rule_ids:
            result = results.get(rule_id)
            if result is None or result.get("breached"):
                breaches.append(f"品質ルール {rule_id} を満たしていません")
    return {"declared": declared, "ok": declared and not breaches, "score": run.score if run else None, "breaches": breaches}


def check(db: Session, contract: DataContract, asset: DataAsset, proposal: Mapping[str, Any], version: str | None) -> dict[str, Any]:
    """案を最新の公開版と比べる。何も保存しない。"""
    baseline = latest_published(db, contract)
    merged = {**(view(baseline) if baseline else {}), **proposal}
    if baseline is None:
        result: dict[str, Any] = {"baseline_version": None, "changes": [], "breaking_changes": [], "required_bump": None,
                                  "suggested_version": version or "1.0.0", "allowed": True}
    else:
        changes = mesh_compat.compare(view(baseline), merged)
        need = mesh_compat.required_bump(changes)
        old = (baseline.major, baseline.minor, baseline.patch)
        allowed = None
        if version is not None:
            actual = mesh_compat.actual_bump(old, mesh_compat.parse_version(version))
            allowed = actual is not None and actual >= need
        result = {"baseline_version": baseline.version, "changes": changes,
                  "breaking_changes": [c for c in changes if c["breaking"]], "required_bump": need.name.lower(),
                  "suggested_version": mesh_compat.next_version(old, need), "allowed": allowed}
    result["quality"] = quality_status(db, asset, merged.get("quality_sla"))
    result["conformance"] = conformance(db, merged)
    return result


def conformance(db: Session, content: Mapping[str, Any]) -> dict[str, Any] | None:
    """conforms_to があれば準拠を検証する。宣言が無ければ None。"""
    target = content.get("conforms_to")
    if not target:
        return None
    violations = mesh_cdm.validate(db, target, content.get("schema_") or [], content.get("cdm_mappings") or {})
    return {"conforms_to": target, "ok": not violations, "violations": violations}


def _check_publishable(db: Session, row: ContractVersion, baseline: ContractVersion | None, asset: DataAsset) -> tuple[list[dict[str, Any]], Bump | None]:
    if row.status != C.DRAFT.value:
        raise MeshStateError(f"状態が {row.status} の版は公開できません")
    level = SENSITIVITY_LEVELS.index
    if level(row.sensitivity) < level(authz.effective_sensitivity(asset)):
        raise MeshValidationError(f"Contract の機密度 {row.sensitivity} が資産の機密度 {authz.effective_sensitivity(asset)} を下回っています")
    result = conformance(db, view(row))
    if result and not result["ok"]:
        raise ProblemError(409, f"{result['conforms_to']} に準拠していません (§15 Standardization)",
                           type_="contract-not-conforming", violations=result["violations"])
    if baseline is None:
        return [], None
    old, new = (baseline.major, baseline.minor, baseline.patch), (row.major, row.minor, row.patch)
    actual = mesh_compat.actual_bump(old, new)
    changes = mesh_compat.compare(view(baseline), view(row))
    need = mesh_compat.required_bump(changes)
    if actual is None:
        raise MeshStateError(f"版 {row.version} は最新の公開版 {baseline.version} より新しくありません")
    if actual < need:
        raise VersionBumpRequired(f"この変更には {need.name.lower()} 版の更新が必要です (指定は {actual.name.lower()} 版の更新)",
                                  required=need.name.lower(), actual=actual.name.lower(), changes=changes)
    return changes, actual


def publish(db: Session, actor: str, contract: DataContract, row: ContractVersion, product: DataProduct, port: OutputPort,
            asset: DataAsset, publisher: str) -> dict[str, Any]:
    _require_open_product(db, contract.product_id)
    baseline = latest_published(db, contract)
    changes, _ = _check_publishable(db, row, baseline, asset)
    now = utcnow()
    row.status, row.published_by, row.published_at = C.PUBLISHED.value, publisher, now
    db.flush()
    result: dict[str, Any] = {"changes": [c["code"] for c in changes]}
    if baseline is not None and row.major > baseline.major:
        result.update(_on_new_major(db, actor, contract, row, product, asset, now))
    check_drift(db, actor, asset)
    outbox.emit(db, "contract.published", row.id, {"contract_id": contract.id, "contract_version_id": row.id, "version": row.version,
                                                   "product_id": product.id, "output_port_id": port.id, "breaking": [c["code"] for c in changes if c["breaking"]]})
    return result


def _on_new_major(db: Session, actor: str, contract: DataContract, row: ContractVersion, product: DataProduct, asset: DataAsset,
                  now: datetime) -> dict[str, Any]:
    """旧版を deprecated にして併存させ、利用者と下流の Owner に知らせる。利用条件から外れた登録は止める。"""
    sunset = now + timedelta(days=settings.contract_deprecation_days)
    deprecated = db.scalars(select(ContractVersion).where(ContractVersion.contract_id == contract.id,
                                                          ContractVersion.status == C.PUBLISHED.value,
                                                          ContractVersion.major < row.major)).all()
    for old in deprecated:
        old.status, old.deprecated_at, old.sunset_at = C.DEPRECATED.value, now, sunset
    db.flush()
    suspended = mesh_subscriptions.suspend_for_narrowed_usage(db, actor, contract, row, product)
    opened = mesh_subscriptions.notify_new_major(db, actor, contract, row, product, asset, sunset)
    return {"deprecated_majors": sorted({v.major for v in deprecated}), "sunset_at": sunset,
            "subscriptions_suspended": suspended, "issues_opened": opened}


def _major_versions(db: Session, contract: DataContract, major: int) -> list[ContractVersion]:
    return list(db.scalars(select(ContractVersion).where(ContractVersion.contract_id == contract.id, ContractVersion.major == major,
                                                         ContractVersion.status != C.DRAFT.value)
                           .order_by(ContractVersion.minor, ContractVersion.patch)))


def _deprecated_major(db: Session, contract: DataContract, major: int) -> list[ContractVersion]:
    versions = _major_versions(db, contract, major)
    if not versions:
        raise MeshValidationError(f"メジャー版 {major} の公開済みの版はありません")
    if any(v.status != C.DEPRECATED.value for v in versions):
        raise MeshStateError(f"メジャー版 {major} は deprecated ではありません (新しいメジャー版の公開が先に必要です)")
    return versions


def retire_major(db: Session, contract: DataContract, major: int, retirer: str) -> list[ContractVersion]:
    """旧版を retired にできるのは、併存期間が満了し、かつ有効な利用登録が残っていない場合だけ。"""
    versions = _deprecated_major(db, contract, major)
    now = utcnow()
    blockers = []
    sunset = max(v.sunset_at for v in versions)
    if sunset > now:
        blockers.append(f"併存期間が満了していません (満了: {sunset.isoformat()})")
    remaining = mesh_subscriptions.valid_count(db, contract.id, major)
    if remaining:
        blockers.append(f"有効な利用登録が {remaining} 件残っています")
    if blockers:
        raise ProblemError(409, f"メジャー版 {major} を retire できません", type_="contract-retire-blocked", blockers=blockers)
    for version in versions:
        version.status, version.retired_at, version.retired_by = C.RETIRED.value, now, retirer
    db.flush()
    return versions


def expedite_retire(db: Session, contract: DataContract, major: int, *, reason: str, sunset_at: datetime | None, actor: str) -> datetime:
    """Committee による併存期間の短縮。retire の条件のうち、期間だけを変える (利用登録の条件は残る)。"""
    versions = _deprecated_major(db, contract, major)
    new_sunset = to_naive_utc(sunset_at) if sunset_at else utcnow()
    if new_sunset >= max(v.sunset_at for v in versions):
        raise MeshValidationError("sunset_at は今の満了日時より前にしてください (延長はできません)")
    for version in versions:
        version.sunset_at, version.sunset_shortened_by, version.sunset_shortened_reason = new_sunset, actor, reason
    db.flush()
    return new_sunset


def drift_of(db: Session, asset: DataAsset, row: ContractVersion) -> list[dict[str, Any]]:
    """Contract と Catalog の実際のスキーマのずれ。説明の違いは含めない。"""
    catalog = [{"name": c.get("name"), "type": c.get("type", "string"), "nullable": c.get("nullable"), "pii": c.get("pii"),
                "description": next((x.get("description") for x in row.schema_ if x["name"] == c.get("name")), ""),
                "identifier_type": next((x.get("identifier_type") for x in row.schema_ if x["name"] == c.get("name")), None),
                "reference_set": next((x.get("reference_set") for x in row.schema_ if x["name"] == c.get("name")), None)}
               for c in asset.schema_ or []]
    # Catalog に nullable の宣言が無いカラムは比較しない
    contract_side = []
    declared = {c["name"]: c["nullable"] for c in catalog}
    for col in row.schema_:
        contract_side.append({**col, "nullable": col.get("nullable") if declared.get(col["name"]) is not None else None})
    changes = mesh_compat.compare_schema(contract_side, catalog)
    level = SENSITIVITY_LEVELS.index
    effective = authz.effective_sensitivity(asset)
    if level(effective) > level(row.sensitivity):
        changes.append({**mesh_compat.make_change("sensitivity_raised", "sensitivity", f"{row.sensitivity} → {effective}")})
    return changes


def check_drift(db: Session, actor: str, asset: DataAsset) -> list[dict[str, Any]]:
    """Catalog の更新は拒否せず、ずれがあれば Output Port の資産の Owner と Product の Owner に Issue で知らせる (§15)。"""
    port = db.scalar(select(OutputPort).where(OutputPort.asset_id == asset.id))
    contract = db.scalar(select(DataContract).where(DataContract.output_port_id == port.id)) if port else None
    row = latest_published(db, contract) if contract else None
    if row is None:
        return []
    changes = drift_of(db, asset, row)
    if not changes:
        return []
    product = db.get(DataProduct, contract.product_id)
    ref = drift_ref(contract.id)
    issue = issues.open_issue(
        db, actor, asset=asset, kind="contract", severity="high", source_ref=ref, level=EscalationLevel.OWNER,
        title=f"[CONTRACT] {asset.qualified_name}: Contract {row.version} と Catalog のスキーマがずれています",
        description="ずれの内容: " + ", ".join(f"{c['code']}({c['target']})" for c in changes) + " (§15 Data Contract)")
    if issue:
        issue.assignee = product.owner
        audit.record(db, actor, "mesh.contract.drift", "data_contract", contract.id, {
            "version": row.version, "asset_id": asset.id, "issue_id": issue.id, "changes": [c["code"] for c in changes]})
        db.flush()
    return changes


def drift_ref(contract_id: str) -> str:
    return fit_identifier(f"mesh-drift:{contract_id}", 100)


def revalidate_for_cdm(db: Session, actor: str, entity: str, version: str) -> list[dict[str, Any]]:
    """CDM の新しい版で、その実体に準拠している Contract の最新の公開版を再検証する。不合格なら Owner に Issue を起票する。

    Contract は宣言した版に準拠したままなので、状態は変えない (移行の判断は Owner が行う)。
    """
    target = f"{entity}@{version}"
    results = []
    for contract in db.scalars(select(DataContract).order_by(DataContract.created_at, DataContract.id)):
        row = latest_published(db, contract)
        if row is None or not row.conforms_to or row.conforms_to.split("@")[0] != entity:
            continue
        violations = mesh_cdm.validate(db, target, row.schema_, row.cdm_mappings or {})
        item = {"contract_id": contract.id, "version": row.version, "conforms_to": row.conforms_to,
                "ok": not violations, "violations": violations, "issue_id": None}
        if violations:
            product = db.get(DataProduct, contract.product_id)
            port = db.get(OutputPort, contract.output_port_id)
            issue = issues.open_issue(
                db, actor, asset=db.get(DataAsset, port.asset_id), kind="contract", severity="high",
                source_ref=fit_identifier(f"mesh-cdm:{contract.id}:{target}", 100), level=EscalationLevel.OWNER,
                title=f"[CDM] {product.name}: Contract {row.version} は {target} に準拠していません",
                description="; ".join(violations)[:2000])
            if issue:
                issue.assignee = product.owner
                item["issue_id"] = issue.id
        results.append(item)
    db.flush()
    return results
