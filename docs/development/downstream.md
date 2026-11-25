# 下游交付项目

Keystone 以上游平台仓库发布，`businesses/` 保持为空。每个交付项目是一个下游仓库，加入自己的业务并定期合并上游。单个业务的写法见[业务接入](business.md)；本页只说明仓库关系、代码归属和合并方式。

## 仓库关系

```bash
git clone <下游仓库地址> && cd <下游仓库>
git remote add upstream https://github.com/pfjhyyj/keystone.git
```

新建下游仓库时从上游 `main` 开始，第一个提交前确定业务命名空间。命名空间同时用于目录、迁移链、表名和权限前缀，部署后改名需要数据迁移，按业务做的事而不是首个站点或首个页面命名。

## 代码归属

| 位置 | 归属 | 下游的改动方式 |
|---|---|---|
| `backend/src/keystone/businesses/<namespace>/` | 下游 | 新增 |
| `backend/migrations/businesses/<namespace>/` | 下游 | 新增，链内只追加，见[迁移说明](../../backend/migrations/README.md#平台链与业务链) |
| `composition.py`（`BUSINESS_CATALOGS`、`FLOW_PACKAGES`、`BUSINESS_ENVIRONMENT_KEYS`、`MESSAGE_*`、`BUSINESS_TASKS`、`WORKFLOW_BINDINGS` 等）、`model_registry.py`、`bootstrap.py` 中的业务声明 | 下游 | 只在已有元组和装配位置追加，不改平台条目 |
| `frontend/console/src/views/<namespace>/`、路由与菜单条目 | 下游 | 新增页面，路由和菜单只追加条目 |
| 业务文档 `docs/<namespace>.md`、业务运维说明 | 下游 | 新增；平台文档只在上游维护 |
| `core/`、`platform/`、平台迁移、运行镜像、控制台框架、平台文档 | 上游 | 下游不修改 |

装配文件、路由和菜单是上下游共同改动的位置，合并上游时的冲突只应出现在这里。下游在别处出现差异，说明有平台改动留在了下游，按下一节移回上游。

## 平台缺口

业务需要平台尚未提供的能力时，先在上游补齐，再合并到下游：

1. 在上游对应领域文档设计通用契约，文档和代码不出现下游业务名；用真实业务场景说明需求，但契约按能力表达。
2. 在上游实现、通过统一检查、合并到 `main`。
3. 下游合并上游后，再写依赖该能力的业务代码。

不在下游临时修改平台代码。一个缺口只服务一个业务时，仍优先找通用的表达，例如在环境 key 或消息命令上让业务声明回调，而不是让平台识别某个业务。

## 合并上游

```bash
git fetch upstream
git merge upstream/main
backend/.venv/bin/python scripts/check.py --scope full
```

冲突只在装配和菜单位置时，保留双方条目。平台迁移链与上游完全相同，业务链的 `depends_on` 指向它需要的平台 revision；合并后执行 `keystone init` 升级到各链 head。

统一检查同时运行平台测试，它们须在登记了业务后仍然通过。只因装配了业务才失败的平台测试依赖了空装配，按[平台缺口](#平台缺口)在上游修正。

## 常见接入做法

| 需要 | 做法 |
|---|---|
| Worker 中的业务代码（Flow 步骤、消息命令、发送时取内容）使用 Runtime | 在 `tasks/` 层写 `bind(runtime)` 并登记到 `WORKFLOW_BINDINGS`；在 bind 内导入执行模块，API 不加载它们 |
| 业务知道自己每段 Flow 的结果 | 平台向业务开放 Run 的结束原因（`FlowService.outcomes_in_transaction`，返回状态、公开错误码和 model_result_unknown，见[结束原因](../domains/flow.md#结束原因)）。段的最后一步以原身份把业务结果写入业务表；Run 失败、取消或超时时没有业务结果，按结束原因区分可重新发起与待核实 |
| 业务页面的输出设置跟随当前模型 | 按 Flow 用途读取当前默认模型及其协议声明的输出选项（`FlowModelDefaults.current` 或 `GET /flow-model-defaults/{definition}/{role}/current`，见[当前模型与输出选项](../domains/flow.md#当前模型与输出选项)），不在业务里写死比例、分辨率或格式 |
| Flow 失败等平台不执行业务代码的情况仍要处理（如回复命令） | 用 `BUSINESS_TASKS` 声明业务自己的计划任务 |
| 短期保存凭据类结果（如订阅链接） | 用 core 的 `CredentialVault` 加密写入业务表并设到期时间，读取时过期即清除；不进入 Step 返回值、日志、审计和投递记录，发送时用 `supplied` 取内容 |
| 入口地址跳到事先不知道的站点 | 见[交互环境 7.2](../domains/environments/README.md#72-业务接入接口) 中的 `ScopeViolation` |

## 从旧分叉重建

下游曾直接修改平台代码、与上游差异过大时，不在旧代码上解决冲突，而是在上游上重写业务：

1. 从 `upstream/main` 新建分支，平台代码与上游一致。
2. 对照旧实现迁移业务知识（流程、规则、验收清单），按当前平台接口重写业务代码和迁移链；旧实现需要的平台改动按[平台缺口](#平台缺口)补到上游。
3. 完成后在新分支执行 `git merge -s ours <旧 main>`，只把旧历史记为祖先，不带入旧代码；再把 `main` 快进到该分支。此后合并上游即为普通合并。
4. 旧部署库与新迁移链不兼容。由下游决定清空重建，或自行编写一次性转换；上游不提供转换工具。
