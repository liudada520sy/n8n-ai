import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import numpy as np


MODULE_PATH = Path(r"D:\n8n_AI\report_pipeline.py")
SPEC = importlib.util.spec_from_file_location("report_pipeline", MODULE_PATH)
report_pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report_pipeline
SPEC.loader.exec_module(report_pipeline)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.response_payload)


class ReportPipelineTests(unittest.TestCase):
    def test_numpy_scalars_are_compared_without_false_positive(self):
        self.assertTrue(
            report_pipeline._values_equal(np.int64(7), np.int64(7))
        )
        self.assertTrue(report_pipeline._is_missing(np.float64("nan")))

    def test_compare_excel_detects_structural_and_cell_changes(self):
        old = pd.DataFrame(
            [
                {"零件号": "A", "平台": "P1", "供应商": "甲", "数量": 2, "旧备注": "保留"},
                {"零件号": "B", "平台": "P1", "供应商": "乙", "数量": 1, "旧备注": "删除"},
            ]
        )
        new = pd.DataFrame(
            [
                {"零件号": "A", "平台": "P1", "供应商": "丙", "数量": 3, "新状态": "量产"},
                {"零件号": "C", "平台": "P1", "供应商": "丁", "数量": 4, "新状态": "试制"},
            ]
        )

        with patch.object(
            report_pipeline.pd,
            "read_excel",
            side_effect=[
                {
                    "BOM": old,
                    "历史Sheet": pd.DataFrame([{"零件号": "Z", "数量": 1}]),
                },
                {
                    "BOM": new,
                    "新增Sheet": pd.DataFrame([{"零件号": "N", "数量": 5}]),
                },
            ],
        ):
            result = report_pipeline.compare_excel(Path("old.xlsx"), Path("new.xlsx"))

        sheet = result["sheets"]["BOM"]
        self.assertEqual(sheet["key_column"], "零件号")
        self.assertEqual(sheet["added_columns"], ["新状态"])
        self.assertEqual(sheet["deleted_columns"], ["旧备注"])
        self.assertEqual(sheet["added_rows"][0]["row"], "C")
        self.assertEqual(sheet["deleted_rows"][0]["row"], "B")
        coordinates = {item["coordinate"] for item in sheet["modified_cells"]}
        self.assertIn("[行: A, 列: 供应商]", coordinates)
        self.assertIn("[行: A, 列: 数量]", coordinates)
        self.assertEqual(result["summary"]["modified_cells"], 2)
        self.assertEqual(result["sheets"]["新增Sheet"]["status"], "added")
        self.assertEqual(result["sheets"]["新增Sheet"]["added_rows"][0]["row"], "N")
        self.assertEqual(result["sheets"]["历史Sheet"]["status"], "deleted")
        self.assertEqual(result["sheets"]["历史Sheet"]["deleted_rows"][0]["row"], "Z")
        card_detail = report_pipeline.format_excel_diff_markdown(result)
        self.assertIn("🟢 **整个 Sheet 为新增**", card_detail)
        self.assertIn("[行: A, 列: 供应商]", card_detail)

    def test_qwen_summarizer_uses_openai_compatible_payload(self):
        async def run():
            client = FakeHttpClient(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "【变更评级 (高/中/低)】\n中\n"
                                "【核心工程变更清单】\n- 供应商变更\n"
                                "【潜在影响提示】\n- 复核验证"
                            }
                        }
                    ]
                }
            )
            summarizer = report_pipeline.QwenSummarizer(
                base_url="http://qwen.local/v1",
                api_key="secret",
                model="qwen3.6",
                http_client=client,
            )
            result = await summarizer.summarize('{"modified_cells":[]}', "BOM.xlsx", "excel")
            self.assertIn("【核心工程变更清单】", result)
            url, request = client.calls[0]
            self.assertEqual(url, "http://qwen.local/v1/chat/completions")
            self.assertEqual(request["json"]["model"], "qwen3.6")
            self.assertIn("汽车总体工程师", request["json"]["messages"][0]["content"])
            self.assertEqual(request["headers"]["Authorization"], "Bearer secret")

        asyncio.run(run())

    def test_feishu_card_v2_and_reply_payload(self):
        async def run():
            client = FakeHttpClient({"code": 0, "msg": "success", "data": {"message_id": "om_reply"}})
            sender = report_pipeline.FeishuCardSender(
                tenant_access_token="tenant-token",
                http_client=client,
                max_detail_chars=20,
            )
            card = sender.build_card(
                file_name="BOM.xlsx",
                summary_markdown="【变更评级 (高/中/低)】\n高",
                detail_diff="0123456789" * 5,
            )
            self.assertEqual(card["schema"], "2.0")
            self.assertEqual(card["body"]["elements"][2]["tag"], "collapsible_panel")
            detail = card["body"]["elements"][2]["elements"][0]["content"]
            self.assertIn("已截断", detail)

            await sender.reply_card("om_source", card)
            url, request = client.calls[0]
            self.assertTrue(url.endswith("/im/v1/messages/om_source/reply"))
            self.assertEqual(request["json"]["msg_type"], "interactive")
            self.assertEqual(json.loads(request["json"]["content"])["schema"], "2.0")

        asyncio.run(run())

    def test_run_pipeline_connects_normal_document_branch(self):
        class MockComparator:
            async def compare(self, old_text, new_text):
                self.received = (old_text, new_text)
                return "参数由 <del>100</del> 修改为 <ins>120</ins>"

        class MockSummarizer:
            async def summarize(self, detail, file_name, change_type):
                self.received = (detail, file_name, change_type)
                return (
                    "【变更评级 (高/中/低)】\n高\n"
                    "【核心工程变更清单】\n- 功率变化\n"
                    "【潜在影响提示】\n- 重新验证"
                )

        async def run():
            fixture_dir = Path.cwd() / "work" / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            old_path = fixture_dir / "old.docx"
            new_path = fixture_dir / "new.docx"
            old_path.touch()
            new_path.touch()
            self.addCleanup(old_path.unlink, missing_ok=True)
            self.addCleanup(new_path.unlink, missing_ok=True)

            async def extractor(path):
                return "旧参数 100" if path == old_path else "新参数 120"

            comparator = MockComparator()
            summarizer = MockSummarizer()
            client = FakeHttpClient(
                {"code": 0, "msg": "success", "data": {"message_id": "om_reply"}}
            )
            sender = report_pipeline.FeishuCardSender(
                tenant_access_token="tenant-token",
                http_client=client,
            )
            result = await report_pipeline.run_pipeline(
                old_path,
                new_path,
                "om_source",
                document_comparator=comparator,
                qwen_summarizer=summarizer,
                card_sender=sender,
                text_extractor=extractor,
            )

            self.assertIn("普通文档", result["change_type"])
            self.assertEqual(comparator.received, ("旧参数 100", "新参数 120"))
            self.assertEqual(result["feishu_result"]["code"], 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
