"""§20 Level 5 自律統治: 操作のカタログとプレイブックの定義 (Phase 19。docs/adr/0001).

操作はコードに実装したカタログからだけ選べる。プレイブック (policies/remediation/playbooks.yaml) は
「どの契機で、どの条件のとき、どの操作を、どのモードで行うか」だけを宣言し、Policy Bundle の承認を経て有効になる。

ロード時の検証:
- カタログに無い操作、区分 C の操作は宣言できない
- 宣言した区分がカタログの区分と違えば拒否する (区分 A の操作を B と偽る、またはその逆)
- 契機が渡す対象の種類と、操作が受け付ける対象の種類が一致すること
- 取り消せる操作には取り消せる日数と、誤って実行した率の閾値が必要
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

PLAYBOOK_FILE = "remediation/playbooks.yaml"
A, B = "A", "B"
DRY_RUN, ACTIVE = "dry_run", "active"
MODES = (DRY_RUN, ACTIVE)
MAX_ACTIONS_LIMIT = 1000
MAX_UNDO_DAYS = 365
_ID = re.compile(r"^[A-Z][A-Z0-9-]{1,39}$")
_PLAYBOOK_KEYS = {"id", "description", "trigger", "condition", "action", "category", "mode", "max_actions_per_run", "undo_days",
                  "false_positive_threshold"}

ANOMALY_KINDS = ("outlier", "trend", "volume_drop", "freshness")
FINDING_KINDS = ("always_pass", "never_applied", "exception_heavy", "long_failing")


class PlaybookInvalid(ValueError):
    """プレイブックの宣言の不備 (ロード時のエラー)。"""


# ---------------------------------------------------------------- 対象の種類と条件
def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise PlaybookInvalid(f"{where} は true / false にしてください")
    return value


def _int(low: int, high: int):
    def check(value: Any, where: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise PlaybookInvalid(f"{where} は {low} 以上 {high} 以下の整数にしてください")
        return value
    return check


def _ratio(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise PlaybookInvalid(f"{where} は 0 以上 1 以下の数値にしてください")
    return float(value)


def _subset(allowed: tuple[str, ...]):
    def check(value: Any, where: str) -> list[str]:
        if not isinstance(value, list) or not value or any(v not in allowed for v in value):
            raise PlaybookInvalid(f"{where} は {list(allowed)} から 1 つ以上を選んでください")
        return list(dict.fromkeys(value))
    return check


@dataclass(frozen=True)
class TargetKind:
    name: str
    description: str
    conditions: Mapping[str, Any] = field(default_factory=dict)  # 条件の名前 → 検証の関数
    defaults: Mapping[str, Any] = field(default_factory=dict)


TARGET_KINDS: dict[str, TargetKind] = {k.name: k for k in (
    TargetKind("anomaly", "品質の異常 (§5 Continuous Data Quality)",
               {"kinds": _subset(ANOMALY_KINDS), "certified_only": _bool}, {"kinds": list(ANOMALY_KINDS), "certified_only": False}),
    TargetKind("recovered_issue", "条件が解消した品質の Issue (ルールが N 回続けて閾値を上回った)",
               {"consecutive_passes": _int(1, 50)}, {"consecutive_passes": 3}),
    TargetKind("stale_grant", "期限切れ、または対象の資産が削除された閲覧の承認",
               {"expired": _bool, "asset_gone": _bool}, {"expired": True, "asset_gone": True}),
    TargetKind("unowned_asset", "Owner の無い資産 (ドメインに Owner がいる)", {}, {}),
    TargetKind("classification_suggestion", "メタデータからの分類の提案 (§12 / §3)",
               {"min_confidence": _ratio}, {"min_confidence": 0.5}),
    TargetKind("policy_finding", "見直しの候補の Policy (統治の自己改善)",
               {"findings": _subset(FINDING_KINDS), "always_pass_days": _int(7, 3650), "exception_threshold": _int(1, 1000),
                "failing_days": _int(1, 3650)},
               {"findings": list(FINDING_KINDS), "always_pass_days": 180, "exception_threshold": 5, "failing_days": 90}),
)}

# 検出のジョブ (契機) → 対象の種類
DETECTORS: dict[str, str] = {
    "quality_anomaly": "anomaly",
    "quality_recovered": "recovered_issue",
    "stale_access": "stale_grant",
    "unowned_assets": "unowned_asset",
    "classification_scan": "classification_suggestion",
    "policy_review_scan": "policy_finding",
}
# イベントの種類 (契機) → (対象の種類, イベントの data の対象の id の項目)。Outbox のイベントのうち、対象を特定できるもの
EVENTS: dict[str, tuple[str, str]] = {
    "quality.anomaly_detected": ("anomaly", "anomaly_id"),
    "classification.suggested": ("classification_suggestion", "suggestion_id"),
}


# ---------------------------------------------------------------- 操作のカタログ
@dataclass(frozen=True)
class Operation:
    id: str
    category: str
    accepts: str
    undoable: bool
    description: str
    # 取り消すと権限や信頼が上がる (閲覧の承認や Certification を元に戻す)。承認行為と同じく、UI の経路ではステップアップを求める
    undo_raises_trust: bool = False


CATALOG: dict[str, Operation] = {o.id: o for o in (
    # 区分 A: 権限や信頼を下げる方向で、元に戻せて、業務の判断を含まない操作 (Issue の起票以外は取り消せる)
    Operation("issue.open", A, "anomaly", False, "品質の異常の Issue を起票する"),
    Operation("certification.revert_to_draft", A, "anomaly", True, "品質の異常を検知した Certified の資産を draft に戻す", undo_raises_trust=True),
    Operation("issue.close_recovered", A, "recovered_issue", True, "条件が解消した品質の Issue を閉じる"),
    Operation("access.revoke_stale", A, "stale_grant", True, "期限切れ、または対象の資産が無くなった閲覧の承認を取り消す",
              undo_raises_trust=True),
    # 区分 B: 提案だけを作り、人が承認する
    Operation("propose.classification", B, "classification_suggestion", False,
              "分類の変更の提案を承認エンジンに作る (確定は対象資産の Owner / Steward)"),
    Operation("propose.owner_assignment", B, "unowned_asset", False, "Owner の割当の提案を承認エンジンに作る (確定は Committee)"),
    Operation("propose.policy_review", B, "policy_finding", False, "Policy の見直しの Issue を Committee に起票する (Policy は変えない)"),
)}

# 区分 C: カタログに存在させない。宣言されたら、区分 C であることを示してロード時に拒否する
FORBIDDEN: dict[str, str] = {
    "asset.delete": "資産の削除",
    "asset.legal_hold_release": "Legal Hold の解除",
    "access.grant": "閲覧の承認の付与",
    "approval.decide": "承認の判断",
    "policy.publish": "Policy の公開",
    "policy.change": "Policy の変更",
    "certification.certify": "Certification の付与",
    "classification.apply": "分類の変更の確定",
    "classification.lower": "分類を下げる変更",
    "ownership.change": "Owner の変更の確定",
    "data.write": "データ本体への操作",
    "data.delete": "データ本体の削除",
}


# ---------------------------------------------------------------- プレイブック
@dataclass(frozen=True)
class Playbook:
    id: str
    description: str
    trigger_type: str  # detector / event
    trigger: str
    condition: Mapping[str, Any]
    action: str
    category: str
    mode: str
    max_actions_per_run: int
    undo_days: int
    false_positive_threshold: float | None

    @property
    def operation(self) -> Operation:
        return CATALOG[self.action]

    @property
    def target_kind(self) -> str:
        return DETECTORS[self.trigger] if self.trigger_type == "detector" else EVENTS[self.trigger][0]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "trigger": {self.trigger_type: self.trigger},
                "condition": dict(self.condition), "action": self.action, "category": self.category, "mode": self.mode,
                "max_actions_per_run": self.max_actions_per_run, "undo_days": self.undo_days,
                "false_positive_threshold": self.false_positive_threshold, "undoable": self.operation.undoable,
                "target_kind": self.target_kind}


@dataclass(frozen=True)
class Playbooks:
    items: tuple[Playbook, ...] = ()
    digest: str | None = None

    def get(self, playbook_id: str) -> Playbook | None:
        return next((p for p in self.items if p.id == playbook_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "playbooks": [p.as_dict() for p in self.items]}


def _trigger(raw: Any, where: str) -> tuple[str, str]:
    if not isinstance(raw, Mapping) or len(raw) != 1 or next(iter(raw)) not in ("detector", "event"):
        raise PlaybookInvalid(f"{where}.trigger は detector か event のどちらか 1 つを持つオブジェクトにしてください")
    kind, name = next(iter(raw.items()))
    known = DETECTORS if kind == "detector" else EVENTS
    if name not in known:
        raise PlaybookInvalid(f"{where}.trigger.{kind}: 存在しない契機です ({name})。{sorted(known)} から選んでください")
    return kind, name


def _condition(raw: Any, kind: TargetKind, where: str) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise PlaybookInvalid(f"{where}.condition はオブジェクトにしてください")
    unknown = set(raw) - set(kind.conditions)
    if unknown:
        raise PlaybookInvalid(f"{where}.condition: 対象 {kind.name} では使えない条件です ({sorted(unknown)})")
    return {**kind.defaults, **{k: kind.conditions[k](v, f"{where}.condition.{k}") for k, v in raw.items()}}


def _parse_one(raw: Any, where: str) -> Playbook:
    if not isinstance(raw, Mapping):
        raise PlaybookInvalid(f"{where}: オブジェクトにしてください")
    unknown = set(raw) - _PLAYBOOK_KEYS
    if unknown:
        raise PlaybookInvalid(f"{where}: 未知の項目です ({sorted(unknown)})")
    pid = raw.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise PlaybookInvalid(f"{where}.id は英大文字で始まる英大文字・数字・- の 2〜40 文字にしてください")
    where = f"{PLAYBOOK_FILE}: {pid}"
    action = raw.get("action")
    if action in FORBIDDEN:
        raise PlaybookInvalid(f"{where}.action: {action} ({FORBIDDEN[action]}) は区分 C の操作で、自動化できません (docs/adr/0001)")
    if action not in CATALOG:
        raise PlaybookInvalid(f"{where}.action: カタログに無い操作です ({action})。{sorted(CATALOG)} から選んでください")
    op = CATALOG[action]
    category = raw.get("category")
    if category == "C":
        raise PlaybookInvalid(f"{where}.category: 区分 C は宣言できません (docs/adr/0001)")
    if category != op.category:
        raise PlaybookInvalid(f"{where}.category: {action} の区分は {op.category} です ({category} と宣言されています。区分を偽ることはできません)")
    trigger_type, trigger = _trigger(raw.get("trigger"), where)
    kind_name = DETECTORS[trigger] if trigger_type == "detector" else EVENTS[trigger][0]
    if kind_name != op.accepts:
        raise PlaybookInvalid(f"{where}: 契機 {trigger} の対象 ({kind_name}) を、操作 {action} は受け付けません ({op.accepts})")
    mode = raw.get("mode")
    if mode not in MODES:
        raise PlaybookInvalid(f"{where}.mode は {list(MODES)} のいずれかにしてください")
    limit = _int(1, MAX_ACTIONS_LIMIT)(raw.get("max_actions_per_run"), f"{where}.max_actions_per_run")
    undo_days, threshold = raw.get("undo_days"), raw.get("false_positive_threshold")
    if op.undoable:
        undo_days = _int(1, MAX_UNDO_DAYS)(undo_days, f"{where}.undo_days")
        if threshold is None:
            raise PlaybookInvalid(f"{where}.false_positive_threshold: 取り消せる操作には、誤って実行した率の閾値が必要です")
        threshold = _ratio(threshold, f"{where}.false_positive_threshold")
        if threshold == 0:
            raise PlaybookInvalid(f"{where}.false_positive_threshold は 0 より大きくしてください")
    else:
        if undo_days not in (None, 0):
            raise PlaybookInvalid(f"{where}.undo_days: {action} は取り消せない操作です (undo_days は書きません)")
        undo_days = 0
        threshold = None if threshold is None else _ratio(threshold, f"{where}.false_positive_threshold")
    description = raw.get("description") or ""
    if not isinstance(description, str) or len(description) > 500:
        raise PlaybookInvalid(f"{where}.description は 500 文字までの文字列にしてください")
    return Playbook(pid, description, trigger_type, trigger, _condition(raw.get("condition"), TARGET_KINDS[kind_name], where), action,
                    category, mode, limit, undo_days, threshold)


def parse(text: str) -> Playbooks:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise PlaybookInvalid(f"{PLAYBOOK_FILE}: YAML を解釈できません") from e
    if not isinstance(doc, Mapping) or set(doc) - {"playbooks"} or not isinstance(doc.get("playbooks", []), list):
        raise PlaybookInvalid(f"{PLAYBOOK_FILE}: playbooks (配列) だけを持つオブジェクトにしてください")
    items: list[Playbook] = []
    for i, raw in enumerate(doc.get("playbooks", [])):
        playbook = _parse_one(raw, f"{PLAYBOOK_FILE}: playbooks[{i}]")
        if any(p.id == playbook.id for p in items):
            raise PlaybookInvalid(f"{PLAYBOOK_FILE}: プレイブックの id が重複しています ({playbook.id})")
        items.append(playbook)
    return Playbooks(tuple(items), hashlib.sha256(text.encode()).hexdigest())


def active(db: Any) -> Playbooks:
    """active の Policy Bundle のプレイブック。承認されて公開されるまで、ディスクの変更は反映しない (無ければ空)。"""
    from edgf.services import policy_bundles  # 循環 import 回避

    bundle = policy_bundles.active_bundle(db)
    text = (bundle.files or {}).get(PLAYBOOK_FILE) if bundle else None
    return parse(text) if text else Playbooks()


def principal_for(playbook_id: str | None = None):
    """自動化の主体 (system:autonomy:<プレイブック>)。監査の actor はこの subject で記録する。承認の権限は持たない。"""
    from edgf.auth import AUTONOMY, AUTONOMY_SUBJECT, Principal

    subject = f"{AUTONOMY_SUBJECT}:{playbook_id}" if playbook_id else AUTONOMY_SUBJECT
    return Principal(subject=subject, roles=frozenset({AUTONOMY}), clearance="restricted", domains=frozenset({"*"}), auth_mode="system")
