# Labeling 页面视频播放改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** labeling 页面的播放从「逐帧请求 JPEG」改为与 viewer 一致的原生 `<video>` 播放（单视角，复用 viewer 的 h264 mp4 缓存）。

**Architecture:** 后端在 `review.py` 提取 `resolve_labeling_image_key` 公共 helper（消除两处重复的 source.json 解析），新增 `/api/labeling/<ns>/<name>/video/<ep>` 端点：按需 `encode_episode_video(overwrite=False)` 后 302 重定向到 `dataset_static` 静态资源（`send_from_directory` 默认支持 Range）。前端用 `<video>` + viewer 同款控制条替换 `<img>` + 定时器逐帧播放；标框编辑门控从 `currentFrame === 0` 改为「暂停且 currentTime 在第 0 帧内」；视频加载失败时回退到第 0 帧静态图（仅编辑、无播放）。

**Tech Stack:** Flask（路由全部是 `run_server` 内的闭包）、原生 JS（labeling 模板不用 Alpine）、pytest。

**Spec:** `docs/superpowers/specs/2026-06-10-labeling-video-playback-design.md`

**与 spec 的两处小偏差（执行时按本计划为准）：**
1. spec 提到 episode JSON 返回 `video_url` 字段。实现改为前端用现有 `query()` 自拼 URL（与 `imageUrlForFrame` 同模式），这样选集时视频可与 episode JSON **并行**加载（现行为也是先设 `img.src` 再发请求）。episode JSON 只新增 `fps` 字段。
2. spec 的「路由测试」：本仓库没有任何 Flask test client 基础设施（app 是 4600 行 `run_server` 里的闭包，无可测的 factory），新建测试桩超出本次范围。改为：对新提取的 `resolve_labeling_image_key` helper 做单元测试（覆盖 variant 回退/坏 JSON/缺文件），路由本身是薄胶水，靠最后的手动验证任务覆盖。

---

## File Structure

- Modify: `lerobot/data_platform/precompute/labeling/review.py` — 新增 `resolve_labeling_image_key` helper
- Modify: `lerobot/data_platform/viewer.py` — 两处路由去重、新增 video 路由、episode JSON 加 `fps`
- Modify: `lerobot/data_platform/templates/visualize_dataset_labeling.html` — 播放 UI/逻辑整体替换
- Test: `tests/datasets/test_labeling.py` — helper 单元测试

---

### Task 1: 提取 `resolve_labeling_image_key` helper（TDD）

**Files:**
- Modify: `lerobot/data_platform/precompute/labeling/review.py`（在 `latest_label_variant` 之后、`resolved_labels_path` 之前，约 line 58）
- Modify: `lerobot/data_platform/viewer.py:118-134`（import）、`api_labeling_episode`（约 2512-2520）、`api_labeling_image`（约 2552-2565）
- Test: `tests/datasets/test_labeling.py`

- [ ] **Step 1: 写失败的测试**

在 `tests/datasets/test_labeling.py` 顶部的 `from lerobot.data_platform.precompute.labeling.review import (...)` 导入块中加入 `resolve_labeling_image_key,`（按字母序放在 `remove_reviewed_record` 之后）。文件末尾追加：

```python
def test_resolve_labeling_image_key(tmp_path: Path):
    labeling_dir = tmp_path / "labeling"
    labeling_dir.mkdir()
    # 无 source 文件
    assert resolve_labeling_image_key(labeling_dir) is None

    (labeling_dir / "source.json").write_text(
        json.dumps({"backend": "qwen_remote", "image_key": "head_cam"})
    )
    assert resolve_labeling_image_key(labeling_dir) == "head_cam"
    # variant 专属 source 缺失 -> 回退到 source.json
    assert resolve_labeling_image_key(labeling_dir, "grounding_dino") == "head_cam"

    (labeling_dir / "source_grounding_dino.json").write_text(
        json.dumps({"backend": "grounding_dino", "image_key": "wrist_cam"})
    )
    assert resolve_labeling_image_key(labeling_dir, "grounding_dino") == "wrist_cam"

    # image_key 为空字符串 -> None
    (labeling_dir / "source.json").write_text(json.dumps({"image_key": ""}))
    assert resolve_labeling_image_key(labeling_dir) is None

    # 坏 JSON -> None
    (labeling_dir / "source.json").write_text("not json")
    assert resolve_labeling_image_key(labeling_dir) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/datasets/test_labeling.py::test_resolve_labeling_image_key -v`
