# 新增业务

**阅读状态：**第 0～8 节是当前脚手架和业务 Flow 接入方法。第 9 节中的任务设施与模块 tasks/ 已实现；FlowPackage、素材粗选和具体审核业务仍为目标设计。完整契约见[任务执行与模块规范设计](execution-design.md)。

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
| `--entity` | 同 namespace | 单数 snake_case，用于模型类名与表名后缀；`<namespace>_<entity>` 表名最多 63 字符 |
| `--resource` | entity + `s` | API 路径与 schemas/services 文件名 |
| `--label` | entity | 权限说明中的中文名称 |
| `--migrations` | `backend/migrations/versions` | 读取完整历史，直接生成 `NNNN_<namespace>_initial.py` |

脚手架只写文件，不改装配代码；生成后会打印需要手工完成的接入点。临时空迁移目录仅用于独立评审或测试；实际接入或评审追加顺序时，应复制完整 `versions/` 历史后再指定 `--migrations`，不能把空目录生成的编号直接合入现有链。

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

脚手架按当前 head 生成一份迁移，包含建表与逆序回滚。路径为 `backend/migrations/versions/NNNN_<namespace>_initial.py`，文件直接平铺，不建立分类或模块子目录；模块归属体现在文件名中，序号在所有模块间连续递增。以当前历史为例，notes 生成 `0011_notes_initial.py`，`revision = "0011"`、`down_revision = "worker_registration"`。历史语义 revision 保留原值；执行仍由 Alembic 原生依赖决定。

生成前校验平铺路径、命名、重复序号/revision、序号连续性和线性依赖，支持带类型注解的静态元数据，失败时不写业务文件。若已有 `<namespace>_initial` 迁移，拒绝再生成初始建表；仅补回代码可使用 `--no-migration`。后续手工增量迁移遵循同一规则，并行开发在合并前处理尚未发布的编号冲突。完整历史和命名规范见[迁移说明](../backend/migrations/README.md)。生成后评审字段、约束和索引，再执行：

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

1. `pytest backend/tests/unit/test_architecture.py`：模型注册、导出、表前缀和依赖方向。
2. 迁移：在随机库执行 `upgrade head`，确认与 ORM 元数据无差异。
3. 用例：功能权限、对象权限、审计字段、事务回滚、重复请求和边界输入。
4. 若接入 Flow：契约校验、发起幂等、取消、结果采用和文件引用。
5. 若增加前端：Vitest 覆盖页面状态与请求参数；需要时再加 Playwright。

可以参考 `backend/tests/integration/test_business_integration.py` 的装配方式。Flow 的测试夹具位于 `backend/tests/support/flow_fixture/`，不要把测试夹具放进生产 `businesses/`。

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

## 9. 任务与模块规范

模块目录和任务接入已实现，可按第 9.2 节扩展；第 9.3 节的 FlowPackage 仍为目标设计。公共契约、代码示例与迁移约束以[任务执行与模块规范设计](execution-design.md)为准。

### 9.1 选择接入方式

| 需求 | 接入方式 | 示例 |
|---|---|---|
| 当前请求内有界完成 | 公开 Service，按需提供 Controller | 保存配置、查询、手动查费 |
| 推进持久后台待办 | TaskDefinition + 原生 DBOS workflow/step | 费用核对、文件清理 |
| 独立受理并管理一次业务运行 | 当前 Definition + 显式注册；目标 FlowPackage | 文件审查；素材粗选、生图、生视频待接入 |
| Flow 内部能力 | Step、Agent、工具或普通函数 | 抽帧、定位、审片、标签 |
| 人工继续/驳回 | 业务 Service 保存决定，通过后受理独立 Flow | 审核结果版本后再发布 |

Task 和 Flow 共用 DBOS，但维护 Task 不创建 FlowRun，每个工具也不注册成 Task。任务耗时、步骤数量或是否含 AI 不决定分类。

### 9.2 模块目录与后台任务

模块按 MVC + Service 组织：controllers/ 处理 HTTP，schemas/ 放输入输出与显式投影，models/ 放实体，services/ 管用例、权限、事务、审计及幂等。Vue 页面承担 View。按需增加 adapters/ 放技术协议，tasks/definitions.py 放轻量任务声明，tasks/workflows.py 放原生 DBOS 执行适配，workflows/<flow>/ 放业务契约、编排、Agent 与资源。

