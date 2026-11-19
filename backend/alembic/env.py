import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import models so Base.metadata is populated
from app.database import Base  # noqa: F401
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from DATABASE_URL env var (avoids ephemeral DB in Docker)
_db_url = os.environ.get("DATABASE_URL", "sqlite:////data/wellness.db")
# Alembic requires a sync driver — strip +aiosqlite if present
_db_url = _db_url.replace("sqlite+aiosqlite:///", "sqlite:///")
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
