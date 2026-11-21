#!/usr/bin/env python3
"""Native regressions using only Lean 4.33.1; no RH archive or Mathlib is needed.

Usage: python3 tests/independent-audit-native.py /path/to/lean-4.33.1/bin
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lean_bin", type=Path)
    args = parser.parse_args()
    lean_bin = args.lean_bin.absolute()
    lean = lean_bin / "lean"
    auditor = Path(__file__).resolve().parents[1] / "audit/IndependentAudit.lean"
    with tempfile.TemporaryDirectory(prefix="independent-audit-regression-") as tmp:
        root = Path(tmp)
        env = dict(os.environ, PATH=f"{lean_bin}:{os.environ['PATH']}", LEAN_PATH=str(root))

        def run(name, command, success=True, contains=None):
            result = subprocess.run(list(map(str, command)), cwd=root, env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if (result.returncode == 0) != success or (contains and contains not in result.stdout):
                raise RuntimeError(f"{name}: exit {result.returncode}\n{result.stdout}")
            print(f"PASS {name}", flush=True)

        def compile_module(module, source):
            path = root / (module + ".lean")
            path.write_text(source)
            run("compile " + module, [lean, "-R", root, "-o", root / (module + ".olean"), path])

        def audit(name, module, target, expected, success=True, contains=None):
            run(name, [lean, "--run", auditor, module, "Expected", target, expected], success, contains)

        run("pinned compiler", [lean, "--version"], contains="version 4.33.1")
        compile_module("Expected", "def expectedTrue : Prop := True\n"
                       "def expectedFalse : Prop := False\ndef expectedEquality : Prop := True = True\n")
        compile_module("Good", "theorem probe : True := True.intro\n")
        audit("genuine theorem", "Good", "probe", "expectedTrue", contains="INDEPENDENT_AUDIT_PASSED")
        audit("wrong statement", "Good", "probe", "expectedFalse", False, "Type comparison failed")
        audit("missing target", "Good", "notPresent", "expectedTrue", False, "Target is missing")
        compile_module("Allowed", "theorem probe : True = True := propext Iff.rfl\n")
        audit("whitelisted axiom", "Allowed", "probe", "expectedEquality", contains="RAW_AXIOMS #[propext]")

        # This frontend extension suppresses an audit written as `run_cmd` after
        # importing Bad. The runtime data reader must still reject the axiom.
        compile_module("Bad", "import Lean.Elab.Command\naxiom bad : False\n"
                       "theorem probe : False := bad\n@[command_elab Lean.runCmd]\n"
                       "def suppressAudit : Lean.Elab.Command.CommandElab := fun _ => pure ()\n")
        frontend = root / "FrontendAudit.lean"
        frontend.write_text("import Bad\nimport Lean.Util.CollectAxioms\n"
                            "open Lean in\nrun_cmd do\n"
                            "  for ax in (← collectAxioms `probe) do\n"
                            "    unless #[`propext, `Classical.choice, `Quot.sound].contains ax do\n"
                            "      logError m!\"Unexpected axiom {ax}\"\n")
        run("imported elaborator bypasses frontend guard", [lean, frontend])
        audit("override cannot suppress data audit", "Bad", "probe", "expectedFalse",
              False, "Unexpected axiom bad")

        compile_module("PrivateBad", "private axiom hiddenBad : False\n"
                       "theorem probe : False := hiddenBad\n")
        audit("private proof dependency", "PrivateBad", "probe", "expectedFalse",
              False, "Unexpected axiom _private.")
        compile_module("OpaqueBad", "axiom bad : False\nopaque hidden : False := bad\n"
                       "theorem probe : False := hidden\n")
        audit("opaque proof dependency", "OpaqueBad", "probe", "expectedFalse",
              False, "Unexpected axiom bad")
        compile_module("AxiomTarget", "axiom probe : True\n")
        audit("axiom is not a theorem", "AxiomTarget", "probe", "expectedTrue",
              False, "Target is missing or is not a theorem")

        compile_module("Initializers", "import Lean.Elab.Command\n"
                       'initialize IO.FS.writeFile "initializer-ran" "executed"\n'
                       "theorem probe : True := True.intro\n")
        marker = root / "initializer-ran"
        if marker.exists():
            marker.unlink()
        frontend.write_text("import Initializers\n")
        run("frontend import executes initializer", [lean, frontend])
        if not marker.exists():
            raise RuntimeError("initializer positive control failed")
        marker.unlink()
        audit("data import does not execute initializer", "Initializers", "probe", "expectedTrue",
              contains="INDEPENDENT_AUDIT_PASSED")
        if marker.exists():
            raise RuntimeError("data import executed an imported initializer")
    print("All independent-auditor regressions passed", flush=True)


if __name__ == "__main__":
    main()
