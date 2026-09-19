# SPDX-License-Identifier: GPL-2.0-only
"""Apple II key codes.

The emulator's key input path takes raw Apple II codes, which for printable
characters are plain ASCII. Control characters double as the cursor keys, so
the arrow constants below are the Ctrl codes the ROM's keyboard handler reads.
"""


class Keys:
    LEFT = 0x08
    DOWN = 0x0A
    UP = 0x0B
    RIGHT = 0x15

    RETURN = 0x0D
    ESCAPE = 0x1B
    TAB = 0x09
    BACKSPACE = 0x7F
    SPACE = 0x20

    CTRL_C = 0x03
    CTRL_R = 0x12
    CTRL_S = 0x13
    CTRL_X = 0x18

    Y = ord("Y")
    N = ord("N")


def ctrl(letter: str) -> int:
    """Returns the control code for a letter, e.g. ctrl('c') -> 0x03."""
    return ord(letter.upper()) - 0x40
