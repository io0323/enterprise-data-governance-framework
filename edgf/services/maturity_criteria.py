"""§20 成熟度の評価の基準 (Phase 18)。policies/maturity/criteria.yaml を宣言的に読む.

基準は 2 種類:
- 自動の指標 (automatic): KPI の名前、比較、閾値、継続の日数。ドメイン別の KPI のスナップショットで判定する
- 手動の項目 (manual): 説明と必要な証拠。ドメインの Owner が自己評価し、Committee が判断する

Policy Bundle に含め、変更は Phase 12 の承認を経て有効になる。ロード時に検証する:
存在しない KPI の参照、レベルの順序 (1 から連続)、Level 4 以上の自動の指標の継続の日数。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import yaml

CRITERIA_FILE = "maturity/criteria.yaml"
MAX_LEVEL = 5
LONG_RUN_LEVEL = 4  # このレベル以上の自動の指標は継続の日数が必須
DEFAULT_LONG_RUN_DAYS = 90
OPERATORS = (">=", "<=", "measured")
# ドメイン別の KPI のスナップショットにある指標 (edgf.services.kpi.compute の出力。tests で一致を確かめる)
KPI_METRICS = frozenset({
    "ai_asset_count", "ai_card_complete_rate", "ai_inheritance_violation_count", "ai_remediation_pending_count", "ai_training_approved_rate",
    "asset_count", "catalog_registration_rate", "cde_column_lineage_rate", "cde_quality_score_avg", "certified_rate", "compliance_rate",
    "discovered_asset_count", "freshness_rate", "governance_activity_assets_30d", "mdm_auto_merge_rate", "mdm_duplicate_rate",
    "mdm_review_pending_count", "mean_time_to_resolve_days", "mesh_breaking_rejected_count", "mesh_datsis_pass_rate",
    "mesh_product_published_count", "mesh_schema_drift_issue_count", "mesh_subscription_active_count", "metadata_completeness",
    "open_issue_count", "overdue_issue_count", "owner_assignment_rate", "privacy_erasure_open_count", "privacy_erasure_overdue_count",
    "privacy_pii_asset_count", "quality_score_avg", "unregistered_asset_count", "unused_asset_count", "usage_active_assets_30d",
    "usage_query_count_30d",
})
# Level 5 (自律統治) の指標。Phase 19 で計測できるようになるまでは「計測できない」として扱い、満たさない
PLANNED_METRICS = frozenset({"auto_remediation_rate", "predictive_quality_coverage"})
_LEVEL_KEYS = {"level", "name", "automatic", "manual"}
_AUTO_KEYS = {"id", "metric", "op", "threshold", "days", "description"}
_MANUAL_KEYS = {"id", "description", "evidence"}


class CriteriaInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Automatic:
    id: str
    metric: str
    op: str
    threshold: float | None
    days: int  # 0 は評価の期間の最新のスナップショットだけで判定する
    description: str = ""

    @property
    def measurable(self) -> bool:
        return self.metric not in PLANNED_METRICS

    def satisfied(self, value: Any) -> bool:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if self.op == "measured":
            return True
        return value >= self.threshold if self.op == ">=" else value <= self.threshold


@dataclass(frozen=True)
class Manual:
    id: str
    description: str
    evidence: str


@dataclass(frozen=True)
class Level:
    level: int
    name: str
    automatic: tuple[Automatic, ...] = ()
    manual: tuple[Manual, ...] = ()


@dataclass(frozen=True)
class Criteria:
    levels: tuple[Level, ...] = ()
    digest: str = ""

    def level(self, number: int) -> Level | None:
        return next((lv for lv in self.levels if lv.level == number), None)

    def manual_ids(self, up_to: int) -> list[str]:
        return [m.id for lv in self.levels if lv.level <= up_to for m in lv.manual]

    def manual_by_id(self) -> dict[str, tuple[int, Manual]]:
        return {m.id: (lv.level, m) for lv in self.levels for m in lv.manual}

    def as_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "levels": [asdict(lv) for lv in self.levels]}


def _text(value: Any, where: str, *, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CriteriaInvalid(f"{where} は 1〜{limit} 文字の文字列にしてください")
    return value.strip()


def _keys(item: Any, allowed: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(item, Mapping):
        raise CriteriaInvalid(f"{where} はオブジェクトにしてください")
    unknown = set(item) - allowed
    if unknown:
        raise CriteriaInvalid(f"{where}: 使えないキーがあります {sorted(unknown)}")
    return item


def _automatic(item: Any, level: int, where: str) -> Automatic:
    item = _keys(item, _AUTO_KEYS, where)
    metric = _text(item.get("metric"), f"{where}.metric", limit=100)
    if metric not in KPI_METRICS | PLANNED_METRICS:
        raise CriteriaInvalid(f"{where}.metric: 存在しない KPI です ({metric})")
    op = item.get("op", ">=")
    if op not in OPERATORS:
        raise CriteriaInvalid(f"{where}.op は {OPERATORS} のいずれかです")
    threshold = item.get("threshold")
    if op == "measured":
        if threshold is not None:
            raise CriteriaInvalid(f"{where}: op が measured のときは threshold を書きません")
    elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise CriteriaInvalid(f"{where}.threshold は数値にしてください")
    days = item.get("days")
    if level >= LONG_RUN_LEVEL:
        if days is None:
            raise CriteriaInvalid(f"{where}: Level {LONG_RUN_LEVEL} 以上の自動の指標には days (継続の日数。既定は {DEFAULT_LONG_RUN_DAYS}) が必要です")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise CriteriaInvalid(f"{where}.days は 1 以上の整数にしてください")
    elif days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0):
        raise CriteriaInvalid(f"{where}.days は 0 以上の整数にしてください")
    return Automatic(_text(item.get("id"), f"{where}.id", limit=40), metric, op, None if threshold is None else float(threshold),
                     int(days or 0), str(item.get("description") or ""))


def parse(text: str) -> Criteria:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise CriteriaInvalid(f"{CRITERIA_FILE}: YAML を解釈できません") from e
    if not isinstance(doc, Mapping) or set(doc) - {"levels"} or not isinstance(doc.get("levels"), list):
        raise CriteriaInvalid(f"{CRITERIA_FILE}: levels (配列) だけを持つオブジェクトにしてください")
    levels: list[Level] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(doc["levels"]):
        where = f"{CRITERIA_FILE}: levels[{i}]"
        raw = _keys(raw, _LEVEL_KEYS, where)
        number = raw.get("level")
        if number != i + 1:
            raise CriteriaInvalid(f"{where}.level は {i + 1} にしてください (レベルは 1 から順に、飛ばさずに並べます)")
        if not isinstance(raw.get("automatic", []), list) or not isinstance(raw.get("manual", []), list):
            raise CriteriaInvalid(f"{where}: automatic と manual は配列にしてください")
        automatic = tuple(_automatic(a, number, f"{where}.automatic[{j}]") for j, a in enumerate(raw.get("automatic", [])))
        manual = tuple(Manual(_text(_keys(m, _MANUAL_KEYS, f"{where}.manual[{j}]").get("id"), f"{where}.manual[{j}].id", limit=40),
                              _text(m.get("description"), f"{where}.manual[{j}].description"),
                              _text(m.get("evidence"), f"{where}.manual[{j}].evidence"))
                       for j, m in enumerate(raw.get("manual", [])))
        if not automatic and not manual:
            raise CriteriaInvalid(f"{where}: 基準がありません")
        for cid in [a.id for a in automatic] + [m.id for m in manual]:
            if cid in seen_ids:
                raise CriteriaInvalid(f"{where}: 基準の id が重複しています ({cid})")
            seen_ids.add(cid)
        levels.append(Level(number, _text(raw.get("name"), f"{where}.name", limit=60), automatic, manual))
    if len(levels) > MAX_LEVEL:
        raise CriteriaInvalid(f"{CRITERIA_FILE}: レベルは {MAX_LEVEL} までです")
    return Criteria(tuple(levels), hashlib.sha256(text.encode()).hexdigest())


def active(db: Any) -> Criteria:
    """active の Policy Bundle の基準。承認されて公開されるまで、ディスクの変更は反映しない。"""
    from edgf.services import policy_bundles  # 循環 import 回避

    bundle = policy_bundles.active_bundle(db)
    text = (bundle.files or {}).get(CRITERIA_FILE) if bundle else None
    return parse(text) if text else Criteria()
