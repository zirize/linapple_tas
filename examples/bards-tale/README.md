# The Bard's Tale — a worked example

> Read this when: you have read [docs/new-game.md](../../docs/new-game.md) and
> want to see what that order looks like carried through on a real game.

The Bard's Tale (Interplay, 1985) shows a first-person view. A tool that only
reads the screen cannot tell a temple from any other wall — the party has to
open the door to find out what is behind it, and Skara Brae has 900 squares.
So this is a memory problem, not a screen problem, and it is the one the
general tools in `tools/` were built for.

Everything below was measured against the game running under the patched
emulator. Where an earlier conclusion turned out to be wrong, it says so and
says what the wrong one was; that is usually the more useful half.

## What is here

| | |
|---|---|
| `walker.py` | `CityWalker` — walks the party and writes down which sides of each square are blocked, answering whatever the game interrupts with. The game-specific half of a `memscan` survey |
| `citymap.py` | reads the map out of RAM, draws it, and routes the party to a square — city and dungeon |
| `dungeon_disk.py` | reads every dungeon level straight off the disk image, without playing |

They import the general tools from `tools/` and the client from
`clients/python/`; nothing in `tools/` imports anything here.

**No game data is in this repository** — no disk images, no save states, no
maps. You need your own copy of the game. What is here is the code and the
method.

## The order it actually went

1. **The screen first.** The game draws text through a hi-res font table, so
   `find_font.py` found it and `hires.py` reads the screen as characters. That
   is what makes `walker.py` able to recognise a fight or a locked Guild at
   all. A game without a font table stops here — see the table in
   `docs/new-game.md`.
2. **Coordinates before maps.** The party's `x`, `y` and facing live in three
   zero-page bytes (`$00`, `$01`, `$02` — the defaults in `CityWalker`), found
   with `memscan.py probe`: snapshot, take one step, diff, keep the bytes that
   count with movement. Having them is what makes a blocked step a fact
   instead of a guess.
3. **Then the map.** `memscan.py stable` looks for a region the right size to
   be a map and finds 900 bytes at `$F800`:

       $F800 + (29 - x) * 30 + y        one byte per square, 30 x 30

   `$00` is open street. Everything else is a building, and the value says
   *which* building — the Adventurers Guild is the only `$0B` on the map, so
   identifying one value at one door identifies every other instance of it
   without walking anywhere.
4. **Underground, the same address means something else.** With the party in
   the Catacombs, the city map is gone and the non-zero region is exactly 484
   bytes = 22 x 22. The address formula is *not* the city's:

       $F800 + x * 22 + y               a dungeon floor, 22 x 22

   and a byte is no longer an identity but the square's **four sides**, two
   bits each. Earlier sessions lost time here by reusing the city formula
   underground and reading bytes that meant nothing.

## How the encoding was settled

Not by reading it off and believing it. 45 sides were measured **by walking**,
and every plausible interpretation was scored against them: 6 address formulas
x 24 orderings of the four 2-bit pairs x own-byte/neighbour-byte x polarity =
**576 candidates**. Exactly one scored 45/45. The next best scored 82%.

Then it was checked on squares that were not used in the fit — 56 of them,
**219 of 220 sides agreeing**.

| code | | how it is known |
|---|---|---|
| `0` | open | walked |
| `1` | wall | walked — `RETURN` does not open it either |
| `2` | door | drawn as a door, opens to `RETURN` |
| `3` | secret door | drawn as **plain wall**, opens to `RETURN` |

On one floor the codes come out `0`:1022 · `1`:826 · `2`:82 · `3`:6.

**The one mismatch was the measurement, not the map.** The single disagreeing
side was a step off the edge of the grid, and the conclusion drawn from it —
"this floor does not wrap" — was wrong. Stepping the same boundary from the
other side comes out on the far edge: floors wrap in both axes. The map had
been saying so all along, since all 22 `y`-boundary pairs and all 22
`x`-boundary pairs agree with each other exactly as the 462 interior pairs do.
It was easy to miss because every `x`-boundary pair is a wall, so wrapping
north-south leads nowhere. With wrapping, every one of the 484 squares is
reachable from `(0,0)` — 329 through open floor, 479 counting doors, 484
counting secret doors. Three separate things (formula, encoding, wrapping)
agreeing is the actual evidence; any one of them alone is a guess.

## What the disk knows

The floor the game puts at `$F800` is copied from the disk byte for byte —
checked against two levels read both ways. So `dungeon_disk.py` reads all
sixteen levels from the image without playing the game at all. A level is four
sectors, a track holds two levels, and on this game they sit on tracks 7
through 14.

The disk also carries a plane the walls never mention: one flag byte per
square — stairs up, stairs down, fixed encounter, inscription, darkness, trap.
**A flag is consumed when it fires**, so testing one means rewinding to the
doorstep and stepping on again. Two flag values are still unidentified.

## What cost days

These are the ones worth carrying to another game.

- **A swallowed keystroke looks exactly like a wall.** Between rounds a fight
  only narrates — no menu, no prompt — and that screen eats keys the same way
  a menu does. A step that never happened records as "that side is blocked",
  and a whole session's survey came out wrong that way. `walker.py` reads the
  position back out of RAM after every step and throws away any step that did
  not move exactly one square; that check is the reason to find the
  coordinates first.
- **The key that opens a thing is not the movement key.** Every door and
  building takes `RETURN`. Walking into one does nothing *and says nothing* —
  identical to a wall from the outside. This holds underground too, which is
  how codes `2` and `3` were told apart from `1`.
- **A party teleported by poking its coordinates is not walking the map.**
  Writing `x`/`y` moves the party, but the game keeps something else when it
  enters a square that the write does not update, and the forked party then
  gets **blocked on sides the map says are open** — the same sides every time,
  and which sides depends on where the save was taken. An earlier note here
  said the opposite ("it passes through everything"); that was measured with
  the wrong address formula. It is still usable as *transport*: fork, then
  walk one step into open floor, and from that square the four sides measure
  correctly.
- **Some interruptions are not failures.** Buildings, stairs asking Y/N,
  "Press a key", a statue at a gate offering a fight, and a request for a
  different floppy — all of them have to be answered before the party can be
  asked to take another step. `walker.py` answers them by matching on what the
  game says, and every one of those strings is this game's. That list is the
  part of a survey that does not transfer.

## Running it

```bash
# all sixteen dungeon levels, from the image — no emulator needed
./examples/bards-tale/dungeon_disk.py /path/to/dungeon.dsk
./examples/bards-tale/dungeon_disk.py /path/to/dungeon.dsk --level t12.15 --specials

# with a game running on the socket
./examples/bards-tale/citymap.py --show              # draw the city
./examples/bards-tale/citymap.py --goto 15 24        # walk there
./examples/bards-tale/citymap.py --dungeon           # read $F800 as a floor
./examples/bards-tale/citymap.py --dungeon-probe     # measure this square's four sides
```

`dungeon_disk.py` needs nothing but the image. The rest need the patched
emulator on `/tmp/linapple-agent.sock` with the game booted — see
[docs/build.md](../../docs/build.md) and [README](../../README.md).