Expected: FAIL，`ImportError: cannot import name 'resolve_labeling_image_key'`

- [ ] **Step 3: 实现 helper**

在 `lerobot/data_platform/precompute/labeling/review.py` 的 `latest_label_variant`（line 56-57）之后插入：

```python
def resolve_labeling_image_key(labeling_dir: Path, variant: str | None = None) -> str | None:
    source_file = source_path(labeling_dir, variant)
    if not source_file.is_file():
        source_file = source_path(labeling_dir)
    if not source_file.is_file():
        return None
    try:
        value = json.loads(source_file.read_text()).get("image_key")
    except (json.JSONDecodeError, OSError, AttributeError):
        return None
    return value or None
```

（`json` 已在该模块导入；异常元组与同文件 `_source_backend` 保持一致。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/datasets/test_labeling.py::test_resolve_labeling_image_key -v`
Expected: PASS

- [ ] **Step 5: 替换 visualize_dataset_html.py 中两处重复逻辑**

5a. import 块（line 118-134）：在 `remove_reviewed_record_for_variant,` 之后加一行 `resolve_labeling_image_key,`。

5b. `api_labeling_episode` 中（约 line 2512-2520），把：

```python
        image_key = None
        source_file = source_path(labeling_dir, variant)
        if not source_file.is_file():
            source_file = source_path(labeling_dir)
        if source_file.is_file():
            try:
                image_key = json.loads(source_file.read_text()).get("image_key") or None
            except (json.JSONDecodeError, OSError):
                image_key = None
```

替换为：

```python
        image_key = resolve_labeling_image_key(labeling_dir, variant)
```

5c. `api_labeling_image` 中（约 line 2552-2565），把：

```python
        image_key = None
        try:
            frame_index = max(0, int(request.args.get("frame", 0)))
        except (TypeError, ValueError):
            frame_index = 0
        variant = request.args.get("variant") or None
        source_file = source_path(ds_static / "labeling", variant)
        if not source_file.is_file():
            source_file = source_path(ds_static / "labeling")
        if source_file.is_file():
            try:
                image_key = json.loads(source_file.read_text()).get("image_key") or None
            except (json.JSONDecodeError, OSError):
                image_key = None
```

替换为：

```python
        try:
            frame_index = max(0, int(request.args.get("frame", 0)))
        except (TypeError, ValueError):
            frame_index = 0
        variant = request.args.get("variant") or None
        image_key = resolve_labeling_image_key(ds_static / "labeling", variant)
