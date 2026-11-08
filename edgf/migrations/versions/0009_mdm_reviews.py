"""mdm_reviews: Steward の裁定キュー。比較結果だけを持つ (§8 / §18.2)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('mdm_reviews',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('source_ref', sa.String(length=64), nullable=False),
    sa.Column('source_system', sa.String(length=100), nullable=False),
    sa.Column('golden_id', sa.String(length=36), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('comparisons', sa.JSON(), nullable=False),
    sa.Column('subject_refs', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('new_golden_id', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_mdm_reviews_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['golden_id'], ['golden_records.id'], name=op.f('fk_mdm_reviews_golden_id_golden_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mdm_reviews')),
    sa.UniqueConstraint('entity_type', 'source_ref', 'golden_id', name='uq_mdm_review_pair')
    )
    op.create_index(op.f('ix_mdm_reviews_source_ref'), 'mdm_reviews', ['source_ref'], unique=False)
    op.create_index(op.f('ix_mdm_reviews_status'), 'mdm_reviews', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mdm_reviews_status'), table_name='mdm_reviews')
    op.drop_index(op.f('ix_mdm_reviews_source_ref'), table_name='mdm_reviews')
    op.drop_table('mdm_reviews')
