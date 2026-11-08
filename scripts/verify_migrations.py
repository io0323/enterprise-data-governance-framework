#!/usr/bin/env python
"""マイグレーションの往復を実 DB で検証する (CI と手元で使う)。

    EDGF_DATABASE_URL=postgresql+psycopg://... python scripts/verify_migrations.py [--reset]

空の DB に対して次を確認する。空でなければ --reset を付けない限り中止する (全テーブルを削除するため)。
  1. upgrade head → downgrade base → upgrade head
  2. 全リビジョンで 1 つずつ downgrade / upgrade
  3. 既存の行がある状態で 0001 → head → 0001 と往復しても行が残ること
  4. head のスキーマがモデルと一致すること (autogenerate の差分がゼロ)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EDGF_AUTH_MODE", "dev")

from edgf import models  # noqa: E402,F401
from edgf.db import Base  # noqa: E402
from edgf.migrate import alembic_config  # noqa: E402

ALEMBIC = str(Path(sys.executable).parent / "alembic")


def alembic(*args: str) -> None:
    res = subprocess.run([ALEMBIC, *args], cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"FAILED: alembic {' '.join(args)}\n{res.stderr}")
    print(f"  ok: alembic {' '.join(args)}")


def main() -> None:
    url = os.environ.get("EDGF_DATABASE_URL")
    if not url:
        sys.exit("EDGF_DATABASE_URL を指定してください")
    engine = create_engine(url)
    print(f"dialect: {engine.dialect.name}")

    def tables() -> set[str]:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}

    def version() -> str | None:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()

    if tables() or version():
        if "--reset" not in sys.argv:
            sys.exit("DB が空ではありません。全テーブルを削除してよければ --reset を付けてください")
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        if tables():  # モデルに無い残骸 (以前のリビジョンなど)
            alembic("stamp", "head")
            alembic("downgrade", "base")

    script = ScriptDirectory.from_config(alembic_config())
    revisions = [r.revision for r in script.walk_revisions()][::-1]
    head = script.get_current_head()
    print(f"revisions: {revisions}")

    print("[1] upgrade head → downgrade base → upgrade head")
    alembic("upgrade", "head")
    assert version() == head and set(Base.metadata.tables) <= tables(), "head のテーブルが足りません"
    alembic("downgrade", "base")
    assert not tables(), f"base にテーブルが残っています: {tables()}"
    alembic("upgrade", "head")
    assert version() == head

    print("[2] 全リビジョンで 1 つずつ downgrade / upgrade")
    for expected in revisions[-2::-1] + [None]:
        alembic("downgrade", "-1")
        assert version() == expected, (version(), expected)
        alembic("upgrade", "+1")
        alembic("downgrade", "-1")
        assert version() == expected
    assert not tables()
    for expected in revisions:
        alembic("upgrade", "+1")
        assert version() == expected

    print("[3] 既存の行がある状態での往復")
    alembic("downgrade", "base")
    alembic("upgrade", revisions[0])
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_assets (id, qualified_name, name, description, data_class, sensitivity, lifecycle_state,"
            " certification, is_cde, contains_pii, legal_hold, schema, tags, controls, created_at, updated_at)"
            " VALUES ('a1', 'verify.asset', 'v', '', 'master', 'internal', 'draft', 'draft', :f, :t, :f,"
            " '[{\"name\": \"email\", \"pii\": true}]', '[]', '[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"),
            {"f": False, "t": True})
        conn.execute(text(
            "INSERT INTO audit_events (ts, actor, action, entity_type, entity_id, payload, prev_hash, hash)"
            " VALUES ('2026-01-01 00:00:00', 'x', 'y', 'z', '1', '{}', :p, :h)"), {"p": "0" * 64, "h": "1" * 64})

    def count(table: str) -> int:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()

    alembic("upgrade", "head")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT privacy_technique, privacy_params FROM data_assets")).one()
        assert row[0] == "none", row
    assert count("data_assets") == 1 and count("audit_events") == 1
    alembic("downgrade", revisions[0])
    assert count("data_assets") == 1 and count("audit_events") == 1
    alembic("upgrade", "head")
    assert count("data_assets") == 1

    print("[4] モデルとの差分")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    if diff:
        sys.exit(f"FAILED: モデルとマイグレーションに差分があります: {diff}")
    print("  ok: 差分なし")
    alembic("downgrade", "base")
    assert not tables()
    print("ALL OK")


if __name__ == "__main__":
    main()
