#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Records every distinct text screen a game shows.

A profile's rules can only be written once the game's actual prompt wording is
known. Attach this to a session someone is playing by hand and it logs each new
screen to a JSONL file, which is the raw material for the 'when' patterns.

    ./tools/capture_prompts.py --out prompts.jsonl

Press Ctrl-C to stop; the summary lists the screens seen most often.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from linapple_client import LinAppleClient  # noqa: E402


def screen_digest(lines: tuple[str, ...]) -> str:
    joined = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha1(joined.encode()).hexdigest()[:12]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/linapple-agent.sock")
    parser.add_argument("--out", default="screens.jsonl")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="seconds between samples (default: %(default)s)")
    parser.add_argument("--advance", type=int, default=0,
                        help="frames to run between samples; leave at 0 when a "
                             "person is driving the emulator")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen: dict[str, int] = {}
    counts: Counter[str] = Counter()
    first_line: dict[str, str] = {}

    print(f"capturing to {out_path} (Ctrl-C to stop)", flush=True)
    with LinAppleClient(args.socket) as client, out_path.open("a", encoding="utf-8") as sink:
        try:
            while True:
                screen = client.screen()
                digest = screen_digest(screen.lines)
                counts[digest] += 1
                if digest not in seen:
                    seen[digest] = len(seen)
                    stripped = [line.rstrip() for line in screen.lines]
                    non_empty = [line for line in stripped if line.strip()]
                    first_line[digest] = non_empty[0].strip() if non_empty else "(blank)"
                    record = {
                        "digest": digest,
                        "columns": screen.columns,
                        "text_mode": screen.text_mode,
                        "lines": stripped,
                    }
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                    print(f"  [{len(seen):3d}] {digest}  {first_line[digest][:60]}",
                          flush=True)
                if args.advance:
                    client.run_frames(args.advance, deterministic=False)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass

    print(f"\n{len(seen)} distinct screens, most frequent first:")
    for digest, count in counts.most_common(15):
        print(f"  {count:5d}x  {digest}  {first_line[digest][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
