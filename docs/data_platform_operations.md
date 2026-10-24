# 当前环境构成与日常更新手册

流程更新：2026-10-15。主机地址和目录来自既有配置，执行前核对目标环境。
2026-10-15 18:04（Asia/Shanghai） 核对到 dev/prod 安装身份均为
`V0.0.6.55_DatasetIndexRefresh_20261015`；
未重新验收远端 Agent、运行任务、维护状态或 TLS。
2026-09-15 的 R2 是历史部署快照；当前版本、维护状态以第 2、5 节的现场检查为准。
已完成首次迁移的环境，日常更新不再执行 `init`、`adopt-legacy`、重新建库或重新登记 Agent。

代码、数据库物理目录、数据集、缓存和配置的完整位置清单见
[开发与生产存储位置说明](data_platform_storage_locations.md)。

## 1. 现在由哪些部分组成

Server A 承载两套独立控制服务，H100 与 Server B 按注册环境运行 Agent；不是两个 Git 分支直接启动。
浏览器使用管理员提供的当前稳定域名/IP；下图用 `<server-address>` 表示，旧 DHCP 地址不作为固定入口。

```text
浏览器
  ├─ https://<server-address>       → Server A：Nginx 443  → prod Web 9091
  └─ https://<server-address>:8443  → Server A：Nginx 8443 → dev  Web 9092

Server A（ubuntu-22）
  ├─ prod：Web、本地任务执行器、生产业务库/日志库、生产控制台状态
  ├─ dev ：Web、本地任务执行器、开发业务库/日志库、开发控制台状态
  └─ 发布桥接服务：接受开发管理员的发布请求，执行生产升级

H100（h100-05，通过 SSH 连接）
  ├─ prod Agent（h100-05）：访问原生产数据，独立程序与运行状态
  └─ dev  Agent（dev-node）：访问开发样本副本，独立程序与运行状态

Server B（固定局域网名 data-platform-server-b.local）
  └─ 已登记 Agent（Server B (bingyu)）：访问授权数据根，环境以登记与 agent.env 为准
```

H100 的 Agent 经现有 SSH 隧道连接 Server A：生产使用 H100 本地 `9443 → Server A 443`，
开发使用 `9444 → Server A 8443`。这些是 Agent 内部连接端口，浏览器使用上面的网页地址。

| 项目 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 网页入口 | `https://<server-address>` | `https://<server-address>:8443` |
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

两套环境共用 MySQL 服务，但使用四个逻辑库及各自数据库账号。Server、执行器、Agent 和隧道的
自启状态需分别用 systemd 核对。开发的 8443 是当前配置，不随普通发行升级自动变化。

主源码工作区是 Server A 上的 `/home/yuhao.song/Codes/data_platform`，功能也可能位于独立 worktree。
用 `git worktree list` 和 `git log --all` 查明实际发布来源；主目录 HEAD 不代表最新安装版本。
修改源码不会立即改变网站；需要提交、构建发行包，再部署。
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

2026-10-14 操作记录中，两端 Avahi、生产隧道及 Server B Agent 已启用开机自启。连接存活检测每 30 秒一次，
连续三次无响应后重建隧道；重建时重新解析名称。SSH 主机密钥已绑定原 Server B 身份，
保留严格校验。2026-10-14 的操作记录已验证固定名称解析、密钥登录、正式部署前检、隧道中断后自动恢复与新心跳；
这不是本次文档更新的重启验收。Mac 的 SSH 别名需单独配置，不能沿用 Server A 已配置成功的结论。
名称发现要求客户端与 Server B 的局域网支持 mDNS；跨越不转发 mDNS 的网段时，应使用网络管理方
提供的固定 DNS 名称或 DHCP 地址保留，不能假定 `.local` 能跨网段解析。

经用户授权，现有部署密钥已移除 `from=` 来源 IP 限制；Server A 的 IP 变化不再要求修改
这条密钥规则。root 密钥仍保留禁止端口转发、Agent 转发、X11 转发和 PTY 的限制，
管理账号另有可正常登录的同一公钥。现有隧道继续使用 `/home/yuhao.song/.ssh/id_ed25519`，
其转发限制及强制命令保持原样；不要用隧道密钥替代普通登录或部署密钥。
密钥文件和配置已有备份；文档不记录密码或私钥内容。

```bash
cd /home/yuhao.song/Codes/data_platform
git status --short --untracked-files=all
readlink -f /opt/data-platform/dev/current
readlink -f /opt/data-platform/prod/current
ls -1 /var/lib/data-platform-releases
ls -1 /opt/data-platform/dev/releases
ls -1 /opt/data-platform/prod/releases
git tag --list 'V*' --sort=version:refname
```

