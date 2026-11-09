"""§13 / §15 Policy as Code engine.

YAML で宣言された Global / Domain Policy を資産メタデータに対して評価する。
OPA 等の外部ポリシーエンジンへ置換可能なよう、評価結果は単純な dict で返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.models import DataAsset, PolicyException, QualityRule, utcnow, term_asset

CHECK_TYPES = {"required_fields", "field_in", "required_controls", "min_quality_rules",
               "min_quality_score", "has_upstream_lineage", "glossary_linked",
               "anonymization_k", "pseudonymization_controls", "pii_columns_documented", "mdm_entity_defined",
               "ai_training_approved", "ai_inheritance", "ai_rag_certified_sources", "ai_embedding_protected"}
# §15 Data Product の出荷基準 (DATSIS)。評価の対象が Data Product のもの (target: product)
MESH_CHECK_TYPES = {"mesh_discoverable", "mesh_addressable", "mesh_trustworthy", "mesh_self_describing", "mesh_interoperable",
                    "mesh_secure"}
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


def load_policies(policy_dir: Path | None = None) -> list[Policy]:
    base = Path(policy_dir or settings.policy_dir)
    files = sorted(base.glob("*.yaml")) + sorted((base / "domains").glob("*.yaml"))
    policies: dict[str, Policy] = {}
    for f in files:
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        scope = doc.get("scope", "global")
        domain = doc.get("domain")
        if scope == "domain" and not domain:
            raise PolicyLoadError(f"{f.name}: domain scope requires 'domain'")
        for p in doc.get("policies", []):
            pid = p["id"]
            if pid in policies:
                # Federated Governance: ドメインは Global Policy を上書き(緩和)できない
                raise PolicyLoadError(f"{f.name}: duplicate policy id {pid} (override is not allowed)")
            target = p.get("target", ASSET)
            check_type = p["check"]["type"]
            if target not in (ASSET, PRODUCT):
                raise PolicyLoadError(f"{f.name}: unknown target {target}")
            if check_type not in (PRODUCT_CHECK_TYPES if target == PRODUCT else CHECK_TYPES):
                raise PolicyLoadError(f"{f.name}: unknown check type {check_type} for target {target}")
            policies[pid] = Policy(id=pid, name=p["name"], severity=p.get("severity", "medium"), check=p["check"],
                                   scope=scope, domain=domain, when=p.get("when", {}), design_ref=p.get("design_ref", ""),
                                   target=target)
    return list(policies.values())


_cache: list[Policy] | None = None


def get_policies(reload: bool = False) -> list[Policy]:
    global _cache
    if _cache is None or reload:
        _cache = load_policies()
    return _cache


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


def evaluate_asset(db: Session, asset: DataAsset, policies: list[Policy] | None = None) -> list[dict[str, Any]]:
    excepted = _active_exceptions(db, asset.id)
    results = []
    for p in policies or get_policies():
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
    for p in policies or get_policies():
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
