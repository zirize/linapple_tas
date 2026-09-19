#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Finding the party's position and the level map inside the game's RAM.

Bard's Tale draws a first-person view, so the screen never says where the party
is standing. Walking blind through a 22x22 grid to find a door is hopeless, but
the data has to be in memory: the level is loaded from disk as a grid, and the
party's square is two counters the movement code increments. This tool finds
both the way a cheat-search tool does — by taking full 64KB snapshots and
letting the party's own movement tell the addresses apart.

The searches, in the order they are meant to be used:

    dump     one snapshot, for looking at later
    diff     what changed between two snapshots
    track    walk a straight line and keep the bytes that count 1 per step
    facing   turn in place and keep the bytes that cycle through four values
    probe    press each of a set of keys and report which ones move the party

Once `track` has named the coordinate bytes, `walls` walks the grid and records
which directions are blocked, and `match` searches the snapshot for a 484-byte
table whose bits agree with those observations.

Snapshots live in a directory as raw 64KB files plus an index, so a hunt can be
picked up again in another session without replaying the walk.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from hires import CachedScreen  # noqa: E402
from linapple_client import LinAppleClient  # noqa: E402

DEFAULT_SOCKET = "/tmp/linapple-agent.sock"
GRID = 22
CELLS = GRID * GRID

# The hi-res pages are redrawn on every step, so they drown out any search that
# asks "what changed". Nothing the party's position lives in is in there.
SCREEN_PAGES = ((0x2000, 0x4000), (0x4000, 0x6000))


def in_screen(addr: int) -> bool:
    return any(lo <= addr < hi for lo, hi in SCREEN_PAGES)


# --- snapshots -------------------------------------------------------------

@dataclass
class Snapshot:
    label: str
    memory: bytes
    meta: dict = field(default_factory=dict)

    def __getitem__(self, addr: int) -> int:
        return self.memory[addr]


