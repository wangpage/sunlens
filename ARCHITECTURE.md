# 向日葵学徒（SunLens）· 项目架构文档

> 代号：**SunLens**（向日葵学徒）
> 版本：v1.0 — **M0–M5 已实现**（捕获→理解→学习→仪表盘→Studio 插件→语义记忆）
> 平台：macOS（仅）
> 引擎：Qwen-VL 云（DashScope 国内站，`qwen-vl-max`）+ 本地 Whisper + DashScope text-embedding


---

## 0. 文档目的

整个项目的架构与分层设计、各层实现、借鉴三个参考项目的部分、分期落地。
最初是开工前的设计稿，现已随 M0–M5 落地同步更新为**实现版**。当前进度见 §11，
代码目录见 §8，每个能力对应的模块/命令在文中标注。

> **快速上手**（详见 [README.md](README.md)）：
> `pip install -e .` → `export DASHSCOPE_API_KEY=sk-xxx` →
> `sunlens doctor`（体检）→ `sunlens start`（录制）→ `sunlens understand <id>`（理解）→
> `sunlens serve`（仪表盘 http://127.0.0.1:8088）。

---

## 1. 背景与目标

### 1.1 场景
你在用向日葵（SunLogin）作为**主控端**远程连接别人的机器。向日葵窗口里显示的是**对方的远程桌面**，你能看到对方在上面操作。你想要一个工具：

- **实时监测**向日葵窗口里发生了什么（对方点了什么、搜了什么、打开了什么）
- **看懂语义**：不只是「鼠标移动到 (300,400)」，而是「对方在 Chrome 里搜索了『报错 0x80070005 解决』，点开了第二个结果」
- **听懂讲解**：远程会话里对方常边操作边讲（或通话解释思路）。捕获音频并本地转写，与屏幕动作时间对齐，把"为什么这么做"补全（§5.2）
- **精确定位**：不止"大概在搜"，而是"点了第几个结果/哪个按钮"——UI grounding（§5.4）
- **学习能力**：把对方反复出现的工作套路（排查思路、常用工具、搜索习惯、操作序列）抽象成结构化知识
- **四种产出**：① 实时侧边栏解说；② 事后生成「对方工作方法技能手册 / SOP」；③ MCP 接口，让 Claude 直接查询这套方法库；④ 可选：把学到的 SOP 用 pynput 重放执行（§5.7）

### 1.2 目标用户
就是你本人——一个想从远程专家（技术支持、运维、师傅）的操作中学习其工作方法的人。

### 1.3 非目标（本期不做）
- ❌ 不做被控端监控（对方控制你的机器那种场景）
- ⚠️ **回放/执行（§5.7）默认关闭、需显式开启 + 二次确认**。它会真的驱动你的鼠标键盘，只在你主动要"照着对方的 SOP 重做一遍"时启用。
- ⚠️ **早期会把「脱敏后的截图」发给 Qwen-VL 云 API（阿里 DashScope）做理解**（见 §3、§5.3）。发图前必须先在本机把画面里的 PII 区域涂掉。后期切本地 Qwen-VL 时画面完全不出本机。
- ✅ **音频转写全程本地**（Whisper），语音不出本机。
- ❌ 不支持 Windows / Linux（本期仅 macOS）

---

## 2. 三个参考项目，我们各借鉴什么

| 参考项目 | 它是什么 | SunLens 借鉴的部分 |
|---|---|---|
| **screenpipe** | Rust + Tauri 的本地「屏幕记忆」App，事件驱动捕获，SQLite+FTS5 检索，localhost REST API，音频转写，MCP/Pipes | ① **事件驱动捕获**（只在有意义的变化时抓帧，省 CPU）；② **音频捕获 + Whisper 本地转写 + 说话人分离**；③ **SQLite + 全文检索**存储模型；④ **本地 REST API + WebSocket + 仪表盘**形态；⑤ **MCP server**（让 Claude 查询数据）；⑥ Tauri 系统托盘常驻 + 性能控制 |
| **OpenAdapt** | Python 的 GUI 自动化框架，录制演示 → VLM 理解 → 训练 → 执行，含 grounding/retrieval/privacy 子包 | ① **VLM 理解画面 + UI grounding（OmniParser/Set-of-Mark）**；② **演示→知识**的抽象管线（capture→understand→learn）；③ **检索增强**（demo-conditioned）；④ **回放/执行（playback.py 用 pynput 重放动作）**；⑤ 输入事件捕获 + 动作合成；⑥ 模块化子包设计 |
| **openadapt-desktop** | Python 引擎 + Tauri 壳，强调本地存储 + 人在环路 PII 脱敏 + 单一出口闸门 | ① **隐私优先 + 单一出口闸门**；② **PII 脱敏**；③ Python 引擎 + Tauri 壳的**双进程 IPC 架构**；④ 录制状态机 `CAPTURED→SCRUBBED→…` + 崩溃恢复；⑤ 分层存储/审计日志 |

