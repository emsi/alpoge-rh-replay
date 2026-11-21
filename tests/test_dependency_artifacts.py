"""Exercise the shipped CLI against mutable caches and Lean namespace shadowing."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHECKER = Path(__file__).resolve().parents[1] / "audit/dependency-artifacts.py"


class DependencyArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.dep = self.root / "dependency"
        self.stdlib = self.root / "stdlib"
        self.baseline = self.root / "baseline.json"
        for directory in [self.project, self.dep, self.stdlib]:
            directory.mkdir()
        (self.dep / "Canonical").mkdir()
        self.canonical = self.dep / "Canonical/Value.olean"
        self.canonical.write_bytes(b"prepared definition")
        (self.stdlib / "Init.olean").write_bytes(b"prepared Init")
        self.lean_path = f"{self.project}:{self.dep}"
        self.record()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(CHECKER), *map(str, args)],
                              capture_output=True, text=True)

    def record(self):
        result = self.run_cli("snapshot", "--lean-path", self.lean_path,
                              "--stdlib", self.stdlib, "--project-output", self.project,
                              self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sha256 = hashlib.sha256(self.baseline.read_bytes()).hexdigest()

    def check(self, failure=None):
        result = self.run_cli("verify", self.baseline, "--sha256", self.sha256)
        if failure is None:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("REPLAY_DEPENDENCY_ARTIFACTS_PASSED", result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(failure, result.stderr)

    def test_clean_cache_and_new_project_outputs(self):
        (self.project / "Main.olean").write_bytes(b"new project theorem")
        (self.dep / "Canonical/Value.setup.json").write_text("generated Lake inputs")
        (self.dep / "Canonical/Value.trace").write_text("generated Lake trace")
        self.check()

    def test_replaced_ignored_dependency_object(self):
        self.canonical.write_bytes(b"alternate definition")
        self.check("prepared dependency artifacts changed")

    def test_missing_dependency_object(self):
        self.canonical.unlink()
        self.check("prepared dependency artifacts changed")

    def test_new_dependency_module(self):
        (self.dep / "Unprepared.olean").write_bytes(b"new definition")
        self.check("prepared dependency artifacts changed")

    def test_unused_package_can_have_no_build_directory(self):
        self.baseline.unlink()
        unused = self.root / "unused/build/lib/lean"
        self.lean_path += f":{unused}"
        self.record()
        self.check()
        unused.mkdir(parents=True)
        (unused / "Unprepared.olean").write_bytes(b"new definition")
        self.check("prepared dependency artifacts changed")

    def test_all_import_and_runtime_parts_are_protected(self):
        for suffix in [".olean.private", ".olean.server", ".ir", ".ir.sig", ".so"]:
            with self.subTest(suffix=suffix):
                companion = self.dep / ("Canonical/Value" + suffix)
                companion.write_bytes(b"unprepared companion")
                self.check("prepared dependency artifacts changed")
                companion.unlink()

    def test_project_root_directory_shadows_whole_namespace(self):
        # Lean stops at this empty directory, even though the canonical object's
        # relative path is absent here. A first-existing-file check misses it.
        (self.project / "Canonical").mkdir()
        self.check("is shadowed")

    def test_root_olean_shadows_nested_namespace(self):
        (self.project / "Canonical.olean").write_bytes(b"alternate root")
        self.check("is shadowed")

    def test_project_cannot_shadow_builtin_init(self):
        (self.project / "Init.olean").write_bytes(b"alternate Init")
        self.check("is shadowed")

    def test_later_path_does_not_shadow_earlier_dependency(self):
        self.baseline.unlink()
        self.lean_path = f"{self.dep}:{self.project}"
        self.record()
        (self.project / "Canonical.olean").write_bytes(b"not selected by Lean")
        self.check()

    def test_snapshot_rewrite_cannot_bless_replaced_cache(self):
        self.canonical.write_bytes(b"alternate definition")
        baseline = json.loads(self.baseline.read_text())
        baseline["dependencies"][str(self.dep)]["artifacts"]["Canonical/Value.olean"] = (
            hashlib.sha256(self.canonical.read_bytes()).hexdigest())
        self.baseline.write_text(json.dumps(baseline))
        self.check("snapshot checksum changed")

    def test_directory_redirection(self):
        relocated = self.root / "relocated"
        self.dep.rename(relocated)
        self.dep.symlink_to(relocated, target_is_directory=True)
        self.check("directory was redirected")

    def test_symlinked_namespace_rejected(self):
        (self.dep / "Extra").symlink_to(self.project, target_is_directory=True)
        self.check("symlink in prepared dependency tree")

    def test_snapshot_fails_for_preexisting_shadow(self):
        self.baseline.unlink()
        (self.project / "Canonical").mkdir()
        result = self.run_cli("snapshot", "--lean-path", self.lean_path,
                              "--stdlib", self.stdlib, "--project-output", self.project,
                              self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is shadowed", result.stderr)
        self.assertFalse(self.baseline.exists())

    def test_snapshot_is_not_overwritten(self):
        result = self.run_cli("snapshot", "--lean-path", self.lean_path,
                              "--stdlib", self.stdlib, "--project-output", self.project,
                              self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hashlib.sha256(self.baseline.read_bytes()).hexdigest(), self.sha256)


if __name__ == "__main__":
    unittest.main()