如果 Git 状态有输出，先审查差异、完成相关测试，只提交本次发布涉及的明确文件路径。
不要删除或暂存隐藏无关改动，也不要把配置、密码或数据加入 Git；无关工作阻碍发布时，准备独立、
干净且已提交的 release worktree。发布命令的 `cd` 必须指向该工作区。提交后再次检查
`git status --porcelain --untracked-files=all`，要求无输出。

根据上述实际部署、已构建包、已安装目录和 Git release tags，按**数值**比较版本段（`.14` 大于 `.9`），
选当前系列最高版本的下一个未使用版本。使用 `V<version>_<FeatureName>_<YYYYMMDD>`，
不能通过更换功能后缀复用旧数字版本。目录不可访问时先解决检查问题，不从旧回复猜版本。
在同一个终端输入已核对的新版本名，后续命令复用该变量：

```bash
read -r -p '输入已核对的新版本名：' RELEASE
test -n "$RELEASE"
```

交付命令前再核对一次版本未被占用，并记录源码工作区和提交。仅文档或本地规则更新无需构建发行包。
默认采用第 4 节的 dev 验证与生产批准流程；第 3 节仅用于明确要求的双环境硬升级。

### 发布源码与并行功能集成

默认工作区可能落后于已安装版本。选择最新已核验源码作为基线，将准备发布的功能分支合入一个
干净的集成 worktree，保留功能分支及未提交修改。确认已部署源码和各功能提交均为集成 HEAD 的祖先，
再运行组合测试；无文本冲突不代表组合行为正确。构建仅包含选定工作区已提交的 HEAD，
部署锁不会合并分支或分配版本号。同一环境的发布串行执行，发布前重新核对线上是否已被其他工作升级。

在实际发布源码目录检查解释器，不依赖 wrapper 回退到已安装版本的 Python：

```bash
test -x .venv/bin/python
.venv/bin/python -c 'import pytest; import lerobot.data_platform.releases; import scripts.build_data_platform_agent_bundle'
```

使用这个解释器运行相关验证。构建 `Validating` 失败不等于已切换线上服务；支持失败记录的版本
将报告保存在 `/var/lib/data-platform-releases/.failed-<版本>-*/validation.txt` 与 `failure.json`。
检查终端给出的实际路径。只有发行包尚未生成且编号仍未占用时，修复后才可重用原 `--version`；
已有包的部署重试使用 `--release`。

## 3. 一条命令同步更新开发和生产（硬升级）

完成第 2 节后执行：

```bash
bash deploy/data-platform/update-all.sh --env both --hard --version "$RELEASE" \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

实际顺序为：

1. 从已提交的源码构建一次 Server/Agent 包，并执行构建回归测试。
2. 更新开发 Server，再更新开发 Agent，验证开发健康状态。
3. 更新生产 Server，再更新生产 Agent，验证生产健康状态。
4. 两套环境最终使用同一个发行包，各自保留数据库与数据集。

这是依次升级，不是两个环境同时切换，也不是跨环境的原子事务。各环境切换时会短暂维护。
`--hard` 跳过人工验收批准，但仍保留包校验、环境身份校验、备份、任务排空及健康检查。
开发失败时不会继续更新生产；生产失败时，开发可能已经是新版本。

使用已核对的源码工作区脚本；不要假定系统全局命令与该工作区具有相同版本或参数行为。

如果构建已经成功、部署中途失败，应复用原包，不要重新构建同名版本：

```bash
bash deploy/data-platform/update-all.sh --env both --hard --release "$RELEASE"
```

先查看第 5 节的部署状态与日志，解决报错后再重试。失败后可能保留维护状态，不能仅看到进程运行
就认定升级完成，也不要在 Server/Agent 版本不一致时手动解除维护。

## 4. 先开发测试，再更新生产（常规发布）

与第 3 节二选一。先按第 2 节准备一个新版本名，再执行：

```bash
bash deploy/data-platform/update-all.sh --env dev --version "$RELEASE" \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

打开 `https://<server-address>:8443`，验证功能、角色权限、环境隔离、本地任务、远端 Viewer、远端预处理和回退。
生产在这期间保持原版本。已有构建包需要重试时使用
`bash deploy/data-platform/update-all.sh --env dev --release "$RELEASE"`，不要同名重建。

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

### 有运行任务时的兼容升级

支持滚动部署的 Server、Agent 和本地执行器会检查数据库、任务协议及兼容契约、有效心跳和
`rolling_deploy_protocol=1`。满足条件时，旧执行服务继续完成原 attempt 的计算、上传和最终确认，
新服务接管后续任务。部署仍短暂停止新提交与领取，不能承诺网页全程无中断。
兼容就绪不代表生产已获批准；仍须完成第 4 节的开发验收并批准同一不可变包。

