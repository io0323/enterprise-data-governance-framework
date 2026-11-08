"""§8 Golden Record: マッチングと生成 (ステートレス).

品質エンジンと同じく、受け取ったソースレコードの値は評価に使うだけで保持しない (§18.2)。
生成した Golden Record の値はレスポンスで返し、Data Plane (MDM Hub) が保存する。
Control Plane に残すのは、相互参照 (xref)、属性ごとの由来、版の情報 (内容ハッシュを含む) だけ。
値を DB・ログ・監査に渡してはならないので、例外メッセージにも値を含めない。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.models import DataAsset, GoldenRecord, GoldenVersion, MdmSubjectLink, MdmXref, utcnow
from edgf.services.mdm_rules import Entity
from edgf.services.subject_ref import to_subject_ref

ACTIVE, TOMBSTONED = "active", "tombstoned"


class MdmStateError(ValueError):
    """現在の状態では実行できない (409)。"""


class MdmInputError(ValueError):
    """入力の不備 (422)。メッセージに値を含めないこと。"""


class ErasedSourceError(MdmStateError):
    """消去済みのソースは再度取り込めない (§12)。"""


# ---------------------------------------------------------------- 値の扱い (保持しない)
@dataclass(frozen=True)
class SourceRecord:
    source_system: str
    key: str
    attributes: Mapping[str, Any]
    updated_at: datetime | None = None
    source_ref: str = field(default="")


def make_source_ref(source_system: str, key: str) -> str:
    """ソースのキーを HMAC に変換する。生のキーは保持しない。"""
    return to_subject_ref(f"mdm_source:{source_system}", key)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalized(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def similarity(a: Any, b: Any) -> float:
    return SequenceMatcher(None, _normalized(a), _normalized(b)).ratio()


def validate_attributes(entity: Entity, attributes: Mapping[str, Any]) -> None:
    unknown = [k for k in attributes if k not in entity.attributes]
    if unknown:
        raise MdmInputError(f"エンティティ {entity.entity_type} に定義されていない属性が {len(unknown)} 件あります")


def prepare_record(entity: Entity, source_system: str, key: str, attributes: Mapping[str, Any],
                   updated_at: datetime | None = None) -> SourceRecord:
    if source_system not in entity.sources:
        raise MdmInputError(f"エンティティ {entity.entity_type} の sources に定義されていない source_system です")
    validate_attributes(entity, attributes)
    try:
        ref = make_source_ref(source_system, key)
    except ValueError as e:
        raise MdmInputError("key が空です") from e
    if updated_at is not None and updated_at.tzinfo is not None:
        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    return SourceRecord(source_system, key, attributes, updated_at, ref)


def exact_key_match(entity: Entity, a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[str, ...] | None:
    """第1段: いずれかのキーの全属性が完全一致 (正規化後) すれば一致とする。"""
    for key in entity.exact_keys:
        if all(not _is_blank(a.get(k)) and not _is_blank(b.get(k)) and _normalized(a[k]) == _normalized(b[k]) for k in key):
            return key
    return None


def compare(entity: Entity, a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[float, dict[str, dict[str, Any]]]:
    """第2段: 属性ごとの類似度の重み付き合計。両方に値がある属性だけで評価する。

    返すのは比較結果だけで、値は含めない (裁定キューに保持できる形)。
    """
    total = weighted = 0.0
    results: dict[str, dict[str, Any]] = {}
    for name, weight in entity.weights.items():
        if _is_blank(a.get(name)) or _is_blank(b.get(name)):
            results[name] = {"result": "missing"}
            continue
        ratio = similarity(a[name], b[name])
        weighted += weight * ratio
        total += weight
        if ratio == 1.0:
            results[name] = {"result": "match"}
        elif ratio >= entity.similar_floor:
            results[name] = {"result": "similar", "similarity": round(ratio, 4)}
        else:
            results[name] = {"result": "mismatch"}
    return (round(weighted / total, 6) if total else 0.0), results


def subject_refs_of(entity: Entity, attributes: Mapping[str, Any]) -> list[tuple[str, str]]:
    """人を表す属性 (identifier_type あり) から Phase 8 の subject_ref を導く。"""
    refs = []
    for name, attr in entity.attributes.items():
        value = attributes.get(name)
        if attr.identifier_type and not _is_blank(value):
            refs.append((attr.identifier_type, to_subject_ref(attr.identifier_type, str(value))))
    return refs


def add_subject_links(db: Session, golden_id: str, refs: Iterable[Sequence[str]]) -> None:
    for identifier_type, ref in refs:
        if db.get(MdmSubjectLink, (golden_id, ref)) is None:
            db.add(MdmSubjectLink(golden_id=golden_id, subject_ref=ref, identifier_type=identifier_type))
    db.flush()


def replace_subject_links(db: Session, golden_id: str, refs: Iterable[Sequence[str]]) -> None:
    for link in db.scalars(select(MdmSubjectLink).where(MdmSubjectLink.golden_id == golden_id)):
        db.delete(link)
    db.flush()
    add_subject_links(db, golden_id, refs)


# ---------------------------------------------------------------- Golden Record / xref / 版
def create_golden(db: Session, entity: Entity, asset: DataAsset, actor: str) -> GoldenRecord:
    golden = GoldenRecord(entity_type=entity.entity_type, asset_id=asset.id, status=ACTIVE, created_by=actor,
                          recompose_required=True, current_version=0, hash_salt=secrets.token_hex(16))
    db.add(golden)
    db.flush()
    return golden


def get_active_golden(db: Session, entity: Entity, golden_id: str) -> GoldenRecord | None:
    golden = db.get(GoldenRecord, golden_id)
    if golden is None or golden.entity_type != entity.entity_type:
        return None
    return golden


def find_xref(db: Session, entity_type: str, source_ref: str) -> MdmXref | None:
    return db.scalar(select(MdmXref).where(MdmXref.entity_type == entity_type, MdmXref.source_ref == source_ref))


def active_source_refs(db: Session, golden_id: str) -> list[str]:
    return sorted(db.scalars(select(MdmXref.source_ref).where(MdmXref.golden_id == golden_id,
                                                              MdmXref.tombstoned_at.is_(None))))


def link_source(db: Session, entity: Entity, golden: GoldenRecord, source_ref: str, source_system: str,
                link_type: str, actor: str) -> MdmXref:
    """xref を作る。(entity_type, source_ref) の一意制約で、1 つのソースが 2 つの Golden Record に属さない。"""
    xref = MdmXref(entity_type=entity.entity_type, source_ref=source_ref, source_system=source_system,
                   golden_id=golden.id, link_type=link_type, linked_by=actor)
    db.add(xref)
    db.flush()
    golden.recompose_required = True
    return xref


def current_version(db: Session, golden: GoldenRecord) -> GoldenVersion | None:
    if not golden.current_version:
        return None
    return db.scalar(select(GoldenVersion).where(GoldenVersion.golden_id == golden.id,
                                                 GoldenVersion.version == golden.current_version))


def append_version(db: Session, golden: GoldenRecord, reason: str, actor: str, *, content_hash: str | None = None,
                   provenance: dict[str, Any] | None = None) -> GoldenVersion:
    """版を追加する。過去の版は valid_to を閉じるだけで、削除も更新もしない。"""
    now = utcnow()
    previous = current_version(db, golden)
    if previous is not None:
        previous.valid_to = now
    version = GoldenVersion(golden_id=golden.id, version=golden.current_version + 1, valid_from=now,
                            content_hash=content_hash, source_refs=active_source_refs(db, golden.id),
                            provenance=provenance or {}, reason=reason, created_by=actor)
    db.add(version)
    golden.current_version = version.version
    db.flush()
    return version


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {k: _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value


def content_hash(golden: GoldenRecord, attributes: Mapping[str, Any]) -> str:
    """正規化した JSON の SHA-256。

    Golden Record ごとのソルトを含める。値の種類が少ない PII は素の SHA-256 だと辞書攻撃で逆算できるため。
    消去時にソルトだけを破棄すれば、ハッシュを証跡として残しても値を検証できなくなる。
    """
    body = json.dumps({"salt": golden.hash_salt, "attributes": _nfc(dict(attributes))}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- Survivorship
def _priority(entity: Entity, record: SourceRecord) -> int:
    return entity.sources[record.source_system]


def _recency(record: SourceRecord) -> datetime:
    return record.updated_at or datetime.min


def _choose(entity: Entity, rule: str, name: str, records: Sequence[SourceRecord]) -> SourceRecord | None:
    """属性の勝者を決める。同点はソースの優先度、最新性、source_ref の順で決定的に解決する。"""
    present = [r for r in records if not _is_blank(r.attributes.get(name))]
    if not present:
        return None
    tie = lambda r: (_priority(entity, r), _recency(r), r.source_ref)  # noqa: E731
    if rule == "source_priority":
        return max(present, key=tie)
    if rule == "most_recent":
        return max(present, key=lambda r: (_recency(r), _priority(entity, r), r.source_ref))
    if rule == "most_complete":
        return max(present, key=lambda r: (len(_normalized(r.attributes[name])), *tie(r)))
    counts = Counter(_normalized(r.attributes[name]) for r in present)  # most_frequent
    return max(present, key=lambda r: (counts[_normalized(r.attributes[name])], *tie(r)))


@dataclass(frozen=True)
class Composition:
    attributes: dict[str, Any]
    provenance: dict[str, dict[str, Any]]


def survive(entity: Entity, records: Sequence[SourceRecord], overrides: Mapping[str, str] | None = None) -> Composition:
    """Survivorship で属性ごとの値を決める。由来には勝ったソースと適用したルールだけを記録する。"""
    overrides = overrides or {}
    unknown = [a for a in overrides if a not in entity.attributes]
    if unknown:
        raise MdmInputError(f"overrides に未定義の属性が {len(unknown)} 件あります")
    attributes: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for name in entity.attributes:
        rule = entity.survivorship[name]
        if name in overrides:
            rule = "steward_override"
            candidates = [r for r in records if r.source_system == overrides[name] and not _is_blank(r.attributes.get(name))]
            if not candidates:
                raise MdmInputError(f"overrides.{name}: 指定した source_system に値がありません")
            winner: SourceRecord | None = max(candidates, key=lambda r: (_recency(r), r.source_ref))
        else:
            winner = _choose(entity, rule, name, records)
        attributes[name] = winner.attributes[name] if winner else None
        provenance[name] = {"source_ref": winner.source_ref if winner else None,
                            "source_system": winner.source_system if winner else None, "rule": rule}
    return Composition(attributes, provenance)


def compose(db: Session, entity: Entity, golden: GoldenRecord, records: Sequence[SourceRecord], actor: str,
            overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
    """構成ソースの値から Golden Record を生成する。永続化するのは由来・内容ハッシュ・版だけ。"""
    if golden.status == TOMBSTONED:
        raise MdmStateError("消去済みの Golden Record は再構成できません")
    expected = set(active_source_refs(db, golden.id))
    given = {r.source_ref for r in records}
    if len(given) != len(records):
        raise MdmInputError("同じソースのレコードが重複しています")
    if given - expected:
        raise MdmInputError(f"この Golden Record に紐付いていないソースが {len(given - expected)} 件あります")
    if expected - given:
        raise MdmInputError(f"構成ソースが {len(expected - given)} 件不足しています")
    result = survive(entity, records, overrides)
    digest = content_hash(golden, result.attributes)
    current = current_version(db, golden)
    unchanged = (current is not None and not golden.recompose_required and not overrides
                 and current.content_hash == digest and current.source_refs == sorted(expected))
    if unchanged:
        version = current
    else:
        version = append_version(db, golden, "steward_override" if overrides else "compose", actor,
                                 content_hash=digest, provenance=result.provenance)
    refs: list[Sequence[str]] = []
    for r in records:
        refs.extend(subject_refs_of(entity, r.attributes))
    replace_subject_links(db, golden.id, sorted(set(map(tuple, refs))))
    golden.recompose_required = False
    db.flush()
    return {"golden_id": golden.id, "entity_type": entity.entity_type, "version": version.version,
            "attributes": result.attributes, "provenance": result.provenance, "content_hash": digest,
            "reason": version.reason, "changed": not unchanged}


# ---------------------------------------------------------------- マッチング
def match(db: Session, entity: Entity, asset: DataAsset, record: SourceRecord,
          candidates: Sequence[tuple[str, Mapping[str, Any]]], actor: str) -> dict[str, Any]:
    """ソースレコードの行き先を判定する。

    既存 Golden Record の値は Control Plane に無いので、候補は Data Plane が candidates として渡す
    (候補の絞り込みは MDM Hub の責務)。候補の値も評価後に破棄する。
    """
    existing = find_xref(db, entity.entity_type, record.source_ref)
    if existing is not None:
        if existing.tombstoned_at is not None:
            raise ErasedSourceError("このソースは消去済みです。再度取り込む場合はデータ主体の権利を確認してください")
        golden = db.get(GoldenRecord, existing.golden_id)
        return _result("linked", record, golden, stage="xref", score=None)

    for golden_id, attributes in candidates:
        validate_attributes(entity, attributes)
        golden = get_active_golden(db, entity, golden_id)
        if golden is None or golden.status != ACTIVE:
            raise MdmInputError("candidates に有効でない Golden Record が含まれています")

    # 第1段: 完全一致キー
    for golden_id, attributes in candidates:
        if exact_key_match(entity, record.attributes, attributes):
            return _link_existing(db, entity, golden_id, record, actor, "exact", 1.0)

    # 第2段: 類似度
    scored = [(golden_id, *compare(entity, record.attributes, attributes)) for golden_id, attributes in candidates]
    scored.sort(key=lambda s: s[1], reverse=True)
    if scored and scored[0][1] >= entity.auto_merge:
        return _link_existing(db, entity, scored[0][0], record, actor, "similarity", scored[0][1])
    band = [s for s in scored if s[1] >= entity.review]
    if band:
        return {"decision": "review", "golden_id": None, "source_ref": record.source_ref, "stage": "similarity",
                "score": band[0][1], "recompose_required": False, "review_ids": [],
                "candidates": [{"golden_id": g, "score": score, "comparisons": results} for g, score, results in band]}

    golden = create_golden(db, entity, asset, actor)
    link_source(db, entity, golden, record.source_ref, record.source_system, "new", actor)
    add_subject_links(db, golden.id, subject_refs_of(entity, record.attributes))
    return _result("new", record, golden, stage="none", score=scored[0][1] if scored else None)


def _link_existing(db: Session, entity: Entity, golden_id: str, record: SourceRecord, actor: str, stage: str,
                   score: float) -> dict[str, Any]:
    golden = db.get(GoldenRecord, golden_id)
    link_source(db, entity, golden, record.source_ref, record.source_system, "auto_merge", actor)
    add_subject_links(db, golden.id, subject_refs_of(entity, record.attributes))
    if golden.current_version:
        append_version(db, golden, "merge", actor)  # 値は Data Plane での再構成で確定する
    return _result("linked", record, golden, stage=stage, score=score)


def _result(decision: str, record: SourceRecord, golden: GoldenRecord, *, stage: str, score: float | None) -> dict[str, Any]:
    return {"decision": decision, "golden_id": golden.id, "source_ref": record.source_ref, "stage": stage, "score": score,
            "recompose_required": golden.recompose_required, "review_ids": [], "candidates": []}
