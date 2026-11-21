#!/usr/bin/env bash
# Smoke tests for verify.sh: direct invocation and per-architecture
# toolchain pin selection. Covers the two regressions a reviewer found
# on the hardening change: verify.sh lost its executable bit (so the
# documented ./verify.sh command failed immediately), and the toolchain
# pins were the Linux x86_64 digests only (so a native Linux AArch64
# setup, as the Docker instructions produce on Apple Silicon, failed
# the first integrity check).
set -uo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd -P)"
pass=0; fail=0
report() { # name ok
  if [[ "$2" == 0 ]]; then echo "PASS  $1"; pass=$((pass+1));
  else echo "FAIL  $1"; fail=$((fail+1)); fi
}

# --- Direct invocation ---------------------------------------------------
[[ -x "$repo/verify.sh" ]]; report "verify.sh is executable" $?
out="$("$repo/verify.sh" 2>&1)"; rc=$?
[[ "$rc" != 0 && "$out" == *"Supply an empty directory path"* ]]
report "direct ./verify.sh invocation reaches the usage check" $?

# --- Pin selection ---------------------------------------------------------
# Extract the shipped pin/function block exactly as verify.sh carries it.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
awk '/^# Integrity pins\./{f=1} /^mkdir "\$work\/parts"/{f=0} f' \
  "$repo/verify.sh" > "$tmp/funcs.sh"
grep -q "select_toolchain_pins()" "$tmp/funcs.sh"
report "pin block defines select_toolchain_pins" $?

pins_for() { # arch -> "lean leanchecker lake libleanshared" digests
  ( source "$tmp/funcs.sh"
    select_toolchain_pins "$1" >/dev/null 2>&1 || exit 1
    printf '%s %s %s %s' "$lean_bin_sha256" "$leanchecker_bin_sha256" \
      "$lake_bin_sha256" "$libleanshared_sha256" )
}
x86="$(pins_for x86_64)"; rc_x86=$?
arm="$(pins_for aarch64)"; rc_arm=$?
arm_alias="$(pins_for arm64)"; rc_alias=$?
[[ "$rc_x86" == 0 && "$x86" == e8baaa71855a616dc351028f3ad2200051b0671f423a1696a100e809302d5550* ]]
report "x86_64 selects the pinned Linux x86_64 digests" $?
[[ "$rc_arm" == 0 && -n "$arm" && "$arm" != "$x86" ]]
report "aarch64 selects its own, different digests" $?
[[ "$rc_alias" == 0 && "$arm_alias" == "$arm" ]]
report "arm64 selects the aarch64 digests" $?
ok=0
for digest in $arm; do
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || ok=1
done
report "aarch64 digests are all SHA-256 shaped" $ok
pins_for riscv64 >/dev/null 2>&1
[[ "$?" != 0 ]]
report "an unsupported architecture is rejected" $?

# --- Integrity regressions -------------------------------------------------
# Covers the three gaps a reviewer found on the hardening change: the
# post-build dependency check trusted whatever lake-manifest.json said at
# the time it ran, a failing `git status` read as a clean tree because its
# exit status was discarded, and --prepare-only returned without rerunning
# the integrity checks. Each test below fails against the unpatched
# verify.sh and passes against the patched one.
real_git="$(command -v git)"
real_sha256sum="$(command -v sha256sum)"

# A project tree whose only dependency is a real, clean git checkout at
# the revision the manifest records.
make_dep_sandbox() { # root -- builds $root/proj
  local dep rev
  dep="$1/proj/.lake/packages/mathlib"
  mkdir -p "$dep"
  git -C "$dep" init -q
  echo stub > "$dep/stub.txt"
  git -C "$dep" add stub.txt
  git -C "$dep" -c user.name=stub -c user.email=stub@example.com commit -qm stub
  rev="$(git -C "$dep" rev-parse HEAD)"
  cat > "$1/proj/lake-manifest.json" <<EOF
{"version": "1.1.0",
 "packagesDir": ".lake/packages",
 "packages": [
 {"rev": "$rev",
  "name": "mathlib"}
 ],
 "name": "lean"}
EOF
}

# The manifest is rewritten after extraction in a way that changes none
# of the values the dependency check parses (the version field is not one
# of them), so only the recorded digest can catch it.
make_dep_sandbox "$tmp/sandbox-a"
( source "$tmp/funcs.sh"
  cd "$tmp/sandbox-a/proj"
  manifest_digest="$(digest lake-manifest.json)"
  sed -i 's/"version": "1.1.0"/"version": "1.1.1"/' lake-manifest.json
  verify_pristine_dependencies ) >/dev/null 2>&1
