# 不同维度的数据集合并

维度长度来自每个源数据集的真实 `meta/info.json`，不限定为 17D/20D。
`action`、`state`、`observation.state` 独立对齐，支持两个以上的源数据集。

## 页面操作

1. 勾选合并源，在 **Signal dimensions** 选择模式：

   | 模式 | 输出维度与行为 |
   | --- | --- |
   | Strict | 保留原有严格校验，不改变维度与顺序。 |
   | Minimum | 采用该字段最短源的命名布局；所有其他源必须包含这些名称，多余维度丢弃。 |
   | Padding | 采用名称并集；先保留最宽源的顺序，再追加其他源独有的名称，缺少的位置填充值。 |

   长度相同时按合并源选择顺序决定参考顺序。名称并集可能大于任何一个源的维度。

2. 在 **Dimension correspondence** 选择对应关系：

   - **Use names from dataset metadata**：已有完整、唯一的逐维名称时使用。
     类似 `names: ["actions"]` 的整体标签不能描述 20 个维度的对应关系。
   - **Explicit mapping**：给每个源的每个字段填写逗号分隔的逐维名称。
     名称列表的第一个元素对应源第 1 维，依次类推。源之间使用相同名称，表示同一个信号。
     每一行必须与该字段的实际维度数一致，名称不能为空或重复。

3. 如果确认数据有共同的前缀与尾部，填写 **Shared first N / Shared last M**，点击
   **Apply prefix / tail preset**。这两个值默认均为 0，需要用户明确填写。
   预设会为前缀生成 `shared_1...`，中间生成 `middle_1...`，尾部生成 `tail_1...`。
   这些名称表示用户确认的对应关系，不是平台推断的关节语义。生成后仍可分别编辑各字段。

4. 检查 **Mapping preview**：显示实际输入/输出维度、逐维来源、丢弃维度和填充值。
   页面维度编号从 **1** 开始。勾选对应关系确认框；改变映射或源选择后需重新确认。

5. Padding 可设置有限数值的 **Padding value**，默认 0；整数信号只接受可表示的整数填充值。
   建议先执行 **Dry run only** 查看后端校验结果与映射，再执行实际合并。

### 任意维度示例

假设 5D 与 8D 信号共同拥有前 3 维、后 2 维，且多出的中间 3 维确实是额外信号。
填写 `N=3, M=2`：

```text
5D: shared_1 shared_2 shared_3                            tail_1 tail_2
8D: shared_1 shared_2 shared_3 middle_1 middle_2 middle_3 tail_1 tail_2
```

- Minimum 输出 5D：8D 源删除第 4/5/6 维，保留第 7/8 维作为输出末两维。
- Padding 输出 8D：5D 源在中间补 3 个值，原第 4/5 维移到输出第 7/8 维。

17D/20D 与前 16、后 1 只是同一种配置的另一个例子，不是实现限制。
如果实际信号顺序不同，直接编辑逐维名称；不要使用不符合数据语义的前缀/尾部预设。

## 校验与输出

映射只作用于新建的 sibling 输出，不改写源数据或源 `info.json`。输出同步更新特征名称、
维度、Parquet 向量、episode/global statistics，以及 CSV/Viewer 缓存。源 episode 的
合并 lineage、视频与未修改字段沿用原有合并逻辑。

`meta/preprocess_merge.json` 的 `dimension_alignment` 记录逐源、逐字段的映射，包含
`source_indices`、`dropped_names`、`padded_names` 和 `padding_value`。
其中 `source_indices` 使用 **0 起始索引**，`null` 表示补齐位置。填充值也计入输出统计，
它是合成数据，不能当作实际采集值；本功能不额外添加训练用 mask 列。

不同机器人、FPS、单位、gripper 编码或 stage profile 的冲突仍会拒绝，显式映射不绕过这些校验。
当前 native embedded-image / UMI 合并仍要求 Strict；本功能扩展的是数值 action/state 对齐。
Minimum 仍要求其他源包含最短源的完整命名布局，不会默默猜索引或截断向量。

## API / CLI

本地和 Agent 的 merge options 使用相同字段：

```json
{
  "dimension_policy": "pad",
  "padding_value": 0,
  "dimension_names": [
    {"action": ["a", "tail"]},
    {"action": ["a", "extra", "tail"]}
  ]
}
```

`dimension_names` 与 `src_keys` / `source_location_ids` 顺序一一对应；CLI 对应 `--root`
加上 `--preprocess-merge-with` 的顺序。可只覆盖缺失名称的字段，其余字段继续使用元数据名称。
不传此字段即保持原有按元数据名称对齐的行为。Strict 不接受显式映射。

CLI 新增：

```text
--preprocess-merge-dimension-policy strict|min|pad
--preprocess-merge-dimension-names /path/to/source-mappings.json
--preprocess-merge-padding-value 0
```

映射 JSON 文件内容是上例 `dimension_names` 对应的数组。
Padding 和显式映射要求 Agent 宣告 `merge_alignment_protocol: 2`；旧 Agent 会明确返回升级提示。
代码发布需先更新 Server，再更新对应 Agent，保留原有环境隔离、权限和任务审计。

## 输出路径与提交反馈

远程 Merge 会在 Agent 上报的第一个 `writable_roots` 下预填 `merged_<timestamp>` 新目录；
可修改为其他允许目录下的新路径。页面检查绝对路径、目录边界和源数据集重叠，
并在字段旁和按钮旁显示原因。服务端与 Agent 仍分别校验路径和实际文件权限；
页面校验无法检查远程路径是否已存在或符号链接的真实目标。
本地 Merge 保持填写输出文件夹名称。

`writable_roots` 来自所选 Agent 的配置和上报信息，不由页面用户角色决定。
管理员身份也不能绕过 Agent 的路径限制或操作系统权限。
提交时显示固定浮层，接受任务后显示任务 ID，拒绝时显示原因；
网络错误或超时显示“无法确认是否收到”，提醒先检查 Runs，避免重复提交。
任务被接受与任务执行完成是两个状态，执行结果在 Runs 中查看。
