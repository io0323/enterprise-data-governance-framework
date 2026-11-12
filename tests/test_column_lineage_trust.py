"""Phase 16c: 信頼できる生成元と、カラム単位のリネージの継承・消去への適用."""
from __future__ import annotations

import uuid

import pytest
import yaml

from edgf.db import SessionLocal
from edgf.services import lineage_trust
from edgf_client import column_lineage_facet, input_field, openlineage_event
from tests.ai_helpers import (ADMIN, API, COMMITTEE, ai_asset, approve_dataset, asset, dataset_version, edge,
                              run_quality)
from tests.process_helpers import audit_actions, decide

PRIV = f"{API}/privacy"
POL = f"{API}/policies"
OL = f"{API}/lineage/openlineage"
ETL = {"X-Actor": "etl", "X-Roles": "custodian", "X-Domains": ""}
OTHER = {"X-Actor": "intruder", "X-Roles": "custodian", "X-Domains": ""}
RAW = "Column.Trust.Person@Example.com"
SRC_SCHEMA = [
    {"name": "email", "pii": True, "identifier_type": "email", "pii_category": "contact", "processing_purposes": ["ai_training"],
     "legal_basis": "consent"},
    {"name": "name", "pii": True, "pii_category": "identifier", "processing_purposes": ["ai_training"], "legal_basis": "consent"},
    {"name": "region"},
]
TRUST = {"producers": [{"job_namespace": "etl", "jobs": ["agg"], "principals": ["dev:etl"], "reason": "テスト"}]}


# ---------------------------------------------------------------- 部品
def trust(client, doc=TRUST, publish=True) -> dict:
    """信頼できる生成元を Policy Bundle で提案し、Committee の承認と公開を経て有効にする。"""
    r = client.post(f"{POL}/bundles", headers=COMMITTEE,
                    json={"changes": {lineage_trust.TRUST_FILE: yaml.safe_dump(doc)}, "reason": "dbt を信頼できる生成元にする"})
    assert r.status_code == 202, r.text
    proposal = r.json()
    if publish:
        decide(client, proposal["approval_id"], {"X-Actor": "vice-chair", "X-Roles": "committee", "X-Domains": ""})
        assert client.post(f"{POL}/bundles/{proposal['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    return proposal


def facet_event(client, fields, *, job="agg", output="bi.agg", who=ETL) -> dict:
    event = openlineage_event("etl", job, str(uuid.uuid4()), [("crm", "crm.people")], [("bi", output)],
                              column_lineage={output: column_lineage_facet(fields)})
    r = client.post(OL, headers=who, json=event)
    assert r.status_code == 202, r.text
    return r.json()


REGION_ONLY = {"region": [input_field("crm", "crm.people", "region", subtype="IDENTITY")],
               "cnt": [input_field("crm", "crm.people", "region", type="INDIRECT", subtype="GROUP_BY")]}


@pytest.fixture()
def world(client):
    """crm.people (email / name が PII) → bi.agg (地域ごとの件数)。bi.agg は PII を含むと保守的に宣言している。"""
    src = asset(client, "crm.people", data_class="master", sensitivity="confidential", contains_pii=True, retention_days=30,
                schema=SRC_SCHEMA, custodian="c1")
    agg = asset(client, "bi.agg", sensitivity="confidential", contains_pii=True, retention_days=30, custodian="c2", owner="agg-owner",
                schema=[{"name": "region"}, {"name": "cnt", "pii": True, "pii_category": "other", "processing_purposes": ["ai_training"],
                                             "legal_basis": "consent"}])
    return {"client": client, "src": src, "agg": agg}


def scoped(client) -> dict:
    r = client.post(f"{PRIV}/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": RAW})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.post(f"{PRIV}/erasure-requests/{rid}/scope", headers=COMMITTEE)
    assert r.status_code == 200, r.text
    return r.json()


def targets(detail) -> set[str]:
    return {t["qualified_name"] for t in detail["tasks"]}


# ---------------------------------------------------------------- 信頼できる生成元の宣言
def test_trust_file_validation():
    assert lineage_trust.parse("producers: []") == ()
    [p] = lineage_trust.parse(yaml.safe_dump(TRUST))
    assert p.matches("etl/agg", "dev:etl") and not p.matches("etl/other", "dev:etl") and not p.matches("etl/agg", "dev:intruder")
    assert not p.matches("etlx/agg", "dev:etl")
    [all_jobs] = lineage_trust.parse("producers: [{job_namespace: dbt, principals: [svc]}]")
    assert all_jobs.matches("dbt/any", "svc")
    for bad in ("[1]", "producers: {}", "other: 1", "producers: [{job_namespace: dbt}]",
                "producers: [{job_namespace: dbt, principals: []}]", "producers: [{job_namespace: dbt/, principals: [a]}]",
                "producers: [{job_namespace: dbt, principals: [a], jobs: dbt}]", "producers: [{job_namespace: dbt, principals: [a], x: 1}]",
                "producers: [1]", ": : :"):
        with pytest.raises(lineage_trust.TrustFileInvalid):
            lineage_trust.parse(bad)


