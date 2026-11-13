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

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from edgf.auth import (ADMIN, COMMITTEE, CONSENT_COLLECTOR, CONSUMER, CUSTODIAN, OWNER, ROLES, SENSITIVITY_LEVELS,
                       STEWARD, Principal)
from edgf.errors import ProblemError
from edgf.models import AccessGrant, DataAsset, utcnow

ASSET_RELATIONS = ("owner", "steward", "custodian")
RELATIONS = frozenset({*ASSET_RELATIONS, "assignee", "requester"})
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
    "lineage.column_write": rule(roles=(ADMIN, OWNER, STEWARD, CUSTODIAN), description="カラム単位のリネージの登録・取込 (columnLineage facet)"),
    # Policy / Compliance (§13, §15)
    "policy.reload": rule(roles=(COMMITTEE, ADMIN), description="ディスクの内容から Policy の変更を提案する (即時には反映しない)"),
    "policy.propose": rule(roles=(ADMIN, COMMITTEE), domain_roles=(OWNER,),
                           description="Policy の変更の提案 (ドメイン内の Owner は Domain Policy だけ)"),
    "policy.publish": rule(roles=(COMMITTEE,), approval=True, description="承認済みの Policy の公開"),
    "policy.evaluate_all": rule(roles=(COMMITTEE, ADMIN), description="全資産の自動監査と Issue 化"),
    "exception.request": rule(roles=(ADMIN,), domain_roles=(OWNER, STEWARD), relations=("owner", "steward"),
                              description="ポリシー例外の申請"),
    "exception.decide": rule(roles=(COMMITTEE, ADMIN), approval=True, description="ポリシー例外の承認/却下"),
    # Privacy (§12)
    "privacy.inventory.read": rule(roles=(ADMIN, COMMITTEE), domain_roles=(OWNER, STEWARD),
                                   description="PII インベントリの参照"),
    "privacy.purpose.write": rule(roles=(ADMIN, COMMITTEE), description="Purpose カタログの登録・更新"),
    "privacy.consent.write": rule(roles=(ADMIN, COMMITTEE, CONSENT_COLLECTOR), domain_roles=(STEWARD,),
                                  description="同意の取得・撤回の記録 (ドメインは Purpose の所属で判定)"),
    "privacy.consent.check": rule(roles=(ADMIN, COMMITTEE, CONSENT_COLLECTOR), domain_roles=(STEWARD,),
                                  description="目的内利用の判定と同意履歴の参照"),
    "privacy.erasure.request": rule(roles=(ADMIN, COMMITTEE),
                                    description="消去要求の受付・対象特定・削除指示の発行・期限超過の処理"),
    "privacy.erasure.read": rule(roles=(ADMIN, COMMITTEE), relations=("owner",), description="消去要求の参照"),
    "privacy.erasure.approve": rule(roles=(COMMITTEE,), relations=("owner",), approval=True,
                                    description="消去の対象範囲の承認 (Owner は自分の資産の分のみ)"),
    "privacy.erasure.reject": rule(roles=(COMMITTEE,), approval=True, description="消去要求の却下 (理由必須)"),
    "privacy.erasure.verify": rule(roles=(COMMITTEE,), relations=("owner",), approval=True,
                                   description="消去の完了確認とクローズ"),
    "privacy.erasure.certificate": rule(roles=(ADMIN, COMMITTEE), description="消去証明の発行"),
    "privacy.erasure.task_read": rule(roles=(ADMIN, COMMITTEE), relations=("custodian",),
                                      description="消去タスクの取得 (Custodian は自分の資産の分のみ)"),
    # 物理削除の実行と報告 (§4.2 ライフサイクル実行 R)。承認ではないため Custodian を含む
    "privacy.erasure.task_ack": rule(relations=("custodian",), description="消去タスクの受領"),
    "privacy.erasure.task_complete": rule(relations=("custodian",), description="消去タスクの完了報告"),
    # Master Data Management (§8)
    "mdm.read": rule(authenticated=True, description="MDM のメタデータの参照 (ABAC で絞り込む。値は返さない)"),
    "mdm.rule.write": rule(roles=(ADMIN, COMMITTEE), approval=True, description="エンティティ定義の再読込 (公開)"),
    "mdm.rule.approve": rule(relations=("owner",), approval=True, description="Golden Record 生成規則の承認 (§8.2)"),
    # Data Plane からの呼び出しを想定した実行行為 (承認ではないため Custodian を含む)
    "mdm.match": rule(domain_roles=(STEWARD,), relations=("custodian",), description="ソースレコードのマッチング"),
    "mdm.compose": rule(domain_roles=(STEWARD,), relations=("custodian",),
                        description="Golden Record の生成と内容ハッシュの検証"),
    "mdm.review.decide": rule(relations=("owner", "steward"), approval=True,
                              description="裁定 (merge / not_match / split) と属性値の指定"),
    "mdm.review.escalate": rule(roles=(ADMIN, COMMITTEE), description="裁定キューの期限超過の処理"),
    "refdata.write": rule(domain_roles=(OWNER, STEWARD), description="Reference Data の下書きの作成"),
    "refdata.publish": rule(roles=(COMMITTEE,), relations=("owner",), approval=True,
                            description="Reference Data の版の公開"),
    # Data Governance & AI (§14)
    "ai.card.write": rule(domain_roles=(STEWARD,), relations=("owner", "steward"),
                          description="Dataset Card / Model Card の登録・更新"),
    # Data Plane のパイプラインからの登録を想定 (承認ではないため Custodian を含む)
    "ai.dataset_version.register": rule(relations=ASSET_RELATIONS, description="Dataset Version の登録"),
    "ai.model_version.register": rule(relations=ASSET_RELATIONS,
                                      description="Model Version の登録、デプロイと利用先の記録"),
    "ai.training.request": rule(relations=("owner", "steward"), description="学習利用の承認の申請と取り下げ"),
    "ai.training.approve": rule(roles=(COMMITTEE,), relations=("owner",), approval=True,
                                description="学習利用の承認 (構成元の資産の Owner と Committee)"),
    "ai.model.approve": rule(relations=("owner",), approval=True, description="Model Version の承認と再開"),
    "ai.model.suspend": rule(roles=(COMMITTEE,), relations=("owner",), description="Model Version の停止と廃止"),
    "ai.incident.create": rule(roles=(COMMITTEE,), domain_roles=(OWNER, STEWARD), description="データ汚染・リコールの起票"),
    "ai.remediation.decide": rule(roles=(COMMITTEE,), approval=True,
                                  description="消去の影響を受けたモデル版の是正の判断 (retrain / unlearn / accept)"),
    # 判断された是正の実施報告 (判断ではないため Custodian を含む)
    "ai.remediation.complete": rule(relations=("owner", "custodian"), description="是正の完了報告"),
    "ai.remediation.escalate": rule(roles=(ADMIN, COMMITTEE), description="是正の期限超過の処理"),
    "ai.inheritance.recompute": rule(roles=(ADMIN, COMMITTEE), description="継承した統制属性の全件再計算"),
    # RAG サービスの実行主体が検索のたびに呼ぶ判定 (承認ではないため Custodian を含む)
    "ai.rag.authorize": rule(relations=("custodian",), description="RAG の検索結果を利用者の権限で絞り込む判定"),
    # Data Mesh 連携 (§15): Data Product の統治のための定義だけを扱う
    "mesh.product.write": rule(domain_roles=(OWNER, STEWARD), description="Data Product と Output Port の登録・更新"),
    "mesh.product.publish": rule(relations=("owner",), approval=True,
                                 description="Data Product の公開・非推奨・廃止 (対象 Product の Owner)"),
    "mesh.contract.write": rule(domain_roles=(OWNER, STEWARD), description="Data Contract の下書きの作成・更新"),
    "mesh.contract.publish": rule(relations=("owner",), approval=True,
                                  description="Data Contract の版の公開と旧版の retire (対象 Product の Owner)"),
    # Data Plane の CI からの呼び出しを想定した判定 (承認ではないため Custodian を含む)
    "mesh.contract.check": rule(relations=ASSET_RELATIONS, description="Contract の案の互換性の判定と影響の確認"),
    "mesh.contract.expedite_retire": rule(roles=(COMMITTEE,), approval=True, description="旧版の併存期間の短縮 (理由必須)"),
    "mesh.subscription.request": rule(authenticated=True, description="利用登録の申請と自分の登録の変更 (閲覧権限は与えない)"),
    "mesh.subscription.approve": rule(relations=("owner",), approval=True, description="利用登録の承認 (対象 Product の Owner)"),
    "mesh.subscription.read": rule(roles=(ADMIN, COMMITTEE), description="全利用者の利用登録の参照"),
    "cdm.import": rule(roles=(ADMIN, COMMITTEE), approval=True,
                       description="外部の CDM 定義の版の取り込み (作成・編集の機能は持たない)"),
    # 汎用の承認エンジン (§16 Approval)。decide の実際の判定は種別とステップごとのルール (approval_types.py) で行う
    "approval.request": rule(authenticated=True, description="承認の申請 (種別ごとの申請条件は approval_types で定義する)"),
    "approval.decide": rule(roles=(COMMITTEE,), domain_roles=(OWNER,), relations=("owner",), approval=True,
                            description="承認の判断 (種別とステップごとのルールで判定する)"),
    "approval.cancel": rule(roles=(ADMIN,), relations=("requester",), description="承認の申請の取り消し (申請者本人、admin)"),
    "approval.read": rule(roles=(ADMIN, COMMITTEE), description="全案件の参照 (ABAC で絞り込む)"),
    # イベント配信 (§16)
    "webhook.manage": rule(roles=(ADMIN,), description="Webhook の配信先の登録・更新・参照"),
    "outbox.redeliver": rule(roles=(ADMIN,), description="デッドレターの参照と手動の再送"),
    # 定期レビュー (§16 Review / §11 四半期の棚卸)
    "review.decide": rule(relations=("owner",), approval=True, description="棚卸と証明の判断 (対象資産の Owner)"),
    "review.read": rule(roles=(ADMIN, COMMITTEE), description="キャンペーンと全項目の参照"),
    "policy.review": rule(roles=(COMMITTEE,), approval=True, description="Policy の年次見直しの記録"),
    # ドメイン別の成熟度の評価 (§20。Phase 18)
    "maturity.assessment.create": rule(roles=(COMMITTEE, ADMIN), description="成熟度の評価の起票、自動の判定、取り消し (定期の評価はジョブが起票する)"),
    "maturity.self_assess": rule(domain_roles=(OWNER,), description="成熟度の自己評価 (対象ドメインの Owner)"),
    "maturity.decide": rule(roles=(COMMITTEE,), approval=True,
                            description="成熟度の判定 (対象ドメインの Owner と Steward は Committee でも加われない)"),
    "maturity.reference.set": rule(roles=(COMMITTEE,), approval=True, description="参照ドメイン (先行ドメイン) の指定と解除"),
    "maturity.read": rule(authenticated=True, description="成熟度の評価と全社の成熟度の参照"),
    # ジョブ (§16 / §19 Observability)
    "jobs.run": rule(roles=(ADMIN,), description="ジョブの手動実行 (通常は外部の cron や CronJob から実行する)"),
    "jobs.read": rule(roles=(ADMIN, COMMITTEE), description="ジョブの実行状況の参照"),
    # Data Plane 連携 (§7 Discovery / §17 Usage / §15 Addressable)。Data Plane のサービスの主体を想定
    "discovery.push": rule(roles=(ADMIN,), domain_roles=(CUSTODIAN,), description="資産一覧のスナップショットの送信 (ソースのドメイン)"),
    "discovery.read": rule(roles=(ADMIN, COMMITTEE), domain_roles=(OWNER, STEWARD), description="検出された資産と保留のスキーマの変化の参照"),
    "discovery.apply_breaking": rule(relations=("owner", "steward"), approval=True, description="破壊的なスキーマの変化を確認して Catalog に反映"),
    "usage.push": rule(roles=(ADMIN,), domain_roles=(CUSTODIAN,), description="利用実績の集計値の送信 (資産のドメイン)"),
    "reachability.report": rule(relations=("custodian",), description="Output Port の到達の報告 (ポートの資産の Custodian)"),
    "probe.configure": rule(roles=(ADMIN,), description="Control Plane からの到達の確認の実行と設定の参照"),
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
        if r.approval and CONSENT_COLLECTOR in r.roles | r.domain_roles:
            raise ValueError(f"{action}: サービスアカウント用ロールに承認権限を与えることはできません")


