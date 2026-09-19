#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Reading Skara Brae's map out of the game's memory and walking to a square.

The Bard's Tale shows a first-person view, so a tool that only looks at the
screen cannot tell a temple from any other wall: the party has to open the
door to find out what is behind it, and there are 900 squares to try. The map
is in RAM, though, laid out exactly as the game's own movement code reads it:

    $F800 + (29 - x) * 30 + y        one byte per square, 30x30

A byte of 0 is open street. Everything else is a building, and the value says
which building — the Adventurers Guild is the only $0B on the map, so once one
of those values has been identified at one door, every other instance is known
without walking anywhere.

    ./examples/bards-tale/citymap.py --show                 draw the map
    ./examples/bards-tale/citymap.py --goto 15 24           walk there
    ./examples/bards-tale/citymap.py --survey               knock on every special square
    ./examples/bards-tale/citymap.py --dungeon              read $F800 as a dungeon floor
    ./examples/bards-tale/citymap.py --dungeon-goto 0 18    walk there, opening doors
    ./examples/bards-tale/citymap.py --dungeon-probe        measure this square's four sides

A door opens to RETURN, not to walking into it, which is why --survey knocks
rather than walks; see README.md. That holds underground too — a
dungeon door and a secret door both ignore a forward step and both open to
RETURN, which is what codes 2 and 3 of the floor map turned out to be. The
same $F800 holds a 22x22 dungeon floor while the party is underground, at a
different address and in a different encoding — see DungeonMap.

How the address was found is in README.md; this tool assumes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "clients" / "python"))

from hires import HiresTextReader, TextBox  # noqa: E402
from linapple_client import LinAppleClient  # noqa: E402
from walker import FORWARD, CityWalker  # noqa: E402

DEFAULT_SOCKET = "/tmp/linapple-agent.sock"
# The same address holds the city while the party is in the city and the
# dungeon level while it is underground — 900 bytes for Skara Brae, 484 for a
# 22x22 dungeon floor. Measured 2026-09-18 from inside the Catacombs.
MAP_BASE = 0xF800
GRID = 30
DUNGEON_GRID = 22

# Squares the party can stand on. $00 is plain street. $68 is street too — the
# search walked over three of them. $60 is a gate watched by a statue that
# stops the party and offers a fight, so routes go around one rather than
# through it; it is listed here to say that leaving it out is deliberate.
STREET = 0x00
GATE = frozenset((0x60,))
WALKABLE = frozenset((0x00, 0x68))

# Which way each facing number steps, measured rather than assumed. The same
# numbers appear at $02 as the party turns.
DELTAS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}

# The key that opens a door. Not the forward key: walking into a building does
# nothing, which cost this project a session to find out. Underground it is
# the same key and the same lesson — a dungeon door ignores (I) too.
ENTER = 13

# How a probe result reads in the log. None is "the party ended up somewhere
# that is not the next square", which measures nothing.
SAYS = {True: "open", False: "blocked", None: "dropped"}

# Every shop, temple and guild lives on the character disk, so a walk that
# cannot answer "Insert CHARACTER disk" stops at the first door it opens.
# No disk images ship with this repository. Point BT_DISKS at your own, or
# pass each one on the command line.
DISK_DIR = Path(os.environ.get("BT_DISKS", "dsk"))
DISKS = {"BOOT": str(DISK_DIR / "bards_tale_boot.dsk"),
         "CHARACTER": str(DISK_DIR / "bards_tale_character.dsk"),
         "DUNGEON": str(DISK_DIR / "bards_tale_dungeon.dsk")}


# A door shut for the night reads like an empty building unless the words are
# kept: Garth's says "closed ... come back in the morning", the Review Board
# "closed for the evening". Telling those apart from an empty building is the
# whole reason this survey is being run a second time.
CLOSED = ("closed", "come back in the morning", "for the evening")
EMPTY = ("empty building",)
# A fight can land on the doorstep and answer in the building's place. That
# reading says nothing about the building and has to be taken again.
INTERRUPTED = ("you face", "stalwart band", "brings your", "hits for",
               "swings at", "chops at", "still face")
# Tracks something time-like: it counts down as the party walks and wraps.
# NOT CONFIRMED as the clock - logged with every observation so that a later
# session can line it up against doors that were shut.
CLOCK_BYTE = 0xEC
SURVEY_RETRIES = 3


