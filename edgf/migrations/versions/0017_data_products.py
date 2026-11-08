"""data_products / output_ports: Data Product の統治のための定義 (§15)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('data_products',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('domain', sa.String(length=100), nullable=False),
    sa.Column('owner', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('published_by', sa.String(length=200), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('deprecated_at', sa.DateTime(), nullable=True),
    sa.Column('retired_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['domain'], ['domains.name'], name=op.f('fk_data_products_domain_domains')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_products')),
    sa.UniqueConstraint('domain', 'name', name='uq_data_product_name')
    )
    op.create_index(op.f('ix_data_products_domain'), 'data_products', ['domain'], unique=False)
    op.create_index(op.f('ix_data_products_owner'), 'data_products', ['owner'], unique=False)
    op.create_index(op.f('ix_data_products_status'), 'data_products', ['status'], unique=False)
    op.create_table('data_product_terms',
    sa.Column('product_id', sa.String(length=36), nullable=False),
    sa.Column('term_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['data_products.id'], name=op.f('fk_data_product_terms_product_id_data_products'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['term_id'], ['glossary_terms.id'], name=op.f('fk_data_product_terms_term_id_glossary_terms'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('product_id', 'term_id', name=op.f('pk_data_product_terms'))
    )
    op.create_table('output_ports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('product_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('address_uri', sa.String(length=2000), nullable=True),
    sa.Column('port_type', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_output_ports_asset_id_data_assets')),
    sa.ForeignKeyConstraint(['product_id'], ['data_products.id'], name=op.f('fk_output_ports_product_id_data_products')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_output_ports')),
    sa.UniqueConstraint('asset_id', name=op.f('uq_output_ports_asset_id')),
    sa.UniqueConstraint('product_id', 'name', name='uq_output_port_name')
    )
    op.create_index(op.f('ix_output_ports_product_id'), 'output_ports', ['product_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_output_ports_product_id'), table_name='output_ports')
    op.drop_table('output_ports')
    op.drop_table('data_product_terms')
    op.drop_index(op.f('ix_data_products_status'), table_name='data_products')
    op.drop_index(op.f('ix_data_products_owner'), table_name='data_products')
    op.drop_index(op.f('ix_data_products_domain'), table_name='data_products')
    op.drop_table('data_products')
