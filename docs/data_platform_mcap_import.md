# GenDAS MCAP → LeRobot v3.0

Data Platform → **Data Import** 提供原始目录发现、预检、转换和结果注册。
当前适配器为 `genrobot_das`；其他 MCAP topic/schema 尚不支持。
默认每个目录只选择按文件名排序的前 **3 个 MCAP**，仅显式设为 0 才选择全部。
每个包含 MCAP 的来源目录生成一个独立 LeRobot 数据集。

## 使用流程

1. 管理员将原始目录配置到数据节点 Agent 的 `allowed_roots`，输出父目录配置到
   `writable_roots`。Agent 服务账号还必须具备对应的 Unix 读取/写入权限。
   Dev 的根目录仍须位于其独立 state root 内；测试时仅复制少量样本到 dev 数据目录，
   不放宽 dev 隔离规则。目录发现不要求来源包含 `meta/info.json`。
2. 打开 MCAP → LeRobot，选择节点，在可搜索、分页的目录列表中勾选来源。
   默认每目录最多 3 个文件进行试运行，显式设为 0 才处理全部；跨页选择会保留。
3. 确认页面显示的输出位置，点击“Convert selected”。每个文件会在同一转换任务中自动
   检查必需信号、时间戳和有效片段，通过后转换；无需先提交独立 Inspect。
4. 默认输出优先匹配 Agent 配置的同级 `<来源树名>_lerobot` 可写根，并保留来源树下的
   相对层级。例如 `/datasets/robot/task/00002` 对应 `/datasets/robot_lerobot/task/00002`。
   没有配对根时，如果来源的同级 `<目录名>_lerobot` 位于可写根内，则使用该位置；否则
   要求显式填写输出父目录，不自动切换到另一块盘。高级设置可覆盖输出父目录，保留列表
   公共来源父目录下的相对路径。实际目标始终在提交前显示；已存在的目标不会被覆盖。
5. 提交后打开统一 Jobs 面板，显示进度、逐文件检查/拒绝原因及最终报告；历史不在导入页
   平铺。完成后在 Jobs 使用输出数据集或 Prepare viewer。沿用取消、强制停止和重试控制。

“Advanced settings & optional inspection” 中仍可执行“Inspect only”作为可选预览，
结果也进入 Jobs。预检扫描消息并试解码视频开头，转换时完整检查所用帧。修改参数后可直接
提交新的转换，不必重新手动预检。视频短边默认 256，生成 LeRobot v3.0、30 FPS 数据集。

Viewer 账号只读。Admin、data_manager、operator 可提交，沿用现有数据可见范围和作业权限。
具有来源显式访问授权的账号继承输出访问范围。原始来源仅在 Data Import 列表显示，
不能直接提交普通 Viewer/预处理任务。转换不修改 MCAP 文件。

## 数据约定

| 来源（robot0 = left，robot1 = right） | 输出 |
| --- | --- |
| `/robot{0,1}/sensor/camera0/compressed` | `observation.images.{left,right}_wrist`，AV1 MP4 |
| `/robot{0,1}/vio/eef_pose` | `{left,right}_arm_quaternion_pose`，xyz + xyzw |
| `/robot{0,1}/sensor/magnetic_encoder` | `{left,right}_gripper_width`，单位米 |

使用 protobuf 内嵌描述符与 H.264 连续解码，不需要 ROS 环境。Viewer 分别显示
Left Gripper / Right Gripper，使用独立颜色和开关，宽度保持米制。
不生成 head、action、虚构 state 或任务阶段标签；这批输出面向 Viewer 和兼容的基础预处理，训练 action 映射另行定义。
任务文本取来源目录的父目录名称。

- 以消息 `header.timestamp` 的纳秒采集时间对齐到 30 FPS。
- 视频最近帧默认误差不超过 20 ms；平移和夹爪宽度线性插值，四元数使用 SLERP。
- 插值端点默认间隔不超过 100 ms；必需信号缺失、非法数值或大间隔会切开连续片段，
  不外推、不填零。默认保留至少 30 帧的片段。一个 MCAP 可能输出多个 episode。
- 视频短边默认 256，保持比例并补齐偶数边长。1600×1300 对应 315×256 内容，
  右侧补 1 像素成为 316×256。不同输出尺寸不会混入同一数据集。
- 标定原文保存在导入报告，但标记未验证；`world` frame_id 不代表双手共用坐标系已获验证。
  来源图像与标定分辨率不一致时不自动生成新的相机内参。

官方 LeRobot 0.4.4 写入器直接产生 v3.0 Parquet、视频、episode/task 元数据和统计量。
离线编码使用阻塞队列，避免实时采集式丢帧。发布前完整解码输出视频并检查帧数、尺寸、
PTS 连续性和 Parquet 行数；episode 视频区间回写为实际 PTS。
`provenance/episodes.json` 保存来源 SHA-256、源时间范围和稳定 episode UID；
`alignment_*.parquet` 保存每帧对应的源图像时间与各数值信号插值端点，
`import_report.json` 保存参数和接收/拒绝文件报告。

