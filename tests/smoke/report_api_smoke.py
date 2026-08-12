from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

import httpx


def _payload(
    *,
    file_path: Path,
    task_id: str,
    version_role: str,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    return {
        "status": "success",
        "event_id": f"evt-{version_role}-{request_id}",
        "message_id": f"om-{version_role}-{request_id}",
        "file_name": file_path.name,
        "extracted_text": file_path.read_text(encoding="utf-8"),
        "error": None,
        "task_id": task_id,
        "version_role": version_role,
    }


def run_smoke(
    *,
    project_root: Path,
    api_base_url: str,
    timeout_seconds: int,
    task_id: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    active_task_id = task_id or f"vehicle-smoke-{uuid.uuid4().hex}"
    test_data = project_root / "test-data"
    old_path = test_data / "vehicle_spec_v1.txt"
    new_path = test_data / "vehicle_spec_v2.txt"
    for path in (old_path, new_path):
        if not path.is_file():
            raise FileNotFoundError(f"Test file was not found: {path}")

    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=timeout_seconds,
        trust_env=False,
    )
    try:
        health = active_client.get(f"{api_base_url.rstrip('/')}/health")
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise RuntimeError(f"Unexpected health response: {health.text}")

        print("Sending the old version...")
        old_response = active_client.post(
            f"{api_base_url.rstrip('/')}/n8n/version-file",
            json=_payload(
                file_path=old_path,
                task_id=active_task_id,
                version_role="old",
            ),
        )
        old_response.raise_for_status()
        old_result = old_response.json()
        print(f"status: {old_result.get('status')}")
        print(json.dumps(old_result, ensure_ascii=False, indent=2))

        print("Sending the new version...")
        new_response = active_client.post(
            f"{api_base_url.rstrip('/')}/n8n/version-file",
            json=_payload(
                file_path=new_path,
                task_id=active_task_id,
                version_role="new",
            ),
        )
        new_response.raise_for_status()
        new_result = new_response.json()
        print(f"status: {new_result.get('status')}")
        print(json.dumps(new_result, ensure_ascii=False, indent=2))

        if old_result.get("status") != "waiting_for_pair":
            raise RuntimeError(
                f"Unexpected old-version response: {old_result.get('status')}"
            )
        if new_result.get("status") != "completed":
            raise RuntimeError(
                f"New-version analysis did not complete: {new_result.get('status')}"
            )
        mock_result = (
            new_result.get("analysis", {})
            .get("feishu_result", {})
            .get("mock")
        )
        if mock_result is not True:
            raise RuntimeError("Report API did not return a Mock-mode result")

        print("report_pipeline_api pairing and analysis smoke test passed.")
        return old_result, new_result
    finally:
        if owns_client:
            active_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test report_pipeline_api")
    parser.add_argument("--project-root", type=Path, default=Path(r"D:\n8n_AI"))
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8090",
    )
    parser.add_argument("--task-id")
    parser.add_argument("--timeout-sec", type=int, default=10)
    args = parser.parse_args()

    try:
        run_smoke(
            project_root=args.project_root,
            api_base_url=args.api_base_url,
            timeout_seconds=args.timeout_sec,
            task_id=args.task_id,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(f"Report API request failed: {exc}") from exc
    except (FileNotFoundError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
