"""§8 Master Data Management: エンティティ定義とルール (Policy as Code と同じ方式).

policies/mdm/*.yaml で属性、ソースの信頼度、マッチング、Survivorship を宣言する。
Golden Record の生成ロジックをメタデータとして公開する (§8.1)。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.models import DataAsset, MdmRuleApproval

SURVIVORSHIP_RULES = ("source_priority", "most_recent", "most_complete", "most_frequent")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
DEFAULT_SIMILAR_FLOOR = 0.6


class MdmRuleError(Exception):
    """エンティティ定義の不備。起動または reload を止める。"""


@dataclass(frozen=True)
class Attribute:
    name: str
    type: str = "string"
    identifier_type: str | None = None  # 人を表す属性: Phase 8 の subject_ref と対応付ける


@dataclass(frozen=True)
class Entity:
    entity_type: str
    asset: str
    attributes: dict[str, Attribute]
    sources: dict[str, int]
    exact_keys: tuple[tuple[str, ...], ...]
    weights: dict[str, float]
    auto_merge: float
    review: float
    similar_floor: float
    survivorship: dict[str, str]
    rule_hash: str
    document: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"entity_type": self.entity_type, "asset": self.asset, "rule_hash": self.rule_hash,
                "attributes": [{"name": a.name, "type": a.type, "identifier_type": a.identifier_type}
                               for a in self.attributes.values()],
                "sources": self.sources, "exact_keys": [list(k) for k in self.exact_keys],
                "similarity": {"weights": self.weights, "similar_floor": self.similar_floor,
                               "auto_merge": self.auto_merge, "review": self.review},
                "survivorship": self.survivorship}


def _number(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MdmRuleError(f"{where} は数値で指定してください")
    return float(value)


def parse_entity(doc: dict[str, Any], origin: str = "(inline)") -> Entity:
    def fail(msg: str) -> MdmRuleError:
        return MdmRuleError(f"{origin}: {msg}")

    et = doc.get("entity_type")
    if not isinstance(et, str) or not _NAME.match(et):
        raise fail("entity_type が不正です")
    asset = doc.get("asset")
    if not isinstance(asset, str) or not asset:
        raise fail("asset (対応する Catalog の master 資産) は必須です")

    attributes: dict[str, Attribute] = {}
    for a in doc.get("attributes") or []:
        name = a.get("name") if isinstance(a, dict) else None
        if not isinstance(name, str) or not _NAME.match(name):
            raise fail(f"属性名が不正です: {a}")
        if name in attributes:
            raise fail(f"属性が重複しています: {name}")
        itype = a.get("identifier_type")
        if itype is not None and (not isinstance(itype, str) or not _NAME.match(itype)):
            raise fail(f"{name}: identifier_type が不正です")
        attributes[name] = Attribute(name, a.get("type", "string"), itype)
    if not attributes:
        raise fail("attributes は必須です")

    sources = doc.get("sources") or {}
    if not isinstance(sources, dict) or not sources:
        raise fail("sources (source_system ごとの優先度) は必須です")
    for system, priority in sources.items():
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise fail(f"sources.{system} は整数で指定してください")

    match = doc.get("match") or {}
    exact_keys = []
    for key in match.get("exact_keys") or []:
        if not isinstance(key, list) or not key:
            raise fail("exact_keys の各要素は属性名のリストです")
        unknown = [k for k in key if k not in attributes]
        if unknown:
            raise fail(f"exact_keys に未定義の属性があります: {unknown}")
        exact_keys.append(tuple(key))

    sim = match.get("similarity") or {}
    weights = {}
    for name, w in (sim.get("weights") or {}).items():
        if name not in attributes:
            raise fail(f"similarity.weights に未定義の属性があります: {name}")
        weights[name] = _number(f"{origin}: weights.{name}", w)
        if weights[name] <= 0:
            raise fail(f"weights.{name} は正の数で指定してください")
    if not weights:
        raise fail("similarity.weights は必須です")
    if "auto_merge" not in sim or "review" not in sim:
        raise fail("similarity の閾値 auto_merge / review は必須です")
    auto_merge = _number(f"{origin}: auto_merge", sim["auto_merge"])
    review = _number(f"{origin}: review", sim["review"])
    if not 0 < review < auto_merge <= 1:
        raise fail(f"閾値は 0 < review < auto_merge <= 1 である必要があります (review={review}, auto_merge={auto_merge})")
    floor = _number(f"{origin}: similar_floor", sim.get("similar_floor", DEFAULT_SIMILAR_FLOOR))
    if not 0 <= floor < 1:
        raise fail("similar_floor は 0 以上 1 未満です")

    survivorship = {}
    for name, spec in (doc.get("survivorship") or {}).items():
        if name not in attributes:
            raise fail(f"survivorship に未定義の属性があります: {name}")
        kind = spec.get("rule") if isinstance(spec, dict) else None
        if kind not in SURVIVORSHIP_RULES:
            raise fail(f"survivorship.{name}: 不明なルール種別です: {kind}")
        survivorship[name] = kind
    missing = [n for n in attributes if n not in survivorship]
    if missing:
        raise fail(f"Survivorship ルールが未定義の属性があります: {missing}")

    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str)
    return Entity(entity_type=et, asset=asset, attributes=attributes, sources=dict(sources),
                  exact_keys=tuple(exact_keys), weights=weights, auto_merge=auto_merge, review=review,
                  similar_floor=floor, survivorship=survivorship, document=doc,
                  rule_hash=hashlib.sha256(canonical.encode()).hexdigest())


def load_entities(policy_dir: Path | None = None) -> dict[str, Entity]:
    base = Path(policy_dir or settings.policy_dir) / "mdm"
    entities: dict[str, Entity] = {}
    assets: dict[str, str] = {}
    for f in sorted(base.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise MdmRuleError(f"{f.name}: YAML の構文エラーです") from e
        entity = parse_entity(doc, f.name)
        if entity.entity_type in entities:
            raise MdmRuleError(f"{f.name}: entity_type が重複しています: {entity.entity_type}")
        if entity.asset in assets:
            raise MdmRuleError(f"{f.name}: 資産 {entity.asset} は {assets[entity.asset]} に対応付け済みです")
        entities[entity.entity_type] = entity
        assets[entity.asset] = entity.entity_type
    return entities


_cache: dict[str, Entity] | None = None


def get_entities(reload: bool = False) -> dict[str, Entity]:
    global _cache
    if _cache is None or reload:
        _cache = load_entities()
    return _cache


def check_entity_defined(db: Session, asset: DataAsset) -> tuple[bool, str]:
    """MDM-001。定義の読み込み時に全属性の Survivorship を検証しているので、定義があればルールも揃っている。"""
    entity = next((e for e in get_entities().values() if e.asset == asset.qualified_name), None)
    if entity is None:
        return False, "エンティティ定義 (policies/mdm) がありません (§8)"
    if approval_of(db, entity) is None:
        return False, f"エンティティ {entity.entity_type} の生成規則を Owner が承認していません (§8.2)"
    return True, "ok"


def find_asset(db: Session, entity: Entity) -> DataAsset | None:
    """対応する master 資産。未登録、または data_class が master でなければ None。"""
    asset = db.scalar(select(DataAsset).where(DataAsset.qualified_name == entity.asset))
    return asset if asset is not None and asset.data_class == "master" else None


def approval_of(db: Session, entity: Entity) -> MdmRuleApproval | None:
    """現在のルール本文に対する Owner の承認 (ルールを変更すると承認は無効になる)。"""
    return db.scalar(select(MdmRuleApproval).where(MdmRuleApproval.entity_type == entity.entity_type,
                                                   MdmRuleApproval.rule_hash == entity.rule_hash))
