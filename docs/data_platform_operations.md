# 当前环境构成与日常更新手册

2026-09-15 核对：生产与开发均为 **R2**，维护状态均关闭。首次迁移已经完成，
日常更新不再执行 `init`、`adopt-legacy`、重新建库或重新登记 Agent。

代码、数据库物理目录、数据集、缓存和配置的完整位置清单见
[开发与生产存储位置说明](data_platform_storage_locations.md)。

## Pipeline coordinator

含统一算子功能的发行包增加 `data-platform-pipeline-coordinator@dev.service` / `@prod.service`。
安装器随 Web 安装此轻量协调进程，使用相同环境配置与管理数据库；每个计算步骤仍由原有 Agent 执行。
管理库仅新增流程表，旧执行器可继续收尾旧任务。维护期间协调器暂停推进。

滚动切换停止 Web 和协调器，保留尚有任务的旧本地执行器代际。回退到不含协调模块的发行包时，
安装器停用协调服务，健康检查只检查旧版本原有服务；流程状态留在管理库供之后恢复。
开发验收需同时检查协调器、原有服务、维护开关、新 Agent 心跳/算子版本，以及实际受保护页面中的
本地和远端多步骤流程。模型算子还需对应后端运行时验收；仅列出能力不代表推理通过。

详情与 API 见 [算子与流程](data_platform_operators.md)。生产推广仍复用完成开发验收且得到批准的同一发行包。

## 1. 现在由哪些部分组成

共用两台机器，各运行两套独立服务；不是两个 Git 分支分别直接启动。

```text
浏览器
  ├─ https://10.8.8.79       → Server A：Nginx 443  → prod Web 9091
  └─ https://10.8.8.79:8443  → Server A：Nginx 8443 → dev  Web 9092

Server A（ubuntu-22）
  ├─ prod：Web、本地任务执行器、生产业务库/日志库、生产控制台状态
  ├─ dev ：Web、本地任务执行器、开发业务库/日志库、开发控制台状态
  └─ 发布桥接服务：接受开发管理员的发布请求，执行生产升级

H100（h100-05，通过 SSH 连接）
  ├─ prod Agent（h100-05）：访问原生产数据，独立程序与运行状态
  └─ dev  Agent（dev-node）：访问开发样本副本，独立程序与运行状态
```

H100 的 Agent 经现有 SSH 隧道连接 Server A：生产使用 H100 本地 `9443 → Server A 443`，
开发使用 `9444 → Server A 8443`。这些是 Agent 内部连接端口，浏览器使用上面的网页地址。

| 项目 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 网页入口 | `https://10.8.8.79` | `https://10.8.8.79:8443` |
| Web 内部端口 | `127.0.0.1:9091` | `127.0.0.1:9092` |
| Server 当前程序 | `/opt/data-platform/prod/current` | `/opt/data-platform/dev/current` |
| Server 配置 | `/etc/data-platform/prod/server.env` | `/etc/data-platform/dev/server.env` |
| 控制台状态父目录 | `/srv/data-platform` | `/srv/data-platform-dev` |
| 业务 MySQL 库 | `data_platform` | `data_platform_dev` |
| 审计 MySQL 库 | `data_platform_logs` | `data_platform_logs_dev` |
| H100 Agent 当前程序 | `/opt/data-platform-agent/prod/current` | `/opt/data-platform-agent/dev/current` |
| H100 Agent 配置 | `/etc/data-platform/prod/agent.env` | `/etc/data-platform/dev/agent.env` |
| H100 Agent 私有状态 | `/var/lib/data-platform-agent-prod` | `/var/lib/data-platform-agent-dev` |
| H100 数据 | 保留原生产目录，具体见生产 Agent 配置 | `/srv/data-platform-dev/datasets` 中的独立副本 |
| 页面账号 | 原生产账号 | 独立开发账号；管理员可切换测试角色 |

两套环境共用 MySQL 服务，但使用四个逻辑库及各自数据库账号。两套 Server、执行器、Agent 和隧道
均已启用开机自启。开发的 8443 是当前固定配置，不会随着每次更新或服务器重启自动变化。

