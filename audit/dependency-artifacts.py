#!/usr/bin/env python3
"""Bind final Lean imports to the artifacts present after dependency preparation.

This is a checkpoint, not a sandbox or proof that a downloaded cache matches its
source. The caller must retain the snapshot digest outside the build subprocess.
Lean 4.33.1 resolves an entire root namespace at the first search-path entry with
that directory or root .olean (Lean/Util/Path.lean, SearchPath.findWithExt).
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


SUFFIXES = (".olean", ".olean.server", ".olean.private", ".ir", ".ir.sig", ".so")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def absolute(path):
    # Keep lexical paths: resolving a symlink here would hide later redirection.
    return Path(os.path.abspath(path))


def artifacts(root):
    result = {}
    # Lake includes unused packages (for example Cli) whose output directory may
    # not exist. Record an empty inventory; later compiled additions still fail.
    if not root.exists():
        return result
    if not root.is_dir():
        raise ValueError(f"prepared dependency path is not a directory: {root}")
    for directory, dirs, files in os.walk(root):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError(f"symlink in prepared dependency tree: {Path(directory) / name}")
        for name in files:
            if name.endswith(SUFFIXES):
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError(f"symlinked dependency artifact: {path}")
                result[path.relative_to(root).as_posix()] = digest(path)
    return result


def check_providers(snapshot):
    paths = [Path(p) for p in snapshot["search_path"]]
    providers = {}
    for directory, record in snapshot["dependencies"].items():
        root = Path(directory)
        if str(root.resolve()) != record["resolved_root"]:
            raise ValueError(f"prepared dependency directory was redirected: {root}")
        for relative in record["artifacts"]:
            if not relative.endswith(".olean"):
                continue
            namespace = relative.split("/", 1)[0].removesuffix(".olean")
            if namespace not in providers:
                providers[namespace] = next((p for p in paths if (p / namespace).is_dir()
                                            or (p / (namespace + ".olean")).exists()), None)
            provider = providers[namespace]
            if provider != root:
                raise ValueError(f"dependency module {relative} is shadowed: "
                                 f"expected {root}, resolver selected {provider}")


def snapshot(lean_path, stdlib, project_output):
    if not absolute(stdlib).is_dir():
        raise ValueError("prepared standard library directory is missing")
    entries = lean_path.split(os.pathsep)
    if any(not entry for entry in entries):
        raise ValueError("LEAN_PATH must not contain empty entries")
    paths = list(dict.fromkeys(str(absolute(p)) for p in [*entries, stdlib]))
    project = str(absolute(project_output))
    if project not in paths:
        raise ValueError("project output is absent from the prepared LEAN_PATH")
    dependencies = {}
    for entry in paths:
        if entry == project:
            continue
        root = Path(entry)
        dependencies[entry] = {"resolved_root": str(root.resolve()), "artifacts": artifacts(root)}
    if not any(record["artifacts"] for record in dependencies.values()):
        raise ValueError("no prepared dependency artifacts found")
    result = {"version": 1, "search_path": paths, "project_output": project,
              "dependencies": dependencies}
    check_providers(result)
    return result


def verify(record):
    if record["version"] != 1:
        raise ValueError("unsupported dependency artifact snapshot")
    check_providers(record)
    for directory, expected in record["dependencies"].items():
        actual = artifacts(Path(directory))
        before = expected["artifacts"]
        changed = sorted(key for key in before.keys() | actual.keys()
                         if before.get(key) != actual.get(key))
        if changed:
            raise ValueError(f"prepared dependency artifacts changed in {directory}: {changed[:5]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("snapshot")
    prepare.add_argument("--lean-path", required=True)
    prepare.add_argument("--stdlib", required=True)
    prepare.add_argument("--project-output", required=True)
    prepare.add_argument("output", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("snapshot", type=Path)
    check.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            record = snapshot(args.lean_path, args.stdlib, args.project_output)
            with args.output.open("x") as stream:
                json.dump(record, stream, sort_keys=True, indent=2)
                stream.write("\n")
            print("REPLAY_DEPENDENCY_ARTIFACTS_RECORDED")
        else:
            # Verify before interpreting any paths or artifact hashes in the file.
            data = args.snapshot.read_bytes()
            if hashlib.sha256(data).hexdigest() != args.sha256:
                raise ValueError("prepared dependency snapshot checksum changed")
            verify(json.loads(data))
            print("REPLAY_DEPENDENCY_ARTIFACTS_PASSED")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"REPLAY_INTEGRITY_CHECK_FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
