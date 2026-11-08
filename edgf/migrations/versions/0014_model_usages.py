"""model_usages: モデル版の利用先 (§14 AI Lineage)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('model_usages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('model_version_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('target', sa.String(length=300), nullable=False),
    sa.Column('target_asset_id', sa.String(length=36), nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'], name=op.f('fk_model_usages_model_version_id_model_versions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_asset_id'], ['data_assets.id'], name=op.f('fk_model_usages_target_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_usages'))
    )
    op.create_index(op.f('ix_model_usages_model_version_id'), 'model_usages', ['model_version_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_model_usages_model_version_id'), table_name='model_usages')
    op.drop_table('model_usages')
