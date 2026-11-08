"""Phase 8d (§12 Anonymization / Pseudonymization): 保護手法の管理とポリシー、マイグレーションの往復."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from edgf.config import Settings
from edgf.db import engine
from edgf.services import privacy
from edgf.services.policy import PolicyLoadError, load_policies

API = "/api/v1"
ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"X-Actor": "root", "X-Roles": "admin", "X-Domains": ""}
COMMITTEE = {"X-Actor": "chair", "X-Roles": "committee", "X-Domains": ""}
PII_COL = {"name": "email", "pii_category": "contact", "processing_purposes": ["crm"], "legal_basis": "contract"}
ANON_OK = {"privacy_technique": "anonymized", "privacy_params": {"k": 5}}
PSEUDO_OK = {"privacy_technique": "pseudonymized", "privacy_params": {"key_vault_ref": "vault://edgf/pii-key"}}


def asset(client, qn="s.a", **kw):
    r = client.post(f"{API}/assets", headers=ADMIN, json={
        "qualified_name": qn, "name": qn, "data_class": "operational", "sensitivity": "confidential", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def results(client, a) -> dict[str, dict]:
    res = client.get(f"{API}/policies/evaluate/assets/{a['id']}", headers=ADMIN).json()["results"]
    return {r["policy_id"]: r for r in res}


# ---------------------------------------------------------------- contains_pii の判定
def test_only_qualifying_anonymization_clears_contains_pii(client):
    a = asset(client, "s.ok", contains_pii=True, schema=[PII_COL], **ANON_OK)
    assert a["contains_pii"] is False and a["privacy_technique"] == "anonymized" and a["privacy_params"] == {"k": 5}
    weak = asset(client, "s.weak", contains_pii=True, schema=[PII_COL], privacy_technique="anonymized", privacy_params={"k": 4})
    assert weak["contains_pii"] is True
    for tech in ("masked", "tokenized", "aggregated", "none"):
        assert asset(client, f"s.{tech}", schema=[PII_COL], privacy_technique=tech)["contains_pii"] is True, tech
    assert asset(client, "s.bad_k", contains_pii=True, privacy_technique="anonymized", privacy_params={"k": "9"})["contains_pii"] is True
    assert asset(client, "s.bool_k", contains_pii=True, privacy_technique="anonymized", privacy_params={"k": True})["contains_pii"] is True
    # 仮名化は宣言が false でも個人データのまま
    assert asset(client, "s.pseudo", contains_pii=False, **PSEUDO_OK)["contains_pii"] is True
    col = {**PII_COL, **ANON_OK}
    assert asset(client, "s.colanon", schema=[col])["contains_pii"] is False  # 全 PII カラムが基準を満たす
    assert asset(client, "s.colmix", schema=[col, PII_COL])["contains_pii"] is True
    assert asset(client, "s.colpseudo", schema=[{**PII_COL, **PSEUDO_OK}], **ANON_OK)["contains_pii"] is True
    # 手作業の false は PII カラムが無ければ従来どおり
    assert asset(client, "s.plain", contains_pii=False)["contains_pii"] is False


def test_patch_transitions(client):
    a = asset(client, "s.a", schema=[PII_COL])
    url = f"{API}/assets/{a['id']}"
    r = client.patch(url, headers=ADMIN, json=ANON_OK).json()
    assert r["contains_pii"] is False
    r = client.patch(url, headers=ADMIN, json={"privacy_params": {"k": 2}}).json()
    assert r["contains_pii"] is True  # 基準を割ると個人データに戻る
    assert client.patch(url, headers=ADMIN, json={"privacy_params": {"k": 9}}).json()["contains_pii"] is False
    assert client.patch(url, headers=ADMIN, json={"privacy_technique": None, "privacy_params": None}).status_code == 200
    r = client.patch(url, headers=ADMIN, json=PSEUDO_OK).json()
    assert r["contains_pii"] is True
    # PII カラムが無くても、匿名化から外れたら個人データとして扱う
    b = asset(client, "s.b", contains_pii=True, **ANON_OK)
    assert b["contains_pii"] is False
    r = client.patch(f"{API}/assets/{b['id']}", headers=ADMIN, json={"privacy_technique": "masked"}).json()
    assert r["contains_pii"] is True
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"privacy_technique": "bogus"}).status_code == 422


def test_technique_change_resets_certification(client):
    a = asset(client, "s.a", owner="o", steward="s")
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET certification = 'certified'"))
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"privacy_technique": "masked"})
    assert r.json()["certification"] == "draft"


def test_min_k_comes_from_setting(monkeypatch, client):
    assert Settings().min_k_anonymity == 5
    monkeypatch.setenv("EDGF_MIN_K_ANONYMITY", "10")
    assert Settings().min_k_anonymity == 10
    monkeypatch.setattr(privacy, "settings", Settings())
    assert privacy.k_satisfied({"k": 9}) is False and privacy.k_satisfied({"k": 10}) is True
    a = asset(client, "s.a", contains_pii=True, **ANON_OK)  # k=5 < 10
    assert a["contains_pii"] is True and results(client, a)["PRV-003"]["status"] == "fail"


# ---------------------------------------------------------------- ポリシー
def test_prv003(client):
    assert "PRV-003" not in results(client, asset(client, "s.none"))  # 宣言が無ければ対象外
    assert results(client, asset(client, "s.ok", **ANON_OK))["PRV-003"]["status"] == "pass"
    bad = results(client, asset(client, "s.bad", privacy_technique="anonymized", privacy_params={"k": 2}))["PRV-003"]
    assert bad["status"] == "fail" and bad["severity"] == "critical" and "k >= 5" in bad["message"]
    assert results(client, asset(client, "s.nok", privacy_technique="anonymized"))["PRV-003"]["status"] == "fail"
    col = results(client, asset(client, "s.col", schema=[{**PII_COL, "privacy_technique": "anonymized", "privacy_params": {"k": 1}}]))
    assert col["PRV-003"]["status"] == "fail" and "email" in col["PRV-003"]["message"]


def test_prv004(client):
    assert "PRV-004" not in results(client, asset(client, "s.none"))
    assert results(client, asset(client, "s.ok", **PSEUDO_OK))["PRV-004"]["status"] == "pass"
    nokey = asset(client, "s.nokey", privacy_technique="pseudonymized")
    assert nokey["contains_pii"] is True
    r = results(client, nokey)["PRV-004"]
    assert r["status"] == "fail" and "key_vault_ref" in r["message"]
    # DB を直接書き換えて contains_pii を外しても検出する (多層防御)
    ok = asset(client, "s.tamper", **PSEUDO_OK)
    with engine.begin() as conn:
        conn.execute(text("UPDATE data_assets SET contains_pii = :f WHERE id = :i"), {"f": False, "i": ok["id"]})
    r = results(client, ok)["PRV-004"]
    assert r["status"] == "fail" and "個人データ" in r["message"]


def test_prv005(client):
    assert "PRV-005" not in results(client, asset(client, "s.none"))
    assert "PRV-005" not in results(client, asset(client, "s.flag_only", contains_pii=True))  # カラム情報なし
    assert results(client, asset(client, "s.ok", schema=[PII_COL]))["PRV-005"]["status"] == "pass"
    r = results(client, asset(client, "s.bad", schema=[{"name": "email", "pii": True}, PII_COL, {"name": "tel", "pii": True, "legal_basis": "consent"}]))["PRV-005"]
    assert r["status"] == "fail" and "email" in r["message"] and "tel" in r["message"]


def test_policies_are_registered_and_domain_cannot_override(tmp_path):
    ids = {p.id: p for p in load_policies(ROOT / "policies")}
    assert {"PRV-003", "PRV-004", "PRV-005"} <= set(ids) and all(ids[i].scope == "global" for i in ("PRV-003", "PRV-004", "PRV-005"))
    shutil.copy(ROOT / "policies" / "global.yaml", tmp_path / "global.yaml")
    (tmp_path / "domains").mkdir()
    for pid in ("PRV-003", "PRV-004", "PRV-005"):
        (tmp_path / "domains" / "x.yaml").write_text(
            f"scope: domain\ndomain: x\npolicies:\n  - {{id: {pid}, name: weaker, severity: low, check: {{type: glossary_linked}}}}\n",
            encoding="utf-8")
        with pytest.raises(PolicyLoadError, match="override"):
            load_policies(tmp_path)


def test_policy_violation_blocks_certification(client):
    a = asset(client, "s.a", owner="o", steward="s", privacy_technique="anonymized", privacy_params={"k": 1})
    chk = client.get(f"{API}/assets/{a['id']}/certification/check", headers=ADMIN).json()
    assert any("PRV-003" in b for b in chk["blockers"])


# ---------------------------------------------------------------- 他機能との連携
def test_inventory_and_erasure_respect_anonymization(client):
    asset(client, "s.src", custodian="c1", schema=[{"name": "email", "identifier_type": "email"}])
    anon = asset(client, "s.anon", custodian="c2", schema=[PII_COL], **ANON_OK)
    pseudo = asset(client, "s.pseudo", custodian="c3", schema=[PII_COL], **PSEUDO_OK)
    assert anon["contains_pii"] is False and pseudo["contains_pii"] is True
    for dst in ("s.anon", "s.pseudo"):
        client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": "s.src", "target": dst})
    names = {e["qualified_name"]: e for e in client.get(f"{API}/privacy/inventory", headers=COMMITTEE).json()}
    assert set(names) == {"s.src", "s.pseudo"} and names["s.pseudo"]["privacy_technique"] == "pseudonymized"
    rid = client.post(f"{API}/privacy/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": "x@example.com"}).json()["id"]
    scoped = client.post(f"{API}/privacy/erasure-requests/{rid}/scope", headers=ADMIN).json()
    # 仮名化された下流は消去の対象、匿名化された下流は個人データ統制の対象外
    assert sorted(t["qualified_name"] for t in scoped["tasks"]) == ["s.pseudo", "s.src"]


# ---------------------------------------------------------------- マイグレーション
def _alembic(args: list[str], url: str) -> None:
    res = subprocess.run([str(Path(sys.executable).parent / "alembic"), *args], cwd=ROOT,
                         env={**os.environ, "EDGF_DATABASE_URL": url}, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _version(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_migration_round_trips_each_step(tmp_path):
    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    _alembic(["upgrade", "head"], url)
    assert _version(db) == "0006" and {"consents", "privacy_purposes", "erasure_requests", "erasure_tasks"} <= _tables(db)
    _alembic(["downgrade", "-1"], url)  # 0006 → 0005
    assert _version(db) == "0005"
    with sqlite3.connect(db) as conn:
        assert "privacy_technique" not in [r[1] for r in conn.execute("PRAGMA table_info(data_assets)")]
    _alembic(["upgrade", "head"], url)
    assert _version(db) == "0006"
    _alembic(["downgrade", "-1"], url)
    _alembic(["downgrade", "-1"], url)  # 0005 → 0004
    assert _version(db) == "0004" and "erasure_tasks" not in _tables(db) and "consents" in _tables(db)
    _alembic(["downgrade", "-1"], url)  # 0004 → 0003
    assert _version(db) == "0003" and "consents" not in _tables(db)
    _alembic(["upgrade", "head"], url)
    assert _version(db) == "0006"


def test_existing_assets_get_defaults_on_upgrade(tmp_path):
    db = tmp_path / "e.db"
    url = f"sqlite:///{db}"
    _alembic(["upgrade", "0005"], url)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO data_assets (id, qualified_name, name, description, data_class, sensitivity, lifecycle_state,"
            " certification, is_cde, contains_pii, legal_hold, schema, tags, controls, created_at, updated_at)"
            " VALUES ('a1', 'old.asset', 'old', '', 'master', 'internal', 'draft', 'draft', 0, 1, 0, ?, '[]', '[]',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (json.dumps([{"name": "email", "type": "string", "description": "", "pii": True}]),))
    _alembic(["upgrade", "head"], url)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT privacy_technique, privacy_params, schema FROM data_assets").fetchone()
    assert row[0] == "none" and json.loads(row[1]) == {} and json.loads(row[2])[0]["pii"] is True
