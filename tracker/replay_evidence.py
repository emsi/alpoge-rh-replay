"""Verify a replay receipt and optionally test installed objects on the receiver."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

import tasks


def require(ok, message):
    if not ok:
        raise ValueError(message)


def pins():
    summary = json.loads((tasks.base / 'summary.json').read_text())
    return dict(lean=summary['lean'], commit=summary['lean_commit'],
                mathlib=summary['mathlib_commit'],
                inputs_manifest_sha256=tasks.digest(tasks.base.parent / 'parts.sha256'))


def identity(version, mathlib, manifest):
    expected = pins()
    match = re.fullmatch(r'Lean \(version ([^,]+), ([\w.-]+), commit ([0-9a-f]{40}), Release\)', version)
    require(match and match[1] == expected['lean'] and match[3] == expected['commit'],
            'Unexpected Lean version or revision')
    require((mathlib, manifest) == (expected['mathlib'], expected['inputs_manifest_sha256']),
            'Unexpected Mathlib revision or input manifest')
    return dict(**expected, platform=match[2])


def checkout_info(project):
    expected = pins()
    toolchain = 'leanprover/lean4:v' + expected['lean']
    require((project / 'lean-toolchain').read_text().strip() == toolchain, 'Wrong lean-toolchain')
    env = dict(os.environ, ELAN_HOME=str(project.parent / 'elan'),
               XDG_CACHE_HOME=str(project.parent / 'cache'))
    launcher = ['elan', 'run', toolchain]
    version = subprocess.check_output([*launcher, 'lean', '--version'], cwd=project, env=env, text=True).strip()
    revision = subprocess.check_output(['git', '-C', project / '.lake/packages/mathlib', 'rev-parse', 'HEAD'],
                                      text=True).strip()
    return identity(version, revision, expected['inputs_manifest_sha256']), launcher, env


REPLAY_FILE = 'kernel-replay.json'


def missing_targets(directory, targets):
    names = {r['module'] for r in targets}
    log = (directory / 'build.log').read_text(errors='replace')
    built = set(tasks.rebuilt_imports(log, names, set()))
    return [r for r in targets if r['module'] not in built]


def replay_command(missing):
    return ['lake', 'env', 'leanchecker', '--verbose', *[r['module'] for r in missing]]


def check_replay(record, directory, missing):
    require(record['receipt_sha256'] == tasks.digest(directory / 'receipt.json'),
            'Kernel replay belongs to a different receipt')
    if 'replays' in record:
        require(not any(k in record for k in ('command', 'exit_code', 'transcript')),
                'Ambiguous kernel replay record')
        runs = record['replays']
        require(isinstance(runs, list) and 0 < len(runs) <= len(missing),
                'Invalid kernel replay batches')
    else:
        runs = [record]
    expected = [r['module'] for r in missing]
    covered = []
    prefix = replay_command([])
    for run in runs:
        require(isinstance(run, dict), 'Invalid kernel replay batch')
        command = run.get('command')
        require(isinstance(command, list) and command[:len(prefix)] == prefix
                and len(command) > len(prefix) and all(isinstance(n, str) for n in command),
                'Invalid kernel replay command')
        names = command[len(prefix):]
        require(type(run.get('exit_code')) is int and run['exit_code'] == 0,
                'Invalid kernel replay exit code')
        require(isinstance(run.get('transcript'), str), 'Invalid kernel replay transcript')
        lines = set(run['transcript'].splitlines())
        for name in names:
            require('replaying ' + name in lines, 'Missing kernel replay line: ' + name)
        covered.extend(names)
    # Contiguous batches retain the requested order. This also rejects omitted,
    # duplicated, extra, or substituted modules, even if other logs name them.
    require(covered == expected, 'Kernel replay target coverage mismatch')


def verify(directory, targets, project_rows, checkout=None):
    """Hash members in place. Bundle verification never extracts files."""
    receipt = json.loads((directory / 'receipt.json').read_text())
    ids = [r['id'] for r in targets]
    names = [r['module'] for r in targets]
    platform = identity(receipt['toolchain'], receipt['mathlib'], receipt['inputs_manifest_sha256'])
    require(type(receipt['exit_code']) is int and receipt['exit_code'] == 0, 'Unsuccessful build')
    require(receipt['tasks'] == ids and set(receipt['objects']) == set(ids), 'Receipt task mismatch')
    require(receipt['command'] == ['lake', 'build', *['+' + n + ':olean' for n in names]],
            'Receipt command mismatch')
    log = directory / 'build.log'
    require(tasks.digest(log) == receipt['log_sha256'], 'Transcript digest mismatch')
    transcript = log.read_text(errors='replace')
    require(re.search(r'^(?:Build completed successfully|All targets up-to-date) \(\d+ jobs?\)\.\s*\Z', transcript, re.M),
            'Missing successful Lake summary')
    built = set(tasks.rebuilt_imports(transcript, project_rows.keys(), set()))
    rebuilt = sorted(built.difference(names))
    if 'rebuilt_project_modules' in receipt:
        require(receipt['rebuilt_project_modules'] == rebuilt, 'Rebuilt import list mismatch')
    objects = {'lib/lean/' + n.replace('.', '/') + '.olean': receipt['objects'][task]
               for n, task in zip(names, ids)}
    require(all(isinstance(v, str) and re.fullmatch(r'[a-f0-9]{64}', v) for v in objects.values()),
            'Invalid object digest')
    bundle = directory / 'targets.tar.gz'
    if bundle.exists() or 'bundle_sha256' in receipt:
        require(tasks.digest(bundle) == receipt.get('bundle_sha256'), 'Bundle digest mismatch')
        stems = {prefix + n.replace('.', '/') for n in names for prefix in ('ir/', 'lib/lean/')}
        seen = set()
        with tarfile.open(bundle, 'r|gz') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                stem = str(path.parent / path.name.split('.')[0])
                require(member.isreg() and str(path) == member.name and stem in stems
                        and '.' in path.name and member.name not in seen, 'Unexpected bundle member: ' + member.name)
                seen.add(member.name)
                if member.name in objects:
                    h = hashlib.sha256()
                    with archive.extractfile(member) as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            h.update(block)
                    require(h.hexdigest() == objects[member.name], 'Bundled object mismatch: ' + member.name)
        require(objects.keys() <= seen, 'Missing bundled object')
    else:
        require(checkout is not None, 'An unbundled receipt requires the prepared checkout')
    if checkout is not None:
        for name in built.union(names):
            row = project_rows[name]
            require(tasks.digest(checkout / 'src' / (name.replace('.', '/') + '.lean')) == row['source_sha256'],
                    'Source mismatch: ' + name)
        for path, digest in objects.items():
            require(tasks.digest(checkout / '.lake/build' / path) == digest, 'Installed object mismatch: ' + path)
    missing = [r for r in targets if r['module'] not in built]
    if missing:
        path = directory / REPLAY_FILE
        require(path.is_file(), 'Missing per-target kernel replay for cached targets')
        check_replay(json.loads(path.read_text()), directory, missing)
    return dict(identity=platform, tasks=ids, kernel_replayed_targets=[r['module'] for r in missing], producer_lake_seconds=receipt.get('elapsed_seconds'), built_targets=sorted(built.intersection(names)),
                rebuilt_project_modules=rebuilt)


def inventory(project, rows):
    host, _, _ = checkout_info(project)
    root = project / '.lake/build/lib/lean'
    by_name = {r['module']: r['id'] for r in rows}
    cached = {by_name[name] for p in root.rglob('*.olean') if p.is_file()
              and (name := '.'.join(p.relative_to(root).with_suffix('').parts)) in by_name}
    return dict(identity=host, modules=sorted(cached))


def read_inventory(path):
    data = json.loads(path.read_text())
    require(all(data['identity'].get(k) == v for k, v in pins().items())
            and isinstance(data['identity'].get('platform'), str), 'Inventory pin mismatch')
    require(isinstance(data['modules'], list) and all(isinstance(i, str) and re.fullmatch(r'M\d{6}', i)
            for i in data['modules']), 'Invalid module inventory')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    inv = commands.add_parser('inventory')
    inv.add_argument('checkout', type=Path)
    for name in ('verify', 'reuse-check'):
        sub = commands.add_parser(name)
        sub.add_argument('target')
        sub.add_argument('directory', type=Path)
        sub.add_argument('--checkout', type=Path, required=name == 'reuse-check')
        if name == 'reuse-check':
            sub.add_argument('--before', type=Path, help='inventory captured on this receiver before transfer')
    args = parser.parse_args()
    project = args.checkout.resolve() if args.checkout else None
    if args.action == 'inventory':
        result = inventory(project, tasks.module_rows())
    else:
        rows = {r['module']: r for r in tasks.module_rows()}
        targets = tasks.select(rows.values(), args.target)
        result = verify(args.directory, targets, rows, project)
        if args.action == 'reuse-check':
            host, launcher, env = checkout_info(project)
            require(result['identity'] == host, 'Receiver and bundle platform/toolchain differ')
            before = read_inventory(args.before) if args.before else None
            if before:
                require(before['identity'] == host, 'Before-inventory platform/toolchain mismatch')
            cmd = [*launcher, 'lake', '--no-build', '--no-cache', 'build',
                   *['+' + r['module'] + ':olean' for r in targets]]
            subprocess.run(cmd, cwd=project, env=env, check=True, stdout=sys.stderr)
            result['reuse_checked'] = result['tasks']
            if before:
                result['new_to_receiver'] = sorted(set(result['tasks']).difference(before['modules']))
                elapsed = result['producer_lake_seconds']
                if isinstance(elapsed, (int, float)) and elapsed > 0:
                    result['new_targets_per_producer_lake_hour'] = 3600 * len(result['new_to_receiver']) / elapsed
    print(json.dumps(result))


if __name__ == '__main__':
    main()
