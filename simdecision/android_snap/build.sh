#!/bin/bash
# Build the on-device snapshot helper: Snap.java -> classes.dex inside a jar that app_process can load.
# Needs the Android SDK (a platform android.jar + build-tools d8) and a JDK 11+.   Usage: build.sh <out.jar>
set -euo pipefail
OUT="$1"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}}"
JAR=$(ls -d "$SDK"/platforms/android-*/android.jar 2>/dev/null | sort -V | tail -1)
D8=$(ls -d "$SDK"/build-tools/*/d8 2>/dev/null | sort -V | tail -1)
[ -n "$JAR" ] && [ -n "$D8" ] || { echo "need \$ANDROID_HOME with platforms/*/android.jar and build-tools/*/d8" >&2; exit 2; }
if [ -z "${JAVA_HOME:-}" ]; then  # MCP servers get a minimal environment: look for a JDK with javac
  for c in "$(/usr/libexec/java_home 2>/dev/null || true)" /opt/homebrew/opt/openjdk@17 /opt/homebrew/opt/openjdk /usr/local/opt/openjdk; do
    [ -n "$c" ] && [ -x "$c/bin/javac" ] && JAVA_HOME="$c" && break
  done
fi
[ -n "${JAVA_HOME:-}" ] && export JAVA_HOME PATH="$JAVA_HOME/bin:$PATH"  # d8 runs on java too
JAVAC=javac
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
"$JAVAC" -source 11 -target 11 -nowarn -cp "$JAR" -d "$TMP/classes" "$HERE/Snap.java" 2>&1 | grep -v "bootstrap classpath" || true
"$D8" --min-api 26 --lib "$JAR" --output "$TMP/out.zip" $(find "$TMP/classes" -name '*.class')
mkdir -p "$(dirname "$OUT")"
cp "$TMP/out.zip" "$OUT"
echo "built $OUT"
