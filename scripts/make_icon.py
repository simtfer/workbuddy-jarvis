"""Generate the JARVIS tray icon (src/jarvis/assets/jarvis.ico).

Run manually when the artwork changes:

    uv run python scripts/make_icon.py

Pure standard library: the design is rasterised with plain maths and packed
into a multi-size .ico (16/24/32/48/64) by hand - no Pillow, no new deps.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "assets" / "jarvis.ico"
SIZES = (16, 24, 32, 48, 64)

CORE = (232, 250, 255)
RING = (0, 190, 255)
RING_DIM = (0, 120, 210)
BACKDROP = (13, 24, 42)
RIM = (32, 54, 88)


def _ramp(value: float, low: float, high: float) -> float:
    """0 below ``low``, 1 above ``high``, linear in between (anti-aliasing)."""

    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (value - low) / (high - low)


def _blend(base: tuple[int, int, int], top: tuple[int, int, int], ratio: float):
    return tuple(round(b + (t - b) * ratio) for b, t in zip(base, top))


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """One size as rows of RGBA pixels, drawn in normalised polar coordinates."""

    half = size / 2.0
    rows: list[list[tuple[int, int, int, int]]] = []
    for py in range(size):
        row: list[tuple[int, int, int, int]] = []
        for px in range(size):
            ux = (px + 0.5 - half) / half
            uy = (py + 0.5 - half) / half
            dist = math.hypot(ux, uy)

            alpha = 1.0 - _ramp(dist, 0.93, 1.0)
            if alpha <= 0.0:
                row.append((0, 0, 0, 0))
                continue

            colour = BACKDROP
            colour = _blend(colour, RIM, _ramp(dist, 0.78, 0.88) * (1 - _ramp(dist, 0.88, 0.96)))

            # Outer ring (the reactor coil).
            ring = _ramp(dist, 0.50, 0.56) * (1 - _ramp(dist, 0.72, 0.79))
            if ring:
                shade = _blend(RING_DIM, RING, 1.0 - _ramp(dist, 0.56, 0.72))
                colour = _blend(colour, shade, ring)

            # Inner glow + white-hot core.
            glow = _ramp(dist, 0.20, 0.42) * (1 - _ramp(dist, 0.44, 0.50))
            if glow:
                colour = _blend(colour, RING_DIM, glow * 0.85)
            core = 1.0 - _ramp(dist, 0.14, 0.24)
            if core:
                colour = _blend(colour, CORE, core)

            row.append((colour[0], colour[1], colour[2], round(alpha * 255)))
        rows.append(row)
    return rows


def bmp_for_ico(rows: list[list[tuple[int, int, int, int]]], size: int) -> bytes:
    """BITMAPINFOHEADER + bottom-up BGRA + (unused) AND mask."""

    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight (XOR + AND)
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression
        size * size * 4,
        0, 0, 0, 0,
    )
    body = bytearray()
    for row in reversed(rows):
        for r, g, b, a in row:
            body += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * size)
    return header + bytes(body) + mask


def build_ico(sizes=SIZES) -> bytes:
    images = [(size, bmp_for_ico(render(size), size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset,
        )
        offset += len(data)
    return header + bytes(entries) + b"".join(data for _size, data in images)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = build_ico()
    OUT.write_bytes(data)
    print(f"wrote {OUT} ({len(data)} bytes, sizes={list(SIZES)})")


if __name__ == "__main__":
    main()
