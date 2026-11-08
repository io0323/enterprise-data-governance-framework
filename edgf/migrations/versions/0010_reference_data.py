"""reference_data_sets / reference_data_versions / reference_codes (§8 Reference Data)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('reference_data_sets',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_reference_data_sets_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reference_data_sets'))
    )
    op.create_index(op.f('ix_reference_data_sets_asset_id'), 'reference_data_sets', ['asset_id'], unique=False)
    op.create_table('reference_data_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('set_id', sa.String(length=80), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('published_by', sa.String(length=200), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['set_id'], ['reference_data_sets.id'], name=op.f('fk_reference_data_versions_set_id_reference_data_sets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reference_data_versions')),
    sa.UniqueConstraint('set_id', 'version', name='uq_reference_data_version')
    )
    op.create_index(op.f('ix_reference_data_versions_set_id'), 'reference_data_versions', ['set_id'], unique=False)
    op.create_table('reference_codes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('version_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('label', sa.String(length=300), nullable=False),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('standard', sa.String(length=100), nullable=True),
    sa.Column('standard_version', sa.String(length=50), nullable=True),
    sa.ForeignKeyConstraint(['version_id'], ['reference_data_versions.id'], name=op.f('fk_reference_codes_version_id_reference_data_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reference_codes')),
    sa.UniqueConstraint('version_id', 'code', name='uq_reference_code')
    )
    op.create_index(op.f('ix_reference_codes_version_id'), 'reference_codes', ['version_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reference_codes_version_id'), table_name='reference_codes')
    op.drop_table('reference_codes')
    op.drop_index(op.f('ix_reference_data_versions_set_id'), table_name='reference_data_versions')
    op.drop_table('reference_data_versions')
    op.drop_index(op.f('ix_reference_data_sets_asset_id'), table_name='reference_data_sets')
    op.drop_table('reference_data_sets')
