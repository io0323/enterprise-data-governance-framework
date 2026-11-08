"""erasure_tasks.golden_ids: 消去対象の Golden Record (§12 / §8)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('erasure_tasks') as batch_op:
        batch_op.add_column(sa.Column('golden_ids', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('erasure_tasks') as batch_op:
        batch_op.drop_column('golden_ids')