[[ "$?" != 0 ]]
report "a lake-manifest.json rewritten after extraction fails the dependency check" $?

# A git whose `status` fails with empty output must fail the check, not
# read as a clean tree. Every other git call delegates to the real git.
make_dep_sandbox "$tmp/sandbox-b"
mkdir -p "$tmp/stub-b"
cat > "$tmp/stub-b/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [[ "\$a" == "status" ]]; then exit 1; fi
done
exec "$real_git" "\$@"
EOF
chmod +x "$tmp/stub-b/git"
( export PATH="$tmp/stub-b:$PATH"
  source "$tmp/funcs.sh"
  cd "$tmp/sandbox-b/proj"
  manifest_digest="$(digest lake-manifest.json)"
  verify_pristine_dependencies ) >/dev/null 2>&1
[[ "$?" != 0 ]]
report "a failing git status fails the dependency check instead of reading as clean" $?

# A stubbed end-to-end --prepare-only run. sha256sum reports the pinned
# digests for toolchain paths (a smoke test cannot reproduce the release
# files those digests belong to) and defers to the real sha256sum for
# everything else, so the recorded Audit.lean digest is genuine. The stub
# lake then tampers with Audit.lean during `lake exe cache get`, after
# the first integrity check has run; --prepare-only must notice before
# it returns.
e2e="$tmp/e2e"
mkdir -p "$e2e/record/audit" "$e2e/stubs" "$e2e/tree/lean/.lake/packages/mathlib/.git"
cp "$repo/verify.sh" "$e2e/record/verify.sh"; chmod +x "$e2e/record/verify.sh"
cp "$repo/audit/dependency-artifacts.py" "$e2e/record/audit/"
echo "theorem placeholder : True := trivial" > "$e2e/record/Audit.lean"
printf 'leanprover/lean4:v4.33.1\n' > "$e2e/tree/lean/lean-toolchain"
cat > "$e2e/tree/lean/lake-manifest.json" <<'EOF'
{"version": "1.1.0",
 "packagesDir": ".lake/packages",
 "packages": [
 {"rev": "0df444a360eaa60ab8c11dca51a86af692955474",
  "name": "mathlib"}
 ],
 "name": "lean"}
EOF
tar -czf "$e2e/archive.tar.gz" -C "$e2e/tree" lean
printf '%s  partaa\n' "$(sha256sum "$e2e/archive.tar.gz" | cut -d' ' -f1)" \
  > "$e2e/record/parts.sha256"
cat > "$e2e/stubs/curl" <<EOF
#!/usr/bin/env bash
out=""
while [[ "\$#" -gt 0 ]]; do
  if [[ "\$1" == "--output" ]]; then out="\$2"; shift 2; else shift; fi
done
cp "$e2e/archive.tar.gz" "\$out"
EOF
cat > "$e2e/stubs/elan" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "toolchain" ]]; then
  tc="\$ELAN_HOME/toolchains/leanprover--lean4---v4.33.1"
  mkdir -p "\$tc/bin" "\$tc/lib/lean"
  for f in lean leanchecker lake; do
    printf '#!/bin/sh\necho final-check-ran >> "%s"\n' "$e2e/final-checks.log" > "\$tc/bin/\$f"
    chmod +x "\$tc/bin/\$f"
  done
  echo prepared > "\$tc/lib/lean/Init.olean"
  for f in libleanshared.so libLake_shared.so libleanshared_1.so \
           libleanshared_2.so libInit_shared.so; do
    echo stub > "\$tc/lib/lean/\$f"
  done
  exit 0
fi
for a in "\$@"; do
  if [[ "\$a" == "cache" ]]; then
    mkdir -p .lake/packages/mathlib/.lake/build/lib/lean .lake/build/lib/lean
    echo prepared > .lake/packages/mathlib/.lake/build/lib/lean/Canonical.olean
    if [[ "\${TEST_ATTACK:-audit}" == audit ]]; then
      echo "-- tampered after the archive was verified" >> "$e2e/record/Audit.lean"
    fi
    exit 0
  elif [[ "\$a" == "printenv" ]]; then
    printf '%s:%s:%s\n' "\$PWD/.lake/packages/mathlib/.lake/build/lib/lean" \\
      "\$PWD/.lake/build/lib/lean" "\$ELAN_HOME/toolchains/leanprover--lean4---v4.33.1/lib/lean"
    exit 0
  elif [[ "\$a" == "build" ]]; then
    case "\${TEST_ATTACK:-audit}" in
      cache) echo alternate > .lake/packages/mathlib/.lake/build/lib/lean/Canonical.olean ;;
      snapshot) echo '{}' > ../dependency-artifacts.json ;;
      helper) echo '# rewritten' >> "$e2e/record/audit/dependency-artifacts.py" ;;
      shadow) echo alternate > .lake/build/lib/lean/Init.olean ;;
    esac
    exit 0
  fi
