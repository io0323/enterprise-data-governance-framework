#!/usr/bin/env python
"""負荷試験の合成データを PostgreSQL に投入する (乱数のシードを固定するので、同じ引数なら同じデータになる).

    EDGF_DATABASE_URL=postgresql+psycopg://... python scripts/perf/seed.py [--assets 100000] [--reset]

規模: 資産 10 万件 (restricted 5%)、ドメイン 50、Lineage は深さ 10 で分岐あり、Product 500、承認待ちの案件 1,000。
シナリオが使う ID は perf/manifest.json に書き出す。個人情報は含まない。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EDGF_AUTH_MODE", "dev")

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from edgf import migrate  # noqa: E402
from edgf.db import Base  # noqa: E402
from edgf.models import (ApprovalRequest, ColumnLineageEdge, ContractVersion, DataAsset, DataContract, DataProduct, Domain, GlossaryTerm, LineageEdge,  # noqa: E402
                         MeshSubscription, OutputPort, PrivacyPurpose, QualityRun, product_term, utcnow)
from edgf.services import policy_bundles  # noqa: E402

DOMAINS = 50
PRODUCTS = 500
APPROVALS = 1000
RESTRICTED_RATE = 0.05
LINEAGE_DEPTH = 10
BATCH = 5000
CONTROLS = ["encryption_at_rest", "encryption_in_transit", "masking"]
WORDS = ["orders", "customers", "invoices", "shipments", "payments", "inventory", "returns", "sessions", "campaigns", "contracts"]
INBOX_OWNER = "perf-owner"
RAG_SERVICE = "perf-rag-service"
CHECK_CUSTODIAN = "perf-ci"


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _insert(conn, model, rows) -> None:
    table = getattr(model, "__table__", model)
    for start in range(0, len(rows), BATCH):
        conn.execute(table.insert(), rows[start:start + BATCH])


def _domain_owner(index: int) -> str:
    return f"owner-d{index:02d}"


def _asset_rows(rng: random.Random, total: int, now) -> list[dict]:
    rows = []
    for i in range(total):
        domain = i % DOMAINS
        word = rng.choice(WORDS)
        sensitivity = "restricted" if rng.random() < RESTRICTED_RATE else rng.choices(["public", "internal", "confidential"], [20, 60, 20])[0]
        rows.append({
            "id": _uuid(rng), "qualified_name": f"d{domain:02d}.{word}.a{i}", "name": f"{word} {i}", "description": f"{word} の合成データ {i}",
            "domain": f"d{domain:02d}", "data_class": "transactional", "sensitivity": sensitivity, "owner": _domain_owner(domain), "steward": f"steward-d{domain:02d}",
            "custodian": f"custodian-d{domain:02d}", "lifecycle_state": "active", "certification": "certified" if rng.random() < 0.3 else "draft",
            "is_cde": rng.random() < 0.05, "contains_pii": False, "legal_hold": False, "schema": [{"name": "id", "type": "bigint"}, {"name": "amount", "type": "int"}],
            "tags": [word], "controls": CONTROLS, "privacy_technique": "none", "privacy_params": {}, "inheritance": {}, "created_at": now - timedelta(days=rng.randint(0, 300)), "updated_at": now,
        })
    return rows


def _tree_edges(names: list[str]) -> list[dict]:
    """分岐 2 の木を深さ 10 まで作る (2,047 ノード)。影響分析の最悪の形。"""
    return [{"source": names[(i - 1) // 2], "target": names[i], "job": "perf", "transformation": ""} for i in range(1, len(names))]


def _chain_edges(rows: list[dict], rng: random.Random, start: int) -> list[dict]:
    """残りの資産は、深さ 10 の鎖に、確率的な分岐を加える。"""
    edges = []
    for i in range(start, len(rows) - 1):
        if (i - start) % (LINEAGE_DEPTH + 1) == LINEAGE_DEPTH:
            continue
        edges.append({"source": rows[i]["qualified_name"], "target": rows[i + 1]["qualified_name"], "job": "perf", "transformation": ""})
        if rng.random() < 0.1 and i + 2 < len(rows):
            edges.append({"source": rows[i]["qualified_name"], "target": rows[i + 2]["qualified_name"], "job": "branch", "transformation": ""})
    return edges


def seed(url: str, assets_total: int, reset: bool, manifest_path: Path) -> dict:
    rng = random.Random(20261109)
    now = utcnow()
    engine = create_engine(url)
    if reset:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migrate.upgrade(engine)
    with Session(engine) as db:
        if db.scalar(select(func.count()).select_from(DataAsset)):
            sys.exit("DB が空ではありません。全データを削除してよければ --reset を付けてください")
        policy_bundles.bootstrap(db)
        db.commit()
    rows = _asset_rows(rng, assets_total, now)
    # シナリオで使う資産の役割を決める
    tree = rows[:2047]
    for row in tree:
        row.update(sensitivity="internal" if rng.random() > RESTRICTED_RATE else "restricted")
    inbox_assets = rows[2047:2047 + APPROVALS]
    for row in inbox_assets:
        row.update(owner=INBOX_OWNER, sensitivity="internal")
    port_assets = rows[2047 + APPROVALS:2047 + APPROVALS + PRODUCTS]
    for row in port_assets:
        row.update(certification="certified", sensitivity="internal")
    check_asset = port_assets[0]
    check_asset["custodian"] = CHECK_CUSTODIAN
    sources = rows[2047 + APPROVALS + PRODUCTS:2047 + APPROVALS + PRODUCTS + 50]
    for i, row in enumerate(sources):
        row.update(certification="certified", sensitivity="restricted" if i % 5 == 0 else "internal")
    rest_start = 2047 + APPROVALS + PRODUCTS + 50
    corpus_id = _uuid(rng)
    rows.append({**rows[-1], "id": corpus_id, "qualified_name": "ai.perf_corpus", "name": "perf corpus", "data_class": "ai", "ai_kind": "rag_corpus", "custodian": RAG_SERVICE,
                 "sensitivity": "restricted", "tags": []})
    with engine.begin() as conn:
        _insert(conn, Domain, [{"name": f"d{i:02d}", "owner": _domain_owner(i), "description": ""} for i in range(DOMAINS)])
        _insert(conn, DataAsset, rows)
        edges = _tree_edges([r["qualified_name"] for r in tree]) + _chain_edges(rows[:-1], rng, rest_start)
        edges += [{"source": r["qualified_name"], "target": "ai.perf_corpus", "job": "perf", "transformation": ""} for r in sources]
        # Contract の check の影響範囲: 下流が 100 件
        edges += [{"source": check_asset["qualified_name"], "target": rows[rest_start + i * 11]["qualified_name"], "job": "perf-check", "transformation": ""} for i in range(100)]
        _insert(conn, LineageEdge, edges)
        # カラム単位のリネージ (Phase 16): 影響分析の木の各辺に、DIRECT の id と INDIRECT の結合キーを付ける
        now_utc = utcnow()
        column_edges = []
        for edge in _tree_edges([r["qualified_name"] for r in tree]):
            base = {"source": edge["source"], "target": edge["target"], "job": edge["job"], "masking": False, "last_run_id": None, "updated_at": now_utc}
            column_edges.append({**base, "source_column": "id", "target_column": "id", "type": "DIRECT", "subtype": "IDENTITY"})
            column_edges.append({**base, "source_column": "region", "target_column": "id", "type": "INDIRECT", "subtype": "JOIN"})
        _insert(conn, ColumnLineageEdge, column_edges)
        _insert(conn, QualityRun, [{"id": _uuid(rng), "asset_id": r["id"], "executed_at": now, "record_count": 100, "score": 0.995, "dimension_scores": {"completeness": 0.995}, "results": []}
                                   for r in [*port_assets, *rows[rest_start:rest_start + 5000]]])
        purpose_id = "perf-analytics"
        _insert(conn, PrivacyPurpose, [{"id": purpose_id, "description": "負荷試験", "default_legal_basis": "contract", "created_at": now, "updated_at": now}])
        term_id = _uuid(rng)
        _insert(conn, GlossaryTerm, [{"id": term_id, "name": "perf-term", "definition": "負荷試験の用語", "synonyms": []}])
        products, ports, contracts, versions, terms, subs = [], [], [], [], [], []
        for i, row in enumerate(port_assets):
            product_id, port_id, contract_id = _uuid(rng), _uuid(rng), _uuid(rng)
            products.append({"id": product_id, "name": f"product-{i}", "domain": row["domain"], "owner": row["owner"], "description": "負荷試験の Data Product", "status": "published", "created_by": "perf", "created_at": now, "updated_at": now})
            ports.append({"id": port_id, "product_id": product_id, "asset_id": row["id"], "name": "main", "address_uri": f"warehouse://{row['qualified_name']}", "port_type": "table"})
            contracts.append({"id": contract_id, "product_id": product_id, "output_port_id": port_id, "name": f"contract-{i}", "schema_object": "main", "created_by": "perf"})
            versions.append({"id": _uuid(rng), "contract_id": contract_id, "version": "1.0.0", "major": 1, "minor": 0, "patch": 0, "status": "published", "description": "負荷試験",
                             "schema": [{"name": "id", "type": "bigint", "nullable": False, "pii": False, "description": "ID"}, {"name": "amount", "type": "int", "nullable": True, "pii": False, "description": "金額"}],
                             "quality_sla": {"min_score": 0.9, "rule_ids": []}, "sensitivity": "internal", "purposes": [purpose_id], "cdm_mappings": {}, "created_by": "perf", "published_by": row["owner"], "published_at": now})
            terms.append({"product_id": product_id, "term_id": term_id})
            subs.extend({"id": _uuid(rng), "subscriber": f"user-{j}", "product_id": product_id, "output_port_id": port_id, "contract_id": contract_id, "major": 1, "purpose_id": purpose_id, "status": "active", "decision_comment": "", "created_at": now, "updated_at": now} for j in range(3))
        for model, data in ((DataProduct, products), (OutputPort, ports), (DataContract, contracts), (ContractVersion, versions), (product_term, terms), (MeshSubscription, subs)):
            _insert(conn, model, data)
        _insert(conn, ApprovalRequest, [{"id": _uuid(rng), "type": "access_request", "target_type": "asset", "target_id": r["id"], "asset_id": r["id"], "requested_by": f"requester-{i}", "reason": "負荷試験",
                                         "payload": {"purpose": "分析", "expires_at": (now + timedelta(days=30)).isoformat()}, "steps": [{"name": "owner", "rule": "asset_owner", "quorum": 1, "domain": None}],
                                         "current_step": 0, "status": "pending", "due_at": now + timedelta(days=3), "created_at": now} for i, r in enumerate(inbox_assets)])
    with engine.begin() as conn:
        conn.execute(text("ANALYZE"))
    readable = [r["id"] for r in rows[rest_start:rest_start + 2000]]
    manifest = {"assets": assets_total, "sample_asset_ids": readable, "impact_root": tree[0]["qualified_name"], "corpus_id": corpus_id, "rag_hits": [r["id"] for r in sources],
                "contract_id": contracts[0]["id"], "check_custodian": CHECK_CUSTODIAN, "rag_service": RAG_SERVICE, "inbox_owner": INBOX_OWNER}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {"assets": len(rows), "edges": len(edges), "products": len(products), "approvals": len(inbox_assets), "restricted": sum(1 for r in rows if r["sensitivity"] == "restricted")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=int, default=100_000)
    parser.add_argument("--reset", action="store_true", help="全テーブルを削除してから投入する")
    parser.add_argument("--manifest", type=Path, default=ROOT / "perf" / "manifest.json")
    args = parser.parse_args()
    url = os.environ.get("EDGF_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        sys.exit("負荷試験は PostgreSQL だけで行います。EDGF_DATABASE_URL を指定してください")
    print(json.dumps(seed(url, args.assets, args.reset, args.manifest)))


if __name__ == "__main__":
    main()
