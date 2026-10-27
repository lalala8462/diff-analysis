import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.architecture_sources import default_projects, isolate_specs
from tests.test_wizard import sample_repo, git


class ArchitectureSourcesTests(unittest.TestCase):
    def test_isolated_copy_and_live_default_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            copies = root / 'copies'
            workspace.mkdir()
            (workspace / 'AGENTS.md').write_text(
                '## 模块职责与任务入口\n| `one` | A |\n| `two` | B |\n## 其他\n', encoding='utf-8')
            one, base, head = sample_repo(workspace, 'one')
            two, _, twohead = sample_repo(workspace, 'two')
            branch = git(two, 'branch', '--show-current')
            remote = root / 'two-remote.git'
            subprocess.check_call(['git', 'clone', '--bare', '-q', str(two), str(remote)])
            git(two, 'remote', 'add', 'origin', str(remote))
            (one / 'Pay.java').write_text('uncommitted user change', encoding='utf-8')
            before = git(one, 'status', '--porcelain')
            selected, failures, active = isolate_specs([(one, 'local/one', base, head)], workspace, copies)
            self.assertTrue(active)
            self.assertEqual([], failures)
            self.assertEqual((copies / 'one').resolve(), selected[0][0])
            self.assertEqual(head, git(copies / 'one', 'rev-parse', head))
            self.assertEqual(before, git(one, 'status', '--porcelain'))
            self.assertFalse((copies / 'one' / 'Pay.java').exists())
            projects, gaps = default_projects(selected, workspace, copies)
            self.assertEqual([], gaps)
            self.assertEqual(1, len(projects))
            self.assertEqual('architecture/two', projects[0]['project'])
            self.assertEqual(branch, projects[0]['branch'])
            self.assertEqual(twohead, projects[0]['head'])
            self.assertEqual(before, git(one, 'status', '--porcelain'))
            subprocess.check_call(['git', '--git-dir', str(remote), 'update-ref', 'refs/heads/master-4.0.0', twohead])
            subprocess.check_call(['git', '--git-dir', str(remote), 'symbolic-ref', 'HEAD', 'refs/heads/master-4.0.0'])
            refreshed, gaps = default_projects(selected, workspace, copies)
            self.assertEqual([], gaps)
            self.assertEqual('master-4.0.0', refreshed[0]['branch'])
            self.assertEqual(twohead, refreshed[0]['head'])
            mr_clone = root / 'mr-clone'
            subprocess.check_call(['git', 'clone', '--no-checkout', '-q', str(remote), str(mr_clone)])
            mr_specs, mr_failures, mr_active = isolate_specs(
                [(mr_clone, 'group/two', twohead, twohead)], workspace, copies)
            self.assertTrue(mr_active)
            self.assertEqual([], mr_failures)
            self.assertEqual((copies / 'two').resolve(), mr_specs[0][0])

    def test_existing_unmanaged_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace, copies = root / 'workspace', root / 'copies'
            workspace.mkdir()
            (workspace / 'AGENTS.md').write_text('## 模块职责与任务入口\n| `one` | A |\n', encoding='utf-8')
            one, base, head = sample_repo(workspace, 'one')
            existing = copies / 'one'
            existing.mkdir(parents=True)
            marker = existing / 'user.txt'
            marker.write_text('keep', encoding='utf-8')
            result, failures, active = isolate_specs([(one, 'local/one', base, head)], workspace, copies)
            self.assertTrue(active)
            self.assertEqual([], result)
            self.assertEqual(1, len(failures))
            self.assertEqual('keep', marker.read_text(encoding='utf-8'))
