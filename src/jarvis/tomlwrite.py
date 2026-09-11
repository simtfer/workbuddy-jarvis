"""A tiny TOML writer.

Python ships ``tomllib`` (read-only), so writing config.toml back out needs a
serialiser. This covers exactly the shapes JARVIS stores - strings, numbers,
booleans, arrays of strings, and one level of nested tables - and keeps the
"no new dependencies" promise.
"""

from __future__ import annotations

import math
from typing import Any

BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _quote(text: str) -> str:
    out = ['"']
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _key(name: str) -> str:
    return name if name and set(name) <= BARE else _quote(name)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"TOML 不能表示 {value}")
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_key(str(k))} = {_scalar(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if value is None:
        return '""'
    raise TypeError(f"无法序列化的类型：{type(value).__name__}")


def dumps(data: dict[str, Any], header: str = "") -> str:
    """Render ``data`` as TOML text (scalars first, then nested tables)."""

    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
        lines.append("")

    _write_table(data, [], lines, top=True)
    text = "\n".join(lines).rstrip() + "\n"
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _write_table(
    data: dict[str, Any], path: list[str], lines: list[str], *, top: bool = False
) -> None:
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for name, value in scalars.items():
        lines.append(f"{_key(str(name))} = {_scalar(value)}")
    if scalars and (tables or not top):
        lines.append("")

    for name, value in tables.items():
        if not value:
            # An empty table still needs a header so the section exists.
            lines.append(f"[{'.'.join(_key(p) for p in path + [str(name)])}]")
            lines.append("")
            continue
        lines.append(f"[{'.'.join(_key(p) for p in path + [str(name)])}]")
        _write_table(value, path + [str(name)], lines)
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
