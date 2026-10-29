"""HTML prioritization renders related evidence in one confirmation item."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.human_report import write_html


class DedupHtmlTests(unittest.TestCase):
    def test_coverage_section_follows_all_main_report_sections(self):
        scoped = {'sdks': [], 'alerts': [], 'projects': [],
                  'gaps': [{'reason': 'REGISTRY_GAP', 'detail': '登记范围缺口'}],
                  'shared_resources': {'impacts': [], 'omissions': [{'reason': '测试缺口'}]}}
        with tempfile.TemporaryDirectory() as d, patch(
                'diff_analysis.human_report.analyze') as analyze:
            root = Path(d)
            write_html(root, {'ledgers': []}, [], [], dependency_scope=scoped)
            page = (root / 'report.html').read_text('utf-8')
            analyze.assert_not_called()
        headings = ['<h2>优先确认', '<h2>最终差异</h2>',
                    '<h2>比较范围内的提交</h2>', '<h2>统一资源关联索引</h2>',
                    '<h2>比较范围与覆盖</h2>']
        positions = [page.index(heading) for heading in headings]
        self.assertEqual(sorted(positions), positions)
        self.assertEqual(1, page.count('<h2>比较范围与覆盖</h2>'))
        self.assertNotIn('程序关注的场景', page)
        coverage = page[positions[-1]:page.index('</section>', positions[-1])]
        self.assertIn('登记范围缺口', coverage)
        self.assertIn('共享资源缺口', coverage)
        self.assertLess(positions[-1], page.index('Markdown 证据入口'))

    def test_query_fields_group_by_exact_query_and_keep_raw_candidates(self):
        common = {
            'project': 'reconciliation', 'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
            'path': 'mapper/Reco.xml', 'namespace': 'example.RecoMapper',
            'select_id': 'queryGroupByOrder', 'priority': 'MEDIUM',
            'status': 'BACKEND_DYNAMIC_SORT_CAPABILITY', 'deleted_column': 'allstate',
            'input_influence': {'level': 'MEDIUM', 'reason': '来源未明'},
            'deleted_projection': {'expression': 'group_concat(state)'},
            'old': {'sql_excerpt': 'SELECT group_concat(state)'},
            'new': {'sql_excerpt': 'SELECT max(score)'}, 'change_ids': ['D-1']}
        renamed = {**common, 'deleted_column': 'state', 'priority': 'HIGH',
                   'input_influence': {'level': 'HIGH', 'reason': '传入参数控制排序'},
                   'column_change_kind': 'OUTPUT_ALIAS_RENAMED',
                   'replacement_column': 'score',
                   'deleted_projection': {'expression': 'max(score)'}, 'change_ids': ['D-2']}
        candidates = [common, renamed,
                      {**common, 'project': 'other'},
                      {**common, 'base_sha': 'd' * 40},
                      {**common, 'head_sha': 'c' * 40},
                      {**common, 'path': 'mapper/Other.xml'},
                      {**common, 'namespace': 'example.OtherMapper'},
                      {**common, 'select_id': 'otherQuery'}]
        scoped = {'sdks': [], 'alerts': [], 'projects': [], 'gaps': [],
                  'query_contract': {'candidates': candidates, 'omissions': []}}
        with tempfile.TemporaryDirectory() as d, patch(
                'diff_analysis.human_report.analyze', return_value=scoped):
            root = Path(d)
            write_html(root, {'ledgers': []}, [], [])
            page = (root / 'report.html').read_text('utf-8')
            raw = json.loads((root / 'query_contract_latest.json').read_text('utf-8'))
        self.assertEqual(7, scoped['human_priority_count'])
        self.assertEqual(7, page.count('旧排序字段可能继续被使用：'))
        group = page.split('旧排序字段可能继续被使用：', 1)[1].split('</article>', 1)[0]
        self.assertIn('reconciliation / queryGroupByOrder（2 个字段）', group)
        self.assertIn('<summary>字段 state（外部影响：高）</summary>', group)
        self.assertIn('<summary>字段 allstate（外部影响：中）</summary>', group)
        self.assertIn('可能的问题：', group)
        self.assertIn('确认动作：', group)
        self.assertIn('state 已改名为 score', group)
        self.assertIn('allstate 已从 SELECT 输出中移除', group)
        self.assertIn('D-1', group)
        self.assertIn('D-2', group)
        self.assertEqual(candidates, raw['candidates'])

    def test_input_influence_does_not_duplicate_query_or_raise_low_scope(self):
        influence = {'level': 'HIGH', 'reason': '参数影响改动', 'method': 'run',
                     'parameters': ['req'], 'evidence': [], 'side': 'base'}
        common = {'project': 'p', 'base_sha': 'a', 'head_sha': 'b', 'path': 'S.java',
                  'surfaces': [], 'priority': 'HIGH', 'input_influence': influence,
                  'requires_confirmation': True}
        external = [dict(common, change_id='D-1'), dict(common, change_id='D-2'),
                    dict(common, change_id='D-query'),
                    dict(common, change_id='D-low', requires_confirmation=False,
                         input_influence={'level': 'LOW', 'reason': '内部确定值'})]
        query = {'project': 'p', 'base_sha': 'a', 'head_sha': 'b', 'path': 'M.xml',
                 'namespace': 'M', 'select_id': 'find', 'deleted_column': 'state',
                 'old': {'sql_excerpt': 'SELECT state'}, 'new': {'sql_excerpt': 'SELECT score'},
                 'change_ids': ['D-query'], 'input_influence': influence}
        scoped = {'sdks': [], 'alerts': [], 'projects': [], 'gaps': [],
                  'query_contract': {'candidates': [query]},
                  'external_influence': {'candidates': external}}
        with tempfile.TemporaryDirectory() as d, patch(
                'diff_analysis.human_report.analyze', return_value=scoped):
            root = Path(d)
            write_html(root, {'ledgers': []}, [], [])
            page = (root / 'report.html').read_text('utf-8')
            saved = json.loads((root / 'external_influence_latest.json').read_text('utf-8'))
        self.assertEqual(2, scoped['human_priority_count'])
        self.assertEqual(1, page.count('改动的输入边界需要确认：'))
        self.assertEqual(1, page.count('旧排序字段可能继续被使用：'))
        self.assertIn('外部影响程度：高', page)
        self.assertIn('该输入相关实现只在旧版存在', page)
        self.assertNotIn('D-low', page)
        self.assertEqual(4, len(saved['candidates']))

    def test_two_resource_types_share_one_priority_card(self):
        base, head, client = ("a" * 40, "b" * 40, "c" * 40)
        table = {
            "table": "trans_record", "producer_project": "adpts",
            "consumer_project": "webapi", "producer_base_sha": base,
            "producer_head_sha": head, "consumer_sha": client,
            "change_ids": ["D-1"], "producer_uses": [],
            "consumer_uses": [{"path": "src/read.sql", "start": 1, "end": 1,
                               "role": "READ"}], "status": "TABLE_NAME_ONLY_UNCONFIRMED",
            "instance_evidence": {"status": "CONFIG_IDENTITY_UNKNOWN"}}
        shared = {
            "kind": "REDIS_KEY", "resource": "trans_record",
            "producer_project": "adpts", "consumer_project": "webapi",
            "base_sha": base, "head_sha": head, "consumer_sha": client,
            "change_ids": ["D-2"], "source_uses": [], "consumer_uses": [],
            "status": "STATIC_RESOURCE_NAME_ONLY_UNCONFIRMED"}
        scoped = {"sdks": [], "alerts": [], "projects": [], "gaps": [],
                  "contract_impacts": [],
                  "database": {"impacts": [table], "omissions": []},
                  "shared_resources": {"impacts": [shared], "omissions": []}}
        with tempfile.TemporaryDirectory() as d, patch(
                "diff_analysis.human_report.analyze", return_value=scoped):
            root = Path(d)
            write_html(root, {"ledgers": []}, [], [])
            page = (root / "report.html").read_text("utf-8")
            self.assertEqual(1, page.count("跨项目资源影响：adpts"))
            self.assertIn("跨项目数据库表：trans_record", page)
            self.assertIn("REDIS_KEY：trans_record", page)
            self.assertEqual(1, scoped["human_priority_count"])
            self.assertEqual(2, scoped["resource_candidate_count"])


if __name__ == "__main__":
    unittest.main()
