"""§4 Data Ownership / §11 Access Control: 認可ロジックの集約点.

Policy as Code と同じく「何を許すか」を宣言的な表 (PERMISSIONS) で定義し、判定はこのモジュールだけが行う。
各 API はアクション名を渡して `require(...)` を呼ぶだけで、ロール名をハードコードしない。

判定の 3 要素 (いずれかを満たせば許可):
- roles        : 全社ロール (RBAC)。admin / committee など
- domain_roles : 対象資産のドメインに所属している (Principal.domains) 場合のみ有効なロール (§15 Domain Ownership)
- relations    : 対象との関係。資産の owner / steward / custodian は asset.<role> == Principal.subject で判定 (§4)。
                 Issue の assignee も同様

読み取りは ABAC (`can_read` / `readable_clause`): 資産の sensitivity と Principal.clearance を比較する。
restricted 資産は Owner の承認 (AccessGrant) が無い限り、当事者・Committee・admin 以外には見せない (一覧からも除外)。

Business Ownership (§4): 承認行為 (approval=True) に Custodian を含めることはできない (ロード時に検証)。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from edgf.auth import (ADMIN, COMMITTEE, CONSUMER, CUSTODIAN, OWNER, ROLES, SENSITIVITY_LEVELS, STEWARD,
                       Principal)
from edgf.errors import ProblemError
from edgf.models import AccessGrant, DataAsset, utcnow

ASSET_RELATIONS = ("owner", "steward", "custodian")
RELATIONS = frozenset({*ASSET_RELATIONS, "assignee"})
PRIVILEGED_READERS = frozenset({ADMIN, COMMITTEE})  # 全資産のメタデータを参照できる (監督・運用責任)
RESTRICTED = "restricted"


@dataclass(frozen=True)
class Rule:
    roles: frozenset[str] = frozenset()
    domain_roles: frozenset[str] = frozenset()
    relations: frozenset[str] = frozenset()
    approval: bool = False          # 承認行為 (§4.2 RACI の A/R)。Custodian 不可
    authenticated: bool = False     # 認証済みなら誰でも可
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"roles": sorted(self.roles), "domain_roles": sorted(self.domain_roles),
                "relations": sorted(self.relations), "approval": self.approval,
                "authenticated": self.authenticated, "description": self.description}


def rule(*, roles: Iterable[str] = (), domain_roles: Iterable[str] = (), relations: Iterable[str] = (),
         approval: bool = False, authenticated: bool = False, description: str = "") -> Rule:
    return Rule(frozenset(roles), frozenset(domain_roles), frozenset(relations), approval, authenticated, description)


# ---------------------------------------------------------------- 権限表 (宣言的定義)
PERMISSIONS: Mapping[str, Rule] = {
    # Catalog / Ownership (§4, §6, §7, §10)
    "domain.create": rule(roles=(COMMITTEE, ADMIN), description="ドメインの新設"),
    "asset.register": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD), description="資産の登録"),
    "asset.update": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD), relations=("owner", "steward"),
                         description="資産メタデータの更新・ライフサイクル遷移 (削除以外)"),
    "asset.refresh_notify": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD, CUSTODIAN),
                                 relations=ASSET_RELATIONS, description="Data Plane からの更新通知"),
    "asset.ownership_change": rule(roles=(COMMITTEE, ADMIN), approval=True, description="Owner/Steward/Custodian の割当"),
    "asset.certify": rule(relations=("owner", "steward"), approval=True, description="Certification の変更"),
    "asset.delete": rule(roles=(COMMITTEE,), relations=("owner",), approval=True, description="削除指示 (§10.1 Delete)"),
    "asset.legal_hold": rule(roles=(COMMITTEE,), relations=("owner",), approval=True, description="Legal Hold の設定/解除"),
    "asset.access_grant": rule(relations=("owner",), approval=True, description="閲覧の承認 (§4.2 アクセス承認 A/R)"),
    "asset.access_revoke": rule(roles=(COMMITTEE,), relations=("owner",), description="閲覧承認の取消"),
    "asset.access_grant.read": rule(roles=(COMMITTEE, ADMIN), relations=("owner", "steward"),
                                    description="閲覧承認の一覧"),
    # Glossary (§6.2): Steward 主管
    "glossary.write": rule(roles=(ADMIN, OWNER, STEWARD), description="用語の作成"),
    "glossary.link": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD), relations=("owner", "steward"),
                          description="用語と資産の紐付け"),
    # Quality (§5, §4.2 品質基準定義: Owner A / Steward R)
    "quality.rule_write": rule(domain_roles=(OWNER, STEWARD), relations=("owner", "steward"),
                               description="品質ルールの追加・削除"),
    "quality.run": rule(domain_roles=(OWNER, STEWARD, CUSTODIAN), relations=ASSET_RELATIONS,
                        description="品質検査の実行 (パイプラインからの送信を含む)"),
    # Issue (§16)
    "issue.create": rule(authenticated=True, description="Issue の起票 (Consumer の品質報告を含む)"),
    "issue.update": rule(roles=(COMMITTEE, ADMIN), relations=("owner", "steward", "assignee"),
                         description="Issue の更新"),
    "issue.escalate": rule(roles=(COMMITTEE, ADMIN), relations=("owner", "steward", "assignee"),
                           description="Issue のエスカレーション"),
    "issue.escalate_overdue": rule(roles=(COMMITTEE, ADMIN), description="SLA 超過 Issue の一括エスカレーション"),
    # Lineage (§9): パイプラインによる自動収集
    "lineage.write": rule(roles=(ADMIN, OWNER, STEWARD, CUSTODIAN), description="リネージの登録・取込"),
    # Policy / Compliance (§13, §15)
    "policy.reload": rule(roles=(COMMITTEE, ADMIN), approval=True, description="ポリシーの再読込 (公開)"),
    "policy.evaluate_all": rule(roles=(COMMITTEE, ADMIN), description="全資産の自動監査と Issue 化"),
    "exception.request": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD), relations=("owner", "steward"),
                              description="ポリシー例外の申請"),
    "exception.decide": rule(roles=(COMMITTEE, ADMIN), approval=True, description="ポリシー例外の承認/却下"),
    # Audit (§13, §19)
    "audit.read": rule(roles=(COMMITTEE, ADMIN), description="監査ログの参照"),
}


def validate_permissions(permissions: Mapping[str, Rule]) -> None:
    """権限表の整合検証。§4 に反する定義 (Custodian への承認権限) は起動を止める。"""
    for action, r in permissions.items():
        unknown = (r.roles | r.domain_roles) - set(ROLES)
        if unknown:
            raise ValueError(f"{action}: unknown roles {sorted(unknown)}")
        if r.relations - RELATIONS:
            raise ValueError(f"{action}: unknown relations {sorted(r.relations - RELATIONS)}")
        if r.approval and (CUSTODIAN in r.roles | r.domain_roles or "custodian" in r.relations):
            raise ValueError(f"{action}: Custodian に承認権限を与えることはできません (§4 Business Ownership)")
        if r.approval and (CONSUMER in r.roles | r.domain_roles or r.authenticated):
            raise ValueError(f"{action}: 承認行為を全員に開放することはできません")


validate_permissions(PERMISSIONS)


# ---------------------------------------------------------------- 判定
def relations_of(principal: Principal, asset: DataAsset | None) -> set[str]:
    if asset is None:
        return set()
    return {rel for rel in ASSET_RELATIONS if getattr(asset, rel, None) and getattr(asset, rel) == principal.subject}


def is_allowed(principal: Principal, action: str, *, asset: DataAsset | None = None, domain: str | None = None,
               relations: Iterable[str] = ()) -> bool:
    r = PERMISSIONS[action]
    if r.authenticated or principal.roles & r.roles:
        return True
    target_domain = asset.domain if asset is not None and domain is None else domain
    if principal.roles & r.domain_roles and principal.in_domain(target_domain):
        return True
    return bool((relations_of(principal, asset) | set(relations)) & r.relations)


def deny(db: Session, principal: Principal, action: str, *, entity_type: str, entity_id: str | None,
         reason: str, required: Rule | None = None) -> NoReturn:
    """403 を返し、拒否の証跡を残す。

    リクエストのトランザクションは巻き戻すが、authz.denied の監査イベントだけは確定させる
    (拒否も統制の実行であり証跡の対象: §13 Evidence)。
    """
    from edgf.services import audit  # 循環 import 回避

    db.rollback()
    audit.record(db, principal.audit_actor, "authz.denied", entity_type, entity_id or "-", {
        "action": action, "reason": reason, "roles": sorted(principal.roles), "auth_mode": principal.auth_mode,
    })
    db.commit()
    ext: dict[str, Any] = {"action": action}
    if required is not None:
        ext["required"] = required.as_dict()
    raise ProblemError(403, reason, type_="forbidden", **ext)


def require(db: Session, principal: Principal, action: str, *, asset: DataAsset | None = None,
            domain: str | None = None, relations: Iterable[str] = (), entity_type: str = "asset",
            entity_id: str | None = None) -> None:
    if not is_allowed(principal, action, asset=asset, domain=domain, relations=relations):
        r = PERMISSIONS[action]
        deny(db, principal, action, entity_type=entity_type,
             entity_id=entity_id or (asset.id if asset is not None else None),
             reason=f"この操作 ({action}: {r.description}) を行う権限がありません", required=r)


# ---------------------------------------------------------------- ABAC (読み取り)
def _level(sensitivity: str) -> int:
    return SENSITIVITY_LEVELS.index(sensitivity) if sensitivity in SENSITIVITY_LEVELS else len(SENSITIVITY_LEVELS)


def _clearance_sensitivities(principal: Principal) -> list[str]:
    """clearance だけで読める機密度。restricted は clearance に関わらず承認が必要。"""
    return [s for s in SENSITIVITY_LEVELS if s != RESTRICTED and _level(s) <= _level(principal.clearance)]


def _active_grant(subject: str) -> ColumnElement[bool]:
    return DataAsset.id.in_(select(AccessGrant.asset_id).where(
        AccessGrant.subject == subject, AccessGrant.revoked_at.is_(None), AccessGrant.expires_at > utcnow()))


def readable_clause(principal: Principal) -> ColumnElement[bool]:
    """一覧取得で使う SQL 条件 (can_read と同じ判定)。DB 側で絞り込むのでページングと件数が正確になる。"""
    if principal.roles & PRIVILEGED_READERS:
        return true()
    party = or_(*[getattr(DataAsset, rel) == principal.subject for rel in ASSET_RELATIONS])
    return or_(party, DataAsset.sensitivity.in_(_clearance_sensitivities(principal)), _active_grant(principal.subject))


def can_read(db: Session, principal: Principal, asset: DataAsset) -> bool:
    if principal.roles & PRIVILEGED_READERS or relations_of(principal, asset):
        return True
    if asset.sensitivity in _clearance_sensitivities(principal):
        return True
    return db.scalar(select(DataAsset.id).where(and_(DataAsset.id == asset.id, _active_grant(principal.subject)))) \
        is not None


def require_read(db: Session, principal: Principal, asset: DataAsset) -> None:
    if not can_read(db, principal, asset):
        if asset.sensitivity == RESTRICTED:
            reason = "restricted 資産の閲覧には Owner の承認が必要です (§11 Access Control)"
        else:
            reason = f"clearance ({principal.clearance}) が資産の機密度 ({asset.sensitivity}) に達していません"
        deny(db, principal, "asset.read", entity_type="asset", entity_id=asset.id, reason=reason)


def permission_matrix() -> dict[str, dict[str, Any]]:
    return {action: r.as_dict() for action, r in sorted(PERMISSIONS.items())}
