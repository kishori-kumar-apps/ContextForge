import json
import unittest

import app


class AppTests(unittest.TestCase):
    def test_expected_public_tool_endpoints_only(self):
        endpoints = set(app.demo.get_api_info()["named_endpoints"])
        self.assertEqual(endpoints, {
            "/search_project", "/record_decision", "/create_work_packet",
            "/record_implementation", "/list_decisions", "/get_work_packet",
            "/project_summary", "/update_record_status", "/health_check",
            "/backup_database",
        })

    def test_validation_error_is_structured_and_redacted(self):
        result = json.loads(app.get_work_packet("not-a-uuid"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertNotIn("Traceback", result["error"]["message"])

    def test_health_tool_is_json(self):
        result = json.loads(app.health_check())
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["storage_mode"], "self_hosted")

    def test_mcp_schema_declares_authorization_header(self):
        import asyncio
        from starlette.requests import Request

        async def schema():
            request = Request({"type": "http", "method": "GET", "path": "/",
                               "headers": [], "query_string": b""})
            response = await app.demo.mcp_server_obj.get_complete_schema(request)
            return json.loads(response.body)

        app.demo.app.setup_mcp_server(app.demo, {}, True)
        tools = asyncio.run(schema())
        self.assertTrue(tools)
        self.assertTrue(all("authorization" in tool["meta"]["headers"] for tool in tools))


if __name__ == "__main__":
    unittest.main()
