# Documentation index

Nine short files. Load the one that answers your question rather than all of
them — each is self-contained and none is larger than about 4 KB.

| file | answers | size |
|---|---|---|
| [protocol.md](protocol.md) | How do I frame a request? What does a reply look like? Why did my client get dropped? | 2.6 KB |
| [commands.md](commands.md) | What is this command called and what does it take? | 4.6 KB |
| [memory.md](memory.md) | Why is the byte I read wrong? Why did my write not stick? What is `view`? | 4.2 KB |
| [timing.md](timing.md) | How much time is a frame? Why did my wait overshoot? Why is the fan loud? | 3.6 KB |
| [screen.md](screen.md) | How do I read what the game is saying when `screen_text` is garbage? | 3.7 KB |
| [tas.md](tas.md) | How do I record and replay input exactly? What breaks reproducibility? | 3.8 KB |
| [save-states.md](save-states.md) | Why did my disk image not rewind with the machine? | 1.9 KB |
| [new-game.md](new-game.md) | How do I point this at a game it has never seen? | 3.3 KB |
| [build.md](build.md) | How do I apply the patches and build? How do I run the tests? | 3.0 KB |

## If you only read two

[memory.md](memory.md) and [timing.md](timing.md). Both describe traps where
the wrong answer looks exactly like the right one — a plausible byte from the
wrong bank, a frame count that is not a duration. Everything else fails
loudly.
