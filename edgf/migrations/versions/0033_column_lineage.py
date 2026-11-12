"""column_lineage_edges: カラム単位のリネージ (§9。CDE はカラム単位を必須)

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0033'
down_revision: str | None = '0032'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('column_lineage_edges',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=300), nullable=False),
    sa.Column('source_column', sa.String(length=200), nullable=False),
    sa.Column('target', sa.String(length=300), nullable=False),
    sa.Column('target_column', sa.String(length=200), nullable=True),
    sa.Column('job', sa.String(length=300), nullable=False),
    sa.Column('type', sa.String(length=10), nullable=False),
    sa.Column('subtype', sa.String(length=30), nullable=True),
    sa.Column('masking', sa.Boolean(), nullable=False),
    sa.Column('last_run_id', sa.String(length=100), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_column_lineage_edges'))
    )
    op.create_index('ix_column_lineage_source', 'column_lineage_edges', ['source', 'source_column'], unique=False)
    op.create_index('ix_column_lineage_target', 'column_lineage_edges', ['target', 'target_column'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_column_lineage_target', table_name='column_lineage_edges')
    op.drop_index('ix_column_lineage_source', table_name='column_lineage_edges')
    op.drop_table('column_lineage_edges')
