import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import replay_evidence as evidence
import tasks
import worker_build


class KernelReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / 'project'
        self.out = self.root / 'receipt'
        self.out.mkdir()
        (self.out / 'receipt.json').write_text('{}\n')
        self.rows = [dict(id=f'M{i:06}', module=f'Fixture.Module{i}') for i in range(3)]
        self.receipt = dict(objects={})
        self.objects = []
        for row in self.rows:
            path = self.project / '.lake/build/lib/lean' / (row['module'].replace('.', '/') + '.olean')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(row['module'])
            self.objects.append(path)
            self.receipt['objects'][row['id']] = tasks.digest(path)

    def run_record(self, rows=None):
        rows = self.rows if rows is None else rows
        return dict(command=evidence.replay_command(rows), exit_code=0,
                    transcript=''.join('replaying ' + row['module'] + '\n' for row in rows))

    def parallel_record(self):
        return dict(receipt_sha256=tasks.digest(self.out / 'receipt.json'),
                    replays=[self.run_record(self.rows[:2]), self.run_record(self.rows[2:])])

    def test_legacy_and_parallel_records(self):
        legacy = dict(self.run_record(), receipt_sha256=tasks.digest(self.out / 'receipt.json'))
        evidence.check_replay(legacy, self.out, self.rows)
        evidence.check_replay(self.parallel_record(), self.out, self.rows)

    def test_parallel_record_is_bound_to_receipt(self):
        record = self.parallel_record()
        (self.out / 'receipt.json').write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, 'different receipt'):
            evidence.check_replay(record, self.out, self.rows)

    def test_batch_cannot_borrow_a_replay_line_from_another_batch(self):
        record = self.parallel_record()
        line = 'replaying Fixture.Module0\n'
        record['replays'][0]['transcript'] = record['replays'][0]['transcript'].replace(line, '')
        record['replays'][1]['transcript'] += line
        with self.assertRaisesRegex(ValueError, 'Missing kernel replay line'):
            evidence.check_replay(record, self.out, self.rows)

    def test_missing_duplicate_extra_and_reordered_targets_are_rejected(self):
        for rows in [self.rows[:2], self.rows + self.rows[:1],
                     self.rows + [dict(id='M999999', module='Fixture.Other')], self.rows[::-1]]:
            with self.subTest(rows=rows):
                record = dict(receipt_sha256=tasks.digest(self.out / 'receipt.json'),
                              replays=[self.run_record(rows)])
                with self.assertRaisesRegex(ValueError, 'coverage mismatch'):
                    evidence.check_replay(record, self.out, self.rows)

    def test_failed_or_boolean_exit_status_is_rejected(self):
        for code in [1, -9, False]:
            record = self.parallel_record()
            record['replays'][1]['exit_code'] = code
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'exit code'):
                evidence.check_replay(record, self.out, self.rows)

    def test_unexpected_flags_or_empty_batches_are_rejected(self):
        for command in [['lake', 'env', 'leanchecker', '--fresh', 'Fixture.Module0'],
                        evidence.replay_command([])]:
            record = self.parallel_record()
            record['replays'][0]['command'] = command
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, 'command'):
                evidence.check_replay(record, self.out, self.rows)

    def test_malformed_or_ambiguous_batch_records_are_rejected(self):
        for batches in [[], {}, [None]]:
            record = self.parallel_record()
            record['replays'] = batches
            with self.subTest(batches=batches), self.assertRaises(ValueError):
                evidence.check_replay(record, self.out, self.rows)
        record = self.parallel_record()
        record.update(self.run_record())
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            evidence.check_replay(record, self.out, self.rows)

    @staticmethod
    def success(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, ''.join('replaying ' + n + '\n' for n in command[4:]))

    def test_default_writer_preserves_legacy_record(self):
        with patch.object(subprocess, 'run', side_effect=self.success) as run:
            worker_build.record_cached_replay(self.project, self.out, self.rows, self.receipt, [], {})
        self.assertEqual(run.call_count, 1)
        record = json.loads((self.out / evidence.REPLAY_FILE).read_text())
        self.assertNotIn('replays', record)
        evidence.check_replay(record, self.out, self.rows)

    def test_parallel_writer_really_overlaps_processes(self):
        barrier = threading.Barrier(2)
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            self.assertEqual(kwargs['env']['LEAN_NUM_THREADS'], '1')
            barrier.wait(timeout=5)
            return self.success(command, **kwargs)

        with patch.object(subprocess, 'run', side_effect=run):
            worker_build.record_cached_replay(self.project, self.out, self.rows, self.receipt,
                                              [], {'REPLAY_KERNEL_JOBS': '2'})
        self.assertEqual(len(seen), 2)
        record = json.loads((self.out / evidence.REPLAY_FILE).read_text())
        self.assertNotIn('command', record)
        evidence.check_replay(record, self.out, self.rows)

    def test_invalid_job_count_never_launches_a_checker(self):
        for jobs in ['0', '-1', '1.5', '', 'many']:
            with self.subTest(jobs=jobs), patch.object(subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'positive integer'):
                    worker_build.record_cached_replay(self.project, self.out, self.rows, self.receipt,
                                                      [], {'REPLAY_KERNEL_JOBS': jobs})
                run.assert_not_called()

    def test_failed_batch_cannot_write_success_evidence(self):
        def run(command, **kwargs):
            if 'Fixture.Module2' in command:
                return subprocess.CompletedProcess(command, 1, 'kernel rejected declaration')
            return self.success(command, **kwargs)
        with patch.object(subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'kernel rejected'):
                worker_build.record_cached_replay(self.project, self.out, self.rows, self.receipt,
                                                  [], {'REPLAY_KERNEL_JOBS': '2'})
        self.assertFalse((self.out / evidence.REPLAY_FILE).exists())

    def test_object_tampering_cannot_write_success_evidence(self):
        def run(command, **kwargs):
            self.objects[0].write_text('changed object')
            return self.success(command, **kwargs)
        with patch.object(subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'Cached object changed'):
                worker_build.record_cached_replay(self.project, self.out, self.rows, self.receipt,
                                                  [], {'REPLAY_KERNEL_JOBS': '2'})
        self.assertFalse((self.out / evidence.REPLAY_FILE).exists())


if __name__ == '__main__':
    unittest.main()
