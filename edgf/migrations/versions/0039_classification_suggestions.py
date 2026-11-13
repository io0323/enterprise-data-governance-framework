"""classification_suggestions: メタデータからの分類の提案 (§3 / §12 / §20 Level 5。Phase 19)

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: str | None = '0038'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('classification_suggestions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('column', sa.String(length=200), nullable=False),
    sa.Column('proposal', sa.JSON(), nullable=False),
    sa.Column('current', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('evidence_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('approval_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_classification_suggestions_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_classification_suggestions'))
    )
    for column in ('asset_id', 'status', 'created_at'):
        op.create_index(op.f(f'ix_classification_suggestions_{column}'), 'classification_suggestions', [column], unique=False)
    op.create_index('ix_classification_suggestions_target', 'classification_suggestions', ['asset_id', 'column', 'evidence_hash'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_classification_suggestions_target', table_name='classification_suggestions')
    for column in ('created_at', 'status', 'asset_id'):
        op.drop_index(op.f(f'ix_classification_suggestions_{column}'), table_name='classification_suggestions')
    op.drop_table('classification_suggestions')
