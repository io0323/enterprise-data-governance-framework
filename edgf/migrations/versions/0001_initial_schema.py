"""initial schema: MVP (Catalog / Glossary / Quality / Issue / Lineage / Policy Exception / Audit)

Phase 6 で create_all から移行。全 DB 方言で共通の DDL のみを使う。

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('seq', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_id', sa.String(length=300), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('seq', name=op.f('pk_audit_events')),
    sa.UniqueConstraint('hash', name=op.f('uq_audit_events_hash')),
    sa.UniqueConstraint('prev_hash', name=op.f('uq_audit_events_prev_hash'))
    )
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_entity_id'), 'audit_events', ['entity_id'], unique=False)
    op.create_index(op.f('ix_audit_events_ts'), 'audit_events', ['ts'], unique=False)

    op.create_table('domains',
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('owner', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_domains'))
    )
    op.create_table('glossary_terms',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('steward', sa.String(length=200), nullable=True),
    sa.Column('synonyms', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_glossary_terms'))
    )
    op.create_index(op.f('ix_glossary_terms_name'), 'glossary_terms', ['name'], unique=True)

    op.create_table('lineage_edges',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=300), nullable=False),
    sa.Column('target', sa.String(length=300), nullable=False),
    sa.Column('job', sa.String(length=300), nullable=False),
    sa.Column('transformation', sa.Text(), nullable=False),
    sa.Column('last_run_id', sa.String(length=100), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lineage_edges')),
    sa.UniqueConstraint('source', 'target', 'job', name='uq_lineage_edge')
    )
    op.create_index(op.f('ix_lineage_edges_source'), 'lineage_edges', ['source'], unique=False)
    op.create_index(op.f('ix_lineage_edges_target'), 'lineage_edges', ['target'], unique=False)

    op.create_table('data_assets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('qualified_name', sa.String(length=300), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('domain', sa.String(length=100), nullable=True),
    sa.Column('data_class', sa.String(length=20), nullable=False),
    sa.Column('sensitivity', sa.String(length=20), nullable=False),
    sa.Column('owner', sa.String(length=200), nullable=True),
    sa.Column('steward', sa.String(length=200), nullable=True),
    sa.Column('custodian', sa.String(length=200), nullable=True),
    sa.Column('lifecycle_state', sa.String(length=20), nullable=False),
    sa.Column('certification', sa.String(length=20), nullable=False),
    sa.Column('is_cde', sa.Boolean(), nullable=False),
    sa.Column('contains_pii', sa.Boolean(), nullable=False),
    sa.Column('legal_hold', sa.Boolean(), nullable=False),
    sa.Column('retention_days', sa.Integer(), nullable=True),
    sa.Column('freshness_sla_hours', sa.Integer(), nullable=True),
    sa.Column('last_refreshed_at', sa.DateTime(), nullable=True),
    sa.Column('schema', sa.JSON(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('controls', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['domain'], ['domains.name'], name=op.f('fk_data_assets_domain_domains')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_assets'))
    )
    op.create_index(op.f('ix_data_assets_owner'), 'data_assets', ['owner'], unique=False)
    op.create_index(op.f('ix_data_assets_qualified_name'), 'data_assets', ['qualified_name'], unique=True)

    op.create_table('glossary_term_asset',
    sa.Column('term_id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_glossary_term_asset_asset_id_data_assets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['term_id'], ['glossary_terms.id'], name=op.f('fk_glossary_term_asset_term_id_glossary_terms'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('term_id', 'asset_id', name=op.f('pk_glossary_term_asset'))
    )
    op.create_table('issues',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('source_ref', sa.String(length=100), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('assignee', sa.String(length=200), nullable=True),
    sa.Column('escalation_level', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_issues_asset_id_data_assets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_issues'))
    )
    op.create_index(op.f('ix_issues_asset_id'), 'issues', ['asset_id'], unique=False)
    op.create_index(op.f('ix_issues_source_ref'), 'issues', ['source_ref'], unique=False)
    op.create_index(op.f('ix_issues_status'), 'issues', ['status'], unique=False)

    op.create_table('policy_exceptions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('policy_id', sa.String(length=50), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('risk_assessment', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_policy_exceptions_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_exceptions'))
    )
    op.create_index(op.f('ix_policy_exceptions_asset_id'), 'policy_exceptions', ['asset_id'], unique=False)
    op.create_index(op.f('ix_policy_exceptions_policy_id'), 'policy_exceptions', ['policy_id'], unique=False)

    op.create_table('quality_rules',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('dimension', sa.String(length=20), nullable=False),
    sa.Column('rule_type', sa.String(length=30), nullable=False),
    sa.Column('column', sa.String(length=200), nullable=True),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_quality_rules_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_rules'))
    )
    op.create_index(op.f('ix_quality_rules_asset_id'), 'quality_rules', ['asset_id'], unique=False)

    op.create_table('quality_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('asset_id', sa.String(length=36), nullable=False),
    sa.Column('executed_at', sa.DateTime(), nullable=False),
    sa.Column('record_count', sa.Integer(), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('dimension_scores', sa.JSON(), nullable=False),
    sa.Column('results', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['asset_id'], ['data_assets.id'], name=op.f('fk_quality_runs_asset_id_data_assets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_runs'))
    )
    op.create_index(op.f('ix_quality_runs_asset_id'), 'quality_runs', ['asset_id'], unique=False)
    op.create_index(op.f('ix_quality_runs_executed_at'), 'quality_runs', ['executed_at'], unique=False)



def downgrade() -> None:
    op.drop_index(op.f('ix_quality_runs_executed_at'), table_name='quality_runs')
    op.drop_index(op.f('ix_quality_runs_asset_id'), table_name='quality_runs')

    op.drop_table('quality_runs')
    op.drop_index(op.f('ix_quality_rules_asset_id'), table_name='quality_rules')

    op.drop_table('quality_rules')
    op.drop_index(op.f('ix_policy_exceptions_policy_id'), table_name='policy_exceptions')
    op.drop_index(op.f('ix_policy_exceptions_asset_id'), table_name='policy_exceptions')

    op.drop_table('policy_exceptions')
    op.drop_index(op.f('ix_issues_status'), table_name='issues')
    op.drop_index(op.f('ix_issues_source_ref'), table_name='issues')
    op.drop_index(op.f('ix_issues_asset_id'), table_name='issues')

    op.drop_table('issues')
    op.drop_table('glossary_term_asset')
    op.drop_index(op.f('ix_data_assets_qualified_name'), table_name='data_assets')
    op.drop_index(op.f('ix_data_assets_owner'), table_name='data_assets')

    op.drop_table('data_assets')
    op.drop_index(op.f('ix_lineage_edges_target'), table_name='lineage_edges')
    op.drop_index(op.f('ix_lineage_edges_source'), table_name='lineage_edges')

    op.drop_table('lineage_edges')
    op.drop_index(op.f('ix_glossary_terms_name'), table_name='glossary_terms')

    op.drop_table('glossary_terms')
    op.drop_table('domains')
    op.drop_index(op.f('ix_audit_events_ts'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_entity_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_action'), table_name='audit_events')

    op.drop_table('audit_events')
