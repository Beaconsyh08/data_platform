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
2. 打开 Data Import，选择节点和一个或多个来源目录。保持每目录 3 个文件进行试运行。
3. 点击“Inspect selected”，检查信号数量、原始/输出尺寸、有效时长、片段和拒绝原因。
   预检扫描消息并试解码视频开头；转换阶段再完整检查所用帧。改变参数后须重新预检。
4. 填写输出父目录，点击“Convert selected”。输出为父目录下的任务名/来源目录名，
   已存在的目标会报错；再次导入使用新的父目录。
5. 在本页查看进度、有效片段和输出位置，或在 Pipeline Runs 使用现有取消、强制停止、
   重试控制。成功结果自动注册，可点击“Prepare Viewer”。

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
`mcap_import_protocol=1`。预检参数摘要与来源文件清单/stat 指纹在转换时复核；
逐文件转换还验证转换前后的内容 SHA-256。

每个成功 MCAP 单独保存完整 v3 检查点；重试时核对参数、源 SHA 和检查点文件 SHA 后复用。
未完成文件重新处理。最终合并到临时目录，通过校验后由现有作业执行器原子发布、注册。
成功发布后删除该作业检查点；取消/失败保留已完成检查点供重试，其空间计入输出盘预算。
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
