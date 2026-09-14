# 开发与生产双环境

使用同一台 Server A、两个 HTTPS 端口、两套独立进程及状态目录。常规流程在开发验收后，生产安装同一份
Server/Agent 包，不重新打包工作区。以下命令是部署工具的操作流程；代码回归测试不表示主机已经完成部署。

## 先理解：分支、环境和数据库有什么区别

**两个 Git 分支不能代替两个环境，也不能代替数据库隔离。** 分支只保存不同版本的代码。
如果两个分支启动的程序都连接生产数据库，开发页面删除或修改记录也会影响生产。

| 名词 | 含义 | 本项目怎么使用 |
| --- | --- | --- |
| Git 分支 | 保存一条代码修改历史 | 可以有开发分支，也可以只用一个主分支；部署工具不强制分支名称 |
| Git 提交 | 给当前确认的源码保存一个可追踪版本 | 构建只打包已提交的代码 |
| 工作区干净 | 没有未提交修改或未跟踪文件 | 确保实际测试、打包的代码就是这个提交；无需删除自己的工作 |
| 运行环境 | 一套正在运行的程序、配置、账号、数据和后台执行器 | dev 和 prod 分别启动，分别使用自己的配置 |
| MySQL 服务 | 管理数据库的程序 | 可以共用一个 MySQL 服务，不需要再买机器或安装第二套 MySQL |
| MySQL 数据库 | 一组独立的数据表 | 开发与生产必须分开；登录账号、任务和审计记录不会串用 |
| 数据集目录 | 保存视频、Parquet、元数据等实际文件 | 开发使用独立的小样本副本，数据库分开后文件也必须分开 |
| 发行包 | 某个提交打包后的 Server 和 Agent 程序 | 如 R0、R1、R2；同一版本构建一次，可分别安装到 dev/prod |

当前程序将业务控制数据和审计日志分库，因此两套环境一共对应四个逻辑 MySQL 库：

| 环境 | 业务控制库：用户、权限、节点、任务 | 日志库：操作审计 |
| --- | --- | --- |
| 生产 | 保留现有生产业务库及名称 | 保留现有生产日志库及名称 |
| 开发 | 新建 `data_platform_dev` | 新建 `data_platform_logs_dev` |

这里“新建两个库”指新建开发的业务库和日志库，不是重建生产。每个库配一个只对它有权限的 MySQL 账号。
MySQL 账号用于程序连接数据库，不是你登录网页的账号；网页管理员是在开发网站首次打开时另行创建的。
控制台本身还有 Lifecycle SQLite 等私有状态，由环境初始化创建在对应目录中，无需手动创建。

代码发布时只升级程序并按需迁移表结构，**不会把开发数据库复制覆盖到生产数据库**。
两套环境可以运行相同的发行包，同时保有各自的数据。

配置文件里的名称也不必猜：

| 配置项 | 需要填写什么 |
| --- | --- |
| `DATA_PLATFORM_ENV` | `dev` 或 `prod`，告诉程序当前是哪套环境 |
| `DATA_PLATFORM_INSTANCE_ID` | UUID，给环境一个固定身份证；首次生成，以后升级不重新生成 |
| `DATA_PLATFORM_DATABASE_URL` | 业务库连接地址，包含 MySQL 账号、密码、主机、端口和库名 |
| `DATA_PLATFORM_LOG_DATABASE_URL` | 日志库连接地址，使用另一个账号和库 |
| `DATA_PLATFORM_BOOTSTRAP_TOKEN` | 首次创建网页管理员的初始化口令，与管理员登录密码不同 |
| `DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN` | 新 Agent 第一次连接 Server 时使用的登记口令 |
| `DATA_PLATFORM_DEPLOY_AGENT_HOST` | Server A 能 SSH 登录的数据节点，例如 SSH 配置中的主机别名 |
| `DATA_PLATFORM_DEPLOY_IDENTITY_FILE` | Server A 上登录该数据节点所用的 SSH 私钥绝对路径 |
| `DATA_PLATFORM_DEPLOY_AGENT_NAMES` | 该环境的 Agent 名称，必须与安装时的 `--name` 一致，例如 `dev-node` |

