"""Phase 4 checks: clipboard tools and process management.

Offline and safe: the clipboard round-trip saves whatever was on the clipboard
first and puts it back afterwards, and the process tests only ever touch a child
process this script spawns itself.

Run with:  uv run python -u tests/phase4.py
"""

from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
import time

import psutil

from jarvis.config import SearchConfig, SecurityConfig
from jarvis.core.registry import ToolError
from jarvis.tools import build_registry, clipboard, procman

faulthandler.dump_traceback_later(180, exit=True)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


def expect_error(label: str, func, *args, **kwargs) -> None:
    try:
        result = func(*args, **kwargs)
    except ToolError as exc:
        check(label, True, str(exc)[:70])
        return
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"抛了 {type(exc).__name__} 而不是 ToolError: {exc}")
        return
    check(label, False, f"没有拒绝，返回：{str(result)[:70]}")


NEW_TOOLS = [
    "read_clipboard",
    "write_clipboard",
    "clear_clipboard",
    "list_processes",
    "process_info",
    "kill_process",
    "freeze_process",
]


# ------------------------------------------------------------------- registry
def test_registry() -> None:
    registry = build_registry(SecurityConfig(), ".", SearchConfig())
    names = [tool.name for tool in registry.tools]
    check("新工具全部注册", all(name in names for name in NEW_TOOLS),
          ",".join(n for n in names if n not in NEW_TOOLS))
    check("工具总数为 15", len(names) == 15, str(len(names)))

    for name in ("read_clipboard", "list_processes", "process_info"):
        check(f"{name} 免确认", not registry.needs_confirmation(registry.get(name)))  # type: ignore[arg-type]
    for name in ("write_clipboard", "clear_clipboard", "kill_process", "freeze_process"):
        check(f"{name} 需确认", registry.needs_confirmation(registry.get(name)))  # type: ignore[arg-type]
    check("每种工具名唯一", len(names) == len(set(names)))

    # auto_approve 通配符仍然生效
    loose = SecurityConfig(auto_approve=["*_clipboard"])
    relaxed = build_registry(loose, ".", SearchConfig())
    check("auto_approve 通配符可用",
          not relaxed.needs_confirmation(relaxed.get("write_clipboard")))  # type: ignore[arg-type]


# ------------------------------------------------------------------ clipboard
def _clipboard_blocked() -> str:
    """'' when the clipboard can be opened, otherwise a readable reason."""

    try:
        with clipboard._ClipboardLock():
            pass
    except ToolError as exc:
        return str(exc)
    return ""


def test_clipboard_registry() -> None:
    """The registry wiring is checked whether or not the OS clipboard is free."""

    import asyncio

    registry = build_registry(SecurityConfig(), ".", SearchConfig())
    out = asyncio.run(registry.call("read_clipboard", '{"max_chars": 50}'))
    check("read_clipboard 调用不炸", "Traceback" not in out and bool(out), out.splitlines()[0][:60])
    out = asyncio.run(registry.call("write_clipboard", '{"text": 1}'))
    check("错类型参数被转成可读错误", out.startswith("[错误]"), out[:60])
    out = asyncio.run(registry.call("read_clipboard", "not-json"))
    check("坏 JSON 参数被转成可读错误", out.startswith("[错误]"), out[:60])
    out = asyncio.run(registry.call("clear_clipboard", "{}"))
    check("clear_clipboard 调用不炸", "Traceback" not in out and bool(out), out.splitlines()[0][:60])


