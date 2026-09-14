# 多用户管理、日志存储与任务执行

本文说明控制面部署的实现与部署步骤。单机、未配置控制面数据库的模式保留原有运行方式。

## 页面和权限

管理员登录现有 `/control-plane` 页面后直接看到使用概览、审计记录和只读数据库面板。
不新增管理员登录入口。后端使用现有 `admin / operator / viewer` 角色校验：

- admin 查看管理面板、控制全部任务、调整排队优先级和申请符合条件的强制终止。
- operator 控制自己提交的普通任务；团队任务保持共享可见。
- viewer 只读。直接请求管理 API 也会被拒绝。

任务表显示状态、阶段、提交者、进度和服务端计算的操作按钮。历史详情保留不同执行批次。
使用日志支持用户、时间、操作、结果、Job 和数据集筛选。数据库浏览限制为服务端固定的表和字段，
不提供任意 SQL、直接修改记录或凭据字段；JSON 内容也脱敏。查询每页默认 50 条、最大 200 条。
数据库浏览独立限流，每用户每分钟最多 30 次记录查询，查询超时约 2 秒。

## 存储分工

| 存储 | 内容 |
| --- | --- |
| `DATA_PLATFORM_DATABASE_URL` 指定的现有控制面库 | 账号、节点、Job、执行批次、队列控制、幂等命令、事件回执、待投递审计 |
| 原有 `lifecycle.db` | 版本、策划、任务语义和物化；配置日志库后增加事务内审计 outbox |
| `DATA_PLATFORM_LOG_DATABASE_URL` 指定的日志库 | 使用审计和任务历史事件，独立账号和连接池 |
| 控制面输出目录及 Agent 状态目录 | 请求审计补报、执行记录、待补报结果；不应配置在源数据集内部 |
| 数据节点 | 源数据、独立 staging 和最终产物 |

`dp_job_controls` 为现有 Job 增加版本号、优先级、执行批次和停止状态；通过一对一表扩展旧模型，
不要求重写原 `dp_jobs`。`dp_job_attempts` 不级联依赖数据位置，保留执行历史。
控制面状态更新和审计 outbox 同事务提交。后台每 2 秒尝试投递，日志库暂时不可用不阻塞任务领取。
事件 ID 去重；现有 `dp_job_events` 暂时保留兼容副本，不自动清除，历史事件可显式回填。

网页请求先持久记录意图，并为结果预留补报容量。登录失败、权限拒绝和业务结果分别记录；
不采集静态资源、视频分片、常规 GET API 轮询或 Agent 心跳。访问次数不表示停留时长。
参数中的密码、令牌、凭据、数据库 URL 和异常中的 URL 用户信息会脱敏。

默认 outbox 上限 100000 条，网页/Agent 事件补报目录默认上限 256 MiB。
达到上限时拒绝需要继续产生可靠事件的操作，避免无限增长。原始请求意图在结果写入失败时仍保留；
管理面板显示投递错误、积压、请求结果预留和最近成功时间。日志不自动过期删除。
日志库不可用时查询页面返回可识别的 503，不把内部数据库连接异常返回客户端。
同一 MySQL 实例内分库仅提供逻辑/连接隔离；CPU、磁盘和实例故障仍共享。

## 队列、取消和重试

默认每用户全平台最多运行 1 个任务，每节点最多运行 1 个任务，每用户排队最多 20 个。
调度先排除用户配额、数据路径和节点能力不满足的任务，再按优先级及入队时间排序。
低/普通/高优先级分别为 0/1/2，默认普通；每等待 30 分钟升一级，最高为高，同级按原入队时间排序。
调整优先级不抢占已运行任务。事务中的调度锁避免多进程领取和取消竞争。

| 场景 | 行为 |
| --- | --- |
| 排队取消 | `queued → cancelled`，之后不能被领取 |
| 请求停止 | `running → cancel_requested`，等待执行器确认退出 |
| 强制终止 | 仅管理员、必须确认并填写原因；SIGTERM 后等待 30 秒，再结束残留进程 |
| 执行器失联 | `interrupted`，保持资源占用，不直接重领或释放冲突路径 |
| 重试 | 对允许的类型和确认停止的任务重新排队，保留 Job ID，新增执行批次 |
| 提交产物 | `phase=finalizing`，此时拒绝取消和强制终止；登记和缓存更新完成才标记 done |

第一批完整停止/重试能力适用于协议 2 Agent 的 Viewer 准备和现有 sibling 预处理操作。
源数据原地修改、旧协议执行、本地请求重放类型不开放通用运行中停止、强杀或重试。
这些任务仍可排队取消；页面根据 `available_actions` 显示实际支持的能力。
本地请求重放保留各领域原本的写入、审批和提交行为，后续开放其运行中控制前需要逐类型实现隔离输出契约。

Agent 父进程监督，子进程计算，进程池有界提交。网页退出或 Gunicorn 重启不会取消已接受任务。
每批执行使用独立 staging。输入校验包括元数据内容和 data/videos 文件大小、mtime/ctime，
不把它描述为所有视频内容的完整密码学摘要。重试后输入发生变化会失败，须创建新任务。
完成结果先写入私有执行目录；断网后优先重报结果，不重新计算。
最终输出已存在时拒绝覆盖。上传按执行批次隔离；失效批次不能提交事件或产物。

Agent 的进程监督和本地文件锁保护一个状态目录对应的执行器；当前默认是每节点一个执行器、一个重任务。
不要把同一数据目录同时注册到多个独立执行器，或启用共享文件系统的跨节点冲突写入。
增加并发前先验证路径归属和资源预算；默认配置优先保护现有浏览负载。

## 本地任务

