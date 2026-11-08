"""Database engine/session. Portable across SQLite / PostgreSQL.

スキーマは Alembic マイグレーション (edgf/migrations) で管理する。create_all は使用しない。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from edgf.config import settings

# 制約名を DB 方言に依存せず決定的にする (マイグレーションでの ALTER/DROP を可能にするため)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)
        # pysqlite は DML まで BEGIN を発行しないので、先頭の SAVEPOINT の RELEASE が確定になってしまう。
        # トランザクションを明示して、監査の再試行で使うセーブポイントを PostgreSQL と同じ意味にする
        event.listen(engine, "connect", lambda conn, _: setattr(conn, "isolation_level", None))
        event.listen(engine, "begin", lambda conn: conn.exec_driver_sql("BEGIN"))
        return engine
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
