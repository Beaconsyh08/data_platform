# Task setup

任务文本不再限定为 pick/place/give。平台使用统一任务目录，将原始文本映射到具体任务、任务类型和属性；分析、curation、阶段缓存与 Agent 共用同一份配置快照。

## 任务如何分类

| 内容 | 用途 | 示例 |
| --- | --- | --- |
| 原始 task 文本 | 保存采集时的指令，不自动改写 | `Open the door of the washing machine below` |
| task_index | 当前物理数据集内的查找编号 | `0`；合并或转换后可以改变 |
| task_id | 跨数据集复用的具体任务身份 | `open_washing_machine_door_below` |
| task_family | 任务类型 | `open_door` |
| attributes | 可在页面扩展的文本属性 | `object=door`、`appliance=washing_machine`、`location=below` |

旧任务也使用这套定义，并保留原来的细分视图和适用算法：

| 旧任务 | task_family | 属性 |
| --- | --- | --- |
| 普通 pick | `pick` | `object`、`position_mode=none` |
| 绝对方位 pick | `pick` | `object`、`position_mode=absolute`、`direction` |
| 参照方位 pick | `pick` | `object`、`position_mode=relative`、`direction`、`reference` |
| place | `place` | 可选的对象属性；`Place object` 不强制填写具体对象 |
| give | `give` | `object` |

内置目录提供原有玩具任务与以下五条洗衣场景任务。其他已有 Pick/Give/Place 文本通过旧规则的兼容适配获得稳定身份，原始文本保持不变。
发现页面的 **Save as definition** 可以将这些旧规则解析的任务存入目录，保留其已有任务 ID，并添加别名或调整属性。

| 原始文本 | task_family | 属性 |
| --- | --- | --- |
| Open the door of the washing machine below | `open_door` | `object=door, appliance=washing_machine, location=below` |
| Close the door of the washing machine below | `close_door` | 同上 |
| Open the door of the clothes dryer above | `open_door` | `object=door, appliance=clothes_dryer, location=above` |
| Close the door of the clothes dryer above | `close_door` | 同上 |
| Grasp the clothes to the washing machine | `load_clothes` | `object=clothes, appliance=washing_machine, destination=washing_machine_interior` |

最后一条的完成目标是“放入洗衣机”，不会因含有 `Grasp` 而归为 pick。

## 新任务接入流程

1. 在数据集控制台点击 **Task setup**，或从分析页面进入 **Task setup**。页面地址为 `/tasks?dataset_key=<数据集标识>`。
2. 点击 **Load instructions**。平台读取任务文本与对应 episode，展示匹配结果；远程数据使用 Agent 上报的元数据。
3. 对已有任务选择目录中的定义；同义表达可加入该定义的别名。新数据集会预览最新目录，已经配置的数据集继续使用原先应用的版本。
4. 对新任务点击 **Define task**（或在 **Task catalog** 中点击 **New task**），填写稳定 ID、显示名称、任务类型和属性。任务类型和属性名使用小写字母、数字和下划线；属性值为文本。可以复用已有类型，也可以直接输入新类型。
5. 点击 **Save definition**，然后 **Preview matches**，检查类型、属性、匹配状态和受影响 episode 数量。
6. 点击 **Apply to dataset**。后台刷新缓存；目录保存本身不会自动升级其他已配置数据集。

例如添加“打开冰箱门”：类型填 `open_door`，具体任务 ID 填 `open_fridge_door`，属性填 `object=door`、`appliance=refrigerator`，将真实采集文本填入别名。无需修改 Python 或页面代码。

匹配顺序为数据集显式映射、唯一目录别名、旧任务兼容规则。多个定义共用同一个别名时，页面显示冲突，需要选择具体任务。未识别文本显示“待配置”，仍能浏览、统计和按原文筛选，不会直接进入 Infra Quarantine。

目录和映射采用版本检查。多人编辑发生冲突时，刷新页面再预览；不会覆盖他人刚保存的版本。映射按文本而不是 task_index 关联，任务清单摘要用于检测预览后数据是否发生变化。
本机已有任务缓存重建作业时，新的应用请求会提示等待，避免两个版本同时写入缓存。

## AI-assisted task setup

