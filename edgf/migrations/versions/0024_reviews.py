"""review_campaigns / review_items / policy_reviews: 定期レビュー (§16 Review / §11)

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('review_campaigns',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('period', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('grace_until', sa.DateTime(), nullable=False),
    sa.Column('item_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('closed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_campaigns')),
    sa.UniqueConstraint('kind', 'period', name='uq_review_campaign')
    )
    op.create_index(op.f('ix_review_campaigns_status'), 'review_campaigns', ['status'], unique=False)
    op.create_table('review_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('campaign_id', sa.String(length=36), nullable=False),
    sa.Column('target_type', sa.String(length=30), nullable=False),
    sa.Column('target_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('approval_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['approval_id'], ['approval_requests.id'], name=op.f('fk_review_items_approval_id_approval_requests')),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_review_items_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['campaign_id'], ['review_campaigns.id'], name=op.f('fk_review_items_campaign_id_review_campaigns')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_items')),
    sa.UniqueConstraint('approval_id', name=op.f('uq_review_items_approval_id')),
    sa.UniqueConstraint('campaign_id', 'target_type', 'target_id', name='uq_review_item')
    )
    for column in ('asset_id', 'campaign_id', 'status'):
        op.create_index(op.f(f'ix_review_items_{column}'), 'review_items', [column], unique=False)
    op.create_table('policy_reviews',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('policy_id', sa.String(length=50), nullable=False),
    sa.Column('reviewed_by', sa.String(length=200), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_reviews'))
    )
    op.create_index(op.f('ix_policy_reviews_policy_id'), 'policy_reviews', ['policy_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_policy_reviews_policy_id'), table_name='policy_reviews')
    op.drop_table('policy_reviews')
    for column in ('status', 'campaign_id', 'asset_id'):
        op.drop_index(op.f(f'ix_review_items_{column}'), table_name='review_items')
    op.drop_table('review_items')
    op.drop_index(op.f('ix_review_campaigns_status'), table_name='review_campaigns')
    op.drop_table('review_campaigns')
