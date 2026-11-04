"""Test businesses compose public services; these routes are never shipped by Keystone."""

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from keystone.bootstrap import create_app as platform_app
from keystone.core.content_response import ContentResponse
from keystone.core.errors import ServiceError
from keystone.core.http import Page
from keystone.platform.files import schemas as files
from keystone.platform.files.services.references import FileReferences, FileReferenceService
from keystone.platform.flow.schemas import Start
from keystone.platform.flow.schemas.files import compile_file_paths, extract_file_ids
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access.schemas import Invoke
from keystone.platform.model_access.schemas.images import (
    ImageEditRequest,
    ImageGenerateRequest,
    VisionRequest,
)
from keystone.platform.model_access.schemas.operations import Delivery, Operation
from keystone.platform.model_access.schemas.types import Key
from keystone.platform.model_access.services.runtime import safe_error
from support.business_catalog import PREFIX


class ImageGenerateInvoke(ImageGenerateRequest):
    model_key: Key


class ImageEditInvoke(ImageEditRequest):
    model_key: Key


class VisionInvoke(VisionRequest):
    model_key: Key


async def _setup(database):
    async with database.transaction() as session:
        await session.execute(
            text("""CREATE TABLE IF NOT EXISTS keystone.test_materials (
            id uuid PRIMARY KEY, owner_id uuid NOT NULL, retired boolean NOT NULL DEFAULT false)""")
        )
        await session.execute(
            text("""CREATE TABLE IF NOT EXISTS keystone.test_submissions (
            actor_id uuid NOT NULL, request_key text NOT NULL, run_id uuid NOT NULL,
            PRIMARY KEY (actor_id,request_key))""")
        )


async def material(session, auth, identifier):
    owned = await session.scalar(
        text("SELECT id FROM keystone.test_materials WHERE id=:id AND owner_id=:owner FOR UPDATE"),
        {"id": identifier, "owner": auth.user.id},
    )
    if owned is None:
        raise ServiceError(404, "material_not_found", "测试业务素材不可访问")
    return owned


async def adopt_in_transaction(identity, session, auth, identifier):
    await session.execute(
        text("""INSERT INTO keystone.test_materials (id,owner_id) VALUES (:id,:owner)
            ON CONFLICT DO NOTHING"""),
        {"id": identifier, "owner": auth.user.id},
    )
    await material(session, auth, identifier)
    await FileReferences(session, auth, "test_files").replace(
        str(identifier), "material", [identifier]
    )
    await identity.log(
        session,
        auth,
        "test_files.asset.save",
        "test_files.asset",
        identifier,
        {"status": "saved"},
    )
    return PREFIX + "inspect" in auth.permissions


async def adopt(identity, identifier):
    async with identity.access(PREFIX + "create") as (session, auth):
        return await adopt_in_transaction(identity, session, auth, identifier)


async def save_review_report(identity, runtime, run_id):
    """The test review business saves its completed report, without a Flow callback."""
    async with identity.access(PREFIX + "create") as (session, auth):
        known = await session.scalar(
            text(
                "SELECT run_id FROM keystone.test_submissions WHERE actor_id=:actor AND run_id=:run"
            ),
            {"actor": auth.user.id, "run": run_id},
        )
        if known is None:
            raise ServiceError(404, "review_not_found", "审核记录不可访问")
        result = await runtime.flow_service(identity).get_in_transaction(session, auth, run_id)
        if result["status"] != "succeeded":
            raise ServiceError(409, "review_incomplete", "审核尚未完成")
        identifier = UUID(result["result"]["report_file_id"])
        await adopt_in_transaction(identity, session, auth, identifier)
        return identifier


