"""検索用の pg_trgm の GIN 索引 (PostgreSQL のみ)

拡張を作れない環境 (権限が無い、contrib が入っていないなど) では、索引を作らずに成功する。
検索は索引なしでも同じ結果を返し、/ready の checks に search_index: degraded を出す。
あとから有効にするには、拡張を作れるようにしてから `alembic downgrade 0027 && alembic upgrade head` を実行する。

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-09
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = '0028'
down_revision: str | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("edgf.migrate")

# 式は edgf/api/catalog.py の検索の条件と一致させる (違うと索引が使われない)
INDEXES = {
    "ix_data_assets_name_trgm": ("data_assets", "lower(name)"),
    "ix_data_assets_qualified_name_trgm": ("data_assets", "lower(qualified_name)"),
    "ix_data_assets_description_trgm": ("data_assets", "lower(description)"),
    "ix_data_assets_tags_trgm": ("data_assets", "lower(CAST(tags AS VARCHAR))"),
    "ix_glossary_terms_name_trgm": ("glossary_terms", "lower(name)"),
    "ix_glossary_terms_definition_trgm": ("glossary_terms", "lower(definition)"),
}


def create_search_indexes(conn: Connection) -> bool:
    """拡張と索引を作る。できなければセーブポイントまで戻して False を返す (マイグレーションは続ける)。"""
    try:
        with conn.begin_nested():
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for name, (table, expression) in INDEXES.items():
                conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({expression} gin_trgm_ops)"))
    except sa.exc.DBAPIError as e:
        log.warning("pg_trgm の索引を作れませんでした。検索は索引なしで動作します", extra={"fields": {"error": type(e.orig).__name__}})
        return False
    return True


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        create_search_indexes(conn)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":  # 拡張は他で使っている可能性があるので残す
        for name in INDEXES:
            conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