```

5d. 检查 `source_path` import 是否仍被该文件其他地方使用：

Run: `grep -n "source_path" lerobot/data_platform/viewer.py`
若只剩 import 行本身，则把它从 line 118-134 的 import 块中删掉；若别处还在用则保留。

- [ ] **Step 6: 跑全量 labeling 测试 + 编译检查**

Run: `python -m pytest tests/datasets/test_labeling.py -q && python -c "import ast; ast.parse(open('lerobot/data_platform/viewer.py').read())"`
Expected: 全部 PASS，无语法错误

- [ ] **Step 7: Commit**

```bash
git add tests/datasets/test_labeling.py lerobot/data_platform/precompute/labeling/review.py lerobot/data_platform/viewer.py
git commit -m "refactor(labeling): extract resolve_labeling_image_key helper"
```

---

### Task 2: 后端 video 端点 + episode JSON 加 fps

**Files:**
- Modify: `lerobot/data_platform/viewer.py` — `api_labeling_image` 之后（约 line 2589 的 `return send_file(...)` 之后）新增路由；`api_labeling_episode` 加 `fps`

- [ ] **Step 1: 新增 `api_labeling_video` 路由**

在 `api_labeling_image` 函数体结束后（`return send_file(BytesIO(image_bytes), mimetype="image/jpeg")` 之后）插入：

```python
    @app.route("/api/labeling/<string:dataset_namespace>/<string:dataset_name>/video/<int:episode_index>")
    def api_labeling_video(dataset_namespace, dataset_name, episode_index):
        dataset_obj, ds_static = _get_ctx(dataset_namespace, dataset_name)
        variant = request.args.get("variant") or None
        image_key = resolve_labeling_image_key(ds_static / "labeling", variant)
        if image_key is None:
            image_keys = _dataset_image_keys(dataset_obj)
            image_key = image_keys[0] if image_keys else None
        if image_key is None:
            return jsonify({"error": "no image key available"}), 404
        try:
            out_path = encode_episode_video(
                dataset_obj.root,
                dataset_obj.meta,
                episode_index,
                image_key,
                ds_static,
                max_frames=None,
                overwrite=False,
            )
        except Exception:
            logging.exception("Failed to prepare labeling video for episode %s", episode_index)
            out_path = None
        if out_path is None or not out_path.is_file() or out_path.stat().st_size == 0:
            return jsonify({"error": "video unavailable"}), 404
        rel_path = Path("videos") / image_key / f"episode_{episode_index:06d}_h264.mp4"
        return redirect(_asset_url(dataset_namespace, dataset_name, rel_path))
```

说明：`encode_episode_video`（`precompute/video.py:71`）在缓存命中时直接返回路径不调 ffmpeg；`_dataset_image_keys`、`_asset_url`、`_get_ctx` 都是 `run_server` 闭包内已有的函数；`redirect`、`jsonify`、`logging`、`Path`、`encode_episode_video` 均已导入。无 image_key 时回退到数据集第一个相机（与 viewer 行为一致；`api_labeling_image` 传 `image_key=None` 时底层也是取默认相机）。

- [ ] **Step 2: `api_labeling_episode` 响应加 `fps`**

在 `record["image_key"] = image_key` 一行之后插入：

```python
        meta = dataset_obj.meta
        try:
            record["fps"] = float(meta.fps if hasattr(meta, "fps") else meta.info["fps"])
        except (AttributeError, KeyError, TypeError, ValueError):
            record["fps"] = None
```

（fps 取法与 `encode_episode_video` 内部一致。）

- [ ] **Step 3: 编译检查 + 回归**

Run: `python -c "import ast; ast.parse(open('lerobot/data_platform/viewer.py').read())" && python -m pytest tests/datasets/test_labeling.py -q`
Expected: 无语法错误，测试全 PASS

- [ ] **Step 4: Commit**

```bash
git add lerobot/data_platform/viewer.py
git commit -m "feat(labeling): add on-demand episode video endpoint and fps in episode JSON"
```

---

### Task 3: 前端改为 video 播放（viewer 同款控制条）

**Files:**
- Modify: `lerobot/data_platform/templates/visualize_dataset_labeling.html`

本任务是同一文件内的一组配套修改，按步骤完成后整体验证。所有被删除的符号（`setFrame`、`scheduleNextFrame`、`startPlayback`、`stopPlayback`、`playbackDelayMs`、`imageUrlForFrame`、`updatePlaybackUi` 旧版、`currentFrame`、`episodeLength`、`playbackTimer`、`playbackActive`、`playbackLoading`、`frameSlider`、`frameHint`、`fpsInput`）在本任务内全部清理，不留死代码。

- [ ] **Step 1: CSS 调整（line 100-140 区域）**

1a. `#img, #overlay { display: block; }` → `#img, #vid, #overlay { display: block; }`

