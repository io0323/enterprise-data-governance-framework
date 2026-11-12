"""§14 Embedding / Vector / RAG: 統制属性の継承 (Lineage の上流を辿って実効値を決める).

実効機密度 = 自身と全上流の最大値、実効 contains_pii = 自身と全上流の論理和。
継承が解除されるのは、上流で PRV-003 を満たす anonymized を経た場合だけ (仮名化では解除しない。§12)。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import Certification, DataClass, EscalationLevel, PrivacyTechnique
from edgf.models import DataAsset, LineageEdge
from edgf.services import audit, issues, lineage, lineage_trust
from edgf.services.privacy import effective_pii_columns, is_anonymized

AI = DataClass.AI.value
MAX_DEPTH = 50
# AI-004 の「pseudonymized 以上」: 仮名化と、それより強い手法
PROTECTED_TECHNIQUES = (PrivacyTechnique.PSEUDONYMIZED.value, PrivacyTechnique.ANONYMIZED.value,
                        PrivacyTechnique.AGGREGATED.value)
INHERITANCE_POLICY = "AI-002"


def level(sensitivity: str | None) -> int:
    return SENSITIVITY_LEVELS.index(sensitivity) if sensitivity in SENSITIVITY_LEVELS else 0


@dataclass
class Inherited:
    sensitivity: str
    contains_pii: bool
    sensitivity_from: list[str] = field(default_factory=list)  # 実効機密度を決めた上流
    pii_from: list[str] = field(default_factory=list)
    released_by: list[str] = field(default_factory=list)  # 継承を解除した anonymized の資産
    unregistered_upstream: list[str] = field(default_factory=list)
    # Phase 16: DIRECT 経路で PII のカラムが届くカラム (広げる方向) と、信頼できる生成元の辺だけで PII が届かない上流
    pii_columns: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    pii_excluded: list[dict] = field(default_factory=list)

    def view(self) -> dict:
        out = {"sensitivity_from": self.sensitivity_from, "pii_from": self.pii_from, "released_by": self.released_by,
               "unregistered_upstream": self.unregistered_upstream}
        if self.pii_columns:  # カラム単位のリネージが無ければ従来と同じ結果にする
            out["pii_columns"] = self.pii_columns
        if self.pii_excluded:
            out["pii_excluded"] = self.pii_excluded
        return out


def compute(db: Session, asset: DataAsset) -> Inherited:
    """上流を辿って実効値を計算する。anonymized の資産の手前で探索を打ち切る (その先は継承しない)。"""
    seen = {asset.qualified_name}
    queue = deque([asset.qualified_name])
    upstream: list[DataAsset] = []
    released, unregistered = [], []
    edges: set[tuple[str, str]] = set()
    while queue:
        name = queue.popleft()
        for source in db.scalars(select(LineageEdge.source).where(LineageEdge.target == name).order_by(LineageEdge.id)):
            edges.add((source, name))
            if source in seen:
                continue
            seen.add(source)
            found = db.scalar(select(DataAsset).where(DataAsset.qualified_name == source))
            if found is None:
                unregistered.append(source)  # 未登録の上流も、その先が登録済みかもしれないので辿る
                queue.append(source)
                continue
            upstream.append(found)
            if is_anonymized(found.privacy_technique, found.privacy_params):
                released.append(source)
            else:
                queue.append(source)
    top = max([level(asset.sensitivity), *[level(a.sensitivity) for a in upstream]])
    sens_from = sorted(a.qualified_name for a in upstream if level(a.sensitivity) == top and top > level(asset.sensitivity))
    pii_upstream = sorted((a for a in upstream if a.contains_pii), key=lambda a: a.qualified_name)
    pii_from, excluded, columns = _narrow_pii(db, asset, pii_upstream, edges)
    return Inherited(sensitivity=SENSITIVITY_LEVELS[top], contains_pii=bool(asset.contains_pii or pii_from),
                     sensitivity_from=sens_from, pii_from=pii_from, released_by=sorted(released),
                     unregistered_upstream=sorted(unregistered), pii_columns=columns, pii_excluded=excluded)


def _narrow_pii(db: Session, asset: DataAsset, pii_upstream: list[DataAsset],
                edges: set[tuple[str, str]]) -> tuple[list[str], list[dict], dict[str, list[dict[str, str]]]]:
    """PII の継承をカラム単位で決める (Phase 16)。

    機密度の継承はデータセット単位のまま。PII は、信頼できる生成元の辺だけを経由して PII のカラムからの
    DIRECT 経路が無い上流に限って外す (masking=true や AGGREGATION の経路では外さない)。
    """
    if not pii_upstream:
        return [], [], {}
    need = lineage_trust.backward(db, asset.qualified_name, edges)
    kept, excluded = [], []
    pii_cols = {a.qualified_name: [c["name"] for c in effective_pii_columns(a.schema_)] for a in pii_upstream}
    for a in pii_upstream:
        if need.reaches(a.qualified_name, pii_cols[a.qualified_name]):
            kept.append(a.qualified_name)
        else:
            producers = sorted({j for (src, _), pair in need.graph.items() if src == a.qualified_name for j in pair.jobs})
            excluded.append({"qualified_name": a.qualified_name, "reason": lineage_trust.REASON, "trusted_producers": producers})
    columns = lineage_trust.column_flow(need.graph, pii_cols, asset.qualified_name)
    return kept, excluded, columns


def is_violation(asset: DataAsset, inherited: Inherited) -> bool:
    return level(inherited.sensitivity) > level(asset.sensitivity) or (inherited.contains_pii and not asset.contains_pii)


def _store(asset: DataAsset, inherited: Inherited | None) -> bool:
    new = (inherited.sensitivity, inherited.contains_pii, inherited.view()) if inherited else (None, None, {})
    old = (asset.effective_sensitivity, asset.effective_contains_pii, asset.inheritance or {})
    asset.effective_sensitivity, asset.effective_contains_pii, asset.inheritance = new
    return new != old


def apply(db: Session, actor: str, asset: DataAsset) -> Inherited | None:
    """実効値を保存する。宣言値を上回ったら Issue を起票し、Certified なら再認証を求める。"""
    if asset.data_class != AI:
        if _store(asset, None):
            audit.record(db, actor, "ai.inheritance.recompute", "asset", asset.id, {"effective": None})
        return None
    inherited = compute(db, asset)
    changed = _store(asset, inherited)
    violation = is_violation(asset, inherited)
    if changed:
        payload = {"effective_sensitivity": inherited.sensitivity, "effective_contains_pii": inherited.contains_pii,
                   "violation": violation, **inherited.view()}
        if violation and asset.certification == Certification.CERTIFIED.value:
            asset.certification = Certification.DRAFT.value
            payload["certification_reset"] = True
        audit.record(db, actor, "ai.inheritance.recompute", "asset", asset.id, payload)
    if violation:
        issues.open_issue(
            db, actor, asset=asset, kind="policy", severity="critical", source_ref=f"policy:{INHERITANCE_POLICY}",
            level=EscalationLevel.OWNER,
            title=f"[POLICY] {asset.qualified_name}: {INHERITANCE_POLICY} 実効の統制属性が宣言値を上回っています",
            description=f"宣言: {asset.sensitivity} / PII={asset.contains_pii}。実効: {inherited.sensitivity} / "
                        f"PII={inherited.contains_pii}。機密度の宣言を上げるか、上流で PRV-003 を満たす匿名化を行ってください。")
    db.flush()
    return inherited


def refresh(db: Session, actor: str, names: Iterable[str]) -> None:
    """起点の資産とその下流の AI 資産を再計算する (機密度・PII・Lineage が変わったときに呼ぶ)。"""
    targets = set(names)
    for name in list(targets):
        targets.update(edge["to"] for edge in lineage.traverse(db, name, "downstream", MAX_DEPTH))
    db.flush()
    for asset in db.scalars(select(DataAsset).where(DataAsset.qualified_name.in_(targets)).order_by(DataAsset.qualified_name)):
        if asset.data_class == AI or asset.effective_sensitivity is not None:
            apply(db, actor, asset)


def recompute_all(db: Session, actor: str) -> dict:
    """全 AI 資産を再計算する (既存データの移行や Lineage の修正の後に使う)。"""
    assets = db.scalars(select(DataAsset).where(DataAsset.data_class == AI, DataAsset.lifecycle_state != "deleted")
                        .order_by(DataAsset.qualified_name)).all()
    violations = [a.qualified_name for a in assets if (i := apply(db, actor, a)) and is_violation(a, i)]
    return {"assets": len(assets), "violations": violations}


def check_inheritance(db: Session, asset: DataAsset) -> tuple[bool | None, str]:
    """AI-002。保存値ではなく、その時点の Lineage から計算する。"""
    inherited = compute(db, asset)
    if not is_violation(asset, inherited):
        return True, "ok"
    return False, (f"実効値 ({inherited.sensitivity}, PII={inherited.contains_pii}) が宣言値 "
                   f"({asset.sensitivity}, PII={asset.contains_pii}) を上回っています: "
                   f"{sorted({*inherited.sensitivity_from, *inherited.pii_from})}")


def constituents(db: Session, asset: DataAsset) -> tuple[list[DataAsset], list[str]]:
    """RAG コーパスの構成元。Embedding やベクトルは派生物なので透過し、その元のデータ資産を返す。

    データ資産の先の上流は辿らない (Certified の資産の中身の品質は、その資産の Certification が担保する)。
    """
    found: dict[str, DataAsset] = {}
    unregistered: set[str] = set()
    seen = {asset.qualified_name}
    queue = deque([asset.qualified_name])
    while queue:
        name = queue.popleft()
        for source in db.scalars(select(LineageEdge.source).where(LineageEdge.target == name)):
            if source in seen:
                continue
            seen.add(source)
            row = db.scalar(select(DataAsset).where(DataAsset.qualified_name == source))
            if row is None:
                unregistered.add(source)
            elif row.data_class == AI:
                queue.append(source)
            else:
                found[source] = row
    return [found[k] for k in sorted(found)], sorted(unregistered)


def check_rag_sources(db: Session, asset: DataAsset) -> tuple[bool | None, str]:
    """AI-003。構成元が不明・未登録・非 Certified なら不合格。"""
    sources, unregistered = constituents(db, asset)
    bad = [s.qualified_name for s in sources if s.certification != Certification.CERTIFIED.value]
    if unregistered:
        return False, f"Catalog に未登録の構成元があります: {unregistered}"
    if not sources:
        return False, "構成元が Lineage に登録されていません"
    return (False, f"Certified でない構成元があります: {bad}") if bad else (True, "ok")


def check_embedding_protected(db: Session, asset: DataAsset) -> tuple[bool | None, str]:
    """AI-004。§14 Embedding Data: PII を含む元からの生成は仮名化後に限定する。上流に PII が無ければ対象外。"""
    if not compute(db, asset).contains_pii:
        return None, "対象外"
    if asset.privacy_technique in PROTECTED_TECHNIQUES:
        return True, "ok"
    return False, f"構成元が PII を含むのに privacy_technique が {asset.privacy_technique} です (pseudonymized 以上が必要)"