同一环境的 Server 和 Agent 填相同的 UUID；dev 与 prod 的 UUID 不同。两种初始化口令分别生成，
开发不复用生产口令。教程中的大写占位符是让你替换的内容，不能原样用于真实部署。

## 从旧单环境开始的操作顺序

如果目前运行的是 `data-platform-web.service`，工作目录为 `/opt/data-platform`，按以下顺序首次启用：

1. 在 Server A 整理待发布代码，准备两个空开发数据库和开发配置。
2. 初始化 dev，安装 R0 的开发 Server；在数据节点安装独立 dev Agent。
3. 打开开发入口创建管理员，初始化角色测试用户，用独立样本验证本地及远端任务。
4. 构建 R1，完成开发升级、回退 R0、重新升级 R1 的演练，记录并批准 R1。
5. 安排生产维护窗口，保留原数据库和数据目录，将旧 Server、旧 Agent 接入 prod，安装同一份 R1。
6. 以后选择“只更新开发”“开发验收后更新生产”或“两个环境一起硬升级”。

首次初始化不使用 `--env both --hard`；这个入口需要已经存在的两套环境配置、数据库身份和 Agent。
以下 `R0/R1/R2` 均为示例版本名；一个名称只构建一次，重试已有包使用 `--release`。
除明确标注的数据节点操作外，命令均在 Server A 执行。

| 日常操作 | 命令（完成首次启用后） |
| --- | --- |
| 只构建并更新开发，包括 Agent | `data-platform-update-all --env dev --version R2` |
| 开发重装已有包 | `data-platform-update-all --env dev --release R2` |
| 记录完成的开发验收 | `data-platform-release approve --env dev --release R2 --evidence /PATH/TO/evidence.json` |
| 只更新生产，包括 Agent | `data-platform-update-all --env prod --release R2` |
| 同一份新包直接更新开发和生产 | `data-platform-update-all --env both --hard --version R2` |
| 两个环境重用已有包硬升级 | `data-platform-update-all --env both --hard --release R2` |

生产单独更新要求该发行包已在开发批准；一起硬升级跳过人工批准，保留隔离、备份和程序校验。

## 布局与配置

| 项目 | prod | dev |
| --- | --- | --- |
| 浏览器入口 | `https://SERVER_IP` | `https://SERVER_IP:8443` |
| Web 内部地址 | `127.0.0.1:9091` | `127.0.0.1:9092` |
| 配置 | `/etc/data-platform/prod/server.env` | `/etc/data-platform/dev/server.env` |
| Server 版本 | `/opt/data-platform/prod/releases/` | `/opt/data-platform/dev/releases/` |
| Web / 本地执行器 | `data-platform-web@prod` / `data-platform-local-worker@prod` | `data-platform-web@dev` / `data-platform-local-worker@dev` |
| OS 账号 | 保留 `data-platform` | `data-platform-dev` |
| 数据和控制台目录 | 保留生产现有配置 | `/srv/data-platform-dev/` 下独立目录 |
| 控制面 / 日志 MySQL | 保留现有库和专用账号 | `data_platform_dev` / `data_platform_logs_dev`，各自专用账号 |
| Agent 配置（数据节点） | `/etc/data-platform/prod/agent.env` | `/etc/data-platform/dev/agent.env` |
| Agent 程序（数据节点） | `/opt/data-platform-agent/prod/current` | `/opt/data-platform-agent/dev/current` |
| Agent 私有状态（数据节点） | `/var/lib/data-platform-agent-prod` | `/var/lib/data-platform-agent-dev` |
| Agent 服务（数据节点） | `data-platform-agent@prod` | `data-platform-agent@dev` |

