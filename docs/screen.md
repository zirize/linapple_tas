# Reading the screen

> Read this when: `screen_text` comes back as garbage, or you need to know what
> a game is saying before deciding what to press.

## Three ways to look

| command | gives you | costs |
|---|---|---|
| `screen_hash` | one number: did the screen change? | a few bytes |
| `screen_text` | 24 rows of the hardware text page | ~1 KB |
| `screen_hires` | the displayed hi-res page, 8 KB | 8 KB + base64 |
| `screen_capture` | the whole frame buffer as BGRA | ~800 KB |

`screen_hash` follows whatever the machine is displaying: the text page in text
mode, the hi-res page otherwise. Mixed mode stays on hi-res deliberately —
hi-res games use the text page as scratch RAM, so folding it in would mean a
screen that never settles. Screen holes are excluded for the same reason.

Compare hashes for equality only; the value means nothing else.

## Many games never touch the text page

A large class of Apple II games draws **every character** into the hi-res page
with their own bitmap font. For those, `screen_text` returns whatever junk is
left in the hardware text page — it is not broken, there is simply nothing
there.

The way to read such a screen is not to train a recogniser. It is to lift the
game's own font out of RAM and invert it into a bitmap-to-character table. The
result is exact: every glyph the game can draw is in the table, spelled the way
the game spells it, with nothing to mislabel.

```
glyph at font_base + code * height, 8 bytes per glyph, 7 pixels per byte
(bit 7 selects the colour palette, not a pixel)
```

`tools/find_font.py` finds `font_base` without being told: a font table indexed
by character code has a shape worth looking for — the space glyph is blank, the
letters are not, no byte sets bit 7, every glyph leaves its bottom row clear.
It scans both the `cpu` and `main` images and reports which one the table was
found in, because a font in the language card is a different table in each.

```bash
./tools/find_font.py --socket /tmp/linapple-agent.sock
# font_base 0xf700  view main  score 2.64  94 glyphs  4 text runs
#     y= 24 x_byte= 0 cols=40  |By Michael Cranford|
```

It also reports the screen rows that decoded to readable text, which is where
a profile's text boxes come from.

**The font is loaded by the game**, so extracting it before the disk has
finished loading yields a table of garbage. `HiresTextReader` re-extracts
whenever decoding starts producing unknown glyphs, which covers a game that
swaps fonts with an overlay.

## Not every game has a font table

This approach assumes the font is indexed by character code and drawn
byte-aligned. Two counterexamples, both checked: Lode Runner draws its letters
as sprites, and Ultima V spaces its text proportionally — of 4,594 eight-row
strips on Ultima V's menu screen, 417 appear verbatim in RAM and all of them
are graphics buffers, not a font. Changing the assumed glyph height from 6 to
12 makes no difference.

There is no shame in this; it means screen-rule automation does not apply to
that game, while TAS, joystick and memory work do. See
[new-game.md](new-game.md).

## 80 columns

`screen_text` reports `columns: 80` when the machine is in 80-column mode and
reads each pair of columns out of two banks — the even column from auxiliary
memory, the odd one from main. This path is checked against the //e's own
80-column firmware, not just against poked bytes.

## Decoding cheaply

Turning a hi-res page into text is a glyph lookup per cell, and it is the most
expensive thing a client does to its host. Gate it on `screen_hash`:
`hires.CachedScreen` decodes only when the hash has moved. Doing this took one
workspace's client from 45.7% of a core to 0.2%.