class SnapshotStore:
    """A directory of raw 64KB dumps with a JSON index beside them."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.index_path = self.path / "index.json"
        self.index: list[dict] = []
        if self.index_path.is_file():
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))

    def add(self, snap: Snapshot) -> Path:
        target = self.path / f"{snap.label}.bin"
        target.write_bytes(snap.memory)
        self.index = [e for e in self.index if e["label"] != snap.label]
        self.index.append({"label": snap.label, "file": target.name, **snap.meta})
        self.index_path.write_text(json.dumps(self.index, indent=2), encoding="utf-8")
        return target

    def get(self, label: str) -> Snapshot:
        entry = next((e for e in self.index if e["label"] == label), None)
        if entry is None:
            raise KeyError(f"no snapshot labelled {label!r} in {self.path}")
        memory = (self.path / entry["file"]).read_bytes()
        meta = {k: v for k, v in entry.items() if k not in ("label", "file")}
        return Snapshot(label, memory, meta)

    def labels(self) -> list[str]:
        return [e["label"] for e in self.index]


def capture(client: LinAppleClient, label: str, meta: dict | None = None) -> Snapshot:
    """One whole-memory snapshot. `mem_read` hands back the memory image, not a
    bus read, so sweeping the I/O page here does not touch a soft switch."""
    return Snapshot(label, client.mem_read(0, 0x10000), dict(meta or {}))


# --- searches --------------------------------------------------------------

def changed(a: Snapshot, b: Snapshot, skip_screen: bool = True) -> list[int]:
    return [addr for addr in range(0x10000)
            if a.memory[addr] != b.memory[addr]
            and not (skip_screen and in_screen(addr))]


def runs(addrs: Sequence[int], gap: int = 1) -> list[tuple[int, int]]:
    """Collapses a sorted address list into (start, length) runs."""
    out: list[tuple[int, int]] = []
    for addr in addrs:
        if out and addr - (out[-1][0] + out[-1][1]) < gap:
            start, length = out[-1]
            out[-1] = (start, addr - start + 1)
        else:
            out.append((addr, 1))
    return out


def counting_bytes(snaps: Sequence[Snapshot], skip_screen: bool = True
                   ) -> list[tuple[int, int]]:
    """Addresses whose value moves by the same non-zero step at every snapshot.

    A coordinate counter is the only thing in memory that does this along a
    straight walk, which is why the walk has to be straight: a turn in the
    middle would break the run and hide the byte we are after.
    """
    if len(snaps) < 3:
        raise ValueError("need at least three snapshots to see a pattern")
    found = []
    for addr in range(0x10000):
        if skip_screen and in_screen(addr):
            continue
        values = [s.memory[addr] for s in snaps]
        step = (values[1] - values[0]) % 256
        if step == 0 or step > 8 and step < 248:
            continue
        if all((values[i + 1] - values[i]) % 256 == step
               for i in range(len(values) - 1)):
            found.append((addr, step - 256 if step > 127 else step))
    return found


def cycling_bytes(snaps: Sequence[Snapshot], period: int = 4,
                  skip_screen: bool = True) -> list[tuple[int, list[int]]]:
    """Addresses that take `period` distinct values and return to the first.

    Facing is the obvious one: four turns in the same direction bring it back to
    where it started, and almost nothing else in memory does that on cue.
    """
    if len(snaps) < period + 1:
        raise ValueError(f"need at least {period + 1} snapshots")
    found = []
    for addr in range(0x10000):
        if skip_screen and in_screen(addr):
            continue
        values = [s.memory[addr] for s in snaps]
        cycle = values[:period]
        if len(set(cycle)) != period:
            continue
        if all(values[i] == cycle[i % period] for i in range(len(values))):
            found.append((addr, cycle))
    return found


def stable_regions(snaps: Sequence[Snapshot], size: int = CELLS,
                   skip_screen: bool = True) -> list[int]:
    """Start addresses of runs of at least `size` bytes that never changed.

    The level grid is loaded once and then only read, so it sits inside such a
    run — along with all the code, which is why this is a filter and not an
    answer. `match` is what tells the map from the code.
    """
    same = bytearray(0x10000)
    first = snaps[0].memory
    for addr in range(0x10000):
        if skip_screen and in_screen(addr):
            continue
        same[addr] = all(s.memory[addr] == first[addr] for s in snaps[1:])
    out, start = [], None
    for addr in range(0x10001):
        if addr < 0x10000 and same[addr]:
            if start is None:
                start = addr
        elif start is not None:
            if addr - start >= size:
                out.append(start)
            start = None
    return out


# --- the map ---------------------------------------------------------------

# Directions are kept as the game's own facing numbers rather than compass
# names: the walk measures what each number does, so nothing here has to assume
# that 0 means north or that the table is stored the way a person would draw it.

@dataclass
class WallSample:
    """What one cell turned out to be like, possibly only partly known."""
    x: int
    y: int
    blocked: dict[int, bool] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y,
                "blocked": {str(k): v for k, v in sorted(self.blocked.items())}}

    @classmethod
    def from_json(cls, data: dict) -> "WallSample":
        return cls(int(data["x"]), int(data["y"]),
                   {int(k): bool(v) for k, v in data["blocked"].items()})


def load_samples(path: str | Path) -> list[WallSample]:
    return [WallSample.from_json(d)
            for d in json.loads(Path(path).read_text(encoding="utf-8"))]


def save_samples(path: str | Path, samples: Iterable[WallSample]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps([s.to_json() for s in samples], indent=2), encoding="utf-8")


def cell_index(x: int, y: int, stride: int, order: str) -> int:
    return y * stride + x if order == "row" else x * stride + y


def match_map(memory: bytes, samples: Sequence[WallSample], grid: int,
              strides: Sequence[int] | None = None,
              bases: Sequence[int] | None = None) -> list[dict]:
    """Every (base, stride, order, mask per facing) that agrees with the walk.

    A sample says "standing on (x,y) facing f, the party could not step". A
    table agrees if some mask of bits in that cell's byte is non-zero exactly
    where the party was stopped. Testing a mask rather than a single bit
    matters: these games tend to spend two bits per side — open, wall, door,
    secret door — so a wall has no one bit in common with every other wall.

    For a given direction the widest mask that can work is whatever bits never
    appear on a side the party walked through; if that mask is non-zero on every
    blocked side, it works, and if it is not, no narrower mask does either. So
    one pass over the samples settles each candidate, both for masks that mark
    walls and for masks that mark openings.
    """
    strides = list(strides or sorted({grid, 32}))
    span = max(max(cell_index(s.x, s.y, st, o) for s in samples)
               for st in strides for o in ("row", "column")) + 1
    candidates = bases if bases is not None else range(0, 0x10000 - span + 1)
    facings = sorted({f for s in samples for f in s.blocked})
    by_facing = {f: [(s.x, s.y, s.blocked[f]) for s in samples if f in s.blocked]
                 for f in facings}
    hits = []
    for base in candidates:
        for stride in strides:
            for order in ("row", "column"):
                masks = {}
                for facing in facings:
                    seen_open = seen_blocked = 0
                    values = []
                    for x, y, blocked in by_facing[facing]:
                        value = memory[base + cell_index(x, y, stride, order)]
                        values.append((value, blocked))
                        if blocked:
                            seen_blocked |= value
                        else:
                            seen_open |= value
                    # Bits that never show up on a side the party walked through
                    # can mark walls; bits that never show up on a wall can mark
                    # openings. Either reading is fine as long as it is total.
                    wall_mask = ~seen_open & 0xFF
                    open_mask = ~seen_blocked & 0xFF
                    if wall_mask and all(bool(v & wall_mask) == b for v, b in values):
                        masks[facing] = ("wall", wall_mask)
                    elif open_mask and all(bool(v & open_mask) != b for v, b in values):
                        masks[facing] = ("open", open_mask)
                    else:
                        break
                else:
                    hits.append({"base": base, "stride": stride, "order": order,
                                 "masks": masks})
    return hits


def walkability(samples: Sequence[WallSample],
                deltas: dict[int, tuple[int, int]]) -> dict[tuple[int, int], bool]:
    """Turns "could not step that way" into "that square is a building".

    A dungeon has walls between squares, but a town is made of squares that are
    either street or building, and the walk sees the difference: every square
    the party stood on is street, and every square a step was refused into is
    not. Squares the evidence disagrees about — a one-way gate, a guardian
    statue — are dropped rather than guessed at.
    """
    votes: dict[tuple[int, int], list[bool]] = {}
    for sample in samples:
        votes.setdefault((sample.x, sample.y), []).append(True)
        for facing, blocked in sample.blocked.items():
            delta = deltas.get(facing)
            if delta is None:
                continue
            target = (sample.x + delta[0], sample.y + delta[1])
            votes.setdefault(target, []).append(not blocked)
    return {cell: seen[0] for cell, seen in votes.items()
            if all(v == seen[0] for v in seen)}


def match_cells(memory: bytes, walk: dict[tuple[int, int], bool], grid: int,
                strides: Sequence[int] | None = None,
                bases: Sequence[int] | None = None) -> list[dict]:
    """Every table whose byte tells street squares from building squares apart.

    No assumption about what the bytes mean: the test is only that no value
    appears both where the party walked and where it was refused. A table of
    code fails that almost immediately, which is what makes a search over every
    address in memory practical.
    """
    strides = list(strides or sorted({grid, 32}))
    layouts = [(stride, order) for stride in strides for order in ("row", "column")]
    hits = []
    for stride, order in layouts:
        cells = [(cell_index(x, y, stride, order), ok)
                 for (x, y), ok in walk.items()]
        span = max(i for i, _ in cells) + 1
        # Two squares that must differ, tried first so most addresses are
        # rejected on one comparison instead of a hundred and thirty-five.
        pivot_ok = next(i for i, ok in cells if ok)
        pivot_no = next((i for i, ok in cells if not ok), None)
        if pivot_no is None:
            continue
        for base in (bases if bases is not None else range(0, 0x10000 - span + 1)):
            if memory[base + pivot_ok] == memory[base + pivot_no]:
                continue
            street, building = set(), set()
            for index, ok in cells:
                (street if ok else building).add(memory[base + index])
                if street & building:
                    break
            else:
                hits.append({"base": base, "stride": stride, "order": order,
                             "street": sorted(street),
                             "building": sorted(building)})
    return hits


def render_cells(memory: bytes, base: int, stride: int, order: str,
                 street: Sequence[int], grid: int,
                 party: tuple[int, int] | None = None,
                 legend: dict[int, str] | None = None) -> str:
    """The whole level as the table has it, not only the part that was walked."""
    street = set(street)
    rows = []
    for y in range(grid - 1, -1, -1):
        row = []
        for x in range(grid):
            value = memory[base + cell_index(x, y, stride, order)]
            if party == (x, y):
                row.append("@")
            elif legend and value in legend:
                row.append(legend[value])
            else:
                row.append("." if value in street else "#")
        rows.append(f"{y:2d} " + "".join(row))
    return "\n".join(rows + ["   " + "".join(str(x % 10) for x in range(grid))])


def render_map(memory: bytes, base: int, stride: int, order: str,
               masks: dict[int, tuple[str, int]],
               deltas: dict[int, tuple[int, int]],
               grid: int, party: tuple[int, int] | None = None) -> str:
    """Draws a candidate table as a maze so a person can judge it at a glance:
    a real town comes out as long straight streets, a false hit as noise."""
    def blocked(value: int, facing: int) -> bool:
        sense, mask = masks[facing]
        return bool(value & mask) if sense == "wall" else not (value & mask)

    by_delta = {deltas[f]: f for f in masks if f in deltas}
    north, west, south = by_delta.get((0, 1)), by_delta.get((-1, 0)), by_delta.get((0, -1))
    rows = []
    for y in range(grid - 1, -1, -1):
        top, mid = [], []
        for x in range(grid):
            value = memory[base + cell_index(x, y, stride, order)]
            top.append("+" + ("--" if north is not None and blocked(value, north)
                              else "  "))
            here = "@" if party == (x, y) else " "
            mid.append(("|" if west is not None and blocked(value, west) else " ")
                       + f"{here} ")
        rows.append("".join(top) + "+")
        rows.append("".join(mid) + "|")
    bottom = []
    for x in range(grid):
        value = memory[base + cell_index(x, 0, stride, order)]
        bottom.append("+" + ("--" if south is not None and blocked(value, south)
                             else "  "))
    rows.append("".join(bottom) + "+")
    return "\n".join(rows)


# --- driving the emulator --------------------------------------------------

def key_code(spec: str) -> int:
    """Accepts a character, a number, or one of the arrow names."""
    named = {"left": 0x08, "down": 0x0A, "up": 0x0B, "right": 0x15,
             "return": 0x0D, "escape": 0x1B, "space": 0x20}
    if spec.lower() in named:
        return named[spec.lower()]
    if len(spec) == 1:
        return ord(spec.upper())
    return int(spec, 0)


def step(client: LinAppleClient, key: int, max_frames: int = 600) -> dict:
    """One keystroke, waited out inside the emulator rather than by polling."""
    return client.press_key(key, max_frames=max_frames)


def walk(client: LinAppleClient, key: int, count: int, label: str,
         store: SnapshotStore | None = None) -> list[Snapshot]:
    """Snapshots before the first step and after every one of them."""
    snaps = [capture(client, f"{label}00")]
    for i in range(count):
        step(client, key)
        snaps.append(capture(client, f"{label}{i + 1:02d}"))
    if store is not None:
        for snap in snaps:
            store.add(snap)
    return snaps


# --- recording where a party may walk -------------------------------------
#
# match_map() and match_cells() need samples: a list of squares, each with the
# directions the game refused to move in. Recording those means driving the
# game, and driving a game means knowing its prompts -- which disk it wants,
# what a fight looks like, how it says "you cannot go that way". That part
# cannot be written once for every game, so it is not here. What is here is
# the format, so a recorder for your game can produce it:
#
#   [{"x": 3, "y": 7, "blocked": [0, 2], "special": []}, ...]
#
# `blocked` holds facing numbers the party could not move through from that
# square, `special` holds facings that did something other than move or
# refuse. WallSample, load_samples() and save_samples() above read and write
# it. Walk with step()/walk(), read the party's square out of RAM with the
# addresses `probe`/`track` found, and append a sample per square.

# --- commands --------------------------------------------------------------

def cmd_dump(client: LinAppleClient, args) -> int:
    store = SnapshotStore(args.store)
    path = store.add(capture(client, args.label))
    print(f"{args.label}: {path}")
    return 0


def cmd_diff(client, args) -> int:
    store = SnapshotStore(args.store)
    a, b = store.get(args.a), store.get(args.b)
    addrs = changed(a, b, skip_screen=not args.include_screen)
    print(f"{len(addrs)} bytes differ between {args.a} and {args.b}"
          f"{'' if args.include_screen else ' (hi-res pages excluded)'}")
    for start, length in runs(addrs, gap=args.gap)[:args.top]:
        print(f"  ${start:04X}..${start + length - 1:04X}  {length:5d} bytes")
    return 0


def cmd_probe(client: LinAppleClient, args) -> int:
    """Which key walks the party forward.

    A movement key is the one that makes some byte count by the same step three
    times running; nothing else in memory does that on command.
    """
    for spec in args.keys:
        key = key_code(spec)
        snaps = walk(client, key, args.repeat, f"probe-{spec}-")
        found = counting_bytes(snaps)
        mark = "MOVES" if found else "     "
        detail = ", ".join(f"${a:04X}{d:+d}" for a, d in found[:6])
        print(f"  {spec:>6} (${key:02X})  {mark}  {len(found):3d} counters  {detail}")
    return 0


def cmd_track(client: LinAppleClient, args) -> int:
    store = SnapshotStore(args.store)
    key = key_code(args.key)
    snaps = walk(client, key, args.steps, args.label + "-", store)
    found = counting_bytes(snaps)
    print(f"{args.steps} steps with key {args.key}: {len(found)} counting bytes")
    for addr, delta in found:
        values = [s.memory[addr] for s in snaps]
        print(f"  ${addr:04X}  {delta:+d}  {values}")
    return 0


def cmd_facing(client: LinAppleClient, args) -> int:
    store = SnapshotStore(args.store)
    key = key_code(args.key)
    snaps = walk(client, key, args.turns, args.label + "-", store)
    found = cycling_bytes(snaps, period=4)
    print(f"{args.turns} turns with key {args.key}: {len(found)} cycling bytes")
    for addr, cycle in found:
        print(f"  ${addr:04X}  cycle {cycle}")
    return 0


def cmd_stable(client, args) -> int:
    store = SnapshotStore(args.store)
    labels = args.labels or store.labels()
    snaps = [store.get(l) for l in labels]
    bases = stable_regions(snaps, size=args.size)
    print(f"{len(bases)} runs of >= {args.size} unchanged bytes "
          f"across {len(snaps)} snapshots")
    for base in bases[:args.top]:
        print(f"  ${base:04X}")
    return 0


def cmd_match(client, args) -> int:
    store = SnapshotStore(args.store)
    snap = store.get(args.snapshot)
    samples = load_samples(args.samples)
    bases = None
    if args.stable:
        snaps = [store.get(l) for l in store.labels()]
        bases = stable_regions(snaps, size=args.grid * args.grid)
        print(f"restricting to {len(bases)} unchanged runs")
    strides = [int(s, 0) for s in args.stride] if args.stride else None
    hits = match_map(snap.memory, samples, grid=args.grid, strides=strides,
                     bases=bases)
    known = sum(len(s.blocked) for s in samples)
    print(f"{len(hits)} candidate tables agree with {known} observations "
          f"over {len(samples)} cells")
    for hit in hits[:args.top]:
        masks = " ".join(f"f{k}={sense[0]}${mask:02X}"
                         for k, (sense, mask) in sorted(hit["masks"].items()))
        print(f"  base ${hit['base']:04X}  stride {hit['stride']:2d}  "
              f"{hit['order']}-major  {masks}")
    if hits and args.render:
        deltas_path = Path(args.samples).with_suffix(".deltas.json")
        deltas = {}
        if deltas_path.is_file():
            deltas = {int(k): tuple(v) for k, v in
                      json.loads(deltas_path.read_text(encoding="utf-8")).items()}
        hit = hits[0]
        print()
        print(render_map(snap.memory, hit["base"], hit["stride"], hit["order"],
                         hit["masks"], deltas, args.grid))
    return 0


def cmd_cells(client, args) -> int:
    store = SnapshotStore(args.store)
    snap = store.get(args.snapshot)
    samples = load_samples(args.samples)
    deltas_path = Path(args.samples).with_suffix(".deltas.json")
    deltas = {int(k): tuple(v) for k, v in
              json.loads(deltas_path.read_text(encoding="utf-8")).items()}
    walk = walkability(samples, deltas)
    street = sum(1 for ok in walk.values() if ok)
    print(f"{len(walk)} squares classified: {street} street, "
          f"{len(walk) - street} building")
    strides = [int(s, 0) for s in args.stride] if args.stride else None
    hits = match_cells(snap.memory, walk, grid=args.grid, strides=strides)
    print(f"{len(hits)} candidate tables")
    for hit in hits[:args.top]:
        print(f"  base ${hit['base']:04X}  stride {hit['stride']:2d}  "
              f"{hit['order']}-major  street values "
              f"{[hex(v) for v in hit['street']]}")
    if hits and args.render:
        hit = hits[0]
        print()
        print(render_cells(snap.memory, hit["base"], hit["stride"], hit["order"],
                           hit["street"], args.grid))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--store", default="memscan-store",
                        help="directory to keep snapshots in (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dump", help="take one snapshot")
    p.add_argument("label")
    p.set_defaults(func=cmd_dump, needs_client=True)

    p = sub.add_parser("diff", help="compare two snapshots")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--gap", type=int, default=1)
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--include-screen", action="store_true")
    p.set_defaults(func=cmd_diff, needs_client=False)

    p = sub.add_parser("probe", help="find which key moves the party")
    p.add_argument("keys", nargs="+")
    p.add_argument("--repeat", type=int, default=3)
    p.set_defaults(func=cmd_probe, needs_client=True)

    p = sub.add_parser("track", help="walk straight and find the coordinate bytes")
    p.add_argument("--key", default="up")
    p.add_argument("--steps", type=int, default=4)
    p.add_argument("--label", default="walk")
    p.set_defaults(func=cmd_track, needs_client=True)

    p = sub.add_parser("facing", help="turn in place and find the facing byte")
    p.add_argument("--key", default="right")
    p.add_argument("--turns", type=int, default=8)
    p.add_argument("--label", default="turn")
    p.set_defaults(func=cmd_facing, needs_client=True)

    p = sub.add_parser("stable", help="runs of bytes that never changed")
    p.add_argument("labels", nargs="*")
    p.add_argument("--size", type=int, default=CELLS)
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(func=cmd_stable, needs_client=False)


    p = sub.add_parser("cells", help="find a table that marks street from building")
    p.add_argument("snapshot")
    p.add_argument("samples")
    p.add_argument("--grid", type=int, default=GRID)
    p.add_argument("--stride", action="append")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--render", action="store_true")
    p.set_defaults(func=cmd_cells, needs_client=False)

    p = sub.add_parser("match", help="find a grid that agrees with walked walls")
    p.add_argument("snapshot")
    p.add_argument("samples", help="JSON list of wall samples")
    p.add_argument("--grid", type=int, default=GRID)
    p.add_argument("--stride", action="append",
                   help="cell stride to try; repeatable (default: grid and 32)")
    p.add_argument("--stable", action="store_true",
                   help="only look inside byte runs that never changed")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--render", action="store_true")
    p.set_defaults(func=cmd_match, needs_client=False)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.needs_client:
        return args.func(None, args)
    with LinAppleClient(args.socket) as client:
        return args.func(client, args)


if __name__ == "__main__":
    raise SystemExit(main())
