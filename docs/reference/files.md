# 文件实现参考

本页说明当前代码，职责与主要流程见[文件设计](../domains/files.md)。代码位于 backend/src/keystone/platform/files/。

## 存储与身份

OpenDAL 0.47.10 支持多个 filesystem/S3 源，每个 File 只有一份不可变内容。storage_key 选源、storage_fingerprint 校验身份、object_key 定位；不自动复制、切源或同步删除。业务需要两份内容时分别保存 File 并维护引用。

keystone.toml 的 file_storages 显式声明源，file_default_storage 仅影响新请求；原幂等请求省略源时沿用原源。filesystem 身份来自 .keystone-storage，迁移或恢复须保留标记；S3 身份由 driver、endpoint、bucket、region、prefix 组成，凭据轮换不改变身份。源消失或身份改变即失败，不回退默认源。

配置任一源都须提供 KEYSTONE_FILE_COORDINATION_ROOT，API/Worker 共用同宿主可靠本地卷与标记。LocalLeases 协调请求键、写入和清理，不兼容时立即返回 409/file_busy。普通读取不持文件锁。不支持多宿主或 NFS 协调，本地锁也不能证明远端请求已停止。init-storage 初始化本地卷/源，不创建 S3 bucket。

默认源和协调卷影响 ready，辅助源故障只标该源 unavailable。S3 就绪探针超时 2 秒，可能需要前缀 ListBucket 权限，只验证连通性；Worker 在数据库可用时继续处理其他源。

## 业务内部接口

装配层用 runtime.managed_files(identity) 创建 FileService，注入业务 Service。它复用现有存储、容量、身份和审计设施，不要求 file:* 权限或 FileGrant；业务须先授权操作和对象，传入从自身记录解析或本次上传取得的精确 ID。不存在客户端跳过授权参数。

| 接口 | 用途 |
|---|---|
| upload / upload_body / upload_stream | 保存受控内容，稳定 request_key 按来源用户隔离；不自动创建业务引用 |
| edit_in_transaction | 在业务事务中改名/启停并写文件审计 |
| metadata_in_transaction | 在调用方已授权的事务中取得安全元数据，可按需锁行；不提交事务 |
| content / open_content | 有界字节或流读取，检查可用状态、存储身份与完整性 |
| transfer | 本地物化、临时工作区和保存；owner/slot 可省略，业务引用随后同事务登记 |
| read_transfer | 有界等待读取容量，再由业务授权对象，调用 transfer.open_content(file_id) |

普通读取由业务授权一次即可；需要等待容量时，先进入 read_transfer，再授权并解析 ID，避免排队前的授权被当作开始读取时的授权。Service 不返回 HTTP Response，业务 Controller 可复用 ContentResponse 处理 Range/HEAD。local_file 只供可信本地集成，不能进入普通 HTTP 响应。

业务在文件保存后复查当前保存资格，同事务更新自己的关系、FileReferences 和成功审计。FileReferences.replace/release 仅维护调用方已授权的精确关系，不检查 FileGrant，不把引用当作读取权。保存失败不撤销文件事实，已知原请求使用原键重试。

## 上传与容量

业务上传路由可接收原始二进制，复用 Upload 参数： name、content_type、expected_size，可选 storage_key、request_key、checksum_sha256。options 返回 max_file_bytes、storage_keys、default_storage。name 最多 255 字符，禁止控制字符和路径分隔符。

本地正文按 256 KiB 块写受限临时文件、增量校验，通过后才登记/重试 File，再写正式对象并确认 active。中断清理临时文件，正式写入失败保留原 File/键；崩溃残留由取得同一 OS 槽锁的下次操作清理，不开放半文件或分片续传。

