#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""The half of a map survey that knows one particular game.

`memscan` can tell which squares a party has stood on and which sides of them
it could not leave, but only if something walks the party around and answers
whatever the game puts on the screen. That answering is all game: The Bard's
Tale interrupts a walk with fights, buildings, stairs, statues and a request
for a different floppy, and every one of them swallows the keystroke that was
meant to be a step.

`CityWalker` is that other half for this one game. It writes `WallSample`s in
the format `memscan.load_samples` reads, so the general tools take its output
without knowing any of this. Writing the same class for another game is the
work described in ../../docs/new-game.md; this is what the finished thing
looks like.

Nothing here is importable knowledge — the strings below are this game's, and
a different game will interrupt in different words.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "clients" / "python"))

from linapple_client import LinAppleClient  # noqa: E402
from memscan import WallSample, runs, step, walk  # noqa: E402


FORWARD = ord("I")

# Things the city interrupts a walk with. None of them are failures; they just
# have to be answered before the party can be asked to take another step.
# Every place the party can walk into offers a way out spelled the same way —
# "(E)xit building", "(E)xit Guild", "(E)xit the Temple" — so the exit is found
# by the bracket rather than by listing the buildings.
BUILDING = ("(E)xit", "You are in", "Thou art in")
PAUSED = ("Pausing",)
# Between rounds a battle only narrates — no menu, no encounter line — and that
# screen swallows keys just like a menu does. A step that was swallowed looks
# exactly like a step into a wall, which is how a whole session's worth of
# "that side is blocked" got recorded wrong; see README.md, "A step that was
# swallowed looks like a wall".
NARRATION = ("hits for", "You still face", "swings at", "chops at",
             "gnaws on", "casts a", "is burnt for", "slashes at",
             "repelled the", "killing him", "bites at", "misses")
# Stairs ask before they are taken, and the question waits for Y or N.
STAIRS = ("wish to take", "[Y-N]")
# Some squares just say their piece and wait — "…beyond which stands
# Kylearan's tower. Press a key to head back." Left unanswered the screen
# sits there and the walk spins on it.
PRESS_ANY = ("Press a key", "press a key", "(Press a key)")
# The game asks for a disk by name. Which image that is depends on the game, so
# the walker is told the mapping rather than guessing it.
DISK_PROMPT = "Insert "
# Some street squares are gates watched by a statue. Standing on one puts up
# "You can: (A)ttack it / (L)eave it and head back." A level 1 party leaves;
# a party that cannot die attacks, and a broken statue lets everyone through
# afterwards — four squares of Skara Brae are only reachable that way.
GUARDIAN = ("(L)eave it",)
GUARDIAN_ATTACK = "A"
GUARDIAN_LEAVE = "L"
# The Guild is where a wiped party ends up, and a dead party cannot walk back
# out — but it is also a building the survey knocks on deliberately, and the
# screen looks the same either way. The difference is behavioural: a live
# party leaves when told to, so settle() asks it to leave and only calls the
# walk trapped once (E)xit has been ignored this many times.
TRAPPED = ("Thou art in the", "Guild of")
GUILD_EXIT_TRIES = 4


