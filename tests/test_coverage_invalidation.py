"""覆盖与补证失效采用同一派生计算，不改写历史事实。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_revision_pipeline import release, D1, D2, G1, G2
from diff_analysis.review_workflow import export_questions, import_review
from diff_analysis.review_feedback import review_status
from diff_analysis.revision_pipeline import update_review_package
from diff_analysis.incremental_review import recompute_review


class CoverageInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = release()
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        self.questions = export_questions(self.root, self.data)['questions']

    def approve(self):
        payload = {'schema_version': 'review-import-v1', 'decisions': [
            {'object_type': obj, 'object_id': oid, 'dimension': 'IMPACT',
             'actor_kind': 'HUMAN', 'actor_id': 'reviewer', 'verdict': 'SUPPORT',
             'reason': '已经核对当前范围和相关证据', 'evidence_ids': [did]}
            for obj, oid, did in [('QUESTION', self.questions[0]['id'], D1),
                                 ('GROUP', G1, D1), ('DIFF', D1, D1),
                                 ('GROUP', G2, D2), ('DIFF', D2, D2)]]}
        import_review(self.root, payload)
        return payload

    def test_supplement_invalidates_related_objects_and_is_idempotent(self):
        payload = self.approve()
        (self.root / 'supplement_state.json').write_text(json.dumps({
            'schema_version': 'supplement-state-v1', 'rounds': 1, 'bytes_used': 12,
            'requests': [], 'evidence': [{'id': 'EV-new', 'sha256': 'content-hash',
                'question_id': self.questions[0]['id'], 'project': 'team/pay',
                'side': 'HEAD', 'sha': '1' * 40, 'path': 'Pay.java', 'start': 2, 'end': 4,
                'created_at_utc': '2000-01-01T00:00:00+00:00'}]}), encoding='utf-8')
        for _ in range(2):
            status = review_status(self.root)
            flags = {(d['object_type'], d['object_id']): d['current_applicability']
                     for d in status['current_latest']}
            self.assertEqual('NEEDS_REVIEW', flags[('GROUP', G1)])
            self.assertEqual('NEEDS_REVIEW', flags[('DIFF', D1)])
            self.assertEqual('CURRENT', flags[('GROUP', G2)])
            self.assertEqual('CURRENT', flags[('DIFF', D2)])
        import_review(self.root, payload)
        self.assertTrue(all(d['current_applicability'] == 'CURRENT'
                            for d in review_status(self.root)['current_latest']))

    def test_refresh_without_grouping_is_offline_and_preserves_history(self):
        self.approve()
        original = (self.root / 'analysis.json').read_bytes()
        journal = (self.root / 'review_state.json').read_bytes()
        with patch('diff_analysis.gitops.git', side_effect=AssertionError('no Git')), \
                patch('diff_analysis.scanner.scan_tree', side_effect=AssertionError('no scan')):
            recompute_review(self.root)
            status = review_status(self.root)
        self.assertEqual(original, (self.root / 'analysis.json').read_bytes())
        self.assertEqual(journal, (self.root / 'review_state.json').read_bytes())
        coverage = json.loads((self.root / 'coverage_latest.json').read_text('utf-8'))
        self.assertEqual('PARTIAL', coverage['status'])
        self.assertEqual('UNKNOWN', coverage['stages']['behavior']['status'])
        self.assertIn('coverage', status)

    def test_coverage_changes_only_invalidate_matching_project_and_file(self):
        from diff_analysis.coverage import refresh_coverage
        from diff_analysis.scanner import SCANNER_RULES
        self.data['java_coverage'] = {'team/pay': {'BASE': {'status': 'COMPLETE', 'diagnostics': [], 'rules_version': SCANNER_RULES},
                                                    'HEAD': {'status': 'COMPLETE', 'diagnostics': [], 'rules_version': SCANNER_RULES}}}
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        self.approve()
        self.data['java_coverage']['team/pay']['HEAD'] = {
            'status': 'PARTIAL', 'rules_version': SCANNER_RULES,
            'diagnostics': [{'path': 'Different.java', 'reason': 'PARSE_FAILED'}]}
        self.data['relations']['graphs'][0]['coverage'] = {'graph_complete': False}
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        refresh_coverage(self.root)
        self.assertTrue(all(d['current_applicability'] == 'CURRENT'
                            for d in review_status(self.root)['current_latest']))

    def test_missing_decision_fingerprint_is_rejected_without_conversion(self):
        self.approve()
        path = self.root / 'review_state.json'
        state = json.loads(path.read_text('utf-8'))
        state['decisions'][0].pop('question_dependency_fingerprint')
        path.write_text(json.dumps(state), encoding='utf-8')
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '依赖指纹'):
            review_status(self.root)
        self.assertEqual(original, path.read_bytes())

    def test_missing_question_file_is_not_reconstructed(self):
        folder = self.root / 'incomplete-run'
        folder.mkdir()
        (folder / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        with self.assertRaises(FileNotFoundError):
            update_review_package(folder)
        self.assertFalse((folder / 'questions.json').exists())

    def test_real_supplement_entrypoints_refresh_related_decisions(self):
        from test_protocol_and_semantic_evidence import make_repo
        from diff_analysis.review_workflow import supplement
        from diff_analysis.semantic_evidence import collect_semantic_evidence
        repo, sha = make_repo(self.root, 'src', 'class Service {}\n')
        self.data['ledgers'][0].update(base=sha, head=sha)
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        common = {'question_id': self.questions[0]['id'], 'project': 'team/pay',
                  'sha': sha, 'side': 'HEAD', 'path': 'Service.java'}
        for semantic in (False, True):
            payload = self.approve()
            if semantic:
                request = {'schema_version': 'semantic-evidence-v1',
                           'selectors': [{**common, 'kind': 'CONTRACT'}]}
                collect = collect_semantic_evidence
            else:
                request = {'schema_version': 'evidence-requests-v1', 'selectors': [
                    {**common, 'start': 1, 'end': 1, 'reason': '核对固定版本完整源码'}]}
                collect = supplement
            # 两入口补相同内容时也只会复用相同依赖；语义入口额外取 BASE 形成新证据。
            if semantic:
                request['selectors'][0]['side'] = 'BASE'
            collect(self.root, request, {'team/pay': repo}, max_rounds=10)
            package = json.loads((self.root / 'review_package_latest.json').read_text('utf-8'))
            self.assertTrue(package['decisions_to_revalidate'])
            flags = {(d['object_type'], d['object_id']): d['current_applicability']
                     for d in review_status(self.root)['current_latest']}
            for obj, oid in [('QUESTION', self.questions[0]['id']), ('GROUP', G1), ('DIFF', D1)]:
                self.assertEqual('NEEDS_REVIEW', flags[(obj, oid)])
            self.assertEqual('CURRENT', flags[('GROUP', G2)])
            import_review(self.root, payload)
            collect(self.root, request, {'team/pay': repo}, max_rounds=10)
            self.assertTrue(all(d['current_applicability'] == 'CURRENT'
                                for d in review_status(self.root)['current_latest']))

    def test_stage_summary_handles_complete_missing_invalid_and_failure(self):
        from diff_analysis.coverage import refresh_coverage, DEPENDENCY_ARTIFACTS
        from diff_analysis.behavior_analysis import RULES_VERSION
        from diff_analysis.scanner import SCANNER_RULES
        data = self.data
        data['java_coverage'] = {'team/pay': {side: {'status': 'COMPLETE', 'diagnostics': [],
            'rules_version': SCANNER_RULES} for side in ('BASE', 'HEAD')}}
        for filename, value in [('unified_graph_latest.json', {'omissions': []}),
                                ('unified_impact_latest.json', {'coverage': {'truncated': False}})]:
            (self.root / filename).write_text(json.dumps(value), encoding='utf-8')
        scope = {'projects': [{'project': 'team/pay', 'base': '0' * 40, 'head': '1' * 40}],
                 'gaps': [], 'coverage_rules_version': 'maven-sdk-declarations-v1'}
        scope.update({name: {'omissions': [], 'rules_version': RULES_VERSION, 'schema_version': name + '-v1'}
                      for name in DEPENDENCY_ARTIFACTS})
        result = refresh_coverage(self.root, data, scope, 'COMPLETE')
        self.assertEqual('COMPLETE', result['status'])
        scope['gaps'] = [{'project': 'team/unavailable', 'reason': 'REGISTRY_REVISION_UNREADABLE'}]
        result = refresh_coverage(self.root, data, scope, 'COMPLETE')
        self.assertEqual('PARTIAL', result['stages']['maven_sdk']['status'])
        self.assertTrue(any(s['project'] == 'team/unavailable' for s in result['stages']['maven_sdk']['scopes']))
        scope['gaps'] = []
        scope['behavior']['status'] = 'FAILED'
        result = refresh_coverage(self.root, data, scope, 'COMPLETE')
        self.assertEqual('PARTIAL', result['status'])
        self.assertEqual('FAILED', result['stages']['behavior']['status'])
        self.assertEqual('COMPLETE', result['stages']['report']['status'])
        scope['behavior'] = {'status': 'COMPLETE'}
        result = refresh_coverage(self.root, data, scope)
        self.assertEqual('FAILED', result['stages']['behavior']['status'])
        (self.root / 'behavior_impact_latest.json').write_text('[]', encoding='utf-8')
        result = refresh_coverage(self.root)
        self.assertEqual('FAILED', result['stages']['behavior']['status'])
        self.assertEqual('FAILED', refresh_coverage(self.root, {'ledgers': []})['status'])

    def test_project_gap_without_file_does_not_invalidate_other_project(self):
        from diff_analysis.scanner import SCANNER_RULES
        from copy import deepcopy
        other = deepcopy(self.data['ledgers'][0])
        other['project'] = 'team/other'
        other['final_changes'] = [{**other['final_changes'][0], 'project': 'team/other',
                                   'id': 'D-3333333333333333'}]
        self.data['ledgers'].append(other)
        self.data['groups'].append({'id': 'G-3333333333333333', 'change_ids': ['D-3333333333333333']})
        self.data['java_coverage'] = {project: {side: {'status': 'COMPLETE', 'diagnostics': [],
            'rules_version': SCANNER_RULES} for side in ('BASE', 'HEAD')} for project in ('team/pay', 'team/other')}
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        export_questions(self.root, self.data)
        payload = self.approve()
        payload['decisions'].append({**payload['decisions'][1], 'object_id': 'G-3333333333333333',
                                     'evidence_ids': ['D-3333333333333333']})
        import_review(self.root, payload)
        self.data['java_coverage']['team/pay']['HEAD'].update(
            status='PARTIAL', diagnostics=[{'reason': 'SOURCE_BUDGET'}])
        (self.root / 'analysis.json').write_text(json.dumps(self.data), encoding='utf-8')
        flags = {(d['object_type'], d['object_id']): d['current_applicability']
                 for d in review_status(self.root)['current_latest']}
        self.assertEqual('NEEDS_REVIEW', flags[('GROUP', G1)])
        self.assertEqual('NEEDS_REVIEW', flags[('GROUP', G2)])
        self.assertEqual('CURRENT', flags[('GROUP', 'G-3333333333333333')])

    def test_later_goal_revision_does_not_reinvalidate_unrelated_reapproved_group(self):
        payload = self.approve()
        payload['expected_goals'] = [{'id': name, 'status': 'HUMAN_CONFIRMED',
            'author': 'reviewer', 'text': '核实对应需求的边界和预期行为'} for name in ('GOAL-A', 'GOAL-B')]
        for decision in payload['decisions']:
            decision['expected_goal_id'] = 'GOAL-B' if decision['object_id'] in (G2, D2) else 'GOAL-A'
        import_review(self.root, payload)
        import_review(self.root, {'schema_version': 'review-import-v1', 'expected_goals': [{
            'id': 'GOAL-A2', 'supersedes': 'GOAL-A', 'status': 'HUMAN_CONFIRMED',
            'author': 'reviewer', 'text': '补充第一组的业务验收边界'}]})
        for decision in payload['decisions']:
            if decision['expected_goal_id'] == 'GOAL-A':
                decision['expected_goal_id'] = 'GOAL-A2'
        payload.pop('expected_goals')
        import_review(self.root, payload)
        import_review(self.root, {'schema_version': 'review-import-v1', 'expected_goals': [{
            'id': 'GOAL-B2', 'supersedes': 'GOAL-B', 'status': 'HUMAN_CONFIRMED',
            'author': 'reviewer', 'text': '补充第二组的业务验收边界'}]})
        flags = {(d['object_type'], d['object_id']): d['current_applicability']
                 for d in review_status(self.root)['current_latest']}
        self.assertEqual('CURRENT', flags[('GROUP', G1)])
        self.assertEqual('CURRENT', flags[('DIFF', D1)])
        self.assertEqual('NEEDS_REVIEW', flags[('GROUP', G2)])


if __name__ == '__main__':
    unittest.main()
