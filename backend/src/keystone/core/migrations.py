from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from keystone.core.config import Settings


def migration_config(settings: Settings) -> Config:
    directory = Path(__file__).resolve().parents[3] / "migrations"
    if not (directory / "env.py").is_file():
        # The non-editable container package runs from /app beside migrations/.
        directory = Path.cwd() / "migrations"
    if not (directory / "env.py").is_file():
        raise RuntimeError(
            "Migration files missing; deploy the backend with its migrations directory"
        )
    config = Config()
    config.set_main_option("script_location", str(directory.resolve()).replace("%", "%%"))
    if settings.database_url:
        config.set_main_option(
            "sqlalchemy.url", settings.database_url.get_secret_value().replace("%", "%%")
        )
    return config


def expected_heads(settings: Settings) -> set[str]:
    return set(ScriptDirectory.from_config(migration_config(settings)).get_heads())
