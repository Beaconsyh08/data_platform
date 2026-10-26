# LangGraph 第一阶段验收

状态：2026-10-22，隔离实验已实现并通过下述验收；**未接入 Server 请求路由、未迁移平台数据库、未部署**。
此目录保留第一阶段实验记录；后续正式异步实现已位于 `ai_tools/runtime*.py` 和 `runner.py`，见
[当前实施记录](../../docs/data_platform_assistant_langgraph.md)。不要将下述实验局限当作正式实现的当前状态。
源码基线为 `68d213ea`，包含 V0.3.5 助手代码及后续 CLI 修复。这里的实验模块不会被平台启动导入，
依赖也未加入 Server/Agent 的安装 extra。

## 实现

- `repository.py`：MySQL 执行领取、租约 epoch、续租、受保护的结果发布，以及模型调用预算与响应回执。
- `checkpointer.py`：复用社区 saver 3.0.0 的检查点格式，在同一事务内锁定 execution 并验证租约；
  checkpoint、blob、pending writes 均受保护。事务结束前再次验证过期，失效则回滚。
  表名加 `dp_ai_` 前缀，限制当前 execution 的 root graph，禁用 runner 的建表和删除能力。
- `s3_graph.py`：上下文和个人偏好加载 → Qwen 协议节点 → 受控工具 → S3 操作卡片。
  通过原有 catalog、`actions.prepare` 和业务 preview 校验，确认继续使用原 `actions.confirm`。
- `acceptance_worker.py`：无模型、无业务计算的跨进程暂停/恢复验收工具。
- `run_acceptance.py`：当前用户启动临时 MySQL，关闭 TCP 与 MySQL X，仅使用私有 Unix socket；
  完成后只终止自己创建的进程并删除自己的临时目录。

社区 saver 的基础事务不会校验平台的执行租约，因此不直接作为多实例运行的安全边界。
本适配器使用它的私有 `_cursor` 扩展点，升级依赖必须重新验收。
[社区实现](https://github.com/tjni/langgraph-checkpoint-mysql)要求 MySQL 8.0.19+；
[LangGraph 持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)区分流程检查点与长期记忆。

## 复现

使用普通用户运行，要求已安装 MySQL 8 的 `mysqld`。启动器不安装系统服务，不读取系统 MySQL 配置，
不需要 sudo，不连接现有平台数据库。测试只在自己创建的随机 `assistant_graph_test_*` 数据库内建表。
模型响应全部由确定性 test double 提供，不需要 Qwen 密钥；启动器清除提供商和平台配置变量并关闭 tracing。

在隔离 checkout 的 Python 3.10+ 开发虚拟环境中安装实验锁定依赖：

```bash
uv pip install --python .venv/bin/python -r experiments/assistant_langgraph/requirements.lock
.venv/bin/python -m experiments.assistant_langgraph.run_acceptance --storage-only
```

完整 S3 验收还需要本项目已有开发依赖（与 `test_ai_assistant_actions.py` 相同）：

```bash
.venv/bin/python -m experiments.assistant_langgraph.run_acceptance
```

这不是部署命令。不要向线上虚拟环境安装实验依赖，也不要对平台数据库调用这里的 `setup()`。
实验表只覆盖测试需要的字段，尚未具备生产 owner/request 归属和原子接入契约。

## 已验证的结果

环境：MySQL 8.0.46；LangGraph 1.2.16、checkpoint 4.3.0、MySQL saver 3.0.0；
Pydantic 2.13.4、SQLAlchemy 2.0.52。完整依赖按 Python 版本记录在 `requirements.lock`。

| 验收 | 结果 |
| --- | --- |
| MySQL 存储与恢复，Python 3.11.14 | 10 项通过 |
| S3 与现有业务服务集成，Python 3.11.14 | 6 项通过 |
| 上述 16 项 + 原助手操作、个人偏好、CLI 回归 | 85 项通过 |
| 独立 Python 3.10.12 存储/恢复验收 | 同样 10 项通过 |
| 一键临时 MySQL 启停和完整验收 | 16 项通过，进程自动停止 |

存储验收覆盖：8 个并发领取者只有一个成功；领取与检查点事务互斥；旧 epoch 的 checkpoint、pending writes、
模型回执与结果均被拒绝；事务内过期回滚；另一进程恢复 interrupt；并行成功节点不因其他节点失败而重做。
模型回执覆盖：恢复不重置 8 次预算、期限或图版本；已有响应直接复用；缺少响应的请求阻止相同或新的 call ID。

S3 验收覆盖：首次模型请求前已加载上下文、偏好和算子描述；禁用偏好不注入；模型跳过 describe 仍由代码补齐；
只准备卡片不创建任务；重复确认只创建一个 Pipeline/Job；模型没有确认工具；模型响应后角色或输入身份变化会被拦截；
响应回执已落库但图检查点尚未保存时，恢复只使用原响应，模型请求总数仍为一次。

## 边界与下一阶段

本轮完成阶段 A 的隔离验收，并验证阶段 B 的 S3 业务接缝。尚未完成正式异步 Runner 接入。
测试通过 monkeypatch 把图注入现有 `_generate`，沿用原有接入与结果发布；这不能证明 Web 进程崩溃后
正式 AssistantTurn 可恢复，更不能把原接入事务与实验 execution 的两个事务描述为原子。

- 正式接入仍需在同一平台事务中创建 Turn 与 execution，保存 owner/request/backend/graph/schema 版本，
  固定后端归属，跨 legacy 与图合并配额。实验 `enqueue` 不承担这些职责。
- 需要独立 Runner、心跳、过期恢复、受 epoch 保护的 Turn 发布，以及替换 legacy 120 秒过期分支；
  必须验证 Server/Runner 重启交叠、schema 兼容和旧执行排空。
- 当前 75 秒是首次领取起的保守墙钟预算，包含停机时间；不是设计中的有效执行耗时预算。
  准备工具本身的耗时截止、每轮工具输出累计预算仍需统一纳入正式 runtime。
- 当前只有 S3 的小型图，未覆盖原有完整四工具集合和多步骤草稿修复；不能开启通用 backend 切换。
- 用户长期记忆、任务完成总结、后台观察、检查点清理/删除、正式 UI 202 轮询和浏览器验收仍未实现。
- 本轮没有真实 Qwen 网络调用，没有运行 S3 计算进程；业务 fixture 使用私有 SQLite，检查点使用真实 MySQL。
  模型服务、业务 MySQL 与真实结果回填的端到端验收仍是后续门槛。

上述正式集成完成并经过开发环境批准前，不分配平台发行版本、不改变 dev/prod，也不发布 CLI。
真正的平台升级仍须按仓库规则同步发布兼容 CLI。生产接入顺序见
[阶段实施记录](../../docs/data_platform_assistant_langgraph.md)。
