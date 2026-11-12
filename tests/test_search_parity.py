"""検索の索引 (pg_trgm) の有無で、結果、件数、並び順が変わらないこと."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import String, cast, event, or_, select, text
from sqlalchemy.orm import aliased

from edgf import migrate
from edgf.api import catalog
from edgf.api.catalog import _LIKE_ESCAPE, _contains
from edgf.db import SessionLocal, engine
from edgf.models import DataAsset, GlossaryTerm
from edgf.services.search_index import DEGRADED, SEARCH_INDEXES, search_index_warning
from tests.process_helpers import ADMIN, API, U, asset

pytestmark = pytest.mark.fresh_schema  # 索引と拡張の削除、マイグレーションの往復を行う (テストごとにスキーマを作り直す)

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("migration_0028", ROOT / "edgf" / "migrations" / "versions" / "0028_search_trgm_indexes.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

POSTGRES = engine.dialect.name == "postgresql"
only_postgres = pytest.mark.skipif(not POSTGRES, reason="pg_trgm は PostgreSQL だけ")
ASSETS = "/api/v1/assets"
# 2 文字以下、記号、大文字小文字、非 ASCII、タグの完全一致、用語の名前と定義
INPUTS = ["a", "or", "受", "受注", "%", "_", "!", "!!", "100%", "a_b", "'", '"', "\\", "[", "()", "ORDERS", "orders", "OrDeRs", "Orders 集計", "café", "CAFÉ", "É", "é", "RÉSUMÉ", "résumé", "日本語", "😀",
          "VIP", "vip", "vi", "日本", "#タグ", "売上", "売上高の定義", "定義", "sales.", "sales.orders", "nothing-here", " ", "  orders"]


@pytest.fixture()
def data(client):
    asset(client, "sales.orders", description="受注の説明")
    asset(client, "sales.total", description="ORDERS 集計", tags=["VIP"])
    asset(client, "sales.pct", description="100% a_b と 'quote' \"double\" \\ [x] ()")
    asset(client, "sales.cafe", description="Café と CAFÉ", tags=["日本"])
    asset(client, "sales.mark", description="!! 😀 日本語", tags=["#タグ", "vip2"])
    asset(client, "other.nodesc", description="")
    asset(client, "other.accent", description="RÉSUMÉ")
    asset(client, "sales.secret", description="受注の機密", sensitivity="restricted")
    deleted = asset(client, "sales.deleted", description="受注 orders")
    with SessionLocal() as db:
        db.get(DataAsset, deleted["id"]).lifecycle_state = "deleted"
        term = GlossaryTerm(name="売上", definition="売上高の定義")
        term.assets = [db.scalar(select(DataAsset).where(DataAsset.qualified_name == "other.nodesc"))]
        db.add(term)
        db.commit()
    return client


def reference_names(q: str) -> list[str]:
    """索引を入れる前の問い合わせ (ILIKE の OR)。これと一致すれば挙動は変わっていない。"""
    like, tag_like = _contains(q), _contains(json.dumps(q))
    term_assets = select(DataAsset.id).join(DataAsset.terms).where(
        or_(GlossaryTerm.name.ilike(like, escape=_LIKE_ESCAPE), GlossaryTerm.definition.ilike(like, escape=_LIKE_ESCAPE)))
    alias = aliased(DataAsset)
    stmt = select(alias.qualified_name).where(or_(alias.name.ilike(like, escape=_LIKE_ESCAPE), alias.qualified_name.ilike(like, escape=_LIKE_ESCAPE),
                                                  alias.description.ilike(like, escape=_LIKE_ESCAPE), alias.id.in_(term_assets),
                                                  cast(alias.tags, String).ilike(tag_like, escape=_LIKE_ESCAPE)),
                                              alias.lifecycle_state != "deleted").order_by(alias.qualified_name)
    with SessionLocal() as db:
        return list(db.scalars(stmt))


def api_names(client, q: str) -> tuple[list[str], str]:
    r = client.get(ASSETS, headers=ADMIN, params={"q": q})
    assert r.status_code == 200, r.text
    return [a["qualified_name"] for a in r.json()], r.headers["x-total-count"]


def assert_same_as_reference(client):
    for q in INPUTS:
        names, total = api_names(client, q)
        assert names == reference_names(q), q
        assert total == str(len(names)), q


@pytest.fixture()
def forced_index():
    """小さな表でも Planner に索引を使わせる。"""
    def force(dbapi_connection, record, proxy):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET enable_seqscan = off")
        cursor.close()

    engine.dispose()
    event.listen(engine, "checkout", force)
    yield
    event.remove(engine, "checkout", force)
    engine.dispose()


def _drop_indexes():
    with engine.begin() as conn:
        for name in SEARCH_INDEXES:
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))


def test_results_match_previous_query_on_current_database(data):
    """SQLite と、索引のある PostgreSQL。"""
    assert_same_as_reference(data)


def test_results_match_when_many_rows_match(data, monkeypatch):
    """一致が多いときの読み方でも結果、件数、並び順は同じ。"""
    monkeypatch.setattr(catalog, "MAX_SORTED_MATCHES", 0)
    assert_same_as_reference(data)
    first = data.get(ASSETS, headers=ADMIN, params={"q": "sales.", "limit": 2, "offset": 1})
    assert [a["qualified_name"] for a in first.json()] == ["sales.mark", "sales.orders"] and first.headers["x-total-count"] == "6"


def test_paging_past_the_end_on_both_paths(data, monkeypatch):
    for limit in (1000, 0):
        monkeypatch.setattr(catalog, "MAX_SORTED_MATCHES", limit)
        r = data.get(ASSETS, headers=ADMIN, params={"q": "sales.", "offset": 100})
        assert r.json() == [] and r.headers["x-total-count"] == "6"


@only_postgres
def test_results_match_when_index_is_forced(data, forced_index):
    assert_same_as_reference(data)


@only_postgres
def test_results_match_without_indexes(data):
    _drop_indexes()
    assert_same_as_reference(data)


@only_postgres
def test_results_match_without_extension(data):
    with engine.begin() as conn:
        conn.execute(text("DROP EXTENSION pg_trgm CASCADE"))
    assert_same_as_reference(data)


@pytest.mark.parametrize("q, expected", [
    ("a", ["other.accent", "sales.cafe", "sales.mark", "sales.orders", "sales.pct", "sales.secret", "sales.total"]),  # 1 文字
    ("or", ["sales.orders", "sales.total"]),                  # 2 文字
    ("ORDERS", ["sales.orders", "sales.total"]),              # 大文字小文字
    ("受注", ["sales.orders", "sales.secret"]),                # 非 ASCII
    ("受", ["sales.orders", "sales.secret"]),
    ("%", ["sales.pct"]),                                     # LIKE の記号はそのままの文字として扱う
    ("_", ["sales.pct"]),
    ("!!", ["sales.mark"]),                                   # エスケープの文字
    ("a_b", ["sales.pct"]),
    ("VIP", ["sales.total"]),                                 # タグは完全一致 (大文字小文字は無視)
    ("vip", ["sales.total"]),
    ("vi", []),
    ("売上", ["other.nodesc"]),                                # 用語の名前
    ("売上高の定義", ["other.nodesc"]),                         # 用語の定義
    ("nothing-here", []),
])
def test_same_answers_on_every_database(data, q, expected):
    """SQLite と PostgreSQL で同じ結果になる入力。CI で両方を実行する。"""
    names, total = api_names(data, q)
    assert names == expected and total == str(len(expected))


def test_non_ascii_case_difference_stays_as_documented(data):
    """非 ASCII の大文字小文字の同一視は DB で異なる (README の既知の制約で、索引の前後で変わらない)。"""
    assert api_names(data, "RÉSUMÉ")[0] == ["other.accent"]
    assert api_names(data, "résumé")[0] == (["other.accent"] if POSTGRES else [])
    assert reference_names("résumé") == api_names(data, "résumé")[0]


def test_order_paging_and_total_are_unchanged(data):
    first = data.get(ASSETS, headers=ADMIN, params={"q": "a", "limit": 2})
    second = data.get(ASSETS, headers=ADMIN, params={"q": "a", "limit": 2, "offset": 2})
    assert first.headers["x-total-count"] == second.headers["x-total-count"] == "7"
    assert [a["qualified_name"] for a in first.json() + second.json()] == ["other.accent", "sales.cafe", "sales.mark", "sales.orders"]
    assert 'rel="next"' in first.headers["link"]


def test_search_still_applies_abac_and_filters(data):
    reader = U("reader", clearance="internal")
    assert [a["qualified_name"] for a in data.get(ASSETS, headers=reader, params={"q": "受注"}).json()] == ["sales.orders"]
    assert "sales.secret" in [a["qualified_name"] for a in data.get(ASSETS, headers=ADMIN, params={"q": "機密"}).json()]
    assert data.get(ASSETS, headers=reader, params={"q": "機密"}).json() == []
    assert data.get(ASSETS, headers=reader, params={"q": "機密"}).headers["x-total-count"] == "0"
    assert "sales.deleted" in [a["qualified_name"] for a in data.get(ASSETS, headers=ADMIN, params={"q": "受注", "include_deleted": "true"}).json()]
    assert [a["qualified_name"] for a in data.get(ASSETS, headers=ADMIN, params={"q": "受注", "domain": "none"}).json()] == []


def _search_statement(client, q="orders"):
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "count(" in statement and "lower" in statement:
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        client.get(ASSETS, headers=ADMIN, params={"q": q})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert captured
    return captured[0]


@only_postgres
def test_explain_uses_the_indexes(data, forced_index):
    statement, parameters = _search_statement(data)
    with engine.connect() as conn:
        plan = "\n".join(row[0] for row in conn.exec_driver_sql(f"EXPLAIN {statement}", parameters))
    # 資産の 4 列をまとめて索引で引く (全行の照合にならない)
    assert "BitmapOr" in plan and all(name in plan for name in SEARCH_INDEXES if name.startswith("ix_data_assets")), plan
    assert "Seq Scan on data_assets" not in plan, plan


@only_postgres
@pytest.mark.parametrize("column, index", [("name", "ix_glossary_terms_name_trgm"), ("definition", "ix_glossary_terms_definition_trgm")])
def test_glossary_expressions_match_the_indexes(client, forced_index, column, index):
    """用語は件数が少なく Planner が索引を選ばないことがあるので、式が索引と一致することを単独で確かめる。"""
    with engine.connect() as conn:
        plan = "\n".join(row[0] for row in conn.execute(text(f"EXPLAIN SELECT id FROM glossary_terms WHERE lower({column}) LIKE '%orders%' ESCAPE '!'")))
    assert index in plan, plan


# ---------------------------------------------------------------- マイグレーション
def test_migration_and_status_list_the_same_indexes():
    assert set(migration.INDEXES) == SEARCH_INDEXES


def test_only_the_conditional_indexes_are_excluded_from_model_comparison():
    """モデルとの差分の検査を弱めていないこと: 除くのは条件付きの 6 個の索引だけ。"""
    assert len(SEARCH_INDEXES) == 6
    assert all(not migrate.include_object(None, name, "index", True, None) for name in SEARCH_INDEXES)
    assert migrate.include_object(None, "ix_other", "index", True, None) and migrate.include_object(None, "ix_data_assets_name_trgm", "table", True, None)


@only_postgres
def test_downgrade_keeps_extension_and_upgrade_restores_indexes(client):
    def state():
        with engine.connect() as conn:
            return (conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")).scalar_one(),
                    search_index_warning(conn))

    assert state() == (1, None)
    migrate.downgrade(engine, "0027")
    assert state() == (1, DEGRADED)
    migrate.upgrade(engine)
    assert state() == (1, None)


@only_postgres
@pytest.mark.parametrize("extension_exists", [False, True], ids=["cannot-create-extension", "cannot-create-index"])
def test_migration_succeeds_without_privileges(client, extension_exists):
    """権限が無くても例外にせず、トランザクションは使えるまま。"""
    _drop_indexes()
    with engine.connect() as conn:
        with conn.begin():
            if not extension_exists:
                conn.execute(text("DROP EXTENSION IF EXISTS pg_trgm"))
            conn.execute(text("CREATE ROLE edgf_no_extension NOSUPERUSER"))
            conn.execute(text("SET LOCAL ROLE edgf_no_extension"))
            assert migration.create_search_indexes(conn) is False
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
            conn.execute(text("RESET ROLE"))
            assert search_index_warning(conn) == DEGRADED
            conn.execute(text("DROP ROLE edgf_no_extension"))


# ---------------------------------------------------------------- /ready
def test_ready_has_no_warning_when_healthy(client):
    body = client.get("/ready").json()
    assert body["status"] == "ready" and "search_index" not in body["checks"]


def test_sqlite_is_never_degraded(client):
    if POSTGRES:
        pytest.skip("SQLite だけ")
    with engine.connect() as conn:
        assert search_index_warning(conn) is None


@only_postgres
def test_ready_warns_but_stays_200_when_indexes_are_missing(client):
    _drop_indexes()
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["search_index"] == "degraded" and r.json()["status"] == "ready"


@only_postgres
def test_one_missing_or_invalid_index_is_degraded(client):
    name = sorted(SEARCH_INDEXES)[0]
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{name}'::regclass"))
        assert search_index_warning(conn) == DEGRADED
        conn.execute(text(f"UPDATE pg_index SET indisvalid = true WHERE indexrelid = '{name}'::regclass"))
        conn.execute(text(f"DROP INDEX {name}"))
        assert search_index_warning(conn) == DEGRADED


def test_ready_survives_a_failing_index_check(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from edgf.api import ops

    def boom(conn):
        raise OperationalError("x", {}, Exception("x"))

    monkeypatch.setattr(ops, "search_index_warning", boom)
    r = client.get("/ready")
    assert r.status_code == 200 and "search_index" not in r.json()["checks"]
