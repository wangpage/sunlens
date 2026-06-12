# 向日葵学徒 · SunLens（M0）

监测向日葵远控窗口 → 用 Qwen-VL（DashScope 云）看懂对方在做什么 → 学习其工作方法。
架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。**仅 macOS。**

## M0 已实现
- 检测向日葵进程与远控窗口（Quartz 窗口枚举 + 尺寸启发式）
- 单窗口抓帧（Quartz `CGWindowListCreateImage`）
- 本机 PII 涂码（Apple Vision OCR 定位 + 矩形涂黑，发图前生效）
- 出口闸门（未脱敏禁止外发）
- Qwen-VL 云理解（DashScope `qwen-vl-max`，OpenAI 兼容端点）

## 安装

```bash
cd sunlens
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export DASHSCOPE_API_KEY=sk-xxx        # 或 cp .env.example .env 后填入
```

首次需在「系统设置 > 隐私与安全性」给你的终端/IDE 授予 **屏幕录制** 权限。

## 用法

```bash
# M0 探针
sunlens doctor       # 体检：依赖/权限/向日葵窗口/API Key 是否就绪
sunlens windows      # 列出当前窗口（确认向日葵 owner/title 规律）
sunlens peek         # 抓远控窗口一帧 → 涂码 → Qwen-VL 看懂 → 打印

# M1 录制（事件驱动抓帧 + 输入事件 + SQLite 落库）
sunlens start        # 检测到远控窗口即开录，Ctrl+C 或 `sunlens stop` 停止
sunlens stop
sunlens list                 # 列出会话
sunlens frames <session_id>  # 看某会话的帧
sunlens recover              # 把崩溃残留会话标记 recovered

# M2 音频 + 本地 Whisper 转写（录制时自动并行）
sunlens audio-devices        # 列出输入设备 + 角色映射(麦克风=你, loopback=对方)
sunlens transcribe <wav>     # 测试转写一个 WAV
sunlens transcript <session_id>   # 看某会话的转写

# M3 理解（帧+输入+语音 → 结构化动作）
sunlens understand <session_id>   # 逐帧脱敏 → Qwen-VL → ActionStep（需 DASHSCOPE_API_KEY）
                                  #   理解时检索相似历史增强，理解完自动建语义索引
sunlens actions <session_id>      # 看某会话的 ActionStep（对方做了什么+当时说了什么）

# 语义记忆 / RAG（DashScope text-embedding，同一个 Key）
sunlens index --all               # 把所有会话动作/转写向量化
sunlens ask "对方怎么排查打印机"   # 语义检索（没原词也能命中）
# 仪表盘「📒 本地问答」也自动用语义检索（无 key 时退回关键词）

# M4 实时仪表盘（NotebookLM 三栏：来源/技能手册 · 总结/时间轴/问答 · Studio）
sunlens serve              # 启动 http://127.0.0.1:8088 ，浏览器打开即可；录制时每 3s 自动刷新
```

提问区四种模式（后三种复用同一个 `DASHSCOPE_API_KEY`）：
- 📒 本地问答：搜本机录制历史（动作/转写）
- 🌐 全网搜索：qwen + 联网增强（`enable_search`）
- 🔬 深度研究：qwen-max + 研究式 prompt + 联网
- 🎨 图片生成：通义万相文生图（异步任务）

### Studio 插件（M5）—— 右侧每张卡片 = 一个插件 [engine/studio/plugins.py](engine/studio/plugins.py)
对**当前选中会话**的 动作+语音+帧 生成产物：

| 卡片 | 产物 | 引擎 |
|---|---|---|
| 数据表格 | 动作流表格 | 纯数据，无需 key |
| 视频概览 | 帧+解说幻灯片 | 纯数据，无需 key |
| 报告/SOP | 可复用技能手册→存库+左下栏 | Qwen |
| 音频概览 | 口播稿→macOS say 合成音频 | Qwen + 本地 TTS |
| 思维导图 | 工作流 mermaid 图 | Qwen |
| 闪卡 / 测验 | 学习卡片 / 测验题 | Qwen |
| 信息图 / 演示文稿 | HTML 信息图 / 幻灯片大纲 | Qwen |

技能手册沉淀在左下栏，点开看 markdown。音频概览中文需系统装中文语音（`SUNLENS_TTS_VOICE=Tingting`）。

`peek` 会把**脱敏后**的截图存到 `~/.sunlens/`，方便你肉眼复核涂码是否干净。
录制时音频自动采集 + 本地 Whisper 转写（语音不出本机），对方声音走向日葵虚拟声卡 `OrayVirtualAudioDevice`。

## 路线图（见 ARCHITECTURE.md §11）
M1 连续捕获+落库 → M2 音频+本地 Whisper 转写 → M3 理解(grounding+屏声对齐) →
M4 实时仪表盘 → M5 学习+技能手册 → M6 MCP 查询 → M7 回放执行(默认关) → M8 Tauri 壳。