Nginx 的开发和生产配置均须启用 `application/json` 的 gzip 压缩，并保留
`Vary: Accept-Encoding`。数据集列表包含 Episode 元数据，任务列表包含执行结果；随着登记数量
增加，完整响应可达数十 MB。Nginx 默认仅压缩 HTML，不能依赖全局 `gzip on` 来压缩这些 API。
控制台列表与任务进度轮询使用 `summary=1`：`/api/control/locations?summary=1`、
`/api/control/jobs?limit=200&summary=1` 和 `/api/control/jobs/<job_id>?summary=1`。
摘要保留权限、操作能力、进度、队列控制和输出链接，省略逐 episode 元数据、执行快照、
MCAP 逐文件报告和预处理 lineage。持久化记录不变，省略 `summary=1` 的原 API 仍返回完整内容。
验证时测量登录后的实际列表耗时与大小，同时检查请求 `Accept-Encoding: gzip`、响应
`Content-Encoding: gzip` 和 Nginx access log；200 也可能是客户端取消前已开始发送的部分响应。
摘要需要正常升级应用发行版，不能只靠延长页面超时或 Nginx 压缩替代。
修正现有配置后先执行 `nginx -t`，再热加载 Nginx，不需重启 Web、Agent 或中断运行任务。

源码工作区是 Server A 上的 `/home/yuhao.song/Codes/data_platform`。
修改这里的代码不会立即改变网站；需要提交、构建发行包，再部署。
发行包统一保存于 `/var/lib/data-platform-releases/版本名`，同一名称对应一份不可变安装包。

**“同步更新”指把同一份程序版本部署到两套环境，不是同步数据库、账号或数据集。**
更新可能在各自数据库上执行表结构迁移；不会把开发库覆盖到生产库。

## 2. 每次更新前

以下命令全部在 **Server A** 上执行，不需要另开 H100 终端手动安装 Agent。

### Server B 的密钥登录与更新连接

2026-10-14 现场核验：Server B 的 DHCP 地址已从 `10.8.8.85` 变为 `10.8.8.62`。
日常连接使用固定局域网名称 `data-platform-server-b.local`，无需随 IP 变化改配置。
管理账号拼写为 `maintenance`，注册的 Agent 名称仍为 `Server B (bingyu)`。
Server A 使用 `/home/yuhao.song/.ssh/id_ed25519_h100` 登录；本机
`/home/yuhao.song/.ssh/config` 已配置 `server-b` 别名及对应密钥，可直接执行：

```bash
ssh server-b
```

完整写法及每次更新前的非交互连接检查：

```bash
ssh -i /home/yuhao.song/.ssh/id_ed25519_h100 \
  -o IdentitiesOnly=yes maintenance@data-platform-server-b.local
ssh -i /home/yuhao.song/.ssh/id_ed25519_h100 \
  -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 root@data-platform-server-b.local 'id -u'
```

第二条应输出 `0` 并正常退出。正式更新使用 `root@data-platform-server-b.local` 的部署密钥通道，
由目标环境 `/etc/data-platform/<env>/server.env` 中的 `DATA_PLATFORM_DEPLOY_AGENT_TARGETS`
指定；`update-all.sh` 的前置检查还会核对远端环境、实例身份及 Agent 名称。SSH 免密码
不改变管理账号原有的 sudo 规则。前置检查失败时先看日志，已有发行包用 `--release` 重试。

固定名称与自动重连的持久配置：

| 主机 | 配置 | 用途 |
| --- | --- | --- |
| Server B | `/etc/avahi/avahi-daemon.conf` 的 `[server] host-name=data-platform-server-b` | 开机发布固定名称，跟随网卡当前地址 |
| Server A | `/home/yuhao.song/.ssh/config` | `server-b` 及旧地址别名连接固定名称，选用管理密钥 |
| Server A | `/etc/data-platform/prod/server.env` | Server B 部署目标使用固定名称，Agent 名称及身份不变 |
| Server A | `/etc/systemd/system/data-platform-serverb-tunnel@prod.service.d/10-stable-server-b.conf` | 隧道连接固定名称，失败后每 5 秒重试，连接超时为 10 秒 |

两端 Avahi、生产隧道及 Server B Agent 均已启用开机自启。连接存活检测每 30 秒一次，
连续三次无响应后重建隧道；重建时重新解析名称。SSH 主机密钥已绑定原 Server B 身份，
保留严格校验。现场已验证固定名称解析、密钥登录、正式部署前检、隧道中断后自动恢复与新心跳。
名称发现要求客户端与 Server B 的局域网支持 mDNS；跨越不转发 mDNS 的网段时，应使用网络管理方
提供的固定 DNS 名称或 DHCP 地址保留，不能假定 `.local` 能跨网段解析。

经用户授权，现有部署密钥已移除 `from=` 来源 IP 限制；Server A 的 IP 变化不再要求修改
这条密钥规则。root 密钥仍保留禁止端口转发、Agent 转发、X11 转发和 PTY 的限制，
管理账号另有可正常登录的同一公钥。现有隧道继续使用 `/home/yuhao.song/.ssh/id_ed25519`，
其转发限制及强制命令保持原样；不要用隧道密钥替代普通登录或部署密钥。
密钥文件和配置已有备份；文档不记录密码或私钥内容。