def test_clipboard() -> None:
    if not clipboard.IS_WINDOWS:
        print("[warn] 非 Windows，跳过剪贴板往返测试")
        return

    blocked = _clipboard_blocked()
    if blocked:
        print(f"[warn] 剪贴板当前打不开，跳过往返测试：{blocked}")
        check("剪贴板不可用时错误可读", "打不开剪贴板" in blocked, blocked[:70])
        check("错误里给出原因/错误码", "占用" in blocked or "Win32 错误" in blocked, blocked[:70])
        return

    # Snapshot first: the user's clipboard must survive this test. Anything that
    # is not plain text (an image, a file list, HTML) cannot be restored, so the
    # write half of the test bows out instead of destroying it.
    with clipboard._ClipboardLock():
        original_text = clipboard._clipboard_text_locked()
        original_formats = clipboard._formats_locked()

    text_formats = {"CF_TEXT", "CF_OEMTEXT", "CF_UNICODETEXT", "CF_LOCALE"}
    exotic = [name for name in original_formats if name not in text_formats]
    if exotic:
        print(f"[warn] 剪贴板里有非文本内容（{', '.join(exotic)}），为免丢失跳过写入测试")
        check("剪贴板读取可用（只读）", "剪贴板" in clipboard.read_clipboard(200))
        return

    marker = "JARVIS-P4-往返-Ω"
    try:
        result = clipboard.write_clipboard(marker)
        check("写入返回完成", result.startswith("[完成]"), result[:50])
        back = clipboard.read_clipboard(4000)
        check("读回包含写入内容", marker in back, back.splitlines()[0][:50])
        check("读取报告了格式", "剪贴板格式：" in back)
        check("格式里有 CF_UNICODETEXT", "CF_UNICODETEXT" in back)

        clipboard.write_clipboard("第二行", append=True)
        appended = clipboard.read_clipboard(4000)
        check("追加保留了原内容", marker in appended and "第二行" in appended)

        long_text = "长" * 300
        clipboard.write_clipboard(long_text)
        clipped = clipboard.read_clipboard(100)
        check("超长文本会被截断", "已截断" in clipped, f"len={len(clipped)}")
        check("截断后不再包含全文", clipped.count("长") <= 120, str(clipped.count("长")))

        clipboard.write_clipboard("")
        emptied = clipboard.read_clipboard(200)
        check("空文本等于清空", "剪贴板是空的" in emptied, emptied[:40])

        clipboard.write_clipboard("清空前")
        cleared = clipboard.clear_clipboard()
        check("清空返回完成", cleared.startswith("[完成]"), cleared[:40])
        check("清空后读为空", "剪贴板是空的" in clipboard.read_clipboard(200))
    finally:
        if original_text:
            clipboard.write_clipboard(original_text)
            check("原剪贴板内容已还原", marker not in clipboard.read_clipboard(4000))
        else:
            clipboard.clear_clipboard()

    # clipboard_text() 是给界面用的，不能抛异常
    check("clipboard_text 不抛异常", isinstance(clipboard.clipboard_text(30), str))


# ------------------------------------------------------------------ processes
def test_list_processes() -> None:
    report = procman.list_processes(sort_by="cpu", limit=5)
    lines = report.splitlines()
    check("列表有表头", "PID" in lines[1] and "CPU%" in lines[1], lines[1][:60])
    check("列表有分隔线", set(lines[2]) == {"-"}, lines[2][:20])
    check("列表条数符合 limit", len(lines) == 3 + 5, str(len(lines)))
    check("列表给出总数", "共" in lines[0] and "进程" in lines[0], lines[0])

    by_mem = procman.list_processes(sort_by="memory", limit=4)
    rows = by_mem.splitlines()[3:]
    check("按内存排序返回 4 行", len(rows) == 4, str(rows[0][:50]) if rows else "")
    check("内存列有单位", all(any(u in row for u in ("B", "KB", "MB", "GB")) for row in rows))

    filtered = procman.list_processes(name_contains="python", limit=50)
    body = filtered.splitlines()[3:]
    check("名称过滤生效", bool(body) and all("python" in line.lower() for line in body),
          f"{len(body)} 行")
    check("过滤时标题说明过滤条件", "含" in filtered.splitlines()[0], filtered.splitlines()[0][:50])

    missing = procman.list_processes(name_contains="绝对不存在的进程名-zzz")
    check("无匹配时给出提示", "没有找到匹配" in missing, missing[:40])

    one = procman.list_processes(limit=1)
    check("limit 下限被夹到 1", len(one.splitlines()) == 4, str(len(one.splitlines())))
    check("limit=0 视为默认值", len(procman.list_processes(limit=0).splitlines()) == 3 + 25)

    expect_error("非法排序字段被拒绝", procman.list_processes, "温度")
    check("默认参数可用", "进程" in procman.list_processes())


