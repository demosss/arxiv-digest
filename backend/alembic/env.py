from alembic import context

import app.models  # noqa: F401  # регистрирует все таблицы в Base.metadata
from app.core.db import Base, get_sync_engine
from app.core.logging import configure_logging

configure_logging("migrate")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_sync_engine().url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with get_sync_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
