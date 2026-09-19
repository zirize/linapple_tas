# Frames, determinism, and not cooking a core

> Read this when: a wait overshoots, a replay drifts, or the host fan is loud.

## `deterministic` decides what "frame" means

It is not a speed switch. Turbo, and the full-speed path the core takes by
itself **whenever a drive is spinning**, both make one frame run a frame's
cycles a hundred times over.

Measured, three frames of an idle program:

| | cycles |
|---|---|
| turbo off | 51,093 |
| turbo on, `deterministic: true` | 51,093 |
| turbo on, `deterministic: false` | 5,109,300 |

| | one frame is | use it for | cost |
|---|---|---|---|
| `deterministic: true` (default for `run_frames`) | always 17,030 cycles | TAS, RL steps, anything counting frames **as time** | a disk load takes as long as it did in 1985 |
| `deterministic: false` | up to 100 frames while a drive spins or turbo is on | skipping boots and loads | a frame count is no longer a duration |

The trap: a wait of "sixty frames" can pass six thousand frames of game time
and walk straight past the screen it was waiting for. Both commands report
`deterministic` in the reply so a caller can tell which it got.

Either way the caller's turbo setting is restored — turbo belongs to the person
at the window, not to the command.

## Waiting for the screen

`run_until_change` waits **inside the emulator** until the screen has changed
and then settled. It hashes the screen every `chunk` frames, and stops after
`stable` identical hashes following at least one change. `max_frames` caps it;
no change at all comes back as `changed: false`.

It defaults to `deterministic: false`, the opposite of `run_frames`, because
what is usually being waited for is a disk load. Under that default
`max_frames` bounds **round trips, not emulated time**. Pass
`deterministic: true` when the budget has to mean a duration.

**Do not reimplement this loop in the client.** Sampling the screen over the
socket costs a round trip per check — hundreds of wakeups for one keypress,
which pins the host core at its top clock while CPU usage still reads near
idle. Moving one 600-frame wait into the emulator took it from 120 round trips
to 1.

## The duty cycle

`duty` (5..100, default 60) is the share of wall-clock the emulator may spend
working during a wait; it rests in proportion for the remainder. Rests shorter
than a millisecond are accumulated and paid at once — a microsecond sleep costs
a wakeup and saves nothing, because the core never reaches a deep idle state at
that granularity.

`run_frames` is the exception: it runs the requested frames flat out, because
the client asked for exactly that many. Long runs should be split and paced on
the client side; the Python client does this itself (`pace_chunk`, `pace_duty`).

## Judging the cost

`tools/cpumon.py` measures what actually matters: CPU share, **wakeups per
second**, real core clock, deep-idle residency, and per-chiplet temperature —
no root needed.

Wakeups are the metric that catches a client polling too fast. Temperature is
the one that catches work that is genuinely heavy: in one case a client was
using 45.7% of a core decoding screens while the emulator used 33%, and only
the thermal reading made it visible. Watch per-chiplet `Tccd`, not the package
average — one chiplet at 84.5 C sat next to another at 46 C.

## One frame of input lag

The input queue is drained *after* a `run_frames` has spent that frame's
cycles, so the program cannot see a key during the frame that delivers it. It
shows up on the frame after. This is fixed, measured against the `$C000` latch,
and pinned by a test — build timing on it rather than around it.
