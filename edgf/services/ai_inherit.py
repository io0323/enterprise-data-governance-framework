"""§14 Embedding / Vector / RAG: 統制属性の継承 (Lineage の上流を辿って実効値を決める).

実効機密度 = 自身と全上流の最大値、実効 contains_pii = 自身と全上流の論理和。
継承が解除されるのは、上流で PRV-003 を満たす anonymized を経た場合だけ (仮名化では解除しない。§12)。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.auth import SENSITIVITY_LEVELS
from edgf.models import DataAsset, LineageEdge
from edgf.services.privacy import is_anonymized


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

    def view(self) -> dict:
        return {"sensitivity_from": self.sensitivity_from, "pii_from": self.pii_from, "released_by": self.released_by,
                "unregistered_upstream": self.unregistered_upstream}


def compute(db: Session, asset: DataAsset) -> Inherited:
    """上流を辿って実効値を計算する。anonymized の資産の手前で探索を打ち切る (その先は継承しない)。"""
    seen = {asset.qualified_name}
    queue = deque([asset.qualified_name])
    upstream: list[DataAsset] = []
    released, unregistered = [], []
    while queue:
        name = queue.popleft()
        for source in db.scalars(select(LineageEdge.source).where(LineageEdge.target == name).order_by(LineageEdge.id)):
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
    pii_from = sorted(a.qualified_name for a in upstream if a.contains_pii)
    return Inherited(sensitivity=SENSITIVITY_LEVELS[top], contains_pii=bool(asset.contains_pii or pii_from),
                     sensitivity_from=sens_from, pii_from=pii_from, released_by=sorted(released),
                     unregistered_upstream=sorted(unregistered))
