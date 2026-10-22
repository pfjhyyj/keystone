# Keystone 文档

Keystone 是模块化单体平台。API、单 Worker 与 CLI 共用一个 Python 工程和镜像，控制台使用 Vue 与 Element Plus。Flow 默认启用，真实模型接入默认可用，权限判断使用 Casbin。

## 阅读入口

| 用途 | 文档 |
|---|---|
| 首次启动 | [仓库说明](../README.md) |
| 源码开发与测试 | [开发说明](development.md) |
| 业务 Flow 示例、输出验收、预算与工具摘要 | [业务 Flow 开发](flow-development.md) |
| 模块边界、代码规范与业务接入 | [整体架构](architecture.md) |
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

## 维护约定

- 架构文档维护模块和跨领域边界；领域文档维护字段、权限及生命周期；操作命令集中在仓库说明与部署手册。
- 新业务按[接入规范](architecture.md#55-业务权限审计与模型接入)显式声明权限、审计、模型和流程，不增加动态插件层。
- 各领域明确对象授权、审计及删除规则，不统一套用管理员绕过、审计字段或软删除。
- 文档只保留当前规则和操作说明，不记录逐批实施、测试清单或验收结果。
