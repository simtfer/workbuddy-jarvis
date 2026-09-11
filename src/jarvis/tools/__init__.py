"""Tool plugins and the registry factory."""

from __future__ import annotations

from functools import partial

from ..config import SearchConfig, SecurityConfig
from ..core.registry import Tool, ToolRegistry
from . import clipboard, fs, procman, shell, sysinfo, web


def build_registry(
    security: SecurityConfig,
    workdir: str,
    search: SearchConfig | None = None,
) -> ToolRegistry:
    """Create a registry with every tool wired to the config."""

    registry = ToolRegistry(security)
    search_cfg = search or SearchConfig()

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

    # ------------------------------------------------------------------ web
    registry.register(
        Tool(
            name="web_search",
            description=(
                "联网搜索，返回标题 / 网址 / 摘要列表。需要最新信息、你不确定的事、"
                "或用户提到「查一下」「搜一下」「最新的」时用它。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，越具体越好"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 5，最多 20"},
                    "provider": {
                        "type": "string",
                        "description": "临时改用别的搜索后端：duckduckgo / bocha / tavily / serper / searxng",
                    },
                },
                "required": ["query"],
            },
            func=partial(_web_search, search_cfg),
        )
    )
    registry.register(
        Tool(
            name="fetch_url",
            description="打开一个网址并把正文抓成纯文本，用来读文章、文档或搜索结果里的页面。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整网址，例如 https://example.com/a"},
                    "max_chars": {"type": "integer", "description": "最多返回字符数，默认 8000"},
                },
                "required": ["url"],
            },
            func=web.fetch,
        )
    )

    # ------------------------------------------------------------ clipboard
    registry.register(
        Tool(
            name="read_clipboard",
            description=(
                "读取当前系统剪贴板的文本内容（同时会报告格式，以及用资源管理器「复制文件」"
                "得到的文件列表）。用户说「看看我复制了什么」「总结一下剪贴板里的内容」时用它。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "max_chars": {"type": "integer", "description": "最多返回字符数，默认 8000"},
                },
            },
            func=clipboard.read_clipboard,
        )
    )
    registry.register(
        Tool(
            name="write_clipboard",
            description=(
                "把一段文本放进系统剪贴板，之后用户可以直接 Ctrl+V 粘贴。"
                "会覆盖剪贴板原有的内容（除非 append=true）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要复制的文本；传空字符串表示清空剪贴板"},
                    "append": {"type": "boolean", "description": "true 表示追加到剪贴板现有文本后面"},
                },
                "required": ["text"],
            },
            func=clipboard.write_clipboard,
            dangerous=True,
            hint="将覆盖系统剪贴板内容",
        )
    )
    registry.register(
        Tool(
            name="clear_clipboard",
            description="清空系统剪贴板（里面可能是文本、图片或文件列表）。",
            parameters={"type": "object", "properties": {}},
            func=clipboard.clear_clipboard,
            dangerous=True,
            hint="将清空系统剪贴板内容",
        )
    )

    # ------------------------------------------------------------- processes
    registry.register(
        Tool(
            name="list_processes",
            description=(
                "列出正在运行的进程（PID、名称、CPU%、内存、状态、用户），类似任务管理器。"
                "想看某个程序占多少资源、或要结束进程前先拿 PID，就用它。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sort_by": {
                        "type": "string",
                        "description": "排序字段：cpu（默认）/ memory / name / pid",
                    },
                    "limit": {"type": "integer", "description": "返回条数，默认 25，最多 200"},
                    "name_contains": {"type": "string", "description": "只看名称包含该文本的进程，例如 chrome"},
                },
            },
            func=procman.list_processes,
        )
    )
    registry.register(
        Tool(
            name="process_info",
            description=(
                "查看某个进程的详细信息：命令行、可执行路径、父进程与子进程、网络连接、"
                "启动时间、CPU/内存占用。参数可以是 PID 或进程名。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PID（如 1234）或进程名（如 chrome）"},
                },
                "required": ["query"],
            },
            func=procman.process_info,
        )
    )
    registry.register(
        Tool(
            name="kill_process",
            description=(
                "结束一个进程（先温和终止，无效时可 force=true 强制结束）。"
                "需要 PID，先调用 list_processes 拿到。系统关键进程与 JARVIS 自身会被拒绝。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "目标进程 PID"},
                    "force": {"type": "boolean", "description": "true 表示温和终止无效时强制结束"},
                    "timeout": {"type": "number", "description": "等待退出的秒数，默认 3"},
                },
                "required": ["pid"],
            },
            func=procman.kill_process,
            dangerous=True,
            hint="将结束一个正在运行的进程（未保存的数据可能丢失）",
        )
    )
    registry.register(
        Tool(
            name="freeze_process",
            description=(
                "挂起（冻结）一个进程以释放 CPU，或把它恢复回来。"
                "适合临时压住吃 CPU 的程序而不丢它的状态；resume=true 表示恢复。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "目标进程 PID"},
                    "resume": {"type": "boolean", "description": "true 表示恢复挂起的进程"},
                },
                "required": ["pid"],
            },
            func=procman.freeze_process,
            dangerous=True,
            hint="将挂起/恢复一个正在运行的进程",
        )
    )

    return registry


def _list_dir(workdir: str, path: str = "", show_hidden: bool = False) -> str:
    return fs.list_dir(path or workdir, show_hidden=show_hidden)


def _search_files(workdir: str, pattern: str, path: str = "", max_results: int = 60) -> str:
    return fs.search_files(pattern, path or workdir, max_results=max_results)


def _web_search(
    cfg: SearchConfig, query: str, max_results: int = 0, provider: str = ""
) -> str:
    return web.search(query, cfg, max_results=max_results or None, provider=provider or None)
