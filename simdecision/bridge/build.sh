#!/bin/bash
# Build the XCUITest bridge for one platform. It drives an INSTALLED app by bundle id, so the app under test needs no
# changes and no rebuild.  Usage: build.sh ios|tvos <simulator-udid> <derived-data-dir>
set -euo pipefail
PLATFORM="$1"; UDID="$2"; DD="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
SCHEME=$([ "$PLATFORM" = tvos ] && echo QABridgeTV || echo QABridge)
# a bridge still running from a previous build would keep serving old code
pkill -f "test-without-building.*only-testing:${SCHEME}UITests" 2>/dev/null || true
command -v xcodegen >/dev/null || { echo "xcodegen not found: brew install xcodegen" >&2; exit 2; }
cd "$HERE"
xcodegen generate >/dev/null
rm -rf "$DD"
exec xcodebuild build-for-testing -project QABridge.xcodeproj -scheme "$SCHEME" -destination "id=$UDID" \
  -derivedDataPath "$DD" -configuration Debug CODE_SIGNING_ALLOWED=NO