分别使用 `deploy/data-platform/server.prod.env.example` 和 `server.dev.env.example`。
每个环境生成独立 `DATA_PLATFORM_INSTANCE_ID` UUID；同一环境的 Server 和所有 Agent 使用同一 UUID，
但每个 Agent 仍有自己的节点令牌。bootstrap token、enrollment token、数据库密码、账号会话不跨环境复制。
正式配置留在 `/etc`，不要提交真实凭据。

开发 MySQL 库名必须以 `_dev` 结尾，初始化时必须为空。两个环境共用 MySQL 实例时，数据库账号只能访问
其所属的那个库；控制面和日志也使用不同账号。开发账号不能授予生产库、`*.*` 或管理员权限。
初始化会为数据库和目录绑定环境身份；普通启动只验证身份，不自动执行命名环境的 schema 迁移。
错误的数据库、实例 ID、目录标记或目录重叠会使启动失败。

开发 Web 和本地执行器共享 `data-platform-dev.slice`，合计上限 4 核、8 GiB。
开发 Agent 上限 2 核、4 GiB，默认单任务、GPU 禁用。它只能使用开发数据根目录；不要给开发 OS 账号
生产数据组的权限。源数据处理及缓存均使用副本，不能使用指回生产的软链接或硬链接。
MySQL、磁盘及主机故障仍然共享；这是同机逻辑隔离，不是独立机器的故障隔离。

同 IP 的 Cookie 不按端口隔离。Nginx 对每个入口只转发对应的会话 Cookie，应用只设置本环境 Cookie，
所有浏览器写请求校验包含端口的 Origin 和 CSRF。页面自动携带 CSRF；自行使用 Cookie 调用 API 的客户端
需要先读取 `/api/auth/status` 的 `csrf_token`，再发送 `X-Data-Platform-CSRF` 和对应 Origin。
Agent 使用自己的 bearer token，不使用浏览器 Cookie。不同端口仍不是互不信任服务的完整浏览器隔离边界。

Agent 使用内部 CA 或平台自签证书时，在对应的 `/etc/data-platform/ENV/agent.env` 中设置
`DATA_PLATFORM_AGENT_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`（前提是系统证书库已信任平台证书）。
安装检查和 Agent 运行均使用该证书库，并保持 TLS 校验。更新现有 Agent 配置不需要重新注册节点。

## 首次建立开发环境

先保留当前工作区修改，将准备发布的代码整理为明确的 Git 提交。候选构建拒绝未提交或未跟踪的工作，
不会自动提交、stash 或覆盖源代码。需要现有 `.venv` 中的 server/test 依赖、`uv`、MySQL 客户端和 systemd。

先在 Server A 的源码目录查看待发布修改，选择本次需要的文件提交；不要把运行目录、数据库和凭据加入提交。

```bash
cd /home/yuhao.song/Codes/data_platform
git status --short
# uv.lock 固定 Server/Agent 的依赖版本，必须和 pyproject.toml 一起纳入 Git。
git ls-files uv.lock
# 上一条若无输出，但本地已有 uv.lock，检查后用 git add uv.lock 将它加入暂存。
# 在编辑器的 Git/源代码管理面板逐个检查并暂存本次需要发布的源码和文档。
# 也可以用 git add 后跟明确的文件路径来暂存。不要将配置密码或运行产物加入暂存。
git diff --cached --stat
git commit -m "feat: add isolated dev and prod deployment workflows"
# 确认下面命令无输出后，才进入后面的发行包构建步骤。
git status --porcelain
```

`git commit` 保存本地代码版本，不会自动部署网站，也不会自动推送到远端。
如果最后仍有输出，先处理剩余修改或未跟踪文件：需要发布的继续检查并提交；个人运行产物按项目规则忽略
或保留在源码目录外。不要通过清空工作区来凑出“干净”状态。

**准备数据库和配置。** 以下 SQL 在 MySQL 管理会话中由数据库管理员执行，将主机和两个密码占位符
替换为真实值；`SERVER_A_DB_CLIENT_HOST` 是 MySQL 看到的 Server A 来源主机，本机连接按实际 localhost/IP 配置。
这里创建的是开发库，生产保留现有库。

