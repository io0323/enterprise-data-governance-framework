"""mdm_rule_approvals: Golden Record 生成規則の Owner 承認 (§8.2)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('mdm_rule_approvals',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('rule_hash', sa.String(length=64), nullable=False),
    sa.Column('approved_by', sa.String(length=200), nullable=False),
    sa.Column('approved_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mdm_rule_approvals')),
    sa.UniqueConstraint('entity_type', 'rule_hash', name='uq_mdm_rule_approval')
    )


def downgrade() -> None:
    op.drop_table('mdm_rule_approvals')
