# SPDX-License-Identifier: GPL-2.0-only
"""Hi-res page decoding and bitmap-font text extraction.

Many Apple II games — Bard's Tale among them — draw their entire interface,
including all prose, into the hi-res page with their own bitmap font. The
hardware text page holds nothing for those games, so reading the screen means
turning the framebuffer back into characters.

Rather than learning glyph shapes from screenshots, this module lifts the
game's own font out of RAM and inverts it into a bitmap-to-character lookup.
The result is exact: every glyph the game can draw is in the table, spelled the
way the game spells it, with no training pass and nothing to mislabel.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

HIRES_PAGE1 = 0x2000
HIRES_PAGE2 = 0x4000
HIRES_SIZE = 0x2000

WIDTH = 280
HEIGHT = 192
BYTES_PER_ROW = 40
PIXELS_PER_BYTE = 7
INK_MASK = 0x7F  # bit 7 selects the colour palette, not a pixel


def row_offset(row: int) -> int:
    """Byte offset of a hi-res scanline within the page.

    The Apple II interleaves scanlines in three nested groups rather than
    storing them in order, which is why this is a formula and not row * 40.
    """
    return (((row & 0x07) << 10) | (((row >> 3) & 0x07) << 7)
            | ((row >> 6) * BYTES_PER_ROW))


def read_page(data: bytes) -> list[list[int]]:
    """Decodes 8 KB of hi-res memory into a 192 x 280 grid of 0/1 pixels."""
    if len(data) < HIRES_SIZE:
        raise ValueError(f"hi-res page needs {HIRES_SIZE} bytes, got {len(data)}")
    rows = []
    for y in range(HEIGHT):
        base = row_offset(y)
        row = [0] * WIDTH
        for byte_index in range(BYTES_PER_ROW):
            value = data[base + byte_index]
            for bit in range(PIXELS_PER_BYTE):
                row[byte_index * PIXELS_PER_BYTE + bit] = (value >> bit) & 1
        rows.append(row)
    return rows


def render_ascii(pixels: list[list[int]], x: int = 0, y: int = 0,
                 width: int = WIDTH, height: int = HEIGHT,
                 on: str = "#", off: str = ".") -> str:
    """Renders a region as ASCII art, for eyeballing layout by hand."""
    lines = []
    for row in range(y, min(y + height, HEIGHT)):
        lines.append("".join(
            on if pixels[row][col] else off
            for col in range(x, min(x + width, WIDTH))
        ))
    return "\n".join(lines)


def glyph_ascii(glyph: bytes, on: str = "#", off: str = ".") -> str:
    """Renders one glyph's bytes as ASCII art. Bit 0 is the leftmost pixel."""
    return "\n".join(
        "".join(on if (byte >> bit) & 1 else off for bit in range(PIXELS_PER_BYTE))
        for byte in glyph
    )


@dataclass(frozen=True)
class TextBox:
    """Where a game's text sits inside the hi-res page.

    `x_byte` is a byte column, not a pixel column: game fonts are drawn a whole
    byte at a time, so cells always start on a seven-pixel boundary.
    """

    x_byte: int
    y_top: int
    cols: int
    rows: int
    cell_height: int = 8
    line_spacing: int = 8

    def row_top(self, row: int) -> int:
        return self.y_top + row * self.line_spacing


