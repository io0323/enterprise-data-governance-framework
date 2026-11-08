"""erasure_requests / erasure_tasks: Right to Erasure (§12 / §10 Delete)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('erasure_requests',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('subject_ref', sa.String(length=64), nullable=False),
    sa.Column('identifier_type', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('scoped_at', sa.DateTime(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('dispatched_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('verified_at', sa.DateTime(), nullable=True),
    sa.Column('verified_by', sa.String(length=200), nullable=True),
    sa.Column('closed_at', sa.DateTime(), nullable=True),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.Column('unregistered_downstream', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erasure_requests'))
    )
    op.create_index(op.f('ix_erasure_requests_status'), 'erasure_requests', ['status'], unique=False)
    op.create_index(op.f('ix_erasure_requests_subject_ref'), 'erasure_requests', ['subject_ref'], unique=False)
    op.create_table('erasure_tasks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('qualified_name', sa.String(length=300), nullable=False),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('depth', sa.Integer(), nullable=False),
    sa.Column('columns', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('withheld_reason', sa.Text(), nullable=True),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('acked_by', sa.String(length=200), nullable=True),
    sa.Column('acked_at', sa.DateTime(), nullable=True),
    sa.Column('completed_by', sa.String(length=200), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('completion_request_id', sa.String(length=128), nullable=True),
    sa.Column('records_deleted', sa.Integer(), nullable=True),
    sa.Column('evidence_hash', sa.String(length=64), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_erasure_tasks_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['request_id'], ['erasure_requests.id'], name=op.f('fk_erasure_tasks_request_id_erasure_requests'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erasure_tasks')),
    sa.UniqueConstraint('request_id', 'asset_id', name='uq_erasure_task_request_asset')
    )
    op.create_index(op.f('ix_erasure_tasks_asset_id'), 'erasure_tasks', ['asset_id'], unique=False)
    op.create_index(op.f('ix_erasure_tasks_request_id'), 'erasure_tasks', ['request_id'], unique=False)
    op.create_index(op.f('ix_erasure_tasks_status'), 'erasure_tasks', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_erasure_tasks_status'), table_name='erasure_tasks')
    op.drop_index(op.f('ix_erasure_tasks_request_id'), table_name='erasure_tasks')
    op.drop_index(op.f('ix_erasure_tasks_asset_id'), table_name='erasure_tasks')
    op.drop_table('erasure_tasks')
    op.drop_index(op.f('ix_erasure_requests_subject_ref'), table_name='erasure_requests')
    op.drop_index(op.f('ix_erasure_requests_status'), table_name='erasure_requests')
    op.drop_table('erasure_requests')
