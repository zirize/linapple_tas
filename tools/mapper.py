# SPDX-License-Identifier: GPL-2.0-only
"""Dungeon mapping: a record of where the party has been and what it found.

🔴 The premise this was written on is gone. It said the movement commands are
the only reliable position source without game-specific memory addresses —
true in general, false for Bard's Tale, where the party's square is at $00,
$01 and its facing at $02, underground as well as in the city. Counting moves
is not merely redundant there, it is wrong: a step fails against a wall, an
encounter interrupts one, and running from a fight throws the party halfway
across the map. Dead reckoning records all three as a normal step and every
later cell is off by one.

So `observe()` is the way to use this against a game whose coordinate bytes
are known, and it is the only way used for Bard's Tale. `forward()` and the
turns are kept for a game where they are not known yet — they are a guess,
and a guess that this project has already been bitten by.

What is worth keeping either way is the bookkeeping: one cell per square,
what the screen said on arrival, coverage, an ASCII render and a JSON dump.

Levels are 22x22, which matches the 484 bytes the game keeps at $F800 for a
floor. `wrap` defaults to True and Catacombs 1 says it should: walking west
off (21,0) lands on (21,21) and east off (19,21) lands on (19,0), measured
2026-09-18. An earlier note here said the opposite on the strength of one
refused step at (21,21); that step was the thing that was wrong. Six squares
of that floor are reachable no other way. Coordinates are (x, y) with x
growing north and y growing east, which is what the game's own bytes and the
walkthroughs use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Facing order is clockwise from north so a right turn is +1 modulo four.
#
# 🔴 Which way "north" moves the party was wrong here until 2026-09-18. The
# game was asked: at facing 0 it prints "Facing north" and x goes up; facing
# 1 is east and y goes up. That is the same convention the published
# walkthroughs write as N<x> E<y>, and the same one $00/$01 use.
NORTH, EAST, SOUTH, WEST = range(4)
FACING_NAMES = ("N", "E", "S", "W")
FACING_DELTA = {NORTH: (1, 0), EAST: (0, 1), SOUTH: (-1, 0), WEST: (0, -1)}


@dataclass
class Cell:
    x: int
    y: int
    visits: int = 0
    notes: set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "visits": self.visits,
                "notes": sorted(self.notes)}


class LevelMap:
    def __init__(self, level: int, size: int = 22, wrap: bool = True):
        self.level = level
        self.size = size
        self.wrap = wrap
        self.cells: dict[tuple[int, int], Cell] = {}

    def normalize(self, x: int, y: int) -> tuple[int, int]:
        if self.wrap:
            return x % self.size, y % self.size
        return max(0, min(self.size - 1, x)), max(0, min(self.size - 1, y))

    def visit(self, x: int, y: int, note: str | None = None) -> Cell:
        key = self.normalize(x, y)
        cell = self.cells.get(key)
        if cell is None:
            cell = Cell(key[0], key[1])
            self.cells[key] = cell
        cell.visits += 1
        if note:
            cell.notes.add(note)
        return cell

    def coverage(self) -> float:
        return len(self.cells) / float(self.size * self.size)

    def render(self, party: tuple[int, int] | None = None) -> str:
        """ASCII map, north at the top — north being +x, so rows are x."""
        rows = []
        for x in range(self.size - 1, -1, -1):
            row = []
            for y in range(self.size):
                if party == (x, y):
                    row.append("@")
                elif (x, y) not in self.cells:
                    row.append(".")
                elif self.cells[(x, y)].notes:
                    row.append("*")
                else:
                    row.append("#")
            rows.append(f"{x:2d} " + "".join(row))
        footer = "   " + "".join(str(y % 10) for y in range(self.size))
        return "\n".join(rows + [footer])

    def to_json(self) -> dict:
        return {
            "level": self.level,
            "size": self.size,
            "wrap": self.wrap,
            "cells": [c.to_json() for c in self.cells.values()],
        }


class Mapper:
    """Tracks the party across levels while it issues movement commands."""

    def __init__(self, size: int = 22, wrap: bool = True):
        self.size = size
        self.wrap = wrap
        self.levels: dict[int, LevelMap] = {}
        self.level = 1
        self.x = 0
        self.y = 0
        self.facing = NORTH
        self.current.visit(self.x, self.y, "start")

    @property
    def current(self) -> LevelMap:
        if self.level not in self.levels:
            self.levels[self.level] = LevelMap(self.level, self.size, self.wrap)
        return self.levels[self.level]

    # -- position ----------------------------------------------------------

    def observe(self, client, note: str | None = None,
                addr_x: int = 0x00, addr_y: int = 0x01,
                addr_facing: int = 0x02) -> Cell:
        """Records where the party actually is, read out of the game.

        Prefer this over counting moves wherever the addresses are known: a
        step that did not happen is then visible as a square that did not
        change, instead of quietly shifting the rest of the map.
        """
        block = client.mem_read(min(addr_x, addr_y, addr_facing), 3)
        lo = min(addr_x, addr_y, addr_facing)
        self.x = block[addr_x - lo]
        self.y = block[addr_y - lo]
        self.facing = block[addr_facing - lo]
        return self.current.visit(self.x, self.y, note)

    # -- movement by guesswork ----------------------------------------------
    # Only for a game whose coordinate bytes are not known yet. See the note
    # at the top of this file before reaching for these.

    def turn_left(self) -> None:
        self.facing = (self.facing - 1) % 4

    def turn_right(self) -> None:
        self.facing = (self.facing + 1) % 4

    def forward(self, note: str | None = None) -> Cell:
        dx, dy = FACING_DELTA[self.facing]
        self.x, self.y = self.current.normalize(self.x + dx, self.y + dy)
        return self.current.visit(self.x, self.y, note)

    def change_level(self, level: int, keep_position: bool = True) -> None:
        self.level = level
        if not keep_position:
            self.x = self.y = 0
        self.current.visit(self.x, self.y, f"entered level {level}")

    def note(self, text: str) -> None:
        self.current.visit(self.x, self.y, text)

    # -- reporting ---------------------------------------------------------

    def position(self) -> str:
        return (f"L{self.level} ({self.x},{self.y}) facing "
                f"{FACING_NAMES[self.facing]}")

    def render(self) -> str:
        return (f"{self.position()}  coverage "
                f"{self.current.coverage() * 100:.1f}%\n"
                f"{self.current.render(party=(self.x, self.y))}")

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "party": {"level": self.level, "x": self.x, "y": self.y,
                      "facing": FACING_NAMES[self.facing]},
            "levels": [lvl.to_json() for lvl in self.levels.values()],
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
