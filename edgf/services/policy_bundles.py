"""§16 Policy / §19 Integrity: Policy の変更のワークフロー.

    提案 → 影響評価 (dry-run) → 承認 → 公開 (旧版は superseded)

DB に記録した承認済みの Bundle が Policy の正で、実行時の評価は active の版だけを使う。
ディスクの内容は提案と初回の取り込みの元にすぎず、変更しても評価は変わらない (違いは /ready の policy_drift で示す)。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from edgf.db import SessionLocal
from edgf.enums import BundleStatus
from edgf.models import ApprovalRequest, DataAsset, PolicyBundle, utcnow
from edgf.services import approvals, audit, lineage_trust, outbox, policy
from edgf.services.approval_types import Step
from edgf.services.policy import Policy, PolicyLoadError

log = logging.getLogger("edgf.policy")
B = BundleStatus
BOOTSTRAP_ACTOR = "system:bootstrap"
_PATH = re.compile(r"^(?:[A-Za-z0-9._-]+\.yaml|domains/[A-Za-z0-9._-]+\.yaml|lineage/trusted_producers\.yaml|maturity/criteria\.yaml)$")
MAX_BUNDLE_BYTES = 2_000_000


class PolicyBundleInvalid(ValueError):
    """不正な提案 (422)。"""


class PolicyBundleConflict(ValueError):
    """現在の状態では実行できない操作 (409)。"""


def active_bundle(db: Session) -> PolicyBundle | None:
    return db.scalar(select(PolicyBundle).where(PolicyBundle.status == B.ACTIVE.value))


def _next_version(db: Session) -> int:
    return (db.scalar(select(func.max(PolicyBundle.version))) or 0) + 1


# ---------------------------------------------------------------- 初回の取り込み
def bootstrap(db: Session) -> PolicyBundle:
    """active の版が無いときだけ、ディスクの内容を取り込む。監査には policy.bootstrap で記録する。"""
    found = active_bundle(db)
    if found is not None:
        return found
    files = policy.read_disk_files()
    policy.parse_files(files)
    bundle = PolicyBundle(version=_next_version(db), status=B.ACTIVE.value, files=files, content_hash=policy.content_hash(files),
                          scope="global", proposed_by=BOOTSTRAP_ACTOR, reason="初回の起動でディスクの内容を取り込み",
                          published_by=BOOTSTRAP_ACTOR, published_at=utcnow())
    db.add(bundle)
    db.flush()
    audit.record(db, BOOTSTRAP_ACTOR, "policy.bootstrap", "policy_bundle", str(bundle.version),
                 {"version": bundle.version, "content_hash": bundle.content_hash, "files": sorted(files)})
    return bundle


def bootstrap_on_startup() -> None:
    """起動時に取り込む。DB に届かない、スキーマが無い場合は構文だけ確認する (/ready がスキーマの状態を示す)。"""
    try:
        with SessionLocal() as db:
            bootstrap(db)
            db.commit()
    except IntegrityError:  # 別のプロセスが先に取り込んだ
        log.info("policy bundle was bootstrapped by another process")
    except SQLAlchemyError as e:
        log.warning("policy bootstrap skipped", extra={"fields": {"error": type(e).__name__}})
        policy.load_policies()


def drift_message() -> str | None:
    """ディスクの内容が active の版と異なるか (503 にはしない)。読めなければ判定しない。"""
    try:
        with SessionLocal() as db:
            active = active_bundle(db)
            if active is None:
                return None
            if policy.content_hash(policy.read_disk_files()) != active.content_hash:
                return f"ディスクの内容が active の版 (v{active.version}) と異なります。POST /policies/reload で提案できます"
    except (SQLAlchemyError, OSError) as e:
        log.warning("policy drift check skipped", extra={"fields": {"error": type(e).__name__}})
    return None


# ---------------------------------------------------------------- 提案の検証
def validate_files(files: Mapping[str, str]) -> list[Policy]:
    """提案の時点で検証する。Global の上書き、未知の check type、スコープとパスの不一致は 422。"""
    if sum(len(v.encode("utf-8")) for v in files.values()) > MAX_BUNDLE_BYTES:
        raise PolicyBundleInvalid(f"Bundle が大きすぎます (上限 {MAX_BUNDLE_BYTES} バイト)")
    for path, text in files.items():
        if not _PATH.match(path):
            raise PolicyBundleInvalid(f"パスが不正です: {path} (*.yaml、domains/<ドメイン>.yaml、lineage/trusted_producers.yaml、maturity/criteria.yaml)")
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise PolicyBundleInvalid(f"{path}: YAML を解釈できません") from e
        if not isinstance(doc, dict):
            raise PolicyBundleInvalid(f"{path}: オブジェクトで指定してください")
        if path.startswith(("lineage/", "maturity/")):
            continue  # 信頼できる生成元の宣言と成熟度の基準。中身は parse_files で検証する。変更は Global の扱い (Committee の承認)
        if path.startswith("domains/"):
            # ファイルの置き場所と宣言を一致させ、Domain の変更に見せて Global を変えられないようにする
            expected = path.removeprefix("domains/").removesuffix(".yaml")
            if doc.get("scope") != "domain" or doc.get("domain") != expected:
                raise PolicyBundleInvalid(f"{path}: scope: domain と domain: {expected} を指定してください")
        elif doc.get("scope", "global") != "global":
            raise PolicyBundleInvalid(f"{path}: Domain Policy は domains/ に置いてください")
    try:
        return policy.parse_files(files)
    except PolicyLoadError as e:
        raise PolicyBundleInvalid(str(e)) from e


def _changed_paths(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    return sorted(k for k in {*old, *new} if old.get(k) != new.get(k))


def _classify(old: Mapping[str, str], new: Mapping[str, str]) -> tuple[str, list[str]]:
    changed = _changed_paths(old, new)
    domains = sorted(p.removeprefix("domains/").removesuffix(".yaml") for p in changed if p.startswith("domains/"))
    return ("domain" if domains and len(domains) == len(changed) else "global"), domains


def apply_changes(db: Session, changes: Mapping[str, str | None]) -> tuple[dict[str, str], str, list[str]]:
    """active の版に変更を重ねる。(ファイル一式, 範囲, ドメイン) を返す。"""
    active = active_bundle(db) or bootstrap(db)
    files = dict(active.files)
    for path, text in changes.items():
        if not _PATH.match(path):
            raise PolicyBundleInvalid(f"パスが不正です: {path} (*.yaml、domains/<ドメイン>.yaml、lineage/trusted_producers.yaml、maturity/criteria.yaml)")
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    scope, domains = _classify(active.files, files)
    return files, scope, domains


# ---------------------------------------------------------------- 影響評価 (dry-run)
def _failing(results: Sequence[Mapping[str, Any]]) -> set[str]:
    return {r["policy_id"] for r in results if r["status"] == "fail"}


def _rate(compliant: int, applicable: int) -> float | None:
    return round(compliant / applicable, 6) if applicable else None


def dry_run(db: Session, before: Sequence[Policy], after: Sequence[Policy]) -> dict[str, Any]:
    """提案した Policy で全資産を評価し、active の版と比べた差分を返す。何も書き込まない。"""
    assets = db.scalars(select(DataAsset).where(DataAsset.lifecycle_state != "deleted").order_by(DataAsset.qualified_name)).all()
    newly_failing, newly_passing = [], []
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # 前の準拠、前の対象、後の準拠、後の対象
    for asset in assets:
        old = policy.evaluate_asset(db, asset, list(before))
        new = policy.evaluate_asset(db, asset, list(after))
        old_fail, new_fail = _failing(old), _failing(new)
        identity = {"asset_id": asset.id, "qualified_name": asset.qualified_name, "domain": asset.domain}
        if new_fail - old_fail:
            newly_failing.append({**identity, "policy_ids": sorted(new_fail - old_fail)})
        if old_fail - new_fail:
            newly_passing.append({**identity, "policy_ids": sorted(old_fail - new_fail)})
        row = counts[asset.domain or "(none)"]
        row[0] += len(old) - len(old_fail)
        row[1] += len(old)
        row[2] += len(new) - len(new_fail)
        row[3] += len(new)
    return {"evaluated_at": utcnow().isoformat(), "asset_count": len(assets), "newly_failing": newly_failing,
            "newly_passing": newly_passing,
            "domain_compliance": {d: {"before": _rate(r[0], r[1]), "after": _rate(r[2], r[3])} for d, r in sorted(counts.items())}}


def diff_view(db: Session, bundle: PolicyBundle) -> dict[str, Any]:
    """元の版と比べた Policy の定義の差分と、提案時に記録した影響評価。"""
    base = db.get(PolicyBundle, bundle.base_version) if bundle.base_version else None
    old = {p.id: p.as_dict() for p in policy.parse_files(base.files)} if base else {}
    new = {p.id: p.as_dict() for p in policy.parse_files(bundle.files)}
    return {"version": bundle.version, "status": bundle.status, "base_version": bundle.base_version, "scope": bundle.scope,
            "domains": bundle.domains, "files_changed": _changed_paths(base.files if base else {}, bundle.files),
            "policies_added": sorted(set(new) - set(old)), "policies_removed": sorted(set(old) - set(new)),
            "policies_changed": sorted(k for k in set(old) & set(new) if old[k] != new[k]), "impact": dict(bundle.impact or {})}


# ---------------------------------------------------------------- 提案
def _steps(scope: str, domains: Sequence[str]) -> tuple[Step, ...]:
    """Global Policy を含む変更は Committee。Domain Policy だけの変更は、そのドメインの Owner と Committee。"""
    owners = tuple(Step(f"owner:{d}", "domain_owner", domain=d) for d in domains) if scope == "domain" else ()
    return (*owners, Step("committee", "committee"))


def propose_replacement(db: Session, actor: str, *, files: Mapping[str, str], proposed_by: str, reason: str,
                        rollback_of: int | None = None) -> PolicyBundle:
    active = active_bundle(db) or bootstrap(db)
    after = validate_files(files)
    digest = policy.content_hash(files)
    if digest == active.content_hash:
        raise PolicyBundleConflict(f"active の版 (v{active.version}) と同じ内容です")
    same = db.scalar(select(PolicyBundle).where(PolicyBundle.content_hash == digest, PolicyBundle.base_hash == active.content_hash,
                                                PolicyBundle.status.in_((B.PROPOSED.value, B.APPROVED.value))))
    if same is not None:  # 再読込を繰り返しても提案は増やさない
        return same
    scope, domains = _classify(active.files, files)
    bundle = PolicyBundle(version=_next_version(db), status=B.PROPOSED.value, files=dict(files), content_hash=digest,
                          base_version=active.version, base_hash=active.content_hash, scope=scope, domains=domains, reason=reason,
                          proposed_by=proposed_by, rollback_of=rollback_of,
                          impact=dry_run(db, policy.parse_files(active.files), after))
    db.add(bundle)
    db.flush()
    req = approvals.create(db, actor, type_name="policy_change", requested_by=proposed_by, target_type="policy_bundle",
                           target_id=str(bundle.version), reason=reason, payload={"scope": scope, "domains": domains, "rollback_of": rollback_of},
                           steps=_steps(scope, domains))
    bundle.approval_id = req.id
    db.flush()
    audit.record(db, actor, "policy.propose", "policy_bundle", str(bundle.version), {
        "content_hash": digest, "base_version": active.version, "scope": scope, "domains": domains, "reason": reason, "rollback_of": rollback_of,
        "newly_failing": len(bundle.impact["newly_failing"]), "newly_passing": len(bundle.impact["newly_passing"])})
    return bundle


def propose_rollback(db: Session, actor: str, *, proposed_by: str, reason: str) -> PolicyBundle:
    previous = db.scalar(select(PolicyBundle).where(PolicyBundle.status == B.SUPERSEDED.value)
                         .order_by(PolicyBundle.superseded_at.desc(), PolicyBundle.version.desc()).limit(1))
    if previous is None:
        raise PolicyBundleConflict("切り戻せる版がありません")
    return propose_replacement(db, actor, files=previous.files, proposed_by=proposed_by, reason=reason, rollback_of=previous.version)


def _bundle_of(db: Session, req: ApprovalRequest) -> PolicyBundle:
    return db.get(PolicyBundle, int(req.target_id))


def on_approved(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    bundle = _bundle_of(db, req)
    bundle.status, bundle.approved_at = B.APPROVED.value, utcnow()


def on_rejected(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    _bundle_of(db, req).status = B.REJECTED.value


def publish(db: Session, actor: str, bundle: PolicyBundle, publisher: str) -> None:
    if bundle.status != B.APPROVED.value:
        raise PolicyBundleConflict(f"状態が {bundle.status} の版は公開できません (承認済みの版だけ)")
    # active の行をロックする。同じ版を元にした提案を同時に公開しても、active の版が 2 つにならない
    active = db.scalar(select(PolicyBundle).where(PolicyBundle.status == B.ACTIVE.value).with_for_update())
    if active is None or active.content_hash != bundle.base_hash:
        raise PolicyBundleConflict("提案の後に active の版が変わりました。影響評価をやり直すため、提案し直してください")
    validate_files(bundle.files)
    trust_changed = (active.files or {}).get(lineage_trust.TRUST_FILE) != (bundle.files or {}).get(lineage_trust.TRUST_FILE)
    now = utcnow()
    active.status, active.superseded_at = B.SUPERSEDED.value, now
    db.flush()
    bundle.status, bundle.published_by, bundle.published_at = B.ACTIVE.value, publisher, now
    db.flush()
    audit.record(db, actor, "policy.publish", "policy_bundle", str(bundle.version), {
        "version": bundle.version, "content_hash": bundle.content_hash, "superseded": active.version, "rollback_of": bundle.rollback_of})
    outbox.emit(db, "policy.published", str(bundle.version), {"version": bundle.version, "content_hash": bundle.content_hash,
                                                              "superseded": active.version, "rollback_of": bundle.rollback_of})
    if trust_changed:
        # 信頼できる生成元が変わると、保存済みの PII の継承の結果が変わりうる。公開と同じトランザクションで計算し直す
        from edgf.services import ai_inherit  # 循環 import 回避

        ai_inherit.recompute_all(db, actor)