class CityWalker:
    """Walks the city and writes down which sides of each square are blocked.

    Position comes out of RAM rather than dead reckoning, so a step that fails
    is visible as a step that failed instead of silently shifting every later
    cell. That is the whole reason to find the coordinate bytes first: with
    them a blocked side is a fact, without them it is a guess.
    """

    def __init__(self, client: LinAppleClient, reader, addr_x: int = 0x00,
                 addr_y: int = 0x01, addr_facing: int = 0x02,
                 verbose: bool = True, checkpoint: str | None = None,
                 combat_preference: Sequence[str] = ("run", "flee", "fight",
                                                     "attack foes",
                                                     "cast a spell", "defend"),
                 disks: Mapping[str, str] | None = None,
                 before_step: Callable[[], None] | None = None,
                 fight_guardians: bool = False):
        self.client = client
        self.reader = reader if isinstance(reader, CachedScreen) \
            else CachedScreen(client, reader)
        self.addr_x, self.addr_y, self.addr_facing = addr_x, addr_y, addr_facing
        self.verbose = verbose
        self.checkpoint = checkpoint
        # Mapping is not adventuring: a fight the party runs from costs a few
        # keystrokes, and one it stands and loses costs the whole walk.
        self.combat_preference = tuple(combat_preference)
        # None means "tried, but it led somewhere that is not the next square"
        # — a shop doorway. Remembering it keeps the walk from stepping into
        # the same shop forever, and keeps it out of the wall evidence.
        # {"CHARACTER": "...character.dsk", ...}: which image to put in drive 1
        # when the game asks for it by name. Without this a walk stops dead at
        # the first shop, because every shop lives on the character disk.
        self.disks = dict(disks or {})
        # Called before every step and every settle. The survey uses it to
        # hold the cheats on: a level 1 party cannot survive 900 squares, and
        # one write of the hit points is undone by the next keypress.
        self.before_step = before_step
        # Attack the statue on a guarded gate instead of backing off it.
        # Only worth setting when the party cannot lose the fight.
        self.fight_guardians = fight_guardians
        self.samples: dict[tuple[int, int], dict[int, bool | None]] = {}
        self.deltas: dict[int, tuple[int, int]] = {}
        self.visits: dict[tuple[int, int], int] = {}

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    # -- state -------------------------------------------------------------

    def position(self) -> tuple[int, int, int]:
        lo = min(self.addr_x, self.addr_y, self.addr_facing)
        hi = max(self.addr_x, self.addr_y, self.addr_facing)
        block = self.client.mem_read(lo, hi - lo + 1)
        return (block[self.addr_x - lo], block[self.addr_y - lo],
                block[self.addr_facing - lo])

    def lines(self) -> list[str]:
        return self.reader.scan()

    def settle(self, quiet_needed: int = 2, settle_frames: int = 30
               ) -> list[str]:
        """Answers whatever the city has put in the way of the next step.

        A screen that is still being drawn looks exactly like a quiet one, so
        returning on the first quiet read hands the caller a prompt that has
        not appeared yet — and the next keypress falls into it. Two quiet
        reads in a row, with frames run between them, is the difference
        between a turn that happens and a turn that is swallowed.
        """
        from bt_combat import CombatDriver  # imported late; combat is optional

        if self.before_step:
            self.before_step()

        driver = CombatDriver(self.client, self.reader,
                              preference=self.combat_preference,
                              verbose=self.verbose)
        guild_tries = 0
        quiet = 0
        for _ in range(24 + quiet_needed):
            self.client.run_frames(settle_frames, deterministic=False)
            self.reader.invalidate()
            lines = self.lines()
            blob = " ".join(lines)
            disk = self.wanted_disk(blob)
            if disk:
                self.log(f"  disk wanted: {disk[0]}")
                self.client.disk_insert(0, disk[1])
                self.client.press_key(13, max_frames=700)
                quiet = 0
                continue
            if any(m in blob for m in PAUSED) or any(m in blob for m in PRESS_ANY):
                self.client.press_key(ord(" "), max_frames=400)
                quiet = 0
                continue
            if driver.in_combat(lines):
                self.log("  a fight — answering it")
                driver.fight()
                quiet = 0
                continue
            # After the menus are gone the battle keeps narrating, and that
            # screen is checked last so it never steals a round from the driver.
            if any(m in blob for m in NARRATION):
                self.client.press_key(13, max_frames=700)
                quiet = 0
                continue
            if all(m in blob for m in STAIRS):
                self.log("  stairs — not taking them")
                self.client.press_key(ord("N"), max_frames=700)
                quiet = 0
                continue
            if any(m in blob for m in GUARDIAN):
                if self.fight_guardians:
                    self.log(f"  guardian: {' / '.join(lines[:3])} — attacking")
                    self.client.press_key(ord(GUARDIAN_ATTACK), max_frames=800)
                else:
                    self.log(f"  guardian: {' / '.join(lines[:3])} — leaving it")
                    self.client.press_key(ord(GUARDIAN_LEAVE), max_frames=800)
                quiet = 0
                continue
            if all(m in blob for m in TRAPPED):
                quiet = 0
                guild_tries += 1
                if guild_tries > GUILD_EXIT_TRIES:
                    self.log("  cannot get out of the Guild — taking the party for dead")
                    return lines
                self.client.press_key(ord("E"), max_frames=800)
                continue
            if any(m in blob for m in BUILDING):
                self.log(f"  inside: {' / '.join(lines[:2])} — leaving")
                self.client.press_key(ord("E"), max_frames=800)
                quiet = 0
                continue
            quiet += 1
            if quiet >= quiet_needed:
                return lines
        return self.lines()

    def wanted_disk(self, blob: str) -> tuple[str, str] | None:
        """The image the game is asking for, if it is asking for one."""
        if DISK_PROMPT not in blob:
            return None
        for name, path in self.disks.items():
            if f"{DISK_PROMPT}{name}" in blob:
                return name, path
        return None

    # -- moving ------------------------------------------------------------

    def face(self, target: int, tries: int = 4) -> bool:
        """Turns to `target`, checking that the turn actually happened.

        A turn key is swallowed like any other when something is waiting on
        screen, and a party that did not turn then walks the wrong way — or
        opens the wrong door. Reading $02 back is the only way to know.
        """
        for attempt in range(tries):
            _, _, facing = self.position()
            if facing == target:
                return True
            if attempt:
                self.settle()
            turns = (target - facing) % 4
            if turns == 3:
                self.client.press_key(TURN_LEFT, max_frames=400)
            else:
                for _ in range(turns):
                    self.client.press_key(TURN_RIGHT, max_frames=400)
        turned = self.position()[2] == target
        if not turned:
            self.log(f"  could not turn to face {target} (facing {self.position()[2]})")
        return turned

    def try_forward(self) -> bool | None:
        """One step. True if it went through, False if a wall stopped it.

        None means the party ended up somewhere that is not the next square —
        a building doorway drops it back on the street elsewhere — and that
        says nothing about the side being tested, so it must not be recorded
        as an opening.
        """
        if self.before_step:
            self.before_step()
        before = self.position()
        self.client.press_key(FORWARD, max_frames=700)
        self.settle()
        after = self.position()
        if after[:2] == before[:2]:
            return False
        dx, dy = after[0] - before[0], after[1] - before[1]
        if (dx, dy) in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            self.deltas[before[2]] = (dx, dy)
            return True
        self.log(f"  {before[:2]} -> {after[:2]} is not one step — dropping it")
        return None

    # -- exploring ---------------------------------------------------------

    def _frontier_step(self, x: int, y: int) -> int | None:
        """Which way to walk to reach the nearest square with something left.

        Picking the neighbour that looks least explored is not enough: two
        fully-known squares next to each other send the walk back and forth
        forever. A breadth-first search over the openings already walked finds
        the real frontier however far away it is, and returns only the first
        step towards it, so the next call re-plans with whatever was learned.
        """
        from collections import deque

        start = (x, y)
        seen = {start}
        queue = deque([(start, None)])
        while queue:
            cell, first = queue.popleft()
            tried = self.samples.get(cell, {})
            if cell != start and len(tried) < 4:
                return first
            for facing, blocked in tried.items():
                if blocked is not False:
                    continue
                delta = self.deltas.get(facing)
                if delta is None:
                    continue
                nxt = (cell[0] + delta[0], cell[1] + delta[1])
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, facing if first is None else first))
        return None

    def trapped(self) -> bool:
        blob = " ".join(self.lines())
        return all(m in blob for m in TRAPPED)

    def restore(self) -> bool:
        """Puts the party back where the walk started.

        Random encounters kill a party eventually, and a dead party ends the
        walk in the Guild with no way out. Rewinding the machine is safe here
        only because the walk never saves a character afterwards — see the
        save-state warning in the README before reusing this anywhere else.
        """
        if not self.checkpoint:
            return False
        self.log("  party wiped — restoring the checkpoint")
        self.client.state_load(self.checkpoint)
        self.client.run_frames(120, deterministic=False)
        self.reader.invalidate()   # a rewound machine may hash the same
        self.settle()
        return True

    def unstick(self) -> None:
        """Gets the party out of a place that swallows movement keys.

        A square whose four sides all read as blocked is nearly always not a
        square at all: the party is standing in a shop or the Guild, where I
        and L do nothing and the only way on is the exit key.
        """
        self.log("  blocked all four ways — taking it for a building, trying to get out")
        for key in (ord("E"), 0x1B, ord(" "), 13):
            self.client.press_key(key, max_frames=600)
            self.settle()

    def explore(self, budget: int) -> dict[tuple[int, int], dict[int, bool]]:
        stuck = 0
        for i in range(budget):
            x, y, facing = self.position()
            self.visits[(x, y)] = self.visits.get((x, y), 0) + 1
            cell = self.samples.setdefault((x, y), {})
            untried = [f for f in range(4) if f not in cell]
            if untried:
                target = min(untried, key=lambda f: (f - facing) % 4)
            else:
                target = self._frontier_step(x, y)
                if target is None:
                    self.log(f"  [{i}] nothing reachable left to try")
                    break
            self.face(target)
            moved = self.try_forward()
            cell[target] = None if moved is None else not moved
            if self.trapped():
                if not self.restore():
                    self.log("  stuck in the Guild — giving up the survey")
                    return self.samples
                stuck = 0
                continue
            stuck = 0 if moved else stuck + 1
            if stuck >= 6:
                self.unstick()
                # Whatever was recorded while stuck describes a building, not a
                # square of the street grid, so it is thrown away.
                self.samples.pop((x, y), None)
                stuck = 0
            if self.verbose and i % 10 == 0:
                known = sum(len(c) for c in self.samples.values())
                state = {True: "open", False: "blocked", None: "special"}[moved]
                self.log(f"  [{i:4d}/{budget}] ({x:2d},{y:2d}) f{target} "
                         f"{state}  squares {len(self.samples)} sides {known}")
        return self.samples

    def wall_samples(self) -> list[WallSample]:
        """Only the sides that were genuinely walked into or through."""
        out = []
        for (x, y), blocked in sorted(self.samples.items()):
            known = {f: v for f, v in blocked.items() if v is not None}
            if known:
                out.append(WallSample(x, y, known))
        return out
