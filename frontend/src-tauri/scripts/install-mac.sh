#!/usr/bin/env bash
# Build the release app and install it to /Applications.
#
# Signing: an ad-hoc signature changes on every build, so macOS drops the
# microphone / Automation / Contacts grants each time and denies them
# silently.  Signing with a stable certificate keeps the grants.  The
# identity comes from $APPLE_SIGNING_IDENTITY, else from the first line of
# ~/.openjarvis/signing-identity (kept out of the repo on purpose), else
# the build falls back to ad-hoc.
set -euo pipefail

FRONTEND="$(cd "$(dirname "$0")/../.." && pwd)"
APP_NAME="OpenJarvis.app"
BUNDLE="$FRONTEND/src-tauri/target/release/bundle/macos/$APP_NAME"
IDENTITY_FILE="$HOME/.openjarvis/signing-identity"

if [[ -z "${APPLE_SIGNING_IDENTITY:-}" && -f "$IDENTITY_FILE" ]]; then
  APPLE_SIGNING_IDENTITY="$(head -n1 "$IDENTITY_FILE")"
fi
if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  export APPLE_SIGNING_IDENTITY
  echo "Signing with: $APPLE_SIGNING_IDENTITY"
else
  echo "warning: no signing identity; ad-hoc signing will reset macOS permissions" >&2
fi

cd "$FRONTEND"
# Updater artifacts need the updater signing key, which local builds lack.
npx tauri build --bundles app --config '{"bundle":{"createUpdaterArtifacts":false}}'

osascript -e "quit app \"OpenJarvis\"" 2>/dev/null || true
pkill -f "/Applications/$APP_NAME" 2>/dev/null || true
rm -rf "/Applications/$APP_NAME"
ditto "$BUNDLE" "/Applications/$APP_NAME"
open "/Applications/$APP_NAME"
echo "Installed /Applications/$APP_NAME"