控制面部署中的本地后台入口通过 `launch_background` 持久化已校验的请求，再由独立本地执行器运行。
Web 进程不执行这些重任务。原 `/api/jobs` 字段保持兼容，重启后可从数据库重新查询本地记录。
本地执行器再次检查提交账号和原路由权限，沿用 Lifecycle 审批、乐观锁和物化租约。
任务映射使用已保存的配置快照只重建缓存，避免重复保存映射；任务建议结果及审阅上下文也持久保存。

输入中必须使用的临时密钥保存在服务器私有文件中，数据库仅存引用，日志不保存密钥值。
执行器不直接覆写 Web 的数据集注册表；完成时由 Web 重新注册产物、刷新元数据、episode 索引及缓存。
启动本地执行器前，本地任务会保持排队。不要把“已经排队”误认为执行器已经运行。

## 配置和部署

先在已有 MySQL 实例创建 `data_platform_logs` 和专用账号，只授权该库。
将连接信息加入服务器私有 `/etc/data-platform/server.env`，不要写入仓库、命令历史或日志。
部署模板包含以下配置：

```text
DATA_PLATFORM_LOG_DATABASE_URL=<日志数据库连接地址>
DATA_PLATFORM_LOCAL_SERVER_URL=http://127.0.0.1:9091
DATA_PLATFORM_USER_RUNNING_LIMIT=1
DATA_PLATFORM_NODE_RUNNING_LIMIT=1
DATA_PLATFORM_USER_QUEUE_LIMIT=20
DATA_PLATFORM_OUTBOX_MAX_EVENTS=100000
DATA_PLATFORM_AUDIT_SPOOL_BYTES=268435456
DATA_PLATFORM_COMPUTE_CPU_FRACTION=0.5
DATA_PLATFORM_COMPUTE_MEMORY_FRACTION=0.6
DATA_PLATFORM_PAUSE_CLAIMS=0
DATA_PLATFORM_GPU_DEVICES=
```

连接地址通过环境注入。执行显式初始化和导入：

```bash
python -m lerobot.data_platform.management_cli migrate
python -m lerobot.data_platform.management_cli import-jsonl <历史日志文件>
python -m lerobot.data_platform.management_cli backfill-job-events
```

这些命令会写数据库，但不修改源数据集；导入可重复执行，不删除旧日志。
新增管理表记录迁移版本，启动迁移以数据库锁串行；现有业务表保持兼容。
日志 schema 初始化是幂等操作。备份和恢复应覆盖控制面库、Lifecycle SQLite、日志库及私有执行目录，
SQLite 在线备份应使用其备份 API，而不是仅复制主文件并遗漏 WAL。

更新顺序：

1. 设置 `DATA_PLATFORM_PAUSE_CLAIMS=1` 并重启 Web，使 Agent 暂停领取；等待当前任务正常结束。
2. 备份存储，配置日志库，执行 schema 初始化，升级 Server A。
3. 安装 `deploy/data-platform/data-platform-local-worker.service`，与 Web 使用同一服务器环境文件。
4. 从同一版本构建 `dist/agent/` 包，升级远程 Agent；安装器使用更新后的 systemd 模板。
5. 检查本地执行器、远程 Agent 和日志投递，恢复 `DATA_PLATFORM_PAUSE_CLAIMS=0`。

本地执行器启动命令为 `python -m lerobot.data_platform.local_execution`；默认读取
`DATA_PLATFORM_OUTPUT_DIR/management/local-executor.json`，该文件由首次本地任务提交建立。

执行器 systemd 单元启用 `Delegate=yes` 和 `DATA_PLATFORM_REQUIRE_CGROUP=1`。
需要 Linux cgroup v2 的 CPU、memory、pids 委派。监督进程与计算子进程分组，默认计算 CPU 预算为
逻辑 CPU 的 50%，内存硬上限为物理内存的 60%。无法建立资源隔离时生产配置拒绝启动该次计算。
开发测试可显式使用 `DATA_PLATFORM_REQUIRE_CGROUP=0`，此时只有进程组控制，没有硬资源隔离保证。
GPU 默认隐藏；有 GPU 工作时由管理员为执行器分配设备 ID。当前单任务默认使同一设备不被多个任务占用。

上线前仍需在实际 MySQL 和 systemd 主机上验证迁移、cgroup 委派、服务账号权限，以及代表性任务下的
页面延迟、磁盘和网络负载。SQLite/子进程回归测试不能代替这些部署验证。

## Ray 和 Airflow 的接入边界

当前继续采用数据库队列和独立执行器。多用户本身不是引入 Ray 或 Airflow 的理由。

- 单个任务确实需要跨多 GPU/多节点拆分计算时，可将 Ray 作为 Agent 的执行后端。
- 出现大量定时、多步骤依赖和历史补跑工作流时，可由 Airflow 调用平台任务 API。

平台仍然负责账号、审计、数据身份、停止确认和产物提交；外部执行框架不能替代这些业务约束。
不会在这一轮额外部署 Ray/Airflow 集群或增加第二套任务管理页面。

## 固定的日常升级命令

双环境部署已改为显式选择 `--env dev|prod`，生产发布同一份开发验收包。
不再把当前未提交工作区直接覆盖到运行目录。首次迁移、独立数据库与 Agent 配置、维护和回退流程见
[双环境部署指南](data_platform_environments.md)。

```bash
data-platform-update-all --env dev --version RELEASE
data-platform-release approve --env dev --release RELEASE --evidence /PATH/TO/evidence.json
data-platform-update-all --env prod --release RELEASE
```

本页前文的单环境配置路径和服务名用于解释现有部署；完成首次接入后，使用环境专属配置、服务和管理命令。
