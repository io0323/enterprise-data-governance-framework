"""audit_events.request_id: 監査イベントとログ・トレースの突合 (§19 Auditability)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('audit_events') as batch_op:
        batch_op.add_column(sa.Column('request_id', sa.String(length=128), nullable=True))
        batch_op.create_index(batch_op.f('ix_audit_events_request_id'), ['request_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('audit_events') as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_request_id'))
        batch_op.drop_column('request_id')
