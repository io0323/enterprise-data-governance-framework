"""Phase 6 (§19 非機能要件): マイグレーション / Problem Details / ページネーション / 構造化ログ / health・ready."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from edgf import migrate
from edgf.db import Base, engine
from edgf.main import create_app
from edgf.observability import JsonFormatter, configure_logging, setup_tracing
from tests.conftest import H, reset_db

API = "/api/v1"
ROOT = Path(__file__).resolve().parent.parent
PROBLEM_KEYS = {"type", "title", "status", "detail", "instance", "request_id"}


def assert_problem(r, status: int, type_slug: str | None = None) -> dict:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert PROBLEM_KEYS <= set(body), body
    assert body["status"] == status
    assert body["type"].startswith("urn:edgf:problem:")
    if type_slug:
        assert body["type"] == f"urn:edgf:problem:{type_slug}"
    assert body["request_id"] == r.headers["x-request-id"]
    return body


# ---------------------------------------------------------------- Alembic
def test_alembic_cli_builds_empty_database(tmp_path):
    """受け入れ基準: `alembic upgrade head` で空の DB が構築できる。"""
    db_file = tmp_path / "fresh.db"
    env = {**os.environ, "EDGF_DATABASE_URL": f"sqlite:///{db_file}"}
    alembic = Path(sys.executable).parent / "alembic"
    res = subprocess.run([str(alembic), "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    eng = create_engine(f"sqlite:///{db_file}")
    try:
        tables = set(inspect(eng).get_table_names())
        assert set(Base.metadata.tables) <= tables and "alembic_version" in tables
        with eng.connect() as conn:
            assert migrate.current_revision(conn) == migrate.head_revision()
    finally:
        eng.dispose()


def test_migrations_match_models_and_downgrade():
    """モデル定義とマイグレーションの乖離 (autogenerate 差分) がないこと。downgrade で空に戻ること。"""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    reset_db()
    migrate.upgrade(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True, "include_object": migrate.include_object}), Base.metadata)
    assert diff == []
    migrate.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    reset_db()


# ---------------------------------------------------------------- Problem Details
def test_domain_errors_are_problem_details(client, sales_asset):
    aid = sales_asset["id"]
    b = assert_problem(client.get(f"{API}/assets/does-not-exist"), 404, "not-found")
    assert b["instance"] == f"{API}/assets/does-not-exist" and "asset not found" in b["detail"]
    assert_problem(client.post(f"{API}/domains", headers=H(), json={"name": "sales"}), 409, "conflict")
    assert_problem(client.post(f"{API}/assets/{aid}/certification", headers=H("stranger"),
                               json={"certification": "certified"}), 403, "forbidden")
    assert_problem(client.post(f"{API}/assets/{aid}/lifecycle", headers=H(),
                               json={"target_state": "archived"}), 409)
    assert_problem(client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": []}), 409)
    assert_problem(client.get(f"{API}/lineage/x/upstream", params={"depth": 0}), 422, "validation-error")


def test_certification_blocked_has_blockers_extension(client, sales_asset):
    r = client.post(f"{API}/assets/{sales_asset['id']}/certification", headers=H("owner1"),
                    json={"certification": "certified"})
    b = assert_problem(r, 409, "certification-blocked")
    assert any("lifecycle_state" in x for x in b["blockers"])


def test_validation_error_lists_fields(client):
    r = client.post(f"{API}/assets", headers=H(), json={"qualified_name": "x.y", "name": "y",
                                                         "data_class": "nope", "sensitivity": "internal"})
    b = assert_problem(r, 422, "validation-error")
    assert b["errors"] and b["errors"][0]["loc"] == ["body", "data_class"]


def test_framework_errors_are_problem_details(client):
    assert_problem(client.get("/no/such/route"), 404, "not-found")
    r = client.delete(f"{API}/domains")
    b = assert_problem(r, 405, "method-not-allowed")
    assert r.headers["allow"] and b["detail"]


def test_unhandled_integrity_and_legacy_errors():
    app = create_app()

    @app.get("/_boom")
    def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/_integrity")
    def integrity():
        raise IntegrityError("INSERT INTO audit_events ...", {}, Exception("UNIQUE constraint failed: prev_hash"))

    @app.get("/_legacy")
    def legacy():
        raise HTTPException(409, {"message": "legacy blocked", "blockers": ["a"]})

    with TestClient(app, raise_server_exceptions=False) as c:
        b = assert_problem(c.get("/_boom"), 500, "internal-error")
        assert "secret" not in json.dumps(b)  # 内部情報を漏らさない
        b = assert_problem(c.get("/_integrity"), 409, "integrity-conflict")
        assert "prev_hash" not in json.dumps(b)
        b = assert_problem(c.get("/_legacy"), 409, "conflict")
        assert b["detail"] == "legacy blocked" and b["blockers"] == ["a"]


def test_openapi_documents_problem_responses(client):
    op = client.get("/openapi.json").json()["paths"]["/api/v1/assets/{asset_id}"]["get"]
    assert "application/problem+json" in op["responses"]["4XX"]["content"]


# ---------------------------------------------------------------- Pagination
def _link_offsets(r) -> dict[str, int]:
    return {rel: int(off) for off, rel in re.findall(r'<[^>]*offset=(\d+)[^>]*>; rel="(\w+)"', r.headers.get("link", ""))}


def test_pagination_headers(client):
    for i in range(5):
        client.post(f"{API}/domains", headers=H(), json={"name": f"d{i}"})
    r = client.get(f"{API}/domains", params={"limit": 2})
    assert [d["name"] for d in r.json()] == ["d0", "d1"]
    assert r.headers["x-total-count"] == "5" and _link_offsets(r) == {"next": 2}
    r = client.get(f"{API}/domains", params={"limit": 2, "offset": 2})
    assert [d["name"] for d in r.json()] == ["d2", "d3"] and _link_offsets(r) == {"next": 4, "prev": 0}
    r = client.get(f"{API}/domains", params={"limit": 2, "offset": 4})
    assert len(r.json()) == 1 and _link_offsets(r) == {"prev": 2}
    assert_problem(client.get(f"{API}/domains", params={"limit": 0}), 422)
    assert_problem(client.get(f"{API}/domains", params={"limit": 1001}), 422)
    assert_problem(client.get(f"{API}/domains", params={"offset": -1}), 422)


@pytest.mark.parametrize("path", [
    "/domains", "/assets", "/assets/retention-due", "/glossary/terms", "/issues", "/audit/events",
    "/policies", "/policies/exceptions", "/kpi/domains",
])
def test_all_list_endpoints_paginated(client, sales_asset, path):
    r = client.get(f"{API}{path}", params={"limit": 1})
    assert r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) <= 1
    assert int(r.headers["x-total-count"]) >= len(r.json())


def test_asset_level_lists_paginated(client, sales_asset):
    aid = sales_asset["id"]
    for col in ("order_id", "email"):
        client.post(f"{API}/quality/assets/{aid}/rules", headers=H(), json={
            "name": f"{col}必須", "dimension": "completeness", "rule_type": "not_null", "column": col})
    r = client.get(f"{API}/quality/assets/{aid}/rules", params={"limit": 1})
    assert len(r.json()) == 1 and r.headers["x-total-count"] == "2"
    for _ in range(3):
        client.post(f"{API}/quality/assets/{aid}/runs", headers=H(), json={"records": [{"order_id": "1"}]})
    r = client.get(f"{API}/quality/assets/{aid}/runs")
    assert len(r.json()) == 3 and r.headers["x-total-count"] == "3"


def test_asset_search_paginates_with_tags_in_sql(client):
    for i, tags in enumerate([["legacy"], ["LEGACY", "顧客"], ["legacy-v2"], [], ["100%"]]):
        client.post(f"{API}/assets", headers=H(), json={"qualified_name": f"t.a{i}", "name": f"a{i}",
                                                         "data_class": "operational", "sensitivity": "internal",
                                                         "tags": tags})
    r = client.get(f"{API}/assets", params={"q": "legacy", "limit": 1})
    assert r.headers["x-total-count"] == "2" and [a["qualified_name"] for a in r.json()] == ["t.a0"]
    assert [a["qualified_name"] for a in client.get(f"{API}/assets", params={"q": "顧客"}).json()] == ["t.a1"]
    # LIKE のメタ文字はワイルドカードとして扱わない
    assert [a["qualified_name"] for a in client.get(f"{API}/assets", params={"q": "100%"}).json()] == ["t.a4"]
    assert client.get(f"{API}/assets", params={"q": "_"}).json() == []


# ---------------------------------------------------------------- Logging / request_id
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[dict] = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture()
def captured():
    h = _Capture()
    logger = logging.getLogger("edgf")
    logger.addHandler(h)
    yield h.lines
    logger.removeHandler(h)


def test_request_id_generated_propagated_and_logged(client, captured):
    r = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])
    r = client.get("/health", headers={"X-Request-ID": "gw-abc.123"})
    assert r.headers["x-request-id"] == "gw-abc.123"
    r = client.get("/health", headers={"X-Request-ID": "bad id\nINJECT"})  # 不正値は採番し直す
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])
    access = [ln for ln in captured if ln["logger"] == "edgf.access"]
    assert access[1]["request_id"] == "gw-abc.123"
    assert {"method", "path", "status", "duration_ms", "ts", "level"} <= set(access[1])
    assert access[1]["path"] == "/health" and access[1]["status"] == 200


def test_json_formatter_fields_and_exceptions():
    rec = logging.LogRecord("edgf.x", logging.ERROR, __file__, 1, "msg %s", ("a",), None)
    rec.fields = {"k": 1, "message": "must-not-override"}
    out = json.loads(JsonFormatter().format(rec))
    assert out["message"] == "msg a" and out["k"] == 1 and out["level"] == "ERROR"
    try:
        raise ValueError("x")
    except ValueError:
        rec.exc_info = sys.exc_info()
    assert "ValueError" in json.loads(JsonFormatter().format(rec))["exc_info"]


def test_configure_logging_is_idempotent_and_supports_text():
    import io
    buf = io.StringIO()
    logger = configure_logging("DEBUG", "text", stream=buf)
    configure_logging("DEBUG", "text", stream=buf)
    assert sum(1 for h in logger.handlers if getattr(h, "_edgf_handler", False)) == 1
    logger.debug("hello")
    assert "hello" in buf.getvalue() and not buf.getvalue().startswith("{")
    configure_logging("INFO", "json")


def test_tracing_is_optional(caplog):
    assert setup_tracing(object(), enabled=False) is False
    try:
        import opentelemetry.sdk  # noqa: F401
        pytest.skip("opentelemetry installed")
    except ImportError:
        pass
    logger = logging.getLogger("edgf")
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="edgf.observability"):
            assert setup_tracing(object(), enabled=True) is False
        assert "OpenTelemetry" in caplog.text
    finally:
        logger.propagate = False


# ---------------------------------------------------------------- health / ready
def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "schema": "ok", "policies": "ok"}


def test_ready_reports_outdated_schema(client):
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    b = assert_problem(client.get("/ready"), 503, "not-ready")
    assert b["checks"]["schema"].startswith("outdated") and b["checks"]["database"] == "ok"
    assert client.get("/health").status_code == 200  # liveness は依存先に影響されない


def test_ready_reports_database_down(client, monkeypatch, tmp_path):
    from edgf import db as db_module
    monkeypatch.setattr(db_module, "engine", create_engine(f"sqlite:///{tmp_path}/missing/dir/x.db"))
    b = assert_problem(client.get("/ready"), 503, "not-ready")
    assert b["checks"]["database"] == "unavailable"


def test_ready_reports_invalid_policies(client, monkeypatch):
    from edgf.api import ops
    from edgf.services.policy import PolicyLoadError

    def broken():
        raise PolicyLoadError("bad yaml")
    monkeypatch.setattr(ops, "get_policies", broken)
    assert assert_problem(client.get("/ready"), 503)["checks"]["policies"] == "invalid"


def test_auto_migrate_on_startup(monkeypatch):
    import edgf.main as main_module
    from edgf.config import Settings

    reset_db()
    monkeypatch.setenv("EDGF_AUTO_MIGRATE", "true")
    monkeypatch.setattr(main_module, "settings", Settings())
    with TestClient(create_app()) as c:
        assert c.get("/ready").status_code == 200
    reset_db()


# ---------------------------------------------------------------- Audit request_id (§19 Auditability)
def test_audit_events_carry_request_id_in_hash(client):
    client.post(f"{API}/domains", headers={**H(), "X-Request-ID": "req-audit-1"}, json={"name": "fin"})
    ev = client.get(f"{API}/audit/events", params={"request_id": "req-audit-1"}).json()
    assert [e["action"] for e in ev] == ["domain.create"]
    assert client.get(f"{API}/audit/verify").json()["valid"]
    with engine.begin() as conn:  # request_id の改ざんも検知できる
        conn.execute(text("UPDATE audit_events SET request_id = 'forged' WHERE request_id = 'req-audit-1'"))
    assert client.get(f"{API}/audit/verify").json()["valid"] is False


def test_legacy_events_without_request_id_still_verify(client):
    from edgf.services import audit
    from edgf.db import SessionLocal
    with SessionLocal() as s:  # リクエスト外 (request_id なし) の記録 = Phase 6 以前と同じハッシュ形式
        audit.record(s, "system", "batch.run", "job", "j1", {"n": 1})
        s.commit()
    assert client.get(f"{API}/audit/events", params={"action": "batch.run"}).json()[0]["request_id"] is None
    assert client.get(f"{API}/audit/verify").json()["valid"]
