"""Tool plugins and the registry factory."""

from __future__ import annotations

from functools import partial

from ..config import SecurityConfig
from ..core.registry import Tool, ToolRegistry
from . import fs, shell, sysinfo


def build_registry(security: SecurityConfig, workdir: str) -> ToolRegistry:
    """Create a registry with every Phase-1 tool wired to the config."""

    registry = ToolRegistry(security)

    # ---------------------------------------------------------------- shell
    def run_shell(command: str, cwd: str = "") -> str:
        return shell.run_shell(
            command,
            cwd=cwd or workdir,
            timeout=security.shell_timeout,
        )

    registry.register(
        Tool(
            name="run_shell",
            description=(
                "在这台 Windows 机器上执行一条命令并返回输出（stdout/stderr/exit code）。"
                "可用于查看信息、运行脚本、管理文件与进程。危险命令会被安全策略拦截。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，例如 'Get-Process | Select-Object -First 5'"},
                    "cwd": {"type": "string", "description": "工作目录（可选），留空使用默认项目目录"},
                },
                "required": ["command"],
            },
            func=run_shell,
            dangerous=True,
            hint="将在这台电脑上执行命令",
        )
    )

    # ------------------------------------------------------------------- fs
    registry.register(
        Tool(
            name="read_file",
            description="读取文本文件内容（支持大文件分段读取）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "完整路径，例如 D:\\code\\notes.md"},
                    "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 60000"},
                    "offset": {"type": "integer", "description": "起始字节偏移，默认 0"},
                },
                "required": ["path"],
            },
            func=fs.read_file,
        )
    )
    registry.register(
        Tool(
            name="write_file",
            description="把文本写入文件（自动创建父目录）。会覆盖原内容，除非 append=true。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "完整目标路径"},
                    "content": {"type": "string", "description": "要写入的文本内容"},
                    "append": {"type": "boolean", "description": "true 表示追加而非覆盖"},
                },
                "required": ["path", "content"],
            },
            func=fs.write_file,
            dangerous=True,
            hint="将修改磁盘上的文件",
        )
    )
    registry.register(
        Tool(
            name="list_dir",
            description="列出目录内容（名称、类型、大小）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，留空使用项目根目录"},
                    "show_hidden": {"type": "boolean", "description": "是否显示隐藏项"},
                },
            },
            func=partial(_list_dir, workdir),
        )
    )
    registry.register(
        Tool(
            name="search_files",
            description="在目录下递归按通配符查找文件，例如 *.py。",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符模式，如 *.py"},
                    "path": {"type": "string", "description": "搜索根目录，留空使用项目根目录"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 60"},
                },
                "required": ["pattern"],
            },
            func=partial(_search_files, workdir),
        )
    )

    # -------------------------------------------------------------- sysinfo
    registry.register(
        Tool(
            name="sys_report",
            description="获取本机实时状态：CPU、内存、磁盘、网络、电池、占资源最多的进程。",
            parameters={
                "type": "object",
                "properties": {
                    "include_processes": {"type": "integer", "description": "列出前 N 个高占用进程，默认 8"},
                },
            },
            func=sysinfo.sys_report,
        )
    )

    return registry


def _list_dir(workdir: str, path: str = "", show_hidden: bool = False) -> str:
    return fs.list_dir(path or workdir, show_hidden=show_hidden)


def _search_files(workdir: str, pattern: str, path: str = "", max_results: int = 60) -> str:
    return fs.search_files(pattern, path or workdir, max_results=max_results)
