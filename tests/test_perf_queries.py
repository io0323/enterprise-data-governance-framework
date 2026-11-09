"""Phase 14b: N+1 の再発の検出と、最適化の前後で結果が変わらないことの確認."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import event

from edgf.db import SessionLocal, engine
from edgf.models import DataProduct
from edgf.services import lineage, mesh_datsis
from tests.mesh_helpers import add_port, domain, port_asset, product, shippable
from tests.process_helpers import ADMIN, API, COMMITTEE, U, asset, future, inbox, request_access


@contextmanager
def count_queries():
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _edge(client, source, target):
    assert client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": source, "target": target}).status_code == 201


def _binary_tree(client, depth):
    nodes = 2 ** (depth + 1) - 1
    for i in range(1, nodes):
        _edge(client, f"n{(i - 1) // 2}", f"n{i}")
    return nodes


def test_traverse_queries_scale_with_depth_not_nodes(client):
    nodes = _binary_tree(client, 5)
    with SessionLocal() as db, count_queries() as statements:
        edges = lineage.traverse(db, "n0", "downstream", 10)
    assert len(edges) == nodes - 1 and len(statements) <= 7


def test_traverse_keeps_order_and_handles_cycles(client):
    for s, t in (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "a")):
        _edge(client, s, t)
    with SessionLocal() as db:
        edges = lineage.traverse(db, "a", "downstream", 10)
        limited = lineage.traverse(db, "a", "downstream", 1)
        up = lineage.traverse(db, "d", "upstream", 1)
    # 幅優先で、同じ段の中は登録順。循環しても止まり、各ノードの出力エッジは 1 回だけ
    assert [(e["from"], e["to"], e["depth"]) for e in edges] == [("a", "b", 1), ("a", "c", 1), ("b", "d", 2), ("c", "d", 2), ("d", "a", 3)]
    assert [e["to"] for e in limited] == ["b", "c"] and {e["from"] for e in up} == {"b", "c"}


def test_traverse_beyond_chunk_size(client, monkeypatch):
    monkeypatch.setattr(lineage, "TRAVERSE_CHUNK", 2)
    nodes = _binary_tree(client, 3)
    with SessionLocal() as db:
        assert len(lineage.traverse(db, "n0", "downstream", 10)) == nodes - 1


def test_impact_redaction_uses_few_queries(client):
    root = asset(client, "root.a")
    for i in range(30):
        asset(client, f"down.a{i}", sensitivity="restricted" if i % 3 == 0 else "internal")
        _edge(client, root["qualified_name"], f"down.a{i}")
    reader = U("reader", clearance="internal")
    with count_queries() as statements:
        r = client.get(f"{API}/lineage/root.a/impact", headers=reader)
    assert r.status_code == 200 and len(statements) < 15
    assert sum(1 for i in r.json()["impacted"] if i.get("redacted")) == 10


def test_inbox_queries_do_not_grow_with_items(client):
    for i in range(20):
        request_access(client, asset(client, f"s.a{i}", owner="many")["id"])
    with count_queries() as statements:
        items = inbox(client, U("many"))
    assert len(items) == 20 and len(statements) < 15


def test_inbox_excludes_already_decided_and_custodian(client):
    a = asset(client, "s.q")
    req = request_access(client, a["id"], type_name="external_share", recipient="取引先A")
    assert client.post(f"{API}/approvals/{req['id']}/decision", headers=U("asset-owner", "committee"), json={"approve": True}).status_code == 200
    # Owner として判断した本人には、次の Committee のステップも出さない
    assert req["id"] not in {i["id"] for i in inbox(client, U("asset-owner", "committee"))}
    assert req["id"] in {i["id"] for i in inbox(client, COMMITTEE)}
    assert req["id"] not in {i["id"] for i in inbox(client, U("asset-custodian", "committee"))}


def test_products_overview_matches_individual_evaluation(client):
    """一括読み込みでも、個別の判定と同じ結果になる。"""
    good = shippable(client)
    domain(client, "bi", owner="bi-owner")
    product(client, "no-ports", domain_name="bi", owner="bi-owner")
    bad = shippable(client, qn="sales.second", quality_sla={})
    other = port_asset(client, "sales.nocontract")
    add_port(client, good["pid"], other["id"], name="extra")
    assert client.post(f"{API}/policies/exceptions", headers=U("x", "owner", "sales"), json={
        "policy_id": "SEC-002", "asset_id": bad["aid"], "reason": "移行中", "expires_at": future()}).status_code == 201
    overview = client.get(f"{API}/mesh/products/overview", headers=COMMITTEE).json()
    assert len(overview) == 3
    for row in overview:
        single = client.get(f"{API}/mesh/products/{row['id']}/datsis", headers=COMMITTEE).json()
        assert row["datsis_passed"] == single["publishable"]
        assert row["datsis_failed"] == sorted(r["policy_id"] for r in single["results"] if r["status"] == "fail")
    assert next(r for r in overview if r["id"] == bad["pid"])["datsis_failed"] == ["MESH-003"]


def test_products_overview_queries_do_not_grow(client):
    domain(client)
    for i in range(12):
        p = product(client, f"p{i}")
        add_port(client, p["id"], port_asset(client, f"sales.p{i}")["id"])
    with count_queries() as statements:
        assert len(client.get(f"{API}/mesh/products/overview", headers=COMMITTEE).json()) == 12
    assert len(statements) < 40


def test_snapshot_does_not_outlive_the_overview(client):
    domain(client)
    product(client)
    with SessionLocal() as db:
        products = db.query(DataProduct).all()
        assert [p.name for p, *_ in mesh_datsis.overview(db, products)] == ["orders"]
        assert mesh_datsis.SNAPSHOT_KEY not in db.info


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="advisory lock は PostgreSQL だけ")
def test_chain_lock_is_used_on_postgres(client):
    with count_queries() as statements:
        assert client.post(f"{API}/issues", headers=ADMIN, json={"kind": "other", "title": "t"}).status_code == 201
    assert any("pg_advisory_xact_lock" in s for s in statements)
