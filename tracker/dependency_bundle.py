#!/usr/bin/env python3
"""Export a completed target's project dependencies and check isolated Lake reuse."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import replay_evidence as evidence
import tasks

require = evidence.require
CONFIG = ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain')
RECEIPT_FILES = ('receipt.json', 'build.log', 'targets.tar.gz', evidence.REPLAY_FILE)


def write(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def closure(rows, targets):
    by_id = {row['id']: row for row in rows}
    found, pending = set(), [row['id'] for row in targets]
    while pending:
        key = pending.pop()
        require(key in by_id, 'Unknown project dependency: ' + key)
        if key not in found:
            found.add(key)
            pending.extend(by_id[key]['imports'])
    return [by_id[key] for key in sorted(found)]


def regular(path, root):
    """Do not follow artifact symlinks, including symlinked parent directories."""
    for parent in [path, *path.parents]:
        require(not parent.is_symlink(), 'Symlink in snapshot path: ' + str(parent))
        if parent == root:
            break
    else:
        raise ValueError('Snapshot path is outside its root: ' + str(path))
    require(path.is_file(), 'Missing regular file: ' + str(path))


def copy_checked(source, destination, root, expected=None):
    regular(source, root)
    before = tasks.digest(source)
    require(expected is None or before == expected, 'Source digest mismatch: ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    regular(source, root)
    require(tasks.digest(destination) == before == tasks.digest(source),
            'File changed during snapshot: ' + str(source))
    return before


def check_archive(path, expected):
    seen = set()
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            require(member.isreg() and member.name in expected and member.name not in seen,
                    'Unexpected archive member: ' + member.name)
            seen.add(member.name)
            digest = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            require(digest.hexdigest() == expected[member.name],
                    'Archive member digest mismatch: ' + member.name)
    require(seen == expected.keys(), 'Missing archive member')


def snapshot_shard(index, rows, project, receiver, output):
    artifacts, modules = {}, []
    for row in rows:
        stem = row['module'].replace('.', '/')
        source = Path('src') / (stem + '.lean')
        copy_checked(project / source, receiver / source, project, row['source_sha256'])
        for prefix in ('lib/lean', 'ir'):
            base = project / '.lake/build' / prefix / stem
            for path in sorted(base.parent.glob(base.name + '.*')):
                if path.name.endswith(('.setup.json', '.trace.nobuild')):
                    continue
                relative = path.relative_to(project / '.lake/build').as_posix()
                artifacts[relative] = copy_checked(path, receiver / '.lake/build' / relative, project)
        obj = 'lib/lean/' + stem + '.olean'
        require(obj in artifacts, 'Missing project object: ' + obj)
        modules.append(dict(id=row['id'], module=row['module'], source_sha256=row['source_sha256'],
                            object_sha256=artifacts[obj]))
    name = f'dependencies-{index:03}.tar.gz'
    path = output / name
    with tarfile.open(path, 'w:gz', compresslevel=1) as archive:
        for relative in sorted(artifacts):
            archive.add(receiver / '.lake/build' / relative, arcname=relative, recursive=False)
    check_archive(path, artifacts)
    return dict(archive=name, sha256=tasks.digest(path), bytes=path.stat().st_size,
                modules=modules, artifacts=artifacts)


def reuse_command(targets):
    return ['lake', '--no-build', '--no-cache', 'build',
            *['+' + row['module'] + ':olean' for row in targets]]


def artifact_names(root, rows):
    names = set()
    for row in rows:
        for prefix in ('lib/lean', 'ir'):
            base = root / '.lake/build' / prefix / row['module'].replace('.', '/')
            for path in base.parent.glob(base.name + '.*'):
                if not path.name.endswith(('.setup.json', '.trace.nobuild')):
                    regular(path, root)
                    names.add(path.relative_to(root / '.lake/build').as_posix())
    return names


def verify(target, directory, rows):
    """Offline verification reads archive members without extracting them."""
    targets = tasks.select(rows, target)
    expected = {row['id']: row for row in closure(rows, targets)}
    manifest = json.loads((directory / 'dependency-manifest.json').read_text())
    require(type(manifest['format']) is int and manifest['format'] == 1 and manifest['target'] == target,
            'Dependency bundle identity mismatch')
    require(manifest['configuration'].keys() == set(CONFIG)
            and all(isinstance(d, str) and re.fullmatch(r'[a-f0-9]{64}', d)
                    for d in manifest['configuration'].values()), 'Invalid configuration digests')
    require(set(manifest['receipt_files']) <= set(RECEIPT_FILES)
            and {'receipt.json', 'build.log', 'targets.tar.gz'} <= manifest['receipt_files'].keys(),
            'Missing or unexpected receipt assets')
    for name, digest in manifest['receipt_files'].items():
        require(tasks.digest(directory / 'receipt' / name) == digest, 'Receipt asset digest mismatch: ' + name)
    verified = evidence.verify(directory / 'receipt', targets, {r['module']: r for r in rows})
    receipt = json.loads((directory / 'receipt/receipt.json').read_text())
    require(manifest['identity'] == verified['identity'], 'Dependency bundle platform/toolchain differs')
    modules, files, names = {}, set(), set()
    for shard in manifest['shards']:
        name = shard['archive']
        require(isinstance(name, str) and re.fullmatch(r'dependencies-\d{3}\.tar\.gz', name)
                and name not in names, 'Invalid or duplicate archive name')
        names.add(name)
        require(shard['modules'], 'Empty dependency shard')
        stems = set()
        for row in shard['modules']:
            key = row['id']
            require(key in expected and key not in modules, 'Unexpected or duplicate closure module: ' + key)
            indexed = expected[key]
            require(row['module'] == indexed['module'] and row['source_sha256'] == indexed['source_sha256'],
                    'Closure module differs from index: ' + key)
            modules[key] = row
            stem = row['module'].replace('.', '/')
            stems.update(prefix + stem for prefix in ('lib/lean/', 'ir/'))
            obj = 'lib/lean/' + stem + '.olean'
            require(shard['artifacts'].get(obj) == row['object_sha256'], 'Closure object digest mismatch')
            if key in receipt['objects']:
                require(row['object_sha256'] == receipt['objects'][key], 'Requested object differs from receipt')
        for relative, digest in shard['artifacts'].items():
            parent, _, name = relative.rpartition('/')
            stem = parent + '/' + name.split('.')[0]
            require(stem in stems and '.' in name and relative not in files
                    and not relative.endswith(('.setup.json', '.trace.nobuild')),
                    'Unexpected artifact path: ' + relative)
            require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest), 'Invalid artifact digest')
            files.add(relative)
        archive = directory / shard['archive']
        require(tasks.digest(archive) == shard['sha256'] and archive.stat().st_size == shard['bytes'],
                'Archive digest or size mismatch')
        check_archive(archive, shard['artifacts'])
    require(modules.keys() == expected.keys(), 'Incomplete project dependency closure')
    result = json.loads((directory / 'transfer-result.json').read_text())
    require(result['manifest_sha256'] == tasks.digest(directory / 'dependency-manifest.json'),
            'Transfer result belongs to a different manifest')
    require(result['receiver_command'] == reuse_command(targets)
            and type(result['receiver_exit_code']) is int and result['receiver_exit_code'] == 0,
            'Invalid receiver command or exit code')
    log = directory / 'receiver-no-build.log'
    require(tasks.digest(log) == result['receiver_log_sha256'], 'Receiver log digest mismatch')
    transcript = log.read_text(errors='replace')
    require(re.search(r'^All targets up-to-date \(\d+ jobs?\)\.\s*\Z', transcript, re.M)
            and not tasks.rebuilt_imports(transcript, {r['module'] for r in rows}, set()),
            'Missing successful no-build summary or unexpected source compilation')
    return dict(identity=verified['identity'], target=target, project_modules=len(modules),
                archives=len(names), receiver_exit_code=0)


def export(target, receipt_dir, project, output, rows, jobs):
    require(type(jobs) is int and 1 <= jobs <= 999, 'Jobs must be between 1 and 999')
    project, receipt_dir, output = project.resolve(), receipt_dir.resolve(), output.resolve()
    require(not output.exists(), 'Output directory already exists')
    require((receipt_dir / 'targets.tar.gz').is_file(), 'A completed bundled receipt is required')
    targets = tasks.select(rows, target)
    verified = evidence.verify(receipt_dir, targets, {r['module']: r for r in rows}, project)
    identity, launcher, env = evidence.checkout_info(project)
    require(identity == verified['identity'], 'Producer platform/toolchain differs from receipt')
    selected = closure(rows, targets)
    output.mkdir(parents=True)
    receiver = output / 'receiver'
    (receiver / '.lake').mkdir(parents=True)
    configs = {name: copy_checked(project / name, receiver / name, project) for name in CONFIG}
    # Package caches are not exported. This is an isolated project-artifact
    # receiver on the producer host, using the producer's pinned dependencies.
    (receiver / '.lake/packages').symlink_to(project / '.lake/packages', target_is_directory=True)
    receipts = {name: copy_checked(receipt_dir / name, output / 'receipt' / name, receipt_dir)
                for name in RECEIPT_FILES if (receipt_dir / name).exists()}
    count = min(jobs, len(selected))
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(snapshot_shard, i + 1, selected[i::count], project, receiver, output)
                   for i in range(count)]
        shards = [future.result() for future in futures]
    command = reuse_command(targets)
    log = output / 'receiver-no-build.log'
    with log.open('wb') as stream:
        result = subprocess.run([*launcher, *command], cwd=receiver, env=env,
                                stdout=stream, stderr=subprocess.STDOUT)
    require(result.returncode == 0, 'Receiver reuse failed; inspect receiver-no-build.log')
    checks = [(Path(name), digest) for name, digest in configs.items()]
    checks += [(Path('src') / (row['module'].replace('.', '/') + '.lean'), row['source_sha256'])
               for row in selected]
    checks += [(Path('.lake/build') / name, digest) for shard in shards for name, digest in shard['artifacts'].items()]
    def unchanged(item):
        relative, digest = item
        for root in (project, receiver):
            regular(root / relative, root)
            require(tasks.digest(root / relative) == digest, 'Snapshot changed: ' + str(root / relative))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(unchanged, checks))
    expected_names = {name for shard in shards for name in shard['artifacts']}
    for root in (project, receiver):
        require(artifact_names(root, selected) == expected_names,
                'Artifact set changed during snapshot: ' + str(root))
    manifest = dict(format=1, target=target, identity=identity, configuration=configs,
                    receipt_files=receipts, shards=shards)
    write(output / 'dependency-manifest.json', manifest)
    write(output / 'transfer-result.json', dict(
        completed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        manifest_sha256=tasks.digest(output / 'dependency-manifest.json'), receiver_command=command,
        receiver_exit_code=0, receiver_log_sha256=tasks.digest(log),
        scope='Complete project dependency closure accepted in a separate checkout on the same host, '
              'sharing the producer package cache. Source rebuilding and downloads disabled. '
              'This is artifact reuse evidence, not fresh kernel replay or Main verification.'))
    return verify(target, output, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('export')
    create.add_argument('target')
    create.add_argument('receipt', type=Path)
    create.add_argument('checkout', type=Path)
    create.add_argument('output', type=Path)
    create.add_argument('--jobs', type=int, default=min(os.cpu_count() or 1, 32))
    check = commands.add_parser('verify')
    check.add_argument('target')
    check.add_argument('directory', type=Path)
    args = parser.parse_args()
    rows = list(tasks.module_rows())
    if args.action == 'export':
        result = export(args.target, args.receipt, args.checkout, args.output, rows, args.jobs)
    else:
        result = verify(args.target, args.directory, rows)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
