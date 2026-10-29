"""固定源码、真实 javac AST 与行为分析端到端验证。"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.scanner import scan_tree, java_toolchain
from diff_analysis.behavior_flow import Flow
from diff_analysis.behavior_analysis import analyze_methods, analyze_behavior, semantics_from_registry, analyze_bound_consumers
from diff_analysis.behavior_resources import sql_facts, resource_adapters, analyze_persistence


class BehaviorTests(unittest.TestCase):
    def test_array_initializer_evaluates_requirements_and_stops_on_throw(self):
        requirements, _, _ = self.requirements('int f(String p) { int[] a = { p.length() }; return 1; }')
        self.assertTrue(any(e['requirement'] == 'NONNULL' for e in requirements))
        methods = self.scan('int fail() { throw new IllegalStateException(); } '
                            'int f(String p) { int[] a = {fail(), p.length()}; return 1; }')
        flow = Flow(methods)
        states = flow.run(next(m for m in methods.values() if m['name'] == 'f'))
        self.assertTrue(all(s.stop == 'throw' for s in states))
        self.assertFalse(any(e['kind'] == 'REQUIREMENT' for s in states for e in s.events))

    def test_constructor_throw_propagates_and_can_be_caught(self):
        for caught in (False, True):
            body = 'new Boom(); return p.length();'
            if caught:
                body = 'try { ' + body + ' } catch (IllegalStateException e) { return 7; }'
            methods = self.scan('int f(String p) { ' + body + ' }',
                                'class Boom { Boom() { throw new IllegalStateException(); } }')
            states = Flow(methods).run(next(m for m in methods.values() if m['name'] == 'f'))
            self.assertTrue(all(s.stop == ('return' if caught else 'throw') for s in states))
            self.assertFalse(any(e['kind'] == 'REQUIREMENT' for s in states for e in s.events))
            if caught:
                self.assertTrue(all(s.value.constant == 7 for s in states))

    def test_java_integer_overflow_promotion_and_cast(self):
        for declaration in ('int n = Integer.MAX_VALUE; n = n + 1;',
                            'long n = Long.MAX_VALUE; n = n + 1L;',
                            'int n = Integer.MIN_VALUE; n = -n;',
                            'int n = (int) 2147483648L;'):
            requirements, _, _ = self.requirements(
                'int f(String p) { ' + declaration + ' if (n < 0) return p.length(); return 0; }')
            self.assertTrue(any(e['requirement'] == 'NONNULL' for e in requirements), declaration)
        _, _, states = self.requirements('long f() { int a = 2147483647; return a + 1L; }')
        self.assertTrue(all(s.value.constant == 2147483648 for s in states))
        _, _, states = self.requirements('int f() { return (byte) 255; }')
        self.assertTrue(all(s.value.constant == -1 for s in states))
        for body, expected in (
                ('long f() { long n = 2147483647; return n + 1; }', 2147483648),
                ('long f() { long n = 0; n = 2147483647; return n + 1; }', 2147483648),
                ('long g(long n) { return n + 1; } long f() { return g(2147483647); }', 2147483648),
                ('long g() { return 2147483647; } long f() { return g() + 1; }', 2147483648),
                ('int f() { int n = 65536; return n * n; }', 0),
                ('int f() { int n = -2147483648; return n - 1; }', 2147483647),
                ('int f() { char c = 65535; return c + 1; }', 65536),
                ("boolean f() { char c = 'A'; return c == 65; }", True)):
            _, _, states = self.requirements(body)
            self.assertTrue(all(s.value.known and s.value.constant == expected for s in states), (body, states))
        requirements, _, _ = self.requirements("int f(String p) { char c = 'A'; if (c == 65) return p.length(); return 0; }")
        self.assertTrue(requirements)

    def test_direct_caller_carries_scalar_and_array_facts(self):
        for signature, unsafe, caller in (
                ('int p', '10 / p', 'int caller(int p) { if (p == 0) return 0; return f(p); }'),
                ('int[] a, int i', 'a[i]', 'int caller(int[] a, int i) { if (a == null || i < 0 || i >= a.length) return 0; return f(a, i); }')):
            before = '\nint f(' + signature + ') { return 0; }\n' + caller + '\n'
            after = before.replace('return 0; }\n', 'return ' + unsafe + '; }\n', 1)
            candidates, _ = self.changes(before, after)
            self.assertFalse(any('caller(' in c['consumer_symbol'] for c in candidates), candidates)
            candidates, _ = self.changes(before, after.replace('return f(p);', 'p = 0; return f(p);'))
            if signature == 'int p':
                self.assertTrue(any('caller(' in c['consumer_symbol'] for c in candidates))

    def test_snapshot_rejects_missing_current_scan_metadata(self):
        from diff_analysis.behavior_analysis import snapshot
        review = {'head': 'a' * 40, 'methods': {}, 'incomplete': True,
                  'warnings': ['new parse: Broken.java']}
        with patch('diff_analysis.behavior_analysis.source_snapshot_index', return_value=(
                {'files': [{'path': 'Broken.java', 'blob_sha': 'b' * 40}], 'omissions': []}, False)), \
                patch('diff_analysis.behavior_analysis.git', return_value=b'class Broken { not java }'):
            with self.assertRaisesRegex(ValueError, '当前规则'):
                snapshot({'repo': '.', 'head': 'a' * 40}, 'p', 'head', self.cache, review)

    def test_cast_does_not_carry_constraints_for_changed_argument_value(self):
        for cast, safe in (('(long)', True), ('(byte)', False)):
            requirements, _, _ = self.requirements(
                'int divide(long p) { return (int) (10 / p); } '
                'int f(int p) { if (p == 0) return 0; return divide(' + cast + 'p); }')
            self.assertEqual(safe, not any(e['requirement'] == 'NONZERO' for e in requirements))

    def test_snapshot_rejects_rule_and_ast_mismatch_without_rescanning(self):
        from diff_analysis.behavior_analysis import snapshot
        from diff_analysis.scanner import SCANNER_RULES
        for rules, ast in ((SCANNER_RULES + '-other', 'behavior-ast-v2'), (SCANNER_RULES, 'unsupported-ast')):
            with self.subTest(rules=rules, ast=ast), \
                    patch('diff_analysis.behavior_analysis.source_snapshot_index', return_value=(
                        {'files': [], 'omissions': []}, False)), \
                    patch('diff_analysis.behavior_analysis.scan_tree', side_effect=AssertionError('不能隐式重扫')):
                with self.assertRaisesRegex(ValueError, '当前规则'):
                    snapshot({'repo': '.', 'head': 'a' * 40}, 'p', 'head', self.cache, {
                        'head': 'a' * 40, 'methods': {'A#f()': {'path': 'A.java', 'source_scope': '',
                            'behavior': {'version': ast}}}, 'java_coverage': {'HEAD': {
                            'status': 'COMPLETE', 'diagnostics': [], 'rules_version': rules}}})

    def test_unsupported_numeric_semantics_stay_unknown(self):
        requirements, flow, states = self.requirements(
            'int f(String p) { float n = 16777216f; if (n + 1f > n) return p.length(); return 0; }')
        self.assertTrue(requirements)
        self.assertTrue(any(g['reason'] == 'NUMERIC_OPERATION_UNKNOWN' for g in flow.gaps))
        self.assertTrue(any(s.uncertain for s in states))

    def test_fresh_cached_and_reused_snapshot_keep_java_diagnostics(self):
        from diff_analysis.behavior_analysis import snapshot
        from diff_analysis.scanner import SCANNER_RULES
        entry = {'repo': '.', 'head': 'f' * 40}
        index = {'files': [{'path': 'Broken.java', 'blob_sha': 'e' * 40}], 'omissions': []}
        with patch('diff_analysis.behavior_analysis.source_snapshot_index', return_value=(index, False)), \
                patch('diff_analysis.behavior_analysis.git', return_value=b'class Broken { not java }'):
            fresh = snapshot(entry, 'broken', 'head', self.cache)
            self.assertTrue(fresh['gaps'])
            with patch('diff_analysis.behavior_analysis.scan_tree', side_effect=AssertionError('cache expected')):
                cached = snapshot(entry, 'broken', 'head', self.cache)
                reused = snapshot(entry, 'broken', 'head', self.cache, {
                    'head': entry['head'], 'methods': fresh['methods'], 'java_coverage': {
                        'HEAD': {'rules_version': SCANNER_RULES, 'status': 'PARTIAL',
                                 'diagnostics': fresh['gaps']}}})
            self.assertEqual(fresh['gaps'], cached['gaps'])
            self.assertTrue(all(g in reused['gaps'] for g in fresh['gaps']))
            self.assertTrue(any(g.get('path') == 'Broken.java' for g in reused['gaps']))

    def test_lower_upper_facts_prune_only_proven_direct_call_paths(self):
        body = ('int risky(int p) { if (p < 1 || p > 8) return 10 / 0; return 1; } '
                'int f(int p) { if (p < 1 || p > 8) return 0; return risky(p); }')
        requirements, _, _ = self.requirements(body)
        self.assertFalse(requirements)
        requirements, _, _ = self.requirements(body.replace('return risky(p);', 'p = 9; return risky(p);'))
        self.assertTrue(requirements)

    def test_constructor_exception_runs_finally_without_assigning_object(self):
        methods = self.scan('int f(String p) { try { Boom b = new Boom(); return p.length(); } '
                            'finally { done(); } } void done() {}',
                            'class Boom { Boom() { throw new IllegalStateException(); } }')
        states = Flow(methods).run(next(m for m in methods.values() if m['name'] == 'f'))
        self.assertTrue(all(s.stop == 'throw' and 'b' not in s.env for s in states))
        self.assertTrue(all(any(e['kind'] == 'CALL' and e['binding'].endswith('#done()')
                                for e in s.events) for s in states))

    @classmethod
    def setUpClass(cls):
        java_toolchain()
        cls.workspace = tempfile.TemporaryDirectory()
        cls.cache = Path(cls.workspace.name) / 'cache'

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def scan(self, body, extra=''):
        with tempfile.TemporaryDirectory() as folder:
            source = 'package demo; class Example { ' + body + ' } ' + extra
            (Path(folder) / 'Example.java').write_text(source, encoding='utf-8')
            methods, calls, errors = scan_tree(folder, self.cache)
            self.assertFalse(errors, errors)
            return methods

    def changes(self, before, after, name='f', semantics=None):
        old, new = self.scan(before), self.scan(after)
        symbol = next(s for s, m in new.items() if m['name'] == name)
        old_method = old.get(symbol, {})
        method = new[symbol]
        change = {'id': 'D-1', 'path': 'Example.java', 'old_path': 'Example.java',
                  'old_range': {'start': old_method.get('start', 0), 'count': 1},
                  'new_range': {'start': method['start'], 'count': 1}}
        # 每个方法独占行，避免人为扩大变化作用域。
        return analyze_methods('demo', {'base': 'a' * 40, 'head': 'b' * 40},
                               {'final_changes': [change]}, {'methods': old}, {'methods': new}, semantics or {})

    def requirements(self, body, name='f'):
        methods = self.scan(body)
        flow = Flow(methods)
        method = next(m for m in methods.values() if m['name'] == name)
        states = flow.run(method)
        return [e for s in states for e in s.events if e['kind'] == 'REQUIREMENT'], flow, states

    def test_ast_contains_parameters_and_control_facts(self):
        methods = self.scan('String f(String p) { if (p == null) return null; return p; }')
        facts = next(m['behavior'] for m in methods.values() if m['name'] == 'f')
        self.assertEqual('p', facts['parameters'][0]['name'])
        self.assertEqual('IF', facts['body']['items'][0]['kind'])

    def test_null_guard_and_early_return(self):
        for body in ('int f(String p) { if (p == null) return 0; return p.length(); }',
                     'int f(String p) { if (p != null) return p.length(); return 0; }',
                     'int f(String p) { return p != null ? p.length() : 0; }'):
            requirements, _, _ = self.requirements(body)
            self.assertFalse(requirements, body)

    def test_short_circuit(self):
        requirements, _, _ = self.requirements('boolean f(String p) { return p != null && p.length() > 0; }')
        self.assertFalse(requirements)

    def test_partial_guard_and_assignment_invalidation(self):
        for body in ('int f(String p, boolean b) { if (b && p == null) return 0; return p.length(); }',
                     'int f(String p) { if (p == null) return 0; p = null; return p.length(); }'):
            requirements, _, _ = self.requirements(body)
            self.assertTrue(any(e['requirement'] == 'NONNULL' for e in requirements))

    def test_local_alias_guard(self):
        requirements, _, _ = self.requirements('int f(String p) { String x = p; if (p == null) return 0; return x.length(); }')
        self.assertFalse(requirements)

    def test_integer_divisor_guard_and_floating_exclusion(self):
        requirements, _, _ = self.requirements('int f(int p) { if (p == 0) return 0; return 10 / p; }')
        self.assertFalse(requirements)
        requirements, _, _ = self.requirements('int f(int p) { return 10 / p; }')
        self.assertTrue(any(e['requirement'] == 'NONZERO' for e in requirements))
        requirements, _, _ = self.requirements('double f(double p) { return 10 / p; }')
        self.assertFalse(requirements)

    def test_array_bounds(self):
        requirements, _, _ = self.requirements('int f(int[] a, int i) { if (a == null || i < 0 || i >= a.length) return 0; return a[i]; }')
        self.assertFalse(requirements)
        requirements, _, _ = self.requirements('int f(int[] a, int i) { return a[i]; }')
        self.assertEqual({'NONNULL', 'ARRAY_BOUNDS'}, {e['requirement'] for e in requirements})

    def test_unknown_loop_is_gap(self):
        _, flow, states = self.requirements('int f(String p) { while (p == null) { p = "x"; } return p.length(); }')
        self.assertTrue(any(g['reason'] == 'WHILE_LOOP' for g in flow.gaps))
        self.assertTrue(any(s.uncertain for s in states))

    def test_new_requirement_and_existing_problem_suppression(self):
        before = '\nint f(String p) { return 0; }\n'
        after = '\nint f(String p) { return p.length(); }\n'
        candidates, _ = self.changes(before, after)
        self.assertTrue(any(c['kind'] == 'PRECONDITION' for c in candidates))
        candidates, _ = self.changes(after, after)
        self.assertFalse(candidates)

    def test_narrower_existing_path_is_not_new_problem(self):
        before = '\nint f(String p, boolean b) { return p.length(); }\n'
        after = '\nint f(String p, boolean b) { if (b) return p.length(); return 0; }\n'
        candidates, _ = self.changes(before, after)
        self.assertFalse(candidates)

    def test_field_guard_invalidated_by_unknown_call(self):
        requirements, flow, _ = self.requirements('String value; int f() { if (value == null) return 0; System.gc(); return value.length(); }')
        self.assertTrue(any(e['requirement'] == 'NONNULL' for e in requirements))
        self.assertTrue(any(g['reason'] == 'CALL_SUMMARY_UNKNOWN' for g in flow.gaps))

    def test_direct_caller_guard(self):
        before = '\nint f(String p) { return 0; }\nint caller(String p) { if (p == null) return 0; return f(p); }\n'
        after = before.replace('return 0; }\nint caller', 'return p.length(); }\nint caller', 1)
        candidates, _ = self.changes(before, after)
        self.assertFalse(any('caller(' in c['consumer_symbol'] for c in candidates))

    def test_new_null_return_used_by_unchanged_caller(self):
        before = '\nString f() { return "ok"; }\nint caller() { String x = f(); return x.length(); }\n'
        after = before.replace('return "ok";', 'return null;')
        candidates, _ = self.changes(before, after)
        self.assertTrue(any(c['kind'] == 'RESULT_HANDLING' and 'caller(' in c['consumer_symbol'] for c in candidates), candidates)

    def test_plain_boolean_ignored_is_not_failure(self):
        before = '\nboolean f() { return true; }\nvoid caller() { f(); }\n'
        candidates, _ = self.changes(before, before.replace('return true;', 'return false;'))
        self.assertFalse(candidates)

    def test_exception_to_mapped_failure_continues_effect(self):
        before = '\nboolean f() { throw new IllegalStateException(); }\nvoid write() {}\nvoid caller() { f(); write(); }\n'
        after = before.replace('throw new IllegalStateException();', 'return false;')
        rules = {'results': [{'method': 'demo.Example#f()', 'failure_values': [False]}],
                 'effects': [{'method': 'demo.Example#write()', 'operation': 'MQ_SEND'}]}
        candidates, _ = self.changes(before, after, semantics=rules)
        self.assertTrue(any(c['kind'] == 'RESULT_HANDLING' and c['evidence']['effects'] for c in candidates), candidates)

    def test_result_mapping_conflict_is_gap(self):
        methods = self.scan('String f() { return "other"; } void caller() { f(); }')
        flow = Flow(methods, {'results': [{'method': 'demo.Example#f()', 'failure_values': ['failed'], 'success_values': ['ok']}]})
        flow.run(next(m for m in methods.values() if m['name'] == 'caller'))
        self.assertTrue(any(g['reason'] == 'RESULT_MAPPING_CONFLICT' for g in flow.gaps))

    def test_finite_return_domain_and_exception_change(self):
        before = '\nString f() { return "A"; }\nint caller() { switch (f()) { case "A": return 1; default: throw new IllegalArgumentException(); } }\n'
        candidates, _ = self.changes(before, before.replace('return "A";', 'return "C";'))
        self.assertTrue(any(c['kind'] == 'RESULT_HANDLING' for c in candidates), candidates)
        before = '\nString f() { return "A"; }\nString caller() { return f(); }\n'
        candidates, _ = self.changes(before, before.replace('return "A";', 'throw new IllegalStateException();'))
        self.assertTrue(any(c['kind'] == 'RESULT_HANDLING' and c['evidence'].get('exception') for c in candidates), candidates)

    def test_cross_project_exact_symbol_and_single_version(self):
        old = self.scan('String f() { return "ok"; }')
        new = self.scan('String f() { return null; }')
        caller_methods = self.scan('String f() { return "ok"; }',
                                   'class Consumer { int use() { return new Example().f().length(); } }')
        caller_methods = {s: m for s, m in caller_methods.items() if m['class'] == 'demo.Consumer'}
        entries = {'p': {'base': 'a' * 40, 'head': 'b' * 40}, 'c': {'base': None, 'head': 'c' * 40}}
        snapshots = {('p', 'base'): {'methods': old, 'sha': 'a' * 40},
                     ('p', 'head'): {'methods': new, 'sha': 'b' * 40},
                     ('c', 'head'): {'methods': caller_methods, 'sha': 'c' * 40}}
        ledger = {'project': 'p', 'final_changes': [{'id': 'D-1', 'path': 'Example.java', 'old_path': 'Example.java',
                  'old_range': {'start': 1, 'count': 1}, 'new_range': {'start': 1, 'count': 1}}]}
        candidates, _ = analyze_bound_consumers(entries, [ledger], snapshots, {}, {'p': {}, 'c': {}})
        self.assertTrue(any(c['relation'] == 'DIRECT_CROSS_PROJECT_JAVAC_SYMBOL' for c in candidates), candidates)
        self.assertTrue(all(c['consumer_sha'] == 'c' * 40 for c in candidates))

    def test_redis_value_flow_historical_null_and_fallback(self):
        def snap(field, guard=False, redis_key='k'):
            source = ('package demo; class Example { Redis redisClient;\n'
                      'void write() { redisClient.set("k", new Payload(' + field + ')); }\n'
                      'int read() { Payload x = (Payload)redisClient.get("' + redis_key + '"); '
                      'if (x == null) return 0; ' + ('if (x.value == null) return 0; ' if guard else '') +
                      'return x.value.length(); }\n}\n'
                      'class Payload { String value; Payload(String v) { this.value = v; } }\n'
                      'class Redis { void set(String k, Object v) {} Object get(String k) { return null; } }')
            with tempfile.TemporaryDirectory() as folder:
                (Path(folder) / 'Example.java').write_text(source, encoding='utf-8')
                methods, _, errors = scan_tree(folder, self.cache)
                self.assertFalse(errors)
            result = {'methods': methods, 'files': {'Example.java': source}, 'sha': 'x' * 40}
            adapter, _ = resource_adapters('p', result)
            return result, adapter

        old, old_adapter = snap('null', guard=True)
        new, new_adapter = snap('"ok"')
        entries = {'p': {'base': 'a' * 40, 'head': 'b' * 40}}
        # 读取方逻辑变化，旧版写入可能为空。
        ledger = {'project': 'p', 'base': 'a' * 40, 'head': 'b' * 40, 'final_changes': [
            {'id': 'D-1', 'path': 'Example.java', 'old_path': 'Example.java',
             'old_range': {'start': 3, 'count': 1}, 'new_range': {'start': 3, 'count': 1}}]}
        rows, _ = analyze_persistence(entries, [ledger], {('p', 'base'): old, ('p', 'head'): new},
                                     {'p': {'base': old_adapter, 'head': new_adapter}}, {}, {}, {})
        self.assertTrue(any(r['version_combination'] == 'OLD_POSSIBLE_WRITE_NEW_READ' for r in rows), rows)
        guarded, adapter = snap('"ok"', guard=True)
        rows, _ = analyze_persistence(entries, [ledger], {('p', 'base'): old, ('p', 'head'): guarded},
                                     {'p': {'base': old_adapter, 'head': adapter}}, {}, {}, {})
        self.assertFalse(any(r['version_combination'] == 'OLD_POSSIBLE_WRITE_NEW_READ' for r in rows), rows)
        isolated, adapter = snap('"ok"', redis_key='k:v2')
        rows, _ = analyze_persistence(entries, [ledger], {('p', 'base'): old, ('p', 'head'): isolated},
                                     {'p': {'base': old_adapter, 'head': adapter}}, {}, {}, {})
        self.assertFalse(rows)

    def test_mapped_failure_continuation_and_propagation(self):
        rules = {'results': [{'method': 'demo.Example#f()', 'failure_values': [False], 'success_values': [True]}],
                 'effects': [{'method': 'demo.Example#write()', 'operation': 'DATABASE_WRITE'}]}
        before = '\nboolean f() { return true; }\nvoid write() {}\nvoid caller() { boolean ok = f(); write(); }\n'
        candidates, _ = self.changes(before, before.replace('return true;', 'return false;'), semantics=rules)
        self.assertTrue(any(c['kind'] == 'RESULT_HANDLING' and c['evidence']['effects'] for c in candidates), candidates)
        before = before.replace('write(); }', 'if (!ok) return; write(); }')
        candidates, _ = self.changes(before, before.replace('return true;', 'return false;'), semantics=rules)
        self.assertFalse(any(c['kind'] == 'RESULT_HANDLING' for c in candidates), candidates)

    def test_constructor_member_result_rule(self):
        rules = {'results': [{'type': 'demo.Result', 'member': 'ok', 'failure_values': [False], 'success_values': [True]}]}
        methods = self.scan('Result f() { return new Result(false); } void caller() { f(); }',
                            'class Result { boolean ok; Result(boolean x) { this.ok = x; } }')
        flow = Flow(methods, rules)
        state = flow.run(next(m for m in methods.values() if m['name'] == 'caller'))[0]
        self.assertTrue(any(e['kind'] == 'OUTCOME' and e.get('failure') for e in state.events), state.events)

    def test_wrapped_failure_checked_through_getter_and_nested_type(self):
        before = ('\nstatic class Result { boolean ok; Result(boolean v) { ok = v; } boolean isOk() { return ok; } }\n'
                  'Result f() { return new Result(true); }\n'
                  'void write() {}\nvoid caller() { Result r = f(); if (!r.isOk()) return; write(); }\n')
        rules = {'results': [{'type': 'demo.Example$Result', 'member': 'ok', 'failure_values': [False], 'success_values': [True]}],
                 'effects': [{'method': 'demo.Example#write()', 'operation': 'DATABASE_WRITE'}]}
        candidates, _ = self.changes(before, before.replace('new Result(true)', 'new Result(false)'), semantics=rules)
        self.assertFalse(any(c['kind'] == 'RESULT_HANDLING' for c in candidates), candidates)

    def test_finally_overrides_return_and_exact_catch(self):
        _, _, states = self.requirements('String f() { try { return null; } finally { return "safe"; } }')
        self.assertTrue(all(s.value.constant == 'safe' for s in states))
        _, _, states = self.requirements('String f() { try { throw new IllegalStateException(); } catch (IllegalStateException e) { return "caught"; } }')
        self.assertTrue(all(s.value.constant == 'caught' for s in states))

    def test_state_budget_is_visible(self):
        methods = self.scan('int f(boolean p, boolean q) { int x = 0; if (p) x = 1; if (q) x = 2; return x; }')
        flow = Flow(methods, max_states=1)
        flow.run(next(m for m in methods.values() if m['name'] == 'f'))
        self.assertTrue(any(g['reason'] == 'STATE_BUDGET' for g in flow.gaps))

    def test_semantics_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'projects.json'
            path.write_text(json.dumps({'projects': [], 'analysis_semantics': {'results': [
                {'method': 'demo.Example#f()', 'failure_values': [False], 'success_values': [True]}]}}), encoding='utf-8')
            self.assertEqual(1, len(semantics_from_registry(path)['results']))
            path.write_text(json.dumps({'analysis_semantics': {'results': [
                {'method': 'f', 'failure_values': [False]}]}}), encoding='utf-8')
            with self.assertRaises(ValueError):
                semantics_from_registry(path)

    def test_sql_inventory_static_and_dynamic(self):
        writes, _ = sql_facts("UPDATE account SET status='C' WHERE id=#{id}", 'A.xml')
        self.assertEqual('C', writes[0]['value'])
        reads, _ = sql_facts("SELECT status FROM account WHERE status IN ('A','B')", 'B.xml')
        self.assertEqual(['A', 'B'], next(r['accepted'] for r in reads if r['role'] == 'DOMAIN'))
        rows, gaps = sql_facts('SELECT * FROM ${table}', 'A.xml')
        self.assertFalse(rows)
        self.assertEqual('DYNAMIC_SQL_VALUE_FLOW', gaps[0]['reason'])
        rows, _ = sql_facts('/* comment\n */\nUPDATE account SET status=1', 'A.sql')
        self.assertEqual(3, rows[0]['line'])

    def test_fixed_git_sql_persistence_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / 'repo'
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()

            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.test')
            path = repo / 'access.sql'
            java = repo / 'A.java'
            java.write_text('package demo;\nclass A {\nString f() { return "ok"; }\n'
                            'int pre(String p) { return 0; }\nint caller() { return f().length(); }\n}\n', encoding='utf-8')
            path.write_text("UPDATE account SET status='A';\nSELECT status FROM account WHERE status IN ('A','B');\n", encoding='utf-8')
            git('add', 'access.sql', 'A.java')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            path.write_text(path.read_text('utf-8').replace("status='A'", "status='C'"), encoding='utf-8')
            java.write_text(java.read_text('utf-8').replace('return "ok";', 'return null;').replace('return 0;', 'return p.length();'), encoding='utf-8')
            git('add', 'access.sql', 'A.java')
            git('commit', '-qm', 'head')
            head = git('rev-parse', 'HEAD')
            entries = {'p': {'repo': repo, 'base': base, 'head': head}}
            ledger = {'project': 'p', 'base': base, 'head': head, 'final_changes': [
                {'id': 'D-1', 'path': 'access.sql', 'old_path': 'access.sql',
                 'old_range': {'start': 1, 'count': 1}, 'new_range': {'start': 1, 'count': 1}},
                {'id': 'D-2', 'path': 'A.java', 'old_path': 'A.java',
                 'old_range': {'start': 3, 'count': 2}, 'new_range': {'start': 3, 'count': 2}}]}
            result = analyze_behavior(entries, [ledger], Path(folder) / 'cache')
            self.assertEqual({'PRECONDITION', 'RESULT_HANDLING', 'PERSISTENCE_COMPATIBILITY'}, {c['kind'] for c in result['candidates']}, result)
            again = analyze_behavior(entries, [ledger], Path(folder) / 'cache')
            self.assertEqual([c['id'] for c in result['candidates']], [c['id'] for c in again['candidates']])
            self.assertTrue(all(c['writer_sha'] in (base, head) and c['reader_sha'] in (base, head) for c in result['candidates'] if c['kind'] == 'PERSISTENCE_COMPATIBILITY'))
            from diff_analysis.release_report import write_release_bundle
            with patch('diff_analysis.maven_inventory.default_projects', return_value=([], [])):
                output, release = write_release_bundle([(repo, 'p', base, head)], [], [], Path(folder) / 'runs')
            facts = json.loads((output / 'behavior_impact_latest.json').read_text('utf-8'))
            self.assertTrue(facts['candidates'])
            review = json.loads((output / 'review_package_latest.json').read_text('utf-8'))
            self.assertTrue(any(q['kind'] == 'BEHAVIOR_IMPACT' for q in review['questions']))
            self.assertIn('读写闭环与历史兼容', (output / 'report.html').read_text('utf-8'))
            with patch('diff_analysis.maven_inventory.default_projects', return_value=([], [])), \
                    patch('diff_analysis.behavior_analysis.analyze_behavior', side_effect=RuntimeError('fixture failure')):
                output, _ = write_release_bundle([(repo, 'p', base, head)], [], [], Path(folder) / 'failed-runs')
            self.assertTrue((output / 'report.html').is_file())
            facts = json.loads((output / 'behavior_impact_latest.json').read_text('utf-8'))
            self.assertEqual('FAILED', facts['status'])
            self.assertEqual('PARTIAL_EVIDENCE', json.loads((output / 'analysis.json').read_text('utf-8'))['status'])
            coverage = json.loads((output / 'coverage_latest.json').read_text('utf-8'))
            self.assertEqual('PARTIAL', coverage['status'])
            self.assertEqual('FAILED', coverage['stages']['behavior']['status'])
            stages = json.loads((output / 'stage_manifest.json').read_text('utf-8'))
            self.assertEqual('PARTIAL', stages['state'])
            self.assertEqual('FAILED', stages['analysis_stages']['behavior'])
            self.assertEqual('COMPLETE', stages['report_stage']['status'])
            self.assertIn('阶段 behavior：FAILED', (output / 'report.html').read_text('utf-8'))
            self.assertIn('- behavior：FAILED', (output / 'report.md').read_text('utf-8'))
            self.assertIn('- behavior：FAILED', (output / 'ai_review.md').read_text('utf-8'))


if __name__ == '__main__':
    unittest.main()
