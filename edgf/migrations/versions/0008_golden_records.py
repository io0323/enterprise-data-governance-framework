"""golden_records / mdm_xrefs / golden_versions / mdm_subject_links (§8)

値は保持せず、相互参照・由来・版の情報 (内容ハッシュ) だけを持つ (§18.2)。

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('golden_records',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('recompose_required', sa.Boolean(), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('hash_salt', sa.String(length=32), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tombstoned_at', sa.DateTime(), nullable=True),
    sa.Column('tombstoned_by_request', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_golden_records_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_golden_records'))
    )
    op.create_index(op.f('ix_golden_records_asset_id'), 'golden_records', ['asset_id'], unique=False)
    op.create_index(op.f('ix_golden_records_entity_type'), 'golden_records', ['entity_type'], unique=False)
    op.create_table('mdm_xrefs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('source_ref', sa.String(length=64), nullable=False),
    sa.Column('source_system', sa.String(length=100), nullable=False),
    sa.Column('golden_id', sa.String(length=36), nullable=False),
    sa.Column('link_type', sa.String(length=20), nullable=False),
    sa.Column('linked_by', sa.String(length=200), nullable=False),
    sa.Column('linked_at', sa.DateTime(), nullable=False),
    sa.Column('tombstoned_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['golden_id'], ['golden_records.id'], name=op.f('fk_mdm_xrefs_golden_id_golden_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mdm_xrefs')),
    sa.UniqueConstraint('entity_type', 'source_ref', name='uq_mdm_xref_source')
    )
    op.create_index(op.f('ix_mdm_xrefs_golden_id'), 'mdm_xrefs', ['golden_id'], unique=False)
    op.create_index(op.f('ix_mdm_xrefs_source_ref'), 'mdm_xrefs', ['source_ref'], unique=False)
    op.create_table('golden_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('golden_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('valid_from', sa.DateTime(), nullable=False),
    sa.Column('valid_to', sa.DateTime(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('source_refs', sa.JSON(), nullable=False),
    sa.Column('provenance', sa.JSON(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('tombstoned', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['golden_id'], ['golden_records.id'], name=op.f('fk_golden_versions_golden_id_golden_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_golden_versions')),
    sa.UniqueConstraint('golden_id', 'version', name='uq_golden_version')
    )
    op.create_index(op.f('ix_golden_versions_golden_id'), 'golden_versions', ['golden_id'], unique=False)
    op.create_table('mdm_subject_links',
    sa.Column('golden_id', sa.String(length=36), nullable=False),
    sa.Column('subject_ref', sa.String(length=64), nullable=False),
    sa.Column('identifier_type', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['golden_id'], ['golden_records.id'], name=op.f('fk_mdm_subject_links_golden_id_golden_records')),
    sa.PrimaryKeyConstraint('golden_id', 'subject_ref', name=op.f('pk_mdm_subject_links'))
    )
    op.create_index(op.f('ix_mdm_subject_links_subject_ref'), 'mdm_subject_links', ['subject_ref'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mdm_subject_links_subject_ref'), table_name='mdm_subject_links')
    op.drop_table('mdm_subject_links')
    op.drop_index(op.f('ix_golden_versions_golden_id'), table_name='golden_versions')
    op.drop_table('golden_versions')
    op.drop_index(op.f('ix_mdm_xrefs_source_ref'), table_name='mdm_xrefs')
    op.drop_index(op.f('ix_mdm_xrefs_golden_id'), table_name='mdm_xrefs')
    op.drop_table('mdm_xrefs')
    op.drop_index(op.f('ix_golden_records_entity_type'), table_name='golden_records')
    op.drop_index(op.f('ix_golden_records_asset_id'), table_name='golden_records')
    op.drop_table('golden_records')
