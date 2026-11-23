"""Locate the platform chain and the explicitly registered business chains.

The platform chain lives in migrations/versions with revisions NNNN. Each business keeps its own
chain in migrations/businesses/<namespace> with revisions <namespace>_NNNN, so a downstream
project adds a directory instead of competing with upstream for sequence numbers.
"""

import os
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from keystone.core.config import Settings

# Alembic stores revisions in VARCHAR(32), so "<namespace>_NNNN" bounds the namespace.
CHAIN = re.compile(r"[a-z][a-z0-9_]{0,26}")
PLATFORM_FILE = re.compile(r"([0-9]{4})_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*\.py")
BUSINESS_FILE = re.compile(r"([0-9]{4})_[a-z][a-z0-9_]*\.py")


def migration_config(settings: Settings, chains: tuple[str, ...] = ()) -> Config:
    directory = Path(__file__).resolve().parents[3] / "migrations"
    if not (directory / "env.py").is_file():
        # The non-editable container package runs from /app beside migrations/.
        directory = Path.cwd() / "migrations"
    if not (directory / "env.py").is_file():
        raise RuntimeError(
            "Migration files missing; deploy the backend with its migrations directory"
        )
    directory = directory.resolve()
    if len(set(chains)) != len(chains) or not all(CHAIN.fullmatch(name) for name in chains):
        raise RuntimeError("Business migration chains must be unique lowercase namespaces")
    locations = [directory / "versions"]
    for name in chains:
        location = directory / "businesses" / name
        if not location.is_dir():
            raise RuntimeError(f"Business migration chain missing: {name}")
        locations.append(location)
    config = Config()
    config.set_main_option("script_location", str(directory).replace("%", "%%"))
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "version_locations",
        os.pathsep.join(str(location).replace("%", "%%") for location in locations),
    )
    if settings.database_url:
        config.set_main_option(
            "sqlalchemy.url", settings.database_url.get_secret_value().replace("%", "%%")
        )
    return config


def _declared(value) -> set[str]:
    return {value} if isinstance(value, str) else set(value or ())


def _chain_of(path: Path, versions: Path, businesses: Path) -> str | None:
    parent = path.resolve().parent
    if parent == versions:
        return None
    if parent.parent == businesses:
        return parent.name
    raise RuntimeError(f"Migration outside a registered chain: {path.name}")


def migration_scripts(config: Config) -> ScriptDirectory:
    """Load the revisions and reject anything that is not one linear chain per owner."""
    script = ScriptDirectory.from_config(config)
    root = Path(script.dir).resolve()
    versions, businesses = root / "versions", root / "businesses"
    chains: dict[str | None, dict[int, object]] = {}
    for revision in script.walk_revisions():
        path = Path(revision.path)
        chain = _chain_of(path, versions, businesses)
        match = (PLATFORM_FILE if chain is None else BUSINESS_FILE).fullmatch(path.name)
        expected = None if match is None else (match[1] if chain is None else f"{chain}_{match[1]}")
        if expected is None or revision.revision != expected:
            raise RuntimeError(f"Migration name and revision disagree: {path.name}")
        chains.setdefault(chain, {})[int(match[1])] = revision
    platform = {revision.revision for revision in chains.get(None, {}).values()}
    for chain, revisions in chains.items():
        if sorted(revisions) != list(range(1, len(revisions) + 1)):
            raise RuntimeError(f"Migration sequence must be contiguous: {chain or 'platform'}")
        previous = None
        for number in sorted(revisions):
            revision = revisions[number]
            # Alembic spreads labels along a branch; check what the file itself declares.
            labels = _declared(revision.module.branch_labels)
            dependencies = _declared(revision.dependencies)
            if revision.down_revision != previous:
                raise RuntimeError(f"Migration chain is not linear: {revision.revision}")
            if chain is None:
                if labels or dependencies:
                    raise RuntimeError(f"Platform migrations cannot depend on businesses: {number}")
            elif labels != ({chain} if number == 1 else set()) or not dependencies <= platform:
                raise RuntimeError(
                    f"Business migrations take their label once and depend only on the "
                    f"platform: {revision.revision}"
                )
            previous = revision.revision
    return script


def stored_heads(script: ScriptDirectory) -> set[str]:
    """The rows Alembic keeps once every head is applied.

    A head that another chain depends on is recorded through its dependant: when a business
    revision depends on the platform head, the version table holds only the business row.
    """
    heads = set(script.get_heads())
    covered, pending = set(), list(heads)
    while pending:
        revision = script.get_revision(pending.pop())
        for parent in _declared(revision.down_revision) | _declared(revision.dependencies):
            if parent not in covered:
                covered.add(parent)
                pending.append(parent)
    return heads - covered


def expected_heads(settings: Settings, chains: tuple[str, ...] = ()) -> set[str]:
    return stored_heads(migration_scripts(migration_config(settings, chains)))
