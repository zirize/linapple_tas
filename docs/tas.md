# Frame-exact input: recording and replaying

> Read this when: you want a run to be reproducible — TAS, a regression check,
> or an RL environment.

## The idea

An agent cannot keep up with an emulator in real time, so the machine is kept
**parked** and stepped: `run_frames` runs exactly what you asked for and leaves
the machine `paused` again. Between steps, time does not pass.

| mode | who owns the clock |
|---|---|
| `paused` | the client. Only `run_frames` advances it |
| `running` | the frontend. Headless sleeps a frame's worth per frame; SDL runs its own 60 Hz loop |

## A movie

```
save state  +  [(frame, input), ...]  +  checkpoints
```

The state fixes where the replay starts — it is matched by fingerprint, and a
replay against a different state is refused. Checkpoints are `screen_hash` and
cumulative cycles at chosen frames; without them a replay is not compared
against anything.

Only these may go into a movie: `joy_axis`, `joy_button`, `key_state`,
`key_press`. Everything else runs frames of its own and desynchronises the
movie's clock — `run_until_change` especially.

## The loop

```
state_load                       (optional) pin the starting point
repeat:
  joy_axis / key_state ...       the inputs for this frame
  run_frames {frames: 1, deterministic: true}
  screen_hash / mem_read         observe
```

Remember the one-frame input lag from [timing.md](timing.md): a key sent before
a `run_frames {1}` is visible to the program on the frame after.

## How sharp is it

Measured by deliberately corrupting a movie and replaying:

| corruption | result |
|---|---|
| all inputs removed | 34 divergences, first at frame 60 |
| one axis value changed 255 → 254 | 12 divergences, first at frame 360 |
| everything shifted 1 frame later | **matches** |
| shifted 2, 5 or 30 frames | all diverge |

So a single step of a stick value is caught, and the timing tolerance is
exactly ±1 frame — which is the same frame the input queue already costs.

## What breaks reproducibility

- **`deterministic: false`** — cycles per call vary by up to 100×. `run_until_change`
  defaults to this; pass `deterministic: true` to put it in a TAS loop.
- **`resume`** — the frontend owns the clock again.
- **a game that writes to disk** — `state_load` rewinds the machine and not the
  host's image. See [save-states.md](save-states.md).
- **a game that reads host time** — a clock card replays different values.

`speed_set` and `turbo_set` change wall-clock speed only, and a deterministic
run turns both off for its duration and restores them, so a TAS loop does not
have to care about them.

## Confirm the game is even reading you

Before blaming the replay: many games choose a controller at the title screen
and then read only that one. The test is a deterministic A/B — from the same
state, change one input, run the same number of frames, compare `screen_hash`.
An input the game ignores gives a hash **identical** to no input at all.

Do not judge this by eye. Titles often run a demo that looks exactly like
someone playing.

## `tools/tas.py`

| subcommand | what |
|---|---|
| `record` | run a written input plan and record it |
| `record --live` | capture what a person plays at the SDL window, then replay once deterministically to bake checkpoints in |
| `replay` | replay and verify; exit code 1 on divergence |
| `replay --video out.mp4` | replay writing PNGs and a WAV, muxed with ffmpeg |
| `bake` | add checkpoints to a movie recorded without them |
| `info` | frames, inputs, dropped count, checkpoint count |

`examples/plans/applesoft.json` is a plan that needs no game: ten keystrokes
at fixed frames, typing an expression at the Applesoft prompt. Recording and
replaying it reproduces the table above on a machine anyone has — inputs
removed gives 12 divergences, shifted one frame still matches, shifted five
gives 10.

A live recording cannot verify itself — nobody counted the frames while a
person was playing — so baking a deterministic replay into it is what turns it
into evidence that the captured input reproduces that play.
