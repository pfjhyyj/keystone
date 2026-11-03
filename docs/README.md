# Keystone 文档

先读整体架构，再按开发、运维或模块设计查阅。领域文档单列设计与当前实现差异。

## 设计

| 文档 | 内容 |
|---|---|
| [整体架构](architecture.md) | 模块、进程、依赖和核心约束 |
| [任务执行](execution.md) | Task、队列、定时计划与 Worker |
| [身份](domains/identity.md) · [审计](domains/audit.md) · [字典](domains/dictionary.md) | 授权、操作记录与业务选项 |
| [文件](domains/files.md) · [模型接入](domains/model-access.md) · [Flow](domains/flow.md) | 文件生命周期、模型调用与持久流程 |

## 开发

| 文档 | 内容 |
|---|---|
| [开发环境与检查](development/environment.md) | 日常开发、前端组织和统一检查入口 |
| [业务接入](development/business.md) | 完整最小示例、模块分层、权限、迁移与装配 |
| [Flow 开发](development/flow.md) | 工作流、Agent 与工具示例 |
| [控制台视觉规范](../DESIGN.md) | 前端样式与交互；PRODUCT.md 为设计工具的本地产品上下文 |
| [后端测试](../backend/tests/README.md) · [数据库迁移](../backend/migrations/README.md) | 各自规则的维护入口 |

## 运维

[配置](operations/configuration.md) · [部署、排障与维护](operations/deployment.md) · [备份恢复](operations/backup-restore.md)

## 实现参考

[文件存储与接口](reference/files.md) · [模型协议与恢复](reference/model-access.md) · [费用预估格式](reference/model-pricing.md)

启动命令见[仓库说明](../README.md)。设计、开发和操作规则各在所属文档维护，其他位置只链接。
