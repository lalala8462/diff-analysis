"""Regression tests for direct external influence surfaces."""
import unittest

from diff_analysis.external_influence import classify_method
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
