# SPDX-License-Identifier: GPL-2.0-only
"""JSON-line client for the LinApple agent socket."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import time
import warnings
from dataclasses import dataclass
from typing import Iterable, Sequence


class LinAppleError(RuntimeError):
    """Raised when the emulator rejects a command."""


class DiskNotRewoundWarning(UserWarning):
    """A save state was used while a writable disk was in the machine.

    The state rewinds the machine and not the image on the host, so the two
    can end up disagreeing about what is on the disk.
    """


@dataclass(frozen=True)
class Screen:
    """A decoded snapshot of the Apple II text screen."""

    lines: tuple[str, ...]
    columns: int
    text_mode: bool
    mixed: bool

    def text(self) -> str:
        return "\n".join(self.lines)

    def find(self, pattern: str) -> tuple[int, int] | None:
        """Returns (row, column) of the first regex match, or None."""
        compiled = re.compile(pattern)
        for row, line in enumerate(self.lines):
            match = compiled.search(line)
            if match:
                return row, match.start()
        return None

    def contains(self, pattern: str) -> bool:
        return self.find(pattern) is not None


class LinAppleClient:
    """Synchronous request/response client.

    Every call blocks until the emulator answers, which keeps an assistant's
    logic linear: advance some frames, look at the screen, decide, act.
    """

    def __init__(self, socket_path: str, timeout: float = 30.0,
                 pace_chunk: int = 600, pace_duty: float = 0.7,
                 wait_duty: int = 55):
        """
        A long run_frames emulates as fast as the host allows, which pins a
        core and holds the CPU at its top frequency for the whole call. To keep
        that in hand the request is split into `pace_chunk`-frame pieces and
        the client rests between them.

        `pace_duty` is the share of wall time the emulator is allowed to work:
        0.7 means it runs 70% of the time and rests 30%, so a burst costs about
        40% more wall clock and leaves the core idle enough to clock down. The
        rest is measured against how long each chunk actually took, so the
        ratio holds on a fast host and a slow one alike. Set pace_duty to 1.0
        (or pace_chunk to 0) when raw speed genuinely matters.
        """
        self.socket_path = socket_path
        self.pace_chunk = pace_chunk
        self.pace_duty = min(max(pace_duty, 0.05), 1.0)
        # How hard the emulator may work while waiting for a redraw, as a
        # percentage. Lower trades a slower wait for a cooler CPU.
        self.wait_duty = wait_duty
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(socket_path)
        self._stream = self._sock.makefile("rwb")
        self._next_id = 1

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._sock.close()

    def __enter__(self) -> "LinAppleClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def call(self, cmd: str, **params) -> dict:
        request = {"id": self._next_id, "cmd": cmd}
        self._next_id += 1
        if params:
            request["params"] = params
        self._stream.write((json.dumps(request) + "\n").encode())
        self._stream.flush()

        raw = self._stream.readline()
        if not raw:
            raise LinAppleError(f"emulator closed the connection during {cmd}")
        response = json.loads(raw)
        if not response.get("ok"):
            raise LinAppleError(f"{cmd}: {response.get('error', 'unknown error')}")
        return response.get("result", {})

    # -- meta --------------------------------------------------------------

    def ping(self) -> None:
        self.call("ping")

    def version(self) -> dict:
        return self.call("version")

    def commands(self) -> list[str]:
        return self.call("commands")["commands"]

    def status(self) -> dict:
        return self.call("status")

    # -- run control -------------------------------------------------------

    def run_frames(self, frames: int = 1, deterministic: bool = True,
                   pace: bool | None = None) -> dict:
        """Runs `frames` frames, yielding the CPU periodically while it does.

        The emulator has no reason to pace itself here — the client asked for
        these frames as fast as possible — so the breathing room is added on
        this side by splitting the request.
        """
        chunked = self.pace_chunk > 0 and frames > self.pace_chunk
        if pace is False or not chunked:
            return self.call("run_frames", frames=frames,
                             deterministic=deterministic)

        rest_ratio = (1.0 - self.pace_duty) / self.pace_duty
        remaining, total_cycles, mode = frames, 0, None
        while remaining > 0:
            batch = min(self.pace_chunk, remaining)
            started = time.monotonic()
            result = self.call("run_frames", frames=batch,
                               deterministic=deterministic)
            elapsed = time.monotonic() - started
            total_cycles += result.get("cycles", 0)
            mode = result.get("mode", mode)
            remaining -= batch
            if remaining > 0 and rest_ratio > 0:
                time.sleep(elapsed * rest_ratio)
        return {"frames": frames, "cycles": total_cycles, "mode": mode}

    def pause(self) -> None:
        self.call("pause")

    def resume(self) -> None:
        self.call("resume")

    def reset(self, full: bool = False) -> None:
        self.call("reset", full=full)

    def set_speed(self, speed: int) -> None:
        self.call("speed_set", speed=speed)

    def set_turbo(self, turbo: bool) -> None:
        self.call("turbo_set", turbo=turbo)

    def quit(self) -> None:
        try:
            self.call("quit")
        except LinAppleError:
            pass

    # -- input -------------------------------------------------------------

    def key_press(self, key: int, frames_after: int = 2) -> None:
        self.call("key_press", key=key)
        if frames_after:
            self.run_frames(frames_after)

    def key_state(self, key: int, down: bool) -> None:
        self.call("key_state", key=key, down=down)

    def type_text(self, text: str, frames_between: int = 2) -> None:
        """Types `text`, stepping the machine between characters.

        Stepping parks the machine when it finishes, so this belongs to a
        client that owns the clock. Use type_text_live() alongside a running
        session a person is watching.
        """
        self.call("key_type", text=text, frames_between=frames_between)

    def type_line(self, text: str, frames_between: int = 2) -> None:
        self.type_text(text + "\r", frames_between=frames_between)

    def type_text_live(self, text: str, delay: float = 0.06) -> None:
        """Types `text` into a freely running machine.

        The Apple II keyboard is a single-byte latch, so characters pushed
        faster than the program reads them are simply lost. Pacing by wall
        clock instead of by frames lets the emulator keep running at full
        speed, which is what a person watching the screen expects.
        """
        for char in text:
            self.call("key_press", key=ord(char))
            time.sleep(delay)

    def type_line_live(self, text: str, delay: float = 0.06) -> None:
        self.type_text_live(text + "\r", delay=delay)

    def joy_axis(self, axis: int, value: int) -> None:
        self.call("joy_axis", axis=axis, value=value)

    def joy_button(self, button: int, down: bool) -> None:
        self.call("joy_button", button=button, down=down)

    def input_capture(self, on: bool = True, limit: int = 0) -> dict:
        """Starts (or stops) logging the input a person makes at the window.

        This socket otherwise sees only what a client sends: someone playing in
        the SDL window is talking to the frontend, and none of it comes back
        here. Calling this again clears the buffer and the dropped count.
        """
        return self.call("input_capture", on=on, limit=limit)

    def audio_dump(self, on: bool = True, path: str | None = None) -> dict:
        """Starts or stops recording the machine's sound to a WAV file.

        Headless writes what the speaker produced, on emulated time, so a
        replay driven as fast as the host allows still comes out at the right
        pitch and length. The SDL frontends write the mixer's output instead —
        the same thing the sound card is playing.
        """
        params: dict = {"on": on}
        if path is not None:
            params["path"] = os.path.abspath(path)
        return self.call("audio_dump", **params)

    def input_poll(self, max_events: int = 256) -> dict:
        """Takes the captured input off the emulator's buffer.

        Events are removed as they are handed over, so each one arrives once.
        `pending` says whether more are waiting, and `dropped` counts input the
        buffer had to throw away — a recorder that sees it above zero is
        looking at a movie with a hole in it.
        """
        return self.call("input_poll", max=max_events)

    # -- disks -------------------------------------------------------------

    def disk_insert(self, drive: int, path: str, write_protected: bool = False) -> dict:
        return self.call(
            "disk_insert",
            drive=drive,
            path=os.path.abspath(path),
            write_protected=write_protected,
        )

    def disk_eject(self, drive: int) -> dict:
        return self.call("disk_eject", drive=drive)

    def disk_swap(self) -> dict:
        return self.call("disk_swap")

    def disk_status(self) -> list[dict]:
        return self.call("disk_status")["drives"]

    # -- memory ------------------------------------------------------------

    def mem_read(self, addr: int, length: int = 1, aux: bool = False,
                 view: str | None = None) -> bytes:
        """Reads `length` bytes.

        `view` picks which image to read: "cpu" is what the running program
        would read this instant, "main" and "aux" are the banks as the
        emulator stores them. They differ wherever the language card has a
        bank swapped in — The Bard's Tale keeps its party there, and the
        "main" copy of it goes stale the moment the game pages the bank out.
        """
        params = {"addr": addr, "len": length}
        if view is None:
            params["aux"] = aux
        else:
            params["view"] = view
        result = self.call("mem_read", **params)
        return base64.b64decode(result["data"])

    def mem_write(self, addr: int, data: bytes | Sequence[int],
                  aux: bool = False, view: str | None = None) -> int:
        """Writes bytes. See `mem_read` for what `view` selects.

        A write the running program is meant to notice needs view="cpu";
        the default lands in the emulator's own copy, which a banked-out
        page is not reading.
        """
        payload = bytes(data)
        params = {"addr": addr, "data": base64.b64encode(payload).decode()}
        if view is None:
            params["aux"] = aux
        else:
            params["view"] = view
        result = self.call("mem_write", **params)
        return result["written"]

    def peek(self, addr: int, aux: bool = False, view: str | None = None) -> int:
        return self.mem_read(addr, 1, aux, view)[0]

    def poke(self, addr: int, value: int, aux: bool = False,
             view: str | None = None) -> None:
        self.mem_write(addr, bytes([value & 0xFF]), aux, view)

    def cpu_regs(self) -> dict:
        return self.call("cpu_regs")

    # -- screen ------------------------------------------------------------

    def screen(self) -> Screen:
        result = self.call("screen_text")
        return Screen(
            lines=tuple(result["lines"]),
            columns=result["columns"],
            text_mode=result["text_mode"],
            mixed=result["mixed"],
        )

    def hires_page(self, page: int | None = None, aux: bool = False) -> bytes:
        """Returns the 8 KB hi-res page the machine is displaying.

        Games that render their own text draw it straight into this page, so
        one call gives a client everything it needs to decode the screen — far
        cheaper than a full framebuffer capture, and it reports which of the two
        pages is live so a page flip cannot be mistaken for a blank screen.
        """
        params: dict = {"aux": aux}
        if page is not None:
            params["page"] = page
        return base64.b64decode(self.call("screen_hires", **params)["data"])

    def screen_hash(self) -> int:
        """A checksum of the live screen, for spotting a redraw cheaply.

        Follows the display mode: the text page in text mode, the hi-res page
        otherwise. Compare it for equality only — the value carries no meaning
        beyond same or different.
        """
        return self.call("screen_hash")["hash"]

    def run_until_change(self, max_frames: int = 600, chunk: int = 48,
                         stable: int = 2, duty: int | None = None,
                         deterministic: bool = False) -> dict:
        """Runs until the screen has changed and then settled.

        The waiting happens inside the emulator. Sampling the screen from here
        would mean a socket round trip per check — hundreds of wakeups for one
        keypress, which holds the CPU at its top clock while usage reads close
        to idle.

        `deterministic` is off, which is almost always what a wait wants:
        whatever is being waited for is usually a disk load, and full speed
        gets through one in a hundredth of the time. The cost is that
        `max_frames` then bounds round trips rather than emulated time — a
        wait of 600 can pass sixty thousand frames of game time. Ask for
        `deterministic=True` when the budget has to mean a duration.
        """
        params = {"max_frames": max_frames, "chunk": chunk, "stable": stable,
                  "deterministic": deterministic}
        if duty is None:
            duty = self.wait_duty
        params["duty"] = duty
        return self.call("run_until_change", **params)

    def press_key(self, key: int, max_frames: int = 600, chunk: int = 48) -> dict:
        """Sends one key and waits for the game to finish reacting."""
        self.call("key_press", key=key)
        return self.run_until_change(max_frames=max_frames, chunk=chunk)

    def hires_info(self) -> dict:
        """Metadata about the live hi-res page, without transferring it."""
        status = self.status()
        return {
            "page": 2 if status.get("page2") else 1,
            "hires": status.get("hires", False),
            "mixed": status.get("mixed", False),
            "text_mode": status.get("text_mode", False),
        }

    def screen_capture(self) -> tuple[int, int, bytes]:
        """Returns (width, height, BGRA8888 pixels) for the current frame."""
        result = self.call("screen_capture")
        return result["width"], result["height"], base64.b64decode(result["data"])

    # A single run_until_change may cover this many frames. The wait is bounded
    # by timeout_frames either way; this only decides how much of it one round
    # trip is allowed to swallow, so a caller still gets a look in on a screen
    # that keeps redrawing.
    wait_window_frames = 600

    def wait_for(
        self,
        patterns: str | Iterable[str],
        timeout_frames: int = 3600,
        chunk: int = 48,
    ) -> tuple[str, Screen]:
        """Advances the machine until one of `patterns` appears on screen.

        Returns the pattern that matched together with the screen it matched
        on. Raises TimeoutError once `timeout_frames` have been consumed, which
        keeps a mis-scripted assistant from spinning forever.

        The screen is only read when something was actually redrawn. The
        waiting itself happens inside the emulator, so a minute of waiting is
        a handful of round trips rather than one per `chunk` frames — polling
        it from here would hold the host CPU at its top clock while usage reads
        close to idle.
        """
        if isinstance(patterns, str):
            patterns = [patterns]
        patterns = list(patterns)

        elapsed = 0
        while True:
            screen = self.screen()
            for pattern in patterns:
                if screen.contains(pattern):
                    return pattern, screen
            if elapsed >= timeout_frames:
                raise TimeoutError(
                    f"none of {patterns!r} appeared within {timeout_frames} frames"
                )
            window = min(self.wait_window_frames, timeout_frames - elapsed)
            result = self.run_until_change(max_frames=window, chunk=chunk,
                                           stable=1)
            # A window that passed with nothing redrawn still counts against the
            # budget, so an unreachable screen times out instead of hanging.
            elapsed += int(result.get("frames", window)) or window

    # -- save states -------------------------------------------------------

    def state_save(self, path: str | None = None) -> str:
        result = self.call("state_save", **({"path": path} if path else {}))
        self._warn_about_unrewound_disks(result, "saved")
        return result["path"]

    def state_load(self, path: str | None = None) -> str:
        result = self.call("state_load", **({"path": path} if path else {}))
        self._warn_about_unrewound_disks(result, "loaded")
        return result["path"]

    def _warn_about_unrewound_disks(self, result: dict, verb: str) -> None:
        """Says out loud what a save state does not carry: the disk images.

        A state is the machine. A program that writes to a disk writes through
        to the file on the host, so rewinding the machine afterwards leaves the
        image written while the machine goes back to believing what it saw
        before — and the two drift apart with nothing to show for it. That is
        how a character disk was destroyed in the workspace this client was
        written for, and why the tool that rolls characters there does not use
        states at all.

        Nothing here can fix it. What it can do is refuse to be silent. Insert
        the disk with `write_protected=True` and the warning goes away, because
        then there is nothing for the machine to disagree with.
        """
        drives = result.get("disks_not_rewound") or []
        if not drives:
            return
        images = {
            entry["drive"]: entry.get("path", "")
            for entry in result.get("disks", [])
        }
        listed = ", ".join(
            f"drive {drive} ({images.get(drive, '?')})" for drive in drives
        )
        warnings.warn(
            f"state {verb}, but a writable disk is in the machine: {listed}. "
            "A state does not carry disk images — anything the game writes to "
            "these stays written after a rewind. Insert them with "
            "write_protected=True to silence this.",
            DiskNotRewoundWarning,
            stacklevel=3,
        )


class EmulatorSession:
    """Launches an emulator process and connects a client to it.

    The socket path is deliberately kept short: `sockaddr_un.sun_path` holds
    only 108 bytes, and a long scratch directory silently overflows it.
    """

    def __init__(
        self,
        binary: str,
        socket_path: str = "/tmp/linapple-agent.sock",
        disk1: str | None = None,
        disk2: str | None = None,
        autoboot: bool = True,
        extra_args: Sequence[str] = (),
    ):
        self.binary = binary
        self.socket_path = socket_path
        self.args = [binary, "--agent-socket", socket_path]
        if disk1:
            self.args += ["--d1", os.path.abspath(disk1)]
        if disk2:
            self.args += ["--d2", os.path.abspath(disk2)]
        if autoboot:
            self.args.append("--autoboot")
        self.args += list(extra_args)
        self.process: subprocess.Popen | None = None
        self.client: LinAppleClient | None = None

    def start(self, connect_timeout: float = 20.0) -> LinAppleClient:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.process = subprocess.Popen(self.args)

        deadline = time.monotonic() + connect_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LinAppleError(
                    f"emulator exited with code {self.process.returncode} before "
                    f"binding {self.socket_path}"
                )
            try:
                self.client = LinAppleClient(self.socket_path)
                return self.client
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_error = exc
                time.sleep(0.1)
        raise LinAppleError(f"could not connect to {self.socket_path}: {last_error}")

    def stop(self) -> None:
        if self.client is not None:
            self.client.quit()
            self.client.close()
            self.client = None
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process = None

    def __enter__(self) -> LinAppleClient:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
