"""検索の索引の状態 (/ready の警告に使う)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from edgf.migrate import CONDITIONAL_INDEXES

SEARCH_INDEXES = CONDITIONAL_INDEXES  # edgf/migrations/versions/0028_search_trgm_indexes.py が作る索引
DEGRADED = "degraded"


def search_index_warning(conn: Connection) -> str | None:
    """PostgreSQL で索引が揃っていなければ degraded を返す。SQLite には索引が無く、警告の対象にしない。"""
    if conn.dialect.name != "postgresql":
        return None
    # current_schemas で検索パスの範囲に限る (別のスキーマの同名の索引を数えない)。無効な索引も数えない
    rows = conn.execute(text("SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid WHERE i.indisvalid AND c.relname LIKE '%\\_trgm' "
                             "AND c.relnamespace = ANY (SELECT oid FROM pg_namespace WHERE nspname = ANY (current_schemas(false)))"))
    return None if SEARCH_INDEXES <= {name for name, in rows} else DEGRADED
