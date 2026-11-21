#!/usr/bin/env bash
set -euo pipefail
record="$(cd "$(dirname "$0")" && pwd -P)"
work="${1:?Supply an empty directory path on a large Linux build volume}"
if ! command -v elan >/dev/null; then
  echo "Install elan and make it available on PATH before starting the replay." >&2
  exit 1
fi
mkdir -p "$work"
work="$(cd "$work" && pwd -P)"
if [[ -n "$(ls -A "$work")" ]]; then
  echo "Choose an empty directory for this independent replay." >&2
  exit 1
fi
exec > >(tee "$work/replay.log") 2>&1
export ELAN_HOME="$work/elan"
export XDG_CACHE_HOME="$work/cache"

# Integrity pins. Preparing and building execute archive code (tactics,
# macros and `run_cmd` blocks run as the user), and everything that later
# judges the result -- the lean, leanchecker and lake binaries and the
# shared libraries they load, Audit.lean and parts.sha256 -- sits in trees
# that code could in principle rewrite. So the checker binaries and shared
# libraries are pinned to the official release digests, the audit inputs
# are hashed before any archive code runs, and both are re-verified
# immediately before the audit and fresh-kernel stages. The library pins
# matter as much as the binary ones: bin/lean, bin/leanchecker and bin/lake
# are small launchers, and the kernel itself lives in
# lib/lean/libleanshared.so, which they load from the same tree.
#
# The digests are those of bin/lean, bin/leanchecker, bin/lake and the
# lib/lean shared libraries in the official per-architecture Linux
# releases (https://github.com/leanprover/lean4/releases/tag/v4.33.1):
# lean-4.33.1-linux.tar.zst (Linux x86_64), whose own SHA-256 is
# 890afd185370f85666025b883914ab4f4b339136f8c96167b69cfb62aecaf235, and
# lean-4.33.1-linux_aarch64.tar.zst (Linux AArch64), whose own SHA-256 is
# f7353a8b2a8741c84558523e450556f9a1c45e3cafcf54399ce68c6a24c55f07.
# Lean ships separate toolchains per architecture, so the pins are
# selected by the machine architecture below; a native AArch64 setup
# (for example the Docker instructions on Apple Silicon) gets the
# AArch64 pins, and an architecture with no pins is rejected here, at
# the top of the script, before the archive is downloaded. elan does
# not checksum the toolchain it downloads, so the installed binaries
# and shared libraries are checked against these pins instead.
select_toolchain_pins() {
  case "$1" in
    x86_64)
      lean_bin_sha256="e8baaa71855a616dc351028f3ad2200051b0671f423a1696a100e809302d5550"
      leanchecker_bin_sha256="84bc3e7cd6efa5bfecd1e7c37571fed9428fb2b3e16da0ddf62f2163573486d0"
      lake_bin_sha256="60330ab6f07dce20f3fa9ebb08e8b984ea9549eac172afeb15d9d2227060e2b3"
      libleanshared_sha256="23d636cac1cadcba37beac30eff9b9b20d66cee726cf5defb4ae39f1284a3e5a"
      liblake_shared_sha256="b1d164bf248f35b9e9664e696f4cd05a79a7066065510fb9b459139c78beb354"
      libleanshared_1_sha256="2f96493ac9046f64ec7bcb78102a4c1e7cd9a7042487f246af45eda8a96667f5"
      libleanshared_2_sha256="2f96493ac9046f64ec7bcb78102a4c1e7cd9a7042487f246af45eda8a96667f5"
      libinit_shared_sha256="2f96493ac9046f64ec7bcb78102a4c1e7cd9a7042487f246af45eda8a96667f5"
      ;;
    aarch64|arm64)
      lean_bin_sha256="79fb1d26fa5a39385d59fdc48a711a14b0710ca6480271acce99b4d177cea085"
      leanchecker_bin_sha256="ff0394605f58d1a1625c786083eb03c7cf7e7401b7c1b557e6724cc15543826e"
      lake_bin_sha256="8229bc302b0b7d0e7679e28cc2b830c5aec995ffe5714833503360252f1490ba"
      libleanshared_sha256="51a52d106dad81cbcb434f993c488c91662b73516200705cc329970eaa4d9586"
      liblake_shared_sha256="1c93a49f0cce8f8f45303f6cbae8720841a473cad65a21fe87578edc2edeab57"
      libleanshared_1_sha256="73506424a15e5c7eef1e90142857314e37fdd6ba07dda72dd12245c75ca1d029"
      libleanshared_2_sha256="73506424a15e5c7eef1e90142857314e37fdd6ba07dda72dd12245c75ca1d029"
      libinit_shared_sha256="73506424a15e5c7eef1e90142857314e37fdd6ba07dda72dd12245c75ca1d029"
      ;;
    *)
      echo "REPLAY_INTEGRITY_CHECK_FAILED: no pinned Lean 4.33.1 digests for Linux architecture '$1' (supported: x86_64, aarch64)" >&2
      return 1
      ;;
  esac
}
select_toolchain_pins "$(uname -m)" || exit 1

