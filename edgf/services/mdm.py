"""§8 Golden Record: マッチングと生成 (ステートレス).

品質エンジンと同じく、受け取ったソースレコードの値は評価に使うだけで保持しない (§18.2)。
生成した Golden Record の値はレスポンスで返し、Data Plane (MDM Hub) が保存する。
Control Plane に残すのは、相互参照 (xref)、属性ごとの由来、版の情報 (内容ハッシュを含む) だけ。
値を DB・ログ・監査に渡してはならないので、例外メッセージにも値を含めない。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.enums import ESCALATION_ORDER
from edgf.models import DataAsset, GoldenRecord, GoldenVersion, Issue, MdmReview, MdmSubjectLink, MdmXref, utcnow
from edgf.services import issues
from edgf.services.mdm_rules import Entity
from edgf.services.subject_ref import to_subject_ref

ACTIVE, TOMBSTONED = "active", "tombstoned"
PENDING, MERGED, NOT_MATCH, SPLIT, SUPERSEDED = "pending", "merged", "not_match", "split", "superseded"


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

    # not_match と裁定されたペアは比較しない (再起票しない)
    rejected = set(db.scalars(select(MdmReview.golden_id).where(MdmReview.entity_type == entity.entity_type,
                                                                MdmReview.source_ref == record.source_ref,
                                                                MdmReview.status == NOT_MATCH)))
    candidates = [c for c in candidates if c[0] not in rejected]

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
    refs = subject_refs_of(entity, record.attributes)
    for golden_id, score, results in band:
        _enqueue(db, entity, asset, record, golden_id, score, results, refs)
    pending = _pending_reviews(db, entity.entity_type, record.source_ref)
    if pending:  # 裁定が終わるまで新規の Golden Record は作らない
        return {"decision": "review", "golden_id": None, "source_ref": record.source_ref, "stage": "similarity",
                "score": max(p.score for p in pending), "recompose_required": False,
                "review_ids": [p.id for p in pending],
                "candidates": [{"golden_id": p.golden_id, "score": p.score, "comparisons": p.comparisons} for p in pending]}

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
    _supersede(db, entity.entity_type, record.source_ref, actor)
    return _result("linked", record, golden, stage=stage, score=score)


def _result(decision: str, record: SourceRecord, golden: GoldenRecord, *, stage: str, score: float | None) -> dict[str, Any]:
    return {"decision": decision, "golden_id": golden.id, "source_ref": record.source_ref, "stage": stage, "score": score,
            "recompose_required": golden.recompose_required, "review_ids": [], "candidates": []}


# ---------------------------------------------------------------- 裁定キュー
def _pending_reviews(db: Session, entity_type: str, source_ref: str) -> list[MdmReview]:
    return list(db.scalars(select(MdmReview).where(MdmReview.entity_type == entity_type, MdmReview.source_ref == source_ref,
                                                   MdmReview.status == PENDING).order_by(MdmReview.score.desc(), MdmReview.id)))


def _enqueue(db: Session, entity: Entity, asset: DataAsset, record: SourceRecord, golden_id: str, score: float,
             comparisons: dict[str, Any], subject_refs: list[tuple[str, str]]) -> MdmReview:
    existing = db.scalar(select(MdmReview).where(MdmReview.entity_type == entity.entity_type,
                                                 MdmReview.source_ref == record.source_ref, MdmReview.golden_id == golden_id))
    if existing is not None:
        return existing
    review = MdmReview(entity_type=entity.entity_type, asset_id=asset.id, source_ref=record.source_ref,
                       source_system=record.source_system, golden_id=golden_id, score=score, comparisons=comparisons,
                       subject_refs=[list(r) for r in subject_refs], status=PENDING,
                       due_at=utcnow() + timedelta(days=settings.mdm_review_sla_days))
    db.add(review)
    db.flush()
    return review


def _supersede(db: Session, entity_type: str, source_ref: str, actor: str) -> None:
    for other in _pending_reviews(db, entity_type, source_ref):
        other.status, other.decided_by, other.decided_at = SUPERSEDED, actor, utcnow()
    db.flush()


def _close(review: MdmReview, status: str, actor: str, reason: str) -> None:
    review.status, review.decided_by, review.decided_at, review.reason = status, actor, utcnow(), reason


def decide_merge(db: Session, entity: Entity, review: MdmReview, actor: str, reason: str) -> None:
    if review.status != PENDING:
        raise MdmStateError(f"裁定の状態が {review.status} のため merge できません")
    golden = db.get(GoldenRecord, review.golden_id)
    if golden.status != ACTIVE:
        raise MdmStateError("消去済みの Golden Record にはマージできません")
    link_source(db, entity, golden, review.source_ref, review.source_system, "steward_merge", actor)
    add_subject_links(db, golden.id, review.subject_refs)
    if golden.current_version:
        append_version(db, golden, "merge", actor)
    _close(review, MERGED, actor, reason)
    db.flush()  # autoflush なし: 次のクエリがこの裁定を pending として拾わないようにする
    _supersede(db, entity.entity_type, review.source_ref, actor)


def decide_not_match(db: Session, entity: Entity, asset: DataAsset, review: MdmReview, actor: str, reason: str) -> None:
    """同じペアは以後起票しない。他に裁定待ちが無ければ、このソースを新しい Golden Record にする。"""
    if review.status != PENDING:
        raise MdmStateError(f"裁定の状態が {review.status} のため not_match にできません")
    _close(review, NOT_MATCH, actor, reason)
    db.flush()
    if not _pending_reviews(db, entity.entity_type, review.source_ref):
        golden = create_golden(db, entity, asset, actor)
        link_source(db, entity, golden, review.source_ref, review.source_system, "new", actor)
        add_subject_links(db, golden.id, review.subject_refs)
        review.new_golden_id = golden.id
    db.flush()


def split_sources(db: Session, entity: Entity, asset: DataAsset, golden: GoldenRecord, source_refs: Sequence[str],
                  actor: str) -> GoldenRecord:
    """誤マージの分割。xref を新しい Golden Record へ付け替え、双方に再構成を求める。

    過去の版は書き換えないので、as-of で分割前の構成を照会できる。
    """
    if golden.status != ACTIVE:
        raise MdmStateError("消去済みの Golden Record は分割できません")
    current = set(active_source_refs(db, golden.id))
    moving = set(source_refs)
    if not moving or moving - current:
        raise MdmInputError("この Golden Record に紐付いていないソースが指定されています")
    if moving == current:
        raise MdmInputError("すべてのソースを分割することはできません")
    new = create_golden(db, entity, asset, actor)
    now = utcnow()
    for xref in db.scalars(select(MdmXref).where(MdmXref.golden_id == golden.id, MdmXref.source_ref.in_(moving))):
        xref.golden_id, xref.link_type, xref.linked_by, xref.linked_at = new.id, "split", actor, now
    db.flush()
    # どのソースの識別子かは値が無いと判定できない。再構成までは両方に残し、消去の対象から漏らさない
    links = db.scalars(select(MdmSubjectLink).where(MdmSubjectLink.golden_id == golden.id)).all()
    add_subject_links(db, new.id, [(link.identifier_type, link.subject_ref) for link in links])
    if golden.current_version:
        append_version(db, golden, "split", actor)
    append_version(db, new, "split", actor)
    golden.recompose_required = new.recompose_required = True
    db.flush()
    return new


def decide_split(db: Session, entity: Entity, asset: DataAsset, review: MdmReview, actor: str, reason: str) -> GoldenRecord:
    if review.status != MERGED:
        raise MdmStateError("split できるのは merge 済みの裁定だけです")
    new = split_sources(db, entity, asset, db.get(GoldenRecord, review.golden_id), [review.source_ref], actor)
    _close(review, SPLIT, actor, reason)
    review.new_golden_id = new.id
    db.flush()
    return new


def escalate_overdue_reviews(db: Session, actor: str) -> dict[str, Any]:
    """期限超過の裁定を既存の Issue の仕組みで起票する。Issue の期限も過ぎていればエスカレーションする。"""
    now = utcnow()
    overdue = db.scalars(select(MdmReview).where(MdmReview.status == PENDING, MdmReview.due_at < now)).all()
    opened, escalated = [], []
    for review in overdue:
        ref = f"mdm-review:{review.id}"
        asset = db.get(DataAsset, review.asset_id)
        issue = issues.open_issue(db, actor, asset=asset, kind="mdm", severity="medium", source_ref=ref,
                                  title=f"[MDM] {asset.qualified_name}: 裁定 {review.id} が期限を超過しています",
                                  description="重複候補の裁定を行ってください (§8 Deduplication)。")
        if issue:
            opened.append(issue.id)
        stale = db.scalars(select(Issue).where(Issue.source_ref == ref, Issue.status.in_(issues.OPEN_STATES), Issue.due_at < now,
                                               Issue.escalation_level != ESCALATION_ORDER[-1].value)).all()
        for item in stale:
            issues.escalate(db, actor, item, "mdm review overdue")
            escalated.append(item.id)
    db.flush()
    return {"overdue_reviews": [r.id for r in overdue], "issues_opened": opened, "issues_escalated": escalated}


# ---------------------------------------------------------------- 版の照会 (値は含まない)
def version_view(version: GoldenVersion) -> dict[str, Any]:
    return {"version": version.version, "valid_from": version.valid_from, "valid_to": version.valid_to,
            "content_hash": version.content_hash, "source_refs": version.source_refs,
            "provenance": version.provenance, "reason": version.reason, "created_by": version.created_by,
            "tombstoned": version.tombstoned}


def golden_view(golden: GoldenRecord) -> dict[str, Any]:
    """ソルトは返さない。"""
    return {"id": golden.id, "entity_type": golden.entity_type, "asset_id": golden.asset_id, "status": golden.status,
            "recompose_required": golden.recompose_required, "current_version": golden.current_version,
            "created_at": golden.created_at, "tombstoned_at": golden.tombstoned_at}


def version_at(db: Session, golden_id: str, at: datetime) -> GoldenVersion | None:
    """その時点で有効だった版。valid_from <= at < valid_to。"""
    return db.scalar(select(GoldenVersion).where(
        GoldenVersion.golden_id == golden_id, GoldenVersion.valid_from <= at,
        (GoldenVersion.valid_to.is_(None)) | (GoldenVersion.valid_to > at)).order_by(GoldenVersion.version.desc()).limit(1))


def get_version(db: Session, golden_id: str, number: int) -> GoldenVersion | None:
    return db.scalar(select(GoldenVersion).where(GoldenVersion.golden_id == golden_id, GoldenVersion.version == number))


def verify(db: Session, golden: GoldenRecord, attributes: Mapping[str, Any], number: int | None) -> dict[str, Any]:
    """Data Plane が持つ値がその版の内容ハッシュと一致するかを検証する。値は保持しない。"""
    if golden.status == TOMBSTONED or golden.hash_salt is None:
        raise MdmStateError("消去済みの Golden Record は検証できません")
    version = get_version(db, golden.id, number) if number else current_version(db, golden)
    if version is None:
        raise MdmInputError("指定した版がありません")
    if version.content_hash is None:
        raise MdmStateError("この版は再構成待ちのため内容ハッシュがありません")
    return {"golden_id": golden.id, "version": version.version, "content_hash": version.content_hash,
            "match": hmac.compare_digest(content_hash(golden, attributes), version.content_hash)}


def lookup_xrefs(db: Session, source_ref: str) -> Sequence[MdmXref]:
    return db.scalars(select(MdmXref).where(MdmXref.source_ref == source_ref).order_by(MdmXref.entity_type)).all()
