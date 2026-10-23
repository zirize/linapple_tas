# Reading the memory the program is reading

> Read this when: a value you read looks plausible but wrong, a value you wrote
> did not take, or you are about to write a cheat.

## The three views

`mem_read` and `mem_write` take `view`:

| `view` | what it is |
|---|---|
| `"cpu"` | **the byte the running program reads right now.** Follows bank switching |
| `"main"` (default) | the emulator's own image of main memory |
| `"aux"` | the auxiliary bank image (80-column text, double hi-res) |

Anything else is refused. The reply echoes which image answered. Omitting
`view` keeps the older `aux: bool` behaviour.

Below `$C000` the three mostly agree. Above `$D000` they do not, and that is
where games keep the data worth reading.

## Why "main" is not the program's memory

The language card swaps 16 KB in over `$D000`-`$FFFF`. The emulator keeps the
two banks it holds **one bank apart** in its main image:

| card bank | where it lives in the main image | `main` address for `$DA80` |
|---|---|---|
| bank 2 | `$D000`-`$DFFF` | `$DA80` |
| bank 1 | `$C000`-`$CFFF` (`$1000` lower) | `$CA80` |

So a tool reading `main` at `$DA80` gets bank 2 whether or not bank 2 is the
one the program is using, and it gets a *plausible* byte either way — the
failure is silent. In one workspace this cost about ten sessions and left a
false explanation in the documentation, plus an address (`$CA80`) written down
as if it were a location of its own rather than the same byte seen through the
other bank.

Which bank a game uses varies by game and by moment. Measured: Ultima V and
The Bard's Tale I were both on bank 2 when sampled; Lode Runner on neither.
There is no table to learn. **Use `view: "cpu"` for anything the game owns.**

## Writes are marked, and why that matters

When the page a write lands on is the one currently mapped in, the byte goes
into the live image rather than into the bank's own store. The next soft switch
copies a page back to its bank **only if something marked the page as
changed**. The CPU marks its own writes; a write arriving over a socket has no
CPU behind it.

Before this was fixed, such a write was thrown away at the next bank switch —
a fraction of a second for a game keeping data in the language card. The
symptom is nasty: the write reads back correctly, holds while nothing happens,
and then silently reverts. `mem_write` now marks the page (patch 0006), for a
write that lands on the live image and not for one that lands in a bank's
store, where marking would copy the unchanged live page over the write instead.

## Checking a view instead of trusting it

The way to be sure is not to compare against a value you expect. It is to ask
the 6502. Plant a routine that reads the address itself and stores the result
somewhere unbanked, run a frame, and compare that byte with what `mem_read`
returned:

```
$0300  AD 8B C0   LDA $C08B     ; select bank 1, twice to enable writes
       AD 8B C0   LDA $C08B
       AD 80 DA   LDA $DA80     ; the address in question
       8D 20 03   STA $0320     ; somewhere no switch moves
       4C 0C 03   JMP $030C
```

Then `mem_read(0x0320, 1)` is the program's own answer. The emulator's test
suite does exactly this across all sixteen of `$C080`-`$C08F`, which is how
the write bug above was found within a minute of the first case being written.

## Finding an address in the first place

`tools/memscan.py` is the search: take full 64 KB snapshots with the machine in
known states, then narrow by how bytes behave — `probe` for bytes that count up
and down as something moves, `track` for a byte following a value you know,
`stable` for regions the right size to be a map, `match`/`cells` for scoring a
candidate map against observed walls.

Its snapshots use the default `main` view, and that is deliberate: for a known
address in language-card RAM, `main` hands back that bank's store whether the
card is paged in or not, while `cpu` hands back the ROM whenever it is paged
out. `cpu` is right when you do not know which bank holds the data; `main` is
steadier once you do. `tools/find_font.py` sidesteps the choice by scanning
both images and reporting which one the table was found in.
