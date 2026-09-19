# SPDX-License-Identifier: GPL-2.0-only
"""Screen-driven automation engine for Apple II games.

An assistant is a list of rules. Each rule names a screen the game can show and
the actions to take when it appears. The engine advances the emulator, reads the
text screen, fires the first matching rule, and repeats. Disk-swap prompts,
combat menus and healing loops are all the same shape, so they share one engine
and differ only in their rule tables.

Rules are matched in declaration order, so put the specific ones first.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]
                       / "clients" / "python"))

from linapple_client import LinAppleClient  # noqa: E402


@dataclass
class Action:
    """One step to perform when a rule fires."""

    kind: str
    value: object = None

    @staticmethod
    def type(text: str) -> "Action":
        return Action("type", text)

    @staticmethod
    def key(code: int) -> "Action":
        return Action("key", code)

    @staticmethod
    def wait(frames: int) -> "Action":
        return Action("wait", frames)

    @staticmethod
    def insert(drive: int, path: str) -> "Action":
        return Action("insert", (drive, path))

    @staticmethod
    def call(fn: Callable[[LinAppleClient, Screen], None]) -> "Action":
        return Action("call", fn)

    @staticmethod
    def stop(reason: str = "rule requested stop") -> "Action":
        return Action("stop", reason)


class ScreenSource:
    """Where the engine reads the screen from.

    Games split into two camps: those that use the hardware text page, which
    `screen_text` reads directly, and those that draw their own font into the
    hi-res page, which need the font-based decoder. A rule table should not
    care which, so both arrive here as plain lines of text.
    """

    def __init__(self, client: LinAppleClient, reader=None):
        self.client = client
        self.reader = reader
        self._digest: int | None = None
        self._lines: list[str] | None = None

    def lines(self) -> list[str]:
        # Reading a screen means an 8 KB transfer and, for the hi-res camp, a
        # glyph lookup per cell in Python — and a polling engine asks for it
        # over and over while nothing on screen moves. The hash is a few bytes
        # and one round trip, so it is worth asking first and reusing the last
        # decode whenever it comes back the same.
        digest = self.client.screen_hash()
        if digest == self._digest and self._lines is not None:
            return self._lines
        self._digest, self._lines = digest, self._read()
        return self._lines

    def _read(self) -> list[str]:
        if self.reader is not None:
            return [line for lines in self.reader.read().values() for line in lines]
        return list(self.client.screen().lines)

    def text(self) -> str:
        return "\n".join(self.lines())


@dataclass
class Rule:
    """A screen pattern and what to do about it."""

    name: str
    when: str
    do: Sequence[Action]
    # Firing the same rule on consecutive polls usually means the action did not
    # change the screen. A limit turns that silent stall into a clear error.
    max_consecutive: int = 8
    settle_frames: int = 6

    _pattern: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._pattern = re.compile(self.when)

    def matches(self, lines: Sequence[str]) -> bool:
        if any(self._pattern.search(line) for line in lines):
            return True
        # Game text wraps mid-sentence, so a phrase the rule names may be split
        # across two lines; matching the joined text as well catches those.
        return self._pattern.search(" ".join(line.strip() for line in lines)) \
            is not None


class StalledError(RuntimeError):
    """Raised when a rule keeps firing without changing the screen."""


class Engine:
    """Polls the screen and fires rules.

    Two pacing modes, and picking the wrong one is the usual first mistake:

    * **stepped** (``live=False``) — the engine owns the clock. It advances the
      machine a fixed number of frames per poll and the machine sits paused in
      between, which makes runs reproducible. Use it headless.
    * **live** (``live=True``) — the emulator runs by itself and the engine only
      watches. Nothing is ever paused and keystrokes are paced by wall clock, so
      a person watching the window sees an uninterrupted game. Use it whenever
      the assistant is helping someone play.
    """

    def __init__(
        self,
        client: LinAppleClient,
        rules: Sequence[Rule],
        poll_frames: int = 12,
        verbose: bool = True,
        live: bool = False,
        poll_interval: float = 0.2,
        reader=None,
        idle_after: int = 0,
        idle_do: Sequence[Action] = (),
        rest: float = 0.02,
    ):
        self.client = client
        self.source = ScreenSource(client, reader)
        self.rules = list(rules)
        self.poll_frames = poll_frames
        self.verbose = verbose
        self.live = live
        self.poll_interval = poll_interval
        self.rest = rest
        self.fired: list[str] = []
        # Title cards and crack intros show no text the rule table can name, so
        # a run would sit on them forever. After enough polls with nothing
        # matched the engine nudges the game instead of waiting.
        self.idle_after = idle_after
        self.idle_do = list(idle_do)
        self._idle_polls = 0
        self._last_rule: str | None = None
        self._repeat_count = 0
        if live:
            self.client.resume()

    def _advance(self, frames: int) -> None:
        """Lets `frames` worth of emulation happen, without ever pausing in
        live mode. One Apple II frame is about 1/60 s."""
        if not frames:
            return
        if self.live:
            time.sleep(frames / 60.0)
        else:
            self.client.run_frames(frames, deterministic=False)

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[engine] {message}", flush=True)

    def _perform(self, action: Action, lines: Sequence[str]) -> bool:
        """Runs one action. Returns False to stop the run."""
        if action.kind == "type":
            if self.live:
                self.client.type_text_live(str(action.value))
            else:
                self.client.type_text(str(action.value))
        elif action.kind == "key":
            self.client.key_press(int(action.value),
                                  frames_after=0 if self.live else 2)
        elif action.kind == "wait":
            self._advance(int(action.value))
        elif action.kind == "insert":
            drive, path = action.value
            self.log(f"drive {drive} <- {path}")
            self.client.disk_insert(drive, path)
        elif action.kind == "call":
            action.value(self.client, lines)
        elif action.kind == "stop":
            self.log(f"stop: {action.value}")
            return False
        else:
            raise ValueError(f"unknown action kind: {action.kind}")
        return True

    def _fire(self, rule: Rule, lines: Sequence[str]) -> bool:
        if rule.name == self._last_rule:
            self._repeat_count += 1
        else:
            self._last_rule, self._repeat_count = rule.name, 1
        if self._repeat_count > rule.max_consecutive:
            raise StalledError(
                f"rule '{rule.name}' fired {self._repeat_count} times without "
                f"changing the screen:\n" + "\n".join(lines)
            )

        self.log(f"rule '{rule.name}'")
        self.fired.append(rule.name)
        for action in rule.do:
            if not self._perform(action, lines):
                return False
        if rule.settle_frames:
            self._advance(rule.settle_frames)
        return True

    def step(self) -> bool:
        """Polls once. Returns False when a rule asked the run to stop."""
        lines = self.source.lines()
        for rule in self.rules:
            if rule.matches(lines):
                self._idle_polls = 0
                return self._fire(rule, lines)

        self._last_rule, self._repeat_count = None, 0
        self._idle_polls += 1
        if self.idle_do and self.idle_after and self._idle_polls >= self.idle_after:
            self._idle_polls = 0
            self.log("idle nudge")
            for action in self.idle_do:
                if not self._perform(action, lines):
                    return False
            return True
        if self.live:
            time.sleep(self.poll_interval)
        else:
            self.client.run_frames(self.poll_frames, deterministic=False)
            # Stepped runs poll in a tight loop; a brief rest keeps a long
            # unattended run from holding the host at full clock throughout.
            if self.rest > 0:
                time.sleep(self.rest)
        return True

    def run(self, max_seconds: float | None = None, max_polls: int | None = None) -> None:
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        polls = 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                self.log("time limit reached")
                return
            if max_polls is not None and polls >= max_polls:
                self.log("poll limit reached")
                return
            polls += 1
            if not self.step():
                return