validate_permissions(PERMISSIONS)


# ---------------------------------------------------------------- 判定
def relations_of(principal: Principal, asset: DataAsset | None) -> set[str]:
    if asset is None:
        return set()
    return {rel for rel in ASSET_RELATIONS if getattr(asset, rel, None) and getattr(asset, rel) == principal.subject}


def is_allowed(principal: Principal, action: str, *, asset: DataAsset | None = None, domain: str | None = None,
               relations: Iterable[str] = ()) -> bool:
    return rule_allows(principal, PERMISSIONS[action], asset=asset, domain=domain, relations=relations)


def rule_allows(principal: Principal, r: Rule, *, asset: DataAsset | None = None, domain: str | None = None,
                relations: Iterable[str] = ()) -> bool:
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


def has_global_role(principal: Principal, action: str) -> bool:
    """ドメイン内ロールを除いた判定。全ドメイン (*) に所属する Owner でも全社の操作は許さない場合に使う。"""
    return bool(principal.roles & PERMISSIONS[action].roles)


def require(db: Session, principal: Principal, action: str, *, asset: DataAsset | None = None,
            domain: str | None = None, relations: Iterable[str] = (), entity_type: str = "asset",
            entity_id: str | None = None) -> None:
    if not is_allowed(principal, action, asset=asset, domain=domain, relations=relations):
        r = PERMISSIONS[action]
        deny(db, principal, action, entity_type=entity_type,
             entity_id=entity_id or (asset.id if asset is not None else None),
             reason=f"この操作 ({action}: {r.description}) を行う権限がありません", required=r)
    if PERMISSIONS[action].approval:
        require_step_up(principal, action)