def strip_hud(text: list[str]) -> list[str]:
    """Drops the party's hit-point column.

    The screen reader scans the whole display, so every reading carries the
    six numbers down the side. Left in, a square that said nothing at all
    still looks like it answered.
    """
    return [line for line in text if line.strip() and not line.strip().isdigit()]


def classify(text: list[str]) -> str:
    """Closed, empty, or a building that said its name."""
    blob = " ".join(strip_hud(text)).lower()
    if any(m in blob for m in CLOSED):
        return "closed"
    if any(m in blob for m in EMPTY):
        return "empty"
    if any(m in blob for m in INTERRUPTED):
        return "interrupted"
    return "named" if blob.strip() else "no answer"


def cell_addr(x: int, y: int, grid: int = GRID) -> int:
    return MAP_BASE + (grid - 1 - x) * grid + y


class CityMap:
    """The 900 bytes the game uses to decide where the party may walk."""

    def __init__(self, data: bytes, labels: dict[int, str] | None = None,
                 grid: int = GRID, walkable: frozenset[int] = WALKABLE):
        if len(data) != grid * grid:
            raise ValueError(f"expected {grid * grid} bytes, got {len(data)}")
        self.data = data
        self.grid = grid
        # A guarded gate is passable once its statue has been broken, and the
        # map byte does not change when it is, so whether $60 counts as ground
        # is the caller's decision rather than the map's.
        self.walkable_values = frozenset(walkable)
        self.labels = dict(labels or {})

    @classmethod
    def from_client(cls, client: LinAppleClient,
                    labels: dict[int, str] | None = None,
                    grid: int = GRID,
                    walkable: frozenset[int] = WALKABLE) -> "CityMap":
        return cls(client.mem_read(MAP_BASE, grid * grid), labels, grid,
                   walkable)

    def value(self, x: int, y: int) -> int:
        return self.data[(self.grid - 1 - x) * self.grid + y]

    def walkable(self, x: int, y: int) -> bool:
        return (0 <= x < self.grid and 0 <= y < self.grid
                and self.value(x, y) in self.walkable_values)

    def specials(self) -> dict[int, list[tuple[int, int]]]:
        """Every non-street value and where it appears, rarest first.

        The rare ones are the interesting ones: a value that occurs once is a
        building there is only one of, which is what the Guild, the temples and
        Garth's all are.
        """
        found: dict[int, list[tuple[int, int]]] = {}
        for x in range(self.grid):
            for y in range(self.grid):
                value = self.value(x, y)
                if value in WALKABLE or value <= 0x04:
                    continue
                found.setdefault(value, []).append((x, y))
        return dict(sorted(found.items(), key=lambda kv: len(kv[1])))

    # -- routing -----------------------------------------------------------

    def path(self, start: tuple[int, int], goal: tuple[int, int],
             avoid: set[tuple[tuple[int, int], tuple[int, int]]] | None = None
             ) -> list[tuple[int, int]] | None:
        """Shortest street route to `goal`, which may itself be a building.

        A building is entered from the street square in front of its door, so
        the goal is allowed to be unwalkable: the route stops beside it and the
        caller takes the last step deliberately.

        `avoid` holds steps the party has already tried and failed. The map
        says nothing about an iron gate that turns the party around, so
        without this a walker keeps planning the same route into it.
        """
        avoid = avoid or set()
        if start == goal:
            return [start]
        seen = {start}
        queue = deque([(start, [start])])
        while queue:
            cell, route = queue.popleft()
            for dx, dy in DELTAS.values():
                nxt = (cell[0] + dx, cell[1] + dy)
                if (cell, nxt) in avoid:
                    continue
                if nxt == goal:
                    return route + [nxt]
                if nxt in seen or not self.walkable(*nxt):
                    continue
                seen.add(nxt)
                queue.append((nxt, route + [nxt]))
        return None

    # -- drawing -----------------------------------------------------------

    def render(self, party: tuple[int, int] | None = None,
               marks: dict[tuple[int, int], str] | None = None) -> str:
        marks = marks or {}
        rows = []
        for y in range(self.grid - 1, -1, -1):
            row = []
            for x in range(self.grid):
                value = self.value(x, y)
                if party == (x, y):
                    row.append("@")
                elif (x, y) in marks:
                    row.append(marks[(x, y)])
                elif value in WALKABLE:
                    row.append("." if value == STREET else "+")
                elif value in GATE:
                    row.append("X")
                elif value <= 0x04:
                    row.append("#")
                else:
                    row.append("?")
            rows.append(f"{y:2d} " + "".join(row))
        rows.append("   " + "".join(str(x % 10) for x in range(self.grid)))
        return "\n".join(rows)

    def legend(self) -> str:
        lines = [". street   + special street   X guarded gate   "
                 "# building   ? named building"]
        for value, cells in self.specials().items():
            name = self.labels.get(value, "")
            where = " ".join(f"({x},{y})" for x, y in cells[:6])
            more = f" +{len(cells) - 6} more" if len(cells) > 6 else ""
            lines.append(f"  ${value:02X} x{len(cells):<3d} {name:<28s} {where}{more}")
        return "\n".join(lines)


