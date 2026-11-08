"""data_assets: 継承した統制属性 (§14 Embedding / Vector / RAG)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.add_column(sa.Column('effective_sensitivity', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('effective_contains_pii', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('inheritance', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('data_assets') as batch_op:
        batch_op.drop_column('inheritance')
        batch_op.drop_column('effective_contains_pii')
        batch_op.drop_column('effective_sensitivity')
