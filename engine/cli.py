"""SunLens CLI（M0）。

命令：
  sunlens doctor   —— 体检：平台/依赖/权限/向日葵窗口/API Key
  sunlens windows  —— 列出当前所有窗口（调试用，找向日葵 owner/title 规律）
  sunlens peek      —— M0 demo：抓远控窗口一帧 → 涂码 → Qwen-VL 看懂 → 打印

借 openadapt-desktop/engine/cli.py 的 argparse 子命令 + 字典调度模式。
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import datetime, timezone

from loguru import logger

from engine.config import SunLensConfig, load_config

_OK = "✅"
_BAD = "❌"
_WARN = "⚠️ "


def _now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d_%H-%M-%S")


# ----------------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace, config: SunLensConfig) -> None:
    print("SunLens 体检\n" + "=" * 40)

    # 1. 平台
    is_mac = platform.system() == "Darwin"
    print(f"{_OK if is_mac else _BAD} 平台: {platform.system()} {platform.mac_ver()[0]}")
    if not is_mac:
        print("   本工具仅支持 macOS，后续检查跳过。")
        return

    # 2. 依赖
    try:
        import Quartz  # noqa: F401

        print(f"{_OK} pyobjc Quartz 可用（窗口枚举/截图）")
    except Exception as e:
        print(f"{_BAD} pyobjc Quartz 不可用：{e}")
    try:
        import Vision  # noqa: F401

        print(f"{_OK} Apple Vision 可用（OCR/PII 定位）")
    except Exception as e:
        print(f"{_WARN}Apple Vision 不可用：{e}（脱敏将退化为不涂码）")

    # 3. API Key
    if config.dashscope_api_key:
        masked = config.dashscope_api_key[:6] + "…" + config.dashscope_api_key[-4:]
        print(f"{_OK} DASHSCOPE_API_KEY 已设置 ({masked})，模型={config.qwen_model}")
    else:
        print(f"{_BAD} 未检测到 DASHSCOPE_API_KEY（export DASHSCOPE_API_KEY=sk-xxx）")

    # 4. 向日葵进程
    from engine.capture.detector import (
        find_sunlogin_windows,
        is_sunlogin_running,
        pick_remote_window,
    )

    procs = is_sunlogin_running(config)
    if procs:
        names = ", ".join(sorted({p.info.get("name", "?") for p in procs}))
        print(f"{_OK} 向日葵进程在运行：{names}（{len(procs)} 个）")
    else:
        print(f"{_WARN}未发现向日葵进程（匹配名：{config.sunlogin_process_names}）")

    # 5. 向日葵窗口 + 截图权限
    try:
        wins = find_sunlogin_windows(config)
    except RuntimeError as e:
        print(f"{_BAD} 窗口枚举失败：{e}")
        return

    if not wins:
        print(f"{_WARN}未发现向日葵窗口。请确认已连入一个远程会话。")
    else:
        print(f"{_OK} 发现 {len(wins)} 个向日葵相关窗口：")
        for w in wins:
            print(f"     id={w.window_id} owner={w.owner!r} title={w.title!r} {w.width}x{w.height} layer={w.layer}")

        remote = pick_remote_window(config)
        if remote:
            print(f"{_OK} 选中远控窗口：id={remote.window_id} {remote.width}x{remote.height}")
            # 试截一帧验证「屏幕录制」权限
            try:
                from engine.capture.window_grabber import capture_window

                img = capture_window(remote.window_id)
                print(f"{_OK} 截图权限正常，截到 {img.size[0]}x{img.size[1]}")
            except Exception as e:
                print(f"{_BAD} 截图失败（多半是缺「屏幕录制」权限）：{e}")
                print("   去 系统设置 > 隐私与安全性 > 屏幕录制 勾选你的终端/IDE。")
        else:
            print(f"{_WARN}没有窗口达到远控尺寸阈值，可能还没进远程会话。")

    print("=" * 40)


# ----------------------------------------------------------------------------- windows
def cmd_windows(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.detector import list_windows

    for w in list_windows():
        if args.all or w.layer == 0:
            print(f"id={w.window_id:<7} pid={w.pid:<7} {w.width}x{w.height}\tlayer={w.layer}\towner={w.owner!r}\ttitle={w.title!r}")


# ----------------------------------------------------------------------------- peek
def cmd_peek(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.detector import pick_remote_window
    from engine.capture.window_grabber import capture_window
    from engine.privacy.gate import check_egress_allowed
    from engine.privacy.scrubber import scrub_image
    from engine.understand.backend import FrameContext
    from engine.understand.qwen_cloud import QwenCloudBackend

    # 1. 定位远控窗口
    remote = pick_remote_window(config)
    if remote is None:
        print(f"{_BAD} 没找到向日葵远控窗口。先连入一个远程会话，再跑 `sunlens doctor` 确认。")
        sys.exit(1)
    print(f"{_OK} 远控窗口 id={remote.window_id} {remote.width}x{remote.height} title={remote.title!r}")

    # 2. 抓一帧
    image = capture_window(remote.window_id)
    print(f"{_OK} 抓帧 {image.size[0]}x{image.size[1]}")

    # 3. 本机涂码
    scrubbed, redactions = scrub_image(image, config)
    print(f"{_OK} 涂码完成，命中 {len(redactions)} 处 PII/敏感行")
    for r in redactions:
        print(f"     [{r.entity}] @({r.left},{r.top}) {r.width}x{r.height} hash={r.text_hash}")

    # 4. 出口闸门：发图前强制校验
    check_egress_allowed(
        scrubbed=True,
        redact_enabled=config.redact_enabled,
        allow_unredacted=args.allow_unredacted,
    )

    # 落盘留存（仅脱敏图，便于你肉眼复核涂得干不干净）
    config.data_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    scrubbed_path = config.data_dir / f"peek_{stamp}.scrubbed.jpg"
    scrubbed.convert("RGB").save(scrubbed_path, format="JPEG", quality=config.jpeg_quality)
    print(f"{_OK} 脱敏图已存：{scrubbed_path}")

    # 5. 发 Qwen-VL 看懂
    if not config.dashscope_api_key:
        print(f"{_BAD} 缺 DASHSCOPE_API_KEY，跳过云端理解。脱敏图已存，可先肉眼检查涂码效果。")
        sys.exit(1)

    print("→ 发往 Qwen-VL（DashScope）理解中……")
    backend = QwenCloudBackend(config)
    ctx = FrameContext(app_window_title=remote.title, timestamp=stamp)
    u = backend.describe_frame(scrubbed, ctx)

    print("\n" + "=" * 40 + "\nQwen-VL 理解结果\n" + "=" * 40)
    print(f"对方在做什么: {u.description}")
    if u.app:
        print(f"当前应用    : {u.app}")
    if u.search_query:
        print(f"搜索内容    : {u.search_query}")
    if u.intent:
        print(f"推断意图    : {u.intent}")
    print("=" * 40)


# ----------------------------------------------------------------------------- 录制 (M1)
def _open_db(config: SunLensConfig):
    from engine.store.db import DB

    return DB(config.data_dir / "sunlens.db").connect()


def cmd_start(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.recorder import Recorder, recover_sessions

    db = _open_db(config)
    rec = recover_sessions(config, db)
    if rec:
        print(f"{_WARN}恢复了 {len(rec)} 个上次未正常结束的会话：{rec}")
    print(f"{_OK} 开始录制（Ctrl+C 或 `sunlens stop` 停止）……")
    try:
        sid = Recorder(config, db).start()
        if sid:
            print(f"{_OK} 会话已保存：{sid}")
    finally:
        db.close()


def cmd_stop(args: argparse.Namespace, config: SunLensConfig) -> None:
    import os
    import signal

    pidfile = config.data_dir / "recorder.pid"
    if not pidfile.exists():
        print(f"{_WARN}没有正在录制的会话（无 pidfile）。")
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"{_OK} 已发送停止信号给录制进程 pid={pid}。")
    except (ValueError, ProcessLookupError):
        print(f"{_WARN}pidfile 失效，清理。")
        pidfile.unlink(missing_ok=True)


def cmd_list(args: argparse.Namespace, config: SunLensConfig) -> None:
    db = _open_db(config)
    try:
        sessions = db.list_sessions(limit=args.limit)
        if not sessions:
            print("（暂无会话，先 `sunlens start`）")
            return
        for s in sessions:
            print(f"{s['id']}  [{s['status']:<9}] 帧={s['frame_count']:<4} 输入={s['input_count']:<4} {s['started_at'][:19]}  {s['host_window_title']!r}")
    finally:
        db.close()


def cmd_frames(args: argparse.Namespace, config: SunLensConfig) -> None:
    db = _open_db(config)
    try:
        frames = db.list_frames(args.session_id, limit=args.limit)
        if not frames:
            print("（该会话无帧，或 session_id 不对）")
            return
        for f in frames:
            print(f"#{f['id']:<5} {f['ts'][11:19]} diff={f['diff_score']:<6} [{f['trigger']:<13}] {f['image_path']}")
    finally:
        db.close()


def cmd_recover(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.recorder import recover_sessions

    db = _open_db(config)
    try:
        rec = recover_sessions(config, db)
        print(f"{_OK} 恢复 {len(rec)} 个残留会话：{rec}" if rec else f"{_OK} 没有残留会话。")
    finally:
        db.close()


# ----------------------------------------------------------------------------- 音频 (M2)
def cmd_audio_devices(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.audio import find_device, list_input_devices

    devs = list_input_devices()
    if not devs:
        print(f"{_BAD} 没有可用输入设备（或缺 sounddevice）。")
        return
    mic = find_device(config.mic_device)
    sysd = find_device(config.system_audio_device)
    print("可用输入设备：")
    for d in devs:
        role = ""
        if d["index"] == mic or (config.mic_device is None and mic is None and d["index"] == sd_default()):
            role += " ←麦克风(你)" if d["index"] == mic else ""
        if d["index"] == sysd:
            role += " ←loopback(对方)"
        print(f"  [{d['index']}] in={d['channels']} sr={d['samplerate']} {d['name']!r}{role}")
    print(f"\n当前配置：mic={config.mic_device!r}(idx={mic})  "
          f"loopback={config.system_audio_device!r}(idx={sysd})")
    if sysd is None:
        print(f"{_WARN}未找到 loopback 设备，将只录麦克风。对方声音需要向日葵虚拟声卡或 BlackHole。")


def sd_default() -> int | None:
    try:
        import sounddevice as sd

        return sd.default.device[0]
    except Exception:
        return None


def cmd_transcribe(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.transcribe import Transcriber

    model = args.model or config.whisper_model
    print(f"用模型 {model} 转写 {args.wav} ……")
    tx = Transcriber(model, config.whisper_device, config.whisper_compute_type, config.whisper_language)
    segs = tx.transcribe(args.wav)
    if not segs:
        print("（无语音内容或转写为空）")
        return
    for s in segs:
        print(f"  [{s.start:6.2f}-{s.end:6.2f}] {s.text}")


def cmd_transcript(args: argparse.Namespace, config: SunLensConfig) -> None:
    db = _open_db(config)
    try:
        rows = db.list_transcripts(args.session_id)
        if not rows:
            print("（该会话暂无转写）")
            return
        for r in rows:
            print(f"{r['ts_start'][11:19]} [{r['speaker']}] {r['text']}")
    finally:
        db.close()


# ----------------------------------------------------------------------------- 理解 (M3)
def cmd_understand(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.understand.action_builder import ActionBuilder

    if not config.dashscope_api_key:
        print(f"{_BAD} 缺 DASHSCOPE_API_KEY，无法调用 Qwen-VL 做理解。")
        sys.exit(1)
    db = _open_db(config)
    try:
        print(f"→ 理解会话 {args.session_id}（逐帧脱敏→Qwen-VL）……")
        n = ActionBuilder(config, db).build_session(args.session_id, scrub=not args.no_scrub)
        print(f"{_OK} 生成 {n} 个 ActionStep。用 `sunlens actions {args.session_id}` 查看。")
    finally:
        db.close()


def cmd_actions(args: argparse.Namespace, config: SunLensConfig) -> None:
    db = _open_db(config)
    try:
        rows = db.list_action_steps(args.session_id)
        if not rows:
            print("（该会话暂无 ActionStep，先 `sunlens understand <id>`）")
            return
        for r in rows:
            line = f"{r['ts'][11:19]} [{r['type'] or '?':<7}]"
            if r["target_app"]:
                line += f" @{r['target_app']}"
            line += f" {r['nl_description'] or ''}"
            print(line)
            if r["narration"]:
                print(f"            🎙 {r['narration']}")
    finally:
        db.close()


def cmd_serve(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.server.app import serve

    serve(config, port=args.port)


def cmd_index(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.memory.index import index_session

    if not config.dashscope_api_key:
        print(f"{_BAD} 缺 DASHSCOPE_API_KEY，语义记忆需要嵌入模型。")
        sys.exit(1)
    db = _open_db(config)
    try:
        if args.all:
            total = sum(index_session(config, db, s["id"]) for s in db.list_sessions(limit=1000))
            print(f"{_OK} 索引 {total} 条，库内共 {db.count_vectors()} 条向量。")
        else:
            n = index_session(config, db, args.session_id)
            print(f"{_OK} 索引 {n} 条，库内共 {db.count_vectors()} 条向量。")
    finally:
        db.close()


def cmd_ask(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.memory.search import semantic_search

    db = _open_db(config)
    try:
        hits = semantic_search(config, db, args.query)
        if not hits:
            print("（无命中；先 `sunlens index --all` 建索引）")
            return
        for h in hits:
            print(f"  [{h['score']:.3f}] ({h['kind']}) {h['text'][:60]}")
    finally:
        db.close()


_COMMANDS = {
    "doctor": cmd_doctor,
    "windows": cmd_windows,
    "peek": cmd_peek,
    "serve": cmd_serve,
    "start": cmd_start,
    "stop": cmd_stop,
    "list": cmd_list,
    "frames": cmd_frames,
    "recover": cmd_recover,
    "audio-devices": cmd_audio_devices,
    "transcribe": cmd_transcribe,
    "transcript": cmd_transcript,
    "understand": cmd_understand,
    "actions": cmd_actions,
    "index": cmd_index,
    "ask": cmd_ask,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sunlens", description="向日葵学徒（M0）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="体检：平台/依赖/权限/向日葵窗口/API Key")

    p_win = sub.add_parser("windows", help="列出当前窗口（调试）")
    p_win.add_argument("--all", action="store_true", help="包含 layer>0 的系统窗口")

    p_peek = sub.add_parser("peek", help="M0 demo：抓一帧→涂码→Qwen-VL 看懂")
    p_peek.add_argument(
        "--allow-unredacted",
        action="store_true",
        help="涂码关闭时仍允许发原图（调试用，慎用）",
    )

    sub.add_parser("start", help="开始录制（事件驱动抓帧+输入事件，Ctrl+C 停止）")
    sub.add_parser("stop", help="停止正在录制的会话")

    p_list = sub.add_parser("list", help="列出录制会话")
    p_list.add_argument("--limit", type=int, default=20)

    p_frames = sub.add_parser("frames", help="列出某会话的帧")
    p_frames.add_argument("session_id")
    p_frames.add_argument("--limit", type=int, default=100)

    sub.add_parser("recover", help="把崩溃残留的会话标记为 recovered")

    sub.add_parser("audio-devices", help="列出音频输入设备 + 当前角色映射")

    p_tx = sub.add_parser("transcribe", help="转写一个 WAV 文件（测试用）")
    p_tx.add_argument("wav")
    p_tx.add_argument("--model", default=None, help="覆盖默认模型(tiny/base/small/medium)")

    p_tr = sub.add_parser("transcript", help="查看某会话的转写")
    p_tr.add_argument("session_id")

    p_un = sub.add_parser("understand", help="把某会话的帧理解成 ActionStep（Qwen-VL）")
    p_un.add_argument("session_id")
    p_un.add_argument("--no-scrub", action="store_true", help="跳过脱敏(调试,慎用)")

    p_ac = sub.add_parser("actions", help="查看某会话的 ActionStep")
    p_ac.add_argument("session_id")

    p_sv = sub.add_parser("serve", help="启动本地仪表盘(localhost:8088)")
    p_sv.add_argument("--port", type=int, default=8088)

    p_ix = sub.add_parser("index", help="把会话向量化进语义记忆")
    p_ix.add_argument("session_id", nargs="?", default=None)
    p_ix.add_argument("--all", action="store_true", help="索引所有会话")

    p_qa = sub.add_parser("ask", help="语义检索记忆（命令行版）")
    p_qa.add_argument("query")

    args = parser.parse_args(argv)
    config = load_config()

    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    _COMMANDS[args.command](args, config)


if __name__ == "__main__":
    main()
