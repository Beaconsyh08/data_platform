# 开发与生产：代码、数据库和文件具体放在哪里

2026-09-15 实机核对，两套环境当前均运行 R2。

## 先回答“是不是放在一起”

**在同一台机器上的不同目录、不同数据库中，不是开发与生产各占一台机器。**

- Server A（`ubuntu-22`，网页 IP `10.8.8.79`）：同时运行生产和开发 Web、本地执行器、MySQL、Nginx。
- H100（`h100-05`，可通过 `ssh h100-server` 登录）：同时运行生产和开发 Agent，并存放各自的数据集文件。
- 两套环境共用操作系统、硬件、Nginx 和 MySQL 服务，但不共用业务库、日志库、登录会话或运行状态目录。
- Git 源码工作区用于修改代码。运行网站使用另外安装的发行包，修改源码文件不会立即改变网站。

**相同路径出现在两台机器上，不代表同一个目录。** 例如 Server A 和 H100 都有开发数据根配置
`/srv/data-platform-dev/datasets`，但它们是各自机器的本地目录，没有因为名字相同而自动同步。
之前复制的开发样本在 **H100** 上，Agent 处理后把 Viewer 缓存上传到 Server A。

## 1. 代码在哪里

日常开发只在 Server A 的这个 Git 工作区修改代码：

```text
/home/yuhao.song/Codes/data_platform
```

生产和开发网站都不是直接从这个工作区启动。部署关系是：

```text
源码工作区 → Git 提交 → 构建一份发行包 → 安装到 dev / prod 各自目录
```

### Server A 上运行的网站代码

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 当前程序入口，软链接 | `/opt/data-platform/prod/current` | `/opt/data-platform/dev/current` |
| 当前 R2 的实际目录 | `/opt/data-platform/prod/releases/R2` | `/opt/data-platform/dev/releases/R2` |
| 应用源码 | `/opt/data-platform/prod/current/lerobot/data_platform` | `/opt/data-platform/dev/current/lerobot/data_platform` |
| Python 虚拟环境 | `/opt/data-platform/prod/current/.venv` | `/opt/data-platform/dev/current/.venv` |
| 所有已安装版本 | `/opt/data-platform/prod/releases` | `/opt/data-platform/dev/releases` |

`current` 是指向当前发行版本的软链接。以后升级 R3 等版本时，切换这个链接，R2 目录保留用于回退。
**不要直接修改 `current` 或 `releases/R2` 下的文件**：修改应回到 Git 工作区，重新构建新版本。

### H100 上运行的 Agent 代码

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 当前 Agent 入口 | `/opt/data-platform-agent/prod/current` | `/opt/data-platform-agent/dev/current` |
| 当前 R2 的实际目录 | `/opt/data-platform-agent/prod/releases/R2` | `/opt/data-platform-agent/dev/releases/R2` |
| Python 虚拟环境 | `/opt/data-platform-agent/prod/current/.venv` | `/opt/data-platform-agent/dev/current/.venv` |

Agent 应用安装在该虚拟环境的 `lib/python3.10/site-packages/lerobot/data_platform` 中。
它由发行包安装，日常也不在这里直接改代码。完整升级命令会自动更新 H100，无需手动复制源码。

### 共用的发行包仓库

Server A 的 `/var/lib/data-platform-releases` 保存构建好的不可变安装包，例如：

```text
/var/lib/data-platform-releases/R2/release.json     # 版本、提交和安装包校验信息
/var/lib/data-platform-releases/R2/server.tar.gz    # Server 源码安装包
/var/lib/data-platform-releases/R2/approval.json    # 该包的开发验收批准记录
```

同一目录还保存对应 Agent 安装包与构建测试报告。这是供两边安装的包仓库，不是网站的数据目录。

## 2. 数据库在哪里

四个 MySQL 库都在 **Server A 的同一个 MySQL 服务** 中，服务端口为 `3306`。
生产业务连接使用本地 Unix socket `/var/run/mysqld/mysqld.sock`，其他当前连接使用本机 TCP。
两种连接方式到达的是同一个 MySQL 服务。

| 用途 | 生产库名 | 开发库名 |
| --- | --- | --- |
| 网页账号、权限、节点、数据集位置登记、任务等业务记录 | `data_platform` | `data_platform_dev` |
| 操作审计日志 | `data_platform_logs` | `data_platform_logs_dev` |

| 数据库连接账号 | 生产 | 开发 |
| --- | --- | --- |
| 业务库账号 | `data_platform_prod` | `data_platform_dev` |
| 日志库账号 | `data_platform_logs` | `data_platform_logs_dev` |

注意：生产业务**库名是 `data_platform`，连接账号名是 `data_platform_prod`**，两者不同。
这些是程序连接 MySQL 的账号，不是你登录网页所用的账号。开发网页注册或测试的用户保存在开发库中，
不会自动出现在生产网页。

MySQL 的物理数据目录是 Server A 的 `/var/lib/mysql/`，库目录分别为：

```text
/var/lib/mysql/data_platform/
/var/lib/mysql/data_platform_logs/
/var/lib/mysql/data_platform_dev/
/var/lib/mysql/data_platform_logs_dev/
```

