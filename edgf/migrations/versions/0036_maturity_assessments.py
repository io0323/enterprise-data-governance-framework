"""maturity_assessments / maturity_references: ドメイン別の成熟度の評価 (§20)

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0036'
down_revision: str | None = '0035'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('maturity_assessments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('domain', sa.String(length=100), nullable=False),
    sa.Column('cycle', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('criteria', sa.JSON(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=True),
    sa.Column('period_end', sa.Date(), nullable=True),
    sa.Column('auto_results', sa.JSON(), nullable=False),
    sa.Column('indicative_level', sa.Integer(), nullable=True),
    sa.Column('previous_level', sa.Integer(), nullable=True),
    sa.Column('level_cap', sa.Integer(), nullable=True),
    sa.Column('evaluated_at', sa.DateTime(), nullable=True),
    sa.Column('self_assessment', sa.JSON(), nullable=False),
    sa.Column('self_assessed_by', sa.String(length=200), nullable=True),
    sa.Column('self_assessed_at', sa.DateTime(), nullable=True),
    sa.Column('approval_id', sa.String(length=36), nullable=True),
    sa.Column('decided_level', sa.Integer(), nullable=True),
    sa.Column('decision', sa.JSON(), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_maturity_assessments'))
    )
    for column in ('domain', 'status', 'due_at', 'decided_at'):
        op.create_index(op.f(f'ix_maturity_assessments_{column}'), 'maturity_assessments', [column], unique=False)
    op.create_table('maturity_references',
    sa.Column('domain', sa.String(length=100), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('assessment_id', sa.String(length=36), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('designated_by', sa.String(length=200), nullable=False),
    sa.Column('designated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('domain', name=op.f('pk_maturity_references'))
    )


def downgrade() -> None:
    op.drop_table('maturity_references')
    for column in ('decided_at', 'due_at', 'status', 'domain'):
        op.drop_index(op.f(f'ix_maturity_assessments_{column}'), table_name='maturity_assessments')
    op.drop_table('maturity_assessments')
