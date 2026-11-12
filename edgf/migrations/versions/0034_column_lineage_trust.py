"""カラム単位のリネージの信頼できる生成元: 取り込みの経路と主体、消去で外した資産の記録

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0034'
down_revision: str | None = '0033'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('column_lineage_edges') as batch:
        batch.add_column(sa.Column('via', sa.String(length=20), nullable=True))
        batch.add_column(sa.Column('recorded_by', sa.String(length=200), nullable=True))
    with op.batch_alter_table('erasure_requests') as batch:
        batch.add_column(sa.Column('lineage_narrowing', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('erasure_requests') as batch:
        batch.drop_column('lineage_narrowing')
    with op.batch_alter_table('column_lineage_edges') as batch:
        batch.drop_column('recorded_by')
        batch.drop_column('via')
