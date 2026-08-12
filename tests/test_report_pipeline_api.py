import asyncio
import unittest

from fastapi.testclient import TestClient

import report_pipeline_api


class ReportPipelineApiTests(unittest.TestCase):
    def test_feishu_token_provider_caches_token_until_refresh_window(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 0,
                    "tenant_access_token": "tenant-token-001",
                    "expire": 7200,
                }

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def post(self, url, **kwargs):
                self.calls += 1
                return FakeResponse()

        async def run():
            client = FakeClient()
            provider = report_pipeline_api.FeishuTenantTokenProvider(
                app_id="cli_test",
                app_secret="secret_test",
                http_client=client,
            )

            first = await provider()
            second = await provider()

            self.assertEqual(first, "tenant-token-001")
            self.assertEqual(second, "tenant-token-001")
            self.assertEqual(client.calls, 1)

        asyncio.run(run())

    def test_infer_version_role_from_file_name(self):
        self.assertEqual(report_pipeline_api.infer_version_role("vehicle_v1.txt"), "old")
        self.assertEqual(report_pipeline_api.infer_version_role("vehicle_v2.txt"), "new")
        self.assertEqual(report_pipeline_api.infer_version_role("整车参数_旧版.docx"), "old")
        self.assertEqual(report_pipeline_api.infer_version_role("整车参数_新版.docx"), "new")
        self.assertIsNone(report_pipeline_api.infer_version_role("vehicle.txt"))

    def test_store_waits_for_pair_then_returns_old_and_new(self):
        async def run():
            store = report_pipeline_api.VersionPairStore()
            old_payload = report_pipeline_api.N8NFilePayload(
                status="success",
                message_id="om_old",
                file_name="vehicle_v1.txt",
                extracted_text="old text",
                task_id="case-001",
            )
            new_payload = report_pipeline_api.N8NFilePayload(
                status="success",
                message_id="om_new",
                file_name="vehicle_v2.txt",
                extracted_text="new text",
                task_id="case-001",
            )

            first = await store.add(old_payload)
            second = await store.add(new_payload)

            self.assertFalse(first.ready)
            self.assertEqual(first.received_role, "old")
            self.assertTrue(second.ready)
            self.assertEqual(second.old.file_name, "vehicle_v1.txt")
            self.assertEqual(second.new.file_name, "vehicle_v2.txt")

        asyncio.run(run())

    def test_n8n_endpoint_waits_then_triggers_analyzer(self):
        async def fake_analyzer(pair):
            return {
                "file_name": pair.new.file_name,
                "change_type": "mock",
                "summary_markdown": "summary",
                "detail_diff": "diff",
                "feishu_result": {"code": 0},
            }

        app = report_pipeline_api.create_app(
            store=report_pipeline_api.VersionPairStore(),
            analyzer=fake_analyzer,
        )
        client = TestClient(app)

        first = client.post(
            "/n8n/version-file",
            json={
                "status": "success",
                "message_id": "om_old",
                "file_name": "vehicle_v1.txt",
                "extracted_text": "old text",
                "task_id": "case-002",
            },
        )
        second = client.post(
            "/n8n/version-file",
            json={
                "status": "success",
                "message_id": "om_new",
                "file_name": "vehicle_v2.txt",
                "extracted_text": "new text",
                "task_id": "case-002",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "waiting_for_pair")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "completed")
        self.assertEqual(second.json()["analysis"]["file_name"], "vehicle_v2.txt")

    def test_failed_analysis_restores_pair_for_n8n_retry(self):
        attempts = 0

        async def flaky_analyzer(pair):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary Qwen failure")
            return {"file_name": pair.new.file_name}

        app = report_pipeline_api.create_app(
            store=report_pipeline_api.VersionPairStore(),
            analyzer=flaky_analyzer,
        )
        client = TestClient(app)
        old_body = {
            "status": "success",
            "message_id": "om_old",
            "file_name": "vehicle_v1.txt",
            "extracted_text": "old text",
            "task_id": "case-retry",
        }
        new_body = {
            "status": "success",
            "message_id": "om_new",
            "file_name": "vehicle_v2.txt",
            "extracted_text": "new text",
            "task_id": "case-retry",
        }

        self.assertEqual(client.post("/n8n/version-file", json=old_body).status_code, 200)
        with self.assertLogs("report-pipeline-api", level="ERROR"):
            failed = client.post("/n8n/version-file", json=new_body)
        retried = client.post("/n8n/version-file", json=new_body)

        self.assertEqual(failed.status_code, 500)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "completed")
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
