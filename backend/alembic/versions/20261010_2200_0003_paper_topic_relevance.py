"""paper_topics: relevance, relevance_reason

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('paper_topics', sa.Column('relevance', sa.String(length=32), nullable=True))
    op.add_column('paper_topics', sa.Column('relevance_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    # статьи, отброшенные как нерелевантные, остаются без текста: для старого кода это ошибка
    op.execute(
        "UPDATE papers SET status = 'failed', error = 'Статья признана нерелевантной' "
        "WHERE status = 'irrelevant'"
    )
    op.drop_column('paper_topics', 'relevance_reason')
    op.drop_column('paper_topics', 'relevance')
