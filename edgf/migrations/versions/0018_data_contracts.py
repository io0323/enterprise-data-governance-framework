"""data_contracts / contract_versions: Data Contract の版 (§15 Standardization)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('data_contracts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('product_id', sa.String(length=36), nullable=False),
    sa.Column('output_port_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('schema_object', sa.String(length=200), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['output_port_id'], ['output_ports.id'], name=op.f('fk_data_contracts_output_port_id_output_ports')),
    sa.ForeignKeyConstraint(['product_id'], ['data_products.id'], name=op.f('fk_data_contracts_product_id_data_products')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_contracts')),
    sa.UniqueConstraint('output_port_id', name=op.f('uq_data_contracts_output_port_id'))
    )
    op.create_index(op.f('ix_data_contracts_product_id'), 'data_contracts', ['product_id'], unique=False)
    op.create_table('contract_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('contract_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('major', sa.Integer(), nullable=False),
    sa.Column('minor', sa.Integer(), nullable=False),
    sa.Column('patch', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('schema', sa.JSON(), nullable=False),
    sa.Column('quality_sla', sa.JSON(), nullable=False),
    sa.Column('freshness_sla_hours', sa.Integer(), nullable=True),
    sa.Column('sensitivity', sa.String(length=20), nullable=False),
    sa.Column('purposes', sa.JSON(), nullable=False),
    sa.Column('conforms_to', sa.String(length=300), nullable=True),
    sa.Column('cdm_mappings', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('published_by', sa.String(length=200), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('deprecated_at', sa.DateTime(), nullable=True),
    sa.Column('sunset_at', sa.DateTime(), nullable=True),
    sa.Column('sunset_shortened_by', sa.String(length=200), nullable=True),
    sa.Column('sunset_shortened_reason', sa.Text(), nullable=True),
    sa.Column('retired_by', sa.String(length=200), nullable=True),
    sa.Column('retired_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['contract_id'], ['data_contracts.id'], name=op.f('fk_contract_versions_contract_id_data_contracts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_versions')),
    sa.UniqueConstraint('contract_id', 'version', name='uq_contract_version')
    )
    op.create_index(op.f('ix_contract_versions_contract_id'), 'contract_versions', ['contract_id'], unique=False)
    op.create_index(op.f('ix_contract_versions_status'), 'contract_versions', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_contract_versions_status'), table_name='contract_versions')
    op.drop_index(op.f('ix_contract_versions_contract_id'), table_name='contract_versions')
    op.drop_table('contract_versions')
    op.drop_index(op.f('ix_data_contracts_product_id'), table_name='data_contracts')
    op.drop_table('data_contracts')