```sql
CREATE DATABASE data_platform_dev CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE data_platform_logs_dev CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'data_platform_dev'@'SERVER_A_DB_CLIENT_HOST' IDENTIFIED BY 'DEV_CONTROL_PASSWORD';
CREATE USER 'data_platform_logs_dev'@'SERVER_A_DB_CLIENT_HOST' IDENTIFIED BY 'DEV_LOG_PASSWORD';
GRANT ALL PRIVILEGES ON data_platform_dev.* TO 'data_platform_dev'@'SERVER_A_DB_CLIENT_HOST';
GRANT ALL PRIVILEGES ON data_platform_logs_dev.* TO 'data_platform_logs_dev'@'SERVER_A_DB_CLIENT_HOST';
```

如果开发库使用 **Server A 本机已有的 MySQL**，可以按以下具体顺序操作：

1. 在自己的终端运行四次 `openssl rand -hex 32`，分别保存为开发业务库密码、开发日志库密码、
   管理员初始化口令、Agent 登记口令。十六进制密码可直接放入下面的 URL，不需要额外编码。
2. 执行 `sudo mysql` 进入 MySQL 管理会话；若本机管理员采用密码登录，则使用 `mysql -u root -p`。
   这里需要的是 MySQL 管理权限，应用账号不能创建新账号和数据库。
3. 将上方 SQL 中的 `SERVER_A_DB_CLIENT_HOST` 全部替换为 `127.0.0.1`，两个密码占位符替换为
   刚生成的对应值，然后在 MySQL 提示符下执行。SQL 不要直接粘贴到 bash 提示符。
4. 执行 `EXIT;` 返回 shell，再分别测试连接，`-p` 会交互询问密码：

   ```bash
   mysql --protocol=TCP -h 127.0.0.1 -u data_platform_dev -p data_platform_dev -e 'SELECT DATABASE();'
   mysql --protocol=TCP -h 127.0.0.1 -u data_platform_logs_dev -p data_platform_logs_dev -e 'SELECT DATABASE();'
   ```

   应分别显示 `data_platform_dev` 和 `data_platform_logs_dev`。若提示已存在，先确认库的用途和是否为空，
   不要删除重建。远端 MySQL 则由对应管理员执行建库，并使用实际主机和访问授权。

在 Server A 准备配置，`cp -n` 保留已经存在的文件：

```bash
sudo install -d -m 0755 /etc/data-platform/dev
sudo cp -n deploy/data-platform/server.dev.env.example /etc/data-platform/dev/server.env
sudo chmod 0600 /etc/data-platform/dev/server.env
python3 -c 'import uuid; print(uuid.uuid4())'
sudoedit /etc/data-platform/dev/server.env
```

填写刚生成的 dev UUID，替换两个数据库 URL 的主机及密码（URL 内的密码需要编码），为 bootstrap 与
Agent enrollment 分别设置独立随机值。可以在自己的终端执行 `openssl rand -hex 32` 每次生成一个值。
填写 SSH 主机、私钥绝对路径与开发节点名称；这几个配置用于后续自动升级，节点名称必须与安装 Agent 时
的 `--name` 相同。保留开发路径 `/srv/data-platform-dev/...`、本地端口 `9092` 和角色切换开关 `1`。
不要把配置里的秘密复制到源码或验收文件。

本机 MySQL 的关键配置填写示例（在复制的模板里修改同名行，不要重复追加）：

```dotenv
DATA_PLATFORM_ENV=dev
DATA_PLATFORM_INSTANCE_ID=填写刚生成的开发UUID
DATA_PLATFORM_DATABASE_URL=mysql+pymysql://data_platform_dev:开发业务库密码@127.0.0.1:3306/data_platform_dev?charset=utf8mb4
DATA_PLATFORM_LOG_DATABASE_URL=mysql+pymysql://data_platform_logs_dev:开发日志库密码@127.0.0.1:3306/data_platform_logs_dev?charset=utf8mb4
DATA_PLATFORM_BOOTSTRAP_TOKEN=管理员初始化口令
DATA_PLATFORM_AGENT_ENROLLMENT_TOKEN=Agent登记口令
DATA_PLATFORM_DEPLOY_AGENT_HOST=你的数据节点SSH别名
DATA_PLATFORM_DEPLOY_IDENTITY_FILE=/home/你的用户名/.ssh/实际私钥文件
DATA_PLATFORM_DEPLOY_AGENT_NAMES=dev-node
```