> 一句话：**捕获(屏+声)学 screenpipe，理解/grounding/回放学 OpenAdapt，隐私与工程骨架学 openadapt-desktop。**

---

## 3. 已确认的关键决策（来自甲方）

1. **捕获对象 = 向日葵窗口区域**（不是整屏，不是被控端）
2. **理解引擎 = Qwen-VL，分两期、可插拔**：
   - **早期（M0–M4）= Qwen-VL 云 API（阿里 DashScope，`qwen-vl-max` / `qwen3-vl`）**。它**本身多模态、能直接看截图**，所以管线是 **抓帧 → 本机脱敏涂码 → 把脱敏截图发给 Qwen-VL → 拿到语义描述 + 元素 bbox**。早期就有视觉 grounding，不靠 OCR 当眼睛。OpenAI 兼容调用格式。
   - **后期（P1+）= 同款本地 Qwen2.5-VL（7B/3B，MLX/Ollama）**。同一套 prompt/接口，画面完全不出本机。
   - 理解层做成**统一接口 + 可切换后端**（云/本地），换引擎不动其它层。
3. **最终产物 = 实时仪表盘 + 可复用技能手册 + MCP 查询接口**（前两个 + 把方法库接成 MCP 让 Claude 直接问）
4. **平台 = 仅 macOS**
5. **扩展能力（甲方确认全要，纳入路线图）**：
   - **音频 + 本地转写**：捕获远程会话音频，Whisper 本地转文字 + 说话人分离，与屏幕动作时间对齐。语音不出本机。
   - **UI grounding**：OmniParser / Set-of-Mark 精确定位元素，理解从"大概在搜"升级到"点了第几个结果"。
   - **回放 / 执行**：把学到的 SOP 用 pynput 重放执行。**默认关闭、需显式开启 + 二次确认**（会真的动你的鼠标键盘）。
   - 这三项分别落在 §5.2 / §5.4 / §5.7，里程碑见 §11。

---

## 4. 总体架构

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              SunLens (macOS)                                 │
│                                                                              │
│  ┌────────────────┐  IPC(JSON/stdio)  ┌──────────────────────────────────┐  │
│  │  Tauri 壳 (Rust)│ <───────────────> │        Python 引擎 (sidecar)      │  │
│  │  系统托盘        │                   │                                  │  │
│  │  实时仪表盘 WebUI │                   │  ┌────────────────────────────┐  │  │
│  │  开关/审阅/导出   │                   │  │ 1. 捕获层 Capture            │  │  │
│  └────────────────┘                   │  │  ·向日葵窗口 事件驱动抓帧(5.1) │  │  │
│         localhost:8088 REST/WS         │  │  ·音频捕获+Whisper转写  (5.2) │  │  │
│  ┌────────────────┐                   │  │  ·你的输入事件(pynput)        │  │  │
│  │ 浏览器/仪表盘    │ <── WS 实时推送 ──│  └──────────────┬─────────────┘  │  │
│  └────────────────┘                   │                 ▼                │  │
│  ┌────────────────┐   MCP (stdio)     │  ┌────────────────────────────┐  │  │
│  │ Claude / Cursor │ <───────────────> │  │ 3. 隐私层 Privacy 出口闸门(5.3)│  │  │
│  │  查方法库         │   (mcp/ 模块)     │  │  ·PII 涂码  ·音频PII         │  │  │
│  └────────────────┘                   │  └──────────────┬─────────────┘  │  │
│                                       │                 ▼                │  │
│                                       │  ┌────────────────────────────┐  │  │
│                                       │  │ 4. 理解层 Understand    (5.4) │  │  │
│                                       │  │  ·Qwen-VL 解析画面            │  │  │
│                                       │  │  ·UI grounding(OmniParser)    │  │  │
│                                       │  │  ·屏+声对齐 → 动作/意图        │  │  │
│                                       │  └──────────────┬─────────────┘  │  │
│                                       │                 ▼                │  │
│                                       │  ┌────────────────────────────┐  │  │
│                                       │  │ 5. 学习层 Learn         (5.5) │  │  │
│                                       │  │  ·会话切分/聚类 ·SOP ·RAG     │  │  │
│                                       │  └──────────────┬─────────────┘  │  │
│                                       │                 ▼                │  │
│                                       │  ┌────────────────────────────┐  │  │
│                                       │  │ 6. 存储 SQLite+FTS5+向量      │  │  │
│                                       │  │    帧/转写/动作/技能手册        │  │  │
│                                       │  └──────────────┬─────────────┘  │  │
│                                       │                 ▼ (可选,默认关)    │  │
│                                       │  ┌────────────────────────────┐  │  │
│                                       │  │ 7. 执行层 Execute       (5.7) │  │  │
│                                       │  │  ·pynput 重放学到的 SOP       │  │  │
│                                       │  │  ·二次确认 + 安全护栏          │  │  │
│                                       │  └────────────────────────────┘  │  │
│                                       └──────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘

