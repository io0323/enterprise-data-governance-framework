"""idempotency_records: Idempotency-Key による送信の重複の防止

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: str | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('idempotency_records',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('content_type', sa.String(length=100), nullable=True),
    sa.Column('response_body', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('locked_until', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_idempotency_records')),
    sa.UniqueConstraint('actor', 'key', name='uq_idempotency_actor_key')
    )
    op.create_index(op.f('ix_idempotency_records_expires_at'), 'idempotency_records', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_idempotency_records_expires_at'), table_name='idempotency_records')
    op.drop_table('idempotency_records')
