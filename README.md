# SunLens 学徒

实时记录你操作**目标窗口**（默认 fnOS NAS 网页会话）→ 用**本地 Ollama 的 qwen3-vl** 看懂每一步在做什么 → 沉淀成可回看的时间轴。
画面不出本机（本地推理，无需 API Key、无需脱敏）。架构见 [ARCHITECTURE.md](ARCHITECTURE.md)（早期设计文档，目标为远控窗口，现已改为通用目标窗口）。**Windows。**

## 已实现
- 检测目标窗口并自动开录（ctypes 窗口枚举 + 尺寸启发式；只在目标窗口在前台时记录，切走即停）
- 单窗口抓帧（`PIL.ImageGrab` 按窗口矩形）
- 事件驱动实时记录（aHash 去重 + 鼠标/键盘事件 + SQLite 落库）
- 本地 qwen3-vl 理解（Ollama OpenAI 兼容端点，逐帧 → 结构化动作）
- 本地仪表盘（会话/时间轴/关键词问答，边录边看）

## 前置
1. 装好 [Ollama](https://ollama.com) 并准备视觉模型。**推荐 32B（质量远胜 8B）**，
   显存/内存够（核显 ≥20GB 可用）就用它；并用 `Modelfile.32b` 把上下文砍到 8K，
   让它 100% 吃进 GPU：
   ```bash
   ollama pull qwen3-vl:32b-instruct
   ollama create sunlens-vl:32b -f Modelfile.32b   # num_ctx=8192，footprint 101GB→21GB、100% GPU
   # 机器较弱可改用 8B（更快、质量略低）：
   #   ollama pull qwen3-vl:8b-instruct
   #   然后 export SUNLENS_VLM_MODEL=qwen3-vl:8b-instruct
   ```
   确保 `ollama serve` 在跑（默认 `http://localhost:11434`）。
2. 打开并聚焦你要记录的目标窗口（默认匹配标题含 `飞牛/fnos/priznas` 的 fnOS NAS 网页会话；
   改别的目标用 `SUNLENS_TARGET_WINDOW_PATTERNS`）。

## 安装（用 uv）
```bash
cd sunlens
uv venv
uv pip install -e .
```

## 用法
```bash
uv run sunlens doctor        # 体检：平台/Ollama/目标窗口/截图
uv run sunlens windows       # 列出当前窗口（确认目标 owner/title 规律）
uv run sunlens peek          # 抓目标窗口一帧 → 本地 qwen3-vl 看懂 → 打印

# 实时记录（事件驱动抓帧 + 输入事件 + SQLite 落库）
uv run sunlens start         # 检测到目标窗口即开始记录，Ctrl+C 或 `sunlens stop` 停止
uv run sunlens stop
uv run sunlens list                 # 列出会话
uv run sunlens frames <session_id>  # 看某会话的帧
uv run sunlens recover              # 把崩溃残留会话标记 recovered

# 理解（帧 + 输入 → 结构化动作）
uv run sunlens understand <session_id>   # 逐帧 → 本地 qwen3-vl → ActionStep
uv run sunlens actions <session_id>      # 看某会话的动作时间轴

# 仪表盘（会话 / 时间轴 / 本地关键词问答）
uv run sunlens serve         # http://127.0.0.1:8088 ，浏览器打开即可；记录时每 3s 自动刷新
```

`peek` 会把截图存到 `~/.sunlens/`，方便肉眼复核。

## 备注
- 截图优先用 `PrintWindow`（抓**窗口自身内容**），目标窗口被别的窗口遮挡/在后台也能正确抓到；
  失败或全黑时回退 `ImageGrab`（截屏幕矩形，需窗口在屏可见）。
- **浏览器标签页的限制**：浏览器只渲染**当前活动标签**，后台标签页抓不到。所以 NAS 跑在浏览器里时，
  需让 NAS 标签页处于活动状态才能记录 —— 这正好等于"只在你操作它时记录"。
- 切到别的标签页/窗口（目标窗口标题不再匹配）超过几秒，会话会自动结束。
- 理解质量取决于本地 `qwen3-vl:8b` 量化模型；可用 `SUNLENS_VLM_MODEL` 换更大的本地模型。
