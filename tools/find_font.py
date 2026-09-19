#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Locates a game's bitmap font and its on-screen text boxes.

Run this once per game, against a session showing a screen with prose on it.
It narrows the font address structurally, then ranks the candidates by how much
readable English each one produces, and reports the text boxes it found so they
can be pasted into a profile's "screen" block.

    ./tools/find_font.py --socket /tmp/la.sock
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from hires import (BitmapFont, TextBox, BYTES_PER_ROW, INK_MASK,  # noqa: E402
                   scan_font_tables)
from linapple_client import LinAppleClient  # noqa: E402

UNKNOWN = "�"
# Short, very common words: enough to tell English from noise without shipping
# a dictionary, and none of them appear by chance in mis-decoded glyphs often.
COMMON = re.compile(
    r"\b(the|and|you|your|are|for|not|with|this|that|press|key|disk|game|"
    r"select|insert|enter|yes|no|one|of|to|in|is|it|a)\b", re.I)


def readable_score(lines: list[str]) -> float:
    text = "\n".join(lines)
    letters = sum(1 for c in text if c.isalnum() or c in " .,:;'()-!?")
    unknown = text.count(UNKNOWN)
    visible = sum(1 for c in text if c.strip())
    if visible == 0:
        return 0.0
    words = len(COMMON.findall(text))
    return (letters - unknown * 2) / visible + words * 0.5


def scan_rows(font: BitmapFont, page: bytes, min_letters: int = 5
              ) -> list[tuple[int, int, int, str]]:
    """Finds (y, x_byte, cols, text) runs that decode to readable text."""
    solid = bytes([INK_MASK] * font.height).hex()
    blank = bytes(font.height).hex()
    found = []
    for y in range(0, 192 - font.height + 1):
        for inverse in (False, True):
            cells = []
            for col in range(BYTES_PER_ROW):
                glyph = BitmapFont._cell(page, y, col, font.height)
                if inverse:
                    glyph = bytes(b ^ INK_MASK for b in glyph)
                key = font._key(glyph)
                cells.append(" " if key in (solid, blank)
                             else font.glyphs.get(key, UNKNOWN))
            line = "".join(cells)
            for match in re.finditer(r"[^�]{4,}", line):
                run = match.group()
                if sum(1 for c in run if c.isalnum()) >= min_letters:
                    found.append((y, match.start(), len(run), run.strip()))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/linapple-agent.sock")
    parser.add_argument("--height", type=int, default=8,
                        help="font cell height in scanlines (default: %(default)s)")
    parser.add_argument("--top", type=int, default=5,
                        help="how many candidates to report")
    args = parser.parse_args()

    with LinAppleClient(args.socket) as client:
        page = client.hires_page()
        # Both images, because above $D000 they are different memory. "main"
        # is the language card's own store, which is where Bard's Tale III's
        # font at $D000 is read from; "cpu" is what the program is reading at
        # this instant, and it is the only one that reaches a bank the
        # emulator keeps $1000 lower. Which one a font turned up in has to be
        # carried into the profile, or the reader looks in the wrong table.
        images = {view: client.mem_read(0, 0x10000, view=view)
                  for view in ("main", "cpu")}

    # The sweep stops below $C000 first, where a game's own RAM ends and the
    # I/O page begins. Retrying wider is cheaper than making every game pay
    # for the range, and saying so keeps the surprise visible.
    candidates = [(view, base)
                  for view, memory in images.items()
                  for base in scan_font_tables(memory, height=args.height)]
    if not candidates:
        print("nothing below $C000; retrying up to $FFFF (language card)",
              file=sys.stderr)
        candidates = [(view, base)
                      for view, memory in images.items()
                      for base in scan_font_tables(memory, height=args.height,
                                                   end=0x10000)]
    if not candidates:
        print("no font table found; is the game loaded and showing text?",
              file=sys.stderr)
        print("  a game that spaces its text proportionally, or draws it from "
              "shapes rather than a table indexed by character code, has "
              "nothing for this to find — Lode Runner and Ultima V are both "
              "like that.", file=sys.stderr)
        return 1
    print(f"{len(candidates)} candidate font tables")

    ranked = []
    for view, base in candidates:
        font = BitmapFont.from_bytes(images[view], base, height=args.height)
        runs = scan_rows(font, page)
        score = readable_score([r[3] for r in runs])
        ranked.append((score, base, view, font, runs))
    # A table found in both images is the same table; report the "main" one,
    # which is the steadier of the two to read back.
    ranked.sort(key=lambda item: (item[0], item[2] == "main"), reverse=True)

    seen: set[int] = set()
    shown = []
    for entry in ranked:
        if entry[1] in seen:
            continue
        seen.add(entry[1])
        shown.append(entry)

    for score, base, view, font, runs in shown[:args.top]:
        print(f"\nfont_base {base:#06x}  view {view}  score {score:.2f}  "
              f"{len(font.glyphs)} glyphs  {len(runs)} text runs")
        for y, x_byte, cols, text in runs[:8]:
            print(f"    y={y:3d} x_byte={x_byte:2d} cols={cols:2d}  |{text}|")

    best_score, best_base, best_view, _, best_runs = shown[0]
    if best_runs:
        top = min(r[0] for r in best_runs)
        left = min(r[1] for r in best_runs)
        width = max(r[1] + r[2] for r in best_runs) - left
        view_field = ("" if best_view == "main"
                      else f'"font_view": "{best_view}", ')
        print(f"\nsuggested profile block for font_base {best_base:#06x}:")
        print(f'  "screen": {{ "font_base": {best_base}, "font_height": '
              f'{args.height}, {view_field}"boxes": {{ "message": {{ '
              f'"x_byte": {left}, "y_top": {top}, "cols": {width}, '
              f'"rows": 9 }} }} }}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
