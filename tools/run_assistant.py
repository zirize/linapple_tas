#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Runs a game profile's assistant against a LinApple session.

    ./tools/run_assistant.py examples/profiles/applesoft.json --check
    ./tools/run_assistant.py my_game.json --binary /path/to/linapple

With --attach the script talks to an emulator that is already running with
--agent-socket, which is the usual way to help a session a person is watching.
Without it the script launches its own emulator from the profile's boot disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clients" / "python"))

from engine import Engine, StalledError  # noqa: E402
from linapple_client import EmulatorSession, LinAppleClient  # noqa: E402
from profile import Profile  # noqa: E402

# Where the patched emulator is built is the reader's business, so the default
# is whatever `linapple` resolves to on PATH; $LINAPPLE_BINARY overrides it and
# --binary overrides that.
DEFAULT_BINARY = os.environ.get("LINAPPLE_BINARY", "linapple")
DEFAULT_SOCKET = "/tmp/linapple-agent.sock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="path to a profile JSON file")
    parser.add_argument("--binary", default=DEFAULT_BINARY,
                        help="emulator binary to launch (default: %(default)s)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help="agent socket path (default: %(default)s)")
    parser.add_argument("--attach", action="store_true",
                        help="connect to an already running emulator")
    parser.add_argument("--check", action="store_true",
                        help="validate the profile and exit without running")
    parser.add_argument("--live", action="store_true",
                        help="never pause the emulator; use this when a person "
                             "is watching the game (implied by --attach)")
    parser.add_argument("--stepped", action="store_true",
                        help="force stepped pacing even with --attach")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="stop after this many seconds of wall clock time")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def run(client: LinAppleClient, profile: Profile, args: argparse.Namespace) -> int:
    live = args.live or (args.attach and not args.stepped)
    reader = profile.make_reader(client)
    engine = Engine(
        client,
        profile.rules,
        poll_frames=profile.poll_frames,
        verbose=not args.quiet,
        live=live,
        reader=reader,
        idle_after=profile.idle_after,
        idle_do=profile.idle_do,
    )
    if not args.quiet:
        source = "hi-res font OCR" if reader else "hardware text page"
        print(f"pacing: {'live' if live else 'stepped'}   screen: {source}")
    try:
        engine.run(max_seconds=args.max_seconds)
    except StalledError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    print(f"rules fired: {len(engine.fired)}")
    return 0


def main() -> int:
    args = parse_args()
    profile = Profile(args.profile)
    print(profile.describe())

    missing = profile.missing_disks()
    if missing:
        print("\nmissing disk images:", file=sys.stderr)
        for key, path in sorted(missing.items()):
            print(f"  {key}: {path}", file=sys.stderr)
        if not args.check:
            print("cannot run without them", file=sys.stderr)
            return 2

    if args.check:
        return 1 if missing else 0

    if args.attach:
        with LinAppleClient(args.socket) as client:
            return run(client, profile, args)

    boot = profile.disks.get(profile.boot_disk) if profile.boot_disk else None
    session = EmulatorSession(
        binary=args.binary,
        socket_path=args.socket,
        disk1=str(boot) if boot else None,
        autoboot=True,
    )
    with session as client:
        return run(client, profile, args)


if __name__ == "__main__":
    raise SystemExit(main())
