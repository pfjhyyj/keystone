"""One-time import of offline and never-started legacy fixed identities."""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres


def test_registration_migration_preserves_latest_offline_and_legacy_members(empty_database_url):
    config = migration_config(
        Settings(
            config_file=None,
            database_url=empty_database_url,
            worker_ids=["keystone-worker", "never-started"],
        )
    )
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "multi_workers")
        first, latest, offline = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            for identifier, name, age, state in (
                (first, "keystone-worker", 3, "ready"),
                (latest, "keystone-worker", 2, "stopped"),
                (offline, "faulted-worker", 1, "ready"),
            ):
                connection.execute(
                    text("""
                    INSERT INTO keystone.worker_heartbeats
                      (instance_id,host_id,worker_id,state,started_at,heartbeat_at,
                       execution_version,config_digest,coordination_id)
                    VALUES (:id,'original-host',:name,:state,now()-make_interval(days=>:age),
                            now()-make_interval(days=>:age),'old-version','old-digest','volume')
                """),
                    {"id": identifier, "name": name, "age": age, "state": state},
                )
        command.upgrade(config, "head")
        with engine.begin() as connection:
            rows = {
                r.worker_id: r
                for r in connection.execute(text("SELECT * FROM keystone.worker_registrations"))
            }
            assert set(rows) == {"keystone-worker", "faulted-worker", "never-started"}
            assert rows["keystone-worker"].instance_id == latest
            assert rows["keystone-worker"].stopped_at is not None
            assert rows["faulted-worker"].instance_id == offline
            assert rows["faulted-worker"].stopped_at is None
            assert rows["never-started"].instance_id is None
            assert all(row.state == "registered" for row in rows.values())
            connection.execute(
                text(
                    "UPDATE keystone.worker_registrations SET state='retired',"
                    "retired_at=now() WHERE worker_id='never-started'"
                )
            )
        config.attributes["legacy_worker_ids"] = ["never-started", "should-not-be-imported"]
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT state FROM keystone.worker_registrations "
                        "WHERE worker_id='never-started'"
                    )
                )
                == "retired"
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM keystone.worker_registrations")) == 3
            )
        command.downgrade(config, "multi_workers")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM keystone.worker_heartbeats")) == 3
            assert (
                connection.scalar(text("SELECT to_regclass('keystone.worker_registrations')"))
                is None
            )
    finally:
        engine.dispose()
