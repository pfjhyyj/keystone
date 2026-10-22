"""Create a business package and its first domain migration.

Usage:
    python scripts/new_business.py notes --entity note
    python scripts/new_business.py media --entity material --resource materials

The generator writes files only; it prints the explicit composition, bootstrap and
model-registry edits so the business stays a visible application declaration.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUSINESSES = ROOT / "backend/src/keystone/businesses"
MIGRATIONS = ROOT / "backend/migrations/versions"

NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,47}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
REVISION = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
DOWN_REVISION = re.compile(r'^down_revision = "([^"]+)"', re.MULTILINE)


def next_migration(versions: Path, namespace: str) -> tuple[str, str | None]:
    revisions = {}
    for path in sorted(versions.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        revision = REVISION.search(text)
        down = DOWN_REVISION.search(text)
        if revision and down:
            revisions[revision.group(1)] = down.group(1)
    heads = set(revisions) - set(revisions.values())
    if len(heads) > 1:
        raise SystemExit(f"Expected exactly one migration head, found {sorted(heads)}")
    return f"{namespace}_init", heads.pop() if heads else None


def render(path: Path, template: str, values: dict[str, str]) -> None:
    content = template
    for key, value in values.items():
        content = content.replace(f"@{key}@", value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def format_generated(*paths: Path) -> None:
    """Keep generated code identical to repository formatting when Ruff is installed."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(ROOT / "backend/pyproject.toml"),
            *(str(path) for path in paths),
        ],
        check=False,
        capture_output=True,
    )


FILES = {
    "__init__.py": '''"""@NAMESPACE@ business package."""
''',
    "catalog.py": '''"""Permissions and audit actions owned by the @NAMESPACE@ business."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "@NAMESPACE@.@ENTITY@:read": "查看@LABEL@",
        "@NAMESPACE@.@ENTITY@:write": "保存@LABEL@",
    },
    audit_actions={"@NAMESPACE@.@ENTITY@.save": {"before", "after"}},
)
''',
    "models/__init__.py": """from .@ENTITY@ import @CLASS@ as @CLASS@
""",
    "models/@ENTITY@.py": """from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class @CLASS@(Base):
    __tablename__ = "@TABLE@"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(String(4000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]
""",
    "schemas/__init__.py": """from .@RESOURCE@ import @CLASS@Create as @CLASS@Create
from .@RESOURCE@ import @CLASS@Edit as @CLASS@Edit
from .@RESOURCE@ import view as view
""",
    "schemas/@RESOURCE@.py": """from pydantic import Field

from keystone.core.schemas import Input


class @CLASS@Create(Input):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=4000)


class @CLASS@Edit(@CLASS@Create):
    pass


def view(row) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "content": row.content,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
""",
    "services/__init__.py": """from .@RESOURCE@ import @CLASS@Service as @CLASS@Service
""",
    "services/@RESOURCE@.py": """from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.identity.services.access import AccessContext

from ..models import @CLASS@
from ..schemas import @CLASS@Create, @CLASS@Edit, view

READ = "@NAMESPACE@.@ENTITY@:read"
WRITE = "@NAMESPACE@.@ENTITY@:write"
SAVE = "@NAMESPACE@.@ENTITY@.save"


class @CLASS@Service:
    def __init__(self, context: AccessContext):
        self.context = context

    async def listing(self, offset: int, limit: int):
        async with self.context.access(READ) as (session, _):
            rows = await session.scalars(
                select(@CLASS@)
                .order_by(@CLASS@.created_at.desc(), @CLASS@.id)
                .offset(offset)
                .limit(limit)
            )
            return [view(row) for row in rows]

    async def create(self, data: @CLASS@Create):
        async with self.context.access(
            WRITE, write=True, action=SAVE, target="@NAMESPACE@.@ENTITY@"
        ) as (session, auth):
            row = @CLASS@(**data.model_dump(), created_by=auth.user.id, updated_by=auth.user.id)
            session.add(row)
            await session.flush()
            await self.context.log(
                session,
                auth,
                SAVE,
                "@NAMESPACE@.@ENTITY@",
                row.id,
                {"after": data.model_dump()},
            )
            return view(row)

    async def edit(self, identifier: UUID, data: @CLASS@Edit):
        async with self.context.access(
            WRITE,
            write=True,
            action=SAVE,
            target="@NAMESPACE@.@ENTITY@",
            target_id=identifier,
        ) as (session, auth):
            row = await session.get(@CLASS@, identifier, with_for_update=True)
            if row is None:
                raise ServiceError(404, "not_found", "记录不存在")
            before = {key: getattr(row, key) for key in data.model_dump()}
            for key, value in data.model_dump().items():
                setattr(row, key, value)
            self.context.touch(row, auth)
            await self.context.log(
                session,
                auth,
                SAVE,
                "@NAMESPACE@.@ENTITY@",
                row.id,
                {"before": before, "after": data.model_dump()},
            )
            return view(row)
""",
    "controllers/__init__.py": """from fastapi import APIRouter

from .@RESOURCE@ import router as @RESOURCE@_router

router = APIRouter()
router.include_router(@RESOURCE@_router)
""",
    "controllers/@RESOURCE@.py": """from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.identity.controllers.dependencies import access_context

from ..schemas import @CLASS@Create, @CLASS@Edit
from ..services import @CLASS@Service

router = APIRouter(prefix="/api/v1/@RESOURCE@", tags=["@NAMESPACE@"])


def _service(request: Request) -> @CLASS@Service:
    return @CLASS@Service(access_context(request))


@router.get("")
async def listing(request: Request, page: Page):
    return await _service(request).listing(page.offset, page.limit)


@router.post("", status_code=201)
async def create(data: @CLASS@Create, request: Request):
    return await _service(request).create(data)


@router.patch("/{identifier}")
async def edit(identifier: UUID, data: @CLASS@Edit, request: Request):
    return await _service(request).edit(identifier, data)
""",
}

