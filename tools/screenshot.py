#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Saves the emulator's current frame as a PNG.

Most Apple II games draw their interface into hi-res graphics rather than the
hardware text page, so `screen_text` returns nothing useful for them and the
only way to see what the machine is showing is to look at the framebuffer.

    ./tools/screenshot.py --socket /tmp/la.sock --out shot.png

Writes a BMP instead when Pillow is unavailable, so it never fails for want of
an optional dependency.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from linapple_client import LinAppleClient  # noqa: E402


def to_rgb_rows(width: int, height: int, bgra: bytes) -> list[bytes]:
    """Converts the emulator's BGRA8888 buffer into per-row RGB triples."""
    rows = []
    stride = width * 4
    for y in range(height):
        row = bytearray()
        base = y * stride
        for x in range(width):
            pixel = base + x * 4
            row += bytes((bgra[pixel + 2], bgra[pixel + 1], bgra[pixel]))
        rows.append(bytes(row))
    return rows


def write_bmp(path: Path, width: int, height: int, rows: list[bytes]) -> None:
    padding = b"\x00" * ((4 - (width * 3) % 4) % 4)
    # BMP stores rows bottom-up.
    pixels = b"".join(row + padding for row in reversed(rows))
    header_size = 14 + 40
    path.write_bytes(
        b"BM"
        + struct.pack("<IHHI", header_size + len(pixels), 0, 0, header_size)
        + struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                      len(pixels), 2835, 2835, 0, 0)
        + pixels
    )


def save(path: Path, width: int, height: int, bgra: bytes) -> Path:
    rows = to_rgb_rows(width, height, bgra)
    try:
        from PIL import Image
    except ImportError:
        target = path.with_suffix(".bmp")
        write_bmp(target, width, height, rows)
        return target

    image = Image.frombytes("RGB", (width, height), b"".join(rows))
    image.save(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/linapple-agent.sock")
    parser.add_argument("--out", default="screenshot.png")
    parser.add_argument("--advance", type=int, default=0,
                        help="frames to run before capturing")
    args = parser.parse_args()

    with LinAppleClient(args.socket) as client:
        if args.advance:
            client.run_frames(args.advance, deterministic=False)
        width, height, pixels = client.screen_capture()
    written = save(Path(args.out), width, height, pixels)
    print(f"{written} ({width}x{height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
