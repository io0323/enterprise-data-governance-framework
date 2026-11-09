"""検索の索引の状態 (/ready の警告に使う)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# edgf/migrations/versions/0028_search_trgm_indexes.py が作る索引
SEARCH_INDEXES = frozenset({"ix_data_assets_name_trgm", "ix_data_assets_qualified_name_trgm", "ix_data_assets_description_trgm", "ix_data_assets_tags_trgm",
                            "ix_glossary_terms_name_trgm", "ix_glossary_terms_definition_trgm"})
DEGRADED = "degraded"


def search_index_warning(conn: Connection) -> str | None:
    """PostgreSQL で索引が揃っていなければ degraded を返す。SQLite には索引が無く、警告の対象にしない。"""
    if conn.dialect.name != "postgresql":
        return None
    # current_schemas で検索パスの範囲に限る (別のスキーマの同名の索引を数えない)。無効な索引も数えない
    rows = conn.execute(text("SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid WHERE i.indisvalid AND c.relname LIKE '%\\_trgm' "
                             "AND c.relnamespace = ANY (SELECT oid FROM pg_namespace WHERE nspname = ANY (current_schemas(false)))"))
    return None if SEARCH_INDEXES <= {name for name, in rows} else DEGRADED