```bash
cd /home/yuhao.song/Codes/data_platform
git status --short
```

如果有输出，先检查并提交准备发布的修改；不要删除自己的未提交工作，也不要把配置、密码或数据加入 Git。
构建脚本要求工作区干净。下面示例用时间生成新版本名，请在同一个终端继续执行后续命令：

```bash
RELEASE="R$(date +%Y%m%d-%H%M%S)"
echo "$RELEASE"
```

记下显示的版本名。R0、R1、R2 已使用，不能再用这些名称构建新代码。

## 2.1 多个功能并行开发，统一合并发布

每个功能使用独立的 Git 分支和 worktree，分别修改、测试并提交，互不改动其他功能的工作目录。
开发工作区不直接运行在 `/opt/data-platform` 下；线上环境继续使用不可变发行包。

发布时使用独立、干净的集成工作区，按以下顺序处理：

1. 核对 `dev/current`、`prod/current`、发行包记录的 Git commit，以及全部已构建版本和 Git 标签。
   集成代码必须包含已部署的最新功能；默认源码目录或旧功能工作区的 HEAD 不代表当前线上代码。
2. 从已核验的最新源码提交建立集成分支，使用 `git merge --no-ff` 合入准备发布的功能分支。
   保留原功能分支和未提交修改；不通过覆盖文件或部署旧工作区来“合并”功能。
3. 检查合并差异，解决冲突，并在集成工作区运行各功能及公共流程的联合测试。
   没有文本冲突也需要验证功能组合；确认各功能提交和当前发行包源码提交都是集成 HEAD 的祖先。
4. 在集成阶段统一选择高于当前已部署、已构建版本的未使用编号，提交集成结果，检查工作区干净，
   并验证该工作区自己的 `.venv/bin/python` 可以运行测试及发行包构建工具。
5. 从集成工作区构建一次并部署 dev。验收通过后，按第 4 节批准并将同一份包提升到 prod；
   发布前若其他功能已升级线上，先重新核对基线、合并与测试，再确定发布编号。

`build_release()` 只打包选定工作区已提交的 HEAD，不会自动合并其他分支。
每个环境的升级有部署锁，但该锁不负责代码合并或版本编号分配。功能开发可以并行，
同一环境的发布由一个集成工作区串行执行；已生成发行包时，失败重试复用原包的 `--release`，
不重新使用 `--version`。

若停在 `Validating` 阶段，尚未安装或切换服务。测试失败日志保存在
`/var/lib/data-platform-releases/.failed-<版本>-*/validation.txt`，旁边的 `failure.json`
记录提交和退出码；终端会显示确切日志路径，日志目录仅构建账号可读。检查并修复失败项后，
确认该版本发行包尚未生成且编号仍未占用，才可使用原 `--version` 重新构建。

## 3. 一条命令同步更新开发和生产（硬升级）

完成第 2 节后执行：

```bash
bash deploy/data-platform/update-all.sh --env both --hard --version "$RELEASE"
```

实际顺序为：

1. 从已提交的源码构建一次 Server/Agent 包，并执行构建回归测试。
2. 更新开发 Server，再更新开发 Agent，验证开发健康状态。
3. 更新生产 Server，再更新生产 Agent，验证生产健康状态。
4. 两套环境最终使用同一个发行包，各自保留数据库与数据集。

这是依次升级，不是两个环境同时切换，也不是跨环境的原子事务。各环境切换时会短暂维护。
`--hard` 跳过人工验收批准，但仍保留包校验、环境身份校验、备份、任务排空及健康检查。
开发失败时不会继续更新生产；生产失败时，开发可能已经是新版本。

**本次已修正源码中的硬升级参数传递遗漏，请先使用上面的仓库脚本命令。**
当前正在运行的 R2 工具还不包含该修正，不要用 R2 的全局命令代替它执行未批准版本的硬升级。
下一份从当前源码构建的发行包会包含修正。本手册更新没有触发实际升级。

如果构建已经成功、部署中途失败，应复用原包，不要重新构建同名版本：

```bash
bash deploy/data-platform/update-all.sh --env both --hard --release "$RELEASE"
```

先查看第 5 节的部署状态与日志，解决报错后再重试。失败后可能保留维护状态，不能仅看到进程运行
就认定升级完成，也不要在 Server/Agent 版本不一致时手动解除维护。

## 4. 先开发测试，再更新生产（常规发布）

与第 3 节二选一。先按第 2 节准备一个新版本名，再执行：

```bash
bash deploy/data-platform/release.sh build --env dev --release "$RELEASE" --source "$PWD" &&
bash deploy/data-platform/update-all.sh --env dev --release "$RELEASE"
```