MIGRATION = '''"""Add the @NAMESPACE@ business table.

Business tables share the keystone schema and use the business prefix.
"""

import sqlalchemy as sa
from alembic import op

revision = "@REVISION@"
down_revision = @DOWN_REVISION@
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "@TABLE@",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.String(length=4000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )


def downgrade():
    op.drop_table("@TABLE@", schema="keystone")
'''


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate a Keystone business skeleton")
    parser.add_argument("namespace", help="Lowercase business namespace, for example notes")
    parser.add_argument("--entity", help="Entity name in singular snake_case (default: namespace)")
    parser.add_argument("--resource", help="API and module name (default: entity + s)")
    parser.add_argument("--label", help="Chinese display label (default: entity)")
    parser.add_argument("--output", type=Path, default=BUSINESSES, help="Businesses directory")
    parser.add_argument("--migrations", type=Path, default=MIGRATIONS, help="Migration directory")
    parser.add_argument("--no-migration", action="store_true", help="Skip the initial migration")
    args = parser.parse_args(argv)
    args.entity = args.entity or args.namespace
    args.resource = args.resource or args.entity + "s"
    args.label = args.label or args.entity
    for name, value in (("namespace", args.namespace), ("entity", args.entity)):
        if not IDENTIFIER.fullmatch(value):
            parser.error(f"{name} must match [a-z][a-z0-9_]*")
    if not IDENTIFIER.fullmatch(args.resource):
        parser.error("resource must match [a-z][a-z0-9_]*")
    if not NAMESPACE.fullmatch(args.namespace):
        parser.error("namespace must start with a lowercase letter and be at most 48 characters")
    if any(character in args.label for character in '"\n\r\\'):
        parser.error("label cannot contain quotes, backslashes or newlines")
    keys = [f"{args.namespace}.{args.entity}:{action}" for action in ("read", "write")]
    if max(map(len, keys)) > 100:
        parser.error("permission keys must be at most 100 characters")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    target = args.output / args.namespace
    if target.exists():
        raise SystemExit(f"{target} already exists; choose another namespace or remove it first")
    values = {
        "NAMESPACE": args.namespace,
        "ENTITY": args.entity,
        "RESOURCE": args.resource,
        "CLASS": "".join(part.capitalize() for part in args.entity.split("_")),
        "TABLE": f"{args.namespace}_{args.entity}",
        "LABEL": args.label,
    }
    generated = []
    for name, template in FILES.items():
        relative = name.replace("@ENTITY@", args.entity).replace("@RESOURCE@", args.resource)
        path = target / relative
        render(path, template, values)
        generated.append(path)

    if not args.no_migration:
        revision, down = next_migration(args.migrations, args.namespace)
        values |= {
            "REVISION": revision,
            "DOWN_REVISION": f'"{down}"' if down else "None",
        }
        migration = args.migrations / f"{revision}.py"
        if migration.exists():
            raise SystemExit(f"{migration} already exists")
        render(migration, MIGRATION, values)
        generated.append(migration)
        try:
            shown = migration.relative_to(ROOT)
        except ValueError:
            shown = migration
        print(f"Created migration {shown}")

    format_generated(*generated)

    print(f"Created business {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
    print(
        "\nNext edits:\n"
        f"1. composition.py: import {args.namespace} CATALOG and add "
        f'("{args.namespace}", CATALOG) to BUSINESS_CATALOGS.\n'
        f"2. bootstrap.py: import the {args.namespace} controllers router and include it.\n"
        f"3. model_registry.py: import {values['CLASS']} from the new models package.\n"
        "4. Run keystone init and keystone check, then verify the table matches the ORM.\n"
        "5. Add tests under backend/tests/ and a business frontend if needed.\n"
        "See docs/extending.md for the complete checklist."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
