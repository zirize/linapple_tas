#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Reading every dungeon level straight off the disk image, without playing.

The party has to walk to a floor to see it in RAM, and getting to Mangar's
fifth level is a game, not a measurement. But the floor the game puts at
$F800 is copied from the disk byte for byte — checked against Catacombs
levels 1 and 2 — so the whole dungeon can be read from the image on the
shelf.

A level is four sectors of one track, and a track holds two levels:

    map       sectors 15 + 14      484 bytes, the walls, as DungeonMap reads
    specials  sectors 13 + 12      484 bytes, one flag byte per square
    map       sectors  7 +  6      the track's second level
    specials  sectors  5 +  4

The first sector of each pair holds squares 0..255 and the second the
remaining 228; the last 28 bytes of the second sector are unused. On
The Bard's Tale I that is tracks 7 through 14 — sixteen levels, which is
every level in the game.

The specials plane is the one the walls do not tell you about: one flag byte
per square. $01 stairs up, $02 stairs down, $04 a fixed encounter or an
inscription, $08 darkness, $10 a trap, $80 a fixed encounter — all met in the
game on Catacombs level 1. $20 and $40 are still unknown. A flag is CONSUMED
when it fires, so testing one means rewinding to the doorstep and stepping on
again; see README.md.

    ./examples/bards-tale/dungeon_disk.py /path/to/dungeon.dsk
    ./examples/bards-tale/dungeon_disk.py <disk> --level t12.15 --show
    ./examples/bards-tale/dungeon_disk.py <disk> --level t12.15 --specials
    ./examples/bards-tale/dungeon_disk.py <disk> --json levels.json

A level whose walls do not agree with themselves is not a broken read: a
door can be one-way. Catacombs level 2 has a sixteen-square run of them
along x=0, a door going east and a wall coming back west, and the game
plays it exactly that way.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from citymap import DELTAS, DUNGEON_GRID, DungeonMap  # noqa: E402

SECTOR = 256
TRACK = 16 * SECTOR
# Which sectors of a track hold what. The map's first half is the HIGHER
# sector of its pair, which is how the image reads back — no byte of it has
# ever needed reordering.
SLOTS = ((15, 14, 13, 12), (7, 6, 5, 4))
PLANE = DUNGEON_GRID * DUNGEON_GRID          # 484
HALF = SECTOR                                # first sector holds 0..255

# Specials flags. All but two were met in the game on Catacombs level 1 by
# rewinding to the doorstep and stepping on four times — once is worthless,
# because a random encounter can land on any square. $20 and $40 appear once
# or twice a floor and nothing has stood on one yet.
STAIRS_UP = 0x01
STAIRS_DOWN = 0x02
SPECIAL_NAMES = {0x01: "stairs up", 0x02: "stairs down",
                 0x04: "fixed encounter or inscription", 0x08: "darkness",
                 0x10: "trap", 0x20: "unknown", 0x40: "unknown",
                 0x80: "fixed encounter"}
# One letter each, so a square carrying two flags still reads. Order matters:
# the first flag a square has decides its letter.
SPECIAL_MARKS = ((0x01, "U"), (0x02, "D"), (0x10, "T"), (0x04, "M"),
                 (0x80, "E"), (0x08, "k"), (0x20, "?"), (0x40, "?"))


def read_plane(image: bytes, track: int, hi: int, lo: int) -> bytes:
    """One 484-byte plane out of its two sectors."""
    base = track * TRACK
    return (image[base + hi * SECTOR: base + hi * SECTOR + HALF]
            + image[base + lo * SECTOR: base + lo * SECTOR + PLANE - HALF])


