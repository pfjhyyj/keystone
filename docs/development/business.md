# 业务接入

从已能登录的开发环境接入一个本人笔记模块；先完成快速接入，再按需查阅后面的规范。日常启动见[Quickstart](../../README.md#quickstart)。

[快速接入](#快速接入) · [分层规范](#模块分层与代码规范) · [脚手架参数](#脚手架参数与目录) · [任务接入](#任务与模块规范)

## 快速接入

### 生成与装配

在仓库根目录执行：

```bash
backend/.venv/bin/python scripts/new_business.py notes --entity note --label 笔记
```

Windows 使用 `backend/.venv/Scripts/python.exe`。脚手架创建业务目录和下一条迁移，采用服务端固定 `owner_id` 的本人范围、部分更新及审计摘要。先评审业务字段和对象策略，再按生成命令的提示完成三处显式装配：

```python
# backend/src/keystone/composition.py
from keystone.businesses.notes.catalog import CATALOG as NOTES
BUSINESS_CATALOGS = (("notes", NOTES),)

# backend/src/keystone/model_registry.py 的 load_models() 内
from keystone.businesses.notes import models as notes_models  # noqa: F401

# backend/src/keystone/bootstrap.py 的 create_app() 内
from keystone.businesses.notes.controllers import router as notes_router
app.include_router(notes_router)
```

已有声明时追加，不覆盖其他业务。启动会拒绝权限或路由冲突。停止旧开发进程后，在 `backend/` 执行：

```bash
uv run --locked keystone init
uv run --locked keystone check
uv run --locked keystone-api
```

初始管理员自动获得新功能权限；普通角色需要授予 `notes.note:read`、`notes.note:write`，已有 PAT 需要重新签发所需范围。管理员也不自动获得他人的笔记。

### 加入控制台

创建 `frontend/console/src/views/notes/NotesView.vue`：

```vue
<script setup lang="ts">
import { ref } from 'vue'
import { request } from '../../api/client'
import { useAction } from '../../composables/useAction'
import { usePagedList } from '../../composables/usePagedList'
import { can } from '../../stores/session'

type Note = { id: string; title: string }
const title = ref('')
const notes = usePagedList<Note>('notes.note:read', (offset) =>
  request<Note[]>(`/notes/notes?offset=${offset}`),
)
const { rows, loading, error, reload } = notes
const { busy, error: saveError, run } = useAction()
async function save() {
  await run(async () => {
    await request('/notes/notes', 'POST', { title: title.value })
    title.value = ''
    await reload()
  })
}
</script>

<template>
  <section aria-label="我的笔记">
    <h1>我的笔记</h1>
    <p v-if="error || saveError" role="alert">{{ error || saveError }}</p>
    <form v-if="can('notes.note:write')" @submit.prevent="save">
      <label>标题 <input v-model="title" required maxlength="200" /></label>
      <button :disabled="busy" type="submit">保存</button>
    </form>
    <p v-if="loading">正在加载</p>
    <ul><li v-for="row in rows" :key="row.id">{{ row.title }}</li></ul>
  </section>
</template>
```

在 `src/router/index.ts` 的 routes 中增加路由，在 navigation 中增加菜单：

```ts
// routes
{
  path: '/notes',
  component: () => import('../views/notes/NotesView.vue'),
  meta: { title: '我的笔记', permissions: ['notes.note:read'] },
},
// navigation
{ path: '/notes', label: '我的笔记', permissions: ['notes.note:read'] },
```

未列入平台固定分组的菜单会进入“业务”。详情页可以只注册路由，并用 meta 声明标题和页面权限；登录跳转使用已注册路由，不维护第三份路径白名单。后端仍负责每次授权，页面隐藏不能代替它。

### 验证业务闭环

登录控制台访问 `/console/notes`，创建笔记并刷新确认保存；以另一个有同样功能权限的用户登录，确认看不到或修改不了原笔记。API 为 `/api/v1/notes/notes`，PATCH 省略字段保持原值，显式 null 返回 422。

为自己的模块编写集成用例，至少核对本人范围、功能权限、PAT 交集、部分更新、审计摘要和回滚。可按 `backend/tests/integration/test_scaffold_business.py` 的完整生成与装配方式编写；测试只使用[专用随机库](../../backend/tests/README.md)，不指向刚才的开发库。

```bash
backend/.venv/bin/python scripts/check.py --scope backend
backend/.venv/bin/python scripts/check.py --scope frontend
```

后台执行按[任务类型](../execution.md#1-选择执行入口)选择 Task 或 Flow；人工决定和业务结论由业务 Service 保存。

## 模块分层与代码规范

| 层 | 负责 | 不承担 |
|---|---|---|
| Controller | HTTP、身份提取、传输、响应 | ORM、业务授权/事务 |
| Service | 用例、规则、授权、事务、审计、领域查询 | Request/Response、DBOS/FlowContext、SDK 协议 |
| Model | 单实体、约束、索引、关系 | 调度和业务用例 |
| Schema | 输入契约、显式白名单投影 | 数据库、Service/ORM/SDK/执行器 |
| Adapter | SDK、协议、存储和许可的技术协调 | 业务授权/事务、反向调用 Service/ORM |
| Task/Workflow | 原生编排、检查点、投递、进度 | 直接读写领域表、持久化运行对象 |

依赖为 Controller/执行适配 → Service → 本模块 Model/Adapter，各层可用轻量 Schema。每持久实体一文件、显式导出，不遍历 ORM 自动投影。纯计算任务可直接组合函数，不强制新增领域 Service。

Model Access 的 inference/billing 分别管理调用/账务协议，Service 不直接依赖 OpenAI/httpx2。Adapter 的 core 技术协调及 flow/adapters/dbos.py 的 DBOSClient 与公开 error 类型检查是明确例外，不放开其他执行器/领域导入。

## 脚手架参数与目录

从仓库根目录执行：

```bash
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
├── services/<resource>.py     # 本人对象范围、事务与审计摘要
└── controllers/<resource>.py  # APIRouter 与请求绑定
```

参数规则：

| 参数 | 默认值 | 规则 |
|---|---|---|
| `namespace` | 必填 | 小写字母开头，`[a-z0-9_]`，最多 48 字符；不能占用 `identity`、`file` 等平台前缀 |
| `--entity` | 同 namespace | 单数 snake_case，用于模型类名与表名后缀；`<namespace>_<entity>` 表名最多 63 字符 |
| `--resource` | entity + `s` | API 路径与 schemas/services 文件名 |
| `--label` | entity | 权限说明中的中文名称 |
| `--migrations` | `backend/migrations/versions` | 读取完整历史，直接生成 `NNNN_<namespace>_initial.py` |

脚手架只写文件，不改装配代码；生成后会打印需要手工完成的接入点。临时空迁移目录仅用于独立评审或测试；实际接入或评审追加顺序时，应复制完整 `versions/` 历史后再指定 `--migrations`，不能把空目录生成的编号直接合入现有链。

生成的是“本人所有”的最小资源示例：独立的 `owner_id` 由服务端固定为当前用户，列表和编辑均按它过滤，管理员也不能直接操作他人的对象。`created_by` 只记录创建事实，不承担对象归属。企业共享、角色范围或可转交资源应在业务 Service 中明确实现自己的策略，并相应修改范围测试；功能权限不能替代对象检查。

创建要求 `title`，`content` 默认为空；PATCH 只更新显式提交的字段，省略字段保持原值，显式 `null` 和空标题返回 422，空正文表示清空正文。空对象 PATCH 仍验证功能权限和对象归属，但不修改数据或增加成功审计。

## 接入权限目录

`catalog.py` 声明本业务的权限和审计动作：

```python
from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "notes.note:read": "查看笔记",
        "notes.note:write": "保存笔记",
    },
    audit_actions={
        "notes.note.save": {
            "before", "after", "title", "content_length", "content_sha256",
        },
    },
)
```

将权限目录加入 composition，装配位置见[快速接入](#生成与装配)。

规则：

- 权限键为 `<业务>.<资源>:<动作>`，审计键为 `<业务>.<资源>.<动作>`，最长 100 字符。
- 业务命名空间不能与平台前缀冲突，不能重复，不能覆盖平台权限。
- 审计字段必须是集合，只声明允许保存的字段；启动时校验，审计写入时再次校验。
- 白名单也检查嵌套字段。脚手架只记录标题、正文字数和 UTF-8 内容的 SHA-256，不将完整正文复制到审计；长正文的合法保存不能因审计超过 8 KiB 而失败。修改业务字段时同时维护摘要、白名单与测试。
- 新增权限只在下次发布后生效；管理员自动获得新权限，普通角色和已有个人令牌不会自动扩大。
- 修改 `catalog.py` 后重新发布 API 和 Worker，不支持热加载。

## 接入实体与迁移

`models/<entity>.py` 只放一个继承 `keystone.core.database.Base` 的实体，表名以业务命名空间为前缀，位于 `keystone` schema。`models/__init__.py` 显式导出：

```python
# businesses/notes/models/__init__.py
from .note import Note as Note
```

模型须加入 model_registry.load_models()，见[快速接入](#生成与装配)。

脚手架基于完整迁移历史的当前 head 生成 `backend/migrations/versions/NNNN_<namespace>_initial.py`，含建表与逆序回滚；序号全局连续，保持同一线性链。当前 head 与命名细则见[迁移说明](../../backend/migrations/README.md)。

生成前校验平铺路径、命名、重复序号/revision、序号连续性和线性依赖，支持带类型注解的静态元数据，失败时不写业务文件。若已有 `<namespace>_initial` 迁移，拒绝再生成初始建表；仅补回代码可使用 `--no-migration`。后续手工增量迁移遵循同一规则，并行开发在合并前处理尚未发布的编号冲突。完整历史和命名规范见[迁移说明](../../backend/migrations/README.md)。生成后评审字段、约束和索引，按快速接入步骤初始化并检查。

`keystone init` 会升级到当前 head 并应用 DBOS 迁移，重复执行保留已有数据和密码；`keystone check` 验证数据库、迁移版本和存储。

约定：

- 迁移只追加，不修改已发布迁移；不导入运行时 ORM 模型建表，也不调用 `create_all`。
- 测试使用随机隔离 PostgreSQL 库验证迁移结果与 ORM 一致；业务模型检查还会校验单实体文件、导出、表名前缀和反向依赖。
- 删除业务表同样通过新迁移完成，不能删除历史迁移“清理”。

## 接入 HTTP

`controllers/<resource>.py` 导出 `router`，用 `_service(request)` 获取服务；控制器只做参数绑定、状态码和响应，不查询 ORM、不开事务、不做业务授权。

在 bootstrap.create_app() 注册路由，见[快速接入](#生成与装配)。

脚手架路由为 `/api/v1/<namespace>/<resource>`，例如业务 `notes` 的 `notes` 资源使用 `/api/v1/notes/notes`。不同业务可以使用相同资源名。API 启动时检查已装配的路由（含嵌套 include 和不进入 OpenAPI 的接口）；重复的方法与路径阻止启动，仅修改路径参数名或显式写出默认的 `:str` 不能避免冲突。不同方法或不同业务前缀可以共存。请求校验失败由统一边界返回 422；安全错误使用 `ServiceError`，不要透传上游或数据库异常。

## 实现 Service

Service 组合 `AccessContext`：

```python
async def create(self, data: NoteCreate):
    async with self.context.access(
        "notes.note:write",
        action="notes.note.save",
        target="notes.note",
    ) as (session, auth):
        row = Note(
            **data.model_dump(), owner_id=auth.user.id,
            created_by=auth.user.id, updated_by=auth.user.id,
        )
        session.add(row)
        await session.flush()
        await self.context.log(
            session, auth, "notes.note.save", "notes.note", row.id, {"after": audit_snapshot(row)}
        )
        return view(row)
```

约定：

- 权限、业务写入和成功审计在同一事务提交；失败或拒绝审计在回滚后独立尝试。
- 普通业务读写使用默认的共享身份锁，同样处于数据库事务中；更新对象按需取得本业务的行锁。只有修改身份、角色、授权或凭据时设置 `identity_write=True`，取得身份排他锁。身份锁等待规则见[身份领域](../domains/identity.md)。
- 对象范围由业务明确判断。脚手架列表过滤 `owner_id`，编辑在同一 SQL 中按 ID 和 `owner_id` 过滤并锁行，不可见对象统一返回 404；客户端不能指定或修改归属。不统一添加管理员绕过。
- 成功审计的 `action` 必须已在 `catalog.py` 声明，字段必须在允许集合内，总大小不超过 8 KiB；上例的 `audit_snapshot` 使用生成 Service 内的受控摘要函数。
- 外部 I/O（文件、模型）不要放在长事务中；按现有 files/model_access 的短事务边界编排。
- 列表接口使用 `keystone.core.http.Page`，不要重复声明分页参数。
- 不新增通用 Repository、BaseService 或动态 CRUD；直接使用 SQLAlchemy 和明确的服务方法。

## 测试

至少覆盖：

1. `pytest backend/tests/unit/test_architecture.py`：模型注册、导出、表前缀和依赖方向。
2. 迁移：在随机库执行 `upgrade head`，确认与 ORM 元数据无差异。
3. 用例：功能权限、对象权限、审计字段、事务回滚、重复请求和边界输入。
4. 若接入 Flow：契约校验、发起幂等、取消、结果采用和文件引用。
5. 若增加前端：Vitest 覆盖页面状态与请求参数；需要时再加 Playwright。

可以参考 `backend/tests/integration/test_business_integration.py` 的装配方式。Flow 的测试夹具位于 `backend/tests/support/flow_fixture/`，不要把测试夹具放进生产 `businesses/`。

test_scaffold_business.py 用临时生成模块、真实 API 和随机库验证装配、迁移、授权及审计回滚；test_identity_locks.py 验证真实锁等待和取消，不以模板断言或 mock 数据库替代。

从仓库根目录运行相关检查（先按[测试说明](../../backend/tests/README.md)准备专用测试库）：

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_new_business.py backend/tests/unit/test_http_routes.py backend/tests/integration/test_scaffold_business.py backend/tests/integration/test_identity_locks.py --require-postgres
```

Windows 将解释器路径替换为 `backend/.venv/Scripts/python.exe`。统一后端验证使用 `backend/.venv/bin/python scripts/check.py --scope backend`。

## 接入 Flow / Agent（可选）

需要独立受理、查询、取消及输入/结果契约时使用 Flow；普通后台执行使用 Task，同步业务直接调用 Service。

最小步骤：

1. 在业务包的 `schemas/` 中声明 `FlowDefinition`（key/revision、输入输出模型、入口、所需权限、文件路径和模型角色）与 `FlowPackage`，保持轻量导入；模块根目录只放 `__init__.py` 与 `catalog.py`。
2. 在 `workflows/` 中用 `@DBOS.workflow(name=definition.entrypoint)` 注册稳定入口；步骤使用平台的 `safe_step` 错误边界，Agent 使用 `DBOSDurability`。
3. `composition.FLOW_PACKAGES` 加入该包；包的 execution_modules 显式列出 workflows 内的执行模块，API 不导入执行代码。
4. 权限必须已声明；文件路径在启动时按 Schema 编译；模型角色须声明 model:invoke 权限，发起时在业务 input 外提交 model_bindings。
5. 在测试中通过 `ExecutionIdentity`、`reconcile_one` 和 Worker 子进程验证执行与恢复。

Flow 只保存运行记录、权限和结果协调；业务结论、实体和采用动作仍归业务自己保存。详细边界见[Flow 开发](flow.md)和[Flow 领域](../domains/flow.md)。

业务 Service 在自己的身份事务中调用 `start_in_transaction`，一起保存业务受理记录和 Run。调用方授权业务对象并确定精确输入文件，Flow 负责原用户/PAT、契约、固定模型及幂等检查；平台不提供通用 Flow 发起 API。

业务执行守卫和 Worker 服务绑定使用 composition 的显式 `EXECUTION_GUARD` / `WORKFLOW_BINDINGS`，契约与调用时机见[Flow 开发](flow.md#业务执行接入)。不必替换平台的 FlowService、ExecutionIdentity 或 Worker 主入口。

## 前端（可选）

console 可直接承载业务页面，优先按现有 `views/<页面>/` 结构新增页面和路由并复用登录、权限与组件。业务确有界面或交付需求时也可使用独立前端工程。业务页面使用自己的输入表单、对象查询和受理接口。平台 Flow 页面只监控运行；console 不提供全局文件库，业务页面需要上传时按业务上传接口自行实现。

集成到 console 的业务页面随现有前端构建和部署；选择独立前端时才单独构建、部署，通过同源 API 代理接入，并继续遵守后端 Cookie 会话、Origin、CSRF 和权限契约，详见[身份与访问控制](../domains/identity.md#7-最小实现约定)。不要为普通业务扩展复制一套 console 或预建共享组件包。

## 检查清单

- [ ] 命名空间、权限、审计动作已声明且通过启动校验
- [ ] `composition.BUSINESS_CATALOGS` 已加入
- [ ] `model_registry.load_models()` 已显式导入模型
- [ ] 新迁移已评审，`keystone init` 与 `keystone check` 通过
- [ ] `bootstrap.py` 已注册路由
- [ ] 对象权限、审计和事务边界有测试
- [ ] Flow 契约、入口与权限已注册（如使用 Flow）
- [ ] 前端表单映射与权限显示已处理（如使用 console）
- [ ] `backend/.venv/bin/python scripts/check.py` 通过

## 任务与模块规范

### 选择接入方式

选择 Service、Task 或 Flow 使用[统一决策表](../execution.md#1-选择执行入口)。开发时按需求确定，不增加运行时分类器；普通工具不是独立 Task，普通后台任务无需领域模型。

### 模块目录与后台任务

目录和依赖以[分层规范](#模块分层与代码规范)为准。只创建实际使用的七类目录；根目录保留 __init__.py/catalog.py，纯业务判断就近放 services/，不预建 policies/runtime 或通用插件层。

后台任务接入：

1. 定义小型对象输入和任务函数；有领域操作时调用所属 Service，无需给纯计算增加 Service。
2. tasks/definitions.py 声明 TaskDefinition 和可选 ScheduleDefinition，输入 extra="forbid"，不保存 Runtime、凭据或句柄。
3. tasks/workflows.py 的工厂注册 Workflow/safe_step，按需注入窄 Service，返回 `(bindings, install)`。
4. 外部 I/O 放 safe_step，Workflow 层 enqueue_task；execution_key 标同次执行，coalesce_key 只合并活动工作，详细语义见[投递](../execution.md#3-投递与合并)。
5. composition 显式汇总绑定、计划及预算，由 queue_budgets 检查入口、输入和队列一致性。
6. 验证稳定 ID、合并、重启、依赖故障与领域幂等；不从请求接受任意处理函数、导入路径或执行身份。

API 的业务 Controller 在授权后通过注入的 Runtime 调用 `await runtime.tasks.enqueue(TASK, payload, execution_key=key)`；TASK 取自轻量 definitions。CLI 命令使用 build_runtime(settings) 装配相同客户端，在 finally 中 `await runtime.close()`，不启动 Worker。Service 不导入执行设施，普通函数的组合与任务投递放入口层。

业务需要 FlowService 时由装配层注入 runtime.flow_service(identity)，自行装配须显式提供 execution_version 来源，不反向导入 composition/bootstrap。普通 Service 不导入执行 Context。

新业务由装配层注入 runtime.managed_files(identity)，先授权对象再使用文件；保存后同事务登记业务关系、引用和审计。读取需排队时先进入 files.read_transfer()，再完成业务授权并读取。临时文件经 files.transfer() 使用 materialize_file/upload_file，物化后另取上传作用域；等待 I/O 和清理后退出，路径/传输对象不进入检查点。完整接口见[文件参考](../reference/files.md)。

### Flow 与业务操作

FlowDefinition/ModelRole/FlowPackage 放轻量 schemas，通过 FLOW_PACKAGES 装配。模型选择在 input 外，受理后固定；契约见[Flow](../domains/flow.md)，示例和业务守卫见[Flow 开发](flow.md)。

人工决定、结果版本和引用由业务 Service 保存，批准与后续独立 Flow 可在同一业务事务受理；不增加通用人工等待。
