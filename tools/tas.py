#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Recording exact input for a game, and playing it back frame for frame.

The emulator is far too fast to drive live, so the machine is parked and the
client owns the clock: every frame that passes is one this tool asked for. That
is what makes a movie possible. A movie is a save state, a list of inputs with
the frame each went in at, and a handful of checkpoints — the screen hash and
the cycle counter at known frames. Replaying it puts the machine back in the
same state and sends the same inputs at the same frames, and the checkpoints
say whether it really came out the same.

    ./tools/tas.py record --plan flight.json --out flight.movie.json
    ./tools/tas.py record --live --state here.state --out played.movie.json
    ./tools/tas.py replay flight.movie.json
    ./tools/tas.py bake played.movie.json
    ./tools/tas.py info flight.movie.json
    ./tools/tas.py demo --out chop.movie.json --state chop.state

A plan is hand-written: which input, at which frame, and how long to run. The
recorder executes it and bakes the resulting hashes into the movie, so from
then on `replay` can prove the run still reproduces.

`record --live` records a person instead. There the frontend owns the clock and
nobody is counting frames, so the input comes back stamped with the cycle it
happened on and the frames are worked out afterwards. A movie caught that way
has no checkpoints of its own — `bake` replays it with the client holding the
clock and writes down what that run produced, which is both what later replays
are judged against and the first proof that the capture was enough to
reproduce the play.

Two rules the format enforces, both from PROTOCOL.md section 6:

  * Frames advance only through `run_frames(deterministic=true)`. Turbo and the
    disk fast path change how many cycles a call consumes, so anything that
    turns them on — `run_until_change` among them — cannot appear in a movie.
  * Input is queued and reaches the running program one frame later. That delay
    applies equally while recording and replaying, so it needs no correction
    here; it only matters if you are counting frames by eye against a video.

A movie does not carry the game. Put the same disk in the same drive, or the
state will load into a machine that has never heard of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from linapple_client import LinAppleClient  # noqa: E402

DEFAULT_SOCKET = "/tmp/linapple-agent.sock"
DEFAULT_CHECK_EVERY = 60
MOVIE_VERSION = 1

# One NTSC frame, the same number the core and the frontends use. A live
# capture is stamped in cycles because there are no frame numbers to stamp it
# with, and this is what turns one into the other.
CYCLES_PER_FRAME = 17030
DEFAULT_POLL_INTERVAL = 0.25
POLL_BATCH = 512

# The 6502 clock the emulator runs at, and the frame rate that falls out of it.
# ffmpeg needs the real number: rounding 59.92 to 60 drifts a second away from
# the sound over ten minutes of movie.
CLOCK_6502 = (157500000.0 / 11.0) * 65.0 / 912.0
FRAMES_PER_SECOND = CLOCK_6502 / CYCLES_PER_FRAME

# Commands a movie may contain. Everything else either runs frames of its own
# or reaches past the machine, and both would make a replay drift.
INPUT_COMMANDS = ("joy_axis", "joy_button", "key_state", "key_press")


