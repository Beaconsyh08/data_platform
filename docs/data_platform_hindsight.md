# 后台任务经验归纳

Hindsight 是可关闭的后台增强，不替换显式记忆、已有 mem0 服务或任何执行/审批机制。本仓库当前的
显式记忆实现是 MySQL `dp_ai_memory`；独立调研中的 mem0 基线不属于平台运行时，本次不迁移它。
新增代码的安装状态和在线验收保存在发行记录中，源码测试不表示已经部署。

## 数据与权限契约

- 只使用本人明确确认的助手 Pipeline 的终态 followup。沿用用户、数据集身份、输入/输出权限、
  Run revision、Job/Attempt 最终确认检查；每个数据集最多 20 个不同 Run、30 天内的有界摘要。
  这阶段归纳的是终态摘要和算子信息，不读取原始数据、图片、完整日志或未审核的 Quality 报告正文。
- 至少两个独立 Run 才允许生成跨任务经验。新控制表 `dp_ai_experiences` 保存可重建缓存和后台租约，
  通过现有 additive migration 建表；不修改已有记忆格式或 Agent 协议。
- 每个后台尝试使用独立银行，作用域包含环境 instance、用户和位置；结束后删除银行。若删除失败，
  不发布缓存，并在下次工作前重试清理。崩溃后的银行 ID 留在持久表中供接管清理。
- observation 的全部来源边必须能解析到已授权文档和精确摘要哈希。重复事实不能冒充多个任务。
  缺失/循环/失效引用、未知任务 UUID、来源变化均拒绝。随后另一次 Qwen 调用逐项检查历史陈述，
  返回所有引用的原文摘录；代码精确验证摘录，没有完整匹配就不缓存。
- 模型的语义核验仍有误判可能，因此经验始终是历史背景，不是现行规则、参数默认值、完成证明或执行授权。
  用户请求和当前显式记忆优先，现有算子校验、权限和确认保持有效。

## 新规则与并发

任何显式记忆或设置修改，都在原事务内递增经验 epoch、清空缓存，并使在途发布者失效。
当前 revision 必须与归纳 revision 一致。为防止旧任务再次生成过时建议，只接受**原始发起请求和完成均晚于
最近一次记忆/设置修改**的任务；同秒记录也排除。使用原始 AssistantTurn 的 BIGINT 时间，并校验
Run 的幂等键确实属于该请求；后来的复用请求不能替旧 Run 提供新时间。缺少原始请求时不归纳。
既有 PipelineRun 的 MySQL FLOAT 时间精度不足，不作为此门禁的依据。迟到完成的旧 Run 不会跨过此边界。这是保守策略：
即使只改沟通习惯，也要有两个新任务后才能重新形成经验。

前台每次读取都重新检查当前来源、权限、Run revision、删除与 30 天保留期。每次模型调用前后再检查
记忆 revision 与来源摘要；最终回答发布也在原调度事务内复核，防止检查后、发布前的规则变更。中途变化终止旧回答并要求新请求。LangGraph 恢复仍校验完整模型请求摘要，
上下文变化时不重放旧响应。移除了模型上下文中每秒变化的 `observed_at`，避免仅跨秒就错误拒绝恢复；
实际数据摘要和身份变化仍使恢复失败。

## 时延和故障

`data-platform-experience@<env>.service` 是独立进程。前台没有 Hindsight、embedding、reflect 或额外模型调用，
只读本地 SQL 缓存；服务停机不影响已有显式记忆及普通 Agent 请求。未生成合格归纳时返回空背景，不降低来源要求。
后台每次最多 240 秒、租约 300 秒、持久失败退避最高一小时，HTTP 请求最多 15 秒，语义核验最多 30 秒。
每次成功最多缓存 4 条、注入完整 JSON 最多 6000 UTF-8 字节。没有新增自动执行工具或外部消息。

交互 Qwen 单次上限从 15 秒改为 30 秒，仍受整轮持久 75 秒和 8 次调用限制。未知结果不自动重发。
运行中的后台操作停止接收新工作后有界退出；服务停止上限 260 秒，独立于交互 Runner 的 100 秒。

## 安装和回退

Python 3.10 平台通过 HTTP 访问 Hindsight，供应商 SDK/依赖单独放在 Python 3.11 环境，避免与 mem0 的
protobuf 依赖冲突。`deploy/data-platform/hindsight-requirements.lock` 固定本次验证的 Hindsight 1.1.0 依赖。
安装器要求提供已核验、含 pgvector 的 PostgreSQL 二进制目录，复制为哈希命名的独立安装；不安装或重启
机器上的其他数据库。本机调研使用 PostgreSQL 18.1 + pgvector 0.8.5，升级该独立发行包前须重新验证。

