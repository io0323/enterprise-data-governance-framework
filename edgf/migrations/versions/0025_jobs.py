"""job_leases / job_runs: ジョブ基盤 (§16 / §19)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0025'
down_revision: str | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('job_leases',
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('holder', sa.String(length=100), nullable=False),
    sa.Column('acquired_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_job_leases'))
    )
    op.create_table('job_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('job', sa.String(length=50), nullable=False),
    sa.Column('holder', sa.String(length=100), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('error', sa.String(length=200), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_runs'))
    )
    for column in ('job', 'started_at', 'status'):
        op.create_index(op.f(f'ix_job_runs_{column}'), 'job_runs', [column], unique=False)


def downgrade() -> None:
    for column in ('status', 'started_at', 'job'):
        op.drop_index(op.f(f'ix_job_runs_{column}'), table_name='job_runs')
    op.drop_table('job_runs')
    op.drop_table('job_leases')
