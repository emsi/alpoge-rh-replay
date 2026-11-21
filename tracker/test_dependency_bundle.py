import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import dependency_bundle as bundle
import replay_evidence as evidence
import tasks


class DependencyBundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'lean'
        self.tracker = self.root / 'tracker'
        self.tracker.mkdir()
        self.summary = json.loads((tasks.base / 'summary.json').read_text())
        shutil.copyfile(tasks.base.parent / 'parts.sha256', self.root / 'parts.sha256')
        (self.tracker / 'summary.json').write_text(json.dumps(self.summary))
        self.rows = []
        for i in range(4):
            source = self.project / f'src/Fixture/Mod{i}.lean'
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text((f'import Fixture.Mod{i-1}\n' if 0 < i < 3 else '')
                              + f'theorem fixture{i} : True := True.intro\n')
            self.rows.append(dict(id=f'M{i+1:06}', packet='P0001', module=f'Fixture.Mod{i}',
                                  imports=[f'M{i:06}'] if 0 < i < 3 else [],
                                  source_sha256=tasks.digest(source)))
        with gzip.open(self.tracker / 'modules.jsonl.gz', 'wt') as stream:
            for row in self.rows:
                print(json.dumps(row), file=stream)
        change = patch.object(tasks, 'base', self.tracker)
        change.start()
        self.addCleanup(change.stop)
        self.receipt = self.root / 'receipt'
        self.output = self.root / 'export'
        self.target = 'M000003'

    def fake_producer(self):
        for row in self.rows:
            stem = row['module'].replace('.', '/')
            for prefix, suffix in [('lib/lean', '.olean'), ('lib/lean', '.olean.private'),
                                   ('lib/lean', '.trace'), ('lib/lean', '.trace.nobuild'),
                                   ('ir', '.setup.json')]:
                path = self.project / '.lake/build' / prefix / (stem + suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(row['module'] + suffix)
        (self.project / '.lake/packages').mkdir()
        for name in bundle.CONFIG:
            (self.project / name).write_text('{}\n')
        self.receipt.mkdir()
        log = self.receipt / 'build.log'
        log.write_text('✔ [1/1] Built Fixture.Mod2 (1s)\nBuild completed successfully (1 job).\n')
        obj = self.project / '.lake/build/lib/lean/Fixture/Mod2.olean'
        artifact = self.receipt / 'targets.tar.gz'
        with tarfile.open(artifact, 'w:gz') as archive:
            archive.add(obj, arcname='lib/lean/Fixture/Mod2.olean')
        record = dict(tasks=[self.target], objects={self.target: tasks.digest(obj)},
                      command=['lake', 'build', '+Fixture.Mod2:olean'], exit_code=0,
                      log_sha256=tasks.digest(log), bundle_sha256=tasks.digest(artifact),
                      toolchain=f"Lean (version 4.33.1, fixture, commit {self.summary['lean_commit']}, Release)",
                      mathlib=self.summary['mathlib_commit'],
                      inputs_manifest_sha256=tasks.digest(self.root / 'parts.sha256'))
        (self.receipt / 'receipt.json').write_text(json.dumps(record))

    def export_fake(self, lake=None):
        def successful(command, **kwargs):
            self.assertEqual(command, ['lake', '--no-build', '--no-cache', 'build', '+Fixture.Mod2:olean'])
            kwargs['stdout'].write(b'All targets up-to-date (3 jobs).\n')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(evidence, 'checkout_info', return_value=(dict(**evidence.pins(), platform='fixture'), [], {})), \
                patch.object(subprocess, 'run', side_effect=lake or successful):
            return bundle.export(self.target, self.receipt, self.project, self.output, self.rows, 2)

    def edit_manifest(self, change):
        path = self.output / 'dependency-manifest.json'
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
        result = self.output / 'transfer-result.json'
        record = json.loads(result.read_text())
        record['manifest_sha256'] = tasks.digest(path)
        result.write_text(json.dumps(record))

    def test_portable_complete_closure_omits_unrelated_modules_and_compiler_inputs(self):
        self.fake_producer()
        self.assertEqual(self.export_fake()['project_modules'], 3)
        manifest = json.loads((self.output / 'dependency-manifest.json').read_text())
        files = {name for shard in manifest['shards'] for name in shard['artifacts']}
        self.assertEqual(len(files), 9)
        self.assertFalse(any('Mod3' in name or name.endswith(('.setup.json', '.trace.nobuild')) for name in files))
        shutil.rmtree(self.project)
        shutil.rmtree(self.receipt)
        shutil.rmtree(self.output / 'receiver')
        self.assertEqual(bundle.verify(self.target, self.output, self.rows)['project_modules'], 3)

    def test_missing_dependency_object_is_rejected(self):
        self.fake_producer()
        (self.project / '.lake/build/lib/lean/Fixture/Mod0.olean').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing project object'):
            self.export_fake()
        self.assertFalse((self.output / 'transfer-result.json').exists())

    def test_snapshot_rejects_source_change_and_symlinked_parents(self):
        for mode in ['source', 'symlink']:
            with self.subTest(mode=mode):
                self.fake_producer()
                if mode == 'source':
                    (self.project / 'src/Fixture/Mod0.lean').write_text('changed')
                else:
                    parent = self.project / '.lake/build/lib/lean/Fixture'
                    saved = parent.with_name('Saved')
                    parent.rename(saved)
                    parent.symlink_to(saved, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'Source digest mismatch|Symlink in snapshot'):
                    self.export_fake()
                shutil.rmtree(self.output)
                shutil.rmtree(self.project)
                shutil.rmtree(self.receipt)
                # Restore the indexed sources for the next independent case.
                for i, row in enumerate(self.rows):
                    p = self.project / 'src' / (row['module'].replace('.', '/') + '.lean')
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text((f'import Fixture.Mod{i-1}\n' if 0 < i < 3 else '')
                                 + f'theorem fixture{i} : True := True.intro\n')

    def test_receiver_failure_preserves_log_without_success_receipt(self):
        self.fake_producer()
        def stale(command, **kwargs):
            kwargs['stdout'].write(b'Target is out of date\n')
            return subprocess.CompletedProcess(command, 3)
        with self.assertRaisesRegex(ValueError, 'Receiver reuse failed'):
            self.export_fake(stale)
        self.assertIn('out of date', (self.output / 'receiver-no-build.log').read_text())
        self.assertFalse((self.output / 'transfer-result.json').exists())

    def test_receiver_mutation_is_rejected_after_no_build_success(self):
        self.fake_producer()
        def changed(command, **kwargs):
            (kwargs['cwd'] / '.lake/build/lib/lean/Fixture/Mod0.olean').write_text('changed')
            kwargs['stdout'].write(b'All targets up-to-date (3 jobs).\n')
            return subprocess.CompletedProcess(command, 0)
        with self.assertRaisesRegex(ValueError, 'Snapshot changed'):
            self.export_fake(changed)
        self.assertFalse((self.output / 'transfer-result.json').exists())

    def test_offline_verifier_rejects_incomplete_closure(self):
        self.fake_producer()
        self.export_fake()
        def remove(data):
            data['shards'] = [s for s in data['shards'] if all(r['id'] != 'M000002' for r in s['modules'])]
        self.edit_manifest(remove)
        with self.assertRaisesRegex(ValueError, 'Incomplete project dependency closure'):
            bundle.verify(self.target, self.output, self.rows)

    def test_artifact_addition_during_check_is_rejected(self):
        self.fake_producer()
        def changed(command, **kwargs):
            (self.project / '.lake/build/lib/lean/Fixture/Mod0.ir').write_text('new artifact')
            kwargs['stdout'].write(b'All targets up-to-date (3 jobs).\n')
            return subprocess.CompletedProcess(command, 0)
        with self.assertRaisesRegex(ValueError, 'Artifact set changed'):
            self.export_fake(changed)
        self.assertFalse((self.output / 'transfer-result.json').exists())

    def test_archive_path_and_member_digest_tampering_are_rejected(self):
        self.fake_producer()
        self.export_fake()
        original = (self.output / 'dependency-manifest.json').read_text()
        for mode in ['traversal', 'digest', 'duplicate', 'symlink']:
            with self.subTest(mode=mode):
                self.edit_manifest(lambda d: d.update(json.loads(original)))
                manifest = json.loads(original)
                shard = manifest['shards'][0]
                path = self.output / shard['archive']
                with tarfile.open(path, 'w:gz') as archive:
                    for name, digest in shard['artifacts'].items():
                        content = (self.output / 'receiver/.lake/build' / name).read_bytes()
                        if mode == 'digest':
                            content += b'changed'
                        member = tarfile.TarInfo('../outside' if mode == 'traversal' else name)
                        member.size = len(content)
                        if mode == 'symlink':
                            member.type = tarfile.SYMTYPE
                            member.linkname = '/outside'
                            member.size = 0
                        archive.addfile(member, io.BytesIO(content))
                        if mode == 'duplicate':
                            archive.addfile(member, io.BytesIO(content))
                def update(data):
                    data['shards'][0].update(sha256=tasks.digest(path), bytes=path.stat().st_size)
                self.edit_manifest(update)
                with self.assertRaisesRegex(ValueError, 'Unexpected archive member|Archive member digest mismatch'):
                    bundle.verify(self.target, self.output, self.rows)

    def test_unknown_import_and_existing_output_are_rejected(self):
        bad = [dict(self.rows[0], imports=['M999999'])]
        with self.assertRaisesRegex(ValueError, 'Unknown project dependency'):
            bundle.closure(bad, bad)
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'Output directory already exists'):
            bundle.export(self.target, self.receipt, self.project, self.output, self.rows, 1)

    @unittest.skipUnless(os.environ.get('REPLAY_NATIVE_TEST'), 'requires the pinned native Lean toolchain')
    def test_native_transitive_closure_transfer_and_downstream_build(self):
        elan_home = Path(os.environ.get('ELAN_HOME', Path.home() / '.elan')).resolve()
        (self.root / 'elan').symlink_to(elan_home, target_is_directory=True)
        (self.project / 'lean-toolchain').write_text('leanprover/lean4:v4.33.1\n')
        (self.project / 'lakefile.toml').write_text('name = "fixture"\n[[lean_lib]]\nname = "Fixture"\nsrcDir = "src"\n')
        dependency = self.project / '.lake/packages/mathlib'
        dependency.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', dependency], check=True)
        subprocess.run(['git', '-C', dependency, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture', '--allow-empty'], check=True)
        self.summary['mathlib_commit'] = subprocess.check_output(['git', '-C', dependency, 'rev-parse', 'HEAD'], text=True).strip()
        (self.tracker / 'summary.json').write_text(json.dumps(self.summary))
        self.assertEqual(tasks.run([self.rows[2]], self.project, self.receipt, True,
                                   project_modules={r['module'] for r in self.rows}), 0)
        result = bundle.export(self.target, self.receipt, self.project, self.output, self.rows, 3)
        self.assertEqual(result['project_modules'], 3)
        receiver = self.output / 'receiver'
        # Reconstruct from the actual published archive bytes, not from the
        # temporary files that the exporter used to create those archives.
        shutil.rmtree(receiver / '.lake/build')
        manifest = json.loads((self.output / 'dependency-manifest.json').read_text())
        bundle.verify(self.target, self.output, self.rows)
        for shard in manifest['shards']:
            with tarfile.open(self.output / shard['archive']) as archive:
                archive.extractall(receiver / '.lake/build', filter='data')
        _, launcher, env = evidence.checkout_info(self.project)
        result = subprocess.run([*launcher, *bundle.reuse_command([self.rows[2]])], cwd=receiver,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (receiver / 'src/Fixture/After.lean').write_text('import Fixture.Mod2\ntheorem after : True := fixture2\n')
        result = subprocess.run([*launcher, 'lake', '--no-cache', 'build', '+Fixture.After:olean'],
                                cwd=receiver, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tasks.rebuilt_imports(result.stdout + result.stderr,
                                             {r['module'] for r in self.rows}, set()), [])
        self.assertEqual([p.name for p in (receiver / '.lake/build').rglob('*.setup.json')], ['After.setup.json'])


if __name__ == '__main__':
    unittest.main()
