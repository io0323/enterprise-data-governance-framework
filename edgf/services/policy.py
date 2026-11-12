"""§13 / §15 Policy as Code engine.

YAML で宣言された Global / Domain Policy を資産メタデータに対して評価する。
OPA 等の外部ポリシーエンジンへ置換可能なよう、評価結果は単純な dict で返す。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.enums import BundleStatus
from edgf.models import DataAsset, PolicyBundle, PolicyException, QualityRule, utcnow, term_asset
from edgf.services import lineage_trust

CHECK_TYPES = {"required_fields", "field_in", "required_controls", "min_quality_rules",
               "min_quality_score", "has_upstream_lineage", "glossary_linked",
               "anonymization_k", "pseudonymization_controls", "pii_columns_documented", "mdm_entity_defined",
               "ai_training_approved", "ai_inheritance", "ai_rag_certified_sources", "ai_embedding_protected"}
# §15 Data Product の出荷基準 (DATSIS)。評価の対象が Data Product のもの (target: product)
MESH_CHECK_TYPES = {"mesh_discoverable", "mesh_addressable", "mesh_trustworthy", "mesh_self_describing", "mesh_interoperable",
                    "mesh_secure", "mesh_reachable"}
# target: product で使える check type。Domain Policy はこれらで基準を追加できる
PRODUCT_CHECK_TYPES = MESH_CHECK_TYPES | {"required_fields", "field_in"}
ASSET, PRODUCT = "asset", "product"


class PolicyLoadError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    severity: str
    check: dict
    scope: str = "global"
    domain: str | None = None
    when: dict = field(default_factory=dict)
    design_ref: str = ""
    target: str = ASSET

    def applies_to(self, asset: Any) -> bool:
        """asset は DataAsset か DataProduct (target による)。どちらも domain を持つ。"""
        if self.scope == "domain" and asset.domain != self.domain:
            return False
        for key, expected in self.when.items():
            actual = getattr(asset, key, None)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "severity": self.severity, "scope": self.scope,
                "domain": self.domain, "when": self.when, "check": self.check, "design_ref": self.design_ref, "target": self.target}


def read_disk_files(policy_dir: Path | None = None) -> dict[str, str]:
    """ディスクの Policy の YAML 一式 (相対パス → 本文)。実行時の正ではなく、提案と初回の取り込みの元にする。"""
    base = Path(policy_dir or settings.policy_dir)
    return {str(f.relative_to(base)): f.read_text(encoding="utf-8")
            for f in [*sorted(base.glob("*.yaml")), *sorted((base / "domains").glob("*.yaml")),
                      *sorted((base / "lineage").glob("trusted_producers.yaml"))]}


def parse_files(files: Mapping[str, str]) -> list[Policy]:
    """YAML 一式を検証して読み込む。Global Policy の上書きと、未知の check type はここで拒否する。"""
    policies: dict[str, Policy] = {}
    top_level = sorted(k for k in files if "/" not in k)
    for name in [*top_level, *sorted(k for k in files if "/" in k)]:
        if name == lineage_trust.TRUST_FILE:  # Policy ではない。信頼できる生成元の宣言 (Phase 16)
            try:
                lineage_trust.parse(files[name])
            except lineage_trust.TrustFileInvalid as e:
                raise PolicyLoadError(str(e)) from e
            continue
        try:
            doc = yaml.safe_load(files[name]) or {}
        except yaml.YAMLError as e:
            raise PolicyLoadError(f"{name}: YAML を解釈できません") from e
        if not isinstance(doc, dict):
            raise PolicyLoadError(f"{name}: オブジェクトで指定してください")
        scope = doc.get("scope", "global")
        domain = doc.get("domain")
        if scope == "domain" and not domain:
            raise PolicyLoadError(f"{name}: domain scope requires 'domain'")
        for p in doc.get("policies", []):
            try:
                pid, check_type, policy_name = p["id"], p["check"]["type"], p["name"]
            except (KeyError, TypeError) as e:
                raise PolicyLoadError(f"{name}: ポリシーには id / name / check.type が必要です") from e
            if pid in policies:
                # Federated Governance: ドメインは Global Policy を上書き(緩和)できない
                raise PolicyLoadError(f"{name}: duplicate policy id {pid} (override is not allowed)")
            target = p.get("target", ASSET)
            if target not in (ASSET, PRODUCT):
                raise PolicyLoadError(f"{name}: unknown target {target}")
            if check_type not in (PRODUCT_CHECK_TYPES if target == PRODUCT else CHECK_TYPES):
                raise PolicyLoadError(f"{name}: unknown check type {check_type} for target {target}")
            policies[pid] = Policy(id=pid, name=policy_name, severity=p.get("severity", "medium"), check=p["check"],
                                   scope=scope, domain=domain, when=p.get("when", {}), design_ref=p.get("design_ref", ""),
                                   target=target)
    return list(policies.values())


def load_policies(policy_dir: Path | None = None) -> list[Policy]:
    return parse_files(read_disk_files(policy_dir))


def content_hash(files: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(files), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


_cache: list[Policy] | None = None
_cache_hash: str | None = None


def _active(db: Session) -> tuple[str, dict[str, str]] | None:
    row = db.execute(select(PolicyBundle.content_hash).where(PolicyBundle.status == BundleStatus.ACTIVE.value)).first()
    if row is None:
        return None
    if row[0] == _cache_hash and _cache is not None:
        return row[0], {}
    return row[0], db.scalar(select(PolicyBundle.files).where(PolicyBundle.status == BundleStatus.ACTIVE.value))


def _resolve(db: Session) -> list[Policy]:
    """active の版の Policy を返す。公開されると内容ハッシュが変わるので、複数のプロセスでも追従する。"""
    global _cache, _cache_hash
    active = _active(db)
    if active is None:
        # 起動時にスキーマが無く取り込めなかった場合 (後からマイグレーションした場合) は、ここで取り込む
        from edgf.services import policy_bundles
        bundle = policy_bundles.bootstrap(db)
        active = bundle.content_hash, bundle.files
    digest, files = active
    if digest != _cache_hash or _cache is None:
        _cache, _cache_hash = parse_files(files), digest
    return _cache


def get_policies(reload: bool = False, *, db: Session | None = None) -> list[Policy]:
    """実行時に評価する Policy。db を渡すとそのセッションで読む (評価の中ではこちらを使う)。

    db を渡さないのは /ready の構文の確認だけ。DB に届かなくても、手元の情報で判定する (参照は fail-open。§19)。
    """
    global _cache, _cache_hash
    if reload:
        _cache, _cache_hash = None, None
    if db is not None:
        return _resolve(db)
    try:
        with SessionLocal() as session:
            found = _active(session)
            if found is None:
                return load_policies()
            digest, files = found
            if digest != _cache_hash or _cache is None:
                _cache, _cache_hash = parse_files(files), digest
            return _cache
    except SQLAlchemyError:
        return _cache if _cache is not None else load_policies()


def _run_check(db: Session, asset: Any, check: dict) -> tuple[bool | None, str]:
    """(合否, メッセージ) を返す。None は対象外で、評価結果に含めない。"""
    t = check["type"]
    if t in MESH_CHECK_TYPES:
        from edgf.services import mesh_datsis
        return mesh_datsis.run(db, asset, check)
    if t == "mdm_entity_defined":
        from edgf.services.mdm_rules import check_entity_defined
        return check_entity_defined(db, asset)
    if t == "ai_training_approved":
        from edgf.services.ai_training import check_training_approved
        return check_training_approved(db, asset)
    if t in ("ai_inheritance", "ai_rag_certified_sources", "ai_embedding_protected"):
        from edgf.services import ai_inherit
        return {"ai_inheritance": ai_inherit.check_inheritance,
                "ai_rag_certified_sources": ai_inherit.check_rag_sources,
                "ai_embedding_protected": ai_inherit.check_embedding_protected}[t](db, asset)
    if t in ("anonymization_k", "pseudonymization_controls", "pii_columns_documented"):
        from edgf.services import privacy
        return {"anonymization_k": privacy.check_anonymization_k,
                "pseudonymization_controls": privacy.check_pseudonymization,
                "pii_columns_documented": privacy.check_pii_documented}[t](asset)
    if t == "required_fields":
        missing = [f for f in check["fields"] if getattr(asset, f, None) in (None, "", [])]
        return not missing, f"未設定: {missing}" if missing else "ok"
    if t == "field_in":
        v = getattr(asset, check["field"], None)
        return v in check["values"], f"{check['field']}={v} は {check['values']} のいずれでもありません" if v not in check["values"] else "ok"
    if t == "required_controls":
        missing = [c for c in check["controls"] if c not in (asset.controls or [])]
        return not missing, f"未実装統制: {missing}" if missing else "ok"
    if t == "min_quality_rules":
        n = db.scalar(select(func.count(QualityRule.id)).where(QualityRule.asset_id == asset.id)) or 0
        return n >= check["count"], f"品質ルール {n}件 < {check['count']}" if n < check["count"] else "ok"
    if t == "min_quality_score":
        from edgf.services.quality import latest_run
        run = latest_run(db, asset.id)
        ok = run is not None and run.score >= check["score"]
        return ok, "ok" if ok else f"Quality Score {run.score if run else None} < {check['score']}"
    if t == "has_upstream_lineage":
        from edgf.services.lineage import has_upstream
        ok = has_upstream(db, asset.qualified_name)
        return ok, "ok" if ok else "上流リネージが未登録です"
    if t == "glossary_linked":
        n = db.scalar(select(func.count()).select_from(term_asset).where(term_asset.c.asset_id == asset.id)) or 0
        return n > 0, "ok" if n else "Glossary用語が未紐付けです"
    raise PolicyLoadError(f"unknown check type {t}")


def _active_exceptions(db: Session, asset_id: str) -> set[str]:
    now = utcnow()
    return set(db.scalars(select(PolicyException.policy_id).where(
        PolicyException.asset_id == asset_id, PolicyException.status == "approved", PolicyException.expires_at > now)))


def evaluate_asset(db: Session, asset: DataAsset, policies: list[Policy] | None = None, *, excepted: set[str] | None = None) -> list[dict[str, Any]]:
    """excepted は有効な例外の一括読み込みの結果 (省略時は問い合わせる)。"""
    excepted = _active_exceptions(db, asset.id) if excepted is None else excepted
    results = []
    for p in get_policies(db=db) if policies is None else policies:
        if p.target != ASSET or not p.applies_to(asset):
            continue
        ok, msg = _run_check(db, asset, p.check)
        if ok is None:
            continue
        status = "pass" if ok else ("excepted" if p.id in excepted else "fail")
        results.append({"policy_id": p.id, "name": p.name, "severity": p.severity, "scope": p.scope,
                        "status": status, "message": msg, "design_ref": p.design_ref})
    return results


def evaluate_product(db: Session, product: Any, policies: list[Policy] | None = None) -> list[dict[str, Any]]:
    """Data Product の出荷基準を評価する。ポリシー例外は適用しない (緩和の経路を作らない)。"""
    results = []
    for p in get_policies(db=db) if policies is None else policies:
        if p.target != PRODUCT or not p.applies_to(product):
            continue
        ok, msg = _run_check(db, product, p.check)
        results.append({"policy_id": p.id, "name": p.name, "severity": p.severity, "scope": p.scope,
                        "status": "pass" if ok else "fail", "message": msg, "design_ref": p.design_ref})
    return results


def evaluate_all(db: Session) -> dict:
    assets = db.scalars(select(DataAsset).where(DataAsset.lifecycle_state != "deleted")).all()
    per_asset, applicable, compliant = [], 0, 0
    for a in assets:
        res = evaluate_asset(db, a)
        applicable += len(res)
        compliant += sum(1 for r in res if r["status"] in ("pass", "excepted"))
        per_asset.append({"asset_id": a.id, "qualified_name": a.qualified_name, "domain": a.domain,
                          "violations": [r for r in res if r["status"] == "fail"], "results": res})
    return {"compliance_rate": round(compliant / applicable, 6) if applicable else 1.0,
            "applicable_checks": applicable, "compliant_checks": compliant, "assets": per_asset}


def evaluate_and_open_issues(db: Session, actor: str) -> dict:
    """全資産の自動監査と、違反の Issue 化 (重複起票なし)。"""
    from edgf.services import issues

    report = evaluate_all(db)
    opened = 0
    for a in report["assets"]:
        asset = db.get(DataAsset, a["asset_id"])
        for v in a["violations"]:
            if issues.open_issue(db, actor, asset=asset, kind="policy", severity=v["severity"], source_ref=f"policy:{v['policy_id']}",
                                 title=f"[POLICY] {a['qualified_name']}: {v['policy_id']} {v['name']}", description=v["message"]):
                opened += 1
    report["issues_opened"] = opened
    return report
