from __future__ import annotations

import logging
import os

# 既定はインメモリの SQLite。EDGF_TEST_DATABASE_URL で PostgreSQL などに向けられる (テストは全テーブルを削除する)
os.environ["EDGF_DATABASE_URL"] = os.environ.get("EDGF_TEST_DATABASE_URL", "sqlite://")
# 既存テストは dev モード (X-Actor ヘッダ) で実行する。JWT 経路は tests/test_auth.py で検証する
os.environ["EDGF_AUTH_MODE"] = "dev"
os.environ.setdefault("EDGF_ENV", "test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from edgf import migrate
from edgf.db import Base, engine
from edgf.main import create_app
from edgf.observability import JsonFormatter


def reset_db() -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    _schema.clear()


# ---------------------------------------------------------------- スキーマはセッションで 1 回、データはテストごとに消す
# 以前はテストごとに全テーブルを削除して Alembic のマイグレーションを全部流していた (PostgreSQL で 1 テスト約 1.1 秒)。
# いまはスキーマを 1 回だけ作り、テストの開始前に全テーブルの行を消して連番を戻す (PostgreSQL で約 5 ミリ秒)。
# スキーマの指紋 (表と索引の数、Alembic の版) が変わっていたら作り直すので、DDL を行うテストのあとでも安全。
# 監査チェーン、並行実行、DDL を扱うテストは @pytest.mark.fresh_schema で従来どおりテストごとに作り直す。
_schema: dict[str, object] = {}


def _fingerprint() -> tuple:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if engine.dialect.name == "postgresql":
            objects = conn.execute(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                                        "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'i', 'S')")).scalar()
        else:
            objects = conn.execute(text("SELECT count(*) FROM sqlite_master")).scalar()
    return version, objects


def build_schema() -> None:
    """スキーマは本番と同じ Alembic マイグレーションで構築する (create_all は使わない)。"""
    reset_db()
    migrate.upgrade(engine)
    _schema["fingerprint"] = _fingerprint()
    # マイグレーションが入れる初期データ (標準の Purpose など)。消したあとに戻す
    with engine.connect() as conn:
        _schema["seed"] = [(t, [dict(r._mapping) for r in conn.execute(t.select())]) for t in Base.metadata.sorted_tables]
    _schema["seed"] = [(t, rows) for t, rows in _schema["seed"] if rows]
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            _schema["sequences"] = [r[0] for r in conn.execute(text(
                "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = current_schema()"))]


def clear_data() -> None:
    """全テーブルの行を消し、連番を戻し、マイグレーションの初期データを入れ直す (監査の seq などがテストごとに 1 から始まるように)。"""
    tables = [t.name for t in reversed(Base.metadata.sorted_tables)]  # 外部キーの依存の逆順
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            statements = [f'DELETE FROM "{t}"' for t in tables] + [f'ALTER SEQUENCE "{s}" RESTART' for s in _schema["sequences"]]
            conn.exec_driver_sql(";".join(statements))  # 1 回の往復で送る
        else:
            for t in tables:
                conn.exec_driver_sql(f'DELETE FROM "{t}"')
            if conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").first():
                conn.exec_driver_sql("DELETE FROM sqlite_sequence")
        for table, rows in _schema["seed"]:
            conn.execute(table.insert(), rows)


def prepare_database(fresh: bool) -> None:
    try:
        reusable = not fresh and _schema.get("fingerprint") == _fingerprint()
    except Exception:  # noqa: BLE001 - スキーマが無い (DDL を行うテストが消した) 場合も作り直す
        reusable = False
    if reusable:
        clear_data()  # 終了時ではなく開始前に消す (異常終了したテストの残りも引き継がない)
    else:
        build_schema()


@pytest.fixture()
def client(request):
    fresh = request.node.get_closest_marker("fresh_schema") is not None
    prepare_database(fresh)
    # 既定ヘッダ = MVP 時代の「誰でも全操作可」に相当する dev 操作者 (個別リクエストの H(...) で上書き)
    with TestClient(create_app(), headers=H()) as c:
        yield c
    if fresh:
        reset_db()  # 従来の方式: 終了時にも削除する


def pytest_configure(config):
    config.addinivalue_line("markers", "fresh_schema: テストごとにスキーマを作り直す (監査チェーン、並行実行、DDL を扱うテスト)")


ALL_ROLES = "admin,committee,owner,steward,custodian"


def H(actor: str = "alice", roles: str = ALL_ROLES, domains: str = "*") -> dict:
    return {"X-Actor": actor, "X-Roles": roles, "X-Domains": domains}


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


def dump_database() -> str:
    """全テーブルの全行を文字列化する (生の識別子が残っていないことの検査用)。"""
    with engine.connect() as conn:
        rows = [str(tuple(row)) for table in Base.metadata.sorted_tables for row in conn.execute(table.select())]
    return "\n".join(rows).lower()


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def text(self) -> str:
        return "\n".join(self.lines).lower()


@pytest.fixture()
def edgf_logs():
    h = LogCapture()
    logger = logging.getLogger("edgf")
    logger.addHandler(h)
    yield h
    logger.removeHandler(h)
