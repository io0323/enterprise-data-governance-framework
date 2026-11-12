"""負荷試験のシナリオ。EDGF_PERF_SCENARIO で 1 つだけ実行する (混ぜると原因を切り分けられないため)。"""
from __future__ import annotations

import itertools
import json
import os
import random
import uuid
from pathlib import Path

from locust import HttpUser, constant, task

API = "/api/v1"
MANIFEST = json.loads(Path(os.environ.get("EDGF_PERF_MANIFEST", Path(__file__).parent / "manifest.json")).read_text(encoding="utf-8"))
SCENARIO = os.environ.get("EDGF_PERF_SCENARIO", "search")
WORDS = ["orders", "customers", "invoices", "shipments", "payments", "inventory", "returns", "sessions", "campaigns", "contracts"]
_counter = itertools.count()


def _headers(actor: str, roles: str = "", clearance: str | None = None) -> dict:
    headers = {"X-Actor": actor, "X-Roles": roles, "X-Domains": ""}
    if clearance:
        headers["X-Clearance"] = clearance
    return headers


CONSUMER = _headers("perf-consumer", "consumer", "internal")


def search(user):
    user.client.get(f"{API}/assets", params={"q": random.choice(WORDS), "limit": 25}, headers=CONSUMER, name="search")


def asset_detail(user):
    user.client.get(f"{API}/assets/{random.choice(MANIFEST['sample_asset_ids'])}", headers=_headers("perf-committee", "committee"), name="asset_detail")


def impact(user):
    user.client.get(f"{API}/lineage/{MANIFEST['impact_root']}/impact", params={"depth": 10}, headers=CONSUMER, name="impact")


def column_impact(user):
    """カラム単位の影響分析 (深さ 10。Phase 16)。"""
    user.client.get(f"{API}/lineage/{MANIFEST['impact_root']}/columns/id/impact", params={"depth": 10}, headers=CONSUMER, name="column_impact")


def rag_authorize(user):
    body = {"corpus_asset_id": MANIFEST["corpus_id"], "user": {"subject": "perf-consumer", "roles": ["consumer"]}, "hit_asset_ids": MANIFEST["rag_hits"]}
    with user.client.post(f"{API}/ai/rag/authorize", json=body, headers=_headers(MANIFEST["rag_service"]), name="rag_authorize", catch_response=True) as response:
        data = response.json() if response.ok else {}
        if response.ok and len(data["allowed_asset_ids"]) + len(data["denied"]) != 50:
            response.failure("hits are not fully judged")


def contract_check(user):
    body = {"schema": [{"name": "id", "type": "bigint", "nullable": False, "description": "ID"}]}
    user.client.post(f"{API}/mesh/contracts/{MANIFEST['contract_id']}/check", json=body, headers=_headers(MANIFEST["check_custodian"]), name="contract_check")


def inbox(user):
    with user.client.get(f"{API}/approvals/inbox", params={"limit": 25}, headers=_headers(MANIFEST["inbox_owner"], "owner"), name="inbox", catch_response=True) as response:
        if response.ok and response.headers.get("X-Total-Count") != "1000":
            response.failure(f"expected 1000 items, got {response.headers.get('X-Total-Count')}")


def products(user):
    user.client.get(f"{API}/mesh/products/overview", params={"limit": 500}, headers=CONSUMER, name="products")


def audit_write(user):
    """監査の書き込みを伴う最小の状態変更。"""
    body = {"kind": "other", "title": f"perf {next(_counter)}"}
    user.client.post(f"{API}/issues", json=body, headers=_headers("perf-writer", "steward"), name="audit_write")


def asset_update(user):
    """検索の索引の対象の列 (名前、説明) の更新。pg_trgm の索引による書き込みの遅れを監視する。"""
    suffix = uuid.uuid4().hex[:12]
    body = {"name": f"perf {suffix}", "description": f"負荷試験の更新 {suffix}"}
    user.client.patch(f"{API}/assets/{random.choice(MANIFEST['sample_asset_ids'])}", json=body, headers=_headers("perf-admin", "admin"), name="asset_update")


def asset_register(user):
    """新規の登録。再計測はプロセスが分かれるので、名前の重複を避けるために UUID を使う。"""
    suffix = uuid.uuid4().hex
    body = {"qualified_name": f"d00.perf.{suffix}", "name": f"perf {suffix[:12]}", "description": f"負荷試験の登録 {suffix[:12]}", "domain": "d00",
            "data_class": "transactional", "sensitivity": "internal", "owner": "perf-admin", "tags": [random.choice(WORDS)]}
    user.client.post(f"{API}/assets", json=body, headers=_headers("perf-admin", "admin"), name="asset_register")


SCENARIOS = {f.__name__: f for f in (search, asset_detail, impact, rag_authorize, contract_check, inbox, products, audit_write, asset_update, asset_register, column_impact)}


class Scenario(HttpUser):
    wait_time = constant(0)

    @task
    def scenario(self):
        SCENARIOS[SCENARIO](self)