上述中文内容全部替换为真实值，模板其余默认项保留。`sudoedit` 打开的是受保护的配置文件，保存后
程序才会读到它；该文件位于 `/etc`，不属于 Git 源码。SSH 私钥应使用已经能登录数据节点的那把，
而不是随便填写一个新文件名。正式自动更新还要求该 SSH 用户在数据节点有已配置的非交互 sudo 权限。

1. 确认上述两个开发库、专用账号及 `/etc/data-platform/dev/server.env` 已准备完成，
   `DATA_PLATFORM_LOCAL_SERVER_URL` 为 `http://127.0.0.1:9092`。
2. 从源码目录初始化数据库、目录和账号，再生成开发 Nginx 配置：

   ```bash
   bash deploy/data-platform/environment.sh init --env dev
   sudo install -d -o data-platform-dev -g data-platform-dev -m 0750 /srv/data-platform-dev/datasets
   bash deploy/data-platform/environment.sh network --env dev
   ```

   开发网络配置使用已有 TLS 证书，只 reload Nginx。若存在标准 H100 反向隧道，会创建独立开发隧道：
   H100 `127.0.0.1:9444` → Server A `127.0.0.1:8443`，生产 `9443 → 443` 保留。
   非标准隧道必须先按实际网络配置；工具不会猜测新的转发目标。
3. 构建第一份候选，并先安装开发 Server：

   ```bash
   bash deploy/data-platform/release.sh build --env dev --release R0 --source "$PWD" &&
   bash deploy/data-platform/update-server.sh --env dev --release R0 &&
   sudo bash deploy/data-platform/install-commands.sh
   ```

   `&&` 表示前一步成功才执行下一步。若构建报缺少 `uv.lock`，不要继续安装：本地存在锁文件不等于
   Git 提交里有它。确认 `git ls-files uv.lock` 有输出且提交后的工作区干净，再重新构建。
   保持 `--frozen`，不要通过删除该参数绕过依赖锁定。若本地也没有锁文件，先执行 `uv lock` 生成、
   检查并提交；已有锁文件可用 `uv lock --check --offline` 检查是否与项目配置一致。

   Server-only 安装完成后保持维护状态；`/healthz` 和 Agent 接口仍可访问。
   发行库保存在 `/var/lib/data-platform-releases/R0/`；Agent 压缩包及校验文件同时保留在源码的 `dist/agent/`。
4. 数据节点创建 `/srv/data-platform-dev/datasets` 和 `/srv/data-platform-dev/outputs`，
   准备独立测试副本。传输 R0 的 Agent 包和 `.sha256`，校验并解压后安装：

   ```bash
   # 以下在数据节点执行；从 Server A 的 dist/agent/ 传来与 R0 对应的包和校验文件。
   sudo install -d -m 0755 /srv/data-platform-dev/datasets /srv/data-platform-dev/outputs
   # 将 ARCHIVE.tar.gz 替换为实际压缩包文件名。
   sha256sum --check ARCHIVE.tar.gz.sha256
   tar -xzf ARCHIVE.tar.gz
   cd ARCHIVE
   sudo ./install.sh --env dev --instance-id DEV_INSTANCE_UUID \
     --server-url https://SERVER_A_ADDRESS:8443 --name dev-node \
     --allowed-root /srv/data-platform-dev/datasets \
     --writable-root /srv/data-platform-dev
   # 安装会创建独立服务账号；下面只授权开发目录。
   sudo chown data-platform-agent-dev:data-platform-agent-dev /srv/data-platform-dev/datasets /srv/data-platform-dev/outputs
   sudo systemctl is-active data-platform-agent@dev.service
   ```

   使用反向隧道时，Server URL 改为节点实际可达且证书验证通过的隧道地址。
   首次安装交互读取 enrollment token；不要将 token 放在命令行。安装后将开发数据父目录的写权限授予
   `data-platform-agent-dev`，不要修改生产目录权限。两个环境的 `agent.json` 不可互相复制。
