"""Regression tests for direct external influence surfaces."""
import unittest
from unittest.mock import patch

from diff_analysis.external_influence import (
    classify_method, mybatis_external_surfaces, mybatis_statements,
    one_hop_external_surfaces, method_input_influence, analyze_external_influence)
from diff_analysis.query_contract import java_method_spans


def surfaces(source):
    spans = java_method_spans(source)
    result = []
    for span in spans:
        result.extend(classify_method(source, "Example.java", span))
    return result


class ExternalInfluenceTests(unittest.TestCase):
    def assess(self, body, changed, parameters='Request req'):
        source = 'class Example {\n  public int run(' + parameters + ') {\n' + body + '\n  }\n}\n'
        line = next(n for n, value in enumerate(source.splitlines(), 1) if changed in value)
        return method_input_influence(source, 'Example.java', java_method_spans(source)[0],
                                      {'start': line, 'count': 1})

    def test_changed_value_uses_caller_parameter(self):
        result = self.assess('    return req.getAmount();', 'return')
        self.assertEqual('HIGH', result['level'])
        self.assertEqual(['req'], result['parameters'])

    def test_local_assignment_propagates_parameter_to_changed_return(self):
        result = self.assess('    int amount = req.getAmount();\n    return amount + 1;', 'return')
        self.assertEqual('HIGH', result['level'])
        self.assertTrue(any('req.getAmount()' in ref['evidence'] for ref in result['evidence']))

    def test_parameter_controls_changed_branch_result(self):
        result = self.assess('    if (req.isEnabled()) {\n      return 1;\n    }\n    return 0;', 'return 1')
        self.assertEqual('HIGH', result['level'])
        self.assertTrue(any('req.isEnabled()' in ref['evidence'] for ref in result['evidence']))

    def test_parameter_controls_changed_else_result(self):
        result = self.assess('    if (req.isEnabled()) {\n      return 1;\n    } else {\n      return 0;\n    }', 'return 0')
        self.assertEqual('HIGH', result['level'])

    def test_outer_parameter_controls_changed_else_if_result(self):
        result = self.assess('    if (req.isEnabled()) {\n      return 1;\n    } else if (true) {\n      return 2;\n    }', 'return 2')
        self.assertEqual('HIGH', result['level'])

    def test_unused_parameter_does_not_raise_internal_literal_change(self):
        result = self.assess('    return 3;', 'return')
        self.assertEqual('LOW', result['level'])

    def test_internal_string_literal_propagates_to_return(self):
        source = 'class C {\n public String name() {\n String value = "fixed";\n return value;\n }\n}'
        result = method_input_influence(source, 'C.java', java_method_spans(source)[0],
                                        {'start': 4, 'count': 1})
        self.assertEqual('LOW', result['level'])

    def test_overwritten_alias_no_longer_depends_on_input(self):
        result = self.assess('    int amount = req.getAmount();\n    amount = 7;\n    return amount;', 'return')
        self.assertEqual('LOW', result['level'])

    def test_parameter_controlled_assignment_reaches_later_return(self):
        result = self.assess('    int amount = 0;\n    if (req.isEnabled()) {\n      amount = 7;\n    }\n    return amount;', 'return')
        self.assertEqual('HIGH', result['level'])

    def test_unknown_condition_does_not_make_literal_branch_low(self):
        result = self.assess('    if (config.isEnabled()) {\n      return 1;\n    }\n    return 0;', 'return 1', parameters='')
        self.assertEqual('MEDIUM', result['level'])

    def test_internal_fixed_condition_remains_low(self):
        result = self.assess('    if (true) {\n      return 1;\n    }\n    return 0;', 'return 1', parameters='')
        self.assertEqual('LOW', result['level'])

    def test_member_name_does_not_bind_same_named_parameter(self):
        result = self.assess('    return config.req();', 'return')
        self.assertEqual('MEDIUM', result['level'])

    def test_current_internal_change_is_low_even_with_http_surface(self):
        old = 'class C {\n @PostMapping("/run")\n public int run(Request req) {\n return req.getValue();\n }\n}'
        new = old.replace('req.getValue()', '7')
        ledger = {'project': 'p', 'base': 'a', 'head': 'b', 'final_changes': [{
            'id': 'D-1', 'path': 'C.java', 'old_path': 'C.java',
            'old_range': {'start': 4, 'count': 1}, 'new_range': {'start': 4, 'count': 1}}]}
        with patch('diff_analysis.external_influence._snapshot', side_effect=[
                ({'C.java': old}, []), ({'C.java': new}, [])]):
            result = analyze_external_influence({'p': {'repo': '.'}}, [ledger], '.')
        candidate = result['candidates'][0]
        self.assertEqual('LOW', candidate['priority'])
        self.assertFalse(candidate['requires_confirmation'])
        self.assertTrue(candidate['surfaces'])

    def test_caller_parameter_is_high_without_known_http_surface(self):
        source = 'class C {\n public int run(Request req) {\n return req.getValue();\n }\n}'
        ledger = {'project': 'p', 'base': 'a', 'head': 'b', 'final_changes': [{
            'id': 'D-1', 'path': 'C.java', 'old_path': 'C.java',
            'old_range': {'start': 3, 'count': 1}, 'new_range': {'start': 3, 'count': 1}}]}
        with patch('diff_analysis.external_influence._snapshot', return_value=({'C.java': source}, [])):
            result = analyze_external_influence({'p': {'repo': '.'}}, [ledger], '.')
        self.assertEqual('HIGH', result['candidates'][0]['priority'])
        self.assertTrue(result['candidates'][0]['requires_confirmation'])

    def test_unresolved_call_is_medium_even_without_endpoint(self):
        result = self.assess('    return config.getLimit();', 'return', parameters='')
        self.assertEqual('MEDIUM', result['level'])

    def test_signature_only_change_is_not_parameter_flow(self):
        result = self.assess('    return req.getAmount();', 'public int')
        self.assertEqual('MEDIUM', result['level'])

    def test_http_endpoint_is_external_surface(self):
        source = (
            "class RecoController {\n"
            "  @PostMapping(\"/query\")\n"
            "  public void query(QueryParams params) { run(params); }\n"
            "}\n")
        found = surfaces(source)
        self.assertTrue(any(
            item["external_kind"] == "HTTP_ENDPOINT" for item in found))

    def test_plain_service_method_is_not_direct_external_surface(self):
        source = (
            "class RecoService {\n"
            "  public void query(QueryParams params) { run(params); }\n"
            "}\n")
        self.assertEqual(surfaces(source), [])

    def test_dubbo_public_method_is_external_surface(self):
        source = (
            "import com.alibaba.dubbo.config.annotation.Service;\n"
            "@Service\n"
            "class PaymentService {\n"
            "  public void pay(Request req) { execute(req); }\n"
            "}\n")
        found = surfaces(source)
        self.assertTrue(any(
            item["external_kind"] == "DUBBO_RPC_SERVICE" for item in found))

    def test_same_class_http_method_calls_changed_helper(self):
        source = (
            "class RecoController {\n"
            "  @PostMapping(\"/query\")\n"
            "  public void query(QueryParams params) { normalize(params); }\n"
            "  private void normalize(QueryParams params) { params.trim(); }\n"
            "}\n")
        spans = {
            span["name"]: span for span in java_method_spans(source)}
        found = one_hop_external_surfaces(
            {"RecoController.java": source}, source,
            "RecoController.java", spans["normalize"])
        self.assertTrue(any(
            item["external_kind"] == "HTTP_ENDPOINT" and
            item["binding"] == "ONE_LOCAL_CALLER_HOP" and
            item["priority"] == "MEDIUM"
            for item in found))

    def test_one_typed_service_hop_to_http_is_external_influence(self):
        service = (
            "class RecoService implements IRecoService {\n"
            "  public void query(QueryParams params) { run(params); }\n"
            "}\n")
        controller = (
            "class RecoController {\n"
            "  IRecoService recoService;\n"
            "  @PostMapping(\"/query\")\n"
            "  public void list(QueryParams params) { recoService.query(params); }\n"
            "}\n")
        files = {
            "RecoService.java": service,
            "RecoController.java": controller,
        }
        span = java_method_spans(service)[0]
        found = one_hop_external_surfaces(
            files, service, "RecoService.java", span)
        self.assertTrue(any(
            item["external_kind"] == "HTTP_ENDPOINT" and
            item["binding"] == "ONE_TYPED_CALLER_HOP"
            for item in found))

    def test_mybatis_statement_one_hop_to_http(self):
        files = {
            "RecoMapper.java": (
                "package x;\n"
                "public interface RecoMapper {\n"
                "  void queryRecords(QueryParams params);\n"
                "}\n"),
            "RecoService.java": (
                "package x;\n"
                "class RecoService implements IRecoService {\n"
                "  RecoMapper mapper;\n"
                "  public void query(QueryParams params) { mapper.queryRecords(params); }\n"
                "}\n"),
            "RecoController.java": (
                "package x;\n"
                "class RecoController {\n"
                "  IRecoService recoService;\n"
                "  @PostMapping(\"/query\")\n"
                "  public void list(QueryParams params) { recoService.query(params); }\n"
                "}\n"),
        }
        xml = (
            '<mapper namespace="x.RecoMapper">'
            '<select id="queryRecords">SELECT id FROM reco</select>'
            '</mapper>')
        statement = mybatis_statements(xml, "RecoMapper.xml")[0]
        found = mybatis_external_surfaces(files, statement)
        self.assertTrue(any(
            item["external_kind"] == "HTTP_ENDPOINT" and
            item["binding"] == "MYBATIS_ONE_TYPED_CALLER_HOP"
            for item in found))

    def test_mq_listener_is_external_surface(self):
        source = (
            "class Listener {\n"
            "  @RabbitListener(queues = \"reco\")\n"
            "  public void consume(Message msg) { handle(msg); }\n"
            "}\n")
        found = surfaces(source)
        self.assertTrue(any(
            item["external_kind"] == "MQ_INBOUND" for item in found))

    def test_class_level_rocketmq_listener_marks_changed_method(self):
        source = (
            "@RocketMQMessageListener(topic = \"pay\", consumerGroup = \"g\")\n"
            "class PayListener {\n"
            "  public void onMessage(String msg) { handle(msg); }\n"
            "}\n")
        found = surfaces(source)
        self.assertTrue(any(
            item["external_kind"] == "MQ_INBOUND" for item in found))


if __name__ == "__main__":
    unittest.main()
