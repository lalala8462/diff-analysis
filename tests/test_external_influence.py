"""Regression tests for direct external influence surfaces."""
import unittest

from diff_analysis.external_influence import (\n    classify_method, mybatis_external_surfaces, mybatis_statements,\n    one_hop_external_surfaces)
from diff_analysis.query_contract import java_method_spans


def surfaces(source):
    spans = java_method_spans(source)
    result = []
    for span in spans:
        result.extend(classify_method(source, "Example.java", span))
    return result


class ExternalInfluenceTests(unittest.TestCase):
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