class DungeonMap:
    """A 22x22 dungeon floor, which the game keeps where the city map was.

    The address is NOT the city's formula. Skara Brae is
    $F800 + (29 - x) * 30 + y; a dungeon floor is $F800 + x * 22 + y, and
    reading it the city's way is what made an earlier attempt at this
    nonsense. Each byte holds its square's four sides, two bits each, and a
    pair of ZERO means the party can walk that way. Which pair belongs to
    which facing is not the obvious order:

        facing 0 (north, +x) -> pair 0      facing 2 (south, -x) -> pair 1
        facing 1 (east,  +y) -> pair 2      facing 3 (west,  -y) -> pair 3

    Fitted against 45 sides that were actually walked, on Catacombs level 1,
    against 576 candidate readings; this one scored 45 of 45 and the next
    best 82%. Then checked on 56 squares it had never seen: 219 of 220
    sides. The one miss was (21,21) reading open off the edge of the floor,
    and it was the measurement that was wrong, not the map: **the floor
    wraps**. Walking west off (21,0) lands on (21,21), and east off (19,21)
    lands on (19,0). The walk is the only valid measurement — see the
    warning below.

    The wrap is in the encoding too. Across the y edge all 22 pairs agree —
    side((x,21), east) == side((x,0), west) — and so do all 22 across the x
    edge, which is the same thing the interior does (462 of 462). Nine of
    the y pairs are not walls and eight of those nine were walked, both
    ways; every x pair on this floor is a wall, which is why the wrap went
    unnoticed. Six squares are reachable only this way.

    Codes, all four now met in the game (2026-09-18):

        0  passage   a step goes through
        1  wall      nothing gets through it
        2  door      drawn as a door; RETURN opens it, (I) will not
        3  secret door   drawn as plain wall; RETURN opens it, (I) will not

    🔑 RETURN is the key that opens a door, exactly as it is in the city —
    walking forward into one does nothing at all and says nothing either.
    That is why 2 and 3 read as "shut" for three sessions. (I) still tells
    the three apart, so wall-measuring steps should use (I) and travel
    should use RETURN.

    Code 3 is the only way into squares nothing else reaches: of the three
    on this floor, two open into a one-square room with an inscription in it
    ("Fifteen doors east…" at (0,18), "The ancient witch king yet lives…" at
    (16,21)). Code 2 is 82 sides; from (0,0) the reachable count runs
    327 squares through passages alone, 473 with doors, 478 with secret
    doors. The last six are two pockets whose remaining sides point off the
    floor, so they are either a wrap this floor was said not to have or a
    way in from somewhere else; not measured yet.

    🔴 Do not measure walls by writing the party's coordinates. A forked
    party is refused on sides the map calls open — reproducibly, and
    differently depending on the square the state was saved on, so whatever
    the game caches on entering a square is not repaired by the write. It is
    usable as transport: after ONE ordinary step the square the party walks
    into measures true (checked on a square whose sides were known). A door
    will not open on the step straight after a fork.
    """

    # facing -> which two-bit field of the byte, measured not assumed
    SIDE_OF_FACING = (0, 2, 1, 3)
    OPEN = 0
    WALL = 1
    DOOR = 2
    SECRET = 3
    # Sides the party gets through, given the right key. A door and a secret
    # door both need RETURN; only OPEN yields to a forward step.
    PASSABLE = frozenset((OPEN, DOOR, SECRET))

    def __init__(self, data: bytes, grid: int = DUNGEON_GRID):
        if len(data) != grid * grid:
            raise ValueError(f"expected {grid * grid} bytes, got {len(data)}")
        self.data = data
        self.grid = grid

    @classmethod
    def from_client(cls, client: LinAppleClient,
                    grid: int = DUNGEON_GRID) -> "DungeonMap":
        return cls(client.mem_read(MAP_BASE, grid * grid), grid)

    def value(self, x: int, y: int) -> int:
        return self.data[x * self.grid + y]

    def side(self, x: int, y: int, facing: int) -> int:
        """The two bits for one side. 0 lets the party through."""
        return (self.value(x, y) >> (2 * self.SIDE_OF_FACING[facing])) & 0b11

    def open_sides(self, x: int, y: int) -> list[int]:
        """Sides a plain forward step goes through.

        A side can read open and still be shut because it leaves the floor:
        (21,21) reads open to the east and the game refuses it. That was the
        one disagreement in 220 walked sides, and it says the floor does not
        wrap — at least not at that edge.
        """
        return [f for f in range(4) if self.side(x, y, f) == self.OPEN]

    def ways_out(self, x: int, y: int) -> list[int]:
        """Sides the party can leave by at all — doors included.

        This is the one to route with, because RETURN opens a door and a
        secret door. `open_sides` is the one to compare a measurement
        against, because only a passage yields to a forward step.
        """
        return [f for f in range(4) if self.side(x, y, f) in self.PASSABLE]

    def path(self, start: tuple[int, int], goal: tuple[int, int],
             avoid: frozenset = frozenset()) -> list[tuple[int, int]] | None:
        """Shortest route through passages and doors, or None."""
        previous: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        queue = deque([start])
        while queue:
            cell = queue.popleft()
            if cell == goal:
                break
            for facing in self.ways_out(*cell):
                step = self.neighbour(*cell, facing)
                if step in previous or (cell, step) in avoid:
                    continue
                previous[step] = cell
                queue.append(step)
        if goal not in previous:
            return None
        route, cell = [], goal
        while cell is not None:
            route.append(cell)
            cell = previous[cell]
        return route[::-1]

    def neighbour(self, x: int, y: int, facing: int) -> tuple[int, int]:
        """The square one step away — the floor wraps, so this always exists.

        Walking west off (21,0) puts the party on (21,21), measured 2026-09-18,
        and the map agrees on every pair along both edges. An earlier note
        here said the floor does not wrap; it was reading one refused step.
        """
        dx, dy = DELTAS[facing]
        return ((x + dx) % self.grid, (y + dy) % self.grid)

    def render(self, party: tuple[int, int] | None = None) -> str:
        """North at the top, so rows are x — the same way up as the city map."""
        rows = []
        for x in range(self.grid - 1, -1, -1):
            row = []
            for y in range(self.grid):
                if party == (x, y):
                    row.append("@")
                else:
                    row.append(str(len(self.ways_out(x, y))))
            rows.append(f"{x:2d} " + "".join(row))
        rows.append("   " + "".join(str(y % 10) for y in range(self.grid)))
        return "\n".join(rows)