打开 `https://10.8.8.79:8443`，验证功能、角色权限、环境隔离、本地任务、远端 Viewer、远端预处理和回退。
生产在这期间保持原版本。已有构建包需要重试时，仅重新执行第二条部署命令。

完成测试后记录验收结果。下面先生成默认未通过的检查表，**不要未经测试就把全部值改为 true**：

```bash
cat > /tmp/data-platform-release-evidence.json <<EOF_ACCEPTANCE
{
  "release": "$RELEASE",
  "role_permissions": false,
  "environment_isolation": false,
  "local_job": false,
  "remote_viewer": false,
  "remote_preprocess": false,
  "rollback_drill": false
}
EOF_ACCEPTANCE
nano /tmp/data-platform-release-evidence.json
```

网页的 **Approve this release** 同时要求自动检查通过及六项人工确认全部勾选。
若显示自动检查失败，核对开发 Web、本地执行器与配置的 Agent 是否属于当前发行版、心跳是否在
60 秒内更新，并完成当前版本的代表性本地任务。`active (running)` 不能证明执行器仍正常轮询。
若本地执行器日志出现 `Too many open files`，先确认没有活动任务，再重启开发本地执行器恢复心跳；
旧版执行器每次轮询新建 Agent 会累积后台读取线程和连接，重启只能暂时恢复。修复版复用 Agent，
配置或节点身份变化时关闭旧读取线程及连接再重建。恢复后关闭并重新打开验收窗口，重新执行自动检查；
人工确认仍须基于该发行版实际完成的场景。批准只记录验收，不会发布生产。

若本地提交返回 `Expected the registered local executor`，检查本地节点心跳中的 `local_requests`。
本地执行器必须在每次心跳中保留此标记；仅注册时设置会被后续心跳覆盖。修复版本为本地执行器
显式启用此能力，远端 Agent 默认关闭。更新后等待新心跳，再通过本地任务提交与完成确认验收。

只将实际通过的项目改为 `true`。全部验收完成后批准当前安装包：

```bash
bash deploy/data-platform/release.sh approve --env dev --release "$RELEASE" \
  --evidence /tmp/data-platform-release-evidence.json
```

然后选择一种方式发布生产：

- 在开发页面以管理员身份点击 **Deploy to production** 并确认；切换到测试角色时不能发布。
- 或在 Server A 执行：

```bash
bash deploy/data-platform/update-all.sh --env prod --release "$RELEASE"
```

生产安装的是已经测试并批准的原包，不会重新构建当前工作区。
`Same release` 表示两边版本相同，按钮禁用正常。不同版本但按钮禁用时，查看横幅提示的未批准、维护中
或部署进行中等原因。这里比较部署版本，不比较每条数据库记录或数据集内容。

## 5. 怎么确认更新完成

### 任务运行期间升级

首次安装支持滚动部署的版本，或现有 Agent 尚未报告 `rolling_deploy_protocol=1` 时，仍会等待所有活动任务
结束，默认等待上限 5 分钟；超时保留维护状态，任务完成后复用同一个发行包重试。

Server、Agent、本地执行器均已安装这套机制后，`update-all.sh` 和网页的生产发布会自动检查兼容性。
管理数据库版本、任务协议和滚动兼容契约完全一致，且 Agent 心跳有效、没有 interrupted 任务时，
升级保留旧版执行服务及其计算进程。旧任务继续原有 attempt 的心跳、事件上传、结果发布与完成确认，
完成后旧执行服务自动退出；新版本恢复接收新任务。部署期间暂停用户写入和新任务领取，网页仍有短暂维护。
旧版本的节点心跳不会覆盖新版能力；新旧版本共享节点/用户配额、任务槽和数据路径锁。

兼容检查不通过会自动等待任务结束。需要强制使用等待流程时：

```bash
bash deploy/data-platform/update-all.sh --env dev --release "$RELEASE" --wait-for-jobs
```

正在使用或仍有未确认本地任务的发行目录不会被历史清理删除。执行服务固定到发行目录，不从 `current`
动态加载任务代码。保持管理员的开发验收、生产批准、Server 先于 Agent 升级的流程。

滚动升级的备份记录 `quiescent: false`，表示备份期间任务仍在写入；只能用于诊断，自动数据库/状态恢复
会拒绝这类快照。需要可直接恢复的完整备份，应使用 `--wait-for-jobs`。程序回退和显式数据库恢复都会
等待活动任务结束。升级失败仍保留维护状态和任务产物，需要核对部署进度后恢复；不得直接停止旧执行服务。

核对当前别名与各版执行服务：

