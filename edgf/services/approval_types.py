"""§16 Approval: 承認の種別の宣言的な定義.

種別ごとに、承認のステップ (承認者のルールと定足数)、SLA、承認・却下されたときの処理を定義する。
承認者のルールは authz の Rule を再利用し、Custodian が承認者になれないことはロード時に検証する (§4)。
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from edgf import authz
from edgf.auth import COMMITTEE, OWNER
from edgf.authz import Rule, rule

# 承認者のルール。ステップは名前で参照し、案件に保存する
APPROVER_RULES: dict[str, Rule] = {
    "asset_owner": rule(relations=("owner",), approval=True, description="対象資産の Owner"),
    "committee": rule(roles=(COMMITTEE,), approval=True, description="Committee"),
    "domain_owner": rule(domain_roles=(OWNER,), approval=True, description="対象ドメインの Owner"),
}
authz.validate_permissions(APPROVER_RULES)


@dataclass(frozen=True)
class Step:
    name: str
    rule: str
    quorum: int = 1
    domain: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rule": self.rule, "quorum": self.quorum, "domain": self.domain}


@dataclass(frozen=True)
class ApprovalType:
    name: str
    description: str
    steps: tuple[Step, ...] = ()  # 案件ごとに変わる種別 (policy_change) は作成時に渡す
    sla_business_days: int = 5
    requestable: bool = False  # 利用者が POST /approvals で申請できるか
    on_approved: str | None = None  # "module:function"。循環 import を避けるため名前で参照する
    on_rejected: str | None = None
    on_cancelled: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


def _validate(definition: ApprovalType) -> None:
    for step in definition.steps:
        if step.rule not in APPROVER_RULES:
            raise ValueError(f"{definition.name}: unknown approver rule {step.rule}")
        if step.quorum < 1:
            raise ValueError(f"{definition.name}: quorum must be >= 1")


TYPES: dict[str, ApprovalType] = {}


def register(definition: ApprovalType) -> ApprovalType:
    _validate(definition)
    TYPES[definition.name] = definition
    return definition


def add_rule(name: str, approver: Rule) -> None:
    """承認者のルールを追加する。Custodian を含むルールはここで拒否される。"""
    authz.validate_permissions({name: approver})
    APPROVER_RULES[name] = approver


HANDLERS = "edgf.services.approval_handlers"
register(ApprovalType(
    "access_request", "利用者の閲覧の申請 (§4.2 アクセス承認。承認されると AccessGrant を作る)",
    steps=(Step("owner", "asset_owner"),), sla_business_days=3, requestable=True, on_approved=f"{HANDLERS}:grant_access"))
register(ApprovalType(
    "external_share", "社外提供 (§10 Share。Owner と Committee の両方の承認が必要)",
    steps=(Step("owner", "asset_owner"), Step("committee", "committee")), requestable=True, on_approved=f"{HANDLERS}:approve_share"))
register(ApprovalType(
    "retention_delete", "保持期限による削除 (§10.2)", steps=(Step("owner", "asset_owner"),),
    on_approved=f"{HANDLERS}:delete_asset"))


BUNDLES = "edgf.services.policy_bundles"
register(ApprovalType(
    "policy_change", "Policy の変更 (§16 Policy。ステップは変更の範囲で決まる)", sla_business_days=10,
    on_approved=f"{BUNDLES}:on_approved", on_rejected=f"{BUNDLES}:on_rejected", on_cancelled=f"{BUNDLES}:on_rejected"))


def resolve(path: str | None) -> Callable[..., None] | None:
    if path is None:
        return None
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def add_business_days(start: datetime, days: int) -> datetime:
    """SLA の期限 (土日を除く。祝日は考慮しない)。"""
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current