def create_app(settings):
    app = platform_app(settings)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            app.state.upload_slots = asyncio.Semaphore(settings.file_transfer_slots)
            await _setup(app.state.runtime.database)
            yield

    app.router.lifespan_context = lifespan
    router = APIRouter(prefix="/api/v1/_test")

    @router.get("/model-options")
    async def model_options(
        request: Request,
        page: Page,
        operation: Operation = "text.generate",
        delivery: Delivery = "sync",
    ):
        return await request.app.state.runtime.model_service(
            access_context(request)
        ).service.catalog(page.offset, page.limit, operation=operation, delivery=delivery)

    @router.post("/materials", status_code=201)
    async def upload(request: Request, data: Annotated[files.Upload, Query()]):
        identity = access_context(request)
        async with identity.access(PREFIX + "create"):
            pass
        target = request.app.state.runtime.managed_files(identity)
        try:
            async with asyncio.timeout(identity.settings.file_upload_timeout_seconds):
                async with request.app.state.upload_slots:
                    stored = await target.upload_body(data, request.stream())
        except TimeoutError:
            raise ServiceError(408, "upload_timeout", "上传超时，请重试") from None
        inspect = await adopt(identity, stored["id"])
        return stored if inspect else {"id": stored["id"]}

    @router.get("/materials/options")
    async def options(request: Request):
        identity = access_context(request)
        async with identity.access(PREFIX + "create"):
            pass
        return request.app.state.runtime.managed_files(identity).options()

    @router.get("/materials")
    async def listing(request: Request, offset: int = 0, limit: int = 50):
        identity = access_context(request)
        service = request.app.state.runtime.managed_files(identity)
        async with identity.access(PREFIX + "inspect") as (session, auth):
            ids = await session.scalars(
                text(
                    "SELECT id FROM keystone.test_materials WHERE owner_id=:owner "
                    "ORDER BY id OFFSET :offset LIMIT :limit"
                ),
                {"owner": auth.user.id, "offset": offset, "limit": limit},
            )
            return [
                await service.metadata_in_transaction(session, identifier) for identifier in ids
            ]

    @router.get("/materials/{identifier}")
    async def detail(identifier: UUID, request: Request):
        identity = access_context(request)
        async with identity.access(PREFIX + "inspect") as (session, auth):
            await material(session, auth, identifier)
            return await request.app.state.runtime.managed_files(identity).metadata_in_transaction(
                session, identifier
            )

    @router.api_route("/materials/{identifier}/content", methods=["GET", "HEAD"])
    async def content(identifier: UUID, request: Request):
        identity = access_context(request)
        service = request.app.state.runtime.managed_files(identity)

        @asynccontextmanager
        async def read():
            async with service.read_transfer() as transfer:
                async with identity.access(PREFIX + "content_read") as (session, auth):
                    exact = await material(session, auth, identifier)
                async with transfer.open_content(exact) as reader:
                    yield reader

        return ContentResponse(read(), request)

    @router.patch("/materials/{identifier}")
    async def edit(identifier: UUID, data: files.Edit, request: Request):
        identity = access_context(request)
        async with identity.access(PREFIX + "manage") as (session, auth):
            await material(session, auth, identifier)
            return await request.app.state.runtime.managed_files(identity).edit_in_transaction(
                session, auth, identifier, data
            )

    @router.delete("/materials/{identifier}", status_code=202)
    async def expire(identifier: UUID, request: Request):
        # A test-only maintenance operation, not a general business-delete endpoint.
        rt = request.app.state.runtime
        identity = access_context(request)
        with rt.storage.lease(identifier):
            async with identity.access(PREFIX + "manage") as (session, auth):
                await material(session, auth, identifier)
                accepted = await FileReferenceService(identity).retire_in_transaction(
                    session,
                    references={(f"test_files:{identifier}", "material", identifier)},
                    artifacts={identifier},
                    owner_id=auth.user.id,
                )
                if not accepted:
                    raise ServiceError(409, "file_referenced", "测试素材仍被使用")
                await session.execute(
                    text("UPDATE keystone.test_materials SET retired=true WHERE id=:id"),
                    {"id": identifier},
                )
        return {"id": identifier}

    @router.post("/review", status_code=202)
    async def review(data: Start, request: Request):
        rt = request.app.state.runtime
        identity = access_context(request)
        async with identity.access(action="flow.start", target="flow.run") as (session, auth):
            result = await rt.flow_service(identity).start_in_transaction(session, auth, data)
            known = await session.scalar(
                text(
                    "SELECT run_id FROM keystone.test_submissions "
                    "WHERE actor_id=:actor AND request_key=:key"
                ),
                {"actor": auth.user.id, "key": data.request_key},
            )
            if known is None:
                definition = rt.flow_definitions[data.definition_key]
                ids = extract_file_ids(
                    result["input"],
                    compile_file_paths(definition.input_schema(), definition.file_fields),
                )
                for identifier in ids:
                    auth.require(PREFIX + "content_read")
                    await material(session, auth, identifier)
                await session.execute(
                    text("INSERT INTO keystone.test_submissions VALUES (:actor,:key,:run)"),
                    {"actor": auth.user.id, "key": data.request_key, "run": result["id"]},
                )
            return result

    @router.post("/review/{identifier}/report")
    async def save_report(identifier: UUID, request: Request):
        return {
            "id": await save_review_report(
                access_context(request), request.app.state.runtime, identifier
            )
        }

    @router.post("/generate-text")
    async def generate_text(data: Invoke, request: Request):
        model = request.app.state.runtime.model_service(access_context(request))
        selected = await model.service.prepare_invoke(data.model_key)
        if not data.stream:
            return await model.invoke(data, selection=selected)

        async def events():
            try:
                async with aclosing(model.events(data, selection=selected)) as stream:
                    async for event in stream:
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            except Exception as error:
                status, code, detail = safe_error(error)
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "error", "http_status": status, "code": code, "detail": detail}
                    )
                    + "\n\n"
                )

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    async def image(data, request, operation):
        rt = request.app.state.runtime
        identity = access_context(request)
        model = rt.model_service(identity)
        selected = await model.service.selection(data.model_key, operation=operation)
        contract = {
            "image.generate": ImageGenerateRequest,
            "image.edit": ImageEditRequest,
            "image.understand": VisionRequest,
        }[operation]
        data = contract.model_validate(data.model_dump(mode="json", exclude={"model_key"}))
        maximum, max_bytes = model.image_limits(selected)
        if len(getattr(data, "images", ())) > maximum:
            raise ServiceError(422, "model_invalid_request", "图片数量超过协议上限")
        images = []
        for reference in getattr(data, "images", ()):
            async with identity.access(PREFIX + "content_read") as (session, auth):
                identifier = await material(session, auth, reference.file_id)
            async with rt.managed_files(identity).open_content(
                identifier, max_bytes=16 * 1024 * 1024
            ) as reader:
                body = b"".join([chunk async for chunk in reader.chunks()])
                images.append((reader.content_type, body, reference.detail))
                if sum(len(value[1]) for value in images) > max_bytes:
                    raise ServiceError(413, "model_input_too_large", "图片累计大小超过上限")
        identifier = uuid4()
        if operation == "image.understand":
            value = await model.invoke_vision(
                data,
                images=images,
                selection=selected,
                invocation_id=identifier,
                context=None,
                worker_identity=None,
            )
            return {"invocation_id": identifier, "content": value}
        async with identity.access(PREFIX + "create"):
            pass
        batch = await model.invoke_image(
            data,
            images=images,
            selection=selected,
            invocation_id=identifier,
            context=None,
            worker_identity=None,
        )
        saved = []
        for output, body in zip(batch.manifest.outputs, batch.contents, strict=True):
            result = await rt.managed_files(identity).upload(
                files.Upload(
                    name=output.artifact_key + ".png",
                    content_type=output.content_type,
                    expected_size=output.size_bytes,
                    checksum_sha256=output.checksum_sha256,
                    request_key=output.artifact_key,
                ),
                body,
            )
            await adopt(identity, result["id"])
            saved.append({"file_id": result["id"]})
        return {"invocation_id": identifier, "files": saved}

    @router.post("/generate-image")
    async def generate(data: ImageGenerateInvoke, request: Request):
        return await image(data, request, "image.generate")

    @router.post("/edit-image")
    async def edit_image(data: ImageEditInvoke, request: Request):
        return await image(data, request, "image.edit")

    @router.post("/understand-image")
    async def vision(data: VisionInvoke, request: Request):
        return await image(data, request, "image.understand")

    app.include_router(router)
    return app