5. 确认开发 Agent 在线后，在 Server A 解除开发维护：

   ```bash
   data-platform-environment maintenance-off --env dev
   ```

   浏览器访问 `https://SERVER_IP:8443/login`，用开发配置中的 `DATA_PLATFORM_BOOTSTRAP_TOKEN`
   创建独立开发管理员。**完成管理员创建后**，再在 Server A 初始化测试用户：

   ```bash
   data-platform-environment seed-dev-users --env dev
   ```

   Server A 本地样本也可通过以下命令复制，输出必须在开发状态根目录内且尚不存在：

   ```bash
   data-platform-environment copy-sample --env dev --source /PATH/TO/SMALL_DATASET \
     --destination /srv/data-platform-dev/datasets/sample
   ```

   这是完整数据集复制，不是 episode 抽样；请先选择小样本数据集。它复制文件内容，不创建硬链接。

6. 开发管理员登录后，通过顶部角色切换测试管理员、操作员 A/B、只读用户。用独立样本完成至少一个
   本地任务、远端 Viewer 和远端预处理。接着按日常更新命令安装 R1，演练
   `data-platform-release rollback --env dev --release R0`，再用 `--release R1` 更新回来。
   实际完成所有场景后填写下文验收 JSON 并批准 R1，然后进入生产首次接入。

## 真实角色体验

开发页面顶部显示环境、版本、当前身份。仅正常登录的开发管理员可切换：管理员、操作员 A、操作员 B、只读用户。
切换通过 `/api/dev/role-session` 保存服务端会话映射，不修改管理员账号角色。
普通业务授权、任务提交者和配额都使用当前测试用户，审计保留原始管理员。

测试用户由 `seed-dev-users` 幂等创建，密码随机且不输出；使用管理员切换入口进入它们。
普通 operator/viewer 不能使用该入口。生产不注册切换接口，且配置开启角色切换时拒绝启动。
切换作用于当前浏览器的整个开发会话，各标签同步刷新；生产登录状态独立。

验证 viewer 无法写入、operator 只能控制自己的任务、operator A/B 的归属区别以及返回管理员。
已提交任务的身份不受之后切换影响。源数据原地修改开关默认关闭，测试身份不会绕过该开关。

## 日常开发、验收和生产发布

单节点可配置 `DATA_PLATFORM_DEPLOY_AGENT_HOST`、`DATA_PLATFORM_DEPLOY_IDENTITY_FILE` 和
`DATA_PLATFORM_DEPLOY_AGENT_NAMES`。多节点使用 `DATA_PLATFORM_DEPLOY_AGENT_TARGETS` 的 JSON 数组：

```text
DATA_PLATFORM_DEPLOY_AGENT_TARGETS='[{"host":"SSH_ALIAS","identity_file":"/home/DEPLOY_USER/.ssh/KEY","node_names":["dev-node"]}]'
```

SSH 使用发起命令的用户及其私钥；工具在本机通过 sudo 执行安装，远端非 root 用户需要已配置的非交互 sudo。
升级前核对每个远端配置的环境、UUID 和节点名，禁止将开发发布到生产 Agent 配置。

```bash
# 在已提交且干净的源码目录构建并更新开发环境。
data-platform-update-all --env dev --version R1

# 自动回归之外，记录人工验收；evidence.json 不要包含凭据或真实数据。
data-platform-release approve --env dev --release R1 --evidence /PATH/TO/evidence.json

# 生产只接受这份已验收的不可变包，不重新构建。
data-platform-update-all --env prod --release R1
```

