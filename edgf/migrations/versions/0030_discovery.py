"""discovery: Data Plane の資産一覧の取り込み (§7 Discovery)

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0030'
down_revision: str | None = '0029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('discovery_sources',
    sa.Column('namespace', sa.String(length=200), nullable=False),
    sa.Column('domain', sa.String(length=100), nullable=False),
    sa.Column('steward', sa.String(length=200), nullable=False),
    sa.Column('last_snapshot_id', sa.String(length=36), nullable=True),
    sa.Column('last_snapshot_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('namespace', name=op.f('pk_discovery_sources'))
    )
    op.create_index(op.f('ix_discovery_sources_domain'), 'discovery_sources', ['domain'], unique=False)
    op.create_table('discovery_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('namespace', sa.String(length=200), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('asset_count', sa.Integer(), nullable=False),
    sa.Column('added_count', sa.Integer(), nullable=False),
    sa.Column('missing_count', sa.Integer(), nullable=False),
    sa.Column('changed_count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_discovery_snapshots'))
    )
    op.create_index(op.f('ix_discovery_snapshots_namespace'), 'discovery_snapshots', ['namespace'], unique=False)
    op.create_table('discovered_assets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('namespace', sa.String(length=200), nullable=False),
    sa.Column('qualified_name', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=True),
    sa.Column('schema', sa.JSON(), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('missing_since', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_discovered_assets')),
    sa.UniqueConstraint('namespace', 'qualified_name', name='uq_discovered_asset')
    )
    op.create_index(op.f('ix_discovered_assets_namespace'), 'discovered_assets', ['namespace'], unique=False)
    op.create_index(op.f('ix_discovered_assets_qualified_name'), 'discovered_assets', ['qualified_name'], unique=False)
    op.create_index(op.f('ix_discovered_assets_status'), 'discovered_assets', ['status'], unique=False)
    op.create_table('schema_change_proposals',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('namespace', sa.String(length=200), nullable=False),
    sa.Column('proposed_schema', sa.JSON(), nullable=False),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('issue_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_schema_change_proposals_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_schema_change_proposals'))
    )
    op.create_index(op.f('ix_schema_change_proposals_asset_id'), 'schema_change_proposals', ['asset_id'], unique=False)
    op.create_index(op.f('ix_schema_change_proposals_status'), 'schema_change_proposals', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_schema_change_proposals_status'), table_name='schema_change_proposals')
    op.drop_index(op.f('ix_schema_change_proposals_asset_id'), table_name='schema_change_proposals')
    op.drop_table('schema_change_proposals')
    op.drop_index(op.f('ix_discovered_assets_status'), table_name='discovered_assets')
    op.drop_index(op.f('ix_discovered_assets_qualified_name'), table_name='discovered_assets')
    op.drop_index(op.f('ix_discovered_assets_namespace'), table_name='discovered_assets')
    op.drop_table('discovered_assets')
    op.drop_index(op.f('ix_discovery_snapshots_namespace'), table_name='discovery_snapshots')
    op.drop_table('discovery_snapshots')
    op.drop_index(op.f('ix_discovery_sources_domain'), table_name='discovery_sources')
    op.drop_table('discovery_sources')
