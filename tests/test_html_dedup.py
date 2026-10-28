"""HTML prioritization renders each producer/consumer pair once."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_analysis.human_report import write_html


class DedupHtmlTests(unittest.TestCase):
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