实时链路:  捕获(屏+声) →(脱敏)→ 理解(VLM+grounding) → WS 推送 → 仪表盘解说
离线链路:  存储的事件流 → 学习层批处理 → 技能手册 / 向量库
查询链路:  Claude ──MCP──> 方法库（搜历史、问"对方怎么排查X"）
执行链路:  选定 SOP →(二次确认)→ pynput 重放    [可选, 默认关闭]
```

---

## 5. 分层设计

### 5.1 捕获层 Capture · 屏幕（借鉴 screenpipe 的事件驱动）

**职责**：检测向日葵是否运行 → 定位向日葵窗口 → 只对该窗口区域做事件驱动抓帧。

- **进程/窗口检测**
  - 向日葵 macOS 客户端进程名实测为 **`AweSun`**（公司 Oray/贝锐；M0 已确认，配置可改）。用 `psutil` 轮询 + macOS `CGWindowListCopyWindowInfo` 拿到窗口 owner/标题/坐标/尺寸。
  - 识别「正在远程会话中」的窗口：当前用"最大 layer0 向日葵窗口"启发式选窗；真实远程会话上需再校准（§12）。
- **抓帧引擎**
  - **M0 用 Quartz `CGWindowListCreateImage`** 按 windowID 单窗口截图（已跑通）。M1 高频抓帧若开销大，再换 **ScreenCaptureKit**（持久 SCStream）。
  - **事件驱动**（不连续录，借 screenpipe）：默认 1–2 fps 轮询做轻量帧差（感知哈希 pHash + 直方图，hash 早退），仅当画面变化超阈值才落「关键帧」；点击/键盘活动立即补抓一帧；debounce ≥200ms；focus-aware 三态省 CPU。目标 CPU < 10%。
- **附加信号**
  - 远控窗口内部是「视频流」，**拿不到对方机器的无障碍树**（与 screenpipe 录本机的根本差异：只有像素）。理解靠 Qwen-VL 直接读像素 + UI grounding（§5.4）。
  - 本机侧能拿到：你自己的鼠标/键盘事件（你在主控端的输入会转发给对方机器）、窗口焦点变化、时间戳。

**产出**：`Frame`（关键帧 JPEG + 时间戳 + 帧差分数 + 你的输入事件）。

### 5.2 捕获层 Capture · 音频 + 转写（借鉴 screenpipe-audio）

**职责**：捕获远程会话期间的音频，本地转成文字，与屏幕动作时间对齐——把对方"边做边讲"的解释补进来。**全程本地，语音不出本机。**

- **采集**：系统音频（向日葵语音/对方端声音）+ 麦克风（你这侧）。用 macOS ScreenCaptureKit 音频 / CoreAudio（系统音频需虚拟声卡或 SCK audio tap）+ 麦克风设备。分轨录，便于区分你 vs 对方。
- **转写**：**本地 Whisper**（`faster-whisper` / `whisper.cpp` / MLX-Whisper，Apple Silicon 优先 MLX），中文模型。流式/分块转写，低延迟。
- **说话人分离**：轻量 diarization（或直接用"系统音频=对方 / 麦克风=你"的分轨先验，省一个模型）。
- **时间对齐**：转写片段带起止时间戳，学习层据此把"这句话"绑到"这个动作"上（对方说"先看事件查看器"+ 同时打开 eventvwr）。

**隐私**：音频转写出的文本同样过隐私层做 PII 过滤（§5.3）。Whisper 本地跑，音频本身不外发。

**产出**：`Transcript { 时间起止, 说话人(你/对方), 文本, 置信度 }`。

### 5.3 隐私层 Privacy（借鉴 openadapt-desktop 的出口闸门）

⚠️ **早期阶段出口闸门是「真生效」而非未来防护，且管的是「图」**：因为脱敏截图要发给 Qwen-VL 云端，所以**每一张离开本机的图都必须先在本机把 PII 区域涂掉**。远程画面可能含密码、工单号、客户信息、身份证、密钥等，发云端前和沉淀进技能手册前都必须脱敏。这里 **OCR 的主要职责变成「定位 PII 在画面哪个区域」**，好让我们对那块像素打码——而不是当理解的眼睛（眼睛交给 Qwen-VL）。

- **状态机**（沿用 openadapt-desktop 思路）：
  `CAPTURED`（原始帧，仅内存/临时）→ `SCRUBBED`（已脱敏）→ `REVIEWED`（你确认可入库）→ 入技能手册。
- **脱敏手段**：
  - OCR 文本过 Presidio/正则，命中 PII 类型（密码框、邮箱、手机号、卡号、密钥）→ 在帧上打码 + 文本替换为 `<REDACTED:类型>`。
  - 密码输入框特殊处理：检测到聚焦密码框时该帧直接丢弃或全黑。
- **单一出口闸门** `check_export_allowed()`：**任何外发（发 Qwen-VL API 的图）/ 写盘持久化 / 技能手册导出**都必须过这一关，确保未脱敏画面既不上云也不落盘到长期库（原始帧默认 N 小时后自动清理）。

**产出**：`ScrubbedFrame`（已涂码的脱敏截图，唯一允许外发的东西）+ PII 审计日志（JSONL）。

### 5.4 理解层 Understand（借鉴 OpenAdapt 的 VLM + grounding）

**职责**：把「一串关键帧 + 你的输入事件 + 对齐的语音转写」翻译成**人类可读的语义动作**。

**统一接口，双后端可切换**（`UnderstandBackend` 协议，借 openadapt 的 protocol 模式；两后端是**同一个 Qwen-VL，只是云/本地之别**，prompt 复用）：

- **后端 A · Qwen-VL 云 API（早期，M0–M4 默认）**
  - 多模态，直接吃图。输入：**脱敏截图 + 你的点击坐标 + 当前应用/窗口标题 + 时间戳 + RAG 检索到的历史片段**。
  - 输出：描述对方在干什么（「在 Chrome 搜索框输入了『0x80070005 解决』并点了第二条结果」）、推断意图（「在排查权限报错」）、关键元素 bbox、解说与 SOP 摘要。
  - 模型：`qwen-vl-max`（默认，效果好）/ `qwen3-vl`；DashScope OpenAI 兼容端点，Key 从 `DASHSCOPE_API_KEY` 读。
  - **优势 vs DeepSeek 方案**：早期就有真·像素视觉，图标/图形界面也能懂，grounding 更准，少了一层「OCR 当眼睛」。
- **后端 B · 本地 Qwen-VL（后期，P1+）**
  - **Qwen2.5-VL（7B 优先，显存不够退 3B）**，经 Ollama / MLX-VLM 跑本机。同一套 prompt，画面完全不出本机。
- **UI grounding（借 OpenAdapt，提升定位精度）**：在发给 VLM 前用 **OmniParser / Set-of-Mark** 本地检测画面上的可交互元素（按钮/输入框/链接/列表项），给它们编号标注。Qwen-VL 引用编号 → 把"大概在搜东西"升级到"点了第 2 个搜索结果""按了『确定』按钮"。元素检测本地跑，只把标注后的脱敏图上云。
- **OCR（辅助，常驻本地）**：**Apple Vision**（macOS 原生，中文好）主要用于**定位 PII 区域做涂码**（见 §5.3）+ 精确文本交叉校验，不再是理解的主力。
- **动作推断**：结合你的输入事件时间戳 + 对齐的语音，把「关键帧变化 + 点击位置 + grounding 元素 + Qwen-VL 语义 + 同时段对方说的话」合成结构化 `ActionStep`。

**产出**：`ActionStep { 时间, 类型(点击/输入/搜索/打开/切换), 目标(应用/元素编号/文本), bbox, 旁白(对方同时说的话), 自然语言描述, 置信度 }`

### 5.5 学习层 Learn（借鉴 OpenAdapt 的 demo→知识 + 检索增强）

**职责**：从一连串 `ActionStep`（含语音旁白）里抽象出「对方的工作方法」。

- **会话切分**：按时间间隔 + 应用切换 + 任务边界，把连续操作切成「任务片段」（如「一次完整的故障排查」）。
- **工作流归纳**：对每个任务片段，用 Qwen-VL（早期云、后期本地）把动作流总结成：目标 → 步骤序列 → 用到的工具 → 搜索关键词 → 决策点（为什么这么做）。
- **能力聚类**：跨多次会话，把相似任务聚类，提炼对方的「招式」：常用工具集、搜索习惯、排错套路、快捷键偏好。
- **检索增强（RAG）**：把历史 `ActionStep`/任务向量化存入向量库；理解新画面时检索相似历史片段，提升解说准确度（这正是 OpenAdapt「demo-conditioned」的核心思想）。

**产出**：
- 实时：`LiveNarration`（推给仪表盘的一句话解说 + 置信度）
- 离线：`SkillManual`（结构化技能手册，见 §6.3）

### 5.6 产物层（三个出口）

#### A. 实时仪表盘（借鉴 screenpipe 的本地 WebUI）
- Tauri WebView / 浏览器访问 `localhost:8088`。
- 左侧：向日葵窗口的实时缩略图（已脱敏）。
- 右侧：实时解说流（「对方正在……」）+ 语音转写流，带置信度、关键词高亮、时间线。
- 顶部：录制开关、当前会话状态、CPU/显存占用。

#### B. 技能手册 / SOP（事后沉淀）
- 每个任务片段生成一篇 Markdown：标题、目标、前置条件、步骤（含脱敏截图 + 对方当时的讲解）、用到的工具、搜索词、要点/坑。
- 汇总页：「对方的工作方法画像」（高频工具、典型排错路径、搜索习惯统计）。

#### C. MCP 查询接口（借鉴 screenpipe-mcp）
- 把方法库（SQLite + FTS5 + 向量 + 技能手册）暴露成一个 **MCP server**（`mcp/` 模块，stdio）。
- 在 Claude Desktop / Cursor 里 `claude mcp add sunlens` 后，你能直接问：「对方一般怎么排查打印机离线？」「上次那个 0x80070005 是怎么解决的？」「对方最常用哪些工具？」
- 工具集：`search_history(query)`、`get_skill_manual(topic)`、`list_workflows()`、`profile_summary()`。
- 让学到的知识**可被调用**，而不只是躺在手册里。

### 5.7 执行层 Execute · 可选，默认关闭（借鉴 OpenAdapt playback.py）

**职责**：把学到的 SOP / 录下的动作序列，用 **pynput** 在你这台机器上**重放执行**（你照着对方的方法重做一遍）。

- **动作回放**：读 `action_step` 序列，用 `pynput` 的 mouse/keyboard Controller 重放点击/输入/快捷键（OpenAdapt `playback.py` 现成模式）。
- **坐标自适应**：远控画面坐标≠你本机坐标。重放需结合 UI grounding 在当前画面**重新定位元素**（找到"那个『确定』按钮"现在在哪），而非死板回放绝对坐标。
- **安全护栏（硬性）**：
  - 默认**完全关闭**，配置 + 命令双重显式开启。
  - 每次执行前弹**二次确认**，逐步执行可暂停/中止（紧急停止热键）。
  - 危险动作（删除/格式化/发送/支付类按钮）识别到就**暂停等人工确认**。
  - 执行全程录屏留证。
- **定位**：本期先做"半自动"——把 SOP 步骤念给你 + 高亮下一步该点哪，你来点；全自动重放作为该层的进阶项。

**产出**：`Replay { sop_id, 步骤执行结果, 截图留证 }`。

---

## 6. 数据模型（SQLite）

```sql
-- 一次向日葵远程会话
session(id, started_at, ended_at, remote_label, host_window_title)

