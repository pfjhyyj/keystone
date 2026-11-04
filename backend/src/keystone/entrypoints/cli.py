import argparse
import getpass
import json
import logging

from alembic import command
from sqlalchemy import select

from keystone.bootstrap import build_runtime
from keystone.composition import build_catalog
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.core.logging import configure_logging, safe_exception
from keystone.core.migrations import expected_heads, migration_config
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.identity.adapters.security import Passwords
from keystone.platform.identity.models import User
from keystone.platform.identity.schemas import UserCreate
from keystone.platform.identity.services.initialize import initialize_admin
from keystone.platform.model_access.services.maintenance import recover_api_calls


async def recover_model_calls(settings, *, api_stopped=False):
    if not api_stopped:
        raise ServiceError(409, "api_stop_required", "请先停止全部 API 进程并明确确认")
    database = Database(settings, catalog=build_catalog())
    try:
        checks = await database.readiness(expected_heads(settings))
        if any(value != "ok" for value in checks.values()):
            raise ServiceError(503, "database_not_ready", "请先运行 keystone init")
        count = await recover_api_calls(database, api_stopped=True)
        logging.getLogger(__name__).info("model_calls_recovered count=%s", count)
        return count
    finally:
        await database.close()


async def check_database(settings: Settings) -> bool:
    runtime = build_runtime(settings)
    try:
        result = await runtime.readiness()
        logging.getLogger(__name__).info("database_check %s", result["checks"])
        return result["status"] == "ready"
    finally:
        await runtime.close()


async def init_admin(settings, data):
    runtime = build_runtime(settings)
    try:
        if (await runtime.readiness())["status"] != "ready":
            raise ServiceError(503, "database_not_ready", "请先运行 keystone init")
        identifier = await initialize_admin(runtime.database, Passwords(), data)
        logging.getLogger(__name__).info("administrator_initialized user_id=%s", identifier)
    finally:
        await runtime.close()


async def ensure_admin(settings, *, username="admin", display_name="管理员", non_interactive=False):
    """Bootstrap an empty installation; never reset an existing account."""
    database = Database(settings, catalog=build_catalog())
    try:
        async with database.transaction() as session:
            if await session.scalar(select(User.id).limit(1)):
                logging.getLogger(__name__).info("administrator_already_initialized")
                return
    finally:
        await database.close()
    password = settings.initial_admin_password
    if password is None:
        if non_interactive:
            raise ServiceError(
                400,
                "initial_admin_password_required",
                "首次启动请设置 KEYSTONE_INITIAL_ADMIN_PASSWORD",
            )
        value = getpass.getpass("Administrator password (8–128 characters): ")
        if value != getpass.getpass("Confirm password: "):
            raise ServiceError(400, "password_mismatch", "Passwords do not match")
        password = value
    data = UserCreate(username=username, display_name=display_name, password=password)
    try:
        await init_admin(settings, data)
    except ServiceError as error:
        # Another initializer may have won the identity lock in the meantime.
        if error.code != "already_initialized":
            raise


