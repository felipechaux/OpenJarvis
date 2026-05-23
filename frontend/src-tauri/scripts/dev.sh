#!/bin/bash
# dev.sh — reliable dev launcher that keeps the binary properly codesigned.
#
# Replaces ``npm run tauri dev`` for local development on macOS.  The
# stock tauri dev workflow re-applies cargo's "linker-signed" adhoc sig
# on every rebuild, which leaves the embedded Info.plist unbound and
# breaks the webview's mic permission prompt.  We orchestrate manually:
#
#   1. start vite (frontend assets at :5173)
#   2. build the Rust binary with ``-Wl,-no_adhoc_codesign`` so the
#      linker leaves it unsigned
#   3. codesign with a stable bundle identifier so TCC sees the plist
#   4. launch the binary directly
#   5. on exit / Ctrl-C: tear everything down
#
# Use this instead of ``npm run tauri dev`` whenever the wake-word /
# voice features matter.  For pure UI work, ``npm run tauri dev`` is
# fine — the codesign issue only shows up if the webview needs
# privacy-gated APIs (mic, camera).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"
SRC_TAURI="$FRONTEND/src-tauri"
BIN="$SRC_TAURI/target/debug/openjarvis-desktop"
IDENTIFIER="com.openjarvis.desktop"

cleanup() {
    echo
    echo "[dev] shutting down..."
    [[ -n "${VITE_PID:-}" ]] && kill "$VITE_PID" 2>/dev/null || true
    [[ -n "${BIN_PID:-}" ]] && kill "$BIN_PID" 2>/dev/null || true
    pkill -f "JarvisWake" 2>/dev/null || true
    lsof -ti:5173 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM EXIT

echo "[dev] starting vite..."
cd "$FRONTEND"
npm run dev > /tmp/openjarvis-vite.log 2>&1 &
VITE_PID=$!
until curl -sS --max-time 1 http://localhost:5173 >/dev/null 2>&1; do sleep 1; done
echo "[dev] vite ready on :5173"

echo "[dev] building Rust binary..."
cd "$SRC_TAURI"
cargo build 2>&1 \
    | grep -vE "warning|note|help|\\^|--" \
    | tail -10

# Cargo's linker-signed adhoc sig leaves the embedded Info.plist
# "not bound" — TCC then refuses to read it and the webview's
# getUserMedia silently fails.  Re-sign with --force to bind the
# plist and lock the identifier so TCC's mic grant is stable
# across rebuilds.
echo "[dev] codesigning..."
codesign --force --sign - --identifier "$IDENTIFIER" "$BIN"
codesign -d --verbose "$BIN" 2>&1 | grep -E "Identifier|Info.plist"

echo "[dev] launching openjarvis-desktop..."
"$BIN" &
BIN_PID=$!
echo "[dev] running (pid $BIN_PID).  Ctrl-C to stop."
wait "$BIN_PID"
