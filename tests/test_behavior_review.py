"""行为候选的报告、精确分组与当前格式的依赖指纹。"""
import json
import tempfile
import unittest
from pathlib import Path

from diff_analysis.behavior_review import refresh_behavior_review, render_behavior
from diff_analysis.human_report import write_html
from diff_analysis.revision_pipeline import update_review_package
from diff_analysis.review_workflow import _state, _write_json


class BehaviorReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = {'schema_version': 'release-evidence-v1', 'ledgers': [{
            'project': 'demo', 'base': 'a' * 40, 'head': 'b' * 40, 'final_changes': [{
                'id': 'D-1', 'path': 'A.java', 'old_path': 'A.java',
                'old_range': {'start': 2, 'count': 2}, 'new_range': {'start': 2, 'count': 2}}]}],
            'groups': [{'id': 'G-1', 'change_ids': ['D-1']}]}
        self.candidate = {
            'id': 'B-1', 'kind': 'PRECONDITION', 'producer_project': 'demo', 'consumer_project': 'demo',
            'producer_base_sha': 'a' * 40, 'producer_head_sha': 'b' * 40, 'consumer_sha': 'b' * 40,
            'source_symbol': 'demo.A#f()', 'consumer_symbol': 'demo.A#f()', 'resource': 'demo.A#f',
            'change_ids': ['D-1'], 'source_uses': [{'path': 'A.java', 'start': 2, 'end': 2, 'side': 'head'}],
            'consumer_uses': [{'path': 'A.java', 'start': 2, 'end': 2, 'sha': 'b' * 40}],
            'assumption': '必须非空', 'change_fact': '新增解引用', 'trigger': [],
            'possible_impact': '<script>unsafe</script>', 'evidence': {'test': True},
            'evidence_level': 'BOUND_STATIC_PATH', 'input_influence_level': 'MEDIUM',
            'relation': 'SAME_METHOD', 'unknown_reasons': [], 'facts_fingerprint': 'original'}
        _write_json(self.root / 'analysis.json', self.release)
        _write_json(self.root / 'questions.json', {'questions': []})
        self.save()

    def save(self):
        from diff_analysis.behavior_analysis import RULES_VERSION
        _write_json(self.root / 'behavior_impact_latest.json', {
            'schema_version': 'behavior-impact-v1', 'status': 'PARTIAL',
            'rules_version': RULES_VERSION,
            'analysis_fingerprint': 'rules-1', 'candidates': [self.candidate], 'omissions': []})

    def test_exact_split_and_facts_unchanged(self):
        before = (self.root / 'behavior_impact_latest.json').read_bytes()
        units = {'D-1': [
            {'id': 'U-1', 'primary_group': 'G-A', 'range_basis': 'NEW_LINE_SEGMENT_ONLY',
             'new_range': {'start': 2, 'count': 1}},
            {'id': 'U-2', 'primary_group': 'G-B', 'range_basis': 'NEW_LINE_SEGMENT_ONLY',
             'new_range': {'start': 3, 'count': 1}}]}
        result = refresh_behavior_review(self.root, self.release, units)
        self.assertEqual(1, result['groups'])
        review = json.loads((self.root / 'behavior_review_latest.json').read_text('utf-8'))
        self.assertEqual({'G-A'}, set(review['groups']))
        self.assertEqual(before, (self.root / 'behavior_impact_latest.json').read_bytes())
        self.assertTrue(review['coverage_gaps'])

    def test_changed_fact_invalidates_question_fingerprint(self):
        update_review_package(self.root)
        before = json.loads((self.root / 'review_package_latest.json').read_text('utf-8'))
        self.candidate['facts_fingerprint'] = 'new-mapping'
        self.save()
        update_review_package(self.root)
        after = json.loads((self.root / 'review_package_latest.json').read_text('utf-8'))
        self.assertNotEqual(before['questions'][0]['dependency_fingerprint'], after['questions'][0]['dependency_fingerprint'])
        self.assertEqual('BEHAVIOR_IMPACT', after['questions'][0]['kind'])

    def test_html_escaping_and_trace(self):
        rendered = render_behavior([self.candidate])
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('data-evidence="D-1"', rendered)
        self.assertIn('A.java:2', rendered)

    def test_missing_behavior_stage_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            result = refresh_behavior_review(folder, self.release)
            self.assertEqual('UNKNOWN', result['status'])
            scope = {'sdks': [], 'alerts': [], 'gaps': [], 'projects': []}
            write_html(Path(folder), {'ledgers': []}, [], [], dependency_scope=scope)
            self.assertIn('行为分析：未知', (Path(folder) / 'report.html').read_text('utf-8'))

    def test_unrelated_batch_fingerprint_does_not_invalidate_group(self):
        update_review_package(self.root)
        before = json.loads((self.root / 'review_package_latest.json').read_text('utf-8'))
        path = self.root / 'behavior_impact_latest.json'
        raw = json.loads(path.read_text('utf-8'))
        raw['analysis_fingerprint'] = 'another-project-changed'
        _write_json(path, raw)
        update_review_package(self.root)
        after = json.loads((self.root / 'review_package_latest.json').read_text('utf-8'))
        self.assertEqual(before['questions'][0]['dependency_fingerprint'], after['questions'][0]['dependency_fingerprint'])

    def test_standalone_count_and_resource_merge(self):
        scope = {'sdks': [], 'alerts': [], 'gaps': [], 'projects': [],
                 'behavior': {'status': 'PARTIAL', 'candidates': [self.candidate], 'omissions': []}}
        result = write_html(self.root, {'ledgers': []}, [], [], dependency_scope=scope)
        self.assertEqual(1, result['human_priority_count'])
        self.candidate.update(kind='PERSISTENCE_COMPATIBILITY', relation='SHARED_RESOURCE', resource='account#status')
        scope['database'] = {'impacts': [{
            'table': 'account', 'producer_project': 'demo', 'consumer_project': 'demo',
            'producer_base_sha': 'a' * 40, 'producer_head_sha': 'b' * 40, 'consumer_sha': 'b' * 40,
            'change_ids': ['D-1'], 'producer_uses': self.candidate['source_uses'],
            'consumer_uses': self.candidate['consumer_uses'], 'status': 'TABLE_NAME_ONLY_UNCONFIRMED'}]}
        result = write_html(self.root, {'ledgers': []}, [], [], dependency_scope=scope)
        self.assertEqual(1, result['human_priority_count'])
        text = (self.root / 'report.html').read_text('utf-8')
        self.assertEqual(1, text.count('读写闭环与历史兼容 · B-1'))


if __name__ == '__main__':
    unittest.main()