1b. `#frameSlider { width: 100%; accent-color: #38bdf8; }` → `#seekSlider { width: 100%; accent-color: #38bdf8; }`

1c. 删除 `#fpsInput { ... }` 整块和 `#frameHint { color: #94a3b8; white-space: nowrap; }`，原位置替换为：

```css
  #timeHint { color: #94a3b8; white-space: nowrap; font-family: monospace; }
  #pbButtons { display: inline-flex; gap: 6px; }
  #rateButtons { display: inline-flex; gap: 4px; }
  #rateButtons button.active { background: #1d4ed8; }
```

（`#playbackControls` 的 `grid-template-columns: auto 1fr auto auto` 不变，正好对应 按钮组/进度条/时间/倍速 四列。）

- [ ] **Step 2: stage 与控制条 markup（line 438-457）**

2a. `<img id="img" style="image-rendering:pixelated" alt="">` 替换为：

```html
      <video id="vid" muted playsinline preload="auto"></video>
      <img id="img" style="image-rendering:pixelated; display:none" alt="">
```

（`#boxLegend`、`<canvas id="overlay">` 不动。）

2b. `<div id="playbackControls">...</div>` 整块替换为：

```html
    <div id="playbackControls">
      <span id="pbButtons">
        <button id="playBtn" type="button" title="Play/Pause (Space)">▶</button>
        <button id="backBtn" type="button" title="Jump backward 5 seconds">⏪</button>
        <button id="fwdBtn" type="button" title="Jump forward 5 seconds">⏩</button>
        <button id="restartBtn" type="button" title="Rewind to start">↩️</button>
      </span>
      <input id="seekSlider" type="range" min="0" max="100" step="0.1" value="0">
      <span id="timeHint">0:00 / 0:00</span>
      <span id="rateButtons"></span>
    </div>
```

- [ ] **Step 3: JS 变量声明替换（line 485-500 区域）**

把：

```js
  var playBtn = document.getElementById("playBtn");
  var frameSlider = document.getElementById("frameSlider");
  var frameHint = document.getElementById("frameHint");
  var fpsInput = document.getElementById("fpsInput");
```

替换为：

```js
  var vid = document.getElementById("vid");
  var playBtn = document.getElementById("playBtn");
  var backBtn = document.getElementById("backBtn");
  var fwdBtn = document.getElementById("fwdBtn");
  var restartBtn = document.getElementById("restartBtn");
  var seekSlider = document.getElementById("seekSlider");
  var timeHint = document.getElementById("timeHint");
  var rateButtons = document.getElementById("rateButtons");
  var playbackControls = document.getElementById("playbackControls");
  var PLAYBACK_RATES = [0.25, 0.5, 1, 2, 3, 5];
```

并把：

```js
  var currentFrame = 0, episodeLength = 1, playbackTimer = null, playbackActive = false, playbackLoading = false;
```

替换为：

```js
  var playbackRate = 1, episodeFps = 30, videoFallback = false;
```

- [ ] **Step 4: 播放函数整体替换（原 line 559-614：`updatePlaybackUi` 至 `togglePlayback`）**

把从 `function updatePlaybackUi() {` 到 `function togglePlayback() { ... }` 结束的整段（含 `imageUrlForFrame`、`setFrame`、`playbackDelayMs`、`scheduleNextFrame`、`stopPlayback`、`startPlayback`）替换为：

