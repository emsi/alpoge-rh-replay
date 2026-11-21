#!/usr/bin/env python3
"""Native Lean 4.33.1 regression; only creates artifacts in a temporary directory.

Run: python3 tests/dependency-artifacts-native.py /path/to/lean-4.33.1/bin
The full fresh-kernel replay is optional because it rechecks all of Init:
  ... /path/to/bin --fresh
"""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lean_bin", type=Path)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    lean_bin = args.lean_bin.absolute()
    checker = Path(__file__).resolve().parents[1] / "audit/dependency-artifacts.py"
    with tempfile.TemporaryDirectory(prefix="dependency-artifact-regression-") as tmp:
        root = Path(tmp)
        dependency = root / "dependency"
        output = root / "output"
        alternate = root / "alternate"
        for directory in (dependency, output, alternate):
            directory.mkdir()
        source = dependency / "Canonical.lean"
        source.write_text("def canonicalValue : Nat := 1\n")
        alternate_source = alternate / "Canonical.lean"
        alternate_source.write_text("def canonicalValue : Nat := 0\n")
        target = root / "Main.lean"
        target.write_text("import Canonical\ntheorem cacheProbe : canonicalValue = 0 := rfl\n")
        env = dict(os.environ, LEAN_PATH=f"{output}:{dependency}",
                   PATH=f"{lean_bin}:{os.environ['PATH']}")

        def run(name, command, success=True, contains=None):
            result = subprocess.run(list(map(str, command)), cwd=root, env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if (result.returncode == 0) != success or (contains and contains not in result.stdout):
                raise RuntimeError(f"{name}: exit {result.returncode}\n{result.stdout}")
            print(f"PASS {name}", flush=True)

        lean = lean_bin / "lean"
        run("pinned compiler version", [lean, "--version"], contains="version 4.33.1")
        run("compile intended dependency", [lean, "-R", dependency, "-o",
                                            dependency / "Canonical.olean", source])
        original = (dependency / "Canonical.olean").read_bytes()
        run("intended definition rejects false statement",
            [lean, "-R", root, "-o", output / "Main.olean", target], success=False)
        baseline = root / "baseline.json"
        run("record prepared dependency artifacts",
            [sys.executable, "-I", checker, "snapshot", "--lean-path", env["LEAN_PATH"],
             "--stdlib", lean_bin.parent / "lib/lean", "--project-output", output, baseline])
        sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
        verify = [sys.executable, "-I", checker, "verify", baseline, "--sha256", sha]
        run("clean native cache passes", verify)
        run("compile alternate cached definition", [lean, "-R", alternate, "-o",
                                                    dependency / "Canonical.olean", alternate_source])
        run("alternate definition accepts changed statement",
            [lean, "-R", root, "-o", output / "Main.olean", target])
        if args.fresh:
            run("fresh kernel accepts internally valid alternate definition",
                [lean_bin / "leanchecker", "--fresh", "Main"])
        assert source.read_text() == "def canonicalValue : Nat := 1\n"
        run("artifact guard rejects alternate cache", verify, success=False,
            contains="prepared dependency artifacts changed")
        shutil.copyfile(dependency / "Canonical.olean", output / "Canonical.olean")
        (dependency / "Canonical.olean").write_bytes(original)
        run("project shadow also accepts changed statement",
            [lean, "-R", root, "-o", output / "Main.olean", target])
        run("artifact guard rejects project shadow", verify, success=False, contains="is shadowed")


if __name__ == "__main__":
    main()
