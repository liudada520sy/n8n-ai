import importlib.util
from pathlib import Path
import sys
import unittest

import httpx


SCRIPT_PATH = (
    Path(__file__).resolve().parent
    / "smoke"
    / "test_report_api.ps1"
)
PYTHON_SCRIPT_PATH = SCRIPT_PATH.with_name("report_api_smoke.py")


def load_smoke_module():
    if not PYTHON_SCRIPT_PATH.is_file():
        raise AssertionError(f"missing smoke helper: {PYTHON_SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location(
        "report_api_smoke",
        PYTHON_SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class N8NIntegrationScriptTests(unittest.TestCase):
    def test_powershell_wrapper_delegates_to_conda_python(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertNotIn("curl.exe", script)
        self.assertNotIn("Invoke-RestMethod", script)
        self.assertIn("report_api_smoke.py", script)
        self.assertIn("E:\\conda_envs\\n8n-ai\\python.exe", script)
        self.assertIn("& $PythonExecutable @arguments", script)
        self.assertIn("--timeout-sec", script)

    def test_python_smoke_uses_unique_pair_and_validates_statuses(self):
        module = load_smoke_module()
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"status": "ok"})

            payload = __import__("json").loads(request.content)
            requests.append(payload)
            if payload["version_role"] == "old":
                return httpx.Response(
                    200,
                    json={"status": "waiting_for_pair"},
                )
            return httpx.Response(
                200,
                json={"status": "completed", "analysis": {"feishu_result": {"mock": True}}},
            )

        with httpx.Client(
            transport=httpx.MockTransport(handler),
            trust_env=False,
        ) as client:
            old_result, new_result = module.run_smoke(
                project_root=Path(r"D:\n8n_AI"),
                api_base_url="http://report-api.test",
                timeout_seconds=10,
                client=client,
            )

        self.assertEqual(old_result["status"], "waiting_for_pair")
        self.assertEqual(new_result["status"], "completed")
        self.assertEqual(
            [payload["version_role"] for payload in requests],
            ["old", "new"],
        )
        self.assertEqual(requests[0]["task_id"], requests[1]["task_id"])
        self.assertTrue(requests[0]["task_id"].startswith("vehicle-smoke-"))


if __name__ == "__main__":
    unittest.main()
