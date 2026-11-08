"""§14 RAG Data: 検索時のアクセス制御 (利用者の権限を検索結果に反映する).

ステートレスな判定で、何も保存しない。検索語やチャンクの本文は受け取らない (§18.2)。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal, normalize_roles, resolve_clearance
from edgf.models import DataAsset
from edgf.services import lineage

MAX_DEPTH = 50
DELETED = "deleted"


def _denied_reason(asset: DataAsset) -> str:
    return "restricted_not_granted" if authz.effective_sensitivity(asset) == authz.RESTRICTED else "clearance"


def authorize(db: Session, corpus: DataAsset, *, subject: str, roles: Sequence[str], clearance: str | None,
              hit_asset_ids: Sequence[str]) -> dict[str, Any]:
    """ABAC を通過した資産だけを allowed に返す。判定はコーパスではなくヒットした資産ごとに行う。

    コーパスの構成元でない資産は除外する (fail-close)。RAG サービスの不具合で別の資産が混ざっても通さない。
    """
    normalized = normalize_roles(roles)
    user = Principal(subject=subject, roles=normalized, clearance=resolve_clearance(clearance, normalized),
                     domains=frozenset(), auth_mode="service")
    ids = list(dict.fromkeys(hit_asset_ids))
    assets = {a.id: a for a in db.scalars(select(DataAsset).where(DataAsset.id.in_(ids)))} if ids else {}
    upstream = {e["from"] for e in lineage.traverse(db, corpus.qualified_name, "upstream", MAX_DEPTH)}
    allowed: list[str] = []
    denied: list[dict[str, str]] = []
    for asset_id in ids:
        asset = assets.get(asset_id)
        if asset is None:
            reason = "unknown_asset"
        elif asset.qualified_name not in upstream:
            reason = "not_in_corpus"
        elif asset.lifecycle_state == DELETED:
            reason = "deleted"
        elif authz.can_read(db, user, asset):
            allowed.append(asset_id)
            continue
        else:
            reason = _denied_reason(asset)
        denied.append({"asset_id": asset_id, "reason": reason})
    return {"corpus_asset_id": corpus.id, "allowed_asset_ids": allowed, "denied": denied}
