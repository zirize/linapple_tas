#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Refuses to let this tree become unpublishable.
#
# It is maintained by hand from a private workspace, so the failure mode is not
# a bug -- it is a copy that brings something along with it. This checks for the
# three kinds of thing that must never land here: files carrying game data,
# paths from the machine it was written on, and code that no longer runs.
#
# Run it before every push. Exit 0 means the tree is safe to publish.
#
# It says nothing about whether any of this works -- that is dev/smoke.sh.

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
say() { printf '%-42s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL"; shift; printf '%s\n' "$@" | sed 's/^/    /'; fail=1; }

# 1. No game data, in any form. Disk images and save states are the whole
#    machine's RAM; JSON dumps of maps and fonts are the game's data by another
#    name.
hits=$(find . -path ./.git -prune -o -type f \
    \( -name '*.dsk' -o -name '*.po' -o -name '*.nib' -o -name '*.woz' \
       -o -name '*.2mg' -o -name '*.state' -o -name '*.aws' -o -name '*.rom' \) -print)
[ -z "$hits" ] && say "no game images or save states" "ok" \
                || bad "no game images or save states" "$hits"

# 2. No paths from the machine this was written on, and no references to files
#    that live only in the private tree.
hits=$(grep -rn --include='*.py' --include='*.md' --include='*.sh' \
    --include='*.json' --exclude-dir=__pycache__ --exclude-dir=.git \
    '/home/[a-z]\+/\|/mnt/sdb1\|captures/\|dsk/' . \
    | grep -v '^\./dev/check-public.sh' || true)
[ -z "$hits" ] && say "no private paths" "ok" || bad "no private paths" "$hits"

# 3. English only in the shipped source. The workspace this came from writes in
#    Korean; a stray log string is how that leaks.
hits=$(grep -rlP --include='*.py' --include='*.md' --exclude-dir=__pycache__ \
    --exclude-dir=.git '[\x{AC00}-\x{D7A3}]' . || true)
[ -z "$hits" ] && say "no untranslated strings" "ok" \
                || bad "no untranslated strings" "$hits"

# 4. GPL-2.0 header on every shipped source file, since that is the licence the
#    whole tree is under.
hits=$(find tools clients examples dev -name '*.py' -o -name '*.sh' \
    | xargs grep -L 'SPDX-License-Identifier: GPL-2.0-only' || true)
[ -z "$hits" ] && say "SPDX header on every source" "ok" \
                || bad "SPDX header on every source" "$hits"

# 5. Everything still imports. A tool broken by an edit made while sanitising
#    is worse than one that was never published.
broken=""
for f in tools/*.py; do
    name=$(basename "$f" .py)
    PYTHONPATH="tools:clients/python" python3 -c "import $name" 2>/dev/null \
        || broken="$broken $name"
done
[ -z "$broken" ] && say "every tool imports" "ok" || bad "every tool imports" "$broken"

# 6. Everything the docs and tools point at has to exist. A reference that
#    dangles is how a reader's first five minutes get wasted, and sanitising
#    this tree by hand is exactly how one gets introduced.
missing=""
for ref in $(grep -rhno --include='*.md' --include='*.py' \
        'examples/[a-z_/]*\.\(py\|json\)' . | sed 's/^[0-9]*://' | sort -u); do
    [ -e "$ref" ] || missing="$missing $ref"
done
[ -z "$missing" ] && say "every referenced example exists" "ok" \
                  || bad "every referenced example exists" "$missing"

# 7. The documents an agent is told to load have to exist.
missing=""
for d in $(grep -o '(\([a-z-]*\.md\))' docs/README.md | tr -d '()'); do
    [ -f "docs/$d" ] || missing="$missing docs/$d"
done
[ -z "$missing" ] && say "every indexed doc exists" "ok" \
                  || bad "every indexed doc exists" "$missing"

echo
[ "$fail" -eq 0 ] && echo "safe to publish" || echo "NOT safe to publish"
exit "$fail"