def require_step_up(principal: Principal, action: str) -> None:
    """✓ の操作 (承認行為) を UI の経路で行うなら、最近ログインしていること (Phase 17)。Bearer の経路には適用しない。

    認可の判定のあとに確かめるので、権限の無い利用者には従来どおり 403 を返す。
    """
    from edgf.services.ui_sessions import step_up_fresh  # 循環 import 回避

    if not step_up_fresh(principal):
        raise ProblemError(401, f"この操作 ({action}) の前に、もう一度ログインしてください (ログインから時間がたっています)",
                           type_="step-up-required", action=action, login_url="/ui/auth/login?max_age=0")


# ---------------------------------------------------------------- ABAC (読み取り)
def effective_sensitivity(asset: DataAsset) -> str:
    """ABAC で使う機密度。AI 資産は上流から継承した実効値で判定する (§14 Embedding / Vector / RAG)。"""
    return asset.effective_sensitivity or asset.sensitivity


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
    sensitivity = func.coalesce(DataAsset.effective_sensitivity, DataAsset.sensitivity)
    return or_(party, sensitivity.in_(_clearance_sensitivities(principal)), _active_grant(principal.subject))


def can_read(db: Session, principal: Principal, asset: DataAsset) -> bool:
    if principal.roles & PRIVILEGED_READERS or relations_of(principal, asset):
        return True
    if effective_sensitivity(asset) in _clearance_sensitivities(principal):
        return True
    return db.scalar(select(DataAsset.id).where(and_(DataAsset.id == asset.id, _active_grant(principal.subject)))) \
        is not None


