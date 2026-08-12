from pathlib import Path
import unittest


SCRIPT_PATH = (
    Path(__file__).resolve().parent
    / "smoke"
    / "test_report_api.ps1"
)


class N8NIntegrationScriptTests(unittest.TestCase):
    def test_report_api_script_uses_proxy_free_curl_with_timeout(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('"$ApiBaseUrl/health"', script)
        self.assertGreaterEqual(script.count("curl.exe"), 2)
        self.assertGreaterEqual(script.count('--noproxy "*"'), 2)
        self.assertGreaterEqual(script.count("--max-time $TimeoutSec"), 2)
        self.assertIn("Report API is unavailable", script)


if __name__ == "__main__":
    unittest.main()