根目录只允许 __init__.py 包说明和 catalog.py 权限/审计声明；不放散落的 policy、host、storage、helpers 实现，不新增一级 policies/ 或 runtime/。纯判断就近放 services/，复杂后可分 services/policies/。一级目录必须属于上述七类并有 __init__.py；没有职责不建空目录。Model Access 的 inference/billing 和凭据、HTTP、协调能力已归 adapters/。独立分发的流程包不需要空建 ORM/HTTP 目录。

Flow 定义使用 `from keystone.platform.flow.schemas.definitions import Definition`；业务执行上下文使用 `from keystone.platform.flow.workflows import context as host`。Flow 根目录 catalog.py 只声明 flow 权限和审计动作，不保存 Definition 或执行代码。API 读取轻量契约；Worker 才导入 workflows/。受权文件操作归 services/resources.py，DBOSClient 包装归 adapters/dbos.py，业务服务不导入执行上下文。

依赖方向为 Controller/执行适配 → Service → Model/Adapter。Controller 不访问 ORM 或开启事务，Schema 不调用 Service/Adapter，Adapter 不调用 Service 或领域 ORM，Service 不依赖 HTTP/协议 SDK/DBOS 实现。tasks/workflows 只编排服务，不直接查询领域表。架构检查递归覆盖 platform 和 businesses，也检查脚手架生成结果；新增文件或层不能绕过检查。

跨模块只使用公开 Service、Schema 或轻量任务声明，禁止导入其他模块的 models/、adapters/；平台不导入具体业务，core 不导入领域或应用组装。DBOSClient 例外仅限 platform/flow/adapters/dbos.py。文件对象授权使用当前 Authorization.role_ids；需要验证用户/角色引用时，在 AccessContext.access() 提供的 session 中调用 Identity 的 require_subjects，不自行查询身份表。

本地文件操作通过 FileService.transfer() 取得短期 FileTransferService，再调用 materialize_file 或 upload_file；同一作用域只允许一个活动操作，复制物化文件时另取上传作用域。调用方必须等待操作和清理完成，不能保存传输对象或把路径写入 DBOS 检查点。Flow 继续使用 host.materialize_file/upload_file，其宿主负责原身份与取消，Files 负责槽位、临时文件和受控上传。共享协调卷标记及锁能力使用 core/local_leases.py，不跨模块引用 Files Adapter。

模块不导入 composition/bootstrap 等上层装配代码。需组合 Flow 的业务由装配层传入 `runtime.flow_service(identity)` 创建的服务，再调用原有 start_in_transaction 等入口；自行装配 FlowService 时显式传入 `execution_version` 查询函数。执行身份和协调器使用当前 Runtime 注入的同一版本来源，不能绕过校验或把版本、身份回调作为持久任务输入。架构检查逐条判断导入例外；同文件导入轻量 contracts 不会放行另一个执行实现导入，definitions 也不会匹配 definitions_runtime 等近似名称。

后台任务接入顺序：

1. 所属模块定义业务待办、授权、事务、审计和幂等，普通 Service 不导入 DBOS。
2. 在 tasks/definitions.py 声明 TaskDefinition 和可选 ScheduleDefinition，输入模型使用 `extra="forbid"` 且序列化为小型 JSON 对象。仅含必要标识，拒绝 RootModel、运行时句柄和凭据。
3. 在 tasks/workflows.py 的 register 工厂绑定服务与 Worker 观测，注册 workflow 和 safe_step，返回 `(bindings, install)`。参考现有费用查询、文件清理和 Flow 同步的实现。
4. 查询/外部 I/O 放 safe_step；子任务投递在 workflow 层使用 `enqueue_task(binding, payload, execution_key=..., coalesce_key=...)`。返回实际执行引用；不传 coalesce_key 时独立投递，同 execution_key 复用原执行；传入时只合并活动工作，活动键自动附加制品版本。每个 Step 的错误在写入引擎前脱敏，默认不自动重试。
5. composition 显式汇总绑定、计划及队列预算，通过 `planning.queue_budgets` 检查唯一性、实际 DBOS 绑定、计划目标/输入与队列预算，API 不导入重型执行代码。
6. 验证执行 ID、活动合并、重启、忙碌暂缓、依赖故障、残留 tick 和业务状态幂等。

