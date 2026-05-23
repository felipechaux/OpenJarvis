#!/bin/bash
# codesign-watch.sh — keep the dev binary properly signed.
#
# Cargo's adhoc linker-sign leaves the embedded ``__TEXT,__info_plist``
# section *unbound*, so macOS TCC can't read the usage descriptions and
# silently denies the webview's getUserMedia() / camera prompts.  This
# watcher re-signs the binary with a stable bundle identifier and a
# bound plist every time cargo overwrites it during a rebuild.  Without
# this, every ``tauri dev`` rebuild breaks the mic.
#
# Usage:
#   bash frontend/src-tauri/scripts/codesign-watch.sh
#
# Run this in a separate terminal alongside ``npm run tauri dev``.  It
# stays running until you Ctrl-C; safe to leave open across many builds.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BIN="$REPO_ROOT/frontend/src-tauri/target/debug/openjarvis-desktop"
IDENTIFIER="com.openjarvis.desktop"

needs_resign() {
    # Returns 0 if the binary needs re-signing, 1 if it's already fine.
    local out
    out=$(codesign -d --verbose "$BIN" 2>&1 || true)
    if ! grep -q "Identifier=$IDENTIFIER" <<<"$out"; then
        return 0
    fi
    if grep -q "Info.plist=not bound" <<<"$out"; then
        return 0
    fi
    if grep -q "linker-signed" <<<"$out"; then
        return 0
    fi
    return 1
}

resign() {
    codesign --force --sign - --identifier "$IDENTIFIER" "$BIN" 2>&1 \
        | sed 's/^/[codesign] /'
}

echo "[codesign-watch] watching $BIN"
last_mtime=""
while true; do
    if [[ -f "$BIN" ]]; then
        mtime=$(stat -f "%m" "$BIN" 2>/dev/null || echo "")
        if [[ "$mtime" != "$last_mtime" ]] && needs_resign; then
            resign
            last_mtime="$mtime"
            # After re-sign, kick the binary so tauri dev relaunches it
            # with the new signature.  Without this the still-running
            # process keeps the old (broken) identity in memory.
            pkill -f "target/debug/openjarvis-desktop" 2>/dev/null || true
            echo "[codesign-watch] re-signed + killed; tauri dev will relaunch"
        fi
    fi
    sleep 1
done
