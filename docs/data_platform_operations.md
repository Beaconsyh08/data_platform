# 当前环境构成与日常更新手册

2026-09-15 核对：生产与开发均为 **R2**，维护状态均关闭。首次迁移已经完成，
日常更新不再执行 `init`、`adopt-legacy`、重新建库或重新登记 Agent。

代码、数据库物理目录、数据集、缓存和配置的完整位置清单见
[开发与生产存储位置说明](data_platform_storage_locations.md)。

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

源码工作区是 Server A 上的 `/home/yuhao.song/Codes/data_platform`。
修改这里的代码不会立即改变网站；需要提交、构建发行包，再部署。
发行包统一保存于 `/var/lib/data-platform-releases/版本名`，同一名称对应一份不可变安装包。

**“同步更新”指把同一份程序版本部署到两套环境，不是同步数据库、账号或数据集。**
更新可能在各自数据库上执行表结构迁移；不会把开发库覆盖到生产库。

## 2. 每次更新前

以下命令全部在 **Server A** 上执行，不需要另开 H100 终端手动安装 Agent。

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
首次生产迁移的额外备份位于 `/var/lib/data-platform-deployment/`。不要删除还需要用于恢复的旧发行包或备份。
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
运行中任务不热修改资源配置；任务级并发不会让一个 Agent 同时领取多个任务。

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