Configure `DASHSCOPE_API_KEY` once in `/etc/data-platform/server.env` on Server A and restart
`data-platform-web`. Task suggestions, Qwen API object labeling and VLM auto-tagging share this
credential. `QWEN_DASHSCOPE_API_KEY` remains a fallback alias. `DASHSCOPE_BASE_URL` selects the
server endpoint; `DATA_PLATFORM_TASK_MODEL` selects the task suggestion model. Labeling/tagging
continue to use their own image-capable model settings. See the [README setup steps](../README.md#shared-qwen-api-key-on-server-a).

1. Open **Task setup → Load instructions** and select the latest catalog version.
2. Click **Suggest with AI**. Existing mappings, unique aliases and compatible legacy tasks are
   resolved first. Only unmatched or conflicting instruction texts are sent to Qwen, in batches of
   up to 20 distinct instructions, together with the catalog. No videos or episode rows are uploaded.
3. Review each suggestion's action, task type, attributes and affected episode count. Suggestions
   can reuse existing tasks, create definitions, or flag an instruction that needs clarification.
   Edit proposed names, types and attributes directly. Nothing is selected or applied automatically.
4. Select individual suggestions or use **Select ready suggestions**, then **Save selected & preview**.
   The server validates the selected definitions and creates a catalog version. Only observed,
   selected instructions become confirmed aliases. Existing alias conflicts remain explicit dataset
   assignments rather than adding more conflicting aliases.
5. Review the resulting matches and click **Apply to dataset** to apply the mapping. This retains the
   existing background CSV refresh; generating suggestions and saving definitions do not need or
   start Prepare cache. Unselected instructions remain available by their original text.

The model uses JSON output with server-side validation and thinking disabled, following the
[Qwen structured output protocol](https://help.aliyun.com/en/model-studio/qwen-structured-output).
Model-provided aliases and stage algorithms are not accepted. New tasks use five equal time stages;
adjust stage settings in the catalog editor if needed. This does not add physical-event detection
or task-success algorithms. Invalid or missing rows remain unresolved; failed requests leave the
catalog and mapping unchanged and manual setup available.

Catalog, mapping and task-inventory checks reject stale suggestions when configuration or source
instructions have changed. Published configurations and selections retain their snapshots. Suggestion
jobs are temporary server jobs; after a service restart, regenerate unsaved suggestions. Saved catalog
definitions are persistent. A source inventory change during review requires loading it again.

Remote suggestions use the Agent's latest reported task metadata and execute on Server A. No Agent
model credentials, new Agent protocol, remote model job, or Viewer cache is required. Apply still
uses the existing configuration protocol when refreshing remote CSV files.

Additional endpoints:

| Endpoint | Behavior |
| --- | --- |
| `GET /api/task-suggestions/capabilities` | Reports the default model and a configured-key boolean, never the key |
| `POST /api/task-suggestions` | Accepts the usual dataset/catalog/mappings plus optional `model`; starts a background job |
| `GET /api/task-suggestions/<job_id>` | Returns job status and validated suggestions |
| `POST /api/task-suggestions/<job_id>/accept` | Saves selected/edited `suggestions` and returns a match preview; does not apply the mapping |

## 分析与 curation

Analysis 顶部显示固定的数据集概览；下方通过 Task type、Instruction contains 和 Review status 筛选。Task distribution 支持按具体任务、任务类型和属性分组，Episode breakdown 展示时长、阶段、缓存及标签分布；点击条目筛选完整分页的 episode 列表。旧方位矩阵位于适用任务的 Object and spatial coverage 折叠区。

Build selection 将任务与原文筛选带入 curation；Review status、方位矩阵单元格及 breakdown 筛选只作用于当前分析页面，不会随链接传递。Analysis 可以直接运行，无需 Prepare cache；任务、帧数和时长使用元数据，阶段和存在标签可由已有 CSV 补充。缺少可选 CSV 不再产生审核异常。使用的目录版本和远程元数据同步时间单独显示。

Task setup 的 **Build selection** 页签 支持：

- 按原文、`task_family`、`task_id`、`task_attributes.<属性名>` 筛选。
- 同一个属性的多个候选值采用 OR，不同属性之间采用 AND。
- 选择基础数据版本，按某一维度设置各组 episode 数量，预览后创建配方和审核工作区。
- 返回控制台的 Curation 页面继续审核、发布和物化。数据集尚未登记 lifecycle 版本时，先在控制台完成版本登记。

例如先筛选 `task_attributes.appliance=washing_machine`，再按 `task_family` 配置开门、关门、装载衣物各组数量。空筛选结果或全部为零的配比会报错，不会被解释为选择全部数据。

一个 episode 若具有多个不同分组值，配比操作会提示冲突，不会只采用第一条任务。分布统计按任务成员关系计数，多任务 episode 可以出现在多个组中。

DatasetProfile、Recipe、Workspace 和 Manifest 固定使用创建时的配置快照。更改目录或重新应用映射，不会改变旧配方的校验、选择结果或已发布结果。物化输出登记继承的任务映射；原始 prompt、Parquet 和源数据元数据不会因配置操作而修改。

## 阶段与算法

新任务默认使用 5 段等时划分，页面可调整为 2–100 段。文字显示 `Stage 1/N` 等中性标签；这些时间段不代表检测到了抓取、开门完成或任务成功。

原有 Pick/Place/Give 的阶段算法保留。阶段策略独立于机器人信号布局，任务配置不替代 DVT1/DVT2 的机器人数据 profile。

源 Parquet 的已有阶段，以及用户编辑的 Viewer 阶段标注优先保留。CSV 旁的 `.stages.json` 记录实际阶段数、归一化编码和来源，Viewer 与分析按该元数据解释阶段。自动生成的阶段转移另外保存来源副本，用于区分人工修改；没有生成来源记录的旧转移按人工标注保留。

任务定义不会自动提供专用目标检测、成功判断或负样本构造算法。现有 Pick/Give 标注和构造路径只接受适用任务；新算法需要单独开发及验证。

## 多服务器与缓存

- 中央 lifecycle 仓库存储任务目录、映射及其不可变版本，位置在源数据目录之外。
- Agent 扫描时上报任务清单和 episode 任务关系，并声明 `task_config_protocol=1`。
- 缓存作业携带完整快照和摘要。相同配置可复用正在运行的作业；不同配置创建不同作业。
- Agent 使用同一解析器，在 Viewer manifest 中回传配置版本。中央检查摘要；任务配置作业的上传目录彼此隔离，晚完成的旧配置不会替换当前配置的缓存。
- 分类配置变化后更新分析；阶段策略变化后重新生成派生 CSV，人工标注继续保留。
- Analysis 使用当前已应用的任务配置与最新同步的元数据；Agent 离线时仍可分析。阶段策略不匹配的缓存阶段会被省略并单独提示，基础统计不等待缓存重建。旧 Agent 缺少 episode 长度时，升级并同步元数据即可补齐，不要求 Prepare cache。

部署时更新中央服务和 Agent 包，再触发 Agent 同步和缓存准备。已有未配置数据集仍可使用内置目录浏览，无需修改源数据或批量迁移 Parquet。

本期支持本机 curation 全流程以及中央/Agent 的任务配置和分析一致性。远程身份与完整元数据同步、中央审核后由 Agent 执行物化的链路属于后续阶段。

## 接口

| 接口 | 行为 |
| --- | --- |
| `GET /api/task-catalogs` | 列出内置及已保存的目录版本 |
| `POST /api/task-catalogs` | 使用 `tasks` 和 `expected_version_id` 创建目录版本 |
| `GET /api/task-mappings?dataset_key=...` | 发现任务并读取当前映射 |
| `POST /api/task-mappings/preview` | 提交数据集、目录版本及文本映射，返回影响预览与清单摘要 |
| `POST /api/task-mappings` | 带上 `expected_version_id` 和 `expected_inventory_digest` 应用映射，返回缓存作业 |

现有分析返回值增加 `task_config`、`task_dimensions`、`task_families`、`task_configuration_pending` 和缓存过期状态；现有字段仍保留。Curation 的 profile、cohort 和 recipe 创建接口接受可选 `task_config` 快照，未传时固定当前数据集配置。快照摘要参与逻辑身份计算，执行 worker 数量不参与。

所有配置写入使用现有操作审计日志。中央控制平面启用时，viewer 账户只读，admin/operator 可维护配置。
