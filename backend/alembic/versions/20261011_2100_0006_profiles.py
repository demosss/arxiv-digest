"""profiles

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# имя профиля, в который попадают данные, накопленные до появления профилей
DEFAULT_PROFILE_NAME = 'Основной'

# таблица -> (ondelete, прежнее ограничение уникальности и его колонки, новые колонки, нужен ли индекс)
TABLES: dict[str, tuple[str, str | None, list[str], list[str], bool]] = {
    'topics': ('RESTRICT', 'uq_topics_name', ['name'], ['profile_id', 'name'], False),
    'recipients': ('RESTRICT', 'uq_recipients_email', ['email'], ['profile_id', 'email'], False),
    'papers': ('RESTRICT', 'uq_papers_arxiv_id', ['arxiv_id'], ['profile_id', 'arxiv_id'], False),
    'digests': (
        'RESTRICT',
        'uq_digests_period_start',
        ['period_start', 'period_end'],
        ['profile_id', 'period_start', 'period_end'],
        False,
    ),
    'schedules': ('RESTRICT', None, [], [], True),
    'task_runs': ('CASCADE', None, [], [], True),
}


def upgrade() -> None:
    op.create_table(
        'profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_profiles')),
        sa.UniqueConstraint('name', name=op.f('uq_profiles_name')),
    )
    op.execute(sa.text('INSERT INTO profiles (name) VALUES (:name)').bindparams(name=DEFAULT_PROFILE_NAME))

    for table, (ondelete, old_unique, _, new_columns, with_index) in TABLES.items():
        op.add_column(table, sa.Column('profile_id', sa.Integer(), nullable=True))
        op.execute(f'UPDATE {table} SET profile_id = (SELECT min(id) FROM profiles)')
        op.alter_column(table, 'profile_id', nullable=False)
        op.create_foreign_key(
            op.f(f'fk_{table}_profile_id_profiles'), table, 'profiles', ['profile_id'], ['id'], ondelete=ondelete
        )
        if old_unique is not None:
            op.drop_constraint(old_unique, table, type_='unique')
            op.create_unique_constraint(op.f(f'uq_{table}_profile_id'), table, new_columns)
        if with_index:
            op.create_index(op.f(f'ix_{table}_profile_id'), table, ['profile_id'], unique=False)
    # поиск той же статьи в другом профиле: готовые текст и резюме копируются оттуда
    op.create_index('ix_papers_arxiv_id', 'papers', ['arxiv_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_papers_arxiv_id', table_name='papers')
    # прежняя схема знает один набор данных: остаются данные первого профиля
    for table in reversed(TABLES):
        op.execute(f'DELETE FROM {table} WHERE profile_id <> (SELECT min(id) FROM profiles)')
    for table, (_, old_unique, old_columns, _, with_index) in reversed(TABLES.items()):
        if with_index:
            op.drop_index(op.f(f'ix_{table}_profile_id'), table_name=table)
        if old_unique is not None:
            op.drop_constraint(op.f(f'uq_{table}_profile_id'), table, type_='unique')
            op.create_unique_constraint(old_unique, table, old_columns)
        op.drop_constraint(op.f(f'fk_{table}_profile_id_profiles'), table, type_='foreignkey')
        op.drop_column(table, 'profile_id')
    op.drop_table('profiles')
