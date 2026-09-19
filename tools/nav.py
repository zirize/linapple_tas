# SPDX-License-Identifier: GPL-2.0-only
"""Advancing the emulator only as far as the game actually needs.

The naive way to drive a menu is to press a key and run a fixed, generous
number of frames — 900 frames is fifteen seconds of emulated time for a redraw
that takes a fraction of one, and every wasted frame is CPU burned at full
clock. Watching for the screen to change instead is the right idea, but it must
not be done from here: sampling over the socket costs a round trip per check,
and a few hundred of those per second keep the CPU out of its idle states, so
usage looks low while the clock and the heat climb. The emulator runs the wait
loop itself and answers once.
"""

from __future__ import annotations

import time


def _rest(pace: float) -> None:
    """A caller's own breather between actions, on top of the duty cycle the
    emulator keeps during the wait. Anything under a millisecond costs a wakeup
    and saves nothing, so it is not worth taking."""
    if pace >= 0.001:
        time.sleep(pace)


def settle(client, max_frames: int = 600, chunk: int = 48,
           stable_chunks: int = 2, pace: float = 0.0) -> int:
    """Runs until the screen stops changing. Returns frames consumed."""
    result = client.run_until_change(max_frames=max_frames, chunk=chunk,
                                     stable=stable_chunks)
    _rest(pace)
    return int(result.get("frames", 0))


def press(client, key: str | int, max_frames: int = 600, chunk: int = 48,
          settle_first: bool = True, pace: float = 0.0) -> int:
    """Sends one key and waits for the game to finish reacting."""
    code = key if isinstance(key, int) else ord(key)
    result = client.press_key(code, max_frames=max_frames, chunk=chunk)
    _rest(pace)
    return int(result.get("frames", 0))


def screen_digest(client) -> int:
    return client.screen_hash()
