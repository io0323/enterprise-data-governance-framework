"""Common FastAPI dependencies."""
from __future__ import annotations

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from edgf.models import DataAsset


def get_actor(x_actor: str = Header(default="anonymous", description="操作者ID (MVP: 本番はOIDC等のIdPトークンから導出)")) -> str:
    return x_actor


def get_asset_or_404(db: Session, asset_id: str) -> DataAsset:
    asset = db.get(DataAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return asset
