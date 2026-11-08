"""access_grants: Owner による閲覧承認 (§4.2 アクセス承認 / §11 Access Control)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('access_grants',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('granted_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_by', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_access_grants_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_access_grants'))
    )
    op.create_index(op.f('ix_access_grants_asset_id'), 'access_grants', ['asset_id'], unique=False)
    op.create_index(op.f('ix_access_grants_subject'), 'access_grants', ['subject'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_access_grants_subject'), table_name='access_grants')
    op.drop_index(op.f('ix_access_grants_asset_id'), table_name='access_grants')
    op.drop_table('access_grants')