-- 关键帧
frame(id, session_id, ts, image_path, phash, diff_score, scrub_state)

-- 音频转写片段（本地 Whisper，§5.2）
transcript(id, session_id, ts_start, ts_end, speaker, text, confidence)

-- 你在主控端的输入事件
input_event(id, session_id, ts, kind, x, y, key, text)

-- UI grounding 检测到的元素（§5.4）
ui_element(id, frame_id, mark_no, kind, label, bbox)

-- 理解层产出的结构化动作（narration=同时段对方说的话）
action_step(id, session_id, ts, type, target_app, target_element_id, target_text,
            bbox, narration, nl_description, confidence, frame_id)

-- 执行层回放记录（§5.7）
replay(id, sop_id, ts, step_results_json, evidence_path, status)

-- 学习层切分出的任务片段
task_segment(id, session_id, start_ts, end_ts, goal, tool_set, summary)

-- 技能手册条目
skill_manual(id, task_segment_id, title, markdown_path, created_at, export_state)

-- PII 审计
pii_audit(id, frame_id, ts, pii_type, action)

-- 向量检索（FTS5 + 向量列 / sqlite-vss）
action_fts(action_step_id, text)        -- 全文检索
action_vec(action_step_id, embedding)   -- 语义检索
```

---

## 7. 技术栈

| 层 | 选型 | 备注 |
|---|---|---|
| 引擎 | **Python 3.11+** | 与 OpenAdapt / openadapt-desktop 一致，复用其 capture/privacy 思路 |
| 外壳/托盘/UI | **Tauri 2.x (Rust) + 轻量 Web 前端** | 与两个 desktop 项目一致；本期可先用纯浏览器仪表盘，Tauri 壳作为 P1 |
| 进程通信 | **JSON over stdio（IPC）+ localhost REST/WS** | sidecar 模式，借 openadapt-desktop |
| 屏幕捕获 | **Quartz CGWindowListCreateImage**（M0 已用）→ **ScreenCaptureKit**（M1+）| 指定窗口捕获，macOS 原生 |
| 音频捕获 | **ScreenCaptureKit audio / CoreAudio**（pyobjc）| 系统音频 + 麦克风分轨 |
| 语音转写 | **本地 Whisper**（MLX-Whisper / faster-whisper）| 中文；语音不出本机 |
| 理解 VLM（早期） | **Qwen-VL 云 API**（DashScope，`qwen-vl-max` 默认 / `qwen3-vl`）| 多模态直接吃图；OpenAI 兼容；Key=`DASHSCOPE_API_KEY` |
| 理解 VLM（后期） | **Qwen2.5-VL 7B/3B**，经 Ollama 或 MLX-VLM | 同款本地化，Apple Silicon 优先用 MLX |
| UI grounding | **OmniParser / Set-of-Mark**（本地）| 元素检测+编号，提升定位精度 |
| OCR（辅助） | **Apple Vision**（pyobjc）/ PaddleOCR | 本地；主要用于 PII 定位涂码 + 文本校验 |
| 回放执行 | **pynput**（mouse/keyboard Controller）| 借 OpenAdapt playback；默认关闭 |
| 查询接口 | **MCP server**（stdio）| 借 screenpipe-mcp，接 Claude |
| 存储 | **SQLite + FTS5 (+ sqlite-vss 向量)** | 借 screenpipe |
| PII | **Apple Vision OCR 定位 + 正则**（M0）→ **Presidio**（增强）| 借 openadapt-privacy |
| CLI | **argparse**（M0 已用）| 借 openadapt-desktop |

---

## 8. 目录结构（拟）

```
sunlens/
├── ARCHITECTURE.md            # 本文档
├── pyproject.toml             # uv / hatchling
├── engine/                    # Python 引擎
│   ├── cli.py                 # CLI 入口 (start/stop/list/export/doctor)
│   ├── main.py                # 双模式入口：CLI 或 Tauri sidecar(IPC)
│   ├── config.py              # Pydantic 设置 (SUNLENS_ 前缀)
│   ├── capture/
│   │   ├── detector.py        # 向日葵进程/窗口检测            [M0✓]
│   │   ├── window_grabber.py  # Quartz/ScreenCaptureKit 窗口抓帧 [M0✓]
│   │   ├── audio.py           # 系统音频+麦克风采集            [M2]
│   │   ├── transcribe.py      # 本地 Whisper 转写+说话人分离    [M2]
│   │   └── input_hook.py      # 主控端鼠标/键盘事件(pynput)    [M1]
│   ├── privacy/
│   │   ├── scrubber.py        # PII 涂码(OCR定位)             [M0✓]
│   │   └── gate.py            # check_egress_allowed 出口闸门  [M0✓]
│   ├── understand/
│   │   ├── ocr.py             # Apple Vision（辅助：PII定位）  [M0✓]
│   │   ├── backend.py         # UnderstandBackend 协议        [M0✓]
│   │   ├── qwen_cloud.py      # 后端A：Qwen-VL DashScope      [M0✓]
│   │   ├── qwen_local.py      # 后端B：本地 Qwen2.5-VL         [P1]
│   │   ├── grounding.py       # OmniParser/Set-of-Mark 元素检测 [M4]
│   │   └── action_builder.py  # 帧+输入+语音+grounding→ActionStep [M3]
│   ├── learn/
│   │   ├── segmenter.py       # 会话/任务切分                  [M5]
│   │   ├── workflow.py        # 工作流归纳 → SOP              [M5]
│   │   ├── retrieval.py       # RAG 检索增强                  [M5]
│   │   └── manual.py          # 技能手册生成                  [M5]
│   ├── execute/
│   │   └── replayer.py        # pynput 重放 SOP(默认关闭)     [M7]
│   ├── mcp/
│   │   └── server.py          # MCP server(接 Claude)        [M6]
│   ├── store/
│   │   ├── db.py              # SQLite schema + DAO           [M1]
│   │   └── vectors.py         # 向量索引                      [M5]
│   ├── server/
│   │   ├── api.py             # REST + WebSocket (localhost:8088) [M4]
│   │   └── ipc.py             # Tauri sidecar 协议            [M8]
│   └── __init__.py
├── dashboard/                 # 实时仪表盘前端                [M4]
├── src-tauri/                 # Tauri 壳                     [M8]
├── skills/                    # 生成的技能手册输出目录
└── tests/
```

---

## 9. 关键流程

### 9.1 实时链路（在线解说）
```
托盘开启 → detector 发现 AweSun 远控窗口 → 创建 session
  ├─ window_grabber 事件驱动抓关键帧 ┐
  └─ audio + 本地 Whisper 转写        ┘ 时间对齐
  → scrubber 即时脱敏(屏幕 PII 涂码 + 转写文本 PII)
  → grounding 标元素 → qwen-vl 解析 → action_builder 合成 ActionStep(带语音旁白)
  → retrieval 拉相似历史增强 → 生成 LiveNarration
  → WebSocket 推送到仪表盘右侧解说流 + 转写流
