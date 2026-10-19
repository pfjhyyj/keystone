from alembic import context
from sqlalchemy import create_engine, pool, text

from keystone.model_registry import load_models

config = context.config
metadata = load_models()


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "keystone"
    return True


def run_migrations():
    # Only the deployment CLI opens this path. API and Worker perform read-only checks.
    engine = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        # Keep schema-qualified tables/FKs stable when the login role is also "keystone".
        connect_args={"connect_timeout": 3, "options": "-csearch_path=public"},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            if config.attributes.get("create_schema"):
                with connection.begin():
                    connection.execute(text("CREATE SCHEMA IF NOT EXISTS keystone"))
            context.configure(
                connection=connection,
                target_metadata=metadata,
                version_table_schema="keystone",
                include_schemas=True,
                include_name=include_name,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
