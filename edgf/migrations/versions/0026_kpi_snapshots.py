"""kpi_snapshots: KPI の日次のスナップショット (§17)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0026'
down_revision: str | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('kpi_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('scope_type', sa.String(length=20), nullable=False),
    sa.Column('scope_name', sa.String(length=100), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_kpi_snapshots')),
    sa.UniqueConstraint('day', 'scope_type', 'scope_name', name='uq_kpi_snapshot')
    )
    op.create_index(op.f('ix_kpi_snapshots_day'), 'kpi_snapshots', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_kpi_snapshots_day'), table_name='kpi_snapshots')
    op.drop_table('kpi_snapshots')
