# 新增业务

Keystone 不在运行时扫描或热加载业务。一个业务由**显式声明**和**显式导入**组成：目录中的代码只有被 `composition.py`、`bootstrap.py` 和 `model_registry.py` 引用后才生效。启动时校验命名空间、权限、审计动作、路由和 Flow 契约，声明错误直接阻止启动。

业务可以只使用身份、文件、字典、模型和 Flow 等底座能力，也可以拥有自己的 ORM 实体、HTTP 接口和 Flow/Agent。先按需增加，不要预建空目录或插件层。

## 0. 快速开始

从仓库根目录执行：

```bash
# 生成 businesses/notes 与下一条迁移
python scripts/new_business.py notes --entity note --label 笔记

# 只生成代码，不生成迁移
python scripts/new_business.py notes --entity note --no-migration

# 生成到临时目录，用于评审或测试
python scripts/new_business.py notes --entity note \
  --output /tmp/businesses --migrations /tmp/migrations
```

脚手架生成：

```text
businesses/<namespace>/
├── __init__.py
├── catalog.py                 # 权限与审计动作
├── models/<entity>.py         # 一个持久化实体一个文件
├── schemas/<resource>.py      # 输入契约与显式响应投影
├── services/<resource>.py     # 用例、对象权限、事务与审计
└── controllers/<resource>.py  # APIRouter 与请求绑定
```

参数规则：

| 参数 | 默认值 | 规则 |
|---|---|---|
| `namespace` | 必填 | 小写字母开头，`[a-z0-9_]`，最多 48 字符；不能占用 `identity`、`file` 等平台前缀 |
| `--entity` | 同 namespace | 单数 snake_case，用于模型类名与表名后缀 |
| `--resource` | entity + `s` | API 路径与 schemas/services 文件名 |
| `--label` | entity | 权限说明中的中文名称 |
| `--migrations` | `backend/migrations/versions` | 读取当前 head，生成 `<namespace>_init.py` |

脚手架只写文件，不改装配代码；生成后会打印需要手工完成的接入点。

## 1. 接入权限目录

`catalog.py` 声明本业务的权限和审计动作：

```python
from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "notes.note:read": "查看笔记",
        "notes.note:write": "保存笔记",
    },
    audit_actions={"notes.note.save": {"before", "after"}},
)
```

在 `backend/src/keystone/composition.py` 显式导入并加入元组：

```python
from keystone.businesses.notes.catalog import CATALOG as NOTES

BUSINESS_CATALOGS = (("notes", NOTES),)
```

规则：

- 权限键为 `<业务>.<资源>:<动作>`，审计键为 `<业务>.<资源>.<动作>`，最长 100 字符。
- 业务命名空间不能与平台前缀冲突，不能重复，不能覆盖平台权限。
- 审计字段必须是集合，只声明允许保存的字段；启动时校验，审计写入时再次校验。
- 新增权限只在下次发布后生效；管理员自动获得新权限，普通角色和已有个人令牌不会自动扩大。
- 修改 `catalog.py` 后重新发布 API 和 Worker，不支持热加载。

## 2. 接入实体与迁移

`models/<entity>.py` 只放一个继承 `keystone.core.database.Base` 的实体，表名以业务命名空间为前缀，位于 `keystone` schema。`models/__init__.py` 显式导出：

```python
# businesses/notes/models/__init__.py
from .note import Note as Note
```

在 `backend/src/keystone/model_registry.py` 的 `load_models()` 中增加显式导入：

```python
from keystone.businesses.notes import models as notes_models  # noqa: F401
```

脚手架已按当前 head 生成一份迁移，包含建表与逆序回滚。迁移按领域/语义命名（如 `notes_init.py`），`revision` 与文件名一致，`down_revision` 指向生成时的 head。生成后评审字段、约束和索引，再执行：

```bash
cd backend
uv run --locked keystone init
uv run --locked keystone check
```

`keystone init` 会升级到当前 head 并应用 DBOS 迁移，重复执行保留已有数据和密码；`keystone check` 验证数据库、迁移版本和存储。

约定：

- 迁移只追加，不修改已发布迁移；不导入运行时 ORM 模型建表，也不调用 `create_all`。
- 测试使用随机隔离 PostgreSQL 库验证迁移结果与 ORM 一致；业务模型检查还会校验单实体文件、导出、表名前缀和反向依赖。
- 删除业务表同样通过新迁移完成，不能删除历史迁移“清理”。

## 3. 接入 HTTP

`controllers/<resource>.py` 导出 `router`，用 `_service(request)` 获取服务；控制器只做参数绑定、状态码和响应，不查询 ORM、不开事务、不做业务授权。

在 `backend/src/keystone/bootstrap.py` 显式导入并注册：

