#!/usr/bin/env bash
# Build the JarvisWake macOS sidecar.
#
# Output: ../binaries/JarvisWake.app
#
# macOS TCC (the privacy daemon) only reads NSMicrophoneUsageDescription
# and NSSpeechRecognitionUsageDescription from a proper .app bundle's
# ``Contents/Info.plist`` — embedding the plist in the binary's
# ``__TEXT,__info_plist`` section is NOT enough for permission prompts to
# appear (the binary crashes with TCC's "missing usage description"
# message).  So we wrap the compiled executable in a minimal LSUIElement
# .app bundle and adhoc-sign the whole thing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/JarvisWake/main.swift"
INFO_PLIST="$SCRIPT_DIR/JarvisWake/Info.plist"
OUT_DIR="$SCRIPT_DIR/../binaries"
APP_BUNDLE="$OUT_DIR/JarvisWake.app"
APP_BIN="$APP_BUNDLE/Contents/MacOS/JarvisWake"
APP_INFO="$APP_BUNDLE/Contents/Info.plist"

if [[ ! -f "$SRC" ]]; then
    echo "build.sh: source not found at $SRC" >&2
    exit 1
fi
if [[ ! -f "$INFO_PLIST" ]]; then
    echo "build.sh: Info.plist not found at $INFO_PLIST" >&2
    exit 1
fi

mkdir -p "$APP_BUNDLE/Contents/MacOS"

# Rebuild only when the source or plist is newer than the bundled binary.
if [[ -f "$APP_BIN" && "$SRC" -ot "$APP_BIN" && "$INFO_PLIST" -ot "$APP_BIN" ]]; then
    echo "JarvisWake.app up to date."
    exit 0
fi

echo "Compiling JarvisWake..."
swiftc \
    -O \
    -target arm64-apple-macos14 \
    -framework AVFoundation \
    -framework Speech \
    -o "$APP_BIN" \
    "$SRC"
chmod +x "$APP_BIN"

cp "$INFO_PLIST" "$APP_INFO"

# Adhoc-sign the bundle so TCC keeps the privacy authorisation cache keyed
# to this exact build.  Without ``--deep`` only the top-level executable
# gets signed, leaving the bundle's Info.plist unsealed.
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - --deep \
        --identifier com.openjarvis.desktop.wake \
        "$APP_BUNDLE"
fi

echo "Built: $APP_BUNDLE"