class DungeonNavigator(CityWalker):
    """Walks a dungeon floor the way the game wants to be walked.

    The city taught this project that RETURN is the key that opens a door;
    underground it is the same key, and it is also a perfectly good forward
    step, so travel uses it throughout. A measurement does the opposite: it
    steps with (I), which a door refuses, and that refusal is how a door is
    told from a passage without looking at the map.
    """

    def __init__(self, client: LinAppleClient, reader,
                 floor: "DungeonMap | None" = None, **kwargs):
        super().__init__(client, reader, **kwargs)
        self.deltas = dict(DELTAS)
        self.map = floor if floor is not None else DungeonMap.from_client(client)

    def facing_to(self, cell: tuple[int, int], step: tuple[int, int]) -> int:
        """Which way to turn to get from `cell` to the square next to it.

        Goes through the map's own neighbour(), because the floor wraps and a
        route may well step off one edge onto the other — adding the delta
        raw lands on (-1, y) or (22, y), which matches nothing and used to
        stop the walk with a StopIteration.
        """
        return next(f for f in DELTAS
                    if self.map.neighbour(*cell, f) == step)

    def step(self, key: int = ENTER) -> bool:
        """One step with `key`. True if the party ended up somewhere else."""
        if self.before_step:
            self.before_step()
        before = self.position()
        self.client.press_key(key, max_frames=700)
        self.settle()
        return self.position()[:2] != before[:2]

    def goto(self, goal: tuple[int, int], max_steps: int = 300) -> bool:
        """Walks to `goal`, re-planning from where the party actually is.

        A fight can carry the party off — running away moves it — so the
        route is recomputed every step rather than followed blind.
        """
        refused: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for _ in range(max_steps):
            here = self.position()[:2]
            if here == goal:
                return True
            route = self.map.path(here, goal, avoid=frozenset(refused))
            if route is None or len(route) < 2:
                self.log(f"  no route from ({here[0]},{here[1]}) to {goal}")
                return False
            step = route[1]
            self.face(self.facing_to(here, step))
            if not self.step() and self.position()[:2] == here:
                self.log(f"  {here} -> {step} blocked — routing around it")
                refused.add((here, step))
        return self.position()[:2] == goal

    def probe(self, checkpoint: str) -> list[dict]:
        """Measures the four sides of the square the party is standing on.

        Each side is tried twice from the same rewind — once with (I) and
        once with RETURN — because that pair is what separates a wall from a
        door: a wall refuses both, a door refuses only the first.

        The party must have WALKED onto this square. A square arrived at by
        writing the coordinates measures wrong; see DungeonMap.
        """
        self.settle()
        cell = self.position()[:2]
        self.client.state_save(checkpoint)
        found = []
        for facing in range(4):
            result = {"cell": list(cell), "facing": facing,
                      "code": self.map.side(*cell, facing)}
            for key, name in ((FORWARD, "forward"), (ENTER, "return")):
                self.client.state_load(checkpoint)
                self.client.run_frames(60, deterministic=False)
                self.reader.invalidate()
                self.settle()
                if not self.face(facing):
                    result[name] = None
                    continue
                moved = self.step(key)
                landed = self.position()[:2]
                step = (cell[0] + DELTAS[facing][0], cell[1] + DELTAS[facing][1])
                # A fight the party runs from moves it somewhere else
                # entirely, and that says nothing about the side.
                result[name] = True if landed == step else (
                    False if not moved else None)
            found.append(result)
            self.log(f"  facing {facing}, code {result['code']}: "
                     f"(I) {SAYS[result['forward']]} · "
                     f"RETURN {SAYS[result['return']]}")
        self.client.state_load(checkpoint)
        self.client.run_frames(60, deterministic=False)
        return found


