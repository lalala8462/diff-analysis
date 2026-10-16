import subprocess
import tempfile
import unittest
from pathlib import Path

from diff_analysis.gitlab import GitLabError, parse_mr_url
from diff_analysis.review import review_repo, is_api_path, stitch_cross_project
from diff_analysis.report import markdown
from diff_analysis.scanner import build_graph, scan_tree


class URLTests(unittest.TestCase):
    def test_valid_url(self):
        self.assertEqual(
            parse_mr_url('https://gitlab.example.cn/team/pay/-/merge_requests/15', 'gitlab.example.cn'),
            ('https://gitlab.example.cn', 'team/pay', 15))

    def test_reject_untrusted_urls(self):
        for u in ('http://gitlab.example.cn/x/-/merge_requests/1',
                  'https://evil.example.cn/x/-/merge_requests/1',
                  'https://gitlab.example.cn/../foo/-/merge_requests/1'):
            with self.assertRaises(GitLabError):
                parse_mr_url(u, 'gitlab.example.cn')


class ASTTests(unittest.TestCase):
    def test_same_class_overloaded_methods(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src'
            src.mkdir()
            (src / 'Pay.java').write_text(
                'package demo; class Pay { void a(){ b(); } void b(){} void b(String x){} }')
            methods, calls, errors = scan_tree(src, Path(d) / 'cache')
            self.assertFalse(errors)
            reverse, unresolved = build_graph(methods, calls)
            self.assertFalse(unresolved)
            self.assertIn('demo.Pay#a()', reverse['demo.Pay#b()'])
            self.assertIn('demo.Pay#b(String)', methods)

    def test_git_diff_identifies_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / 'repo'
            repo.mkdir()
            def git(*argv):
                return subprocess.check_output(
                    ['git', '-C', str(repo), *argv], stderr=subprocess.STDOUT).decode().strip()
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            src = repo / 'src'
            src.mkdir()
            path = src / 'Pay.java'
            path.write_text('package demo; class Pay {void caller(){action();} int action(){return 1;}}')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            path.write_text('package demo; class Pay {void caller(){action();} int action(){return 2;}}')
            git('add', '.')
            git('commit', '-qm', 'head')
            head = git('rev-parse', 'HEAD')
            review = review_repo(repo, 'demo', base, head, Path(d)/'work', Path(d)/'cache')
            self.assertIn('demo.Pay#action()', review['changed_methods'])
            self.assertIn('demo.Pay#caller()', review['impact_paths'])
            self.assertIn('静态调用链', markdown([review]))

    def test_api_detection(self):
        self.assertTrue(is_api_path('src/main/java/com/foo/api/PayApi.java'))
        self.assertFalse(is_api_path('src/main/java/com/foo/service/PayService.java'))

    def test_cross_project_is_candidate_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'target').mkdir()
            (root/'consumer').mkdir()
            (root/'target'/'Target.java').write_text(
                'package x; class Target {void pay() {}}')
            (root/'consumer'/'Caller.java').write_text(
                'package y; class Caller {void x(){Target.pay();}}')
            cache = root/'cache'
            a, calls_a, e1 = scan_tree(root/'target', cache)
            b, calls_b, e2 = scan_tree(root/'consumer', cache)
            self.assertFalse(e1 + e2)
            _, unknown = build_graph(b, calls_b)
            target = {'project':'target','changed_methods':list(a),'methods':a,
                      'old_methods':{},'unresolved':[]}
            consumer = {'project':'consumer','changed_methods':[],'methods':b,
                        'old_methods':{},'unresolved':unknown}
            result = stitch_cross_project([target, consumer])
            self.assertEqual(1, len(result))
            self.assertEqual('CANDIDATE', result[0]['status'])


if __name__ == '__main__':
    unittest.main()
