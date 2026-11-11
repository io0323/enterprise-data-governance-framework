"""Phase 15e: MDM Hub の参照実装 (examples/mdm-hub) の E2E。match → compose → verify → 消去の完了まで."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.data_plane_e2e.conftest import HUB, HUB_SECRET

ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_NO = "C-HUB-0001"
NAME = "参照実装太郎ZZHUB"


def _hub_module():
    spec = importlib.util.spec_from_file_location("mdm_hub_app", ROOT / "examples" / "mdm-hub" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass が自分のモジュールを参照するため
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hub(idp, server, as_user, tmp_path_factory):
    from edgf_client import ClientCredentials, EdgfClient
    admin = as_user("root", ["admin"])
    assert admin.post("/domains", json={"name": "master", "owner": "own"}).status_code == 201
    asset = admin.post("/assets", json={"qualified_name": "mdm.party", "name": "顧客マスタ", "description": "d", "domain": "master",
                                        "data_class": "master", "sensitivity": "confidential", "owner": "own", "steward": "stw", "custodian": HUB})
    assert asset.status_code == 201, asset.text
    module = _hub_module()
    client = EdgfClient(server["url"], ClientCredentials(idp.token_endpoint, HUB, HUB_SECRET))
    app = module.create_app(module.HubSettings(database=str(tmp_path_factory.mktemp("hub") / "hub.db")), client)
    with TestClient(app) as http:
        yield {"http": http, "hub": app.state.hub, "admin": admin, "committee": as_user("chair", ["committee"]), "server": server}


def test_match_compose_verify_then_erasure(hub):
    http = hub["http"]
    first = http.post("/records", json={"source_system": "crm", "key": "hub-c1", "attributes": {"customer_no": CUSTOMER_NO, "full_name": NAME}})
    assert first.status_code == 200 and first.json()["decision"] == "new" and first.json()["verified"] is True
    golden_id = first.json()["golden_id"]
    second = http.post("/records", json={"source_system": "erp", "key": "hub-e1", "attributes": {"customer_no": CUSTOMER_NO, "full_name": NAME + "別表記"}})
    assert second.json()["decision"] == "linked" and second.json()["golden_id"] == golden_id
    assert second.json()["version"] > first.json()["version"] and second.json()["verified"] is True
    held = http.get(f"/golden/{golden_id}").json()
    assert held["attributes"]["full_name"] == NAME and held["verified"] is True  # crm の信頼度が高い
    # Control Plane には値が無い (由来と内容ハッシュだけ)
    record = hub["admin"].get(f"/mdm/party/golden-records/{golden_id}").json()
    assert NAME not in str(record) and record["version"]["content_hash"]

    # 消去: Control Plane で受付 → 対象の特定 → 承認 → 発行。Hub がタスクを処理する
    committee = hub["committee"]
    request = hub["admin"].post("/privacy/erasure-requests", json={"identifier_type": "customer_id", "identifier": CUSTOMER_NO})
    assert request.status_code == 201, request.text
    rid = request.json()["id"]
    scoped = committee.post(f"/privacy/erasure-requests/{rid}/scope").json()
    assert [t["qualified_name"] for t in scoped["tasks"]] == ["mdm.party"] and scoped["tasks"][0]["golden_ids"] == [golden_id]
    for action in ("approve", "dispatch"):
        assert committee.post(f"/privacy/erasure-requests/{rid}/{action}").status_code == 200
    assert http.post("/erasure/run").json() == {"completed": 1}
    assert http.get(f"/golden/{golden_id}").status_code == 404  # Hub の値は物理削除された
    detail = committee.get(f"/privacy/erasure-requests/{rid}").json()
    assert detail["status"] == "completed" and detail["tasks"][0]["status"] == "completed" and detail["tasks"][0]["records_deleted"] == 3
    assert hub["admin"].get(f"/mdm/party/golden-records/{golden_id}").json()["status"] == "tombstoned"
    assert http.post("/erasure/run").json() == {"completed": 0}  # 処理済みのタスクは繰り返さない


def test_values_never_reach_the_control_plane_database(hub):
    import sqlite3
    with sqlite3.connect(hub["server"]["db"]) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        dump = "\n".join(str(row) for t in tables for row in conn.execute(f'SELECT * FROM "{t}"'))  # nosec B608 - sqlite_master の表名
    assert NAME not in dump and CUSTOMER_NO not in dump