def require_read(db: Session, principal: Principal, asset: DataAsset) -> None:
    if not can_read(db, principal, asset):
        sensitivity = effective_sensitivity(asset)
        if sensitivity == RESTRICTED:
            reason = "restricted 資産の閲覧には Owner の承認が必要です (§11 Access Control)"
        else:
            reason = f"clearance ({principal.clearance}) が資産の機密度 ({sensitivity}) に達していません"
        deny(db, principal, "asset.read", entity_type="asset", entity_id=asset.id, reason=reason)


def require_scope(db: Session, principal: Principal, action: str, domain_column: Any = None) -> ColumnElement[bool]:
    """ドメイン横断の一覧で使う SQL 条件。全社ロールは全件、ドメイン内ロールは所属ドメインのみ。権限が無ければ 403。"""
    r = PERMISSIONS[action]
    domain_column = DataAsset.domain if domain_column is None else domain_column
    if r.authenticated or principal.roles & r.roles:
        return true()
    if principal.roles & r.domain_roles:
        if "*" in principal.domains:
            return true()
        if principal.domains:
            return domain_column.in_(sorted(principal.domains))
    deny(db, principal, action, entity_type="privacy", entity_id="*",
         reason=f"この操作 ({action}: {r.description}) を行う権限がありません", required=r)


def permission_matrix() -> dict[str, dict[str, Any]]:
    return {action: r.as_dict() for action, r in sorted(PERMISSIONS.items())}
