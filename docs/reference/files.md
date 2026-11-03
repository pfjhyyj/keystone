# 文件实现参考

本页说明当前代码，设计目标和迁移差异见[文件设计](../domains/files.md)。代码位于 backend/src/keystone/platform/files/。

## 存储与身份

OpenDAL 0.47.10 支持多个 filesystem/S3 源，每个 File 只有一份不可变内容。storage_key 选源、storage_fingerprint 校验身份、object_key 定位；不自动复制、切源或同步删除。业务需要两份内容时分别保存 File 并维护引用。

keystone.toml 的 file_storages 显式声明源，file_default_storage 仅影响新请求；原幂等请求省略源时沿用原源。filesystem 身份来自 .keystone-storage，迁移或恢复须保留标记；S3 身份由 driver、endpoint、bucket、region、prefix 组成，凭据轮换不改变身份。源消失或身份改变即失败，不回退默认源。

配置任一源都须提供 KEYSTONE_FILE_COORDINATION_ROOT，API/Worker 共用同宿主可靠本地卷与标记。LocalLeases 协调请求键、写入、当前读取和清理，不兼容时立即返回 409/file_busy。不支持多宿主或 NFS 协调，本地锁也不能证明远端请求已停止。init-storage 初始化本地卷/源，不创建 S3 bucket。

默认源和协调卷影响 ready，辅助源故障只标该源 unavailable。S3 就绪探针超时 2 秒，可能需要前缀 ListBucket 权限，只验证连通性；Worker 在数据库可用时继续处理其他源。

## 上传与容量

POST /api/v1/files 接受原始二进制，参数为 name、content_type、expected_size，可选 storage_key、request_key、checksum_sha256。options 返回 max_file_bytes、storage_keys、default_storage。name 最多 255 字符，禁止控制字符和路径分隔符。

本地正文按 256 KiB 块写受限临时文件、增量校验，通过后才登记/重试 File，再写正式对象并确认 active。中断清理临时文件，正式写入失败保留原 File/键；崩溃残留由取得同一 OS 槽锁的下次操作清理，不开放半文件或分片续传。

