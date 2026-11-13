"""quality_anomalies: 品質の異常の検知の結果 (§5 / §20 Level 5。Phase 19)

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0038'
down_revision: str | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('quality_anomalies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('metric', sa.String(length=100), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('fingerprint', sa.String(length=300), nullable=False),
    sa.Column('detected_at', sa.DateTime(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_quality_anomalies_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_anomalies')),
    sa.UniqueConstraint('fingerprint', name=op.f('uq_quality_anomalies_fingerprint'))
    )
    for column in ('asset_id', 'kind', 'detected_at'):
        op.create_index(op.f(f'ix_quality_anomalies_{column}'), 'quality_anomalies', [column], unique=False)


def downgrade() -> None:
    for column in ('detected_at', 'kind', 'asset_id'):
        op.drop_index(op.f(f'ix_quality_anomalies_{column}'), table_name='quality_anomalies')
    op.drop_table('quality_anomalies')