def test_trust_file_is_part_of_the_bundle_and_needs_committee(client):
    active = client.get(f"{POL}/bundles", headers=ADMIN, params={"status": "active"}).json()[0]
    files = client.get(f"{POL}/bundles/{active['version']}", headers=ADMIN).json()["files"]
    assert yaml.safe_load(files[lineage_trust.TRUST_FILE]) == {"producers": []}  # 既定は空 (どの辺も絞らない)
    proposal = trust(client, publish=False)
    req = client.get(f"{API}/approvals/{proposal['approval_id']}", headers=COMMITTEE).json()
    assert [s["name"] for s in req["steps"]] == ["committee"]
    # 主体の無い宣言は提案の時点で拒否する
    r = client.post(f"{POL}/bundles", headers=COMMITTEE, json={
        "changes": {lineage_trust.TRUST_FILE: "producers: [{job_namespace: etl}]"}, "reason": "x"})
    assert r.status_code == 422 and "principals" in r.text
    r = client.post(f"{POL}/bundles", headers=COMMITTEE, json={"changes": {"lineage/other.yaml": "producers: []"}, "reason": "x"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 消去 (確定事項 2)
def test_untrusted_edges_keep_dataset_level_scope(world):
    client = world["client"]
    facet_event(client, REGION_ONLY)
    detail = scoped(client)
    assert targets(detail) == {"crm.people", "bi.agg"} and detail["lineage_exclusions"] == []


def test_trusted_producer_without_pii_path_is_excluded_with_reason(world):
    client = world["client"]
    facet_event(client, REGION_ONLY)
    trust(client)
    detail = scoped(client)
    assert targets(detail) == {"crm.people"}
    [excluded] = detail["lineage_exclusions"]
    assert excluded == {"qualified_name": "bi.agg", "asset_id": world["agg"]["id"], "reason": "no_direct_path_from_pii_columns",
                        "via": ["crm.people"], "trusted_producers": ["etl/agg"]}
    rid = detail["id"]
    for action in ("approve", "dispatch"):
        assert client.post(f"{PRIV}/erasure-requests/{rid}/{action}", headers=COMMITTEE).status_code == 200
    [task] = detail["tasks"]
    assert client.post(f"{PRIV}/erasure-tasks/{task['id']}/ack", headers={"X-Actor": "c1"}).status_code == 200
    assert client.post(f"{PRIV}/erasure-tasks/{task['id']}/complete", headers={"X-Actor": "c1"},
                       json={"records_deleted": 1, "evidence_hash": "ab" * 32}).status_code == 200
    cert = client.get(f"{PRIV}/erasure-requests/{rid}/certificate", headers=COMMITTEE).json()
    assert cert["lineage_exclusions"][0]["reason"] == "no_direct_path_from_pii_columns"
    assert cert["lineage_exclusions"][0]["trusted_producers"] == ["etl/agg"]
    scope_event = [e for e in audit_actions(client) if e["action"] == "erasure.scope"]
    assert scope_event[-1]["payload"]["lineage_excluded"] == ["bi.agg"]


def test_aggregation_and_masking_paths_are_not_excluded(world):
    client = world["client"]
    facet_event(client, {"cnt": [input_field("crm", "crm.people", "email", subtype="AGGREGATION")],
                 "region": [input_field("crm", "crm.people", "name", subtype="TRANSFORMATION", masking=True)]})
    trust(client)
    detail = scoped(client)
    assert targets(detail) == {"crm.people", "bi.agg"} and detail["lineage_exclusions"] == []
    agg_task = next(t for t in detail["tasks"] if t["qualified_name"] == "bi.agg")
    # 広げる方向: 値の流れたカラム (宣言の無い region) も対象のカラムに加える
    assert agg_task["columns"] == ["cnt", "region"]


def test_unapproved_trust_change_is_not_reflected(world):
    client = world["client"]
    facet_event(client, REGION_ONLY)
    trust(client, publish=False)
    assert targets(scoped(client)) == {"crm.people", "bi.agg"}


@pytest.mark.parametrize("case", ["manual", "other_principal", "other_job", "facetless_job"])
def test_edges_not_from_the_trusted_producer_do_not_narrow(world, case):
    client = world["client"]
    trust(client)
    if case == "manual":  # 手動の登録は job の名前を名乗っても信頼しない
        r = client.post(f"{API}/lineage/column-edges", headers=ETL, json={
            "source": "crm.people", "source_column": "region", "target": "bi.agg", "target_column": "region", "job": "etl/agg"})
        assert r.status_code == 201, r.text
    elif case == "other_principal":
        facet_event(client, REGION_ONLY, who=OTHER)
    elif case == "other_job":
        facet_event(client, REGION_ONLY, job="adhoc")
    else:  # 信頼できる生成元の辺に、facet の無い別の job の辺が並ぶ
        facet_event(client, REGION_ONLY)
        edge(client, "crm.people", "bi.agg")
    detail = scoped(client)
    assert targets(detail) == {"crm.people", "bi.agg"} and detail["lineage_exclusions"] == []


def test_untrusted_rewrite_of_a_trusted_edge_restores_dataset_scope(world):
    client = world["client"]
    trust(client)
    facet_event(client, REGION_ONLY)
    assert targets(scoped(client)) == {"crm.people"}
    facet_event(client, REGION_ONLY, who=OTHER, job="agg")  # 同じ辺を別の主体が書き直す
    r = client.post(f"{PRIV}/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": RAW + "x"})
    rid = r.json()["id"]
    assert targets(client.post(f"{PRIV}/erasure-requests/{rid}/scope", headers=COMMITTEE).json()) == {"crm.people", "bi.agg"}


def test_downstream_of_an_excluded_asset_is_excluded_and_hidden_names_are_not_shown(world):
    client = world["client"]
    asset(client, "bi.report", sensitivity="restricted", contains_pii=True, retention_days=30, custodian="c3", owner="report-owner",
          schema=[{"name": "cnt", "pii": True}])
    edge(client, "bi.agg", "bi.report")  # 信頼できない辺でも、上流に PII が無ければ伝播しない
    facet_event(client, REGION_ONLY)
    trust(client)
    detail = scoped(client)
    assert targets(detail) == {"crm.people"}
    assert [e["qualified_name"] for e in detail["lineage_exclusions"]] == ["bi.agg", "bi.report"]
    assert detail["lineage_exclusions"][1]["via"] == ["bi.agg"]
    # restricted の資産を読めない Owner には、その除外を見せない
    who = {"X-Actor": "src-owner", "X-Roles": "owner", "X-Domains": ""}
    seen = client.get(f"{PRIV}/erasure-requests/{detail['id']}", headers=who)
    assert seen.status_code == 200, seen.text
    assert [e["qualified_name"] for e in seen.json()["lineage_exclusions"]] == ["bi.agg"] and "bi.report" not in seen.text


# ---------------------------------------------------------------- AI の版 (superseded にしない)
def test_ai_dataset_version_is_not_superseded_without_pii_path(world):
    client = world["client"]
    run_quality(client, world["agg"]["id"])
    train = ai_asset(client, "ai.train", "training_dataset", sensitivity="confidential")
    dv = dataset_version(client, train["id"], [world["agg"]["id"]], version="v1")
    approve_dataset(client, dv["id"], ("agg-owner",), committee=True)
    facet_event(client, REGION_ONLY)
    trust(client)
    detail = scoped(client)
    assert targets(detail) == {"crm.people"}
    assert {e["qualified_name"] for e in detail["lineage_exclusions"]} == {"bi.agg", "ai.train"}
    assert detail["ai_impact"]["dataset_version_ids"] == []
    assert detail["ai_impact"]["lineage_excluded_asset_ids"] == sorted([world["agg"]["id"], train["id"]])
    rid = detail["id"]
    for action in ("approve", "dispatch"):
        assert client.post(f"{PRIV}/erasure-requests/{rid}/{action}", headers=COMMITTEE).status_code == 200
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=ADMIN).json()["status"] != "superseded"