integrity_failed() {
  echo "REPLAY_INTEGRITY_CHECK_FAILED: $1" >&2
  exit 1
}

digest() { sha256sum "$1" | cut -d' ' -f1; }

# Recompute every recorded digest: the pinned toolchain binaries and shared
# libraries plus Audit.lean and parts.sha256 as they were when the archive
# was verified.
verify_integrity() {
  local toolchain_bin="$ELAN_HOME/toolchains/leanprover--lean4---v4.33.1/bin"
  local toolchain_lib="$ELAN_HOME/toolchains/leanprover--lean4---v4.33.1/lib/lean"
  [[ "$(digest "$toolchain_bin/lean")" == "$lean_bin_sha256" ]] ||
    integrity_failed "bin/lean does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_bin/leanchecker")" == "$leanchecker_bin_sha256" ]] ||
    integrity_failed "bin/leanchecker does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_bin/lake")" == "$lake_bin_sha256" ]] ||
    integrity_failed "bin/lake does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_lib/libleanshared.so")" == "$libleanshared_sha256" ]] ||
    integrity_failed "lib/lean/libleanshared.so does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_lib/libLake_shared.so")" == "$liblake_shared_sha256" ]] ||
    integrity_failed "lib/lean/libLake_shared.so does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_lib/libleanshared_1.so")" == "$libleanshared_1_sha256" ]] ||
    integrity_failed "lib/lean/libleanshared_1.so does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_lib/libleanshared_2.so")" == "$libleanshared_2_sha256" ]] ||
    integrity_failed "lib/lean/libleanshared_2.so does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$toolchain_lib/libInit_shared.so")" == "$libinit_shared_sha256" ]] ||
    integrity_failed "lib/lean/libInit_shared.so does not match the pinned Lean 4.33.1 release digest"
  [[ "$(digest "$record/Audit.lean")" == "$audit_digest" ]] ||
    integrity_failed "Audit.lean changed after the archive was verified"
  [[ "$(digest "$record/parts.sha256")" == "$parts_digest" ]] ||
    integrity_failed "parts.sha256 changed after the archive was verified"
  [[ "$(digest "$record/audit/dependency-artifacts.py")" == "$artifact_checker_digest" ]] ||
    integrity_failed "dependency-artifacts.py changed after the archive was verified"
  echo REPLAY_INTEGRITY_CHECK_PASSED
}