```js
  function atFirstFrame() {
    if (videoFallback) return true;
    return vid.paused && vid.currentTime < 0.5 / Math.max(1, episodeFps);
  }
  function formatTime(time) {
    var hours = Math.floor(time / 3600);
    var minutes = Math.floor((time % 3600) / 60);
    var seconds = Math.floor(time % 60);
    return (hours > 0 ? hours + ":" : "") + (minutes < 10 ? "0" + minutes : minutes) + ":" + (seconds < 10 ? "0" + seconds : seconds);
  }
  function updatePlaybackUi() {
    var editable = atFirstFrame();
    var dur = (!videoFallback && isFinite(vid.duration) && vid.duration) ? vid.duration : 0;
    playBtn.textContent = (videoFallback || vid.paused) ? "▶" : "⏸";
    seekSlider.value = dur ? String((vid.currentTime / dur) * 100) : "0";
    timeHint.textContent = formatTime(videoFallback ? 0 : (vid.currentTime || 0)) + " / " + formatTime(dur);
    playbackControls.style.display = videoFallback ? "none" : "grid";
    canvas.style.pointerEvents = editable ? "auto" : "none";
    canvas.style.opacity = editable ? "1" : "0";
  }
  function firstFrameImageUrl() {
    return API_BASE + "/image/" + currentIdx + query({t: Date.now()});
  }
  function videoUrl() {
    return API_BASE + "/video/" + currentIdx + query();
  }
  function enterImageFallback() {
    if (currentIdx === null || videoFallback) return;
    videoFallback = true;
    vid.removeAttribute("src");
    vid.style.display = "none";
    img.style.display = "block";
    img.src = firstFrameImageUrl();
    updatePlaybackUi();
    redraw();
  }
  function loadEpisodeVideo() {
    videoFallback = false;
    img.style.display = "none";
    vid.style.display = "block";
    vid.src = videoUrl();
    vid.playbackRate = playbackRate;
    updatePlaybackUi();
  }
  function togglePlayback() {
    if (videoFallback || !vid.src) return;
    if (vid.paused) vid.play();
    else vid.pause();
  }
  function setPlaybackRate(rate) {
    playbackRate = rate;
    vid.playbackRate = rate;
    var buttons = rateButtons.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].className = Number(buttons[i].dataset.rate) === rate ? "active" : "";
    }
  }
  function renderRateButtons() {
    rateButtons.innerHTML = "";
    for (var i = 0; i < PLAYBACK_RATES.length; i++) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.rate = String(PLAYBACK_RATES[i]);
      btn.textContent = PLAYBACK_RATES[i] + "x";
      btn.onclick = (function (rate) { return function () { setPlaybackRate(rate); }; })(PLAYBACK_RATES[i]);
      rateButtons.appendChild(btn);
    }
    setPlaybackRate(playbackRate);
  }
```

- [ ] **Step 5: `selectEpisode` 改造（原 line 793-813）**

把函数开头：

```js
  function selectEpisode(idx, scrollActive) {
    stopPlayback();
    currentIdx = Number(idx);
    currentFrame = 0;
    episodeLength = 1;
    updatePlaybackUi();
    img.src = imageUrlForFrame(0);
```

替换为：

```js
  function selectEpisode(idx, scrollActive) {
    vid.pause();
    currentIdx = Number(idx);
    loadEpisodeVideo();
```

把回调内：

```js
      currentRec = data;
      episodeLength = Math.max(1, Number(currentRec.episode_length) || 1);
      currentFrame = 0;
```

替换为：

```js
      currentRec = data;
      episodeFps = Math.max(1, Number(currentRec.fps) || 30);
```

（回调内已有的 `updatePlaybackUi(); renderInfo(); redraw(); highlightSidebar(scrollActive);` 保留。）

- [ ] **Step 6: 编辑门控两处替换**

6a. `redraw()`（原 line 1048）：`if (!currentRec || currentFrame !== 0) return;` → `if (!currentRec || !atFirstFrame()) return;`

6b. `canvas.onmousedown`（原 line 1091）：`if (currentFrame !== 0) return;` → `if (!atFirstFrame()) return;`

- [ ] **Step 7: `img.onload` 改为仅 fallback 生效（原 line 1126-1134）**

替换为：

```js
  img.onload = function () {
    if (!videoFallback) return;
    baseW = img.naturalWidth || 256;
    baseH = img.naturalHeight || 256;
    resizeStage();
    redraw();
    updatePlaybackUi();
  };
```

