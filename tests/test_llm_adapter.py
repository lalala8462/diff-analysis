"""Optional model adapter must be opt-in and evidence-bound."""
import json
import unittest
from unittest.mock import patch

from diff_analysis.llm_adapter import (
    validate_model_endpoint, run_model, ModelError)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, max_bytes):
        return self.payload


class _Opener:
    def __init__(self, response):
        self.response = response
    def open(self, req, timeout):
        return self.response


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "group_id": "G-1", "facts": [{"diff_id": "D-1"}],
            "unprovided_change_ids": [], "task": "explain"
        }

    def test_no_remote_upload_without_opt_in(self):
        with self.assertRaisesRegex(ModelError, "requires HTTPS"):
            validate_model_endpoint("https://example.com/v1/chat/completions")
        with self.assertRaisesRegex(ModelError, "requires HTTPS"):
            validate_model_endpoint("http://example.com/v1/chat/completions",
                                    allow_remote=True)
        self.assertEqual(
            "http://127.0.0.1:1234/v1/chat/completions",
            validate_model_endpoint("http://127.0.0.1:1234/v1/chat/completions"))
        self.assertEqual(
            "https://example.com/v1/chat/completions",
            validate_model_endpoint("https://example.com/v1/chat/completions",
                                    allow_remote=True))

    def test_valid_model_result_remains_hypothesis(self):
        obj = {"possible_requirement": "可能调整支付状态",
               "possible_impacts": [{"description": "核实退款判断",
                                     "diff_ids": ["D-1"],
                                     "confidence": "CERTAIN"}]}
        response = {"choices": [{"message": {"content": json.dumps(obj)}}]}
        with patch("diff_analysis.llm_adapter.urllib.request.build_opener",
                   return_value=_Opener(_Response(response))):
            result = run_model(self.payload,
                "http://127.0.0.1:1234/v1/chat/completions", "local-model")
        self.assertEqual("MODEL_HYPOTHESIS", result["status"])
        self.assertEqual("HYPOTHESIS",
                         result["output"]["possible_impacts"][0]["confidence"])
        self.assertTrue(result["output"]["needs_human_confirmation"])

    def test_unknown_diff_id_is_rejected(self):
        obj = {"possible_impacts": [{"description": "claim", "diff_ids": ["D-fake"]}]}
        response = {"choices": [{"message": {"content": json.dumps(obj)}}]}
        with patch("diff_analysis.llm_adapter.urllib.request.build_opener",
                   return_value=_Opener(_Response(response))):
            with self.assertRaisesRegex(ModelError, "nonexistent"):
                run_model(self.payload,
                    "http://127.0.0.1:1234/v1/chat/completions", "local-model")


if __name__ == "__main__":
    unittest.main()
