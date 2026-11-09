"""audit_anchors: 監査チェーンのアンカー (§13)

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0027'
down_revision: str | None = '0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audit_anchors',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('anchored_at', sa.DateTime(), nullable=False),
    sa.Column('key_id', sa.String(length=32), nullable=False),
    sa.Column('signature', sa.String(length=128), nullable=False),
    sa.Column('sinks', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_anchors')),
    sa.UniqueConstraint('seq', name=op.f('uq_audit_anchors_seq'))
    )


def downgrade() -> None:
    op.drop_table('audit_anchors')
