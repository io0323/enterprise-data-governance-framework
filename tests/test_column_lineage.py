"""Phase 16a: カラム単位のリネージの取り込み (columnLineage facet と手動の登録)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from edgf.db import SessionLocal
from edgf.models import ColumnLineageEdge, LineageEdge
from edgf_client import column_lineage_facet, input_field, openlineage_event
from edgf_client.lineage import SCHEMA_URL
from tests.conftest import dump_database
from tests.process_helpers import ADMIN, API, U, asset

OL = f"{API}/lineage/openlineage"
ETL = U("etl", "custodian")


def post(client, event, who=ETL, code=202):
    r = client.post(OL, headers=who, json=event)
    assert r.status_code == code, r.text
    return r.json()


def event(facets=None, inputs=(("crm", "crm.customers"),), outputs=(("dw", "dw.orders"),), event_type="COMPLETE"):
    return openlineage_event("etl", "load_orders", str(uuid.uuid4()), inputs, outputs, column_lineage=facets, event_type=event_type)


def column_edges(**filters):
    with SessionLocal() as db:
        stmt = select(ColumnLineageEdge).order_by(ColumnLineageEdge.id)
        for k, v in filters.items():
            stmt = stmt.where(getattr(ColumnLineageEdge, k) == v)
        return [(e.source, e.source_column, e.target, e.target_column, e.job, e.type, e.subtype, e.masking) for e in db.scalars(stmt)]


def dataset_edges():
    with SessionLocal() as db:
        return sorted((e.source, e.target, e.job) for e in db.scalars(select(LineageEdge)))


# ---------------------------------------------------------------- 取り込み
def test_facet_edges_are_stored(client):
    facet = column_lineage_facet({
        "customer_id": [input_field("crm", "crm.customers", "id", subtype="IDENTITY")],
        "total": [input_field("crm", "crm.orders", "amount", subtype="AGGREGATION"),
                  input_field("crm", "crm.customers", "region", type="INDIRECT", subtype="GROUP_BY")],
        "email_hash": [input_field("crm", "crm.customers", "email", subtype="TRANSFORMATION", masking=True)],
    })
    result = post(client, event({"dw.orders": facet}))
    assert result["edges"] == 1 and result["column_edges"] == 4 and result["warnings"] == []
    assert column_edges() == [
        ("crm.customers", "id", "dw.orders", "customer_id", "etl/load_orders", "DIRECT", "IDENTITY", False),
        ("crm.orders", "amount", "dw.orders", "total", "etl/load_orders", "DIRECT", "AGGREGATION", False),
        ("crm.customers", "region", "dw.orders", "total", "etl/load_orders", "INDIRECT", "GROUP_BY", False),
        ("crm.customers", "email", "dw.orders", "email_hash", "etl/load_orders", "DIRECT", "TRANSFORMATION", True),
    ]
    # inputs に無い crm.orders も、データセット単位の辺を作る
    assert dataset_edges() == [("crm.customers", "dw.orders", "etl/load_orders"), ("crm.orders", "dw.orders", "etl/load_orders")]
    # 再送しても重複しない
    post(client, event({"dw.orders": facet}))
    assert len(column_edges()) == 4


def test_unknown_subtype_is_other_with_warning(client):
    facet = column_lineage_facet({"x": [input_field("a", "src.t", "c", subtype="QUANTUM_MERGE")]})
    result = post(client, event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    assert column_edges()[0][6] == "OTHER"
    assert result["warnings"] == [{"code": "unknown_subtype", "where": "fields.x.inputFields[0].transformations[0]", "value": "QUANTUM_MERGE"}]


def test_unknown_type_is_treated_as_direct_with_warning(client):
    facet = column_lineage_facet({"x": [input_field("a", "src.t", "c", type="SEMANTIC")]})
    result = post(client, event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    assert column_edges()[0][5] == "DIRECT" and result["warnings"][0]["code"] == "unknown_type"


def test_deprecated_transformation_type_only_without_transformations(client):
    facet = {"_schemaURL": SCHEMA_URL, "fields": {
        "legacy_direct": {"inputFields": [{"namespace": "a", "name": "src.t", "field": "c1"}], "transformationType": "IDENTITY"},
        "legacy_indirect": {"inputFields": [{"namespace": "a", "name": "src.t", "field": "c2"}], "transformationType": "INDIRECT"},
        "legacy_masked": {"inputFields": [{"namespace": "a", "name": "src.t", "field": "c3"}], "transformationType": "MASKED"},
        "new_wins": {"inputFields": [{"namespace": "a", "name": "src.t", "field": "c4", "transformations": [{"type": "INDIRECT", "subtype": "FILTER"}]}],
                     "transformationType": "IDENTITY"},
        "nothing": {"inputFields": [{"namespace": "a", "name": "src.t", "field": "c5"}]},
    }}
    post(client, event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    by_target = {e[3]: e[5:] for e in column_edges()}
    assert by_target == {"legacy_direct": ("DIRECT", "IDENTITY", False), "legacy_indirect": ("INDIRECT", None, False),
                         "legacy_masked": ("DIRECT", "TRANSFORMATION", True), "new_wins": ("INDIRECT", "FILTER", False),
                         "nothing": ("DIRECT", None, False)}


def test_dataset_array_is_dataset_level_indirect(client):
    facet = column_lineage_facet({}, dataset=[input_field("a", "src.t", "status", subtype="FILTER"), input_field("a", "src.t", "region", type="DIRECT")])
    post(client, event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    assert column_edges() == [("src.t", "status", "dw.orders", None, "etl/load_orders", "INDIRECT", "FILTER", False),
                              ("src.t", "region", "dw.orders", None, "etl/load_orders", "INDIRECT", None, False)]


def test_unregistered_columns_are_warned_not_rejected(client):
    asset(client, "crm.customers", schema=[{"name": "id", "type": "bigint"}])
    facet = column_lineage_facet({"customer_id": [input_field("crm", "crm.customers", "id"), input_field("crm", "crm.customers", "ghost")]})
    result = post(client, event({"dw.orders": facet}))
    assert result["column_edges"] == 2
    assert result["warnings"] == [{"code": "unregistered_column", "asset": "crm.customers", "column": "ghost"}]  # dw.orders は未登録の資産なので対象外


@pytest.mark.parametrize("url", ["https://openlineage.io/spec/facets/2-0-0/ColumnLineageDatasetFacet.json", "", None,
                                 "https://example.test/ColumnLineageDatasetFacet.json"])
def test_only_major_version_one_is_accepted(client, url):
    facet = column_lineage_facet({"x": [input_field("a", "src.t", "c")]})
    facet["_schemaURL"] = url
    r = client.post(OL, headers=ETL, json=event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:column-lineage-invalid"
    assert column_edges() == [] and dataset_edges() == []  # 一部だけ取り込まない


@pytest.mark.parametrize("facet", [
    {"_schemaURL": SCHEMA_URL, "fields": []}, {"_schemaURL": SCHEMA_URL, "fields": {"x": {"inputFields": "a"}}},
    {"_schemaURL": SCHEMA_URL, "fields": {"x": {"inputFields": [{"name": "a.b"}]}}},
    {"_schemaURL": SCHEMA_URL, "fields": {"x": {"inputFields": [{"name": "a.b", "field": "c", "transformations": "DIRECT"}]}}},
    {"_schemaURL": SCHEMA_URL, "dataset": {"name": "a"}}, "not-an-object",
])
def test_malformed_facets_are_422(client, facet):
    r = client.post(OL, headers=ETL, json=event({"dw.orders": facet}))
    assert r.status_code == 422


def test_event_without_facet_is_unchanged(client):
    result = post(client, event())
    assert result == {"accepted": True, "edges": 1}
    assert dataset_edges() == [("crm.customers", "dw.orders", "etl/load_orders")] and column_edges() == []
    assert post(client, event(event_type="START")) == {"accepted": True, "edges": 0, "note": "START はエッジ確定対象外"}


def test_facet_on_non_complete_event_stores_nothing(client):
    facet = column_lineage_facet({"x": [input_field("a", "src.t", "c")]})
    post(client, event({"dw.orders": facet}, event_type="RUNNING"))
    assert column_edges() == []


def test_free_text_descriptions_leave_no_trace(client, edgf_logs):
    marker = "zzsqlliteral-secret@example.com"
    facet = {"_schemaURL": SCHEMA_URL, "fields": {"x": {
        "inputFields": [{"namespace": "a", "name": "src.t", "field": "c",
                         "transformations": [{"type": "DIRECT", "subtype": "TRANSFORMATION", "description": f"CASE WHEN email = '{marker}' THEN 1 END"}]}],
        "transformationDescription": f"SELECT * FROM t WHERE email = '{marker}'"}}}
    result = post(client, event({"dw.orders": facet}, inputs=(("a", "src.t"),)))
    assert result["column_edges"] == 1 and marker not in str(result)
    audit = client.get(f"{API}/audit/events", headers=ADMIN, params={"limit": 1000}).text.lower()
    for haystack in (dump_database(), audit, edgf_logs.text()):
        assert marker.lower() not in haystack


def test_facet_requires_column_write(client):
    facet = column_lineage_facet({"x": [input_field("a", "src.t", "c")]})
    assert client.post(OL, headers=U("reader"), json=event({"dw.orders": facet})).status_code == 403


# ---------------------------------------------------------------- 手動の登録
def test_manual_column_edge(client):
    asset(client, "src.t", schema=[{"name": "c", "type": "int"}])
    r = client.post(f"{API}/lineage/column-edges", headers=ETL, json={"source": "src.t", "source_column": "c", "target": "dst.t", "target_column": "d",
                                                                       "subtype": "identity"})
    assert r.status_code == 201 and r.json()["type"] == "DIRECT" and r.json()["subtype"] == "IDENTITY" and r.json()["warnings"] == []
    whole = client.post(f"{API}/lineage/column-edges", headers=ETL, json={"source": "src.t", "source_column": "c", "target": "dst.t", "type": "DIRECT"})
    assert whole.json()["type"] == "INDIRECT" and whole.json()["target_column"] is None  # データセット全体は INDIRECT
    assert dataset_edges() == [("src.t", "dst.t", "manual")]
    events = client.get(f"{API}/audit/events", headers=ADMIN, params={"action": "lineage.column_edge"}).json()
    assert len(events) == 2


@pytest.mark.parametrize("body, code", [
    ({"source": "a.b", "source_column": "c", "target": "a.b", "target_column": "d"}, 422),
    ({"source": "a.b", "source_column": "c", "target": "x.y", "type": "LATERAL"}, 422),
    ({"source": "a.b", "source_column": "c", "target": "x.y", "transformationDescription": "x"}, 422),
])
def test_manual_edge_validation(client, body, code):
    assert client.post(f"{API}/lineage/column-edges", headers=ETL, json=body).status_code == code


def test_manual_edge_requires_permission(client):
    body = {"source": "a.b", "source_column": "c", "target": "x.y", "target_column": "d"}
    assert client.post(f"{API}/lineage/column-edges", headers=U("reader"), json=body).status_code == 403
