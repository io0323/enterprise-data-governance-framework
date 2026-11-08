from __future__ import annotations

import os

os.environ["EDGF_DATABASE_URL"] = "sqlite://"
# 既存テストは dev モード (X-Actor ヘッダ) で実行する。JWT 経路は tests/test_auth.py で検証する
os.environ["EDGF_AUTH_MODE"] = "dev"
os.environ.setdefault("EDGF_ENV", "test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from edgf import migrate
from edgf.db import Base, engine
from edgf.main import create_app


def reset_db() -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture()
def client():
    # スキーマは本番と同じ Alembic マイグレーションで構築する (create_all は使わない)
    reset_db()
    migrate.upgrade(engine)
    # 既定ヘッダ = MVP 時代の「誰でも全操作可」に相当する dev 操作者 (個別リクエストの H(...) で上書き)
    with TestClient(create_app(), headers=H()) as c:
        yield c
    reset_db()


ALL_ROLES = "admin,committee,owner,steward,custodian"


def H(actor: str = "alice", roles: str = ALL_ROLES, domains: str = "*") -> dict:
    return {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}


@pytest.fixture()
def sales_asset(client):
    client.post("/api/v1/domains", json={"name": "sales", "owner": "sales-head"}, headers=H())
    r = client.post("/api/v1/assets", headers=H(), json={
        "qualified_name": "sales.orders", "name": "受注", "description": "受注トランザクション",
        "domain": "sales", "data_class": "transactional", "sensitivity": "confidential",
        "owner": "owner1", "steward": "steward1", "is_cde": True, "freshness_sla_hours": 24,
        "retention_days": 2555,
        "schema": [{"name": "order_id", "type": "string"}, {"name": "email", "type": "string", "pii": True}],
        "controls": ["encryption_at_rest", "encryption_in_transit", "masking"],
    })
    assert r.status_code == 201, r.text
    return r.json()