class BitmapFont:
    """A game's own font, inverted into a bitmap-to-character lookup.

    `base` is the address the game indexes by character code, so a glyph lives
    at `base + code * height`. Finding it once for a game is the whole job;
    after that the table is exact.
    """

    def __init__(self, glyphs: dict[str, str], height: int = 8,
                 base: int | None = None, name: str = ""):
        self.height = height
        self.base = base
        self.name = name
        # key: glyph bytes as hex, value: the character
        self.glyphs: dict[str, str] = dict(glyphs)

    # -- construction ------------------------------------------------------

    @staticmethod
    def _key(glyph: bytes) -> str:
        return bytes(b & INK_MASK for b in glyph).hex()

    @classmethod
    def from_bytes(cls, memory: bytes, base: int, height: int = 8,
                   codes: Iterable[int] = range(0x20, 0x7F),
                   name: str = "") -> "BitmapFont":
        """Builds a table from a RAM image the game's font lives in."""
        glyphs: dict[str, str] = {}
        for code in codes:
            start = base + code * height
            glyph = memory[start:start + height]
            if len(glyph) < height:
                continue
            key = cls._key(glyph)
            # A blank cell is whitespace whatever character produced it, and
            # duplicate shapes resolve to the first (lowest) code, which keeps
            # the common letter rather than an obscure synonym.
            glyphs.setdefault(key, chr(code))
        return cls(glyphs, height=height, base=base, name=name)

    @classmethod
    def from_client(cls, client, base: int, height: int = 8,
                    codes: Iterable[int] = range(0x20, 0x7F),
                    name: str = "", view: str = "main") -> "BitmapFont":
        """Reads just the font's own bytes out of the running machine.

        Only the table itself is fetched — reading from address zero instead
        would pull in a kilobyte of zero page and miss the font entirely.

        `view` picks which image the bytes come from, and for a font above
        $D000 the two are different tables. "main" is the language card's own
        store, which holds the font whether or not the card is paged in at this
        instant — that is why Bard's Tale III reads reliably at $D000. "cpu" is
        what the program is reading right now, which is the only one that finds
        a font in the bank the emulator keeps $1000 lower. `find_font.py`
        reports which view it found the table in.
        """
        codes = list(codes)
        first, last = min(codes), max(codes)
        start = base + first * height
        length = (last - first + 1) * height
        if start < 0 or start + length > 0x10000:
            raise ValueError(
                f"font table at {base:#06x} does not fit in memory")
        window = client.mem_read(start, length, view=view)
        # from_bytes indexes by absolute address, so present the window as if
        # it were a full image with the table at the right offset.
        return cls.from_bytes(bytes(start) + window, base, height, codes, name)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "BitmapFont":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["glyphs"], height=data.get("height", 8),
                   base=data.get("base"), name=data.get("name", ""))

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "name": self.name,
            "base": self.base,
            "height": self.height,
            "glyphs": self.glyphs,
        }, indent=1, sort_keys=True), encoding="utf-8")

    # -- reading -----------------------------------------------------------

    def lookup(self, glyph: bytes, unknown: str = "�") -> str:
        key = self._key(glyph)
        if int(key, 16) == 0:
            return " "
        return self.glyphs.get(key, unknown)

    def read_box(self, page: bytes, box: TextBox, inverse: bool | None = None,
                 unknown: str = "�") -> list[str]:
        """Decodes a text box out of a raw hi-res page.

        `inverse` tells whether the box is drawn as dark ink on a light panel;
        left as None it is detected per box, since a game mixes both.
        """
        if inverse is None:
            inverse = self._looks_inverse(page, box)

        lines = []
        for row in range(box.rows):
            top = box.row_top(row)
            out = []
            for col in range(box.cols):
                glyph = self._cell(page, top, box.x_byte + col, box.cell_height)
                if inverse:
                    glyph = bytes(b ^ INK_MASK for b in glyph)
                out.append(self.lookup(glyph, unknown))
            lines.append("".join(out).rstrip())
        return lines

    @staticmethod
    def _cell(page: bytes, top: int, byte_col: int, height: int) -> bytes:
        return bytes(
            page[row_offset(top + i) + byte_col] & INK_MASK
            if 0 <= top + i < HEIGHT and 0 <= byte_col < BYTES_PER_ROW else 0
            for i in range(height)
        )

    @staticmethod
    def _looks_inverse(page: bytes, box: TextBox) -> bool:
        """A text panel is inverse when most of its pixels are lit."""
        lit = total = 0
        for row in range(box.rows):
            top = box.row_top(row)
            for i in range(box.cell_height):
                y = top + i
                if not 0 <= y < HEIGHT:
                    continue
                base = row_offset(y)
                for col in range(box.cols):
                    byte_col = box.x_byte + col
                    if not 0 <= byte_col < BYTES_PER_ROW:
                        continue
                    lit += bin(page[base + byte_col] & INK_MASK).count("1")
                    total += PIXELS_PER_BYTE
        return total > 0 and lit * 2 > total


