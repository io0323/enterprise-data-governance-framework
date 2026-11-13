"""ui_sessions / ui_login_states: UI のログインのセッション (BFF)

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: str | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('ui_sessions',
    sa.Column('id_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_hash', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('roles', sa.JSON(), nullable=False),
    sa.Column('clearance', sa.String(length=20), nullable=False),
    sa.Column('domains', sa.JSON(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('auth_time', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id_hash', name=op.f('pk_ui_sessions'))
    )
    op.create_index(op.f('ix_ui_sessions_subject'), 'ui_sessions', ['subject'], unique=False)
    op.create_index(op.f('ix_ui_sessions_last_seen_at'), 'ui_sessions', ['last_seen_at'], unique=False)
    op.create_index(op.f('ix_ui_sessions_expires_at'), 'ui_sessions', ['expires_at'], unique=False)
    op.create_table('ui_login_states',
    sa.Column('ref_hash', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=100), nullable=False),
    sa.Column('nonce', sa.String(length=100), nullable=False),
    sa.Column('code_verifier', sa.String(length=128), nullable=False),
    sa.Column('return_to', sa.String(length=500), nullable=False),
    sa.Column('max_age', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('ref_hash', name=op.f('pk_ui_login_states'))
    )
    op.create_index(op.f('ix_ui_login_states_expires_at'), 'ui_login_states', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ui_login_states_expires_at'), table_name='ui_login_states')
    op.drop_table('ui_login_states')
    op.drop_index(op.f('ix_ui_sessions_expires_at'), table_name='ui_sessions')
    op.drop_index(op.f('ix_ui_sessions_last_seen_at'), table_name='ui_sessions')
    op.drop_index(op.f('ix_ui_sessions_subject'), table_name='ui_sessions')
    op.drop_table('ui_sessions')
