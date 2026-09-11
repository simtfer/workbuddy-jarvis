"""Display-width helpers for terminal layout.

Terminals address text in *cells*, not characters: a CJK glyph takes two cells
while an ASCII one takes one. Padding with ``str.ljust`` therefore silently
misaligns any row that mixes the two - ``"模型".ljust(6)`` is 6 characters but 8
cells, so a ``模型`` row drifts two columns right of a ``工具`` row.

Everything the TUI aligns goes through this module so labels, tables and the
sidebar share one measurement.
"""

from __future__ import annotations

import unicodedata

# East Asian Width classes that occupy two cells (Wide and Fullwidth).
_WIDE = ("W", "F")


def dwidth(text: str) -> int:
    """Number of terminal cells ``text`` occupies."""

    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in _WIDE else 1
    return total


def pad(text: str, width: int) -> str:
    """Left-align ``text`` inside ``width`` cells (never truncates)."""

    return text + " " * max(0, width - dwidth(text))


def clip(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate ``text`` to at most ``width`` cells.

    When the text is cut a trailing ``ellipsis`` is appended, so the result is
    never wider than ``width`` and never wider than the caller's box.
    """

    if width <= 0:
        return ""
    if dwidth(text) <= width:
        return text
    budget = max(0, width - dwidth(ellipsis))
    kept = ""
    used = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in _WIDE else 1
        if used + step > budget:
            break
        kept += char
        used += step
    return kept + ellipsis
