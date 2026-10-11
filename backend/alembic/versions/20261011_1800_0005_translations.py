"""translations

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'translations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paper_id', sa.Integer(), nullable=False),
        sa.Column('language', sa.String(length=64), nullable=False),
        sa.Column(
            'status',
            sa.Enum('queued', 'running', 'ready', 'failed', name='translationstatus', native_enum=False, length=32),
            server_default='queued',
            nullable=False,
        ),
        sa.Column('source', sa.String(length=16), nullable=True),
        sa.Column('docx_path', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=255), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['paper_id'], ['papers.id'], name=op.f('fk_translations_paper_id_papers'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['task_runs.id'], name=op.f('fk_translations_run_id_task_runs'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_translations')),
        sa.UniqueConstraint('paper_id', 'language', name=op.f('uq_translations_paper_id')),
    )
    op.create_index(op.f('ix_translations_paper_id'), 'translations', ['paper_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_translations_paper_id'), table_name='translations')
    op.drop_table('translations')
