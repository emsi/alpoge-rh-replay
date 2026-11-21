#!/usr/bin/env bash
# Run in the prepared, integrity-checked Lean environment after Main is built.
set -euo pipefail
out="${1:?usage: independent-check.sh EMPTY_OUTPUT_DIRECTORY}"
here="$(cd "$(dirname "$0")" && pwd -P)"
: "${LEAN_PATH:?use the prepared Lean search path}"
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
if [[ -n "$(ls -A "$out")" ]]; then
  echo "Choose an empty directory for this audit's evidence." >&2
  exit 1
fi
lean --version | tee "$out/toolchain.log"
sha256sum "$here/ExpectedStatements.lean" "$here/IndependentAudit.lean" > "$out/sources.sha256"
# The expected statements are elaborated with Mathlib alone. Main is never
# imported by the frontend of either source file below.
lean -R "$here" -o "$out/ExpectedStatements.olean" "$here/ExpectedStatements.lean" \
  2>&1 | tee "$out/expected-build.log"
sha256sum "$out/ExpectedStatements.olean" > "$out/expected-object.sha256"
LEAN_PATH="$out:$LEAN_PATH" lean --run "$here/IndependentAudit.lean" 2>&1 \
  | tee "$out/audit.log"
sha256sum -c "$out/sources.sha256"
sha256sum -c "$out/expected-object.sha256"
