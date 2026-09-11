"""Shell tool: run PowerShell / cmd commands with a hard safety net."""

from __future__ import annotations

import locale
import re
import subprocess
from pathlib import Path

from ..core.registry import ToolError

BLOCKED_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+-rf\s+/(\s|$)", "递归删除根目录"),
    (r"\bdel\s+/[sq]\b", "递归/静默删除"),
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bmkfs", "格式化文件系统"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"vssadmin\s+delete", "删除卷影副本"),
    (r"\bbcdedit\b", "修改引导配置"),
    (r"\breg(\.exe)?\s+delete", "删除注册表项"),
    (r"\bcipher\s+/w", "擦除磁盘空闲空间"),
    (r"shutdown\s+/[sr]", "关机/重启"),
    (r"\bnet\s+user\s+\S+\s+/delete", "删除账户"),
    (r"Remove-Item\s+.*-Recurse.*-Force\s+[A-Z]:\\(\s|$)", "强制递归删除盘根目录"),
]

_MAX_OUTPUT = 12000


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _guard(command: str) -> None:
    lowered = command.lower()
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            raise ToolError(f"命令被安全策略拦截（{reason}）：{command}")


def run_shell(
    command: str,
    shell: str = "powershell",
    cwd: str = "",
    timeout: int = 60,
) -> str:
    """Execute a command on the local machine and return its output.

    Args:
        command: Command line to run.
        shell: ``powershell`` (default) or ``cmd``.
        cwd: Working directory; empty means the JARVIS project root.
        timeout: Seconds before the command is killed.
    """

    if not command.strip():
        raise ToolError("命令为空。")
    _guard(command)

    exit_code, stdout, stderr = run_raw(command, shell=shell, cwd=cwd, timeout=timeout)

    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip())
    parts.append(f"[exit code] {exit_code}")
    return "\n".join(parts)[:_MAX_OUTPUT]


def run_raw(
    command: str,
    *,
    shell: str = "powershell",
    cwd: str = "",
    timeout: int = 60,
    workdir: Path | None = None,
) -> tuple[int, str, str]:
    """Low-level runner shared by the tool and internal helpers."""

    working_dir = Path(cwd) if cwd else (workdir or Path.cwd())

    if shell.lower() in {"cmd", "cmd.exe"}:
        argv = ["cmd.exe", "/d", "/c", command]
    else:
        argv = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; {command}",
        ]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(working_dir) if working_dir.exists() else None,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令执行超过 {timeout} 秒，已终止：{command}") from None
    except FileNotFoundError as exc:
        raise ToolError(f"找不到可执行文件：{exc}") from None

    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
