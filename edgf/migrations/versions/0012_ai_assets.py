"""ai_kind / Dataset・Model Card / 版の情報、標準の ai_training Purpose (§14)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from edgf.models import utcnow

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AI_TRAINING = 'ai_training'


def upgrade() -> None:
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.add_column(sa.Column('ai_kind', sa.String(length=20), nullable=True))
        batch_op.create_index(batch_op.f('ix_data_assets_ai_kind'), ['ai_kind'], unique=False)
    op.create_table('dataset_cards',
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('collection_source', sa.Text(), nullable=False),
    sa.Column('license', sa.String(length=200), nullable=False),
    sa.Column('consent_purpose_id', sa.String(length=80), nullable=True),
    sa.Column('known_biases', sa.Text(), nullable=False),
    sa.Column('constraints', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=200), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_dataset_cards_asset_id_data_assets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['consent_purpose_id'], ['privacy_purposes.id'], name=op.f('fk_dataset_cards_consent_purpose_id_privacy_purposes')),
    sa.PrimaryKeyConstraint('asset_id', name=op.f('pk_dataset_cards'))
    )
    op.create_table('model_cards',
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('intended_use', sa.Text(), nullable=False),
    sa.Column('out_of_scope_use', sa.Text(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('bias_evaluation_ref', sa.String(length=2000), nullable=False),
    sa.Column('constraints', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=200), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_model_cards_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('asset_id', name=op.f('pk_model_cards'))
    )
    op.create_table('dataset_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('record_count', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.DateTime(), nullable=True),
    sa.Column('period_end', sa.DateTime(), nullable=True),
    sa.Column('external_uri', sa.String(length=2000), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('registered_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('superseded_at', sa.DateTime(), nullable=True),
    sa.Column('superseded_reason', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_dataset_versions_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dataset_versions')),
    sa.UniqueConstraint('asset_id', 'version', name='uq_dataset_version')
    )
    op.create_index(op.f('ix_dataset_versions_asset_id'), 'dataset_versions', ['asset_id'], unique=False)
    op.create_index(op.f('ix_dataset_versions_status'), 'dataset_versions', ['status'], unique=False)
    op.create_table('dataset_version_sources',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('dataset_version_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('qualified_name', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_dataset_version_sources_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], name=op.f('fk_dataset_version_sources_dataset_version_id_dataset_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dataset_version_sources')),
    sa.UniqueConstraint('dataset_version_id', 'asset_id', 'kind', 'version', name='uq_dataset_version_source')
    )
    op.create_index(op.f('ix_dataset_version_sources_asset_id'), 'dataset_version_sources', ['asset_id'], unique=False)
    op.create_index(op.f('ix_dataset_version_sources_dataset_version_id'), 'dataset_version_sources', ['dataset_version_id'], unique=False)
    op.create_table('model_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('external_uri', sa.String(length=2000), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('registered_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('deployed_at', sa.DateTime(), nullable=True),
    sa.Column('suspended_at', sa.DateTime(), nullable=True),
    sa.Column('suspended_reason', sa.Text(), nullable=True),
    sa.Column('retired_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_model_versions_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_versions')),
    sa.UniqueConstraint('asset_id', 'version', name='uq_model_version')
    )
    op.create_index(op.f('ix_model_versions_asset_id'), 'model_versions', ['asset_id'], unique=False)
    op.create_index(op.f('ix_model_versions_status'), 'model_versions', ['status'], unique=False)
    op.create_table('model_version_datasets',
    sa.Column('model_version_id', sa.String(length=36), nullable=False),
    sa.Column('dataset_version_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], name=op.f('fk_model_version_datasets_dataset_version_id_dataset_versions')),
    sa.ForeignKeyConstraint(['model_version_id'], ['model_versions.id'], name=op.f('fk_model_version_datasets_model_version_id_model_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('model_version_id', 'dataset_version_id', name=op.f('pk_model_version_datasets'))
    )
    op.create_index(op.f('ix_model_version_datasets_dataset_version_id'), 'model_version_datasets', ['dataset_version_id'], unique=False)
    # 標準の Purpose。同じ ID を既に登録している環境では、その定義を尊重する
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM privacy_purposes WHERE id = :id"), {"id": AI_TRAINING}).first() is None:
        now = utcnow()
        conn.execute(sa.text(
            "INSERT INTO privacy_purposes (id, description, default_legal_basis, created_at, updated_at) "
            "VALUES (:id, :description, 'consent', :now, :now)"),
            {"id": AI_TRAINING, "description": "AI モデルの学習への利用 (§14 Training Data)", "now": now})


def downgrade() -> None:
    # 同意の履歴が参照している行は消せない (同意の記録は証跡なので残す)
    op.get_bind().execute(sa.text(
        "DELETE FROM privacy_purposes WHERE id = :id AND NOT EXISTS (SELECT 1 FROM consents WHERE purpose_id = :id)"),
        {"id": AI_TRAINING})
    op.drop_index(op.f('ix_model_version_datasets_dataset_version_id'), table_name='model_version_datasets')
    op.drop_table('model_version_datasets')
    op.drop_index(op.f('ix_model_versions_status'), table_name='model_versions')
    op.drop_index(op.f('ix_model_versions_asset_id'), table_name='model_versions')
    op.drop_table('model_versions')
    op.drop_index(op.f('ix_dataset_version_sources_dataset_version_id'), table_name='dataset_version_sources')
    op.drop_index(op.f('ix_dataset_version_sources_asset_id'), table_name='dataset_version_sources')
    op.drop_table('dataset_version_sources')
    op.drop_index(op.f('ix_dataset_versions_status'), table_name='dataset_versions')
    op.drop_index(op.f('ix_dataset_versions_asset_id'), table_name='dataset_versions')
    op.drop_table('dataset_versions')
    op.drop_table('model_cards')
    op.drop_table('dataset_cards')
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.drop_index(batch_op.f('ix_data_assets_ai_kind'))
        batch_op.drop_column('ai_kind')
