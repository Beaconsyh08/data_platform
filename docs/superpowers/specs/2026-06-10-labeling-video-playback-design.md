# Labeling 页面视频播放改造设计

日期：2026-06-10
状态：已确认（用户已批准）

## 背景

data_platform 的 labeling 页面（`lerobot/data_platform/templates/visualize_dataset_labeling.html`）目前用
`<img>` + `setTimeout` 定时器逐帧请求 JPEG（`/api/labeling/<ns>/<name>/image/<ep>?frame=N`）
来模拟播放，每帧一个 HTTP 请求，播放卡顿。viewer 页面则用原生 `<video>` 播放预编码的
h264 mp4（`ds_static/videos/<image_key>/episode_XXXXXX_h264.mp4`，经 `dataset_static`
路由以支持 Range 的方式提供）。

labeling 的 `ds_static` 与 viewer 的视频缓存是同一目录，可直接复用已编码的 mp4。

## 需求

- labeling 页面的播放方式改成与 viewer 类似的原生 `<video>` 播放。
- 只播放 labeling 使用的单个视角（`image_key` 对应的相机），不需要多视角网格。
- 控制条与 viewer 一致：播放/暂停、⏪-5s、⏩+5s、↩️回开头、进度条、`mm:ss / mm:ss`
  时间显示、倍速选择。
- 标框编辑保持现有行为：只在第 0 帧可编辑；视频回到开头（暂停且 currentTime 在第 0 帧内）
  时自动恢复可编辑。

## 方案（已选：方案 A）

复用 viewer 视频缓存 + 新增 labeling 视频端点。

### 后端（`lerobot/data_platform/viewer.py`）

1. 新增路由 `GET /api/labeling/<ns>/<name>/video/<int:episode_index>`：
   - 复用 `api_labeling_image` 中解析 `image_key` 的逻辑（读 labeling source 文件，
     支持 `variant` 查询参数）。
   - 调 `encode_episode_video(dataset_obj.root, dataset_obj.meta, episode_index,
     image_key, ds_static, max_frames=None, overwrite=False)`：已有缓存则直接返回路径，
     无缓存则按需编码（首次打开等待几秒，之后永久缓存）。
   - 成功 → `redirect(_asset_url(ns, name, rel_path))`（302，`dataset_static` 用
     `send_from_directory`，默认支持 Range 请求，可拖动）。
   - 失败（无 parquet / 编码失败 / image_key 缺失且无法回退）→ 404。
2. `api_labeling_episode` 响应增加两个字段：
   - `video_url`：只拼 URL 字符串（`/api/labeling/<ns>/<name>/video/<ep>?variant=...`），
     不做编码、不阻塞。
   - `fps`：取自 `dataset_obj.meta.fps`（前端用于第 0 帧编辑门控阈值）。

### 前端（`lerobot/data_platform/templates/visualize_dataset_labeling.html`）

1. `#stage` 内 `<img id="img">` 替换为 `<video id="vid" muted playsinline>`；
   overlay canvas 仍叠加其上，尺寸逻辑不变。
2. `baseW/baseH` 改从 `loadedmetadata` 事件的 `videoWidth/videoHeight` 获取
   （替代 `img.onload` 的 `naturalWidth/naturalHeight`）。
3. 控制条换成 viewer 同款：▶️/⏸️、⏪-5s、⏩+5s、↩️、进度条（0–100 映射 duration）、
   时间显示、倍速选择（与 viewer 相同档位）。删除 fps 输入框。
4. 删除逐帧播放逻辑：`scheduleNextFrame`、`playbackLoading`、`playbackTimer`、
   `imageUrlForFrame` 的逐帧轮询用法等。
5. 标框编辑门控：原 `currentFrame === 0` 改为「视频暂停且
   `currentTime < 0.5 / fps`」；↩️ 或拖回开头即恢复可编辑。fps 取 episode JSON 的
   `fps` 字段，缺失时默认 30。
6. 容错：`<video>` `onerror` 时退回显示第 0 帧静态图（现有 `/image/<ep>` 端点），
   标框编辑可用，仅无播放功能。

## 错误处理

- 视频端点 404（数据缺失/编码失败）→ 前端走静态图 fallback，页面其余功能不受影响。
- 编码进行中：浏览器 `<video>` 请求等待后端响应即可，无需额外状态。

## 测试

`tests/datasets/test_labeling.py` 增加视频端点路由测试：
- 已有缓存 mp4 → 返回 302，Location 指向 dataset_static 资源。
- 无数据 → 404。
