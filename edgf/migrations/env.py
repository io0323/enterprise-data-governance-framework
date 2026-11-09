"""Alembic environment for the Governance Control Plane.

接続先の優先順位: (1) config.attributes["connection"](アプリ/テストからの共有接続)
(2) alembic.ini / Config の sqlalchemy.url (3) EDGF_DATABASE_URL。
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from edgf import models  # noqa: F401  (register tables on Base.metadata)
from edgf.config import settings
from edgf.db import Base, make_engine
from edgf.migrate import include_object

config = context.config
if config.config_file_name:
    # 既存ロガー(edgf の構造化ログ等)を無効化しない
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite の ALTER 制約を回避
    )


def run_migrations_offline() -> None:
    url = _url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = config.attributes.get("connection")
    if shared is not None:
        _configure(shared)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url())
    try:
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