首次升级到该机制、旧 Agent 不具备能力或兼容检查失败时，会等待活动任务结束。
需要完整静止备份时显式等待（仅在支持该参数的发布源码中执行）：

```bash
bash deploy/data-platform/update-all.sh --env dev --release "$RELEASE" --wait-for-jobs
```

滚动备份的 `quiescent: false` 表示仍有任务写入，不能作为自动数据库/状态恢复的完整快照。
不要手动停止旧版执行服务或删除仍有未确认任务的发行目录。升级失败时保留任务产物，核对部署
进度与维护状态后重试，不能靠取消无关队列来通过部署。

### 更新后的检查

```bash
bash deploy/data-platform/release.sh status --env dev
bash deploy/data-platform/release.sh status --env prod
curl -fsS http://127.0.0.1:9092/healthz
curl -fsS http://127.0.0.1:9091/healthz
```

上面的 HTTP 地址只供 Server A 本机检查。最终应看到两边 `release` 为预期版本、`maintenance: false`，
部署状态为 `installed`；完整升级命令还会验证 Agent 的新版本心跳。核对 Server/Agent 的实际
`current` 链接与预期发行包，并检查本次功能需要的 capability，例如 `mutation.trim_episode`、
`result_sync_protocol=1` 或 `caption_signal_options`。浏览器刷新后，用对应角色验证实际操作。
进程 `active` 或 `/healthz` 成功不能单独证明新功能已上线；准备好命令也不等于已经部署。
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

## 7. Viewer 缓存与结果同步排障

先核对环境、注册的 location ID、Agent、源根与 `output_dir`，再按
[存储位置说明](data_platform_storage_locations.md) 区分四类目录：

| 所在位置 | 用途 | 检查重点 |
| --- | --- | --- |
| Agent 的 `vis/.dp-<job_id>-<attempt_id>/cache` | 执行暂存 | 任务是否仍在计算或发布 |
| Agent 登记的 `output_dir`，默认 `vis/local_vis_<dataset>` | 已发布缓存 | manifest、源身份、旧缓存备份 |
| Server A 的 `remote-cache/<location_id>/static/` | 网页结果主副本 | 保存后的结果及同步 revision |
| Server A 的 `.jobs/<job_id>/<attempt_id>/` | 上传暂存 | 不应当作已完成结果的长期目录 |

网页保存成功与 Agent 同步成功是两个状态。只有对应 revision 获得 Agent 写入确认，才能认定
`Results synced`；待同步或失败时检查 Agent 在线状态、`result_sync_protocol=1`、摘要和身份匹配，
以及实际服务账号的目录/文件权限。检查父目录遍历权限、ACL mask、allowed/writable roots 和备份目录，
不能靠切换网页管理员解决 Linux 权限错误。详见[权限说明](data_platform_permissions.md)。

重建缓存会保留兼容的人工结果；episode 布局或阶段配置变化时，不兼容结果留在备份中。
结果回传只同步受支持的标注和编辑产物，不会把源 Parquet、视频或 `meta/` 改成网页标注。
不要为消除同步错误直接删除旧缓存或强行套用不兼容标注。

## 8. 远程任务停滞与恢复

页面长时间停在 `Checking existing video/CSV cache` 或发布步骤，不足以证明任务挂起。
先核对同一 job/attempt 的事件时间、执行 lease、进程、输出大小/修改时间，以及 Agent 执行目录中的：

- `marker.json`：执行身份、staging、publishing 和 acknowledged 状态；
- `result.json`：计算是否已结束、是否成功；
- `published.json`：发布阶段产物；存在此文件仍不等于 Server 已确认完成；
- `stop.json` 与上传记录：是否请求停止、哪些产物尚待上传或确认。

`result.json` 为 done、计算进程已退出但尚未 acknowledged 时，应先排查发布/上传/确认与 lease，
不要直接重跑 Merge 或再次调用模型。遇到目标缓存已存在，先验证归属和 manifest 并保留恢复备份。
恢复应使用现有执行协议完成 lease、finalizing、上传与确认，不直接篡改数据库状态来“标记成功”。
完成后核对控制台终态、acknowledged、注册输出、manifest 中的最终路径和 Viewer 是否可打开。
报告时分别说明“本次任务已恢复”和“防复发修改是否已验证、部署”。

查询日志用明确时间范围，例如在目标 Agent 主机执行（按实际服务名和时间替换）：

```bash
journalctl -u data-platform-agent@dev.service \
  --since '2026-09-22 09:00:00' --until '2026-09-22 09:30:00' --no-pager
```

## 9. 网页 HTTPS 证书提示

