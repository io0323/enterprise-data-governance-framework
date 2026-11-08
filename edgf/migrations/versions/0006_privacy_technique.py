"""data_assets.privacy_technique / privacy_params: 匿名化・仮名化の管理 (§12)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.add_column(sa.Column('privacy_technique', sa.String(length=20), nullable=False, server_default='none'))
        batch_op.add_column(sa.Column('privacy_params', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.drop_column('privacy_params')
        batch_op.drop_column('privacy_technique')
