# Command reference

> Read this when: you know what you want to do and need the exact name and
> parameters. For framing and error shape see [protocol.md](protocol.md).

Thirty commands, protocol version 1. Defaults are shown as `= value`.
`commands` at runtime lists what the build in front of you actually answers.

## Meta

| command | params | result |
|---|---|---|
| `ping` | — | `{}` |
| `version` | — | `{protocol, emulator, frontend}` |
| `commands` | — | `{commands: [...]}` |
| `status` | — | `{mode, speed, turbo, cycles, text_mode, hires, col80, mixed, page2, alt_charset}` |

## Input

| command | params | result |
|---|---|---|
| `key_press` | `key: 0..255` | `{}` |
| `key_state` | `key: 0..255`, `down: bool` | `{}` |
| `key_type` | `text: string`, `frames_between: int = 2` | `{typed}` |
| `joy_axis` | `axis: 0..3`, `value: 0..255` | `{}` |
| `joy_button` | `button: 0..2`, `down: bool` | `{}` |

`joy_axis(0..3)` maps to `PDL(0..3)` and the value round-trips exactly.
Buttons are `0..2`, which is what the machine has.

Input is **queued**, not applied on the spot: the queue is drained after a
`run_frames` has already spent that frame's cycles, so the program sees a key
on the *next* frame. A movie that wants a key held must use `key_state(down)`;
`key_press` clears the latch again before a program can read it.

## Run control

| command | params | result |
|---|---|---|
| `run_frames` | `frames: int`, `deterministic: bool = true` | `{frames, cycles, deterministic, mode}` |
| `run_until_change` | `max_frames: int = 600`, `chunk: int = 48`, `stable: int = 2`, `duty: 5..100 = 60`, `deterministic: bool = false` | `{frames, cycles, changed, hash, deterministic, mode}` |
| `pause` / `resume` | — | `{}` |
| `reset` | `full: bool = false` | `{}` |
| `speed_set` | `speed: 0..40` | `{speed}` |
| `turbo_set` | `turbo: bool` | `{turbo}` |
| `quit` | — | `{}` |

`run_frames` always leaves the machine `paused`. What a *frame* means depends
on `deterministic` — see [timing.md](timing.md), which is not optional reading.

## Disk

| command | params | result |
|---|---|---|
| `disk_insert` | `drive: 0\|1`, `path: string`, `write_protected: bool = false` | disk status |
| `disk_eject` | `drive: 0\|1` | disk status |
| `disk_swap` | — | disk status |
| `disk_status` | — | disk status |

Disk status is `{drives: [{drive, loaded, spinning, writing, write_protected, name, path, last_error}, ...]}`.
`disk_insert` returns `ok: true` for a path that does not exist; `loaded` is
the field that tells you.

## Memory and CPU

| command | params | result |
|---|---|---|
| `mem_read` | `addr: 0..65535`, `len: int`, `view: "cpu"\|"main"\|"aux"`, `aux: bool = false` | `{addr, len, view, data: "<b64>"}` |
| `mem_write` | `addr`, `data: "<b64>"` or `bytes: [int]`, `view`, `aux: bool = false` | `{written, view}` |
| `cpu_regs` | — | `{a, x, y, pc, sp, ps, cycles}` |

**`view` is the one parameter most likely to be wrong.** Read
[memory.md](memory.md) before using either command against a game.

## Screen

| command | params | result |
|---|---|---|
| `screen_text` | — | `{columns, rows, lines: [24 strings], text_mode, mixed}` |
| `screen_hash` | — | `{page, source, hash}` |
| `screen_hires` | `page: 1\|2` (optional), `aux: bool = false` | `{page, addr, len, hires, mixed, data: "<b64>"}` |
| `screen_capture` | — | `{width, height, format: "bgra8888", data: "<b64>"}` |

Many games never touch the text page. See [screen.md](screen.md).

## Watching a person play

| command | params | result |
|---|---|---|
| `input_capture` | `on: bool = true`, `limit: int = 4096` | `{capturing, pending}` |
| `input_poll` | `max: int = 256` | `{events, capturing, pending, dropped, cycles}` |

Keys and sticks a person uses at the SDL window are logged here as the bytes
the machine received. `dropped` counts events pushed out of a full buffer,
which is how a recording says it has a gap.

## Audio

| command | params | result |
|---|---|---|
| `audio_dump` | `on: bool = true`, `path: string` (required when turning on) | `{dumping, path}` |

Writes a RIFF WAV of the speaker, stereo 16-bit at 44.1 kHz. Frontends that
cannot record refuse rather than producing a silent file.

**The two frontends time it differently, and one of them is what you want.**

| | what a recording is as long as |
|---|---|
| headless | **emulated time.** 600 frames of emulation wrote 10.014 s of WAV against 10,219,093 cycles run - the same 10.014 s |
| SDL3 | **wall-clock time**, because the samples reach the file from the sound card's callback. A `run_frames` that emulates ten seconds in a tenth of a second wall-clock yields a tenth of a second of audio; 600 frames measured 0.12 s |

So a recording made while a tool drives the machine belongs to the headless
build. The SDL3 one is for watching a session in real time, where the two
agree anyway.

To record sound for a length of emulated time, play it back at that speed —
which is what `tas.py replay --video` does, pairing the dump with `screen_capture`
PNGs and muxing them with ffmpeg.

## Save states

| command | params | result |
|---|---|---|
| `state_save` | `path: string` (optional) | `{path, disks, disks_not_rewound}` |
| `state_load` | `path: string` (optional) | `{path, disks, disks_not_rewound}` |

A state does not carry disk images. Read [save-states.md](save-states.md)
before combining states with a game that writes to disk.