PostgreSQL 只接受私有 Unix socket 上的 peer 认证，没有 TCP 监听；数据分别属于 dev/prod 的服务账号，
放在 `/var/lib/data-platform-hindsight/<env>/postgres`，socket 位于 `/run/data-platform-hindsight-<env>`。
Hindsight API 只监听环回 19911(dev)/19912(prod)，启用 API key tenant 扩展，不加入公开 Nginx 路由。
配置在 `/etc/data-platform/<env>/hindsight.env`，权限 0640；沿用该环境已配置的 Qwen key，API key 随机生成。
不要把凭据放入命令行或文档。

安装器需要 root。先核验 PostgreSQL 发行目录和当前发布版本，再执行开发环境安装：

```bash
# SOURCE 为已提交、已验证的发行 worktree；PG_DIST 为核验过的 PostgreSQL/pgvector 二进制目录。
sudo "$SOURCE/.venv/bin/python" "$SOURCE/deploy/data-platform/install-hindsight.py" \
  --env dev --postgres-root "$PG_DIST" --uv "$(command -v uv)"
# 随后按既有 update-all.sh 流程安装一个新的、已核验未占用的平台版本到 dev。
```

安装器校验已认证 API，备份原 server.env 后添加 `DATA_PLATFORM_HINDSIGHT_URL`、
`DATA_PLATFORM_HINDSIGHT_API_KEY`、`DATA_PLATFORM_AI_EXPERIENCE_ENABLED=1`，不自行部署平台。
平台发行流程安装/重启经验 worker。dev 实际验收通过后，才对 prod 重复侧车安装并提升同一个平台包。
同步构建和发布独立 CLI，完成 TLS 登录、`/memory`、`/updates`、`--web` 和授权 `/open`，最后推进 latest。

关闭增强：将目标环境 `DATA_PLATFORM_AI_EXPERIENCE_ENABLED=0`，按既有环境配置流程重启 Web/Runner；
让经验 worker 有界完成清理后停止。已有显式记忆和 mem0 不受影响。回退旧平台时安装器停用经验 worker，
保留新增 SQL 表；如侧车不再使用，完成银行清理后再停 API/数据库。不要删除平台控制数据库或旧执行器。

## 验收

```bash
.venv/bin/python -m pytest -q tests/datasets/test_ai_experience.py tests/datasets/test_ai_memory_followups.py
.venv/bin/python experiments/assistant_langgraph/run_acceptance.py --runtime
# 真实模型测试要求环境变量已配置，只生成独立合成数据，最多 20 次请求；会消耗模型配额。
.venv/bin/python experiments/hindsight/validate_live.py --output-dir /tmp/hindsight-acceptance-UNIQUE --samples 20
```

真实模型脚本调用实际异步 Runner；历史完成由仓库 fixture 模拟，不是真实 Agent 计算验收。
安装后的 dev 门禁必须额外覆盖：新助手确认产生真实终态跟进、后台归纳、来源链接/引用、编辑规则立即失效、
来源删除/重试/权限撤销、Hindsight 停机时普通提问和待确认操作卡正常，以及现有 Web/CLI 权限与确认流程。
连续 20 次代表性请求应无超时/服务错误，所有操作卡参数正确、无意外 Job/Pipeline；缓存读取最长小于 1 秒。
需验证四个并发请求及故障降级，记录精确 Server/Agent/CLI 版本、TLS、心跳和维护状态，才能提升生产。

2026-10-22 的隔离实测：20/20 成功，0 超时，中位 3.32 秒、最长 7.05 秒；10/10 操作卡参数正确，
10/10 解释包含两个来源 Run。SQL 缓存读取中位约 8.4 毫秒、最长约 11.2 毫秒，规则编辑立即隐藏旧经验，
没有新增 Job/Pipeline。两批小样本归纳未产生通过来源门禁的候选，返回空缓存，其中一批只有两个
分别关联单任务的 observation；调整按算子/结果合并的归纳指令后，直接 Qwen + 原生 PostgreSQL
实测生成了双来源经验，后台耗时 26.26 秒，4 次连续请求全部通过。关闭该隔离 API 后，4 个不同账户
并发请求全部通过、参数正确，整批 9.30 秒；规则编辑立即隐藏旧经验，仍没有新增 Job/Pipeline。
这说明没有合格结果时会降级，不代表每批任务都能形成有用经验；这些数字不能替代安装后的 dev/生产验收。
