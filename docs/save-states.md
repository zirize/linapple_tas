# Save states do not carry the disk

> Read this when: you are combining `state_load` with a game that saves.

A state is the machine — memory, registers, where each drive's head is, which
image is in it. It is **not** the image.

A program that writes to a disk writes through to the file on the host. Loading
a state afterwards winds the machine back to before that write while the file
stays written. The machine then goes on believing it is reading the disk it saw
at save time, and the two drift apart with nothing to say so. In the workspace
this code came from, that destroyed a character disk once.

Nothing here can fix it; a state that carried whole disk images would be a
different feature with a different cost. What the commands can do is stop the
mismatch being invisible:

```json
{"ok": true, "result": {
  "path": "/tmp/machine.state",
  "disks": [{"drive": 0, "loaded": true, "write_protected": false, "path": "...", ...},
            {"drive": 1, "loaded": false, ...}],
  "disks_not_rewound": [0]
}}
```

`disks_not_rewound` lists the drives holding an image the rewind will not
reach — loaded and not write-protected. Both `state_save` and `state_load`
report it.

An empty list means there is nothing to worry about. Three ways to get one:

- eject before saving;
- insert with `write_protected: true`, so the machine cannot change the image
  and a rewind cannot end up disagreeing with one;
- accept it deliberately, and do not let the game save.

The Python client raises `DiskNotRewoundWarning` whenever the list is not
empty:

```python
import warnings
from linapple_client import DiskNotRewoundWarning

warnings.simplefilter("error", DiskNotRewoundWarning)   # refuse the combination
```

## Round-tripping

A state restores memory, registers and the screen exactly; the same state plus
the same inputs costs the same cycles, which is what makes a replay checkable.
Both properties are pinned by tests.
