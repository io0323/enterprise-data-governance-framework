"""usage_daily: 資産 × 日の利用の集計値 (§17 Usage)

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0032'
down_revision: str | None = '0031'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('usage_daily',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('query_count', sa.Integer(), nullable=False),
    sa.Column('user_count', sa.Integer(), nullable=False),
    sa.Column('reporter', sa.String(length=200), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_usage_daily_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_daily')),
    sa.UniqueConstraint('asset_id', 'day', name='uq_usage_asset_day')
    )
    op.create_index(op.f('ix_usage_daily_asset_id'), 'usage_daily', ['asset_id'], unique=False)
    op.create_index(op.f('ix_usage_daily_day'), 'usage_daily', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_usage_daily_day'), table_name='usage_daily')
    op.drop_index(op.f('ix_usage_daily_asset_id'), table_name='usage_daily')
    op.drop_table('usage_daily')
