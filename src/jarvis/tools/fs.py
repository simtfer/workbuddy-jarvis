"""File system tools: read, write, list, search."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ..core.registry import ToolError

TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".csv", ".log", ".html", ".css", ".xml", ".sql", ".sh", ".ps1", ".bat",
    ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs", ".rb", ".php", ".lua", ".env", ".gitignore",
}

PROTECTED_ROOTS = [
    Path(os.environ.get("SystemRoot", r"C:\Windows")),
    Path(r"C:\Program Files"),
    Path(r"C:\Program Files (x86)"),
]


def _expand(path: str) -> Path:
    if not path.strip():
        raise ToolError("路径为空。")
    return Path(os.path.expandvars(path.strip().strip('"'))).expanduser()


def _assert_writable(path: Path) -> None:
    resolved = path.resolve()
    for root in PROTECTED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        raise ToolError(f"拒绝写入系统目录：{resolved}")


def read_file(path: str, max_bytes: int = 60000, offset: int = 0) -> str:
    """Read a text file from disk.

    Args:
        path: Absolute Windows path, e.g. ``D:\\code\\notes.md``.
        max_bytes: Maximum number of bytes to return.
        offset: Byte offset to start from (for reading large files in chunks).
    """

    target = _expand(path)
    if not target.exists():
        raise ToolError(f"文件不存在：{target}")
    if target.is_dir():
        raise ToolError(f"这是目录而不是文件：{target}")

    size = target.stat().st_size
    with target.open("rb") as handle:
        if offset:
            handle.seek(offset)
        payload = handle.read(max_bytes)

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        if target.suffix.lower() not in TEXT_SUFFIXES:
            return f"[二进制文件] {target}，大小 {size / 1024:.1f} KB，无法按文本读取。"
        text = payload.decode("utf-8", errors="replace")

    header = f"# {target}  ({size} bytes"
    if offset or len(payload) < size:
        header += f", 本次读取 {offset}-{offset + len(payload)}"
    header += ")\n```\n"
    return header + text + "\n```"


def write_file(path: str, content: str, append: bool = False) -> str:
    """Write text to a file, creating parent directories as needed.

    Args:
        path: Absolute target path.
        content: Text content to write.
        append: Append instead of overwriting.
    """

    target = _expand(path)
    _assert_writable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    verb = "追加到" if append else "写入"
    return f"[完成] 已{verb} {target}（{len(content)} 字符，当前大小 {target.stat().st_size} 字节）"


def list_dir(path: str = ".", show_hidden: bool = False) -> str:
    """List a directory with sizes and modification times.

    Args:
        path: Directory path. Defaults to the JARVIS project root.
        show_hidden: Include dotfiles and hidden entries.
    """

    target = _expand(path)
    if not target.exists():
        raise ToolError(f"路径不存在：{target}")
    if not target.is_dir():
        return f"[提示] {target} 是文件，大小 {target.stat().st_size} 字节。"

    rows: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if not show_hidden and entry.name.startswith("."):
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        if entry.is_dir():
            rows.append(f"DIR            {entry.name}")
        else:
            rows.append(f"FILE {size:>10,}  {entry.name}")

    lines = rows or ["(空目录)"]
    return f"{target}\n共 {len(rows)} 项\n" + "\n".join(lines)


def search_files(pattern: str, path: str = ".", max_results: int = 60) -> str:
    """Search for files by wildcard pattern under a directory (recursive).

    Args:
        pattern: Glob pattern matched against file names, e.g. ``*.py`` or ``notes*``.
        path: Root directory to search.
        max_results: Cap on returned matches.
    """

    root = _expand(path)
    if not root.is_dir():
        raise ToolError(f"不是目录：{root}")

    skip = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".idea"}
    hits: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                hits.append(str(Path(current) / name))
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"在 {root} 下没有匹配 '{pattern}' 的文件。"
    return f"在 {root} 下匹配 '{pattern}'（{len(hits)} 个）：\n" + "\n".join(hits)
