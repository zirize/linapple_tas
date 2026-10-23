# Notes for an AI agent working in this repository

This file exists because the repository is meant to be used by models with a
small context window. Read this, then load **one** document.

## Load only what you need

`docs/README.md` is a table of nine files against the questions they answer.
Each is self-contained and under ~4 KB. Do not read the whole `docs/`
directory; do not read `tools/*.py` to find out what a command does — that is
what `docs/commands.md` is for.

Rough sizes, so you can budget:

| | |
|---|---|
| this file | 2 KB |
| any one doc | 2-5 KB |
| all nine docs | ~30 KB |
| `clients/python/linapple_client/client.py` | 22 KB |
| the largest tool (`tools/memscan.py`) | 29 KB |

## Where to start, by task

| task | read |
|---|---|
| write a client in another language | `docs/protocol.md`, then `docs/commands.md` |
| drive a game from Python | `examples/hello.py`, then `docs/commands.md` |
| read or write game memory | `docs/memory.md` **first** |
| record or replay input | `docs/tas.md` |
| read what is on screen | `docs/screen.md` |
| add support for a new game | `docs/new-game.md` |
| build or test the emulator side | `docs/build.md` |

## Three facts that cause most wrong answers

1. **`mem_read` defaults to `view: "main"`, which is not what the program
   reads.** Above `$D000` the language card makes them different bytes, and the
   wrong one looks plausible. Use `view: "cpu"` for anything the game owns.
2. **A frame is only 17,030 cycles when `deterministic` is true.** Otherwise
   one frame can be a hundred, so a frame count is not a duration.
3. **`ok: true` does not mean it worked.** `disk_insert` says `ok` for a
   missing file; the truth is in `drives[n].loaded`. Input is queued and lands
   a frame later.

## When you are unsure whether a view or a value is right

Do not compare against a value you expect — ask the machine. Plant a small 6502
routine, let it read the address and store the answer somewhere unbanked, run a
frame, and read that. `docs/memory.md` has the six instructions. This is how
the bug in patch 0006 was found, after forty-seven tests had missed it for ten
sessions.

## House style

Prose explains *why*; the code says *what*. Comments that restate the line
below them are noise. When a claim is measured, the number goes in — the docs
here say 51,093 against 5,109,300 rather than "much faster".
