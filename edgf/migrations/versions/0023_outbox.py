"""outbox_events / webhook_endpoints / webhook_deliveries: イベント配信 (§16)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('outbox_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('type', sa.String(length=100), nullable=False),
    sa.Column('subject', sa.String(length=300), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('request_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('fanned_out_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox_events'))
    )
    for column in ('created_at', 'fanned_out_at', 'type'):
        op.create_index(op.f(f'ix_outbox_events_{column}'), 'outbox_events', [column], unique=False)
    op.create_table('webhook_endpoints',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('secret_ref', sa.String(length=200), nullable=False),
    sa.Column('event_types', sa.JSON(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_endpoints'))
    )
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('event_id', sa.String(length=36), nullable=False),
    sa.Column('endpoint_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('last_error', sa.String(length=200), nullable=True),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.Column('dead_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['endpoint_id'], ['webhook_endpoints.id'], name=op.f('fk_webhook_deliveries_endpoint_id_webhook_endpoints')),
    sa.ForeignKeyConstraint(['event_id'], ['outbox_events.id'], name=op.f('fk_webhook_deliveries_event_id_outbox_events')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries')),
    sa.UniqueConstraint('event_id', 'endpoint_id', name='uq_webhook_delivery')
    )
    for column in ('endpoint_id', 'event_id', 'next_attempt_at', 'status'):
        op.create_index(op.f(f'ix_webhook_deliveries_{column}'), 'webhook_deliveries', [column], unique=False)


def downgrade() -> None:
    for column in ('status', 'next_attempt_at', 'event_id', 'endpoint_id'):
        op.drop_index(op.f(f'ix_webhook_deliveries_{column}'), table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_table('webhook_endpoints')
    for column in ('type', 'fanned_out_at', 'created_at'):
        op.drop_index(op.f(f'ix_outbox_events_{column}'), table_name='outbox_events')
    op.drop_table('outbox_events')
