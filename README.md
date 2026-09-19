# LinApple TAS

A control channel for the [LinApple](https://github.com/linappleii/linapple)
Apple II emulator, and the tools that sit on it: frame-exact input recording
and replay, reading a game's screen through its own font, and finding things in
its memory.

One Unix socket, one JSON object per line. Everything a person can do at the
window a program can do over the socket — keys, sticks, disks, memory, the
screen, save states — and, unlike a person, it can do it one frame at a time.

```bash
socat - UNIX-CONNECT:/tmp/linapple-agent.sock
{"id":1,"cmd":"screen_text"}
```

```python
from linapple_client import LinAppleClient

with LinAppleClient("/tmp/linapple-agent.sock") as c:
    c.run_frames(240)
    print(c.screen().text())
    c.type_line("PRINT 2+2")
    hp = c.peek(0xDA80, view="cpu")
```

## Does it work here?

Needs no game disk — it boots the emulator's own test fixture and asks
Applesoft to add two numbers:

```bash
python3 examples/hello.py --binary ./build-headless/linapple \
                          --disk tests/fixtures/minimal.dsk
```

```
connected: {'emulator': 'linapple', 'frontend': 'headless', 'protocol': 1}
  |]PRINT 2+2
  |4

the machine did the arithmetic. everything is wired up.
```

Building the patched emulator: [docs/build.md](docs/build.md).

## What is here

```
patches/     nine commits against LinApple: the socket, its tests, and the
             bugs those tests found
clients/     the Python client (standard library only, >= 3.10)
tools/       twelve tools that use it
docs/        nine short documents, one question each
examples/    a script, a profile and a TAS plan that all run without a game
dev/         check-public.sh (safe to publish?) and smoke.sh (does it work?)
```

`examples/` is the fastest way in, and none of it needs a licensed disk:

| | |
|---|---|
| `hello.py` | boots, enters Applesoft, reads the answer back |
| `profiles/applesoft.json` | the smallest profile that does something — a regex, some keys, a rule that ends the run |
| `plans/applesoft.json` | a TAS plan: ten keystrokes at fixed frames, recordable and replayable |

| tool | |
|---|---|
| `tas.py` | record and replay input frame for frame; verify; render video |
| `hires.py` | decode the hi-res page; read text through a game's own font |
| `find_font.py` | find that font in RAM without being told where |
| `memscan.py` | find coordinates and maps in RAM the way a cheat search does |
| `mapper.py` | record and render where a party has been |
| `engine.py`, `profile.py`, `run_assistant.py` | screen-rule automation from a JSON profile |
| `capture_prompts.py` | log every distinct screen, to write those rules against |
| `screenshot.py` | frame buffer to PNG |
| `nav.py` | advance only until the screen changes |
| `cpumon.py` | what this is costing the host: wakeups, clock, per-chiplet heat |

## Three things worth knowing before you start

1. **`mem_read` defaults to the emulator's image, not the program's memory.**
   Above `$D000` the language card makes those different bytes and the wrong
   one looks plausible — this cost about ten sessions once.
   [docs/memory.md](docs/memory.md)
2. **A frame is a fixed length of time only when you ask for it.** With a drive
   spinning, one frame can be a hundred. [docs/timing.md](docs/timing.md)
3. **Many Apple II games never touch the text page.** They draw every character
   into hi-res with their own font, so reading the screen means lifting that
   font out of RAM. [docs/screen.md](docs/screen.md)

## Documentation

[docs/README.md](docs/README.md) is an index of nine files against the
questions they answer. Nothing is longer than about 4 KB, so a model with a
small context window can load exactly the one it needs —
[AGENTS.md](AGENTS.md) says how.

## Status

Protocol v1, 33 commands. 56 test cases on the emulator side, no game disk
needed. Exercised against several commercial games; what transfers to a new one
and what does not is written down in [docs/new-game.md](docs/new-game.md)
rather than assumed.

## License

GPL-2.0-only, throughout. `patches/` modifies LinApple, which is GPL-2.0, and
the rest is kept under the same terms so that code can move between the two
without a licence question. Full text in [LICENSE](LICENSE).

## Provenance

This is the reusable part of a private workspace that automates specific games.
What stayed behind is everything derived from those games — disk images, save
states, extracted fonts and maps, per-game profiles. None of that is
redistributable, and none of it is needed to use what is here.
