"""cdm_entities / cdm_versions: 外部の CDM 定義の写し (冒頭の役割分離 / §15 Standardization)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: str | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cdm_entities',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_cdm_entities_asset_id_data_assets')),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_cdm_entities')),
    sa.UniqueConstraint('asset_id', name=op.f('uq_cdm_entities_asset_id'))
    )
    op.create_table('cdm_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity', sa.String(length=120), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('json_schema', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('source_uri', sa.String(length=2000), nullable=True),
    sa.Column('imported_by', sa.String(length=200), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['entity'], ['cdm_entities.name'], name=op.f('fk_cdm_versions_entity_cdm_entities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cdm_versions')),
    sa.UniqueConstraint('entity', 'version', name='uq_cdm_version')
    )
    op.create_index(op.f('ix_cdm_versions_entity'), 'cdm_versions', ['entity'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cdm_versions_entity'), table_name='cdm_versions')
    op.drop_table('cdm_versions')
    op.drop_table('cdm_entities')