done
exit 0
EOF
cat > "$e2e/stubs/git" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [[ "$a" == "rev-parse" ]]; then
    echo "0df444a360eaa60ab8c11dca51a86af692955474"
    exit 0
  fi
done
exit 0
EOF
pin_values="$( ( source "$tmp/funcs.sh"
  select_toolchain_pins "$(uname -m)" >/dev/null 2>&1
  printf '%s %s %s %s %s %s %s %s' "$lean_bin_sha256" "$leanchecker_bin_sha256" \
    "$lake_bin_sha256" "$libleanshared_sha256" "$liblake_shared_sha256" \
    "$libleanshared_1_sha256" "$libleanshared_2_sha256" "$libinit_shared_sha256" ) )"
read -r p_lean p_leanchecker p_lake p_shared p_lakeshared p_shared1 p_shared2 p_init \
  <<< "$pin_values"
cat > "$e2e/stubs/sha256sum" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in
    */toolchains/*)
      case "\$(basename "\$a")" in
        lean) echo "$p_lean  \$a" ;;
        leanchecker) echo "$p_leanchecker  \$a" ;;
        lake) echo "$p_lake  \$a" ;;
        libleanshared.so) echo "$p_shared  \$a" ;;
        libLake_shared.so) echo "$p_lakeshared  \$a" ;;
        libleanshared_1.so) echo "$p_shared1  \$a" ;;
        libleanshared_2.so) echo "$p_shared2  \$a" ;;
        libInit_shared.so) echo "$p_init  \$a" ;;
      esac
      exit 0
      ;;
  esac
done
exec "$real_sha256sum" "\$@"
EOF
chmod +x "$e2e/stubs/curl" "$e2e/stubs/elan" "$e2e/stubs/git" "$e2e/stubs/sha256sum"
PATH="$e2e/stubs:$PATH" "$e2e/record/verify.sh" "$e2e/run" --prepare-only \
  >"$e2e/out.log" 2>&1
rc_e2e=$?
for _ in 1 2 3 4 5 6 7 8 9 10; do # tee may still be flushing replay.log
  grep -q "REPLAY_INTEGRITY_CHECK_FAILED" "$e2e/run/replay.log" 2>/dev/null && break
  sleep 0.5
done
[[ "$rc_e2e" != 0 ]] && grep -q "REPLAY_INTEGRITY_CHECK_FAILED" "$e2e/run/replay.log"
report "--prepare-only reruns the integrity checks before returning" $?

# Full runs with real artifact hashing and stubbed compiler processes. The guard
# must stop before invoking either final checker, not merely emit an error later.
for attack in clean cache snapshot helper shadow; do
  echo "theorem placeholder : True := trivial" > "$e2e/record/Audit.lean"
  cp "$repo/audit/dependency-artifacts.py" "$e2e/record/audit/"
  rm -f "$e2e/final-checks.log"
  # Wait for verify.sh's asynchronous tee to close its output before reading it.
  TEST_ATTACK="$attack" PATH="$e2e/stubs:$PATH" \
    "$e2e/record/verify.sh" "$e2e/run-$attack" 2>&1 | cat >"$e2e/$attack.log"
  rc=${PIPESTATUS[0]}
  if [[ "$attack" == clean ]]; then
    [[ "$rc" == 0 && -f "$e2e/final-checks.log" ]] &&
      grep -q REPLAY_FRESH_KERNEL_PASSED "$e2e/$attack.log"
    ok=$?
    [[ "$ok" == 0 ]] || cat "$e2e/$attack.log"
    report "an unchanged prepared cache reaches both final checks" "$ok"
  else
    [[ "$rc" != 0 && ! -e "$e2e/final-checks.log" ]] &&
      grep -q REPLAY_INTEGRITY_CHECK_FAILED "$e2e/$attack.log"
    ok=$?
    [[ "$ok" == 0 ]] || cat "$e2e/$attack.log"
    report "post-build $attack tampering stops before the final checks" "$ok"
  fi
done

echo "== verify.sh smoke tests: $pass passed, $fail failed"
[[ "$fail" == 0 ]]