- [ ] **Step 8: 事件绑定替换（原 line 1182-1184）**

把：

```js
  playBtn.onclick = togglePlayback;
  frameSlider.oninput = function () { stopPlayback(); setFrame(Number(this.value) || 0); };
  fpsInput.onchange = function () { if (playbackActive) { if (playbackTimer) window.clearTimeout(playbackTimer); playbackTimer = null; scheduleNextFrame(); } };
```

替换为：

```js
  playBtn.onclick = togglePlayback;
  backBtn.onclick = function () { if (!videoFallback) vid.currentTime = Math.max(0, vid.currentTime - 5); };
  fwdBtn.onclick = function () { if (!videoFallback && isFinite(vid.duration)) vid.currentTime = Math.min(vid.duration, vid.currentTime + 5); };
  restartBtn.onclick = function () { if (!videoFallback) { vid.pause(); vid.currentTime = 0; } };
  seekSlider.oninput = function () { if (!videoFallback && isFinite(vid.duration)) vid.currentTime = (vid.duration * (Number(this.value) || 0)) / 100; };
  vid.addEventListener("loadedmetadata", function () {
    baseW = vid.videoWidth || 256;
    baseH = vid.videoHeight || 256;
    resizeStage();
    redraw();
    updatePlaybackUi();
  });
  vid.addEventListener("timeupdate", function () { updatePlaybackUi(); redraw(); });
  vid.addEventListener("play", function () { updatePlaybackUi(); redraw(); });
  vid.addEventListener("pause", function () { updatePlaybackUi(); redraw(); });
  vid.addEventListener("seeked", function () { updatePlaybackUi(); redraw(); });
  vid.addEventListener("ended", function () { updatePlaybackUi(); redraw(); });
  vid.addEventListener("error", enterImageFallback);
```

（键盘 Space → `togglePlayback()` 的既有 handler 不需要改。）

- [ ] **Step 9: 初始化处加倍速按钮渲染（原 line 1196 附近）**

`resizeStage();` 之前加一行：

```js
  renderRateButtons();
```

- [ ] **Step 10: 清理检查**

Run: `grep -n "currentFrame\|episodeLength\|playbackTimer\|playbackActive\|playbackLoading\|frameSlider\|frameHint\|fpsInput\|setFrame\|scheduleNextFrame\|startPlayback\|stopPlayback\|playbackDelayMs\|imageUrlForFrame" lerobot/data_platform/templates/visualize_dataset_labeling.html`
Expected: 无输出（所有旧播放符号已清理干净）

- [ ] **Step 11: Jinja 模板编译检查**

Run: `python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('lerobot/data_platform/templates'))
env.get_template('visualize_dataset_labeling.html')
print('template OK')
"`
Expected: `template OK`

- [ ] **Step 12: Commit**

```bash
git add lerobot/data_platform/templates/visualize_dataset_labeling.html
git commit -m "feat(labeling): viewer-style native video playback with frame-0 editing gate"
```

---

### Task 4: 手动验证（需要用户配合）

**Files:** 无代码改动

- [ ] **Step 1: 启动 data_platform 服务，打开某数据集的 labeling 页面，确认：**
  1. 选集后视频正常加载（首次无缓存的集会等待编码几秒）；
  2. ▶/⏸、⏪、⏩、↩️、进度条拖动、倍速切换都生效；
  3. 第 0 帧（暂停在开头）时标框可编辑，播放/拖走后框隐藏且不可点，↩️ 回开头后恢复；
  4. Space 播放/暂停、j/k 切集、Enter 保存等快捷键不回归；
  5. variant 切换、compare、save/reset 流程不回归；
  6. （可选）临时改坏 video 端点验证静态图 fallback。

- [ ] **Step 2: 跑全量相关测试**

Run: `python -m pytest tests/datasets/test_labeling.py tests/datasets/test_precompute.py -q`
Expected: 全部 PASS