def initialize(settings, *, username="admin", display_name="管理员", non_interactive=False):
    config = migration_config(settings)
    config.attributes["create_schema"] = True
    command.upgrade(config, "head")
    from dbos import DBOS

    DBOS.migrate(settings.database_url.get_secret_value(), schema="dbos")
    StorageRegistry(settings).initialize()
    run_async(
        ensure_admin(
            settings, username=username, display_name=display_name, non_interactive=non_interactive
        )
    )
    logging.getLogger(__name__).info("initialization_completed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Keystone deployment commands")
    parser.add_argument(
        "command",
        choices=[
            "init",
            "revision",
            "check",
            "init-admin",
            "init-storage",
            "recover-model-calls",
            "retire-worker",
            "reactivate-worker",
            "prune-history",
        ],
    )
    parser.add_argument("--username", help="Initial administrator login name")
    parser.add_argument("--worker-id", help="Fixed identity for retire-worker or reactivate-worker")
    parser.add_argument("--display-name", default="管理员")
    parser.add_argument("--apply", action="store_true", help="Delete one eligible history batch")
    parser.add_argument(
        "--history-offset", type=int, default=0, help="Continue a maintenance history scan"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Read the initial admin password from settings",
    )
    parser.add_argument(
        "--confirm-api-stopped",
        action="store_true",
        help="Confirm all API processes are stopped and will stay stopped during recovery",
    )
    args = parser.parse_args()
    if args.apply and args.command != "prune-history":
        parser.error("--apply only applies to prune-history")
    if not 0 <= args.history_offset <= 1_000_000_000 or (
        args.history_offset and args.command != "prune-history"
    ):
        parser.error("--history-offset must be a valid prune-history page offset")
    if args.command in {"retire-worker", "reactivate-worker"}:
        if not args.worker_id:
            parser.error("Worker lifecycle commands require --worker-id")
    elif args.worker_id:
        parser.error("--worker-id is only valid with worker lifecycle commands")
    if args.non_interactive and args.command != "init":
        parser.error("--non-interactive is only valid with init")
    if args.command == "recover-model-calls" and not args.confirm_api_stopped:
        parser.error(
            "recover-model-calls requires stopping all API processes and --confirm-api-stopped"
        )
    if args.confirm_api_stopped and args.command != "recover-model-calls":
        parser.error("--confirm-api-stopped is only valid with recover-model-calls")
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        if args.command == "init-storage":
            StorageRegistry(settings).initialize()
            logging.getLogger(__name__).info("storage_initialized")
            return
        if not settings.database_url:
            raise RuntimeError("Database is not configured")
        if args.command == "prune-history":

            async def prune():
                runtime = build_runtime(settings)
                runtime.history_offset = args.history_offset
                try:
                    checks = await runtime.database.readiness(runtime.heads)
                    if any(value != "ok" for value in checks.values()):
                        raise ServiceError(503, "database_not_ready", "请先运行 keystone init")
                    return await runtime.history_retention().execute(apply=args.apply)
                finally:
                    await runtime.close()

            print(json.dumps(run_async(prune()), ensure_ascii=False))
            return
        if args.command == "init":
            initialize(
                settings,
                username=args.username or "admin",
                display_name=args.display_name,
                non_interactive=args.non_interactive,
            )
            return
        if args.command == "recover-model-calls":
            run_async(
                recover_model_calls(
                    settings,
                    api_stopped=args.confirm_api_stopped,
                )
            )
            return
        if args.command in {"retire-worker", "reactivate-worker"}:
            from keystone.worker.lifecycle import change_worker

            run_async(
                change_worker(
                    settings, args.worker_id, reactivate=args.command == "reactivate-worker"
                )
            )
            print("Worker reactivated" if args.command == "reactivate-worker" else "Worker retired")
            return
        if args.command == "check":
            raise SystemExit(0 if run_async(check_database(settings)) else 1)
        if args.command == "init-admin":
            if not args.username:
                parser.error("init-admin requires --username")
            password = getpass.getpass("Administrator password (8–128 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ServiceError(400, "password_mismatch", "Passwords do not match")
            data = UserCreate(
                username=args.username, display_name=args.display_name, password=password
            )
            del password
            run_async(init_admin(settings, data))
            return
        command.current(migration_config(settings))
    except Exception as error:
        # Do not print raw settings/driver exceptions, which can contain secrets.
        logging.getLogger(__name__).error(
            "command_failed type=%s code=%s detail=%s",
            type(error).__name__,
            error.code if isinstance(error, ServiceError) else "invalid_configuration_or_database",
            error.detail if isinstance(error, ServiceError) else "请检查配置及数据库连接",
            extra={"exception": safe_exception(error)},
        )
        raise SystemExit(1) from None