def test_ai_dataset_version_is_superseded_without_trust(world):
    client = world["client"]
    run_quality(client, world["agg"]["id"])
    train = ai_asset(client, "ai.train", "training_dataset", sensitivity="confidential")
    dv = dataset_version(client, train["id"], [world["agg"]["id"]], version="v1")
    approve_dataset(client, dv["id"], ("agg-owner",), committee=True)
    facet_event(client, REGION_ONLY)
    detail = scoped(client)
    assert dv["id"] in detail["ai_impact"]["dataset_version_ids"] and "lineage_excluded_asset_ids" not in detail["ai_impact"]
    rid = detail["id"]
    for action in ("approve", "dispatch"):
        assert client.post(f"{PRIV}/erasure-requests/{rid}/{action}", headers=COMMITTEE).status_code == 200
    assert client.get(f"{API}/ai/dataset-versions/{dv['id']}", headers=ADMIN).json()["status"] == "superseded"


# ---------------------------------------------------------------- PII の継承 (ai_inherit)
@pytest.fixture()
def inherit_world(client):
    """crm.people → bi.stats (PII を宣言しない集計) → ai.emb。"""
    asset(client, "crm.people", data_class="master", sensitivity="internal", contains_pii=True, retention_days=30, schema=SRC_SCHEMA)
    asset(client, "bi.stats", sensitivity="internal", schema=[{"name": "region"}, {"name": "cnt"}])
    emb = ai_asset(client, "ai.emb", "embedding_set", sensitivity="internal", privacy_technique="pseudonymized",
                   privacy_params={"key_vault_ref": "kv://1"})
    edge(client, "bi.stats", "ai.emb")
    return client, emb