```

### 9.3 查询链路（MCP）
```
Claude/Cursor ──MCP──> sunlens server
  → search_history / get_skill_manual / list_workflows / profile_summary
  → 在 SQLite+FTS5+向量库里检索 → 返回结构化结果给 Claude
```

### 9.4 执行链路（可选，默认关闭）
```
你选定一个 SOP → execute.replayer 读 action_step 序列
  → 每步用 grounding 在当前画面重新定位元素 →(二次确认)→ pynput 重放
  → 危险动作暂停等确认 → 全程录屏留证
```

### 9.2 离线链路（沉淀技能手册）
```
session 结束（远控窗口关闭） → segmenter 把 ActionStep 切成 task_segment
  → workflow 用本地 LLM 归纳每段为 SOP
  → gate.check_export_allowed 确认全脱敏
  → manual 生成 Markdown + 脱敏截图 → 写入 skills/
  → 更新「对方工作方法画像」汇总页
```

---

## 10. macOS 权限与向日葵识别（落地前置）

- **系统权限**：屏幕录制（Screen Recording）、辅助功能（Accessibility，用于读窗口信息和你的输入）、输入监控（Input Monitoring）。首启引导用户去「系统设置 > 隐私与安全性」授权。
- **向日葵窗口识别**：需实测向日葵 mac 客户端的进程名与远控窗口标题规律（§12 第一件事就是验证）。设计上做成**可配置匹配规则**（进程名/标题正则），避免向日葵改版就失效。
- **像素 only 约束**：再次强调——远控窗口内是视频流，**没有对方机器的 a11y 树**，理解层靠 Qwen-VL 直接读像素。这是本项目与 screenpipe（录本机有 a11y）的根本差异，已在选型中接受。

---

## 11. 分期里程碑

| 阶段 | 目标 | 交付 / 命令 | 状态 |
|---|---|---|---|
| **M0 可行性** | 检测远控窗口 + Quartz 单窗口抓帧 + OCR 涂码 + Qwen-VL 看懂一帧 | `doctor`/`windows`/`peek` | ✅ 已实现 |
| **M1 捕获+存储** | 事件驱动抓帧(pHash去重)、输入事件(pynput)、SQLite 落库、崩溃恢复 | `start`/`stop`/`list`/`frames`/`recover` | ✅ 已实现 |
| **M2 音频+转写** | 系统音频+麦克风分轨采集、本地 Whisper 转写、说话人(分轨)、时间对齐 | `audio-devices`/`transcribe`/`transcript` | ✅ 已实现 |
| **M3 理解** | Qwen-VL + 屏(帧)+声(转写)+输入 对齐融合 → ActionStep | `understand`/`actions` | ✅ 已实现 |
| **M4 实时仪表盘** | NotebookLM 三栏 + 时间轴 + 提问区(本地/全网/深研/文生图) | `serve`（localhost:8088，轮询刷新）| ✅ 已实现 |
| **M5 Studio 插件 + 技能手册** | 9 个插件(报告/数据表/视频/音频/思维导图/闪卡/测验/信息图/演示)，多来源勾选 | 仪表盘右侧卡片 + 左下手册 | ✅ 已实现 |
| **语义记忆 / RAG** | 动作/转写/手册向量化(DashScope embedding)，语义检索 + demo-conditioned 增强 | `index`/`ask` + 本地问答升级 | ✅ 已实现 |
| **M6 MCP 查询** | MCP server 暴露方法库（semantic_search/manual/workflows/profile）| Claude 里问"对方怎么排查X" | ⏳ 下一步 |
| **M7 执行（可选）** | pynput 重放 SOP + grounding 重定位 + 二次确认/护栏 | 半自动"照着重做一遍" | ⏳ 默认关 |
| **M8 Tauri 壳** | 系统托盘常驻、权限引导、打包 | 双击即用的 .app | ⏳ |
| **P1（演进）** | 理解后端从云 Qwen-VL 切到**本地 Qwen-VL**，画面完全不出本机 | 切 backend，其它层不动 | ⏳ |

> M0–M5 + RAG 已落地并逐块测试（细节见 commit 历史与 README）。无 key 的部分（捕获/落库/数据表/视频/say 合成）完整跑通；需 key 的 LLM 部分用 mock + 路由验证接线正确，真实效果需配 `DASHSCOPE_API_KEY` 实跑。
> **下一步建议 M6 MCP**——语义记忆已是它的底座。

---

## 12. 风险与待验证项

1. **向日葵进程名/窗口标题**：需实测（你机器上向日葵已装？版本？）。
2. **隐私：脱敏截图要上云到 DashScope —— 最大风险点**。涂码漏一处（密码、身份证、密钥）就是敏感画面外泄。所以 **PII 涂码必须在发图前、在本机完成，宁可多涂**。M2 专门测涂码召回率；介意的会话可一键切「本地 only」（P1 后端）。OCR 对中文小字的漏检会直接变成涂码漏判——M0 就要在真实远控画面上实测 Apple Vision 的 PII 命中率。
3. **远控无 a11y**：纯像素，没有对方机器控件树。好在 Qwen-VL 直接看像素，理解和 grounding 不依赖控件树；精确点击坐标对齐仍靠 VLM bbox + 你的输入坐标近似。
4. **实时延迟与成本**：每关键帧走 涂码→上传图→Qwen-VL 一个来回，传图比传文本慢，延迟可能 >2–3s。需异步队列/降帧/合批/缩图。DashScope 按图+token 计费，长会话成本高于纯文本方案，需在 config 设预算/采样率/最长边压缩。
5. **向日葵自带画面压缩/低帧率**：远控画面本身可能模糊/卡顿，影响 VLM 读小字。
6. **音频采集（M2）**：macOS 系统音频不能直接抓，需 ScreenCaptureKit audio tap（14+）或虚拟声卡（BlackHole）。麦克风要「麦克风」权限。Whisper 本地跑吃 CPU/内存——和 Mac 配置相关。语音转写文本同样要过脱敏。
7. **UI grounding（M4）成本**：OmniParser 本地推理有开销/显存需求；元素检测慢会拖累实时性。可降频（只对落库的关键帧做 grounding，不是每帧）。
8. **回放/执行（M7）安全**：这是最危险的能力——会真的动你的鼠标键盘。远控坐标≠本机坐标，盲目回放绝对坐标会乱点。必须 grounding 重定位 + 二次确认 + 危险动作拦截 + 紧急停止热键。默认完全关闭。
9. **法律/伦理边界**：本工具录的是**你自己屏幕上**的画面/声音、用于**你个人学习**。这是正当用途。截图会过阿里云（语音不会，本地转写），含对方敏感信息的会话靠涂码兜底；若日后要把手册分享给他人，需对方知情同意。

---

## 13. 已敲定的关键决策（开发过程中确认）

1. **引擎**：DashScope **国内站** + `qwen-vl-max`（理解）/ `qwen-plus`·`qwen-max`（联网问答/研究）/ `wan2.2-t2i-flash`（文生图）/ `text-embedding-v3`（语义记忆）。全部复用 `DASHSCOPE_API_KEY`，从环境变量读，不入库。
2. **向日葵**：v16.2.0.27059，mac 进程名实测 **`AweSun`**（Oray/贝锐），自带虚拟声卡 `OrayVirtualAudioDevice` 可作"对方"音轨。
3. **录制触发**：检测到远控窗口**自动开录**（可暂停）。
4. **解说语言**：中文。
5. **隐私姿态**：截图脱敏后发云端（万相/Qwen-VL）；语音转写全程本地；语义向量本地存储；出口闸门在发图前生效。

> 待定/演进项：P1 本地 Qwen-VL 取决于你的 Mac 芯片/内存；音频概览中文 TTS 需系统装中文语音（`SUNLENS_TTS_VOICE`）。

---

## 14. 当前实现一览（截至本次提交）

| 层 | 模块 | 状态 |
|---|---|---|
| 捕获·屏幕 | `engine/capture/{detector,window_grabber,framediff,recorder}.py` | ✅ |
| 捕获·音频 | `engine/capture/{audio,transcribe,audio_session}.py` | ✅ |
| 捕获·输入 | `engine/capture/input_hook.py` | ✅ |
| 隐私 | `engine/privacy/{scrubber,gate}.py` + `understand/ocr.py` | ✅ |
| 理解 | `engine/understand/{backend,qwen_cloud,action_builder}.py` | ✅（grounding 待 M-grounding）|
| 存储 | `engine/store/db.py`（session/frame/input/transcript/action/manual/vector）| ✅ |
| 学习/产物 | `engine/studio/{base,plugins}.py`（9 插件）| ✅ |
| 提问助手 | `engine/assist/{web,image}.py`（全网/深研/文生图）| ✅ |
| 语义记忆 | `engine/memory/{embed,index,search}.py` | ✅ |
| 仪表盘 | `engine/server/app.py` + `static/index.html`（三栏 + 勾选 + Studio + RAG）| ✅ |
| CLI | `engine/cli.py`（doctor/start/understand/serve/index/ask …）| ✅ |
| 理解本地化 P1 | `understand/qwen_local.py` | ⏳ |
| MCP / 执行 / Tauri | `mcp/` `execute/` `src-tauri/` | ⏳ M6/M7/M8 |

> 测试方式：无 key 路径（捕获/落库/数据表/视频/say/帧差/DB/语义检索逻辑）用真实数据或假向量**全测**；需 key 的云调用（Qwen-VL/嵌入/联网/文生图）用 mock 验证 payload 与路由，真实效果配 `DASHSCOPE_API_KEY` 实跑。浏览器渲染因沙箱限制未截图，用户本机 `sunlens serve` 自验。