@dataclass
class Level:
    """One floor as the disk holds it: walls, flags, and where it came from."""

    track: int
    slot: int                       # 15 or 7 — the map's first sector
    walls: bytes
    specials: bytes
    grid: int = DUNGEON_GRID
    name: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.name = f"t{self.track}.{self.slot}"

    @property
    def map(self) -> DungeonMap:
        return DungeonMap(self.walls, self.grid)

    def agreement(self) -> tuple[int, int, list[str]]:
        """How many of the floor's side-pairs agree, and which do not.

        Every side is stored twice, once in each of the two squares it
        separates, and the wrap makes that true at the edges too. A pair
        that disagrees is a one-way side — measured on Catacombs level 2 —
        so this counts rather than validates.
        """
        floor, ok, disagreements = self.map, 0, []
        for x in range(self.grid):
            for y in range(self.grid):
                for facing in (0, 1):
                    other = floor.neighbour(x, y, facing)
                    mine = floor.side(x, y, facing)
                    theirs = floor.side(*other, facing + 2)
                    if mine == theirs:
                        ok += 1
                    else:
                        disagreements.append(
                            f"({x},{y}) {('north', 'east')[facing]}={mine} vs "
                            f"({other[0]},{other[1]}) "
                            f"{('south', 'west')[facing]}={theirs}")
        return ok, 2 * self.grid * self.grid, disagreements

    def squares_with(self, flag: int) -> list[tuple[int, int]]:
        return [(i // self.grid, i % self.grid)
                for i, v in enumerate(self.specials) if v & flag]

    def stairs_up(self) -> list[tuple[int, int]]:
        return self.squares_with(STAIRS_UP)

    def stairs_down(self) -> list[tuple[int, int]]:
        return self.squares_with(STAIRS_DOWN)

    def flags_seen(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for v in self.specials:
            for bit in range(8):
                if v & (1 << bit):
                    counts[1 << bit] = counts.get(1 << bit, 0) + 1
        return dict(sorted(counts.items()))

    def render_specials(self, party: tuple[int, int] | None = None) -> str:
        """The flag plane, north at the top, the same way up as the walls."""
        rows = []
        for x in range(self.grid - 1, -1, -1):
            row = []
            for y in range(self.grid):
                v = self.specials[x * self.grid + y]
                if party == (x, y):
                    row.append("@")
                elif not v:
                    row.append(".")
                else:
                    row.append(next((mark for flag, mark in SPECIAL_MARKS
                                     if v & flag), "#"))
            rows.append(f"{x:2d} " + "".join(row))
        rows.append("   " + "".join(str(y % 10) for y in range(self.grid)))
        return "\n".join(rows)

    def as_dict(self) -> dict:
        ok, total, _ = self.agreement()
        return {"name": self.name, "track": self.track, "slot": self.slot,
                "walls": list(self.walls), "specials": list(self.specials),
                "agreement": [ok, total],
                "stairs_up": [list(c) for c in self.stairs_up()],
                "stairs_down": [list(c) for c in self.stairs_down()],
                "flags": {f"0x{k:02X}": v for k, v in self.flags_seen().items()}}


def levels(image: bytes, grid: int = DUNGEON_GRID,
           floor_only: bool = True) -> list[Level]:
    """Every level record the image holds, in track order.

    A record is recognised by its walls agreeing with themselves: a real
    floor stores each side twice and the two copies match nearly always,
    which nothing else on the disk does. The threshold is deliberately
    loose because of one-way doors — the worst real level here scores 942
    of 968 and the best non-level 850.
    """
    found = []
    for track in range(len(image) // TRACK):
        for hi, lo, shi, slo in SLOTS:
            walls = read_plane(image, track, hi, lo)
            if len(walls) < grid * grid or not any(walls):
                continue
            level = Level(track, hi, walls,
                          read_plane(image, track, shi, slo), grid)
            ok, total, _ = level.agreement()
            if floor_only and ok / total < 0.92:
                continue
            found.append(level)
    return found


def link(found: list[Level]) -> list[str]:
    """Which floor each staircase lands on.

    Going down at (15,16) of Catacombs level 1 puts the party on (15,16) of
    level 2, standing on that floor's stairs up — the game keeps the
    coordinates and only changes the floor. So a down-staircase names its
    destination: the level whose up-staircase is on the same square.
    """
    up = {}
    for level in found:
        for cell in level.stairs_up():
            up.setdefault(cell, []).append(level.name)
    lines = []
    for level in found:
        for cell in level.stairs_down():
            goes = up.get(cell, [])
            lines.append(f"{level.name} ({cell[0]},{cell[1]}) down -> "
                         + (" or ".join(goes) if goes else "no match"))
    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="a dungeon disk image (.dsk)")
    parser.add_argument("--level", help="which level, as printed (e.g. t12.15)")
    parser.add_argument("--show", action="store_true", help="draw the walls")
    parser.add_argument("--specials", action="store_true",
                        help="draw the flag plane")
    parser.add_argument("--disagreements", action="store_true",
                        help="list the one-way sides of --level")
    parser.add_argument("--json", help="write every level to this file")
    parser.add_argument("--all", action="store_true",
                        help="keep records that do not look like floors")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image = Path(args.image).read_bytes()
    found = levels(image, floor_only=not args.all)
    if not found:
        print("no dungeon levels in this image")
        return 1

    if args.level:
        chosen = next((f for f in found if f.name == args.level), None)
        if chosen is None:
            print(f"no such level {args.level} — have "
                  f"{', '.join(f.name for f in found)}")
            return 1
        ok, total, bad = chosen.agreement()
        print(f"{chosen.name}: sides agreeing {ok}/{total} · "
              f"up {chosen.stairs_up()} · down {chosen.stairs_down()}")
        if args.show or not (args.specials or args.disagreements):
            print(chosen.map.render())
            print("\n0-4 ways out of that square, doors counted")
        if args.specials:
            print(chosen.render_specials())
            print("\nU up · D down · T trap · M inscription · E encounter · "
                  "k darkness · ? unknown · . nothing")
            print("flags: " + " ".join(
                f"0x{k:02X}×{v}({SPECIAL_NAMES.get(k, '?')})"
                for k, v in chosen.flags_seen().items()))
        if args.disagreements:
            print(f"{len(bad)} sides that open only one way:")
            for line in bad:
                print("  " + line)
        return 0

    print(f"{Path(args.image).name}: {len(found)} levels")
    for level in found:
        ok, total, bad = level.agreement()
        print(f"  {level.name:8s} sides {ok}/{total}"
              f"{f' (one-way {len(bad)})' if bad else '':>14s}"
              f"  up {level.stairs_up()} down {level.stairs_down()}"
              f"  flags " + " ".join(f"0x{k:02X}x{v}"
                                    for k, v in level.flags_seen().items()))
    print("\nwhere the stairs lead:")
    for line in link(found):
        print("  " + line)
    if args.json:
        Path(args.json).write_text(
            json.dumps([f.as_dict() for f in found], indent=2), encoding="utf-8")
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
