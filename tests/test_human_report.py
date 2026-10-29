import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.human_report import _diff_rows, _file_evolution, _group_sdk_alerts, _noise_changes, write_html
from diff_analysis.maven_inventory import analyze, inventory
from diff_analysis.release_analysis import collect_commits, final_diff_ledger
from tests.test_release_analysis import setup, commit, git


class HumanReportTests(unittest.TestCase):
    def setUp(self):
        mock = patch('diff_analysis.maven_inventory.default_projects', return_value=([], []))
        mock.start()
        self.addCleanup(mock.stop)

    def test_all_commits_and_title_prefix(self):
        ids = ['%040x' % i for i in range(1, 303)]
        def fake(repo, *args):
            if args[0] == 'merge-base': return b''
            if args[0] == 'rev-list': return ('\n'.join(ids)).encode()
            return ('PAY-123 hello PAY-999\0body OTHER-3\0').encode()
        with patch('diff_analysis.release_analysis.git', side_effect=fake):
            result = collect_commits('.', 'a' * 40, 'b' * 40)
        self.assertEqual(302, result['total'])
        self.assertEqual(302, len(result['commits']))
        self.assertEqual(['PAY-123'], result['commits'][-1]['issue_keys'])

    def test_diff_alignment_and_html_escaping(self):
        rows, changed = _diff_rows(['one', '<old>', 'end'], ['one', '<new>', 'extra', 'end'])
        self.assertEqual([1, 2], changed)
        self.assertIn('&lt;', rows[1][1])
        self.assertEqual('', rows[2][0])
        self.assertEqual('end', rows[-1][1])

    def test_sdk_alerts_group_by_sdk_and_consumer_without_losing_evidence(self):
        sdk = {'project': 'local/cps', 'module': 'api/pom.xml', 'group': 'g',
               'artifact': 'api', 'base_sha': 'a', 'head_sha': 'b'}
        alerts = [
            {'sdk': sdk, 'consumer': 'local/webapi', 'path': 'app/pom.xml', 'consumer_context': {'base': {}, 'head': {}}, 'reason': '旧版'},
            {'sdk': sdk, 'consumer': 'local/webapi', 'path': 'A.java', 'change_ids': ['D-A'], 'imports': ['p.A']},
            {'sdk': sdk, 'consumer': 'local/webapi', 'path': 'B.java', 'change_ids': ['D-B'], 'imports': ['p.B']},
            {'sdk': sdk, 'consumer': 'local/adpts', 'path': 'app/pom.xml', 'consumer_context': {'base': {}, 'head': {}}, 'reason': '未知'},
            {'sdk': {**sdk, 'artifact': 'other'}, 'consumer': 'local/webapi', 'path': 'C.java', 'change_ids': ['D-C']},
            {'kind': '同类多工单', 'path': 'D.java'},
        ]
        groups = _group_sdk_alerts(alerts)
        self.assertEqual(4, len(groups))
        self.assertEqual(1, len(groups[0]['declarations']))
        self.assertEqual(['D-A', 'D-B'], [r['change_ids'][0] for r in groups[0]['references']])
        self.assertEqual('local/adpts', groups[1]['consumer'])
        self.assertEqual('other', groups[2]['sdk']['artifact'])
        self.assertEqual('同类多工单', groups[3]['kind'])

    def test_multiticket_classes_grouped_by_project_with_evidence(self):
        def change(project, path, num, issue):
            return {'id': 'D-' + str(num), 'project': project, 'path': path,
                    'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
                    'line_commit_candidates': ['commit'], 'line_issue_candidates': [issue],
                    'before': [], 'after': [], 'old_range': {'start': 0, 'count': 0},
                    'new_range': {'start': 0, 'count': 0}}
        def ledger(project, paths):
            changes = [change(project, path, i * 2 + n, 'PAY-' + str(n + 1))
                       for i, path in enumerate(paths) for n in range(2)]
            return {'project': project, 'base': 'a' * 40, 'head': 'b' * 40,
                    'final_changes': changes, 'final_files': [],
                    'history': {'status': 'COMPLETE', 'total': 0, 'commits': []}}
        ledgers = [ledger('local/one', ['A.java', 'B.java']), ledger('local/two', ['C.java'])]
        for index, item in enumerate(ledgers[1]['final_changes']):
            item['file_status'] = 'A'
            item['line_commit_candidates'] = ['c' * 39 + str(index)]
        ledgers[1]['history']['commits'] = [
            {'sha': 'c' * 39 + '0', 'subject': 'PAY-1 新增文件', 'issue_keys': ['PAY-1']},
            {'sha': 'c' * 39 + '1', 'subject': 'PAY-2 后续调整', 'issue_keys': ['PAY-2']}]
        sdk = {'alerts': [], 'projects': [], 'gaps': [], 'sdks': []}
        with tempfile.TemporaryDirectory() as tmp, patch('diff_analysis.human_report.analyze', return_value=sdk):
            result = write_html(Path(tmp), {'ledgers': ledgers}, [], [])
            output = (Path(tmp) / 'report.html').read_text(encoding='utf-8')
        self.assertEqual(2, result['human_priority_count'])
        self.assertEqual(1, output.count('项目中的多工单交叉：local/one'))
        self.assertIn('2 个类的改动关联多个工单', output)
        self.assertIn('<summary>A.java', output)
        self.assertIn('<summary>B.java', output)
        self.assertIn('data-evidence="D-0"', output)
        self.assertEqual(1, output.count('项目中的多工单交叉：local/two'))
        self.assertIn('旧版不存在此文件；新版文件中有来自多个提交的保留行', output)
        self.assertIn('PAY-1 新增文件', output)
        self.assertIn('PAY-2 后续调整', output)

    def test_added_file_evolution_shows_later_change_in_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / 'repo')
            base = commit(repo, 'README.md', 'before\n', 'base')
            added = commit(repo, 'A.java', 'class A {\n int x = 1;\n}\n', 'PAY-1 新增')
            head = commit(repo, 'A.java', 'class A {\n int x = 2;\n}\n', 'PAY-2 调整')
            ledger = final_diff_ledger(repo, 'local/repo', base, head)
            result = _file_evolution(repo, ledger, 'A.java', ledger['final_changes'])
            self.assertEqual('COMPLETE', result['status'])
            self.assertTrue(result['linear'])
            self.assertEqual([added, head], [event['sha'] for event in result['events']])
            self.assertEqual(['新增文件', '修改文件'], [event['operation'] for event in result['events']])
            self.assertIn('- int x = 1;', result['events'][1]['patch'])
            self.assertIn('+ int x = 2;', result['events'][1]['patch'])
            self.assertEqual('UNKNOWN', _file_evolution(None, ledger, 'A.java', ledger['final_changes'])['status'])

    def test_noise_filter_preserves_semantic_and_literal_changes(self):
        def check(old, new):
            change = {'id': 'D-test', 'path': 'A.java', 'old_range': {'start': 1, 'count': 1},
                      'new_range': {'start': 1, 'count': 1}}
            ledger = {'project': 'p', 'base': 'base', 'head': 'head',
                      'final_files': [{'path': 'A.java', 'status': 'M'}], 'final_changes': [change]}
            with patch('diff_analysis.human_report._show_lines', side_effect=[([old], None), ([new], None)]):
                return _noise_changes([ledger], {'p': 'repo'})
        self.assertIn('D-test', check('import a.B;', 'import a.C;'))
        self.assertIn('D-test', check('int x=1;', '  int x = 1;'))
        self.assertNotIn('D-test', check('int x=1;', 'int x=2;'))
        self.assertNotIn('D-test', check('String s="a b";', 'String s="ab";'))
        self.assertNotIn('D-test', check('// a b', '// ab'))
        self.assertNotIn('D-test', check('double n=1.0;', 'double n=1 . 0;'))
        self.assertNotIn('D-test', check('int value=0b10;', 'int value=0 b10;'))

    def test_sdk_consumer_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = setup(root / 'sdk')
            consumer = setup(root / 'consumer')
            pom1 = '<project><groupId>test</groupId><artifactId>pay-api</artifactId><version>1</version></project>'
            base = commit(sdk, 'pom.xml', pom1, 'base')
            commit(sdk, 'src/main/java/p/Dto.java', 'package p;\npublic class Dto {}\n', 'PAY-1 add')
            head = commit(sdk, 'pom.xml', pom1.replace('>1<', '>2<'), 'PAY-1 version')
            cpom = '<project><groupId>test</groupId><artifactId>consumer</artifactId><version>1</version><dependencies><dependency><groupId>test</groupId><artifactId>pay-api</artifactId><version>${sdk.version}</version></dependency></dependencies><properties><sdk.version>1</sdk.version></properties></project>'
            cbase = commit(consumer, 'pom.xml', cpom, 'base')
            chead = commit(consumer, 'src/main/java/C.java', 'import p.Dto;\nclass C { Dto x; }\n', 'PAY-2 change')
            specs = [(sdk, 'local/sdk', base, head), (consumer, 'local/consumer', cbase, chead)]
            ledgers = [final_diff_ledger(*x) for x in specs]
            result = analyze(specs, ledgers)
            self.assertTrue(any(x['kind'] == 'SDK消费者' and '旧版本' in x['reason'] for x in result['alerts']))
            self.assertFalse(any(x['kind'] == '变化类引用SDK' for x in result['alerts']))
            report = root / 'out'
            report.mkdir()
            write_html(report, {'ledgers': ledgers}, specs, [])
            output = (report / 'report.html').read_text(encoding='utf-8')
            self.assertIn('可能的问题', output)
            self.assertIn('PAY-1', output)
            self.assertIn('data-distance', output)
            self.assertIn('&lt;project&gt;', output)
            change_id = ledgers[0]['final_changes'][0]['id']
            self.assertIn('data-evidence="' + change_id + '"', output)
            self.assertIn('<template id="evidence-' + change_id + '">', output)
            self.assertIn('id="evidence-panel"', output)
            self.assertIn('查看消费者依赖声明（旧版和新版）', output)
            self.assertIn('SDK 项目本次比较新版', output)
            self.assertIn('不代表生产或最新发布版本', output)
            self.assertEqual(1, output.count('SDK 与消费者项目：local/consumer'))
            self.assertNotIn('相关变更类和引用（1 处）', output)
            self.assertIn('src/main/java/C.java', output)
            upgraded = commit(consumer, 'pom.xml', cpom.replace('<sdk.version>1</sdk.version>', '<sdk.version>2</sdk.version>'), 'PAY-2 upgrade')
            upgraded_specs = [(sdk, 'local/sdk', base, head), (consumer, 'local/consumer', cbase, upgraded)]
            upgraded_ledgers = [final_diff_ledger(*x) for x in upgraded_specs]
            upgraded_result = analyze(upgraded_specs, upgraded_ledgers)
            self.assertFalse(any(x['kind'] == 'SDK消费者' for x in upgraded_result['alerts']))
            sdk_head = commit(sdk, 'src/main/java/p/Dto.java', 'package p;\npublic class Dto { int x; }\n', 'PAY-3 change')
            same_version_specs = [(sdk, 'local/sdk', head, sdk_head), (consumer, 'local/consumer', cbase, chead)]
            same_version_ledgers = [final_diff_ledger(*x) for x in same_version_specs]
            same_version_result = analyze(same_version_specs, same_version_ledgers)
            self.assertFalse(any(x['kind'] == 'SDK消费者' for x in same_version_result['alerts']))
            self.assertTrue(any(x['kind'] == 'SDK自身版本' for x in same_version_result['alerts']))
            self.assertFalse(any(x['kind'] == '变化类引用SDK' for x in same_version_result['alerts']))

    def test_table_impact_html_for_unchanged_consumer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            producer = setup(root / 'adpts')
            consumer = setup(root / 'webapi')
            mapper = 'src/main/resources/TransMapper.xml'
            base = commit(producer, mapper,
                          '<mapper><select id="get">SELECT * FROM trans_record WHERE state=0</select></mapper>',
                          'PAY-1 base')
            head = commit(producer, mapper,
                          '<mapper><select id="get">SELECT * FROM trans_record WHERE state=1</select></mapper>',
                          'PAY-1 update')
            consumer_sha = commit(consumer, 'src/dao.sql',
                                  'SELECT * FROM trans_record;', 'consumer')
            registry_file = root / 'projects.json'
            registry_file.write_text(json.dumps({'projects': [
                {'project': 'rhf/webapi', 'repo': str(consumer),
                 'revision': consumer_sha}]}), encoding='utf-8')
            specs = [(producer, 'rhf/adpts', base, head)]
            ledgers = [final_diff_ledger(*specs[0])]
            report = root / 'report'
            report.mkdir()
            outcome = write_html(report, {'ledgers': ledgers}, specs, [],
                                 registry_file)
            page = (report / 'report.html').read_text('utf-8')
            self.assertEqual(1, len(outcome['database']['impacts']))
            self.assertIn('跨项目数据库表：trans_record', page)
            self.assertIn('rhf/webapi', page)
            self.assertIn(ledgers[0]['final_changes'][0]['id'], page)
            self.assertIn('TABLE_NAME_ONLY_UNCONFIRMED', page)

    def test_local_parent_and_fixed_consumer_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = setup(root / 'consumer')
            parent = '<project><groupId>test</groupId><artifactId>parent</artifactId><version>1</version><properties><api.version>2</api.version></properties><dependencyManagement><dependencies><dependency><groupId>test</groupId><artifactId>pay-api</artifactId><version>${api.version}</version></dependency></dependencies></dependencyManagement></project>'
            commit(repo, 'pom.xml', parent, 'base')
            child = '<project><parent><groupId>test</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>child</artifactId><dependencies><dependency><groupId>test</groupId><artifactId>pay-api</artifactId></dependency></dependencies></project>'
            head = commit(repo, 'child/pom.xml', child, 'PAY-2 child')
            child_model = next(m for m in inventory(repo, head)['modules'] if m['path'] == 'child/pom.xml')
            self.assertEqual('2', child_model['dependencies'][0]['version'])
            registry_file = root / 'projects.json'
            registry_file.write_text(json.dumps({'projects': [{'project': 'local/consumer', 'repo': str(repo), 'revision': head}]}), encoding='utf-8')
            result = analyze([], [], registry_file)
            self.assertEqual('登记固定版本', result['projects'][0]['source'])
            self.assertIsNone(result['projects'][0]['base'])

    def test_nonancestor_keeps_no_commit_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup(Path(tmp) / 'repo')
            base = commit(repo, 'A.java', 'class A {}', 'base')
            first = commit(repo, 'A.java', 'class A { int a; }', 'PAY-1 first')
            ledger = final_diff_ledger(repo, 'local/repo', first, base)
            self.assertEqual('BASE_NOT_ANCESTOR', ledger['history']['status'])
            self.assertTrue(ledger['final_changes'])
            self.assertEqual([], ledger['final_changes'][0]['commit_candidates'])


if __name__ == '__main__':
    unittest.main()
