"""Run inside the isolated Linux builder, including after a controller restart."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import re
import subprocess
from pathlib import Path
import uuid

import replay_evidence as evidence
import tasks


def record_cached_replay(project, out, missing, receipt, launcher, env):
    # Bind the kernel run to the same objects that were hashed and bundled by Lake.
    paths = [(project / '.lake/build/lib/lean' / (r['module'].replace('.', '/') + '.olean'),
              receipt['objects'][r['id']]) for r in missing]

    def check_objects():
        for path, expected in paths:
            evidence.require(tasks.digest(path) == expected, 'Cached object changed: ' + str(path))

    check_objects()
    requested = env.get('REPLAY_KERNEL_JOBS', '1')
    evidence.require(isinstance(requested, str) and re.fullmatch(r'[1-9][0-9]*', requested),
                     'REPLAY_KERNEL_JOBS must be a positive integer')
    jobs = min(int(requested), len(missing))
    width = (len(missing) + jobs - 1) // jobs
    groups = [missing[i:i + width] for i in range(0, len(missing), width)]

    def replay(group):
        cmd = evidence.replay_command(group)
        result = subprocess.run([*launcher, *cmd], env=dict(env, LEAN_NUM_THREADS='1'), cwd=project,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return dict(command=cmd, exit_code=result.returncode, transcript=result.stdout)

    if jobs == 1:
        runs = [replay(missing)]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            runs = list(pool.map(replay, groups))
    for run in runs:
        evidence.require(run['exit_code'] == 0,
                         f"Kernel replay failed ({run['exit_code']}) for {run['command']}: {run['transcript']}")
    check_objects()
    record = dict(receipt_sha256=tasks.digest(out / 'receipt.json'))
    if jobs == 1:
        record.update(runs[0])
    else:
        record['replays'] = runs
    evidence.check_replay(record, out, missing)
    path = out / evidence.REPLAY_FILE
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(record, indent=2) + '\n')
    tmp.replace(path)


def build(target, project, out, retry_failed=False):
    platform, launcher, env = evidence.checkout_info(project)
    # Claim this prepared Lake cache for a single native toolchain.
    stamp = project / '.lake/build/replay-worker-identity.json'
    stamp.parent.mkdir(parents=True, exist_ok=True)
    with (stamp.parent / 'replay-worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if stamp.exists():
            evidence.require(json.loads(stamp.read_text()) == platform, 'Use a separate native build volume')
        else:
            tmp = stamp.with_suffix('.tmp')
            tmp.write_text(json.dumps(platform))
            tmp.replace(stamp)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = out / 'receipt.json'
        if receipt.exists() and retry_failed and json.loads(receipt.read_text())['exit_code'] != 0:
            out.rename(out.with_name(out.name + '.failed-' + uuid.uuid4().hex))
        if not receipt.exists():
            if out.exists():
                # A container shutdown can leave a transcript without a finished receipt.
                out.rename(out.with_name(out.name + '.interrupted-' + uuid.uuid4().hex))
            names = set()
            targets = tasks.select(tasks.module_rows(), target, project_modules=names)
            status = tasks.run(targets, project, out, False, True, project_modules=names)
            if status:
                return status
        record = json.loads(receipt.read_text())
        if record['exit_code'] == 0:
            evidence.require(evidence.identity(record['toolchain'], record['mathlib'],
                             record['inputs_manifest_sha256']) == platform, 'Receipt and builder platform differ')
            names = set(record['rebuilt_project_modules'])
            ids = set(record['tasks'])
            rows = {r['module']: r for r in tasks.module_rows()
                    if r['module'] in names or r['id'] in ids or target in (r['id'], r['packet'])}
            targets = tasks.select(rows.values(), target)
            missing = evidence.missing_targets(out, targets)
            if missing and not (out / evidence.REPLAY_FILE).exists():
                record_cached_replay(project, out, missing, record, launcher, env)
            evidence.verify(out, targets, rows, project)
        return record['exit_code']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    probe = commands.add_parser('identity')
    probe.add_argument('checkout', type=Path)
    run = commands.add_parser('build')
    run.add_argument('target')
    run.add_argument('checkout', type=Path)
    run.add_argument('output', type=Path)
    run.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    if args.action == 'identity':
        host, _, _ = evidence.checkout_info(args.checkout.resolve())
        print(json.dumps(host))
        raise SystemExit(0)
    raise SystemExit(build(args.target, args.checkout.resolve(), args.output.resolve(), args.retry_failed))
