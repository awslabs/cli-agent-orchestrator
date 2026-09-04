#!/usr/bin/env python3
"""Rebuild the favicons from docusaurus/static/img/favicon.svg.

The ICO is a committed artifact because browsers still ask for `/favicon.ico`
and it has to carry several sizes in one file. Pillow is not a dependency of
this project and ImageMagick is not assumed to be installed, so this writes the
container by hand: an ICO is a 6-byte header, one 16-byte directory entry per
image, then the image payloads. Payloads may be PNG rather than BMP, which is
what makes a stdlib-only implementation practical.

The web dashboard needs its own copies: Vite only publishes what is under
`web/public/`, and Docusaurus only publishes what is under
`docusaurus/static/`, so neither can reference the other's file. This script
writes both sets from the one source rather than leaving two hand-maintained
copies to drift apart.

Requires `rsvg-convert` (librsvg) on PATH for the SVG rasterization step.

    python3 scripts/build_favicon.py
"""

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
IMG = ROOT / "docusaurus" / "static" / "img"
WEB_PUBLIC = ROOT / "web" / "public"
SOURCE = IMG / "favicon.svg"
TARGETS = (IMG / "favicon.ico", WEB_PUBLIC / "favicon.ico")

# 48 is the largest size Windows and older browsers actually pull from an ICO;
# anything bigger is served by favicon.svg instead.
SIZES = (16, 32, 48)


def rasterize(source: Path, size: int, dest: Path) -> bytes:
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), str(source), "-o", str(dest)],
        check=True,
    )
    return dest.read_bytes()


def pack_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    entries, payloads = b"", b""
    for size, data in images:
        # Width and height are single bytes, where 0 means 256. Every size we
        # emit is under 256, so it can be written directly.
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
        payloads += data
        offset += len(data)
    return header + entries + payloads


def main() -> int:
    if not SOURCE.is_file():
        print(f"missing source: {SOURCE}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        images = [(size, rasterize(SOURCE, size, Path(tmp) / f"{size}.png")) for size in SIZES]

    ico = pack_ico(images)
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ico)
        print(f"wrote {target.relative_to(ROOT)} ({', '.join(f'{s}x{s}' for s in SIZES)})")

    web_svg = WEB_PUBLIC / SOURCE.name
    shutil.copyfile(SOURCE, web_svg)
    print(f"wrote {web_svg.relative_to(ROOT)} (copy of {SOURCE.relative_to(ROOT)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
