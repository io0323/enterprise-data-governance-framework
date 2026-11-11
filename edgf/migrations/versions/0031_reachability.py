"""reachability_reports: Output Port の到達の記録 (§15 Addressable、MESH-007)

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0031'
down_revision: str | None = '0030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('reachability_reports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('port_id', sa.String(length=36), nullable=False),
    sa.Column('reachable', sa.Boolean(), nullable=False),
    sa.Column('method', sa.String(length=10), nullable=False),
    sa.Column('observed_at', sa.DateTime(), nullable=False),
    sa.Column('detail', sa.String(length=200), nullable=False),
    sa.Column('reporter', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['port_id'], ['output_ports.id'], name=op.f('fk_reachability_reports_port_id_output_ports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reachability_reports'))
    )
    op.create_index(op.f('ix_reachability_reports_observed_at'), 'reachability_reports', ['observed_at'], unique=False)
    op.create_index(op.f('ix_reachability_reports_port_id'), 'reachability_reports', ['port_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reachability_reports_port_id'), table_name='reachability_reports')
    op.drop_index(op.f('ix_reachability_reports_observed_at'), table_name='reachability_reports')
    op.drop_table('reachability_reports')