这些目录由 MySQL 管理，并不等于每个库都只有一个可复制文件；事务日志等还可能位于共同的数据目录。
**不要用文件管理器修改这些文件，也不要复制一个库目录来同步环境。** 使用 SQL 或现有备份/恢复工具。
数据库连接密码只在受权限保护的环境配置及备份中，不写入本手册。

另外，两套控制台各有独立的 Lifecycle SQLite 文件，记录数据集版本与生命周期状态：

| 生产，Server A | 开发，Server A |
| --- | --- |
| `/srv/data-platform/console/static/lifecycle/lifecycle.db` | `/srv/data-platform-dev/console/static/lifecycle/lifecycle.db` |

旁边的 `-wal`、`-shm` 文件是 SQLite 运行文件。备份由工具通过数据库接口完成，不应只在运行时拷走 `.db`。

## 3. 视频、Parquet 和数据集文件在哪里

数据库主要保存管理记录，真正的视频、Parquet、`meta/info.json` 等在文件系统中。

### H100：目前远端数据集的实际存放位置

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| Agent 允许读取的数据根目录 | `/data1/huggingface` | `/srv/data-platform-dev/datasets` |
| Agent 允许写入的输出父目录 | `/data1/huggingface` | `/srv/data-platform-dev` |
| Viewer 预处理缓存位置 | 由具体数据集位置记录指定 | 已有开发任务使用 `/srv/data-platform-dev/datasets/vis` |

之前复制过来的开发副本，例如 UMI 样本，位于：

```text
H100:/srv/data-platform-dev/datasets/dev-sample-umi
H100:/srv/data-platform-dev/datasets/dev-sample-recording
H100:/srv/data-platform-dev/datasets/dev-sample-20260909_154754
H100:/srv/data-platform-dev/datasets/dev-sample-20260911_172712
H100:/srv/data-platform-dev/datasets/dev-sample-20260909_165406
```

它们是独立副本，不是指向生产源数据的链接。开发环境对这些副本的修改不会写回原生产数据。
这里的 `H100:` 只是注明所在机器，并不是目录名称的一部分。

### Server A：本地数据根、控制台和远端缓存

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 本机配置的数据根目录 | `/home/yuhao.song/Datasets` | `/srv/data-platform-dev/datasets` |
| 控制台输出目录 | `/srv/data-platform/console` | `/srv/data-platform-dev/console` |
| 接收 H100 上传的 Viewer 缓存 | `/srv/data-platform/remote-cache` | `/srv/data-platform-dev/remote-cache` |
| 本地执行器状态 | `/srv/data-platform/console/management` | `/srv/data-platform-dev/console/management` |

这解释了为什么“数据在 H100”但网页仍能在 Server A 显示：Agent 在 H100 读取源数据，
生成缓存并上传到 Server A 的 `remote-cache`，Web 再提供页面、视频和曲线。
这里的缓存不等于源数据全集。

## 4. 配置、运行状态和备份在哪里

### Server A

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| 环境配置 | `/etc/data-platform/prod/server.env` | `/etc/data-platform/dev/server.env` |
| 最近一次部署状态 | `/opt/data-platform/prod/deployment.json` | `/opt/data-platform/dev/deployment.json` |
| 成功升级历史 | `/opt/data-platform/prod/update-history.jsonl` | `/opt/data-platform/dev/update-history.jsonl` |
| 升级前备份 | `/opt/data-platform/prod/backups` | `/opt/data-platform/dev/backups` |
| Python 依赖下载缓存 | `/var/cache/data-platform-uv/prod` | `/var/cache/data-platform-uv/dev` |
| Nginx 配置 | `/etc/nginx/sites-available/data-platform-prod` | `/etc/nginx/sites-available/data-platform-dev` |

额外的首次迁移备份在 `/var/lib/data-platform-deployment/`。配置和备份可能包含凭据，
不要提交进 Git，也不要直接发到聊天或共享文档中。

### H100

| 内容 | 生产 prod | 开发 dev |
| --- | --- | --- |
| Agent 环境配置 | `/etc/data-platform/prod/agent.env` | `/etc/data-platform/dev/agent.env` |
| Agent 身份文件 | `/var/lib/data-platform-agent-prod/agent.json` | `/var/lib/data-platform-agent-dev/agent.json` |
| Agent 执行状态父目录 | `/var/lib/data-platform-agent-prod` | `/var/lib/data-platform-agent-dev` |
| Python 依赖下载缓存 | `/var/cache/data-platform-agent/prod/uv` | `/var/cache/data-platform-agent/dev/uv` |

Agent 身份文件含节点令牌，不应把生产的身份文件复制给开发。旧的非分环境安装目录和配置作为迁移恢复资料
保留，当前服务已经不从旧的 `/opt/data-platform` 工作目录或 `/opt/data-platform-agent/current` 启动。

## 5. 怎么自己查

在 Server A 看当前实际运行版本：

```bash
readlink -f /opt/data-platform/prod/current
readlink -f /opt/data-platform/dev/current
```

进入 H100 查 Agent 和开发样本：

```bash
ssh h100-server
readlink -f /opt/data-platform-agent/prod/current
readlink -f /opt/data-platform-agent/dev/current
ls /srv/data-platform-dev/datasets
exit
```

后续更新命令请看 [当前环境构成与更新手册](data_platform_operations.md)。
记住三个动作的区别：**改工作区代码 → 构建发行包 → 部署到指定环境**。
这三个动作都不会自动把开发数据库或样本复制覆盖到生产。
