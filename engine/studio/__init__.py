"""Studio 插件系统：每个仪表盘右侧卡片 = 一个插件。

插件输入 = 一个已录会话的 动作/语音/帧；输出 = 某种产物（SOP/表格/导图/卡片/音视频…）。
统一接口 + 共享基建（digest + qwen_text），每个插件只需写 prompt/渲染。
"""

from engine.studio.plugins import PLUGINS, run_plugin  # noqa: F401
