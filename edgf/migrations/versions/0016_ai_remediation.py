"""model_remediations / ai_incidents、消去への AI 資産の伝播 (§12 / §14)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('erasure_requests') as batch_op:
        batch_op.add_column(sa.Column('ai_impact', sa.JSON(), nullable=False, server_default='{}'))
    with op.batch_alter_table('erasure_tasks') as batch_op:
        batch_op.add_column(sa.Column('ai_kind', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('dataset_version_ids', sa.JSON(), nullable=False, server_default='[]'))
    op.create_table('model_remediations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('model_version_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('erasure_request_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('accept_until', sa.DateTime(), nullable=True),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('completed_by', sa.String(length=200), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('replacement_model_version_id', sa.String(length=36), nullable=True),
    sa.Column('evidence_hash', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_model_remediations_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['erasure_request_id'], ['erasure_requests.id'], name=op.f('fk_model_remediations_erasure_request_id_erasure_requests')),
    sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'], name=op.f('fk_model_remediations_model_version_id_model_versions')),
    sa.ForeignKeyConstraint(['replacement_model_version_id'], ['model_versions.id'], name=op.f('fk_model_remediations_replacement_model_version_id_model_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_remediations')),
    sa.UniqueConstraint('model_version_id', 'erasure_request_id', name='uq_model_remediation')
    )
    op.create_index(op.f('ix_model_remediations_asset_id'), 'model_remediations', ['asset_id'], unique=False)
    op.create_index(op.f('ix_model_remediations_erasure_request_id'), 'model_remediations', ['erasure_request_id'], unique=False)
    op.create_index(op.f('ix_model_remediations_model_version_id'), 'model_remediations', ['model_version_id'], unique=False)
    op.create_index(op.f('ix_model_remediations_status'), 'model_remediations', ['status'], unique=False)
    op.create_table('ai_incidents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('origin_asset_id', sa.String(length=36), nullable=False),
    sa.Column('origin_dataset_version_id', sa.String(length=36), nullable=True),
    sa.Column('affected_model_version_ids', sa.JSON(), nullable=False),
    sa.Column('suspended_model_version_ids', sa.JSON(), nullable=False),
    sa.Column('issue_ids', sa.JSON(), nullable=False),
    sa.Column('reported_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['origin_asset_id'], ['data_assets.id'], name=op.f('fk_ai_incidents_origin_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['origin_dataset_version_id'], ['dataset_versions.id'], name=op.f('fk_ai_incidents_origin_dataset_version_id_dataset_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_incidents'))
    )
    op.create_index(op.f('ix_ai_incidents_origin_asset_id'), 'ai_incidents', ['origin_asset_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_incidents_origin_asset_id'), table_name='ai_incidents')
    op.drop_table('ai_incidents')
    op.drop_index(op.f('ix_model_remediations_status'), table_name='model_remediations')
    op.drop_index(op.f('ix_model_remediations_model_version_id'), table_name='model_remediations')
    op.drop_index(op.f('ix_model_remediations_erasure_request_id'), table_name='model_remediations')
    op.drop_index(op.f('ix_model_remediations_asset_id'), table_name='model_remediations')
    op.drop_table('model_remediations')
    with op.batch_alter_table('erasure_tasks') as batch_op:
        batch_op.drop_column('dataset_version_ids')
        batch_op.drop_column('ai_kind')
    with op.batch_alter_table('erasure_requests') as batch_op:
        batch_op.drop_column('ai_impact')