流式上传预留临时与正式副本容量，内存上传在正式写入前预留；磁盘低水位、临时总额、文件上限和传输槽位同时生效。具体参数见[配置](../operations/configuration.md#2-应用参数)。S3 仍为 64 MiB 内存上限、30 秒存储超时，不随本地扩容改变。

上传创建 uploading 与来源用户的三项默认授权，开始审计记用户，已受理写入的事实确认记 system。完成响应复查当前身份及 create；有 inspect 返回元数据，只有 create 返回 ID。原键重试另要求原文件 manage；返回 active 元数据还要求 inspect，不补回撤销授权。

## 读取与传输

本地 GET/HEAD 检查 active、当前内容权限、存储身份和大小。首次或 stat 身份改变时分块核验 SHA-256，最多缓存 1024 个已验证身份；核验失败返回 503。受管 active 内容不可外部覆盖。

GET 支持单区间 bytes=start-end、start-、-suffix；多区间或不可满足返回 416。HEAD 忽略 Range；强 ETag 为内容哈希，If-Range 不匹配返回完整 200。音视频 inline，其他 attachment，保留 sandbox、nosniff、no-store。每个请求独立授权，已准入传输可完成。

当前读取持共享文件锁、清理持排他锁；目标将移除读取锁。数据库事务不跨传输，断开或取消先完成 I/O、关闭句柄，再释放锁和容量。

FileService.content 默认最多 64 MiB，显式 max_bytes 必须非负；内部 content 超限在读取前返回 file_too_large，引用错误为 file_reference_invalid。Flow 错误由调用方转换。

## 授权与接口

当前 FileGrant 与功能权限同时检查，owner_id 不授予隐式访问权。上传创建 inspect/content_read/manage 三项默认授权，失败重试或重新启用不补回。subject 支持 user、role、authenticated，后者仅可 inspect/content_read，非匿名公开。

| 动作 | 普通用户 | 内置管理员 |
|---|---|---|
| inspect | file:inspect＋对应 Grant | file:inspect |
| manage | file:manage＋对应 Grant | file:manage |
| content_read | file:content_read＋对应 Grant＋active | 同普通用户 |

管理者可显式给自己读取授权并留审计，仍须对应功能权限；不设最后文件管理者保护。FileGrant.role_id 阻止角色删除，RoleDeletion 通过 Files 公开 Service 同事务检查。

| API（/api/v1/files） | 当前规则 |
|---|---|
| GET /、/{id} | 先过滤对象范围再分页/详情；默认 50、最高 100，offset 最高 100000 |
| GET /options | create，返回上传能力 |
| POST / | create，上传 |
| GET/HEAD /{id}/content | content_read，受控内容传输 |
| PATCH /{id} | manage，name 或 active/disabled，返回 {id} |
| DELETE /{id} | manage，无引用后受理，202＋{id} |
| GET/POST /{id}/grants | manage，授权列表/新增，重复新增返回原关系 |
| DELETE /{id}/grants/{grant_id} | manage，撤销 |
| GET /{id}/references | manage，仅 owner/slot，不泄露业务内容 |

active/disabled 可编辑和新增授权；uploading/failed 可查看或撤销授权；deleting 仅状态及幂等删除请求，禁止授权变更。deleted 清除全部 Grant，普通用户不可见，内置管理员可看保留元数据。

/console/files 当前提供上传、列表、改名、启停、删除、授权与引用查看。无用户/角色目录权限时可填已知 UUID，不额外读取身份资料；没有解除业务引用入口。分页、异步代次及权限变化清空规则沿用控制台通用约定。

## 引用与事务接口

FileReferences(session, auth, namespace).replace/release 在已授权业务事务内使用可信 owner/slot，仅增删变化项。当前新增/更换要求 active 及内容权限，保留旧引用和解除自身引用不重新要求内容权；文件管理权不能解除他人业务引用。单 slot 最多 100 文件，owner 最多 160 字符、slot 最多 100。

同 owner/slot 事务互斥，File 按 ID 排序锁定，与删除及授权写入协调。锁序为身份/业务或 Run → 模型/预算 → File；调用方成功审计和引用一并提交。

| 内部入口 | 当前职责 |
|---|---|
| metadata_in_transaction(session, auth, file_id, manage=False, lock=False) | 同详情的公开字段；inspect，按需另查 manage/锁行，无内容与物理定位 |
| release_in_transaction(session, auth, references) | 只移除精确 owner/slot/file_id；重复不重复审计，无前缀批量清空 |
| request_delete_in_transaction(session, auth, file_id) | 检查 manage、对象范围及剩余引用，登记 deleting 和受理审计 |
| registered_file_ids(session, owner, slot) | 已授权精确槽位的 ID，最多取两个以判断单产物歧义 |
| require_registered_file(session, auth, owner, slot, file_id) | 锁行复查唯一引用、active 及当前内容权，不读存储 |
| grant_in_transaction(session, auth, file_id, data, grant_id=...) | 沿用权限、主体和生命周期的授权变更 |

上述入口不提交、不开新事务；业务先授权对象并限定清单。引用和删除组合时先按 ID 锁定全部影响文件，包括仅解除引用的输入；异常或取消整体回滚，物理删除仍由维护任务完成。

upload_referenced 在上传完成事务内登记固定 owner/slot，不暴露 ORM 或完成回调。受限终态同步仅保留该 Run 原登记的 active/disabled 文件，不增授权；Run 引用当前保留到清理，尚无自动 Run 清理。

可信系统维护可用 claims_in_transaction 有界读取已授权 owner 的引用，validate_retirement_scope 校验清单，再通过 retire_in_transaction 同事务释放精确引用并受理派生产物删除。调用方须证明期限已到、执行结束、清单归属，持业务锁及全部待删产物的排他 OS 租约直至提交。

该入口按 ID 锁定并核对归属和引用；外部引用、归属不符或清单未实际引用的 active/disabled 产物使整组返回 False，不部分释放。输入仅解除引用，deleting/deleted 可幂等核对。使用 system 审计，仅供显式装配的维护任务，不连接任意用户请求；平台不定义业务保留天数或扫描业务表。

## 清理与 S3 残留

keystone.files.cleanup 每 5 秒触发、启动立即扫描，使用 keystone-files-maintenance 串行队列，同类活动合并；无存储时禁用，不补停机时间片。

每轮最多 20 个候选，共享于 deleting、1 小时未更新的 uploading/failed、每小时复核的 S3 deleted 墓碑。取得文件锁后复查状态和引用；活跃写入跳过，过期 uploading 标 failed，failed 保留以便原键重试，残留最多每小时重试。不自动清理全部无引用 active/disabled。

删除成功后同事务写 deleted、deleted_at、清除全部 Grant 和完成审计；外部删除后确认失败仍为 deleting，后续幂等确认。请求记用户，维护记 system。

S3 的迟到 PUT 不能靠本地锁排除。重试只写同一校验内容；deleted/failed 保留精确 object_key 与原源配置，Worker 对残留重删，不恢复状态或授权。无桶扫描、版本或 multipart 清理；这些由桶策略管理。

## 临时空间与本地定位

FileService.transfer() 返回作用域内有效的 FileTransferService。materialize_file 校验授权和完整性，退出清临时副本；upload_file 从固定打开句柄确定大小/摘要，保存原键与引用。同一作用域只允许一个活动操作，物化后复制上传须另取作用域。退出前等待全部 I/O，路径、句柄不进检查点。

可信宿主可传 TransferScope(owner, max_temporary_bytes)。workspace 提供受限临时 Path，upload_file 的可选 admission(data) 在句柄校验后、正文发送前执行；不向 HTTP 开放任意 owner/额度。

容量账本 v2 在 OS 所有者记录 scope/scope_bytes/scope_limit，全局锁下汇总 live 承诺并取最严格上限。顺序刷盘不提前释放完整临时承诺；正式副本受物理容量管理，不重复计临时额度。无 scope 写 v1，读兼容 v1/v2；崩溃只以 OS 锁释放回收，损坏 live 记录拒绝，不靠 PID/时间推断。超 scope 返回 file_scope_limit；发布前停止全部旧协调写入进程，旧代码不可与 v2 混跑。

local_origin(file_id) 仅供可信本地业务，要求当前 inspect/content_read 及对象授权，只接受 filesystem。核验后返回实际路径、大小和校验和，返回前复查；无通用 HTTP 路由，NAS 目录准入由业务判断。

## Run 文件诊断

可信事务入口按精确 flow:<run_id> 查询，上层先授权 Run，Files 再按 inspect 对象范围过滤分页。公开只返回 File ID/slot；内部摘要、大小、MIME 用于核对原 Manifest，仅匹配的同 Run 可见图片调用补 invocation_id。引用不授予内容权，完整诊断见[Flow](../domains/flow.md#8-api授权与诊断)。

## 代码组织

models/ 每实体一文件；services/files、grants、references、cleanup 分管生命周期、授权、引用和清理。adapters/storage、capacity、transfers 管存储、容量、传输，LocalLeases 位于 core。任务仅做 DBOS 适配。授权使用当前 Authorization.role_ids，主体存在性调用 Identity require_subjects，不跨领域查询 ORM。
