"""approval_requests / approval_decisions: 汎用の承認エンジン (§16 Approval)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('approval_requests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('target_type', sa.String(length=50), nullable=False),
    sa.Column('target_id', sa.String(length=300), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=True),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('current_step', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_approval_requests_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_requests'))
    )
    for column in ('asset_id', 'requested_by', 'status', 'target_id', 'type'):
        op.create_index(op.f(f'ix_approval_requests_{column}'), 'approval_requests', [column], unique=False)
    op.create_table('approval_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=False),
    sa.Column('approve', sa.Boolean(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['request_id'], ['approval_requests.id'], name=op.f('fk_approval_decisions_request_id_approval_requests')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_decisions')),
    sa.UniqueConstraint('request_id', 'decided_by', name='uq_approval_decision')
    )
    op.create_index(op.f('ix_approval_decisions_request_id'), 'approval_decisions', ['request_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_approval_decisions_request_id'), table_name='approval_decisions')
    op.drop_table('approval_decisions')
    for column in ('type', 'target_id', 'status', 'requested_by', 'asset_id'):
        op.drop_index(op.f(f'ix_approval_requests_{column}'), table_name='approval_requests')
    op.drop_table('approval_requests')