class CityNavigator(CityWalker):
    """A walker that knows the map, so it can be told where to go."""

    def __init__(self, client: LinAppleClient, reader,
                 walkable: frozenset[int] = WALKABLE, **kwargs):
        super().__init__(client, reader, **kwargs)
        self.deltas = dict(DELTAS)
        self.map = CityMap.from_client(client, walkable=walkable)

    def goto(self, goal: tuple[int, int], max_steps: int = 200) -> bool:
        """Walks to `goal`, re-planning whenever a step does not land as planned.

        Steps fail for reasons the map does not model — a fight interrupts, a
        guardian refuses a direction — so the route is recomputed from where
        the party actually is rather than followed blind.
        """
        refused: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for _ in range(max_steps):
            x, y, _ = self.position()
            if (x, y) == goal:
                return True
            route = self.map.path((x, y), goal, avoid=refused)
            if route is None or len(route) < 2:
                self.log(f"  no route from ({x},{y}) to {goal}")
                return False
            nxt = route[1]
            facing = next(f for f, d in DELTAS.items()
                          if (x + d[0], y + d[1]) == nxt)
            self.face(facing)
            if self.try_forward() is False and self.position()[:2] == (x, y):
                # The map thought that side was open and the game disagreed.
                # Remembering it is what stops the walk pacing at a gate until
                # max_steps runs out.
                self.log(f"  ({x},{y}) -> {nxt} is blocked — routing around it")
                refused.add(((x, y), nxt))
        return self.position()[:2] == goal

    def identify(self, cell: tuple[int, int]) -> list[str]:
        """Walks into a building and reads whatever it says before leaving."""
        neighbours = [(cell[0] + dx, cell[1] + dy) for dx, dy in DELTAS.values()]
        for street in neighbours:
            if not self.map.walkable(*street):
                continue
            if not self.goto(street):
                continue
            x, y, _ = self.position()
            facing = next(f for f, d in DELTAS.items()
                          if (x + d[0], y + d[1]) == cell)
            self.face(facing)
            before = self.position()[:2]
            # RETURN is what opens a door. Walking forward into a building
            # does nothing at all — measured 2026-09-18 on the Guild, which
            # has exactly one street neighbour and so cannot be entered any
            # other way. Every "empty building" recorded before that date was
            # read through this same wrong key and is not evidence.
            self.client.press_key(ENTER, max_frames=900)
            lines = self.lines()
            if self.position()[:2] == before and not lines:
                continue          # that side is a blank wall; try another
            text = [line for line in lines if line.strip()]
            self.settle()
            if text:
                return text
        return []


