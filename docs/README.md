# Keystone 文档

Keystone 是模块化单体平台。API、Worker 与 CLI 共用一个 Python 工程和镜像，控制台使用 Vue 与 Element Plus。默认一个 Worker，可配置同宿主同版本的固定身份。Flow 默认启用，真实模型接入默认可用，权限判断使用 Casbin。

[整体架构第 3 节](architecture.md#3-api-与-worker-的分工)说明自动任务、定时任务和多 Worker 的职责。DBOS 公共生命周期、文件清理、Flow 协调及自动查费已实现；关闭 Flow 后公共运行时、文件维护和查费仍运行。Yunwu/Openlux 分页查费规则见[模型接入](domains/model-access.md#36-自动费用核对)。升级时重新执行 `keystone init`，应用最新基线迁移和 DBOS schema。默认一个 Worker；同宿主、同版本固定身份支持动态登记与显式退役，新增进程无需重启 API 或已有 Worker，步骤见[部署说明](deployment.md#4-多-worker-部署与发布)。

## 阅读入口

| 用途 | 文档 |
|---|---|
| Task/Flow 边界、已实现任务接口、模块目录与后续素材接入设计 | [任务执行与模块规范设计](execution-design.md) |
| 首次启动 | [仓库说明](../README.md) |
| 源码开发与测试 | [开发说明](development.md) |
| 业务 Flow 示例、输出验收、预算与工具摘要 | [业务 Flow 开发](flow-development.md) |
| 新增业务、权限、迁移、路由与可选 Flow | [新增业务](extending.md) |
| 模块边界、Worker/Flow/系统任务、多 Worker 机制与代码规范 | [整体架构](architecture.md) |
| 后端、前端和部署环境变量 | [配置说明](configuration.md) |
| Dockerfile 打包与发布 | [打包说明](deployment.md) |
| 联合备份与恢复 | [操作手册](backup-restore.md) |
| 数据库迁移 | [迁移说明](../backend/migrations/README.md) |

## 按领域阅读

| 领域 | 职责 |
|---|---|
| [身份与权限](domains/identity.md) | 用户、角色、Cookie 会话、个人令牌与功能授权 |
| [审计日志](domains/audit.md) | 管理变更与受保护操作留痕，成功审计与变更同事务 |
| [文件](domains/files.md) | 本地/S3 存储、上传下载、对象授权、业务引用和内容清理 |
| [字典](domains/dictionary.md) | 选项维护、启停、业务校验和历史名称解析 |
| [模型接入](domains/model-access.md) | 渠道、模型、调用记录、费用与余额 |
| [Flow](domains/flow.md) | DBOS 执行、受理幂等、取消、恢复、步骤和结果文件 |

## 当前实现与目标设计

当前三类维护入口位于 `platform/{model_access,files,flow}/tasks/`，公共执行设施位于 `core/execution/`。TaskDefinition/TaskBinding、统一计划和投递已可使用；FlowPackage、多角色模型、数组文件路径与素材粗选接入仍为[目标设计](execution-design.md)。

人工审核、继续和驳回归具体业务模块，前后自动处理分别运行独立 Flow。业务资源保存版本、决定、文件引用与前后 Run 关联；批准与后续 Flow 受理在同一业务事务衔接。详见[业务人工操作与独立 Flow 衔接](domains/flow.md#83-业务人工操作与独立-flow-衔接目标设计)。当前有事务内受理和产物校验接口，具体审核业务、页面与幂等关联仍需业务实现；不建设通用人工节点或跨人工操作悬挂的 Run。

## 维护约定

- 架构文档维护模块和跨领域边界；领域文档维护字段、权限及生命周期；操作命令集中在仓库说明与部署手册。
- 新业务按[接入规范](architecture.md#55-业务权限审计与模型接入)显式声明权限、审计、模型和流程，不增加动态插件层。
- 各领域明确对象授权、审计及删除规则，不统一套用管理员绕过、审计字段或软删除。
- 已确定的目标架构与当前实现须明确区分；尚未实现的配置或部署方式不得写成可执行操作说明。不记录逐批实施、测试日志或验收结果。
