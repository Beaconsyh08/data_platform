# UMI LeRobot v3 数据支持

平台直接接入 `robot_type=UMI` 的 LeRobot v3 数据。图像可内嵌于 Parquet，
信号按 `head_pose`、`left_arm_pose`、`right_arm_pose`、对应四元数位姿和左右夹爪字段读取。
不需要先生成 `action/state`，也不会修改源数据或创建训练目标。

## 使用方式

1. 将数据集放在 Agent 的 allowed roots 下，等待目录同步；本地数据也可通过 Register 注册。
2. 数据列表显示机器人类型、LeRobot 存储版本及 Raw/Standard/Curated 阶段。
   DVT1/DVT2 不再作为列表标签。
3. 选择数据集，运行 Viewer cache。三路图像生成独立预览视频；遥测按头部、左手和右手分组。
   曲线使用 `head_pose.x` 等唯一名称；源数据与缓存 CSV 保留夹爪采集值，Viewer 显示时按 `/100` 归一化到 0–1。
   UMI 使用六个常驻按钮 Head3 / Head4 / Left3 / Left4 / Right3 / Right4，高亮表示开启，再次点击关闭。
   每个部位两组分别为 XYZ + Roll/Pitch/Yaw、
   XYZ + qw/qx/qy/qz。两组共用 `*_pose` 的 XYZ，只绘制一次，不显示 `*_quaternion_pose` 中重复的 XYZ。
   任一组开启时显示该部位 XYZ，两组都关闭时隐藏。夹爪和 Stage 使用独立的信号复选框。
   信号面板按 Head / Left / Right 三列排列，分量名称省略字段前缀，完整名称和单位保留在悬停提示中。
   每个位姿分量使用独立颜色；分组和单条曲线可独立切换，不依赖 `action/state`。
   UMI Viewer 的标题、分组和悬停提示使用英文。
4. Analysis 可直接统计 episode 数、帧数、时长与任务分布；原始信号统计取自
   `meta/stats.json`，代表完整数据集，不随页面筛选变化。没有统计值时显示未提供。
5. Split 按 episode 范围或任务筛选；Merge 仅接受机器人 profile、FPS、完整字段及已声明语义一致的源数据。
   UMI 合并必须使用 `dimension_policy=strict`，不支持截断、补维或与 DVT 数据混合。

单位和坐标系未声明时显示 `Not declared`。夹爪显示使用约定的 0–100 原始范围（小于 1.5 的值也除以 100），
不裁剪超出范围的采集值；不自动执行 `/100`、
位姿坐标变换或四元数重排。UMI 默认按时间等分生成 Stage，默认 5 段，可在操作表单调整。
已有合法阶段标注默认保留；点击 Stage 重新切分时才忽略旧阶段。
Viewer 使用 `Stage 1/N` 等时间段名称，不将时间等分结果解释为抓取、抬起等动作事件。

## 可用操作与兼容

- 通用查看、元数据分析、拆分和同结构合并适用于 UMI。
- DVT 标准化、动作维度转换、平滑、动作状态数值编辑、基于关节/夹爪事件的 DVT Stage、动作状态异常检测和
  依赖特定策略输入的 Embedding 不适用于 UMI；页面与服务端均校验，Agent 会再次检查。
- DVT 操作默认统一使用 `h10w_dvt2_stage_v1`，包括原始记录为 DVT1 的数据；
  `--data-version DVT1` 或表单手动选择仍可使用旧规则。源数据 profile 不做批量改写。
- UMI 的 Stage 策略为 `time_equal_v1`，所有任务均仅按时间戳等分，不读取或生成 DVT `action/state`。
  UMI 不接受 DVT override，其他依赖 DVT 语义的操作保持不可用；未知机器人保留未知。
- `DatasetDataProfile` schema 2 允许 `legacy_data_version=null`，兼容读取 schema 1。
  数据摘要与 Viewer manifest 增加 `robot_type`、`data_profile`、`default_processing_profile`、`operation_capabilities`；
  manifest 还记录 `signal_columns_version=2`、列描述和 `fallback_stage_count`。
  更改策略或等分段数后，重建 CSV/manifest 并复用视频。
- 升级后再次准备缓存会重建不兼容的 CSV/manifest，复用兼容视频。源数据和已发布生命周期版本不做批量迁移。

## 数据写入与来源追踪

Viewer cache 写入缓存目录，不修改源数据。Split/Merge 才会生成新数据集，默认采用带时间戳的
兄弟目录；输出存在时拒绝覆盖。dry-run 仅读取元数据和文件信息，不生成临时数据集。

对于 v3 内嵌图像，Split/Merge 直接按 `episode_index` 过滤共享 Parquet 分片，保留图像 bytes、
数值类型和维度名称，在 staging 中重建输出索引、episode/task 元数据和统计，验证后提交。
本路径不把内嵌图像转成视频。其他已有存储路径继续使用其原有实现。

源元数据、`export_manifest.json` 和其他来源 sidecar 保存在
`provenance/source_000/` 等来源目录下；输出 `meta/preprocess_split.json` 或
`meta/preprocess_merge.json` 记录 episode lineage，生命周期登记补齐源版本关联。
这些来源清单中的旧索引不代表输出位置。源中的已有符号链接按链接保存。

本地任务在输出缓存准备和登记完成后结束；远程任务还需要上传并绑定 Viewer cache。
任务失败会报告失败原因，不能将未完成的缓存当成成功结果。

## 升级顺序

1. 先在 Server A 使用现有 `sudo data-platform-update` 更新服务。
2. 检查 `/healthz` 返回 `data_profile_protocol: 3`。
3. 从同一工作区构建 Agent 包，再按照分布式部署文档升级 Agent。
4. 确认节点心跳中的 `data_profile_protocol` 为 3，并等待数据目录重新同步。
5. 选择少量 episode 验证预览，再运行拆分或合并。

旧 Agent 接收 UMI 或强制重新切分 Stage 的任务前会被中央服务以 409 拒绝，并提示升级。安装包位于 `dist/agent/`，
可用以下命令构建一个新的版本（版本名必须未使用）：

```bash
python3 scripts/build_data_platform_agent_bundle.py --output-dir dist/agent --version 0.1.0-r12-stage-defaults
```

测试覆盖共享分片、三路内嵌图像、重名维度、原始夹爪数值、缓存失效、旧 profile、
本地与远程接口、Agent 协议、拆分合并一致性、输出冲突、dry-run 和失败清理。
