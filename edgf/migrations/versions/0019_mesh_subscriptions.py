"""mesh_subscriptions: 利用登録、privacy_purposes: 目的の撤回 (§15 / §12)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('privacy_purposes') as batch_op:
        batch_op.add_column(sa.Column('withdrawn_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('withdrawn_by', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('withdrawn_reason', sa.Text(), nullable=True))
    op.create_table('mesh_subscriptions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('subscriber', sa.String(length=200), nullable=False),
    sa.Column('product_id', sa.String(length=36), nullable=False),
    sa.Column('output_port_id', sa.String(length=36), nullable=False),
    sa.Column('contract_id', sa.String(length=36), nullable=False),
    sa.Column('major', sa.Integer(), nullable=False),
    sa.Column('purpose_id', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('decision_comment', sa.Text(), nullable=False),
    sa.Column('suspended_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['contract_id'], ['data_contracts.id'], name=op.f('fk_mesh_subscriptions_contract_id_data_contracts')),
    sa.ForeignKeyConstraint(['output_port_id'], ['output_ports.id'], name=op.f('fk_mesh_subscriptions_output_port_id_output_ports')),
    sa.ForeignKeyConstraint(['product_id'], ['data_products.id'], name=op.f('fk_mesh_subscriptions_product_id_data_products')),
    sa.ForeignKeyConstraint(['purpose_id'], ['privacy_purposes.id'], name=op.f('fk_mesh_subscriptions_purpose_id_privacy_purposes')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mesh_subscriptions'))
    )
    for column in ('contract_id', 'output_port_id', 'product_id', 'purpose_id', 'status', 'subscriber'):
        op.create_index(op.f(f'ix_mesh_subscriptions_{column}'), 'mesh_subscriptions', [column], unique=False)


def downgrade() -> None:
    for column in ('subscriber', 'status', 'purpose_id', 'product_id', 'output_port_id', 'contract_id'):
        op.drop_index(op.f(f'ix_mesh_subscriptions_{column}'), table_name='mesh_subscriptions')
    op.drop_table('mesh_subscriptions')
    with op.batch_alter_table('privacy_purposes') as batch_op:
        batch_op.drop_column('withdrawn_reason')
        batch_op.drop_column('withdrawn_by')
        batch_op.drop_column('withdrawn_at')