```python
from keystone.businesses.notes.controllers import router as notes_router

app.include_router(notes_router)
```

路由统一挂在 `/api/v1` 下，路径带业务资源名（例如 `/api/v1/notes`）。请求校验失败由统一边界返回 422；安全错误使用 `ServiceError`，不要透传上游或数据库异常。

## 4. 实现 Service

Service 组合 `AccessContext`：

```python
async def create(self, data: NoteCreate):
    async with self.context.access(
        "notes.note:write",
        write=True,
        action="notes.note.save",
        target="notes.note",
    ) as (session, auth):
        row = Note(**data.model_dump(), created_by=auth.user.id, updated_by=auth.user.id)
        session.add(row)
        await session.flush()
        await self.context.log(
            session, auth, "notes.note.save", "notes.note", row.id, {"after": data.model_dump()}
        )
        return view(row)
```

约定：

- 权限、业务写入和成功审计在同一事务提交；失败或拒绝审计在回滚后独立尝试。
- 写入使用 `write=True` 取得身份排他锁；对象范围由业务明确判断，不能只依赖功能权限。
- 成功审计的 `action` 必须已在 `catalog.py` 声明，字段必须在允许集合内，总大小不超过 8 KiB。
- 外部 I/O（文件、模型）不要放在长事务中；按现有 files/model_access 的短事务边界编排。
- 列表接口使用 `keystone.core.http.Page`，不要重复声明分页参数。
- 不新增通用 Repository、BaseService 或动态 CRUD；直接使用 SQLAlchemy 和明确的服务方法。

## 5. 测试

至少覆盖：

1. `pytest backend/tests/test_business_models.py`：模型注册、导出、表前缀和依赖方向。
2. 迁移：在随机库执行 `upgrade head`，确认与 ORM 元数据无差异。
3. 用例：功能权限、对象权限、审计字段、事务回滚、重复请求和边界输入。
4. 若接入 Flow：契约校验、发起幂等、取消、结果采用和文件引用。
5. 若增加前端：Vitest 覆盖页面状态与请求参数；需要时再加 Playwright。

可以参考 `backend/tests/test_business_integration.py` 的装配方式。Flow 的测试夹具位于 `backend/tests/flow_fixture/`，不要把测试夹具放进生产 `businesses/`。

## 6. 接入 Flow / Agent（可选）

业务不要求使用 Flow。普通请求可以直接调用自己的 Service；只有需要持久执行、长任务、Agent 或多步骤编排时才增加 Flow。

最小步骤：

1. 在业务包中增加 `contracts.py`，声明 `Definition`（key、输入输出模型、入口、所需权限、文件字段、可选模型字段）。
2. 在业务包中增加 `workflow.py`，用 `@DBOS.workflow(name=definition.entrypoint)` 注册稳定入口；步骤使用 `@DBOS.step`，Agent 使用 `DBOSDurability`。
3. `composition.FLOW_DEFINITIONS` 加入该定义；`composition.register_workflows()` 中显式导入 workflow，API 不会导入它。
4. 权限必须已声明；文件字段和模型字段在启动时按输入 Schema 校验。
5. 在测试中通过 `ExecutionIdentity`、`reconcile_one` 和 Worker 子进程验证执行与恢复。

Flow 只保存运行记录、权限和结果协调；业务结论、实体和采用动作仍归业务自己保存。详细边界见[Flow 开发](flow-development.md)和[Flow 领域](domains/flow.md)。

## 7. 前端（可选）

console 是平台控制台，业务页面应放进独立的业务前端工程，或按现有 `views/<页面>/` 结构新增。需要为某个 Flow 提供专用输入表单时，在 `frontend/console/src/views/flows/forms.ts` 注册组件；未注册的定义继续使用通用 JSON 表单：

```ts
import type { Component } from 'vue'
import NotesInput from '../../businesses/notes/FlowInput.vue'

export const flowForms: Readonly<Record<string, Component>> = {
  'notes.note.review': NotesInput,
}
```

业务前端独立构建、独立部署，共用后端 Cookie 会话和权限。不要为业务扩展复制一套 console 或引入共享组件包。

## 8. 检查清单

- [ ] 命名空间、权限、审计动作已声明且通过启动校验
- [ ] `composition.BUSINESS_CATALOGS` 已加入
- [ ] `model_registry.load_models()` 已显式导入模型
- [ ] 新迁移已评审，`keystone init` 与 `keystone check` 通过
- [ ] `bootstrap.py` 已注册路由
- [ ] 对象权限、审计和事务边界有测试
- [ ] Flow 契约、入口与权限已注册（如使用 Flow）
- [ ] 前端表单映射与权限显示已处理（如使用 console）
- [ ] `backend/.venv/bin/python scripts/check.py` 通过
