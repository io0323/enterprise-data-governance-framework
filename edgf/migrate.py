"""§19 Maintainability: Alembic によるスキーマ版管理のプログラム用インターフェース.

CLI からは `alembic upgrade head`(リポジトリ直下の alembic.ini)を使う。
アプリ/テストからは本モジュールを使う(パッケージ同梱の migrations を参照するため ini 不要)。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# PostgreSQL だけに条件付きで作る索引 (0028)。モデルに置けないので、モデルとの差分の比較から除く
CONDITIONAL_INDEXES = frozenset({"ix_data_assets_name_trgm", "ix_data_assets_qualified_name_trgm", "ix_data_assets_description_trgm", "ix_data_assets_tags_trgm",
                                 "ix_glossary_terms_name_trgm", "ix_glossary_terms_definition_trgm"})


def include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """alembic の autogenerate に渡すフィルタ。"""
    return not (type_ == "index" and name in CONDITIONAL_INDEXES)


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # configparser の補間を回避
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()