流式上传预留临时与正式副本容量，内存上传在正式写入前预留；磁盘低水位、临时总额、文件上限和传输槽位同时生效。具体参数见[配置](../operations/configuration.md#2-应用参数)。S3 仍为 64 MiB 内存上限、30 秒存储超时，不随本地扩容改变。

上传登记 uploading，开始审计记用户，已受理写入的完成事实记 system；完成时写 completed_at。响应前复查身份，业务再复查保存资格并登记关系。原键重试核对原用户、请求摘要和原存储，不自动改变业务授权。

## 读取与传输

业务内容路由先授权对象，Files 检查 active、存储身份和大小。首次或 stat 身份改变时分块核验 SHA-256，最多缓存 1024 个已验证身份；核验失败返回 503。受管 active 内容不可外部覆盖。

GET 支持单区间 bytes=start-end、start-、-suffix；多区间或不可满足返回 416。HEAD 忽略 Range；强 ETag 为内容哈希，If-Range 不匹配返回完整 200。音视频 inline，其他 attachment，保留 sandbox、nosniff、no-store。每个请求独立授权，已准入传输可完成。

读取不持文件锁，接受并发回收造成打开或下载失败；清理与写入继续互斥。数据库事务不跨传输，断开或取消先完成 I/O、关闭句柄，再释放容量。

FileService.content 默认最多 64 MiB，显式 max_bytes 必须非负；内部 content 超限在读取前返回 file_too_large，引用错误为 file_reference_invalid。Flow 错误由调用方转换。

## 引用与事务接口

FileReferences(session, auth, namespace).replace/release 在已授权业务事务内使用可信 owner/slot，仅增删变化项。新增/更换要求 active，内容资格由业务确认；保留旧引用和解除自身引用不重新要求内容权。文件管理权不能解除他人业务引用。单 slot 最多 100 文件，owner 最多 160 字符、slot 最多 100。

同 owner/slot 事务互斥，File 按 ID 排序锁定，与删除受理协调。锁序为身份/业务或 Run → 模型/预算 → File；调用方成功审计和引用一并提交。

| 事务接口 | 职责 |
|---|---|
| metadata_in_transaction(session, file_id, lock=False) | 安全元数据，按需锁行，不返回物理定位 |
| metadata_many_in_transaction(session, file_ids) | 最多 100 个 ID 的公开元数据，不加行锁 |
| request_delete_in_transaction(session, auth, file_id) | 无引用且状态允许时受理 deleting，与业务事务一起提交 |
| edit_in_transaction(session, auth, file_id, data) | 对已授权文件改名或启停，与审计同事务 |
| release_in_transaction(session, auth, references) | 只移除精确 owner/slot/file_id；重复不重复审计 |
| registered_file_ids(session, owner, slot) | 精确槽位的已登记 ID |
| require_registered_file(session, owner, slot, file_id) | 复查唯一引用和技术可用状态，不读存储 |

这些入口不提交、不开新事务。业务先授权对象并限定清单；Flow 另外限定 Run 和固定文件范围。

upload_referenced 在上传完成事务内登记固定 owner/slot，不暴露 ORM 或完成回调。受限终态同步仅保留该 Run 原登记的 active/disabled 文件，不增授权；Run 引用当前保留到清理，尚无自动 Run 清理。

可信系统维护可用 claims_in_transaction 有界读取已授权 owner 的引用，validate_retirement_scope 校验清单，再通过 retire_in_transaction 同事务释放精确引用并受理派生产物删除。调用方须证明期限已到、执行结束、清单归属，持业务锁及全部待删产物的排他 OS 租约直至提交。

该入口按 ID 锁定并核对归属和引用；外部引用、归属不符或清单未实际引用的 active/disabled 产物使整组返回 False，不部分释放。输入仅解除引用，deleting/deleted 可幂等核对。使用 system 审计，仅供显式装配的维护任务，不连接任意用户请求；平台不定义业务保留天数或扫描业务表。

## 清理与 S3 残留

keystone.files.cleanup 每 5 秒触发、启动立即扫描，使用 keystone-files-maintenance 串行队列，同类活动合并；无存储时禁用，不补停机时间片。

每轮最多 20 个候选，共享于 deleting、1 小时未更新的 uploading/failed、每小时复核的 S3 deleted 墓碑，以及显式启用后的超期无引用 active 文件。取得文件锁后复查状态、引用及期限；活跃写入跳过，过期 uploading 标 failed，failed 保留以便原键重试，残留最多每小时重试。

无引用回收默认关闭；file_unreferenced_grace_seconds 设置后，仅扫描已知 completed_at 且超过宽限期的 active 文件。先锁 File 行复查并提交 deleting 和 system 受理审计，再删除内容；引用先提交则跳过，deleting 先提交则拒绝新增引用。disabled 和完成时间未知的存量始终跳过，读取不刷新宽限期。启用前核对所有调用方、存量引用；旧授权不能当作保留关系。

删除成功后同事务写 deleted、deleted_at 和完成审计；外部删除后确认失败仍为 deleting，后续幂等确认。请求记用户，维护记 system。

S3 的迟到 PUT 不能靠本地锁排除。重试只写同一校验内容；deleted/failed 保留精确 object_key 与原源配置，Worker 对残留重删，不恢复状态或授权。无桶扫描、版本或 multipart 清理；这些由桶策略管理。

## 临时空间与本地定位

FileService.transfer() 返回作用域内有效的 FileTransferService。materialize_file 校验技术可用状态和完整性，退出清临时副本；upload_file 从固定打开句柄确定大小/摘要，保存原键与引用。同一作用域只允许一个活动操作，物化后复制上传须另取作用域。退出前等待全部 I/O，路径、句柄不进检查点。

可信宿主可传 TransferScope(owner, max_temporary_bytes)。workspace 提供受限临时目录 Path，单文件在目录内选择路径，upload_file 的可选 admission(data) 在句柄校验后、正文发送前执行；不向 HTTP 开放任意 owner/额度。

容量账本 v2 在 OS 所有者记录 scope/scope_bytes/scope_limit，全局锁下汇总 live 承诺并取最严格上限。顺序刷盘不提前释放完整临时承诺；正式副本受物理容量管理，不重复计临时额度。所有预留统一使用 v2，无 scope 时 scope/scope_limit 为 null、scope_bytes 为 0；崩溃只以 OS 锁释放回收，损坏 live 记录拒绝，不靠 PID/时间推断。超 scope 返回 file_scope_limit。

local_file(file_id) 接受已获业务授权的本地文件，作用域内返回 LocalFile(path, size_bytes, checksum_sha256)。共享文件锁保持到作用域退出，与清理 Worker 的排他锁互斥；本地完整性检查复用现有读取槽位。业务负责用户操作或维护资格，路径只在作用域内供可信代码使用。

## Run 文件诊断

可信事务入口按精确 flow:<run_id> 查询，上层先授权 Run，Files 再按该 Run 的精确引用分页。公开只返回 File ID/slot；内部摘要、大小、MIME 用于核对原 Manifest，仅匹配的同 Run 可见图片调用补 invocation_id。引用不授予内容权，完整诊断见[Flow](../domains/flow.md#8-api授权与诊断)。

## 代码组织

models/ 每实体一文件；services/files、references、cleanup 分管生命周期、引用和清理。adapters/storage、capacity、transfers 管存储、容量、传输，LocalLeases 位于 core。任务仅做 DBOS 适配。业务授权在调用方 Service 完成，跨模块不查询其他领域 ORM。
