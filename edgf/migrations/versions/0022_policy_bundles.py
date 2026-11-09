"""policy_bundles: Policy の正 (§16 Policy / §19 Integrity)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('policy_bundles',
    sa.Column('version', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('files', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('base_version', sa.Integer(), nullable=True),
    sa.Column('base_hash', sa.String(length=64), nullable=True),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('domains', sa.JSON(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('proposed_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('approval_id', sa.String(length=36), nullable=True),
    sa.Column('impact', sa.JSON(), nullable=False),
    sa.Column('rollback_of', sa.Integer(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('published_by', sa.String(length=200), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('superseded_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['approval_id'], ['approval_requests.id'], name=op.f('fk_policy_bundles_approval_id_approval_requests')),
    sa.PrimaryKeyConstraint('version', name=op.f('pk_policy_bundles'))
    )
    op.create_index(op.f('ix_policy_bundles_status'), 'policy_bundles', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_policy_bundles_status'), table_name='policy_bundles')
    op.drop_table('policy_bundles')