不从请求接收任意处理函数、导入路径或执行身份。execution_key 恢复同一次执行，coalesce_key 只合并活动工作；同一范围的队列合并不能替代 API/Worker 共享锁。需要逐次处理的事件不能用类型级合并键。

### 9.3 FlowPackage 与素材粗选

目标包通过 definitions.py 提供轻量流程定义、输入输出模型、契约 revision、受限文件路径、角色模型字段，以及显式执行模块/资源清单。宿主受理时固定 ModelEntry，API 只读契约，Worker 加载 workflow、Agent、工具、提示词和 Skills。FlowContext 只暴露当前 Run 的受控能力。

```text
businesses/<namespace>/workflows/<flow>/
  contracts.py
  workflow.py
  steps/  agents/  tools/  prompts/  skills/    # 按需

独立流程包/
  definitions.py                               # FlowPackage 与轻量流程定义
  domain/
  adapters/
  workflows/                                  # 不要求 ORM 或 Controller
```

素材粗选保留准备原片 → 发现 → 定位 → 实际裁片 → 独立验收 → 标签 → 有界补查 → 保存结果。宿主业务校验后入库并允许编辑，不默认增加采用门槛；若业务需要审核，使用业务资源上的普通操作，通过后另行受理独立 Flow。现有 Ether 包的模型引用、文件权限与宿主 API 需要适配，多模态大小和预算需要核验，详见[具体差距](execution-design.md#63-素材粗选的执行与真实差距)。

### 9.4 人工操作与独立 Flow 衔接

业务 Service 负责审核对象与版本、处理人范围、决定载荷、幂等及审计。Flow A 结束后，结果先采用为持久业务版本并登记业务文件引用，再允许人工操作；通过后发起独立 Flow B。业务记录关联 A、固定结果版本、决定和 B 的执行尝试，不改 A 的 input/result，也不让原 Run 等待人工。

批准即继续的业务在同一身份事务内锁定版本、验证业务批准权与后续 Flow 的发起/能力权限、保存决定、调用 FlowService.start_in_transaction()、保存 Run 关联及审计。任何一步失败全部回滚，提交后由既有 Worker 扫描 pending；不用额外消息唤醒。B 使用明确授权的新发起身份，A 的身份保持不变，业务页面可关联查询但仍分别授权 Run 和文件。

业务关联使用资源 ID、固定版本、动作和执行尝试的唯一约束；批准请求另存规范化载荷摘要。重复同一动作先返回已有、经当前授权的关联，同键不同载荷或相反决定冲突；不能仅用 Flow 的 actor_id/request_key 去重，因为不同审核人或凭据不共享同一受理范围。B 已失败时，重复批准仍返回原 B；只有显式重试操作在确认旧执行终态及副作用可安全处理后，创建新尝试和新 Run。

生成结果状态、审核状态、后续执行状态分别表达：A 成功可以待审核；驳回不使 A 失败；批准后 B 失败保留批准；取消 B 不回滚人工决定。新内容生成新版本并重新审核，旧 B 不能发布被替换的内容。状态矩阵、事务示例和验收见 [Flow 衔接设计](domains/flow.md#83-业务人工操作与独立-flow-衔接目标设计)。现有平台只提供基础受理与产物服务，具体审核业务和页面仍需实现；本次不建设通用人工待办或跨人工操作的执行等待。

### 9.5 实施边界与切换

TaskDefinition/TaskBinding、core/execution/、files/model_access/flow 的 tasks/、MVC + Service 目录归位和架构导入检查已落地。FlowPackage、多角色模型及数组文件路径仍未实现，具体业务的审核/衔接也没有通用实现。四层脚手架不自动创建任务空目录，后台任务按上述步骤显式装配。目录依赖检查递归覆盖平台及业务模块，包含嵌套任务文件与经公共执行实现包绕过的依赖。入口只保留最新实现，不加版本后缀；制品指纹校验和原身份恢复规则继续保留。目录移动会改变执行制品，新增 Step 或参数结构变化必须按原制品在途执行收尾后切换；不能因为“只整理目录”就跳过发布与恢复约束。
