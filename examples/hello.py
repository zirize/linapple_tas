#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Smallest end-to-end check: drive an Apple II and read what it printed.

Needs no game disk. It boots the emulator's own test fixture to the monitor,
jumps to Applesoft in ROM, and asks it to add two numbers -- so if this prints
a 4, the patch, the socket, the client and the screen reader all work.

    python3 examples/hello.py --binary /path/to/linapple \
                              --disk /path/to/linapple/tests/fixtures/minimal.dsk
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from linapple_client import EmulatorSession  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",
                        default=os.environ.get("LINAPPLE_BINARY", "linapple"))
    parser.add_argument("--disk", required=True,
                        help="any bootable .dsk; tests/fixtures/minimal.dsk "
                             "in the LinApple tree will do")
    parser.add_argument("--socket", default="/tmp/linapple-hello.sock")
    args = parser.parse_args()

    session = EmulatorSession(args.binary, socket_path=args.socket,
                              disk1=args.disk, autoboot=True)
    client = session.start()
    try:
        print("connected:", client.version())
        client.run_frames(300)          # boot to the monitor prompt
        client.type_line("E000G")       # enter Applesoft
        client.run_frames(300)
        client.type_line("HOME")
        client.run_frames(200)
        client.type_line("PRINT 2+2")
        client.run_frames(300)

        lines = [line.rstrip() for line in client.screen().lines]
        for line in lines[:6]:
            print(f"  |{line}")
        if "4" in lines:
            print("\nthe machine did the arithmetic. everything is wired up.")
            return 0
        print("\nno answer on screen -- see docs/build.md", file=sys.stderr)
        return 1
    finally:
        session.stop()


if __name__ == "__main__":
    raise SystemExit(main())