## 执行、恢复与安装

作业类型为 `import.mcap.inspect` / `import.mcap.convert`，要求 Agent job protocol 2 和
`mcap_import_protocol=1`。直接转换在执行开始时记录来源文件清单/stat 指纹，并在结束时
复核；逐文件转换还验证转换前后的内容 SHA-256。旧 API 客户端仍可提供
`inspection_job_id`，此时继续校验预检的账号、来源、参数和文件指纹。输出路径始终由
Server 和 Agent 分别校验可写根、来源隔离及文件系统访问权限。

每个成功 MCAP 单独保存完整 v3 检查点；重试时核对参数、源 SHA 和检查点文件 SHA 后复用。
未完成文件重新处理。最终合并到临时目录，通过校验后由现有作业执行器原子发布、注册。
成功发布后删除该作业检查点；取消/失败保留已完成检查点供重试，其空间计入输出盘预算。
服务器或 Agent 重启后，Agent 先核对原执行尝试及计算进程；尚有进程时继续监督，已有完整结果时
继续发布/上传/确认。若已启动的计算进程确认消失且未留下完整结果，
Server 将 `import.mcap.convert` 的同一 Job 自动重新排队，并记录失败尝试与自动恢复审计事件。
恢复保留原排队时间、输出路径和来源指纹；重新领取时仍检查账号权限、节点能力、维护状态、
并发限制和路径互斥。来源指纹变化会停止执行，需重新检查来源并创建任务。
每次连续进程丢失最多自动恢复 3 次，之后保留失败状态供人工排查和重试。
主动取消/强制停止、普通转换错误和已经进入发布阶段的失败不会自动重新排队。
恢复按完整 MCAP 文件检查点继续；中断文件和最终合并/校验可能重做，不恢复进程内存或单帧进度。
服务恢复后自动生效，无需保持网页打开；已经完成、取消或失败的历史任务不会批量重启。
处理规模再大也不将全部图像放入内存，不经过 PNG 或 v2.1 中间格式。

Agent bundle 安装器自动创建同版本目录内的 `mcap-runtime`，固定写入器、MCAP、AV、NumPy、
SciPy 等适配依赖；复用该不可变 release 的 Torch 等基础库，不替换平台的 LeRobot 包。
运行环境自检失败时节点不宣告转换能力，页面显示安装提示。
开发环境可单独安装：

```bash
python -m lerobot.data_platform.mcap_runtime --destination /tmp/mcap-runtime \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
export DATA_PLATFORM_MCAP_PYTHON=/tmp/mcap-runtime/bin/python
```

使用已安装平台依赖的 Python 执行安装命令，机器需提供 `uv`。
环境变量应设置在 Agent 进程中；安装不会自动启动转换或扩大 Agent 的目录授权。

## 验证范围

自动化测试覆盖配置校验、原始目录发现、时间断裂分段、预检绑定、来源变化拦截、账号范围、
结果注册及访问继承、检查点内容校验和停止信号。
真实验证只使用三个源文件，分别覆盖正常双手信号与单手姿态提前结束；不执行全量转换。

2026-09-22 的三条样本分别输出 1817、918、2048 帧（60.57、30.60、68.27 秒），
各 1 个 episode；双路视频均为 316×256。三者均通过 v3 元数据与全帧视频校验并生成 Viewer
缓存。另用其中一条验证浏览器提交、真实 Agent 子进程执行、原子发布和注册、手机布局以及
Viewer 账号只读；这些测试在隔离的本地测试服务执行，未部署到线上服务。

## 头部通道核验（2026-09-22）

对 H100 已下载数据的 19 个编号目录、36,409 个 MCAP 逐一读取 summary、通道、protobuf
schema 类型和消息计数，全部成功，0 个读取错误。通道并集为 14 种，全部位于 `/robot0/`
或 `/robot1/`，每侧最多包括：

- `sensor/camera0/compressed` 和 `sensor/camera0/camera_info`
- `sensor/imu`、`sensor/magnetic_encoder`
- `vio/eef_pose`、`sim/robot_info`、`system_info`

未发现独立头部相机或头部姿态通道，导入器没有遗漏相应的头部观测。此结论针对本次已下载
的录制文件，不代表所有上游版本或物理采集设备。少数文件缺少单手信号，仍需执行预检和
有效连续片段规则；此次全量检查仅扫描元数据，没有新增全量转换。

## 部署依赖镜像与中断恢复

构建时指定的 `--index-url` 会保存在发布清单，并传递到远端 Agent 安装进程（包括 MCAP
隔离运行环境）。后续通过网页发布到生产时沿用该镜像；命令行显式指定的镜像优先。
持久化的镜像地址必须使用 HTTPS，且不包含凭据、查询参数或片段。

已构建的版本使用 `--release <version>` 恢复，避免用 `--version` 重复构建。
如果 Web 已更新但 Agent 仍旧，维护模式应保留到 Agent 安装、健康检查和心跳版本校验
全部通过。先停止已确认失效的旧安装进程，再恢复当前目标版本；不要手工关闭维护模式。
