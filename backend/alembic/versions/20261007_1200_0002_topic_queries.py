"""topics: keywords -> queries, description; paper_topics.matched_queries

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 12:00:00
"""

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger('alembic.runtime.migration')

# то, во что upgrade превращает ключевое слово: all:"фраза" (или all:слово)
_SIMPLE_QUERY_RE = re.compile(r'^all:(?:"([^"]+)"|([^\s()":]+))$')


def _keyword_to_query(keyword: str) -> str | None:
    phrase = ' '.join(keyword.replace('"', '').split())
    return f'all:"{phrase}"' if phrase else None


def upgrade() -> None:
    op.add_column('topics', sa.Column('description', sa.Text(), nullable=True))
    op.add_column(
        'topics',
        sa.Column('queries', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    )
    op.add_column(
        'paper_topics',
        sa.Column(
            'matched_queries', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False
        ),
    )

    conn = op.get_bind()
    update = sa.text('UPDATE topics SET queries = :queries WHERE id = :id').bindparams(
        sa.bindparam('queries', type_=postgresql.ARRAY(sa.Text()))
    )
    for row in conn.execute(sa.text('SELECT id, keywords FROM topics')).all():
        queries = [q for q in map(_keyword_to_query, row.keywords or []) if q]
        conn.execute(update, {'id': row.id, 'queries': queries})

    op.drop_column('topics', 'keywords')


def downgrade() -> None:
    op.add_column(
        'topics',
        sa.Column('keywords', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    )

    conn = op.get_bind()
    update = sa.text('UPDATE topics SET keywords = :keywords WHERE id = :id').bindparams(
        sa.bindparam('keywords', type_=postgresql.ARRAY(sa.Text()))
    )
    for row in conn.execute(sa.text('SELECT id, name, queries FROM topics')).all():
        keywords: list[str] = []
        for query in row.queries or []:
            match = _SIMPLE_QUERY_RE.match(query.strip())
            if match is None:
                log.warning(
                    'Тема %r (id=%s): запрос %r нельзя превратить в ключевое слово, пропущен',
                    row.name,
                    row.id,
                    query,
                )
                continue
            keywords.append(match.group(1) or match.group(2))
        if not keywords:
            log.warning('Тема %r (id=%s) осталась без ключевых слов', row.name, row.id)
        conn.execute(update, {'id': row.id, 'keywords': keywords})

    op.drop_column('paper_topics', 'matched_queries')
    op.drop_column('topics', 'queries')
    op.drop_column('topics', 'description')
