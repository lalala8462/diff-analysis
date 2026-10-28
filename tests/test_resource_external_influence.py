"""External influence must survive normalization into resource candidates."""
import unittest

from diff_analysis.resource_review import normalized_candidates


class ResourceExternalInfluenceTests(unittest.TestCase):
    def test_shared_resource_keeps_external_surface(self):
        scope = {
            "shared_resources": {
                "impacts": [{
                    "kind": "REDIS_KEY",
                    "resource": "pay:state",
                    "producer_project": "pay",
                    "consumer_project": "job",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "consumer_sha": "c" * 40,
                    "change_ids": ["D-1"],
                    "source_uses": [],
                    "consumer_uses": [],
                    "status": "STATIC_UNCONFIRMED",
                }],
                "omissions": [],
            },
            "external_influence": {
                "candidates": [{
                    "change_id": "D-1",
                    "surfaces": [{
                        "external_kind": "HTTP_ENDPOINT",
                        "priority": "HIGH",
                        "side": "head",
                        "path": "PayController.java",
                        "line": 20,
                        "method": "pay",
                        "reason": "external",
                        "evidence": "@PostMapping",
                    }],
                }],
            },
        }
        result = normalized_candidates(scope)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertTrue(candidate["externally_influenced"])
        self.assertEqual(
            candidate["external_influence"][0]["external_kind"],
            "HTTP_ENDPOINT")


if __name__ == "__main__":
    unittest.main()
