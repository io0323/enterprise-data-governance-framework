"""privacy_purposes / consents: Consent Registry (§12)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('privacy_purposes',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('default_legal_basis', sa.String(length=30), nullable=False),
    sa.Column('retention_days', sa.Integer(), nullable=True),
    sa.Column('domain', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['domain'], ['domains.name'], name=op.f('fk_privacy_purposes_domain_domains')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_privacy_purposes'))
    )
    op.create_table('consents',
    sa.Column('seq', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('subject_ref', sa.String(length=64), nullable=False),
    sa.Column('purpose_id', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('recorded_by', sa.String(length=200), nullable=False),
    sa.Column('recorded_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['purpose_id'], ['privacy_purposes.id'], name=op.f('fk_consents_purpose_id_privacy_purposes')),
    sa.PrimaryKeyConstraint('seq', name=op.f('pk_consents'))
    )
    op.create_index('ix_consents_subject_purpose', 'consents', ['subject_ref', 'purpose_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_consents_subject_purpose', table_name='consents')
    op.drop_table('consents')
    op.drop_table('privacy_purposes')
