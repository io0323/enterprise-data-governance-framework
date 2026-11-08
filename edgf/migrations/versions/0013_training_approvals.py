"""training_approvals / training_approval_decisions: 学習利用の承認 (§14 Training Data)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('training_approvals',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('dataset_version_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('license_status', sa.String(length=20), nullable=False),
    sa.Column('license_ref', sa.String(length=2000), nullable=False),
    sa.Column('bias_evaluation_ref', sa.String(length=2000), nullable=False),
    sa.Column('consent_summary', sa.JSON(), nullable=False),
    sa.Column('quality_summary', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], name=op.f('fk_training_approvals_dataset_version_id_dataset_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_training_approvals'))
    )
    op.create_index(op.f('ix_training_approvals_dataset_version_id'), 'training_approvals', ['dataset_version_id'], unique=False)
    op.create_index(op.f('ix_training_approvals_status'), 'training_approvals', ['status'], unique=False)
    op.create_table('training_approval_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('approval_id', sa.String(length=36), nullable=False),
    sa.Column('slot', sa.String(length=210), nullable=False),
    sa.Column('required_subject', sa.String(length=200), nullable=True),
    sa.Column('asset_ids', sa.JSON(), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('comment', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['training_approvals.id'], name=op.f('fk_training_approval_decisions_approval_id_training_approvals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_training_approval_decisions')),
    sa.UniqueConstraint('approval_id', 'slot', name='uq_training_approval_slot')
    )
    op.create_index(op.f('ix_training_approval_decisions_approval_id'), 'training_approval_decisions', ['approval_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_training_approval_decisions_approval_id'), table_name='training_approval_decisions')
    op.drop_table('training_approval_decisions')
    op.drop_index(op.f('ix_training_approvals_status'), table_name='training_approvals')
    op.drop_index(op.f('ix_training_approvals_dataset_version_id'), table_name='training_approvals')
    op.drop_table('training_approvals')