```bash
systemctl status data-platform-local-worker@dev.service
systemctl list-units --all 'data-platform-local-worker-*@dev.service'
# 在对应 Agent 主机检查：
systemctl status data-platform-agent@dev.service
systemctl list-units --all 'data-platform-agent-*@dev.service'
```

`@prod.service` 对应生产环境。当前别名指向新版本，旧服务运行中表示还有任务收尾；完整部署成功仍需
新版 Server/Agent 心跳、维护关闭以及受影响功能验证，不能仅凭旧任务仍在运行就认定升级完成。

### 更新后的检查

```bash
bash deploy/data-platform/release.sh status --env dev
bash deploy/data-platform/release.sh status --env prod
curl -fsS http://127.0.0.1:9092/healthz
curl -fsS http://127.0.0.1:9091/healthz
```

上面的 HTTP 地址只供 Server A 本机检查。最终应看到两边 `release` 为预期版本、`maintenance: false`，
部署状态为 `installed`；完整升级命令还会验证 Agent 的新版本心跳。浏览器刷新后再检查实际功能。
只执行 `update-server.sh` 会保留维护状态，因此日常完整更新应使用 `update-all.sh`。

命令行执行失败时保留终端错误输出。页面发起的生产升级可查看：

```bash
sudo journalctl -u data-platform-production-promotion --no-pager -n 100
sudo journalctl -u data-platform-web@prod.service --no-pager -n 100
sudo journalctl -u data-platform-promotion.service --no-pager -n 50
```

备份位于 `/opt/data-platform/dev/backups/` 和 `/opt/data-platform/prod/backups/`；
首次生产迁移的额外备份位于 `/var/lib/data-platform-deployment/`。
完整升级成功后，每套环境自动保留当前程序、另外两个最近的完整安装版本和最近三份完整部署备份；
失败或仅 Server 更新不会触发清理。未完成的安装、备份及链接目录留待人工检查。
发行包清理须保留两套环境的当前版本和需要用于回退的包，不能仅按目录名称或磁盘大小删除。