def inheritance(client, emb) -> dict:
    r = client.get(f"{API}/ai/assets/{emb['id']}/inheritance", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_inheritance_is_dataset_level_without_trust(inherit_world):
    client, emb = inherit_world
    facet_event(client, REGION_ONLY, output="bi.stats")
    got = inheritance(client, emb)
    assert got["effective_contains_pii"] is True and got["pii_from"] == ["crm.people"] and got["pii_excluded"] == []


def test_inheritance_drops_pii_only_for_trusted_producers(inherit_world):
    client, emb = inherit_world
    facet_event(client, REGION_ONLY, output="bi.stats")
    trust(client)
    got = inheritance(client, emb)
    assert got["pii_from"] == [] and got["effective_contains_pii"] is got["declared_contains_pii"]
    assert got["pii_excluded"] == [{"qualified_name": "crm.people", "reason": "no_direct_path_from_pii_columns",
                                    "trusted_producers": ["etl/agg"]}]
    with SessionLocal() as db:  # 保存された継承の結果にも理由が残る
        from edgf.models import DataAsset
        stored = db.get(DataAsset, emb["id"]).inheritance
    assert stored["pii_excluded"][0]["qualified_name"] == "crm.people"


def test_masking_does_not_stop_inheritance_and_columns_are_widened(inherit_world):
    client, emb = inherit_world
    facet_event(client, {"cnt": [input_field("crm", "crm.people", "email", subtype="TRANSFORMATION", masking=True)]}, output="bi.stats")
    trust(client)
    got = inheritance(client, emb)
    assert got["effective_contains_pii"] is True and got["pii_from"] == ["crm.people"] and got["pii_excluded"] == []
    # 広げる方向: bi.stats → ai.emb のカラムの辺が無いので、ai.emb のカラムは分からない (資産単位で継承する)
    assert got["pii_columns"] == {}


def test_pii_columns_are_reported_for_direct_flows(client):
    asset(client, "crm.people", data_class="master", sensitivity="internal", contains_pii=True, retention_days=30, schema=SRC_SCHEMA)
    emb = ai_asset(client, "ai.vec", "embedding_set", sensitivity="internal", privacy_technique="pseudonymized",
                   privacy_params={"key_vault_ref": "kv://1"})
    facet_event(client, {"vector": [input_field("crm", "crm.people", "email", subtype="TRANSFORMATION", masking=True)],
                 "bucket": [input_field("crm", "crm.people", "region", type="INDIRECT", subtype="GROUP_BY")]}, output="ai.vec")
    got = inheritance(client, emb)
    assert got["pii_columns"] == {"vector": [{"asset": "crm.people", "column": "email"}]}
    assert got["effective_contains_pii"] is True


def test_widening_keeps_dataset_results_when_no_facet(client):
    """facet が無ければ継承の結果 (保存値を含む) は従来と同じ。"""
    asset(client, "crm.people", data_class="master", sensitivity="internal", contains_pii=True, retention_days=30, schema=SRC_SCHEMA)
    emb = ai_asset(client, "ai.vec", "embedding_set", sensitivity="internal", privacy_technique="pseudonymized",
                   privacy_params={"key_vault_ref": "kv://1"})
    edge(client, "crm.people", "ai.vec")
    got = inheritance(client, emb)
    assert got["pii_columns"] == {} and got["pii_excluded"] == [] and got["pii_from"] == ["crm.people"]
    with SessionLocal() as db:
        from edgf.models import DataAsset
        stored = db.get(DataAsset, emb["id"]).inheritance
    assert set(stored) == {"sensitivity_from", "pii_from", "released_by", "unregistered_upstream"}
