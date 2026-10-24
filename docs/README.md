# Keystone 文档

Keystone 是模块化单体：API、Worker、CLI 共用后端工程，控制台使用 Vue 与 Element Plus。Flow、模型、文件及持久后台 Task 使用同一身份和授权边界。

| 用途 | 文档 |
|---|---|
| 启动与常用命令 | [仓库说明](../README.md) |
| 模块边界、进程与事实归属 | [整体架构](architecture.md) |
| Task、队列、计划和 Worker | [任务执行](execution-design.md) |
| Flow 契约、角色、恢复、预算与诊断 | [Flow 领域](domains/flow.md) |
| Flow/Agent 的实际接入示例 | [Flow 开发](flow-development.md) |
| 新业务、权限、审计与迁移 | [模块扩展](extending.md) |
| 配置、发布与恢复 | [配置](configuration.md)、[部署](deployment.md)、[备份恢复](backup-restore.md) |
| 开发、测试和迁移规范 | [开发](development.md)、[测试](../backend/tests/README.md)、[迁移](../backend/migrations/README.md) |

## 按领域阅读

| 领域 | 职责 |
|---|---|
| [身份](domains/identity.md) | 用户、角色、会话、PAT 和功能权限 |
| [审计](domains/audit.md) | 变更和拒绝留痕，成功审计与业务变更同事务 |
| [文件](domains/files.md) | 存储、传输、对象授权、业务引用与清理 |
| [字典](domains/dictionary.md) | 选项维护、业务校验及历史名称 |
| [模型接入](domains/model-access.md) | 现有渠道、操作、恢复与费用；[第 8 节](domains/model-access.md#8-多渠道模型接入设计)说明代码适配层、数据库事实及版本发布边界 |
| [Flow](domains/flow.md) | 独立受理、原身份执行、取消、恢复和结果 |

架构文档维护边界，领域文档维护当前契约及明确标注的目标设计，开发文档维护已实现的接入示例，操作手册维护当前可用命令。每个主题只在一处维护完整规则；不保存独立实施计划、阶段记录、逐轮测试日志或未实现业务的详细方案。目标接口不作为现有功能或配置使用。人工决定和业务流程由业务模块实现，平台不提供通用人工等待节点。