网页可访问、TLS 建连、证书受信任和名称匹配需要分别检查。`curl -k` 会跳过证书验证，
不能作为证书修复成功的证据。以浏览器实际使用的域名/IP 和端口做正常验证，并检查证书
issuer、有效期和 SAN；自签证书不受信任与访问 IP 不在 SAN 中可以同时存在。

长期使用稳定域名和客户端信任的 CA；保留 IP 访问时证书也需要相应 IP SAN。
证书与 Nginx 修改按实际授权执行，修改后先 `nginx -t` 再 reload，并重新正常验证。
不要输出私钥、令牌或完整环境配置来排障。


## 10. 数据集与任务列表超时

登录后分别检查 `/api/control/locations`、`/api/control/jobs?limit=200` 的耗时、传输大小与错误，
同时查看对应 Nginx access log。401 只是未登录，`/healthz` 很快也不代表大列表传输正常。
完整元数据/结果可能使 JSON 达到数十 MB；499 需结合请求时间和客户端取消分析，不能直接认定数据库卡死。

Nginx 的 dev/prod 配置须包含以下配置，代码生成器与示例配置也要一致：

```nginx
gzip on;
gzip_types application/json;
gzip_min_length 1024;
gzip_vary on;
```

备份实际加载的配置，执行 `nginx -t` 后热加载 Nginx。仅修改代理配置时无需重启 Web 或 Agent。
用已登录浏览器重新打开受影响列表，检查请求 `Accept-Encoding`、响应 `Content-Encoding: gzip`
和 `Vary: Accept-Encoding`，再确认渲染与交互；压缩不替代对慢查询的检查。

2026-10-14 的历史修复记录显示，locations/jobs 响应从约 10.2/52.8 MB 降到约 1.2/6.0 MB，
用户确认刷新恢复；这些是当时测量，不能当作当前性能指标。持久化修复提交为 `1f44fd2d`。
后续 `f3165e76` 批量读取 Job 关联记录，并让节点、数据集和任务列表独立渲染；慢任务请求不再
阻塞已返回的数据集，失败保留对应列表上次内容并显示错误。代理热加载与应用发行升级分别记录。

## 11. 扫描、配额与任务恢复

- **目录发现**：`Scan` 请求 Agent 即时只读扫描，不占计算槽；`Refresh` 只读已登记数据。
  `Known directory` 来自 Agent 的实际 `allowed_roots`，自定义子路径不增加授权。修改根配置后，
  通过安全重启或正常升级使新 Agent 在心跳中同步，无需重新登记或换令牌；检查实际路径和 Linux 权限。
- **任务并发**：节点槽位、用户额度、路径锁和资源预算同时生效。最多 4 个远程槽位是能力上限，
  不是每个节点的当前配置。优先看 `queue_wait_reason`、`node_slots` 与资源事件，再调整配置。
- **额外额度**：管理员给单个排队任务 `Use extra quota (+1)` 只绕过普通用户并发额度，仍受节点
  容量、路径锁、维护和权限限制；授权并非已经运行。手动 Retry 清除授权，同一 MCAP 自动恢复保留。
- **MCAP 自动恢复**：确认计算进程丢失后，原任务可从校验过的检查点重新排队，连续最多 3 次。
  主动取消、普通转换错误和发布失败不自动重算。检查历史 attempt、`auto_recovery` 及最终输出登记，
  不把重新排队计作成功。已完成计算的 Viewer 上传失败按第 8 节处理，不能无条件重新 Prepare。
- **本地执行器失联**：若日志出现 `Too many open files`，核对版本与活动任务；修复版复用 Agent
  连接并关闭旧读取资源，单纯重启只能暂时缓解旧版泄漏。恢复必须检查新心跳与代表性任务完成。

`.55` 的被动页面加载、返回首页及 Refresh 使用 `GET /api/dataset_roots?scan=0` 读取登记，
不会遍历 Server A 数据目录；显式 Scan/本地 Rescan 才发现目录。省略 `scan` 的旧 API 保留原语义。

后台进度按 attempt 保存。阶段 `current/total` 与总体百分比不同；普通日志不能清零已知进度，
旧 attempt 事件不能覆盖新执行。实际上传完成才增加上传计数，最终成功确认才设置 100%。

## 12. 动态地址与通知记录

稳定内网 DNS 或 DHCP 地址保留优先；`.local` 仅适用于能解析 mDNS 的网络。检查隧道实际
systemd 配置、目标主机密钥、监听端口和新 Agent 心跳，不能只看 SSH 连接成功。

此前已配置 Server A 的事件驱动地址通知：开机及地址变化触发，避免依赖 Mac 持续轮询。
该记录不代表已经做过真实重启到通知送达的端到端验收，也不代表当前通知服务健康。
如需验收，应另行安排获授权的维护窗口。Webhook 只保存在受保护的本机配置，不写入仓库、截图或日志。