# Every dependency Lake resolved must sit at the revision lake-manifest.json
# records, with no tracked file modified. The manifest itself is pinned by
# the digest recorded from the SHA-verified extraction before Lake ran, so
# a manifest rewritten afterwards is rejected before any revision it names
# is trusted. The clean check covers tracked
# files only (--untracked-files=no): Lake and the Mathlib cache write build
# outputs inside these trees, and those are untracked or ignored. The
# project tree itself is pinned by parts.sha256 over the archive it was
# extracted from; if it ever ships as a git checkout, its tracked files
# must be clean too.
verify_pristine_dependencies() {
  local packages_dir name rev dir base status checked=0 seen=" "
  [[ "$(digest lake-manifest.json)" == "$manifest_digest" ]] ||
    integrity_failed "lake-manifest.json changed after the archive was verified"
  packages_dir="$(sed -n 's/.*"packagesDir": "\([^"]*\)".*/\1/p' lake-manifest.json | head -n 1)"
  packages_dir="${packages_dir:-.lake/packages}"
  [[ -d "$packages_dir" ]] ||
    integrity_failed "dependency directory $packages_dir is missing"
  if [[ -d .git ]]; then
    status="$(git status --porcelain --untracked-files=no)" ||
      integrity_failed "git status failed in the project tree"
    [[ -z "$status" ]] ||
      integrity_failed "the project tree has modified tracked files"
  fi
  # The manifest is machine-generated: within the packages array each entry
  # lists "rev" before "name". The root project's own trailing "name" lies
  # outside the array and is not a dependency.
  while read -r name rev; do
    dir="$packages_dir/$name"
    [[ -d "$dir/.git" ]] ||
      integrity_failed "dependency $name is missing or is not a git checkout"
    [[ "$(git -C "$dir" rev-parse HEAD)" == "$rev" ]] ||
      integrity_failed "dependency $name is not at the revision lake-manifest.json records ($rev)"
    status="$(git -C "$dir" status --porcelain --untracked-files=no)" ||
      integrity_failed "git status failed for dependency $name"
    [[ -z "$status" ]] ||
      integrity_failed "dependency $name has modified tracked files"
    checked=$((checked + 1))
    seen+="$name "
  done < <(awk '
    /"packages": \[/ { in_packages = 1 }
    in_packages && /"rev":/ { rev = $0; sub(/.*: "/, "", rev); sub(/".*/, "", rev) }
    in_packages && /"name":/ { name = $0; sub(/.*: "/, "", name); sub(/".*/, "", name); print name, rev }
    in_packages && /^[[:space:]]*\],/ { in_packages = 0 }
  ' lake-manifest.json)
  [[ "$checked" -gt 0 ]] ||
    integrity_failed "no dependencies parsed from lake-manifest.json"
  for dir in "$packages_dir"/*/; do
    base="$(basename "$dir")"
    [[ "$seen" == *" $base "* ]] ||
      integrity_failed "unexpected directory $base in $packages_dir"
  done
  echo REPLAY_DEPENDENCIES_PRISTINE_PASSED
}

verify_dependency_artifacts() {
  python3 -I "$record/audit/dependency-artifacts.py" verify "$work/dependency-artifacts.json" \
    --sha256 "$dependency_artifacts_digest"
}

mkdir "$work/parts"
cd "$work/parts"
while read -r hash part; do
  curl --fail --location --retry 4 --output "$part" "https://alpo.ge/lean.tar.gz.$part"
done < "$record/parts.sha256"
sha256sum -c "$record/parts.sha256"
audit_digest="$(digest "$record/Audit.lean")"
parts_digest="$(digest "$record/parts.sha256")"
artifact_checker_digest="$(digest "$record/audit/dependency-artifacts.py")"
cat part* | tar -xz --no-same-owner -C "$work"
cd "$work/lean"
manifest_digest="$(digest lake-manifest.json)"
toolchain="$(cat lean-toolchain)"
test "$toolchain" = "leanprover/lean4:v4.33.1"
elan toolchain install "$toolchain"
elan run "$toolchain" lean --version
verify_integrity
lake_cmd=(elan run "$toolchain" lake)
"${lake_cmd[@]}" exe cache get
test "$(git -C .lake/packages/mathlib rev-parse HEAD)" = "0df444a360eaa60ab8c11dca51a86af692955474"
verify_pristine_dependencies
# Record the prepared cache before compiling project proof modules. Git's clean
# check deliberately ignores these files; a fresh kernel accepts valid alternate
# definitions just as it accepts the intended ones. Keep the snapshot's digest in
# this parent shell, and freeze the search path used by the final checks.
verify_integrity
toolchain_root="$ELAN_HOME/toolchains/leanprover--lean4---v4.33.1"
toolchain_bin="$toolchain_root/bin"
prepared_lean_path="$("${lake_cmd[@]}" env printenv LEAN_PATH)"
verify_integrity
python3 -I "$record/audit/dependency-artifacts.py" snapshot \
  --lean-path "$prepared_lean_path" --stdlib "$toolchain_root/lib/lean" \
  --project-output "$work/lean/.lake/build/lib/lean" "$work/dependency-artifacts.json"
dependency_artifacts_digest="$(digest "$work/dependency-artifacts.json")"
readonly prepared_lean_path dependency_artifacts_digest artifact_checker_digest
if [[ "${2:-}" == "--prepare-only" ]]; then
  verify_integrity
  verify_pristine_dependencies
  verify_dependency_artifacts
  echo "Prepared project in $work/lean"
  exit 0
fi
"${lake_cmd[@]}" build +Main:olean
echo REPLAY_SOURCE_BUILD_PASSED
verify_integrity
verify_pristine_dependencies
verify_dependency_artifacts
# Avoid re-evaluating a project lakefile to choose the final import environment.
# leanchecker invokes `lean` to find its sysroot, hence the pinned bin path too.
export LEAN_PATH="$prepared_lean_path" PATH="$toolchain_bin:$PATH"
"$toolchain_bin/lean" "$record/Audit.lean"
verify_integrity
verify_dependency_artifacts
echo REPLAY_AXIOM_AUDIT_PASSED
"$toolchain_bin/leanchecker" --fresh Main
verify_integrity
verify_dependency_artifacts
echo REPLAY_FRESH_KERNEL_PASSED
# Optional: compare Main's statements with Mathlib-only expected propositions (audit/README.md).
if [[ "${REPLAY_STATEMENT_CHECK:-0}" == 1 ]]; then
  bash "$record/audit/statement-check.sh" main "$work/statement-check"
  verify_integrity
  verify_dependency_artifacts
  echo REPLAY_STATEMENT_CHECK_PASSED
fi
