#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Runs the tools against a real emulator, with no game disk.
#
# check-public.sh says the tree is safe to publish; this says it works. Every
# step here drives the machine through the socket and looks at what came back,
# so a tool that imports fine and does nothing is caught.
#
#   dev/smoke.sh /path/to/linapple /path/to/linapple/tests/fixtures/minimal.dsk
#
# The disk can be any bootable image; the fixture in the LinApple tree is the
# one that needs no licence.

set -uo pipefail
cd "$(dirname "$0")/.."

BINARY="${1:-${LINAPPLE_BINARY:-linapple}}"
DISK="${2:-${LINAPPLE_DISK:-}}"
SOCK="${LINAPPLE_SOCKET:-/tmp/linapple-smoke.sock}"
WORK="$(mktemp -d)"
export PYTHONPATH="tools:clients/python"

[ -n "$DISK" ] || { echo "usage: dev/smoke.sh <linapple binary> <boot .dsk>"; exit 2; }

fail=0
say() { printf '%-34s %s\n' "$1" "$2"; }
ok()  { say "$1" "ok"; }
bad() { say "$1" "FAIL"; [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/    /'; fail=1; }

cleanup() { [ -n "${HOLD:-}" ] && kill "$HOLD" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# A session held open for every step below, parked at the Applesoft prompt.
python3 - "$BINARY" "$DISK" "$SOCK" >"$WORK/hold.log" 2>&1 <<'PY' &
import sys, time
sys.path.insert(0, "clients/python")
from linapple_client import EmulatorSession
binary, disk, sock = sys.argv[1:4]
s = EmulatorSession(binary, socket_path=sock, disk1=disk, autoboot=True)
c = s.start()
c.run_frames(300); c.type_line("E000G"); c.run_frames(300)
c.type_line("HOME"); c.run_frames(200)
print("READY", flush=True)
try:
    while True: time.sleep(1)
finally:
    s.stop()
PY
HOLD=$!
for _ in $(seq 1 60); do grep -q READY "$WORK/hold.log" 2>/dev/null && break; sleep 1; done
grep -q READY "$WORK/hold.log" || { bad "emulator reaches Applesoft" "$(tail -5 "$WORK/hold.log")"; exit 1; }
ok "emulator reaches Applesoft"

# The client's own round trip, and the screen reader.
python3 - "$SOCK" >"$WORK/hello.out" 2>&1 <<'PY'
import sys
sys.path.insert(0, "clients/python")
from linapple_client import LinAppleClient
c = LinAppleClient(sys.argv[1])
assert c.version()["protocol"] == 1
c.type_line("PRINT 2+2"); c.run_frames(300)
assert "4" in [l.strip() for l in c.screen().lines], c.screen().text()
print("ok")
PY
grep -q '^ok$' "$WORK/hello.out" && ok "client round trip and screen_text" \
                                 || bad "client round trip and screen_text" "$(cat "$WORK/hello.out")"

# Every command the docs list is a command the build answers.
python3 - "$SOCK" >"$WORK/cmds.out" 2>&1 <<'PY'
import re, sys
sys.path.insert(0, "clients/python")
from linapple_client import LinAppleClient
live = set(LinAppleClient(sys.argv[1]).commands())
doc = open("docs/commands.md").read()
named = set(re.findall(r'^\| `([a-z_0-9]+)`', doc, re.M))
named |= {n for row in re.findall(r'^\| `([a-z_0-9]+)` / `([a-z_0-9]+)`', doc, re.M) for n in row}
missing, extra = sorted(live - named), sorted(named - live)
print("undocumented:", missing or "none", " not in build:", extra or "none")
print("ok" if not missing and not extra else "mismatch")
PY
grep -q '^ok$' "$WORK/cmds.out" && ok "docs cover every live command" \
                                || bad "docs cover every live command" "$(cat "$WORK/cmds.out")"

# The profile engine, end to end. The screen is cleared first: the step above
# left an answer on it and the profile's stopping rule watches for exactly
# that, so without this the engine would stop on the previous step's leftovers
# without ever having driven anything.
python3 - "$SOCK" >/dev/null 2>&1 <<'CLEAR'
import sys
sys.path.insert(0, "clients/python")
from linapple_client import LinAppleClient
c = LinAppleClient(sys.argv[1])
c.type_line("HOME"); c.run_frames(200)
CLEAR
python3 tools/run_assistant.py examples/profiles/applesoft.json --attach \
        --socket "$SOCK" --max-seconds 90 >"$WORK/engine.out" 2>&1
if grep -q 'stop: Applesoft printed the answer' "$WORK/engine.out" \
   && grep -qE 'rules fired: [2-9]' "$WORK/engine.out"; then
    ok "profile engine drives the machine"
else
    bad "profile engine drives the machine" "$(tail -5 "$WORK/engine.out")"
fi

python3 tools/screenshot.py --socket "$SOCK" --out "$WORK/shot.png" >/dev/null 2>&1 \
    && [ -s "$WORK/shot.png" ] && ok "screenshot writes a PNG" || bad "screenshot writes a PNG"

python3 tools/memscan.py --socket "$SOCK" --store "$WORK/scan" dump a >/dev/null 2>&1 \
    && python3 tools/memscan.py --socket "$SOCK" --store "$WORK/scan" dump b >/dev/null 2>&1 \
    && python3 tools/memscan.py --socket "$SOCK" --store "$WORK/scan" diff a b >/dev/null 2>&1 \
    && ok "memscan snapshots and diffs" || bad "memscan snapshots and diffs"

# A font search on a hardware-text screen must come back empty rather than
# inventing a table -- the honest answer is the one being checked.
python3 tools/find_font.py --socket "$SOCK" >"$WORK/font.out" 2>&1
grep -q 'no font table found' "$WORK/font.out" && ok "find_font declines a text screen" \
                                               || bad "find_font declines a text screen" "$(head -3 "$WORK/font.out")"

# TAS: record a movie, replay it, and confirm the replay check is not vacuous.
python3 - "$SOCK" "$WORK" >/dev/null 2>&1 <<'PY'
import json, sys
sys.path.insert(0, "clients/python")
from linapple_client import LinAppleClient
sock, work = sys.argv[1:3]
LinAppleClient(sock).state_save(f"{work}/start.state")
plan = json.load(open("examples/plans/applesoft.json"))
plan["state"] = f"{work}/start.state"
json.dump(plan, open(f"{work}/plan.json", "w"))
PY
python3 tools/tas.py --socket "$SOCK" record --plan "$WORK/plan.json" \
        --out "$WORK/m.json" >/dev/null 2>&1
python3 tools/tas.py --socket "$SOCK" replay "$WORK/m.json" >"$WORK/replay.out" 2>&1
grep -q 'replay matches' "$WORK/replay.out" && ok "TAS records and replays" \
                                            || bad "TAS records and replays" "$(tail -3 "$WORK/replay.out")"

python3 - "$WORK" >/dev/null 2>&1 <<'PY'
import json, sys
work = sys.argv[1]
m = json.load(open(f"{work}/m.json"))
m["events"] = []
json.dump(m, open(f"{work}/m_empty.json", "w"))
PY
python3 tools/tas.py --socket "$SOCK" replay "$WORK/m_empty.json" >"$WORK/replay2.out" 2>&1
grep -q 'divergence' "$WORK/replay2.out" && ok "a replay check that can fail" \
                                         || bad "a replay check that can fail" "$(tail -3 "$WORK/replay2.out")"

echo
[ "$fail" -eq 0 ] && echo "the tools work" || echo "SOMETHING IS BROKEN"
exit "$fail"
