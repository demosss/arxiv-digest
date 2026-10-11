"""summaries: abstract_ru; digests: content

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('summaries', sa.Column('abstract_ru', sa.Text(), nullable=True))
    # уже собранные дайджесты содержат только разделы резюме
    op.add_column(
        'digests',
        sa.Column('content', sa.String(length=32), server_default='summary', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('digests', 'content')
    op.drop_column('summaries', 'abstract_ru')