def make_navigator(client: LinAppleClient, font_base: int = 0x1530,
                   verbose: bool = True,
                   checkpoint: str | None = None,
                   disks: dict[str, str] | None = None,
                   cheat: bool = False,
                   through_gates: bool = False) -> CityNavigator:
    reader = HiresTextReader(client, font_base,
                             {"message": TextBox(x_byte=21, y_top=24,
                                                 cols=18, rows=12)})
    if checkpoint:
        client.state_save(checkpoint)
    before_step = None
    if cheat:
        from bt_cheat import heal, stop_encounters   # optional import

        def before_step() -> None:
            heal(client)
            stop_encounters(client)

    return CityNavigator(client, reader, verbose=verbose, checkpoint=checkpoint,
                         disks=DISKS if disks is None else disks,
                         before_step=before_step,
                         walkable=WALKABLE | GATE if through_gates else WALKABLE,
                         fight_guardians=through_gates)


def make_dungeon_navigator(client: LinAppleClient,
                           floor: "DungeonMap | None" = None,
                           font_base: int = 0x1530, verbose: bool = True,
                           checkpoint: str | None = None,
                           cheat: bool = False) -> DungeonNavigator:
    reader = HiresTextReader(client, font_base,
                             {"message": TextBox(x_byte=21, y_top=24,
                                                 cols=18, rows=12)})
    before_step = None
    if cheat:
        from bt_cheat import heal          # optional import

        def before_step() -> None:
            heal(client)

    return DungeonNavigator(client, reader, floor=floor, verbose=verbose,
                            checkpoint=checkpoint, disks=DISKS,
                            before_step=before_step)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--labels", default=str(HERE / "profiles" / "skara_brae.json"),
                        help="JSON of {value: building name} (default: %(default)s)")
    parser.add_argument("--show", action="store_true", help="draw the map")
    parser.add_argument("--dungeon", action="store_true",
                        help="read $F800 as a 22x22 dungeon floor instead")
    parser.add_argument("--dungeon-goto", nargs=2, type=int, metavar=("X", "Y"),
                        help="walk to a square of the dungeon floor, opening "
                             "doors and secret doors on the way")
    parser.add_argument("--dungeon-probe", action="store_true",
                        help="measure the four sides of the square the party "
                             "is standing on, with (I) and with RETURN")
    parser.add_argument("--goto", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--survey", action="store_true",
                        help="walk into every named square and record what it says")
    parser.add_argument("--out", help="where --survey writes its findings")
    parser.add_argument("--checkpoint",
                        help="save state here and rewind to it if the party dies")
    parser.add_argument("--character-disk", default=DISKS["CHARACTER"],
                        help="image to feed the 'Insert CHARACTER disk' prompt. "
                             "Point it at a copy whose party cannot die to "
                             "survey the whole city in one pass "
                             "(default: %(default)s)")
    parser.add_argument("--through-gates", action="store_true",
                        help="smash the statue on a guarded gate and route "
                             "through it. Four squares of the city are behind "
                             "one; only sensible with --cheat, because a level "
                             "1 party loses that fight")
    parser.add_argument("--cheat", action="store_true",
                        help="hold hit points and the encounter counter while "
                             "walking, so a survey finishes on one life. Pair "
                             "it with --character-disk")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    labels = {}
    if Path(args.labels).is_file():
        # Keys starting with an underscore carry provenance rather than a
        # tile value — where a name came from matters here, because most of
        # these were read off a walkthrough and checked against the map in
        # RAM, not off a sign inside the game.
        labels = {int(k, 0): v for k, v in
                  json.loads(Path(args.labels).read_text(encoding="utf-8")).items()
                  if not k.startswith("_")}

    with LinAppleClient(args.socket) as client:
        x, y, facing = (client.mem_read(0, 3))
        if args.dungeon or args.dungeon_goto or args.dungeon_probe:
            floor = DungeonMap.from_client(client)
            if args.dungeon_goto or args.dungeon_probe:
                nav = make_dungeon_navigator(client, verbose=not args.quiet,
                                             floor=floor,
                                             checkpoint=args.checkpoint,
                                             cheat=args.cheat)
            if args.dungeon_goto:
                goal = (args.dungeon_goto[0], args.dungeon_goto[1])
                ok = nav.goto(goal)
                print(f"{'arrived' if ok else 'failed'}: {nav.position()[:2]} (goal {goal})")
                if not args.dungeon_probe:
                    return 0 if ok else 1
            if args.dungeon_probe:
                checkpoint = args.checkpoint or "/tmp/bt1-dungeon-probe.state"
                found = nav.probe(checkpoint)
                if args.out:
                    Path(args.out).write_text(json.dumps(found, indent=2),
                                              encoding="utf-8")
                    print(f"-> {args.out}")
                return 0
            print(floor.render(party=(x, y)))
            print("\n0-4 ways out of that square, doors counted   @ the party\n"
                  "code 0 = open, 1 = wall, 2 = door, 3 = secret door —\n"
                  "all four met in the game. A door and a secret door open\n"
                  "to RETURN; walking into one (I) does nothing.")
            print(f"({x},{y}) facing {facing}: {floor.open_sides(x, y)} sides a step "
                  f"gets out of, {floor.ways_out(x, y)} counting doors")
            return 0
        city = CityMap.from_client(
            client, labels,
            walkable=WALKABLE | GATE if args.through_gates else WALKABLE)
        if args.show or not (args.goto or args.survey):
            print(city.render(party=(x, y)))
            print()
            print(city.legend())
            print(f"\nparty at ({x},{y}) facing {facing}")
        disks = dict(DISKS, CHARACTER=args.character_disk)
        if args.goto:
            nav = make_navigator(client, verbose=not args.quiet,
                                 checkpoint=args.checkpoint, disks=disks,
                                 cheat=args.cheat,
                                 through_gates=args.through_gates)
            goal = (args.goto[0], args.goto[1])
            ok = nav.goto(goal)
            print(f"{'arrived' if ok else 'failed'}: {nav.position()[:2]} (goal {goal})")
            for line in nav.lines()[:6]:
                print(f"  {line}")
            return 0 if ok else 1
        if args.survey:
            nav = make_navigator(client, verbose=not args.quiet,
                                 checkpoint=args.checkpoint, disks=disks,
                                 cheat=args.cheat,
                                 through_gates=args.through_gates)
            found = {}
            for value, cells in city.specials().items():
                if nav.trapped():
                    nav.restore()
                text = strip_hud(nav.identify(cells[0]))
                # A fight that lands on the doorstep answers in the building's
                # place, and that reading is worth nothing. Unreachable squares
                # are not retried — they will be just as unreachable next time.
                for _ in range(SURVEY_RETRIES):
                    if classify(text) != "interrupted":
                        break
                    nav.settle()
                    text = strip_hud(nav.identify(cells[0]))
                # Garth's and the Review Board close at night, and a closed
                # door reads exactly like an empty one, so the reading is
                # worth nothing without the hour it was taken at. $EC is the
                # best candidate for the clock and has not been confirmed.
                kind = classify(text)
                found[f"0x{value:02X}"] = {"cell": list(cells[0]),
                                           "kind": kind,
                                           "clock_EC": client.peek(CLOCK_BYTE),
                                           "text": text}
                print(f"${value:02X} {cells[0]} [{kind}] "
                      f"$EC={client.peek(CLOCK_BYTE):3d}: {' / '.join(text[:3])}",
                      flush=True)
            if args.out:
                Path(args.out).write_text(json.dumps(found, indent=2),
                                          encoding="utf-8")
                print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