def find_font_base(memory: bytes, page: bytes, samples: list[tuple[str, int, int]],
                   height: int = 8, search: range | None = None) -> list[int]:
    """Finds candidate font base addresses from known on-screen characters.

    Each sample is (character, screen byte column, screen row top). The glyph is
    lifted off the screen, un-inverted, and searched for in RAM; a base address
    that explains every sample is a real hit. This is how a new game's font is
    located the first time.
    """
    candidates: set[int] | None = None
    for char, byte_col, top in samples:
        cell = bytes(page[row_offset(top + i) + byte_col] & INK_MASK
                     for i in range(height))
        found: set[int] = set()
        for pattern in (cell, bytes(b ^ INK_MASK for b in cell)):
            if int(pattern.hex(), 16) == 0:
                continue
            start = 0
            while True:
                index = memory.find(pattern, start)
                if index < 0:
                    break
                base = index - ord(char) * height
                if base >= 0 and (search is None or base in search):
                    found.add(base)
                start = index + 1
        candidates = found if candidates is None else (candidates & found)
        if not candidates:
            return []
    return sorted(candidates or [])


class HiresTextReader:
    """Reads a game's on-screen text through its own font.

    A game loads its font as part of loading itself, and can replace it when it
    swaps in a new overlay, so the base address alone is not enough: the reader
    re-extracts the table whenever decoding starts producing unknown glyphs.
    Without that, a font captured a moment too early — before the disk has
    loaded it — yields a table of garbage that fails silently.
    """

    def __init__(self, client, font_base: int, boxes: dict[str, TextBox],
                 height: int = 8, min_quality: float = 0.75,
                 view: str = "main"):
        self.client = client
        self.font_base = font_base
        self.boxes = dict(boxes)
        self.height = height
        self.min_quality = min_quality
        self.view = view
        self.font: BitmapFont | None = None

    def refresh_font(self) -> BitmapFont:
        self.font = BitmapFont.from_client(self.client, self.font_base,
                                           height=self.height, view=self.view)
        return self.font

    @staticmethod
    def quality(lines: Iterable[str], unknown: str = "�") -> float:
        """Share of non-blank characters that decoded to a real glyph."""
        total = bad = 0
        for line in lines:
            for char in line:
                if not char.strip():
                    continue
                total += 1
                if char == unknown:
                    bad += 1
        return 1.0 if total == 0 else 1.0 - (bad / total)

    def read(self, page: bytes | None = None) -> dict[str, list[str]]:
        """Decodes every configured box, refreshing the font if it looks stale."""
        if page is None:
            page = self.client.hires_page()
        if self.font is None:
            self.refresh_font()

        result = self._read_all(page)
        if self.quality(line for lines in result.values() for line in lines) \
                < self.min_quality:
            self.refresh_font()
            result = self._read_all(page)
        return result

    def _read_all(self, page: bytes) -> dict[str, list[str]]:
        assert self.font is not None
        return {name: self.font.read_box(page, box)
                for name, box in self.boxes.items()}

    def text(self, page: bytes | None = None) -> str:
        """All boxes joined into one blob, for pattern matching."""
        boxes = self.read(page)
        return "\n".join(
            line for name in self.boxes for line in boxes.get(name, [])
        )

    def scan(self, page: bytes | None = None, min_letters: int = 3
             ) -> list[str]:
        """Reads every line of text on screen, wherever it happens to sit.

        A fixed box works while a game keeps its text on one grid, but this one
        moves it: menus start at one scanline and battle narration at another,
        and being six pixels out turns the whole read to noise. Scanning every
        scanline costs more than a box lookup and stops alignment from being
        something that has to be right.
        """
        if page is None:
            page = self.client.hires_page()
        if self.font is None:
            self.refresh_font()
        assert self.font is not None

        runs = self._scan_with_font(page, min_letters)
        if not runs:
            # A font captured before the game finished loading is a table of
            # noise, and every cell then misses. One refresh distinguishes that
            # from a screen that genuinely holds no text.
            self.refresh_font()
            runs = self._scan_with_font(page, min_letters)
        return runs

    def _scan_with_font(self, page: bytes, min_letters: int) -> list[str]:
        assert self.font is not None
        solid = bytes([INK_MASK] * self.font.height).hex()
        blank = bytes(self.font.height).hex()
        found: list[str] = []
        seen: set[str] = set()
        for y in range(0, HEIGHT - self.font.height + 1):
            for inverse in (False, True):
                cells = []
                for col in range(BYTES_PER_ROW):
                    glyph = BitmapFont._cell(page, y, col, self.font.height)
                    if inverse:
                        glyph = bytes(b ^ INK_MASK for b in glyph)
                    key = self.font._key(glyph)
                    cells.append(" " if key in (solid, blank)
                                 else self.font.glyphs.get(key, "\ufffd"))
                line = "".join(cells)
                for match in re.finditer(r"[^\ufffd]{3,}", line):
                    run = match.group().strip()
                    if sum(1 for c in run if c.isalnum()) < min_letters:
                        continue
                    if run in seen:
                        continue
                    seen.add(run)
                    found.append(run)
        return found