验收文件结构：

```json
{
  "release": "R1",
  "role_permissions": true,
  "environment_isolation": true,
  "local_job": true,
  "remote_viewer": true,
  "remote_preprocess": true,
  "rollback_drill": true
}
```

这些字段是管理员完成操作后的确认，不能在未测试时直接填 true。批准时还会检查开发 Server 的环境、
UUID、版本以及配置节点的最新心跳和协议。构建时已执行环境、发布、控制面、任务、Agent 和管理页面回归。
首次演练可先安装 R0，再安装 R1、回退 R0、重新安装 R1；开发回退不要求生产批准记录。

每次更新先完成依赖准备，再启用维护并等待已有任务结束，默认等待 300 秒；`interrupted` 任务需先确认停止。
维护期间禁止新的业务写入和任务领取，已有任务允许继续上报完成。随后停止本环境服务、备份、显式迁移、
切换 Server 和本地执行器，再逐个安装 Agent。只有预期版本的心跳验证完成后才解除维护。
原有 `DATA_PLATFORM_PAUSE_CLAIMS` 设置保留，因此配置仍为 1 时任务领取仍暂停。

`--index-url https://MIRROR/simple` 传给 Server 的 uv 同步，保持 `--frozen`，先离线再在线。
Agent 依赖安装继续使用其自身包源配置。安装到新版本目录，失败不会覆盖正在运行的代码目录。
成功后删除本次远端上传目录，本地包、已安装版本和按环境区分的历史记录保留；失败保留远端现场。

## 开发和生产一起硬升级

保留直接更新两个环境的入口。两套环境已初始化、生产已完成首次接入后，在干净的源码目录执行：

```bash
# 构建一次，依次更新 dev 和 prod，包括各自的 Server、本地执行器和远端 Agent。
data-platform-update-all --env both --hard --version R2

# 重用已有发行包（例如修复部署问题后重试），不重新构建。
data-platform-update-all --env both --hard --release R2
```

源码入口是 `bash deploy/data-platform/update-all.sh --env both --hard --version R2`。
已安装旧版程序的主机，首次硬升级从新源码入口执行；两套服务升级完成后，再从新源码执行
`sudo bash deploy/data-platform/install-commands.sh` 更新包装脚本，即可使用上述短命令。
可追加 `--index-url https://MIRROR/simple`，含义与常规更新相同。

`--hard` 跳过人工开发验收要求，不生成或覆盖 `approval.json`；自动构建测试、制品哈希、环境身份、
任务排空、备份、健康检查和 Agent 心跳仍执行，数据库和数据目录仍各自独立。
构建仍要求干净且已提交的 Git 工作区。普通 `--env prod --release R2` 继续要求开发批准记录。

工具先校验两套环境和 Agent 的配置，然后完整更新 dev，再完整更新 prod；每套内部先 Server A 后 Agent。
这是一个命令顺序完成两次升级，不是跨环境原子切换：dev 失败时不启动 prod 更新；prod 失败时 dev 保留
已更新版本，prod 按失败阶段保持原服务或维护状态。排查后使用相同 `--release` 重试，或按各自备份恢复。
两套环境的 `deployment.json` 和成功更新历史均记录 `mode=hard`、发起用户以及是否跳过验收。
该入口始终包含 Agent，不支持与 `--server-only`、`--restore-backup` 或 `rollback` 混用。

## 首次将现有部署接入 prod

开发验收完成后再操作。准备 `/etc/data-platform/prod/server.env`，保留原控制库、日志库、输出目录和
远端缓存路径，添加独立 prod UUID、环境配置及生产 Agent 连接信息。

在 Server A 从现有配置复制，而不是直接用生产示例文件替换数据库连接：

```bash
sudo install -d -m 0755 /etc/data-platform/prod
sudo cp -n /etc/data-platform/server.env /etc/data-platform/prod/server.env
sudo chmod 0600 /etc/data-platform/prod/server.env
python3 -c 'import uuid; print(uuid.uuid4())'
sudoedit /etc/data-platform/prod/server.env
```

