from logging.config import fileConfig
from alembic import context
from backend.app.db import engine
from backend.app.models import Base

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata

def migrate(connection):
    sqlite = connection.dialect.name == 'sqlite'
    if sqlite:
        # SQLite table rebuilds require this only on the migration connection.
        # App connections always enforce foreign keys through build_engine.
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, render_as_batch=sqlite)
        with context.begin_transaction():
            context.run_migrations()
        if sqlite and connection.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('Migration produced invalid references')
        connection.commit()
    finally:
        if sqlite:
            connection.rollback()
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.commit()


if context.is_offline_mode():
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get('connection')
    if supplied is not None:
        migrate(supplied)
    else:
        with engine.connect() as connection:
            migrate(connection)