def test_process_info() -> None:
    pid = os.getpid()
    detail = procman.process_info(str(pid))
    check("按 PID 查到进程", f"PID     : {pid}" in detail, detail.splitlines()[0])
    check("详情含内存", "内存    :" in detail)
    check("详情含命令行", "命令行  :" in detail)
    check("详情含父进程", "父进程  :" in detail)
    check("识别出自身进程链", "JARVIS 自己的进程链" in detail)

    mine = procman.process_info("python")
    check("按名字能查到", "PID" in mine, mine.splitlines()[0][:60])

    expect_error("空查询被拒绝", procman.process_info, "   ")
    expect_error("不存在的名字被拒绝", procman.process_info, "绝对不存在的进程名-zzz")
    expect_error("不存在的 PID 被拒绝", procman.process_info, "999999")

    if os.name == "nt":
        many = procman.process_info("exe")
        check("多命中时列出候选", "匹配到" in many.splitlines()[0], many.splitlines()[0][:60])


def test_guards() -> None:
    expect_error("拒绝结束自己", procman.kill_process, os.getpid())
    expect_error("拒绝挂起自己", procman.freeze_process, os.getpid())
    expect_error("拒绝不存在的 PID", procman.kill_process, 999999)
    expect_error("拒绝挂起不存在的 PID", procman.freeze_process, 999999)

    if os.name != "nt":
        print("[warn] 非 Windows，跳过系统进程保护检查")
        return

    expect_error("拒绝结束 PID 4（System）", procman.kill_process, 4)
    expect_error("拒绝结束 PID 0（Idle）", procman.kill_process, 0)

    for name in ("smss.exe", "csrss.exe", "lsass.exe"):
        hits = [p for p in psutil.process_iter(["name"])
                if (p.info.get("name") or "").lower() == name]
        if not hits:
            print(f"[warn] 没找到 {name}，跳过")
            continue
        expect_error(f"拒绝结束 {name}", procman.kill_process, hits[0].pid)


def test_real_child() -> None:
    """Spawn a throwaway process and really end it."""

    def spawn() -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])

    child = spawn()
    try:
        check("子进程已启动", psutil.pid_exists(child.pid), str(child.pid))
        time.sleep(0.4)
        info = procman.process_info(str(child.pid))
        check("能查到子进程", f"PID     : {child.pid}" in info)

        result = procman.kill_process(child.pid)
        check("结束子进程成功", result.startswith("[完成]") and "已结束" in result, result[:60])
        time.sleep(0.3)
        check("子进程真的没了", not psutil.pid_exists(child.pid))

        expect_error("重复结束时给出可读错误", procman.kill_process, child.pid)
    finally:
        if child.poll() is None:
            child.kill()

    # 挂起 / 恢复
    child = spawn()
    try:
        time.sleep(0.5)
        proc = psutil.Process(child.pid)
        frozen = procman.freeze_process(child.pid)
        check("挂起返回完成", frozen.startswith("[完成]"), frozen[:50])
        time.sleep(0.4)
        stopped = proc.status()
        check("挂起后状态为 stopped", stopped == psutil.STATUS_STOPPED, stopped)
        thawed = procman.freeze_process(child.pid, resume=True)
        check("恢复返回完成", thawed.startswith("[完成]"), thawed[:50])
        time.sleep(0.4)
        check("恢复后仍在运行", child.poll() is None and psutil.pid_exists(child.pid))

        forced = procman.kill_process(child.pid, force=True)
        check("force 结束成功", forced.startswith("[完成]"), forced[:60])
        time.sleep(0.3)
        check("force 后进程消失", not psutil.pid_exists(child.pid))
    finally:
        if child.poll() is None:
            child.kill()


# ------------------------------------------------------------------------ tui
def test_tui_hooks() -> None:
    from jarvis.tui.app import JarvisApp

    check("/clip 命令已接线", hasattr(JarvisApp, "_handle_clip_command"))
    check("/ps 命令已接线", hasattr(JarvisApp, "_handle_ps_command"))
    from jarvis.tui.screens import HelpScreen

    check("帮助里有剪贴板说明", "/clip" in HelpScreen.HELP_TEXT)
    check("帮助里有进程说明", "/ps" in HelpScreen.HELP_TEXT)


def main() -> int:
    test_registry()
    test_clipboard_registry()
    test_clipboard()
    test_list_processes()
    test_process_info()
    test_guards()
    test_real_child()
    test_tui_hooks()

    faulthandler.cancel_dump_traceback_later()
    if FAILURES:
        print(f"\nPHASE4 TEST FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("\nPHASE4 TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
