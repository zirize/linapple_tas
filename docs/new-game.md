# Bringing up a new game

> Read this when: you want to point this at a game it has never seen.

## What transfers, and what does not

Checked against three games from different houses — The Bard's Tale, Lode
Runner, Ultima V:

| | transfers |
|---|---|
| socket, input, `run_until_change`, `screen_hash`, `screen_hires`, `screen_capture` | yes, untouched |
| `mem_read`/`mem_write` with `view: "cpu"` | yes — the only value that is not game-specific |
| save states, TAS record/replay, joystick | yes |
| **hi-res font OCR** (`find_font.py`, `hires.py`) | **only if the game has a font table** |
| screen-rule automation (`engine.py`, profiles) | only where OCR works |
| finding coordinates and maps (`memscan.py`) | the method transfers; the addresses never do |

The split is structural, not incidental. OCR assumes the font is indexed by
character code and drawn byte-aligned. Lode Runner draws letters as sprites;
Ultima V spaces text proportionally. Neither has a table to find, so
`find_font.py` correctly reports nothing, and screen-rule automation has
nothing to stand on. TAS and memory work on both.

## The order that works

0. **Check your build first.** `dev/smoke.sh <binary> <any bootable .dsk>`
   drives nine things through the socket and says whether they worked. If it
   fails, the problem is not your game.
1. **Boot it and look.** `examples/hello.py` shows the shape; swap in the
   game's disk and read `screen_text`. If real words come back, the game uses
   the hardware text page and you can skip straight to step 3.
2. **Find the font.** `./tools/find_font.py --socket ...` on a screen with
   prose on it. It reports `font_base`, the view it was found in, and the rows
   that decoded — those rows are your text boxes. Nothing found means see the
   table above.
3. **Collect the wording.** `./tools/capture_prompts.py --out prompts.jsonl`
   while a person plays. A profile's rules match on what the game actually
   says, and guessing the wording is the usual reason a rule never fires.
4. **Write a profile.** JSON: a screen block (font, boxes) and rules of
   `when` (a regex on the text) and `do` (keys, disk swaps, waits).
5. **Check it, then attach.**
   ```bash
   ./tools/run_assistant.py my_game.json --check     # disks exist, rules parse
   ./tools/run_assistant.py my_game.json --attach    # drive a live session
   ```
6. **Find the addresses you need.** `tools/memscan.py` — snapshot, move, diff.
   `probe` finds bytes that count with movement, `stable` finds regions the
   right size to be a map, `match`/`cells` score a candidate map against walls
   you actually walked into.

## Things that cost other people days

- **The key that opens a thing is often not the movement key.** In one game
  every door and building took `RETURN`, while walking into them did nothing
  *and said nothing* — indistinguishable from a wall. If something looks
  uniformly blocked, try the other keys before concluding it is blocked.
- **Do not count steps.** If the coordinates are in RAM, read them. Counting
  moves records blocked steps and forced moves as real ones; over 22 steps one
  such tracker invented ten squares that do not exist.
- **Do not trust a position you wrote.** Teleporting a party by poking its
  coordinates skips the game's own wall checks, so the map you measure that way
  is not the map the game enforces. Walk it.
- **Check the game is reading your input at all** before debugging anything
  else — the A/B in [tas.md](tas.md).
