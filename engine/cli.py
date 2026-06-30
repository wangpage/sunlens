"""SunLens CLI（Windows + 本地 Ollama）。

命令：
  doctor   —— 体检：平台/Ollama/目标窗口/截图
  windows  —— 列出当前窗口（调试，找目标 owner/title 规律）
  peek     —— demo：抓目标窗口一帧 → 本地 qwen3-vl 看懂 → 打印
  start/stop/list/frames/recover —— 录制 + 会话管理
  understand/actions —— 把帧理解成 ActionStep + 查看
  sop      —— 把会话动作流总结成 SOP/操作手册（本地模型）
  serve    —— 本地仪表盘
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import datetime, timezone

import requests
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
    is_win = platform.system() == "Windows"
    print(f"{_OK if is_win else _WARN}平台: {platform.system()} {platform.release()}")

    # 2. 本地 Ollama 可达性 + 模型在列
    try:
        v = requests.get(config.ollama_base_url.replace("/v1", "") + "/api/version", timeout=5)
        print(f"{_OK} Ollama 在线：v{v.json().get('version', '?')}（{config.ollama_base_url}）")
        tags = requests.get(config.ollama_base_url.replace("/v1", "") + "/api/tags", timeout=5)
        names = [m["name"] for m in tags.json().get("models", [])]
        if config.vlm_model in names:
            print(f"{_OK} 视觉模型已就绪：{config.vlm_model}")
        else:
            print(f"{_BAD} 缺模型 {config.vlm_model}。请 `ollama pull {config.vlm_model}`。可用：{names}")
    except Exception as e:
        print(f"{_BAD} 连不上 Ollama（{config.ollama_base_url}）：{e}")
        print("   请先启动 Ollama（ollama serve）。")

    # 3. 目标窗口（默认 fnOS NAS 网页会话）+ 截图
    from engine.capture.detector import find_target_windows, pick_target_window

    wins = find_target_windows(config)
    if not wins:
        print(f"{_WARN}未发现目标窗口（匹配规则：{config.target_window_patterns}）。"
              f"请打开并聚焦 fnOS NAS 会话窗口。")
    else:
        print(f"{_OK} 发现 {len(wins)} 个目标相关窗口：")
        for w in wins:
            print(f"     hwnd={w.window_id} owner={w.owner!r} title={w.title!r} {w.width}x{w.height}")
        target = pick_target_window(config)
        if target:
            print(f"{_OK} 选中目标窗口：hwnd={target.window_id} {target.width}x{target.height}")
            try:
                from engine.capture.window_grabber import capture_window

                img = capture_window(target)
                print(f"{_OK} 截图正常，截到 {img.size[0]}x{img.size[1]}")
            except Exception as e:
                print(f"{_BAD} 截图失败：{e}")
        else:
            print(f"{_WARN}没有窗口达到尺寸阈值。")
    print("=" * 40)


# ----------------------------------------------------------------------------- windows
def cmd_windows(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.detector import list_windows

    for w in list_windows():
        print(f"hwnd={w.window_id:<9} pid={w.pid:<7} {w.width}x{w.height}\towner={w.owner!r}\ttitle={w.title!r}")


# ----------------------------------------------------------------------------- peek
def cmd_peek(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.capture.detector import pick_target_window
    from engine.capture.window_grabber import capture_window
    from engine.understand.vlm import FrameContext, describe_frame

    target = pick_target_window(config)
    if target is None:
        print(f"{_BAD} 没找到目标窗口。先打开并聚焦 fnOS NAS 会话窗口，再跑 `sunlens doctor` 确认。")
        sys.exit(1)
    print(f"{_OK} 目标窗口 hwnd={target.window_id} {target.width}x{target.height} title={target.title!r}")

    image = capture_window(target)
    print(f"{_OK} 抓帧 {image.size[0]}x{image.size[1]}")

    # 落盘留存（便于肉眼复核）
    config.data_dir.mkdir(parents=True, exist_ok=True)
    peek_path = config.data_dir / f"peek_{_now_stamp()}.jpg"
    image.save(peek_path, format="JPEG", quality=config.jpeg_quality)
    print(f"{_OK} 截图已存：{peek_path}")

    print("→ 发往本地 qwen3-vl 理解中……")
    ctx = FrameContext(app_window_title=target.title, timestamp=_now_stamp())
    u = describe_frame(config, image, ctx)

    print("\n" + "=" * 40 + "\nqwen3-vl 理解结果\n" + "=" * 40)
    print(f"在做什么    : {u.description}")
    if u.app:
        print(f"当前应用    : {u.app}")
    if u.search_query:
        print(f"搜索内容    : {u.search_query}")
    if u.intent:
        print(f"推断意图    : {u.intent}")
    print("=" * 40)


# ----------------------------------------------------------------------------- 录制
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
    except (ValueError, ProcessLookupError, OSError):
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


# ----------------------------------------------------------------------------- 理解
def cmd_understand(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.understand.action_builder import ActionBuilder

    db = _open_db(config)
    try:
        print(f"→ 理解会话 {args.session_id}（逐帧 → 本地 qwen3-vl）……")
        n = ActionBuilder(config, db).build_session(args.session_id)
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
    finally:
        db.close()


def cmd_sop(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.studio import generate_sop

    db = _open_db(config)
    try:
        print(f"→ 把会话 {args.session_id} 的动作流总结成 SOP（本地模型）……")
        res = generate_sop(config, db, args.session_id)
        if res.get("type") == "error":
            print(f"{_BAD} {res['error']}")
            return
        print(f"{_OK} 已生成《{res['title']}》(manual_id={res['manual_id']})，存入技能手册。\n")
        print(res["data"])
    finally:
        db.close()


def cmd_intent(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.intent import analyze_intent

    db = _open_db(config)
    try:
        print(f"→ 分析会话 {args.session_id} 的用户意图（草稿 → 反思自检）……")
        r = analyze_intent(config, db, args.session_id)
        if r.get("error"):
            print(f"{_BAD} {r['error']}")
            return
        print("\n" + "=" * 44 + "\n🎯 意图摘要\n" + "=" * 44)
        print(f"任务      : {r.get('task', '')}")
        print(f"动机      : {r.get('why', '')}")
        info = r.get("info_sought") or []
        print("想获取信息: " + ("；".join(info) if isinstance(info, list) else str(info)))
        steps = r.get("key_steps") or []
        if steps:
            print("关键步骤  :")
            for s in (steps if isinstance(steps, list) else [steps]):
                print(f"   - {s}")
        print(f"结论      : {r.get('outcome', '')}")
        print(f"把握度    : {r.get('confidence', '')}")
        if r.get("reflection"):
            print(f"🤔 反思    : {r['reflection']}")
        print("=" * 44)
    finally:
        db.close()


def cmd_correct(args: argparse.Namespace, config: SunLensConfig) -> None:
    db = _open_db(config)
    try:
        db.add_feedback(args.session_id, args.text)
        print(f"{_OK} 已记下你的纠正，下次分析会作为上下文吸取（越用越准）。")
    finally:
        db.close()


def cmd_serve(args: argparse.Namespace, config: SunLensConfig) -> None:
    from engine.server.app import serve

    serve(config, port=args.port)


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
    "understand": cmd_understand,
    "actions": cmd_actions,
    "sop": cmd_sop,
    "intent": cmd_intent,
    "correct": cmd_correct,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sunlens", description="SunLens 学徒（Windows + 本地 Ollama）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="体检：平台/Ollama/目标窗口/截图")
    sub.add_parser("windows", help="列出当前窗口（调试）")
    sub.add_parser("peek", help="demo：抓一帧→本地 qwen3-vl 看懂")
    sub.add_parser("start", help="开始录制（事件驱动抓帧+输入事件，Ctrl+C 停止）")
    sub.add_parser("stop", help="停止正在录制的会话")

    p_list = sub.add_parser("list", help="列出录制会话")
    p_list.add_argument("--limit", type=int, default=20)

    p_frames = sub.add_parser("frames", help="列出某会话的帧")
    p_frames.add_argument("session_id")
    p_frames.add_argument("--limit", type=int, default=100)

    sub.add_parser("recover", help="把崩溃残留的会话标记为 recovered")

    p_un = sub.add_parser("understand", help="把某会话的帧理解成 ActionStep（本地 qwen3-vl）")
    p_un.add_argument("session_id")

    p_ac = sub.add_parser("actions", help="查看某会话的 ActionStep")
    p_ac.add_argument("session_id")

    p_sop = sub.add_parser("sop", help="把某会话的动作流总结成 SOP/操作手册（本地模型）")
    p_sop.add_argument("session_id")

    p_intent = sub.add_parser("intent", help="会话级意图分析：任务/动机/想获取的信息/结论（含反思自检）")
    p_intent.add_argument("session_id")

    p_corr = sub.add_parser("correct", help="纠正某会话的意图理解（存入反馈记忆，越用越准）")
    p_corr.add_argument("session_id")
    p_corr.add_argument("text", help="你的纠正，例如：他其实在排查录像是否丢失")

    p_sv = sub.add_parser("serve", help="启动本地仪表盘(localhost:8088)")
    p_sv.add_argument("--port", type=int, default=8088)

    # Windows 控制台默认 GBK，强制 UTF-8 以输出 emoji/中文
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 设进程 DPI-aware：让窗口矩形返回物理像素，PrintWindow 抓取尺寸才对得上
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    args = parser.parse_args(argv)
    config = load_config()

    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    _COMMANDS[args.command](args, config)


if __name__ == "__main__":
    main()