def scan_font_tables(memory: bytes, height: int = 8,
                     first: int = 0x20, last: int = 0x7E,
                     start: int = 0x0800, end: int = 0xC000) -> list[int]:
    """Finds font tables in a RAM image without needing known on-screen text.

    A bitmap font indexed by character code has a shape worth looking for: the
    space glyph is blank, the letters are not, no byte sets bit 7 (that bit
    picks a colour rather than a pixel), and every glyph leaves its last row
    clear for line spacing. Requiring all of that at once is specific enough
    that false hits are rare, which makes this the first thing to run against a
    game whose font address nobody knows yet.
    """
    letters = [ord(c) for c in "AEIOUaeiou"]
    hits: list[int] = []
    for base in range(start - first * height, end - last * height, 1):
        if base < 0:
            continue
        space = base + 0x20 * height
        if memory[space:space + height] != bytes(height):
            continue
        ok = True
        for code in letters:
            glyph = memory[base + code * height: base + (code + 1) * height]
            if len(glyph) < height or not any(glyph):
                ok = False
                break
            if any(byte & 0x80 for byte in glyph):
                ok = False
                break
            if glyph[-1] != 0:  # bottom row reserved for line spacing
                ok = False
                break
        if ok:
            hits.append(base)
    return hits


class CachedScreen:
    """A screen reader that decodes only when the screen has actually changed.

    Scanning every scanline through the game's own font is a glyph lookup per
    cell in Python, and it is the most expensive thing this tool does to the
    host — measured at 46% of a core against the emulator's 33%, which is what
    pushes the clock up and the fan with it. The screen hash is a few bytes and
    one round trip. `engine.ScreenSource` has gated its reads this way since
    the heat was first tracked down; walking and fighting bypassed it until the
    frequency was measured rather than assumed.
    """

    def __init__(self, client, reader):
        self.client = client
        self.reader = reader
        self._digest: int | None = None
        self._lines: list[str] | None = None

    def scan(self, *args, **kwargs) -> list[str]:
        digest = self.client.screen_hash()
        if digest == self._digest and self._lines is not None:
            return self._lines
        self._digest = digest
        self._lines = self.reader.scan(*args, **kwargs)
        return self._lines

    def invalidate(self) -> None:
        self._digest = None

    def __getattr__(self, name):
        return getattr(self.reader, name)
