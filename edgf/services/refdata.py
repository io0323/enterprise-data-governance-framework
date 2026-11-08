"""§8 Reference Data: コード値の版管理.

Control Plane が値を保持する唯一の例外 (§18.2)。機密度が public / internal で、PII を含まない資産に限る。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.enums import EscalationLevel
from edgf.models import DataAsset, QualityRule, RefCode, RefDataSet, RefDataVersion, utcnow
from edgf.services import issues, lineage

DRAFT, PUBLISHED = "draft", "published"
ALLOWED_SENSITIVITY = ("public", "internal")
RULE_TYPES = ("allowed_values", "referential")


class RefDataError(ValueError):
    """入力の不備 (422)。"""


class RefDataStateError(ValueError):
    """状態の不整合 (409)。"""


def check_asset(asset: DataAsset) -> None:
    """値を保持してよい資産かを判定する。違反は 422。"""
    if asset.data_class != "reference":
        raise RefDataError(f"Reference Data Set は data_class=reference の資産に対応付けます (現在: {asset.data_class})")
    if asset.sensitivity not in ALLOWED_SENSITIVITY:
        raise RefDataError(f"機密度 {asset.sensitivity} の資産の値は Control Plane に保持できません (public / internal のみ)")
    if asset.contains_pii:
        raise RefDataError("PII を含む資産の値は Control Plane に保持できません")


def versions_of(db: Session, set_id: str) -> Sequence[RefDataVersion]:
    return db.scalars(select(RefDataVersion).where(RefDataVersion.set_id == set_id).order_by(RefDataVersion.version)).all()


def get_version(db: Session, set_id: str, number: int) -> RefDataVersion | None:
    return db.scalar(select(RefDataVersion).where(RefDataVersion.set_id == set_id, RefDataVersion.version == number))


def codes_of(db: Session, version: RefDataVersion) -> Sequence[RefCode]:
    return db.scalars(select(RefCode).where(RefCode.version_id == version.id).order_by(RefCode.code)).all()


def code_view(code: RefCode) -> dict[str, Any]:
    return {"code": code.code, "label": code.label, "valid_from": code.valid_from, "valid_to": code.valid_to,
            "standard": code.standard, "standard_version": code.standard_version}


def version_view(db: Session, version: RefDataVersion, *, with_codes: bool = True, at: date | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {"set_id": version.set_id, "version": version.version, "status": version.status,
                            "note": version.note, "content_hash": version.content_hash, "created_by": version.created_by,
                            "created_at": version.created_at, "published_by": version.published_by,
                            "published_at": version.published_at}
    if with_codes:
        codes = [c for c in codes_of(db, version) if at is None or _in_force(c, at)]
        view["codes"] = [code_view(c) for c in codes]
    return view


def _in_force(code: RefCode, at: date) -> bool:
    return (code.valid_from is None or code.valid_from <= at) and (code.valid_to is None or at < code.valid_to)


def _replace_codes(db: Session, version: RefDataVersion, codes: Sequence[Mapping[str, Any]]) -> None:
    seen = [c["code"] for c in codes]
    if len(seen) != len(set(seen)):
        raise RefDataError("code が重複しています")
    for c in codes:
        if c.get("valid_from") and c.get("valid_to") and c["valid_to"] <= c["valid_from"]:
            raise RefDataError(f"code {c['code']}: valid_to は valid_from より後である必要があります")
    for old in codes_of(db, version):
        db.delete(old)
    db.flush()
    db.add_all(RefCode(version_id=version.id, **c) for c in codes)
    db.flush()


def create_version(db: Session, data_set: RefDataSet, codes: Sequence[Mapping[str, Any]], note: str, actor: str) -> RefDataVersion:
    number = (db.scalar(select(func.max(RefDataVersion.version)).where(RefDataVersion.set_id == data_set.id)) or 0) + 1
    version = RefDataVersion(set_id=data_set.id, version=number, status=DRAFT, note=note, created_by=actor)
    db.add(version)
    db.flush()
    _replace_codes(db, version, codes)
    return version


def update_draft(db: Session, version: RefDataVersion, codes: Sequence[Mapping[str, Any]]) -> None:
    if version.status != DRAFT:
        raise RefDataStateError("公開済みの版は変更できません。新しい版を作成してください")
    _replace_codes(db, version, codes)


def _content_hash(codes: Sequence[RefCode]) -> str:
    body = json.dumps([code_view(c) for c in codes], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def publish(db: Session, data_set: RefDataSet, version: RefDataVersion, asset: DataAsset, actor: str, audit_actor: str) -> dict[str, Any]:
    """版を公開する。公開後は変更できない。Lineage の下流資産の Owner に通知用の Issue を起票する。"""
    if version.status != DRAFT:
        raise RefDataStateError("この版は公開済みです")
    check_asset(asset)  # 資産の機密度は下書きの作成後に変わっている可能性がある
    codes = codes_of(db, version)
    if not codes:
        raise RefDataStateError("コード値の無い版は公開できません")
    version.status, version.published_by, version.published_at = PUBLISHED, actor, utcnow()
    version.content_hash = _content_hash(codes)
    db.flush()
    impact = lineage.impact_analysis(db, asset.qualified_name)
    opened = []
    for item in impact["impacted"]:
        downstream = db.get(DataAsset, item["asset_id"]) if item["asset_id"] else None
        if downstream is None or downstream.lifecycle_state == "deleted":
            continue
        issue = issues.open_issue(
            db, audit_actor, asset=downstream, kind="refdata", severity="low", level=EscalationLevel.OWNER,
            source_ref=f"refdata:{data_set.id}@{version.version}",
            title=f"[REFDATA] {downstream.qualified_name}: 上流の {data_set.id} 第{version.version}版が公開されました",
            description="参照している区分値の追加・廃止・ラベル変更を確認してください (§8 / §16 Change)。")
        if issue:
            opened.append(issue.id)
    return {"issues_opened": opened, "unregistered_downstream": impact["unregistered_downstream"],
            "owners_notified": impact["owners_to_notify"]}


def latest_published(db: Session, set_id: str) -> RefDataVersion | None:
    return db.scalar(select(RefDataVersion).where(RefDataVersion.set_id == set_id, RefDataVersion.status == PUBLISHED)
                     .order_by(RefDataVersion.version.desc()).limit(1))


def published_as_of(db: Session, set_id: str, at: datetime) -> RefDataVersion | None:
    return db.scalar(select(RefDataVersion).where(RefDataVersion.set_id == set_id, RefDataVersion.status == PUBLISHED,
                                                  RefDataVersion.published_at <= at)
                     .order_by(RefDataVersion.published_at.desc(), RefDataVersion.version.desc()).limit(1))


def diff(db: Session, old: RefDataVersion, new: RefDataVersion) -> dict[str, Any]:
    before = {c.code: c for c in codes_of(db, old)}
    after = {c.code: c for c in codes_of(db, new)}
    return {"set_id": old.set_id, "from_version": old.version, "to_version": new.version,
            "added": [code_view(after[c]) for c in sorted(after.keys() - before.keys())],
            "retired": [code_view(before[c]) for c in sorted(before.keys() - after.keys())],
            "label_changed": [{"code": c, "from": before[c].label, "to": after[c].label}
                              for c in sorted(before.keys() & after.keys()) if before[c].label != after[c].label]}


# ---------------------------------------------------------------- 品質ルールからの参照
def parse_reference(ref: Any) -> tuple[str, int | None]:
    """reference_set: <id>[@version]"""
    if not isinstance(ref, str) or not ref:
        raise RefDataError("reference_set は <id>[@version] の形式で指定してください")
    set_id, sep, version = ref.partition("@")
    if not sep:
        return set_id, None
    if not version.isdigit() or int(version) < 1:
        raise RefDataError("reference_set の版は正の整数で指定してください")
    return set_id, int(version)


def validate_rule_reference(db: Session, rule_type: str, params: Mapping[str, Any]) -> None:
    if "reference_set" not in params:
        return
    if rule_type not in RULE_TYPES:
        raise RefDataError(f"reference_set は {RULE_TYPES} のルールでのみ指定できます")
    set_id, number = parse_reference(params["reference_set"])
    if db.get(RefDataSet, set_id) is None:
        raise RefDataError(f"unknown reference set: {set_id}")
    if number is not None:
        version = get_version(db, set_id, number)
        if version is None or version.status != PUBLISHED:
            raise RefDataError(f"reference set {set_id} の第{number}版は公開されていません")




def _resolve_values(db: Session, ref: str, today: date) -> list[str]:
    set_id, number = parse_reference(ref)
    if number is None:
        version = latest_published(db, set_id)
        if version is None:
            raise RefDataStateError(f"reference set {set_id} に公開済みの版がありません")
        return [c.code for c in codes_of(db, version) if _in_force(c, today)]
    version = get_version(db, set_id, number)
    if version is None or version.status != PUBLISHED:
        raise RefDataStateError(f"reference set {set_id} の第{number}版は公開されていません")
    return [c.code for c in codes_of(db, version)]  # 版を固定した参照は有効期間で絞らない


def resolve_rules(db: Session, rules: Sequence[QualityRule]) -> list[Any]:
    """reference_set を参照するルールを、値を展開した評価用のコピーに置き換える (ルール自体は変更しない)。"""
    today = utcnow().date()
    resolved: list[Any] = []
    for rule in rules:
        params = rule.params or {}
        if "reference_set" not in params:
            resolved.append(rule)
            continue
        values = _resolve_values(db, params["reference_set"], today)
        resolved.append(SimpleNamespace(
            id=rule.id, name=rule.name, dimension=rule.dimension, rule_type=rule.rule_type, column=rule.column,
            threshold=rule.threshold, severity=rule.severity, params={**params, "values": values}))
    return resolved
