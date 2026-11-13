"""autonomy_control / autonomy_playbook_states / remediation_runs: 自律統治の実行の記録と安全装置 (§20 Level 5。Phase 19)

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0037'
down_revision: str | None = '0036'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('autonomy_control',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('stopped', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('changed_by', sa.String(length=200), nullable=True),
    sa.Column('changed_at', sa.DateTime(), nullable=True),
    sa.Column('last_detection_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_autonomy_control'))
    )
    op.create_table('autonomy_playbook_states',
    sa.Column('playbook_id', sa.String(length=40), nullable=False),
    sa.Column('suspended', sa.Boolean(), nullable=False),
    sa.Column('suspended_at', sa.DateTime(), nullable=True),
    sa.Column('suspended_reason', sa.Text(), nullable=False),
    sa.Column('resumed_at', sa.DateTime(), nullable=True),
    sa.Column('resumed_by', sa.String(length=200), nullable=True),
    sa.Column('event_cursor', sa.DateTime(), nullable=True),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('playbook_id', name=op.f('pk_autonomy_playbook_states'))
    )
    op.create_table('remediation_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('batch_id', sa.String(length=36), nullable=False),
    sa.Column('playbook_id', sa.String(length=40), nullable=False),
    sa.Column('playbook_digest', sa.String(length=64), nullable=True),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('trigger', sa.String(length=100), nullable=False),
    sa.Column('target_type', sa.String(length=40), nullable=False),
    sa.Column('target_id', sa.String(length=300), nullable=False),
    sa.Column('fingerprint', sa.String(length=300), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=True),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('category', sa.String(length=1), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('before', sa.JSON(), nullable=False),
    sa.Column('after', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('approval_id', sa.String(length=36), nullable=True),
    sa.Column('issue_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('undo_until', sa.DateTime(), nullable=True),
    sa.Column('undone_at', sa.DateTime(), nullable=True),
    sa.Column('undone_by', sa.String(length=200), nullable=True),
    sa.Column('undo_reason', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_remediation_runs_asset_id_data_assets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_remediation_runs'))
    )
    for column in ('batch_id', 'playbook_id', 'asset_id', 'status', 'created_at'):
        op.create_index(op.f(f'ix_remediation_runs_{column}'), 'remediation_runs', [column], unique=False)
    op.create_index('ix_remediation_runs_target', 'remediation_runs', ['playbook_id', 'target_type', 'fingerprint', 'mode'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_remediation_runs_target', table_name='remediation_runs')
    for column in ('created_at', 'status', 'asset_id', 'playbook_id', 'batch_id'):
        op.drop_index(op.f(f'ix_remediation_runs_{column}'), table_name='remediation_runs')
    op.drop_table('remediation_runs')
    op.drop_table('autonomy_playbook_states')
    op.drop_table('autonomy_control')
