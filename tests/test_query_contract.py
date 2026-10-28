"""Static query contract regression tests."""
import unittest
from diff_analysis.query_contract import (
    caller_sort_evidence, mapper_callers, mapper_declaration, projection,
    scan_references, selects)


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

    def test_nested_from_and_function_commas(self):
        sql = ("SELECT (SELECT max(v) FROM inner_t) AS state, "
               "concat(a, b) AS label, id FROM outer_t")
        self.assertEqual(projection(sql), (["id", "label", "state"], []))

    def test_non_projected_column_is_not_sql_failure_proof(self):
        old, _ = projection("SELECT state, id FROM records")
        new, gaps = projection("SELECT id FROM records ORDER BY state DESC")
        self.assertEqual(set(old) - set(new), {"state"})
        self.assertEqual(gaps, [])

    def test_expression_without_alias_is_uncertain(self):
        self.assertIn("WILDCARD_OR_COMPLEX_EXPRESSION",
                      projection("SELECT CASE WHEN ok THEN 1 ELSE 0 END FROM t")[1])

    def test_literal_pagehelper_sort(self):
        code = 'PageHelper.startPage(page, 10, "state desc");'
        self.assertEqual(len(scan_references(code, "Svc.java", "state")), 1)

    def test_implicit_alias(self):
        self.assertEqual(
            projection("SELECT state stateAlias, id FROM records"),
            (["id", "statealias"], []))

    def test_vue_template_prop(self):
        vue = '<el-table-column prop="state" sortable="custom" />'
        found = scan_references(vue, "Check.vue", "state")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "FRONTEND_SORT_FIELD")

    def test_mapper_to_caller_and_dynamic_sort_binding(self):
        files = {
            "src/x/RecoMapper.java": (
                "package x; public interface RecoMapper { "
                "java.util.List queryRecoRecords(BasePageQueryParams params); }"),
            "src/x/RecoService.java": (
                "package x; class RecoService { RecoMapper recoMapper; "
                "void query(BasePageQueryParams params) { "
                "PageHelper.startPage(1, 20, params.getOrderBy()); "
                "recoMapper.queryRecoRecords(params); } }"),
        }
        declaration = mapper_declaration(
            files, "x.RecoMapper", "queryRecoRecords")
        self.assertIsNotNone(declaration)
        callers = mapper_callers(files, declaration)
        self.assertEqual(len(callers), 1)
        self.assertEqual(callers[0]["caller_method"], "query")
        direct, dynamic = caller_sort_evidence(
            files["src/x/RecoService.java"], "src/x/RecoService.java",
            callers[0], "state")
        self.assertEqual(direct, [])
        self.assertTrue(any(
            item["kind"] == "DYNAMIC_GET_ORDER_BY" and
            item["receiver_type"] == "BasePageQueryParams"
            for item in dynamic))
        self.assertTrue(any(
            item["kind"] == "PAGEHELPER_DYNAMIC_SORT"
            for item in dynamic))

    def test_mapper_namespace_must_match_java_type(self):
        files = {
            "RecoMapper.java": (
                "package x; public interface RecoMapper { "
                "void queryRecoRecords(); }")
        }
        self.assertIsNone(mapper_declaration(
            files, "other.RecoMapper", "queryRecoRecords"))

    def test_dynamic_gap(self):
        xml = ('<mapper namespace="x"><select id="query">SELECT state, '
               '<if test="x">amount,</if> id FROM reco</select></mapper>')
        self.assertIn("DYNAMIC_SQL", selects(xml, "m.xml")[("x", "query")]["gaps"])


if __name__ == "__main__":
    unittest.main()