Server A 的 MySQL binlog 保留策略为 3 天，使用 `MINIMAL` 行记录、`PARTIAL_JSON` 和事务压缩；
修改这项主机配置不会自动由代码部署完成。首次收紧期限前应完成可校验的数据库备份，并检查复制及恢复需求，
旧 binlog 通过 MySQL 的 `PURGE BINARY LOGS` 清理，不能直接删除文件。
文件操作日志每份达到 32 MiB 时轮转，当前文件保持 JSONL，四份历史文件采用 gzip 无损压缩；
审计查询兼容新旧格式并包含这些近期文件，
数据库审计记录不随文件轮转删除。数据集周期同步的审计保留登记身份和脱敏元信息摘要，不重复保存完整目录。
元信息没有变化时不逐轮更新数据集登记表，存在时间最多每五分钟刷新一次；真正的元信息变化仍立即保存。
Agent 心跳请求与两分钟离线判定保持原有频率；数据库心跳快照最多每 30 秒刷新一次，
磁盘空间快照随心跳刷新，节点状态、协议、版本及权限能力变化立即保存。
成功的节点心跳、空闲任务领取、普通租约续期和无登记变化的目录同步不逐条写操作审计；
失败、实际领取任务、停止指令、新登记和元信息变化仍保留审计，任务事件和结果同步不受影响。
MySQL 调度使用事务行锁，避免空闲轮询递增锁记录；SQLite 仍保留写锁保护并发。
程序回退与数据库恢复是不同操作，具体参见 [双环境详细教程](data_platform_environments.md#查看重启与故障恢复)。

## 6. 构建遇到之前的 PyPI TLS 错误

仅当出现此前同样的 PyPI 代理/TLS 下载错误时，可以在 Server A 使用已验证可直连的镜像重新构建：

```bash
sudo env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
  -u https_proxy -u http_proxy -u all_proxy \
  UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  bash deploy/data-platform/release.sh build --env dev --release "$RELEASE" --source "$PWD"
```

构建成功后，根据所选流程使用 `--release "$RELEASE"` 部署已有包。
这是依赖下载方式的调整，不会关闭 TLS 校验，也不需要修改锁文件。

## Agent 任务并发与资源预算

Agent 按 `--sync-seconds` 定期扫描 `allowed_roots` 并上报数据集目录（默认 300 秒，最短 10 秒），
有任务运行或全部槽位占满时也继续扫描；计算与任务续租由执行线程独立处理。
扫描只读取数据集元数据，不自动准备 Viewer。Viewer 标注结果由独立后台线程回传，任务运行及
槽位占满时也继续执行；网络传输不占用计算槽位，也不阻塞主循环心跳和任务领取。
同一数据集的源修改任务未确认完成、或缓存正在发布时，只推迟该位置的写入并自动重试；
其他位置继续同步。远端页面的 `Scan` 向选中 Agent 提交即时目录扫描，由下一次心跳领取，
不等待自动扫描周期或计算任务槽位；`Refresh` 只重新读取已登记目录。扫描请求持久化保存，
每节点最多一个未完成请求；同一账号重复提交相同路径复用该请求。状态依次为等待、扫描中、
完成或失败，只有源目录读取、登记同步及最终确认都成功才显示完成。最终确认暂时失败时优先重试
确认，不重复扫盘；Agent 重启后可重新执行未完成的只读扫描。

即时扫描要求节点心跳能力 `dataset_scan_protocol=1`，Server 先迁移到管理 schema 5 并升级，
然后升级 Agent。旧 Agent 的 Scan 禁用并提示升级，Refresh 仍可使用。请求在 Server 和 Agent
两端检查 `allowed_roots`，Agent 解析真实路径以拒绝越界软链接；读取仍受服务账号的 Linux 权限限制。
首页 **Known directory** 按所选 Agent 的实际 `allowed_roots` 显示下拉选项；也可输入其下的自定义路径，
不会通过网页增加授权。修改 Agent 环境配置中的根目录后，需安全重启或随正常滚动升级启动新版 Agent；
新版 Agent 在心跳中同步实际的 `allowed_roots` / `writable_roots`，无需重新注册或轮换节点令牌。
旧版本执行器的心跳不能覆盖当前发行版的目录配置；旧 Agent 未上报目录时保留原登记。
只有 Admin、Data Manager 和具有全数据集读取范围的 Operator 可发起发现，Viewer 保持只读。
权限在提交和开始扫描时复核，扫描中的任务不因范围撤销而取消。请求与结果沿用 `dataset.scan`
操作审计，不新增独立日志系统。

`workers` / `prepare_workers` 是请求值。Agent 在启动任务时结合节点上限、CPU affinity、
cpuset 和所有父级 cgroup 限制计算实际值，并通过 Jobs 的 `resources` 事件记录请求值、
生效值和截断原因。显式 `1` 保持串行；支持 `0` 的预处理操作继续将其解释为自动选择。
普通节点未配置时保留最多 4 worker 和原有 CPU/内存比例。

管理员可在目标环境的 `agent.env` 中设置：

| 配置 | 含义 |
| --- | --- |
| `DATA_PLATFORM_COMPUTE_MAX_WORKERS` | 单任务 worker 上限，正整数，默认 4 |
| `DATA_PLATFORM_COMPUTE_CPU_LIMIT` | 单任务 CPU 核数预算，优先于 CPU fraction |
| `DATA_PLATFORM_COMPUTE_MEMORY_LIMIT_GIB` | 单任务内存 GiB，优先于 memory fraction |
| `DATA_PLATFORM_COMPUTE_NATIVE_THREADS` | 每个计算进程的 BLAS/OpenMP/OpenCV/Arrow 线程数 |
| `DATA_PLATFORM_VIDEO_THREADS` | 每个视频编码、解码和滤镜的线程上限 |
| `DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS` | Agent 同时执行的远程 Job 槽位，默认 1，当前最多 4；CPU 和扣除 Agent 预留量后的内存按槽位数固定均分（例如父配额 32 核/64 GiB，双槽各 16 核/30 GiB） |
| `DATA_PLATFORM_CAPTION_RUNNING_LIMIT` | 旧 Agent 的 Temporal Caption 双任务兼容上限；新 Agent 使用通用 Job 槽位 |

Viewer 的并发单位是一个相机视频或一份 episode CSV；单 episode 的多相机也可并行。
所有输出共用一个有界队列，episode 的全部工作成功才增加完成计数，文件完成数单独显示在事件中。
保持编码器、时间边界及 Stage 语义；失败/取消不发布不完整的新缓存。
Merge、标准化、平滑和转换继续沿用既有算法及线程/进程池，通过相同预算限制并发。

提高应用配置不能突破 systemd 配额。开发 Agent 的通用默认值仍是 2 核/4 GiB。
需要提高时，排空开发任务后使用 `deploy/data-platform/agent-resources.conf.example`
建立 `data-platform-agent@dev.service.d/50-resources.conf`，再执行 `systemctl daemon-reload`
并重启开发 Agent。该 drop-in 与生成的 unit 分开保存，升级不会覆盖；生产配置不随之变化。
修改前备份目标 `agent.env` 及 drop-in（记录原先不存在的情况），回退时在任务排空后一起恢复。
保留原有 GPU 隔离、CPUWeight/IOWeight 和文件系统保护设置。

基准脚本 `scripts/benchmark_data_platform_compute.py` 仅处理指定开发样本，将结果写入全新的
输出目录，并核对输入指纹。`--sample single|v21|v3` 分别匹配三相机的 1/48 episode v2.1、
451 episode v3.0（Viewer 取前 64 个 episode）；不匹配或匹配不唯一时拒绝执行。
使用同一服务账号、独立 systemd cgroup 和相同输入比较旧/新版本，原配额与提高后的配额分开记录。
该脚本测量计算与本地结果写入，不模拟上传；线上验收另外记录完整 Jobs 生命周期。

```bash
python scripts/benchmark_data_platform_compute.py \
  --datasets /path/to/development-datasets \
  --output /path/to/new-benchmark-output \
  --sample v21 --workers 16 --operation viewer
```

高并发测试从 32 核/64 GiB 开始，再测试 64 核/128 GiB；native threads 设为 1，
video threads 测试 1/2。每次输出目录必须唯一，不清理全机页缓存。结果 JSON 不保存源数据路径。
H100 开发节点可将 Server 的 `DATA_PLATFORM_NODE_RUNNING_LIMIT` 和
`DATA_PLATFORM_USER_RUNNING_LIMIT` 都设为 `2`，Agent 设 `DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS=2`。
实际节点并发取 Server 上限与 Agent 心跳能力中的较小者；旧 Agent 未上报通用槽位时按 1 槽处理，
并保留旧 Caption 专用双任务兼容逻辑。Viewer、预处理、Temporal Caption、Curation、MCAP 导入
共用配置的槽位；不同任务只有在所有输入数据集和输出路径互不重叠时并行，路径相同或互为父子时排队。
同一数据集的 Caption 可以并行标注不同 episode，同一 episode 不会重复执行；直接修改源数据的
任务独占节点。Agent 启动前以解析后的真实路径再次检查，持有资源锁至结果确认；等待锁位时续租。
Jobs 显示 `queue_wait_reason` 与 `node_slots`，以区分节点容量、用户容量与路径冲突。

原始 MCAP location 不设置 Viewer `output_dir`；`import.mcap.inspect` 只保留源路径占用，
`import.mcap.convert` 保留源路径及最终输出路径。对已完成计算、但上传中断的 `viewer.prepare`
任务，同一任务的安全 Retry 会在源数据指纹、发布凭据、任务配置和全部缓存文件均吻合时复用
Agent 上的已发布缓存，仅重新上传文件并完成 Server 注册。上传期间持续续租；任一校验不符时
停止复用，不覆盖既有缓存。确认 Server 端 Viewer 已注册后才将任务视为恢复完成。

开发父配额 32 核/64 GiB、双槽时，无论只运行一个还是两个任务，每个任务最多 16 核/30 GiB，余下约 4 GiB 留给 Agent 与系统；
`workers` 仍受每槽 CPU 和节点 worker 上限约束，原生与视频线程单独限制。超过 30 GiB 的任务应
在排空后改为单槽运行，不能临时借用第二槽内存。API 标注并发可能受供应商限流约束。
生产 H100 可将 Server 的 `DATA_PLATFORM_NODE_RUNNING_LIMIT=4`、
`DATA_PLATFORM_USER_RUNNING_LIMIT=2` 与 Agent 的 `DATA_PLATFORM_AGENT_MAX_RUNNING_JOBS=4` 配套设置。
同一用户最多运行两个任务，多个用户合计最多四个；资源按四槽固定均分，单任务不借用空闲槽配额。
保留既有节点总资源预算；Server A 本地执行器仍为单槽。配置变更须在运行任务完成或确认取消后
重启对应服务，运行中任务不热改配额；排队任务保留。

完整基准矩阵可由 `scripts/benchmark_data_platform_matrix.py` 顺序执行。该驱动需要测试节点的
root 权限来创建临时 systemd 服务，计算进程使用 `--user` 指定的开发服务账号。
对相同参数依次运行 `--stage baseline`、`--stage screen`、`--stage repeat`；已完成结果可复用，
未完成目录会阻止重跑，以免覆盖失败证据。`--operation` 可选 viewer/smooth/standardize/merge/convert；
Merge 自动选择另一份兼容的开发数据集，找不到时停止，不复制或修改源数据。

```bash
python scripts/benchmark_data_platform_matrix.py \
  --old-python /path/to/baseline/.venv/bin/python \
  --new-python /path/to/candidate/.venv/bin/python \
  --datasets /path/to/development-datasets \
  --output /path/to/benchmark-matrix \
  --user data-platform-agent-dev --operation viewer --stage baseline
```

`screen` 对 32/64 核分别测试 4/8/16/32（以及 64 核下的 64）worker；Viewer/转换测试
每视频 1/2 线程，其余预处理使用 1 线程。`repeat` 将基线及筛选出的最快两组补足三次。
源代码验证可附加 `--new-source /path/to/checkout`；正式部署验收应使用安装后的解释器。

## 任务进度与日志接入约定

新增或修改任务必须沿用 `job_progress.py`、`RouteContext.update_job()` 和 Agent 的事件通道，
不要在模板里从最后一条日志重新计算总体百分比，也不要建立另一套日志或进度状态。

- `phase`（或已有 `step`）表示稳定的阶段；`current/total` 表示该阶段已完成的工作量。
  多阶段任务应显式提供 `progress`（0–100 的总体估计），在调用子流程时映射到预留区间；
  不能把每个 episode、文件、子流程各自的 0–100 当成整个任务的百分比。
- 不知道总量时只报告阶段和消息，不编造计数或 ETA。Server 保留已知进度；旧回调的计数只作为
  单调的历史最高估计，不保证线性时间。100% 由最终成功确认设置，计算结束仍可能有发布和上传。
- 当前状态与日志摘要分离：每条事件更新快照；摘要保留阶段切换、约 10% 里程碑和不同的普通提示。
  警告/错误不采样。高频逐帧/文件事件应使用计数与稳定阶段；不能靠不断改变阶段名绕过摘要规则。
- 上传计数只能在真实上传成功后递增。离线计算把 artifact 加入 spool 不是上传完成；由 supervisor
  上报真实传输进度。事件按生产序号补发，完成前排空积压；同一个事件重送保留 delivery ID。
- 重试清空当前进度、错误和计时，保留历史 attempt；旧 attempt 不得更新新 attempt。
  同一执行的百分比采用单调保护，晚到的页面请求不能覆盖更近的任务状态。

`tests/datasets/test_job_progress.py` 已列入发行包必跑验证。接入多阶段或并发任务时补充行为测试，
覆盖阶段切换、文本事件、真实完成计数、重试、事件顺序和最终确认；UI 改动还需浏览器验证。


## 源目录删除与缓存回收

Server 与 Agent 均需支持 `source_cleanup_protocol=1`，且执行器发行身份须与当前环境一致。
控制面在维护模式暂停检查与确认；普通 Refresh 不进行目录扫描。节点后台以 15 秒间隔分页检查
已登记路径，两次一致的缺失证据后清理。大量数据集按页轮转，不能用 30 秒作为完成时限。
`dp_source_cleanups` 保存 location、令牌、阶段、错误、任务页游标及短租约；重启会续清理。
MySQL 的确认/更新时间及租约使用双精度列，避免 epoch 秒被普通 FLOAT 舍入。升级自动扩宽旧清理表并重新计时未确认的缺失证据。
`dataset.source_cleanup` 事件写入现有审计。`deleting`/`deleted` 不允许新 Job 或 Pipeline 输入，
普通位置列表隐藏它们；历史查询仍保留原 location 和 Job 外键。
历史 Job 的手动重试和 MCAP 自动恢复也校验全部输入；已删除输入不会重新排队，Jobs 显示不可重试原因。

若清理停留在 waiting，先查看该位置及缓存别名的运行、取消确认和 interrupted 任务；不要强制删除缓存。
node/server 阶段失败时检查登记 output_dir 的归属、可写根、文件系统身份、共享缓存和独立数据保护。
错误可修复后自动重试。服务器不依据心跳丢失或扫描未发现就删除数据。手动 unregister 仍仅移除登记，
不删除文件。范围与保留规则见[存储说明](data_platform_storage_locations.md#原始目录删除后的清理)。

## HTTPS 地址与证书

Server A 的用户入口是 `https://10.8.8.29`（开发环境端口 `8443`）。证书 SAN 必须包含
实际访问 IP；仅信任证书不能解决 IP 不匹配。当前服务器叶子证书由原内网 CA 签发，
包括当前 IP、已有回环及旧 IP，Agent 的 SSH 转发入口继续使用 `127.0.0.1`。
CA 公共证书为 `/etc/nginx/tls/data-platform-ca.crt`，尚未信任该 CA 的客户端须导入它；
不得分发任何私钥。证书更新前备份证书与密钥，验证证书链和 SAN，运行 `nginx -t` 后重载，
等待新连接提供新证书，再使用可信 CA 正常校验开发/生产 `/healthz` 和 Agent 心跳。
使用 `curl -k` 或浏览器忽略证书错误不构成 TLS 验证通过。
