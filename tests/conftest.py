from __future__ import annotations

import os

os.environ["EDGF_DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient

from edgf.db import Base, engine
from edgf.main import create_app


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    with TestClient(create_app()) as c:
        yield c
    Base.metadata.drop_all(engine)


def H(actor: str = "alice") -> dict:
    return {"X-Actor": actor}


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
