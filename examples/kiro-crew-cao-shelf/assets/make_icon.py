#!/usr/bin/env python3
"""Draw assets/icon.png (512x512, opaque) with the standard library only.

Run: python3 assets/make_icon.py  (rewrites icon.png next to this file).
The picture is a shelf: three boards, each carrying a few cards of different height.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 512
BG = (24, 30, 44)
BOARD = (96, 112, 140)
CARDS = [(86, 156, 214), (115, 201, 145), (232, 176, 75), (224, 108, 117)]


def main() -> None:
    px = [[BG for _ in range(SIZE)] for _ in range(SIZE)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(max(0, y0), min(SIZE, y1)):
            row = px[y]
            for x in range(max(0, x0), min(SIZE, x1)):
                row[x] = color

    for shelf in range(3):
        base = 150 + shelf * 130
        rect(64, base, 448, base + 12, BOARD)
        x = 84
        for i in range(4):
            w = 66 if (i + shelf) % 2 else 80
            h = 70 + 18 * ((i * 2 + shelf) % 3)
            rect(x, base - h, x + w, base, CARDS[(i + shelf) % len(CARDS)])
            x += w + 14
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    (Path(__file__).resolve().parent / "icon.png").write_bytes(png)


if __name__ == "__main__":
    main()
