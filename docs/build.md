# Building the patched emulator

> Read this when: you are setting up from scratch, or the tests will not build.

## What the patches do

`patches/` holds seven commits against [LinApple](https://github.com/linappleii/linapple)
(CMake + SDL3 tree). They add `src/frontends/common/agent/` — a Unix socket
control channel — plus the tests for it, and fix what those tests found. They
are `git format-patch` output, so `git am` applies them.

> **They are against upstream `master` at `86bee1c` (2026-10-10).** A fresh
> clone plus these seven gives tree hash `f348b23`, the same as the branch they
> were made from. From that tree `ctest` is **118/118** (the base is 117; the
> series adds `test-agent-commands`), also under ASAN and UBSAN, **all five
> frontends build with 0 errors** — sdl1, sdl2, sdl3, tui, headless — and sdl3
> and headless each pass `dev/smoke.sh` 9/9. If upstream has moved again since
> and a patch refuses, `git checkout 86bee1c` first and the series applies as
> it did here.
>
> The series used to be nine commits on `3c00687`. Two of them fixed slot 0's
> peripherals reading each other's commands; upstream merged its own version
> of that fix (command subsystems and `peripheral_command_by_id`), so they were
> dropped when the series moved.

| | |
|---|---|
| 0001 | the agent socket control channel |
| 0002 | refuse an SDL3 recording that would be silent |
| 0003 | test that a state comes back and a run repeats |
| 0004 | close four gaps in the agent tests |
| 0005 | let a caller read the memory the program is actually reading (`view`) |
| 0006 | mark a page an agent write changed, and ask the 6502 about it |
| 0007 | read the screen off a real program; say what a run and a state promise |

## Dependencies

`cmake`, `libsdl3-dev`, `libsdl3-image-dev`, `libcurl4-openssl-dev`,
`libzip-dev`, `zlib1g-dev`, `nlohmann-json3-dev`, `imagemagick`.

The sdl1 and sdl2 frontends additionally need `libsdl1.2-dev`,
`libsdl-image1.2-dev`, `libsdl2-dev` and `libsdl2-image-dev`. They are not
needed for the headless or sdl3 builds below.

## Apply and build

```bash
git clone https://github.com/linappleii/linapple.git && cd linapple
git am /path/to/linapple_tas/patches/*.patch

# windowed, for watching a session
cmake -B build -G Ninja -DFRONTEND=sdl3 -DBUILD_TESTING=OFF
cmake --build build -j$(nproc)

# headless, for batch work and TAS
cmake -B build-headless -G Ninja -DFRONTEND=headless -DBUILD_TESTING=OFF
cmake --build build-headless -j$(nproc)
```

`-DENABLE_AGENT=OFF` builds without the agent module.

Both frontends are verified from a clean checkout of the series. They differ in
one way worth knowing: the SDL3 build owns its own clock, so `status` reports
`running` until the first `run_frames` parks it, while headless is parked from
the start. Everything else behaves the same, TAS replay included.

You can drive the SDL3 build without it taking over a screen or a keyboard,
which is what you want when a tool is driving it rather than a person:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
    ./build/linapple --agent-socket /tmp/linapple-agent.sock --d1 game.dsk --autoboot
```

## Tests

50 cases, about five seconds (two of them boot the ROM to Applesoft), no
game disk needed. They are built only when `ENABLE_AGENT` is on.

```bash
cmake -B build-tests -G Ninja -DFRONTEND=headless -DBUILD_TESTING=ON
cmake --build build-tests --target test-agent-commands -j$(nproc)
./build-tests/test-agent-commands
```

If doctest is not installed, CMake fetches it — and the fetched 2.4.11
declares `cmake_minimum_required(3.0)`, which current CMake refuses:

```
CMake Error at build-tests/_deps/doctest-src/CMakeLists.txt:1:
  Compatibility with CMake < 3.5 has been removed from CMake.
```

Take the escape hatch that error itself names. This configures and builds from
a clean checkout, verified:

```bash
cmake -B build-tests -G Ninja -DFRONTEND=headless -DBUILD_TESTING=ON \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5
```

The failed configure downloads doctest before rejecting it, so pointing at the
header it left behind works too, if you would rather not loosen a policy for
the whole build:

```bash
cmake -B build-tests -G Ninja -DFRONTEND=headless -DBUILD_TESTING=ON \
    -DDOCTEST_INCLUDE_DIR=$PWD/build-tests/_deps/doctest-src/doctest
```

## Run it

```bash
./build/linapple --agent-socket /tmp/linapple-agent.sock --d1 game.dsk --autoboot
```

The socket path must be under 108 bytes (`sun_path`). The tools here default to
`/tmp/linapple-agent.sock`, so using that path means never passing `--socket`.

Talk to it with nothing but `socat`:

```bash
socat - UNIX-CONNECT:/tmp/linapple-agent.sock
{"id":1,"cmd":"version"}
```

## Python side

Standard library only, Python ≥ 3.10. `Pillow` is needed for PNG output
(`screenshot.py`, `tas.py --video`) and `ffmpeg` for muxing video.

```bash
pip install -e clients/python        # or just add it to PYTHONPATH
python3 examples/hello.py --binary ./build-headless/linapple \
                          --disk tests/fixtures/minimal.dsk
```

## Checking the whole thing

```bash
dev/smoke.sh ./build-headless/linapple tests/fixtures/minimal.dsk
```

It takes either frontend's binary. Nine checks against a live machine — the client's round trip, the screen
reader, that the command reference still matches what the build answers, the
profile engine, screenshots, memory snapshots, a font search that correctly
finds nothing on a text screen, and a TAS recording replayed both intact and
deliberately broken. No game disk anywhere in it.
