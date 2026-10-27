# Keystone

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

企业内部平台管理员。控制台主要用于平台后台管理，用量与费用同样是重要的管理信息。

## Product Purpose

为单企业内部多个业务模块提供统一管理入口，覆盖身份与权限、审计、文件、字典、模型接入和 Flow。

## Operating Context

管理员检查 API、数据库、存储和 Worker 状态，管理用户与角色，配置模型渠道和业务模型，测试调用，并按渠道、模型、用户、流程等维度追踪用量与费用。

## Capabilities and Constraints

- 模块化单体，API、Worker、CLI 共用 Python 工程与发布制品；控制台使用 Vue、Vue Router、Element Plus。
- 路由及操作按权限开放，前端展示不能替代后端授权。
- API 就绪、Worker 进程健康、共享维护分别表达，查询失败不能直接判定离线。
- 费用按原单位分别统计；未知费用、待核对费用、账户余额和 Key 额度分别表达。不得将未知费用计为零，也不添加无来源的费用预测。
- Flow 运行保留真实执行、取消、恢复和费用语义。
- 当前重设计先审视全局问题并确认代表页面与高保真方案，再实现代码。

## Brand Commitments

保留产品名称 Keystone 与中文控制台语境。尚无用户指定的色彩、字体或视觉参考。

## Evidence on Hand

现有页面与接口定义位于 frontend/console/src；架构与领域契约位于 docs。设计预览数据必须注明为示例，不代表生产运行情况。

## Product Principles

- 优先帮助管理员发现、定位和处理平台问题。
- 状态与费用口径准确，区分已确认、未知和待处理信息。
- 保留现有功能、权限和真实业务约束。
- 以真实任务组织导航和信息层级，减少重复查找。
