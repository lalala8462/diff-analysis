"""Static query contract regression tests."""
import unittest
from diff_analysis.query_contract import (
    caller_sort_evidence, controller_endpoints, endpoint_frontend_references,
    mapper_callers, mapper_declaration, projection, scan_references, selects)


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
                "package x;\n"
                "public interface RecoMapper {\n"
                "    java.util.List queryRecoRecords(BasePageQueryParams params);\n"
                "}\n"),
            "src/x/RecoService.java": (
                "package x;\n"
                "class RecoService {\n"
                "    RecoMapper recoMapper;\n"
                "    void query(BasePageQueryParams params) {\n"
                "        PageHelper.startPage(1, 20, params.getOrderBy());\n"
                "        recoMapper.queryRecoRecords(params);\n"
                "    }\n"
                "}\n"),
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

    def test_controller_http_and_frontend_binding(self):
        files = {
            "src/x/RecoMapper.java": (
                "package x;\n"
                "public interface RecoMapper {\n"
                "    java.util.List queryRecoRecords(BasePageQueryParams params);\n"
                "}\n"),
            "src/x/RecoService.java": (
                "package x;\n"
                "class RecoService extends BaseService implements IRecoService {\n"
                "    RecoMapper recoMapper;\n"
                "    public void query(BasePageQueryParams params) {\n"
                "        PageHelper.startPage(1, 20, params.getOrderBy());\n"
                "        recoMapper.queryRecoRecords(params);\n"
                "    }\n"
                "}\n"),
            "src/x/RecoController.java": (
                "package x;\n"
                "@RequestMapping(\"/reco\")\n"
                "@RestController\n"
                "class RecoController {\n"
                "    IRecoService recoService;\n"
                "    @PostMapping(\"/query\")\n"
                "    public void list(BasePageQueryParams params) {\n"
                "        recoService.query(params);\n"
                "    }\n"
                "}\n"),
        }
        declaration = mapper_declaration(
            files, "x.RecoMapper", "queryRecoRecords")
        callers = mapper_callers(files, declaration)
        endpoints = controller_endpoints(files, callers)
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["http_method"], "POST")
        self.assertEqual(endpoints[0]["url"], "/reco/query")

        snapshots = {
            ("backend", "head"): files,
            ("front", "head"): {
                "src/api/reco.js": (
                    'const url = "/reco/query";\n'
                    'const query = { sortName: "state" };\n')
            },
        }
        entries = {
            "backend": {"head": "a" * 40},
            "front": {"head": "b" * 40},
        }
        refs = endpoint_frontend_references(
            snapshots, entries, endpoints, "state")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["project"], "front")
        self.assertEqual(refs[0]["binding"], "HTTP_PATH_AND_SORT_FIELD_SAME_FILE")

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
        self.assertIn(
            "DYNAMIC_SELECT_PROJECTION",
            selects(xml, "m.xml")[("x", "query")]["gaps"])

    def test_dynamic_where_does_not_hide_static_projection(self):
        xml = (
            '<mapper namespace="x"><select id="query">'
            'SELECT state, id FROM reco '
            '<where><if test="state != null">AND state = #{state}</if></where>'
            '</select></mapper>')
        found = selects(xml, "m.xml")[("x", "query")]
        self.assertEqual(found["columns"], ["id", "state"])
        self.assertEqual(found["gaps"], [])


if __name__ == "__main__":
    unittest.main()