对照 `server.prod.env.example` 添加或修改以下项，每个键只出现一次：

- `DATA_PLATFORM_ENV=prod`、新的 prod `DATA_PLATFORM_INSTANCE_ID`（不同于 dev）。
- `DATA_PLATFORM_STATE_ROOT` 设置为现有 console 与 remote-cache 的共同专用父目录，通常是 `/srv/data-platform`。
- `DATA_PLATFORM_ENABLE_DEV_ROLE_SWITCH=0`、`DATA_PLATFORM_LOCAL_SERVER_URL=http://127.0.0.1:9091`。
- `DATA_PLATFORM_DEPLOY_AGENT_HOST`、`DATA_PLATFORM_DEPLOY_IDENTITY_FILE`、`DATA_PLATFORM_DEPLOY_AGENT_NAMES`
  指向现有生产节点，名称保持与旧 Agent 一致；多节点用上文所述 JSON 配置。
- 原有两条数据库 URL、输出目录和远端缓存目录原样保留，控制库与日志库必须使用各自专用账号。
  生产服务配置、令牌和数据保留现有生产值，不复制开发配置。

以下操作会进入生产维护窗口；先确认 R1 已批准且开发环境运行正常，再执行：

```bash
# 建立入口维护状态，暂停旧服务领取；有未完成任务时退出并保持维护，等待结束后重跑。
bash deploy/data-platform/environment.sh adopt-legacy --env prod

# 安装已经通过开发验收的 Server；此时继续维护。
bash deploy/data-platform/update-server.sh --env prod --release R1
```

接着在数据节点用 R1 包执行一次：

```bash
sudo ./install.sh --env prod --instance-id PROD_INSTANCE_UUID --adopt-legacy
```

它检查生产 Server 正在维护，再停止旧 Agent，复制私有状态，保留节点名称和令牌，改用 prod 服务与目录。
原状态保留供恢复，未重新注册为另一个节点。首次迁移时，旧服务、MySQL 备份及配置副本需保留到验收完成。
然后执行 `data-platform-update-all --env prod --release R1` 完成统一校验和恢复入口。

## 查看、重启与故障恢复

```bash
data-platform-release status --env prod
data-platform-environment status --env prod
data-platform-restart --env dev
data-platform-release rollback --env prod --release PREVIOUS
```

单环境重启只处理对应 Web 与本地执行器，不重启 MySQL、另一环境或共享 Nginx。
Server-only 更新通过 `data-platform-update --env ... --release ...` 执行，完成后保持维护，需匹配 Agent 验证后恢复。

部署状态和备份路径记录在 `/opt/data-platform/ENV/deployment.json`，成功历史保存在同目录的
`update-history.jsonl`。备份包含两个 MySQL 库、Lifecycle SQLite 及工件、数据集注册表、私有任务状态和配置；
SQLite 使用备份 API。大型可重建视频缓存不重复备份，源数据不受版本切换影响。

兼容的 schema 可直接回退程序。跨不兼容 schema 的恢复必须显式指定同环境备份：

```bash
data-platform-release rollback --env prod --release PREVIOUS --restore-backup /PATH/TO/BACKUP
```

此命令会覆盖所选环境的数据库及所列私有状态，丢弃备份之后的这些记录；先确认恢复点。
工具校验环境和备份校验和，先备份当前状态，再恢复；不接受包含平台之外表的数据库。
配置副本保留供检查，避免自动将已轮换的数据库密码改回旧值。迁移或恢复失败时保持维护，不自动恢复流量。
旧的单环境安装恢复还需使用首次迁移保留的旧配置和服务，不能把新环境数据库直接交给未配置环境的程序。

上线验收必须额外在真实 MySQL、systemd、Nginx、SSH 隧道和数据节点完成。重点检查开发账号不能访问生产库、
两个环境同时登录、独立执行器资源限制、Viewer/预处理产物归属及一次完整回退演练。
