"""Static query contract regression tests."""
import unittest
from diff_analysis.query_contract import projection, selects, scan_references


class QueryContractTests(unittest.TestCase):
    def test_columns(self):
        cols, gaps = projection("SELECT id, state, SUM(total) AS amount FROM reco_record")
        self.assertEqual(cols, ["amount", "id", "state"])
        self.assertEqual(gaps, [])

    def test_mapper(self):
        xml = ('<mapper namespace="com.example.RecoMapper">'
               '<select id="queryRecoRecords">SELECT id, state FROM reco_record</select>'
               '</mapper>')
        found = selects(xml, "mapper.xml")
        self.assertEqual(found[("com.example.RecoMapper", "queryRecoRecords")]["columns"], ["id", "state"])

    def test_deleted_field_sort_reference(self):
        java = 'params.setSortName("state");'
        vue = 'sortName: "state"'
        self.assertEqual(len(scan_references(java, "Svc.java", "state")), 1)
        self.assertEqual(len(scan_references(vue, "Check.vue", "state")), 1)
        self.assertEqual(scan_references('sortName: "allState"', "Check.vue", "state"), [])

    def test_dynamic_gap(self):
        xml = ('<mapper namespace="x"><select id="query">SELECT state, '
               '<if test="x">amount,</if> id FROM reco</select></mapper>')
        self.assertIn("DYNAMIC_SQL", selects(xml, "m.xml")[("x", "query")]["gaps"])


if __name__ == "__main__":
    unittest.main()