def state_digest(path: str) -> str:
    """A short fingerprint of the save state, so a replay can tell it is being
    run against the state it was recorded from rather than a similar file."""
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class Movie:
    state: str
    frames: int = 0
    events: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": MOVIE_VERSION,
                "state": self.state,
                "frames": self.frames,
                "meta": self.meta,
                "events": self.events,
                "checks": self.checks,
            },
            indent=2,
        )

    @classmethod
    def load(cls, path: str) -> "Movie":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = raw.get("version")
        if version != MOVIE_VERSION:
            raise ValueError(f"movie version {version}, expected {MOVIE_VERSION}")
        for event in raw.get("events", []):
            if event.get("cmd") not in INPUT_COMMANDS:
                raise ValueError(f"movie contains a non-input command: {event}")
        return cls(
            state=raw["state"],
            frames=int(raw.get("frames", 0)),
            events=list(raw.get("events", [])),
            checks=list(raw.get("checks", [])),
            meta=dict(raw.get("meta", {})),
        )

    def save(self, path: str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")


class Recorder:
    """Drives the machine and writes down what it was told, and when.

    Any tool can record: make one of these, then send input and advance frames
    through it instead of through the client. The frame counter here is the
    movie's clock, which is why every frame has to come through `run`.
    """

    def __init__(self, client: LinAppleClient, state: str,
                 check_every: int = DEFAULT_CHECK_EVERY, note: str = ""):
        self.client = client
        self.state = str(Path(state).resolve())
        self.check_every = max(int(check_every), 0)
        self.frame = 0
        self.movie = Movie(state=self.state)
        self.movie.meta = {
            "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
            "protocol": client.version().get("protocol"),
            "emulator": client.version().get("emulator"),
            "note": note,
        }
        # The state load is part of the movie's prologue, not something the
        # caller does first, so that a replay starts from exactly here.
        #
        # Parking first is what makes the prologue a fixed point. A session a
        # person was playing is still running frames of its own, and any that
        # land between the load and the first checkpoint move every checkpoint
        # after it — the machine is in the right place but the clock is not.
        client.pause()
        client.state_load(self.state)
        self.movie.meta["state_sha256"] = state_digest(self.state)
        self.movie.meta["start_cycles"] = client.status()["cycles"]
        self._next_check = 0
        self._checkpoint()

    # -- input -------------------------------------------------------------

    def _record(self, cmd: str, params: dict) -> None:
        self.movie.events.append({"frame": self.frame, "cmd": cmd,
                                  "params": params})

    def joy_axis(self, axis: int, value: int) -> None:
        self.client.joy_axis(axis, value)
        self._record("joy_axis", {"axis": axis, "value": value})

    def joy_button(self, button: int, down: bool) -> None:
        self.client.joy_button(button, down)
        self._record("joy_button", {"button": button, "down": bool(down)})

    def key_state(self, key: int | str, down: bool) -> None:
        code = key if isinstance(key, int) else ord(key)
        self.client.key_state(code, down)
        self._record("key_state", {"key": code, "down": bool(down)})

    def key_press(self, key: int | str) -> None:
        """Press and release inside one frame. `key_type` has no place in a
        movie: it runs frames of its own between characters, which would put
        the movie's clock out of step with the machine's."""
        code = key if isinstance(key, int) else ord(key)
        self.client.key_press(code, frames_after=0)
        self._record("key_press", {"key": code})

    # -- time --------------------------------------------------------------

    def run(self, frames: int) -> None:
        """Advances the movie's clock, taking checkpoints as it passes them."""
        remaining = int(frames)
        while remaining > 0:
            if self.check_every:
                until_check = self._next_check - self.frame
                step = min(remaining, until_check) if until_check > 0 else remaining
            else:
                step = remaining
            self.client.run_frames(step, deterministic=True)
            self.frame += step
            remaining -= step
            if self.check_every and self.frame >= self._next_check:
                self._checkpoint()

    def _checkpoint(self) -> None:
        self.movie.checks.append({
            "frame": self.frame,
            "hash": self.client.screen_hash(),
            "cycles": self.client.status()["cycles"],
        })
        if self.check_every:
            self._next_check = self.frame + self.check_every

    # -- result ------------------------------------------------------------

    def finish(self) -> Movie:
        if not self.movie.checks or self.movie.checks[-1]["frame"] != self.frame:
            self._checkpoint()
        self.movie.frames = self.frame
        return self.movie


def _send(client: LinAppleClient, cmd: str, params: dict) -> None:
    """Sends one recorded input and nothing else.

    key_press on the client runs a couple of frames afterwards as a
    convenience, which would put a replay out of step with the movie's clock
    by two frames per keystroke.
    """
    if cmd == "key_press":
        client.key_press(params["key"], frames_after=0)
        return
    getattr(client, cmd)(**params)


def save_frame(path: Path, width: int, height: int, bgra: bytes) -> None:
    """Writes one captured frame.

    Pillow turns the emulator's BGRA buffer into a PNG in C; the fallback in
    screenshot.py does it a pixel at a time in Python, which is fine for one
    screenshot and far too slow for a thousand of them.
    """
    try:
        from PIL import Image
    except ImportError:
        import screenshot
        screenshot.save(path, width, height, bgra)
        return
    Image.frombytes("RGB", (width, height), bgra, "raw", "BGRX").save(path)


class FrameWriter:
    """Drops a PNG every `every` frames while a movie plays.

    A frame is 860 KB of pixels off the socket before it is compressed, so this
    is for a passage worth looking at rather than for a whole session.
    """

    def __init__(self, client: LinAppleClient, directory: str, every: int = 1):
        self.client = client
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.every = max(int(every), 1)
        self.count = 0

    def stops(self, total: int) -> set[int]:
        """The frames the replay has to stop on to catch them all."""
        return set(range(0, int(total) + 1, self.every))

    def capture(self, frame: int) -> None:
        if frame % self.every:
            return
        width, height, pixels = self.client.screen_capture()
        save_frame(self.directory / f"frame_{self.count:06d}.png",
                   width, height, pixels)
        self.count += 1

    @property
    def fps(self) -> float:
        return FRAMES_PER_SECOND / self.every


def encode_video(frames: FrameWriter, audio: str | None, out: str,
                 verbose: bool = True) -> None:
    """Puts the PNGs and the WAV together with ffmpeg.

    Both come off the same emulated clock — the frames are counted and the
    samples are produced by the speaker as those frames run — so they line up
    without anything having to be stretched, as long as ffmpeg is told the
    frame rate the machine actually has rather than a round 60.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("no ffmpeg, so nothing can be muxed "
                           "(the PNGs and the WAV are still there)")
    command = ["ffmpeg", "-y", "-loglevel", "error",
               "-framerate", f"{frames.fps:.6f}",
               "-i", str(frames.directory / "frame_%06d.png")]
    if audio:
        command += ["-i", str(audio), "-c:a", "aac", "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    if verbose:
        print(f"  ffmpeg: {frames.count} frames @ {frames.fps:.2f}fps"
              f"{' + audio' if audio else ''} -> {out}")
    subprocess.run(command, check=True)


@dataclass
class Divergence:
    frame: int
    field: str
    expected: int
    actual: int

    def __str__(self) -> str:
        return (f"frame {self.frame}: {self.field} expected {self.expected}, "
                f"got {self.actual}")


def replay(client: LinAppleClient, movie: Movie, state: str | None = None,
           verbose: bool = True, frames: "FrameWriter | None" = None,
           audio: str | None = None) -> list[Divergence]:
    """Plays the movie back and returns every checkpoint that did not match.

    An empty list means the run reproduced exactly: same screen at every
    checkpoint and the same cycle count, which is the stricter of the two —
    the cycle counter notices a divergence the screen has not shown yet.

    `frames` writes a PNG every so many frames as it goes, and `audio` records
    the sound to a WAV for the length of the replay. Both come off the same
    emulated clock, so they can be put together into a video afterwards.
    """
    path = state or movie.state
    recorded = movie.meta.get("state_sha256")
    if recorded and state_digest(path) != recorded:
        raise ValueError(
            f"{path} is not the state this movie was recorded from "
            f"(fingerprint {state_digest(path)}, movie wants {recorded})")

    # Same reason as in the recorder: whoever was running the machine has to
    # let go of the clock before the first checkpoint is read.
    client.pause()
    client.state_load(str(Path(path).resolve()))

    events: dict[int, list[dict]] = {}
    for event in movie.events:
        events.setdefault(int(event["frame"]), []).append(event)
    checks = {int(check["frame"]): check for check in movie.checks}

    stops = set(events) | set(checks) | {movie.frames}
    if frames is not None:
        # Every frame that has to be photographed is a frame the replay has to
        # stop on, on top of the ones it was already stopping on.
        stops |= frames.stops(movie.frames)
    divergences: list[Divergence] = []
    frame = 0

    if audio:
        client.audio_dump(True, audio)
    try:
        for stop in sorted(stops):
            if stop > frame:
                client.run_frames(stop - frame, deterministic=True)
                frame = stop
            # A checkpoint describes the machine before this frame's input, in
            # the same order the recorder saw it.
            check = checks.get(frame)
            if check is not None:
                for name, actual in (("hash", client.screen_hash()),
                                     ("cycles", client.status()["cycles"])):
                    if actual != check[name]:
                        divergences.append(Divergence(frame, name,
                                                      check[name], actual))
                        if verbose:
                            print(f"  ! {divergences[-1]}")
            if frames is not None:
                frames.capture(frame)
            for event in events.get(frame, []):
                _send(client, event["cmd"], event["params"])
                if verbose:
                    print(f"  frame {frame:>6}  {event['cmd']} "
                          f"{event['params']}")

        if movie.frames > frame:
            client.run_frames(movie.frames - frame, deterministic=True)
    finally:
        if audio:
            # Closing the file is what writes the RIFF sizes into it, so it has
            # to happen even when the replay fell over halfway.
            client.audio_dump(False)

    return divergences


def record_plan(client: LinAppleClient, plan: dict, check_every: int,
                note: str = "", verbose: bool = True) -> Movie:
    """Runs a hand-written plan and bakes the result into a movie."""
    recorder = Recorder(client, plan["state"], check_every=check_every,
                        note=note or plan.get("note", ""))
    ordered = sorted(plan.get("events", []), key=lambda e: int(e["frame"]))
    for event in ordered:
        target = int(event["frame"])
        if target < recorder.frame:
            raise ValueError(f"plan goes backwards at frame {target}")
        recorder.run(target - recorder.frame)
        cmd = event["cmd"]
        if cmd not in INPUT_COMMANDS:
            raise ValueError(f"{cmd} cannot appear in a movie")
        getattr(recorder, cmd)(**event["params"])
        if verbose:
            print(f"  frame {recorder.frame:>6}  {cmd} {event['params']}")
    total = int(plan.get("frames", recorder.frame))
    if total > recorder.frame:
        recorder.run(total - recorder.frame)
    return recorder.finish()


# --- recording a person -----------------------------------------------------

def _movie_event(item: dict, start_cycles: int) -> dict | None:
    """Turns one captured event into something a movie can send back.

    The capture is stamped with the cumulative cycle count: a live session has
    no frame numbers because nobody is asking for frames. Which frame it fell
    in is a division away, and where inside that frame is not recoverable —
    an error of at most one frame, the same size as the delay the input queue
    already costs.
    """
    frame = (int(item["cycles"]) - start_cycles) // CYCLES_PER_FRAME
    kind = item.get("kind")
    if kind == "joy_axis":
        cmd, params = "joy_axis", {"axis": int(item["code"]),
                                   "value": int(item["value"])}
    elif kind == "joy_button":
        cmd, params = "joy_button", {"button": int(item["code"]),
                                     "down": bool(item["value"])}
    elif kind == "key":
        # key_state, not key_press: the capture has the press and the release
        # as two events, and how long a key was held is part of the play.
        cmd, params = "key_state", {"key": int(item["code"]),
                                    "down": bool(item["value"])}
    else:
        return None
    return {"frame": max(int(frame), 0), "cmd": cmd, "params": params}


def _thin_axes(events: list[dict]) -> list[dict]:
    """Drops stick positions that say nothing new.

    A joystick reports while it is held over, and the paddle value is an
    absolute position: repeating one changes nothing, and two in the same frame
    cannot be told apart on replay because the command queue empties at the
    frame boundary anyway. Keys and buttons are left alone — a repeat there is
    a person leaning on the key, and the machine can tell.
    """
    kept: list[dict] = []
    last: dict[int, int] = {}
    for event in events:
        if event["cmd"] != "joy_axis":
            kept.append(event)
            continue
        axis = event["params"]["axis"]
        value = event["params"]["value"]
        if last.get(axis) == value:
            continue
        for prior in reversed(kept):
            if prior["frame"] != event["frame"]:
                break
            if prior["cmd"] == "joy_axis" and prior["params"]["axis"] == axis:
                kept.remove(prior)
                break
        last[axis] = value
        kept.append(event)
    return kept


def _drain(client: LinAppleClient, into: list[dict]) -> int:
    """Takes everything waiting, in as many polls as that needs.

    Returns the emulator's dropped count, which is cumulative since capture
    started — so the last one seen is the whole story.
    """
    dropped = 0
    for _ in range(64):
        result = client.input_poll(POLL_BATCH)
        into.extend(result.get("events", []))
        dropped = int(result.get("dropped", 0))
        if not result.get("pending"):
            break
    return dropped


def record_live(client: LinAppleClient, state_path: str, seconds: float | None = None,
                poll_interval: float = DEFAULT_POLL_INTERVAL, limit: int = 0,
                note: str = "", verbose: bool = True) -> Movie:
    """Records a person playing at the window.

    The machine is not parked here. The frontend owns the clock, the person is
    watching it run, and this only saves the state the play starts from, turns
    on capture and collects what the frontend logs. The movie comes back
    without checkpoints — `bake` is what gives it those.
    """
    path = str(Path(state_path).resolve())
    # Freezing first makes the state and the starting cycle count agree; a
    # frame that ran between them would be a frame the movie never sees.
    client.pause()
    client.state_save(path)
    start_cycles = client.status()["cycles"]
    client.input_capture(True, limit=limit)
    client.resume()

    if verbose:
        stop = f"stops after {seconds:g}s" if seconds else "Ctrl-C to stop"
        print(f"recording - play in the window ({stop})")
        print(f"  start state: {path}")

    raw: list[dict] = []
    dropped = 0
    started = time.monotonic()
    try:
        while True:
            time.sleep(poll_interval)
            dropped = _drain(client, raw)
            if verbose:
                print(f"\r  {len(raw)} inputs"
                      f"{f' ({dropped} dropped)' if dropped else ''}",
                      end="", flush=True)
            if seconds is not None and time.monotonic() - started >= seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if verbose:
            print()
        # Freezing before the last poll keeps the play from running on past the
        # final event while the tail of the buffer is still coming across.
        client.pause()
        elapsed = time.monotonic() - started
        dropped = _drain(client, raw)
        end_cycles = client.status()["cycles"]
        end_hash = client.screen_hash()
        client.input_capture(False)

    movie = Movie(state=path)
    movie.events = _thin_axes(
        [event for event in (_movie_event(item, start_cycles) for item in raw)
         if event is not None])
    movie.frames = max((end_cycles - start_cycles) // CYCLES_PER_FRAME, 0)
    movie.meta = {
        "recorded": time.strftime("%Y-%m-%d %H:%M:%S"),
        "protocol": client.version().get("protocol"),
        "emulator": client.version().get("emulator"),
        "note": note,
        "state_sha256": state_digest(path),
        "start_cycles": start_cycles,
        "live": True,
        "live_seconds": round(elapsed, 1),
        "live_captured": len(raw),
        "live_end_hash": end_hash,
        "dropped": dropped,
    }
    if dropped and verbose:
        print(f"! {dropped} inputs were pushed out of the buffer - the movie has "
              f"a gap. Lower --poll or raise --limit.")
    return movie


def bake(client: LinAppleClient, movie: Movie, check_every: int = DEFAULT_CHECK_EVERY,
         verbose: bool = True) -> Movie:
    """Replays a movie with the client holding the clock and fills in the
    checkpoints.

    A live recording cannot judge itself: nothing counted frames while the
    person played, so there is nothing for a later run to be compared against.
    One deterministic pass produces the hashes and cycle counts that `replay`
    checks — and since it walks the same path the person did, it is also the
    first evidence that what was captured is enough to reproduce the play.

    It rewinds the session to the start state to do it, and leaves the machine
    where the replay ended, which is where the person left off.
    """
    plan = {"state": movie.state, "events": movie.events,
            "frames": movie.frames, "note": movie.meta.get("note", "")}
    baked = record_plan(client, plan, check_every=check_every, verbose=False)
    # The live pass knows things a replay cannot: how long it ran, what was
    # dropped, and what the screen looked like when the person stopped.
    baked.meta.update({key: value for key, value in movie.meta.items()
                       if key.startswith("live") or key == "dropped"})
    baked.meta["baked"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Did the replay end up where the person left off? The live session stopped
    # in the middle of a frame, so a screen that moves on its own — a flashing
    # cursor is enough — can be a frame or two further on than the movie's last
    # frame. Stepping past the end costs nothing: the checkpoints are written.
    live_hash = movie.meta.get("live_end_hash")
    final = baked.checks[-1]["hash"] if baked.checks else None
    offset = None
    if live_hash is not None and final is not None:
        if live_hash == final:
            offset = 0
        else:
            for step in range(1, 4):
                client.run_frames(1, deterministic=True)
                if client.screen_hash() == live_hash:
                    offset = step
                    break
    baked.meta["live_end_offset"] = offset

    if verbose:
        if offset == 0:
            print("the replay ends on the same screen the live session did - "
                  "the captured inputs reproduce that play on their own.")
        elif offset:
            print(f"the replay matches the live ending {offset} frames off - a live "
                  f"session stops mid-frame, so that much is expected.")
        elif live_hash is not None:
            print("! the replay does not end where the live session did (no match "
                  "within three frames). Some input may not have been "
                  "captured - `info` shows the dropped count and `replay` "
                  "checks reproducibility itself.")
    return baked


# --- demo -------------------------------------------------------------------

def demo(client: LinAppleClient, state_path: str, check_every: int,
         verbose: bool = True) -> Movie:
    """Records a short Choplifter flight, disk already in drive 1.

    Choplifter picks its controller at the title and reads only that one:
    started with the space bar it reads the joystick and ignores the keyboard.
    Start it with the button instead and every joy_axis in this movie would be
    thrown away — which looks exactly like a broken recorder.
    """
    if verbose:
        print("booting, then starting CHOPLIFTER from the menu")
    for _ in range(8):
        if not client.run_until_change(max_frames=400, chunk=48)["changed"]:
            break
    client.key_press(ord("C"))
    for _ in range(10):
        client.run_until_change(max_frames=900, chunk=48)
        if not client.status()["text_mode"]:
            break
    client.run_frames(120, deterministic=True)
    client.key_press(ord(" "))          # space: the joystick-reading start
    client.run_frames(300, deterministic=True)
    client.state_save(str(Path(state_path).resolve()))
    if verbose:
        print(f"saved the starting state to {state_path}")

    plan = {
        "state": state_path,
        "note": "Choplifter: lift off, bank right, then left",
        "events": [
            {"frame": 0, "cmd": "joy_axis", "params": {"axis": 1, "value": 0}},
            {"frame": 300, "cmd": "joy_axis",
             "params": {"axis": 0, "value": 255}},
            {"frame": 600, "cmd": "joy_axis",
             "params": {"axis": 0, "value": 0}},
            {"frame": 900, "cmd": "joy_axis",
             "params": {"axis": 0, "value": 127}},
        ],
        "frames": 1000,
    }
    return record_plan(client, plan, check_every=check_every, verbose=verbose)


# --- cli --------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record",
                            help="run a plan, or record a person playing")
    source = record.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", help="a hand-written plan to execute")
    source.add_argument("--live", action="store_true",
                        help="record what a person plays at the window")
    record.add_argument("--out", required=True)
    record.add_argument("--state",
                        help="with --live: where to save the state the "
                             "recording starts from")
    record.add_argument("--seconds", type=float,
                        help="with --live: stop after this long (default: "
                             "until Ctrl-C)")
    record.add_argument("--poll", type=float, default=DEFAULT_POLL_INTERVAL,
                        help="with --live: seconds between polls")
    record.add_argument("--limit", type=int, default=0,
                        help="with --live: how many events the emulator "
                             "buffers (default 4096)")
    record.add_argument("--no-bake", action="store_true",
                        help="with --live: leave the movie without "
                             "checkpoints, for `bake` later")
    record.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY)
    record.add_argument("--note", default="")

    play = sub.add_parser("replay", help="play a movie back and verify it")
    play.add_argument("movie")
    play.add_argument("--state", help="load this state instead of the one named")
    play.add_argument("--frames", metavar="DIR",
                      help="write a PNG of the screen into DIR as it plays")
    play.add_argument("--every", type=int, default=1, metavar="N",
                      help="with --frames: one PNG every N frames (default 1)")
    play.add_argument("--audio", metavar="WAV",
                      help="record the sound of the replay to this WAV file")
    play.add_argument("--video", metavar="FILE",
                      help="put the frames and the sound together with ffmpeg "
                           "(implies --frames and --audio)")

    fill = sub.add_parser("bake",
                          help="replay a movie deterministically and write "
                               "its checkpoints")
    fill.add_argument("movie")
    fill.add_argument("--out", help="write here instead of over the movie")
    fill.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY)

    show = sub.add_parser("info", help="describe a movie")
    show.add_argument("movie")

    example = sub.add_parser("demo", help="record a Choplifter flight")
    example.add_argument("--out", required=True)
    example.add_argument("--state", required=True)
    example.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verbose = not args.quiet

    if args.command == "info":
        movie = Movie.load(args.movie)
        print(f"state    {movie.state}")
        print(f"frames   {movie.frames}")
        print(f"events   {len(movie.events)}")
        print(f"checks   {len(movie.checks)}")
        for key, value in movie.meta.items():
            print(f"  {key}: {value}")
        if not movie.checks:
            print("! no checkpoints - until `bake` runs, a replay is not "
                  "compared against anything.")
        if movie.meta.get("dropped"):
            print(f"! {movie.meta['dropped']} inputs were lost while recording "
                  f"- the movie has a gap.")
        for event in movie.events:
            print(f"  frame {event['frame']:>6}  {event['cmd']} {event['params']}")
        return 0

    with LinAppleClient(args.socket) as client:
        if args.command == "record":
            if args.live:
                if not args.state:
                    print("--live needs --state - where a replay starts is what "
                          "the whole movie is measured against.")
                    return 2
                movie = record_live(client, args.state, seconds=args.seconds,
                                    poll_interval=args.poll, limit=args.limit,
                                    note=args.note, verbose=verbose)
                if not args.no_bake:
                    movie = bake(client, movie, check_every=args.check_every,
                                 verbose=verbose)
                    # The person was playing when this started; hand the
                    # machine back the way it was found.
                    client.resume()
            else:
                plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                movie = record_plan(client, plan, check_every=args.check_every,
                                    note=args.note, verbose=verbose)
            movie.save(args.out)
            print(f"-> {args.out} ({movie.frames} frames, "
                  f"{len(movie.events)} events, {len(movie.checks)} checks)")
            return 0

        if args.command == "bake":
            movie = Movie.load(args.movie)
            baked = bake(client, movie, check_every=args.check_every,
                         verbose=verbose)
            out = args.out or args.movie
            baked.save(out)
            print(f"-> {out} ({baked.frames} frames, "
                  f"{len(baked.events)} events, {len(baked.checks)} checks)")
            return 0

        if args.command == "demo":
            movie = demo(client, args.state, check_every=args.check_every,
                         verbose=verbose)
            movie.save(args.out)
            print(f"-> {args.out} ({movie.frames} frames, "
                  f"{len(movie.events)} events, {len(movie.checks)} checks)")
            return 0

        if args.command == "replay":
            movie = Movie.load(args.movie)
            if verbose:
                print(f"replaying {args.movie}: {movie.frames} frames, "
                      f"{len(movie.events)} events")
            # A video is frames and sound put together, so asking for one asks
            # for both; where they land is the caller's business if they say so
            # and beside the video if they do not.
            frames_dir, audio_path = args.frames, args.audio
            if args.video:
                beside = Path(args.video).with_suffix("")
                frames_dir = frames_dir or str(beside) + ".frames"
                audio_path = audio_path or str(beside) + ".wav"
            writer = (FrameWriter(client, frames_dir, args.every)
                      if frames_dir else None)
            try:
                divergences = replay(client, movie, state=args.state,
                                     verbose=verbose, frames=writer,
                                     audio=audio_path)
            except ValueError as exc:
                # A mismatched state is a setup mistake, not a failed replay,
                # and deserves to be told apart from one.
                print(f"cannot replay: {exc}")
                return 2
            if writer is not None and verbose:
                print(f"{writer.count} frames -> {writer.directory}")
            if audio_path and verbose:
                print(f"audio -> {audio_path}")
            if args.video:
                encode_video(writer, audio_path, args.video, verbose=verbose)
                print(f"-> {args.video}")
            if not movie.checks:
                print("! no checkpoints, so the replay was not compared against "
                      "anything - bake them in with `bake`.")
                return 0
            if divergences:
                print(f"{len(divergences)} replay divergences "
                      f"(first at frame {divergences[0].frame})")
                return 1
            print(f"replay matches - all {len(movie.checks)} checkpoints identical")
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
