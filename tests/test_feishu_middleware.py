import asyncio
import base64
import hashlib
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from tests._unittest_compat import load_function_tests, pytest


APP_PATH = Path(r"D:\n8n_AI\feishu-middleware\app.py")


def load_module():
    assert APP_PATH.exists(), f"missing implementation: {APP_PATH}"
    spec = importlib.util.spec_from_file_location("feishu_middleware", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_get_tenant_access_token_uses_internal_app_credentials(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(module, "FEISHU_APP_SECRET", "secret_test")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/v3/tenant_access_token/internal")
        assert json.loads(request.content) == {
            "app_id": "cli_test",
            "app_secret": "secret_test",
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "t-test",
                "expire": 7200,
            },
        )

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await module.get_tenant_access_token(client)

    token = asyncio.run(run_test())
    assert token == "t-test"


def test_authentication_failure_raises_typed_error(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(module, "FEISHU_APP_SECRET", "bad_secret")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 10003, "msg": "invalid app secret"})

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await module.get_tenant_access_token(client)

    with pytest.raises(module.FeishuAuthError):
        asyncio.run(run_test())


def test_download_message_file_writes_binary_content():
    module = load_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/im/v1/messages/om_test/resources/file_test"
        )
        assert request.url.params["type"] == "file"
        assert request.headers["authorization"] == "Bearer t-test"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-test",
        )

    async def run_test(destination):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await module.download_message_file(
                client,
                "t-test",
                "om_test",
                "file_test",
                destination,
            )

    destination = Path("work/test-downloaded-vehicle-spec.pdf")
    try:
        content_type = asyncio.run(run_test(destination))
        assert destination.read_bytes() == b"%PDF-test"
        assert content_type == "application/pdf"
    finally:
        destination.unlink(missing_ok=True)


def test_tika_extracts_plain_text():
    module = load_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/tika"
        assert request.headers["accept"] == "text/plain"
        assert request.headers["content-type"] == "application/pdf"
        return httpx.Response(200, text="parsed vehicle specification")

    async def run_test(source):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await module.extract_text_with_tika(
                client,
                source,
                "application/pdf",
            )

    source = Path("work/test-tika-source.pdf")
    try:
        source.write_bytes(b"%PDF-test")
        text = asyncio.run(run_test(source))
        assert text == "parsed vehicle specification"
    finally:
        source.unlink(missing_ok=True)


def test_unsupported_file_type_is_rejected():
    module = load_module()

    with pytest.raises(module.UnsupportedFileTypeError):
        module.ensure_supported_file_type("payload.exe")


def test_file_event_fields_are_extracted_from_receive_v1_payload():
    module = load_module()
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "event-test",
            "event_type": "im.message.receive_v1",
            "token": "verification-token",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "union_id": "on_test",
                    "user_id": "user_test",
                    "open_id": "ou_test",
                },
                "sender_type": "user",
                "tenant_key": "tenant_test",
            },
            "message": {
                "message_id": "om_test",
                "root_id": "",
                "parent_id": "",
                "create_time": "1609459200000",
                "chat_id": "oc_test",
                "chat_type": "p2p",
                "message_type": "file",
                "content": json.dumps(
                    {"file_key": "file_test", "file_name": "vehicle-spec.pdf"}
                ),
            },
        },
    }

    file_event = module.extract_file_event(payload)

    assert file_event == (
        "event-test",
        "om_test",
        "file_test",
        "vehicle-spec.pdf",
    )


def test_tenant_access_token_is_reused_until_refresh_window(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(module, "FEISHU_APP_SECRET", "secret_test")
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "t-cached",
                "expire": 7200,
            },
        )

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            first = await module.get_tenant_access_token(client)
            second = await module.get_tenant_access_token(client)
            return first, second

    assert asyncio.run(run_test()) == ("t-cached", "t-cached")
    assert request_count == 1


def test_duplicate_event_id_is_claimed_once():
    module = load_module()

    async def run_test():
        first = await module.claim_event_once("event-duplicate")
        second = await module.claim_event_once("event-duplicate")
        return first, second

    assert asyncio.run(run_test()) == (True, False)


def encrypt_feishu_payload(payload: dict, encrypt_key: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = b"0123456789abcdef"
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    plaintext = json.dumps(payload).encode("utf-8")
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def test_encrypted_url_verification_payload_is_decrypted(monkeypatch):
    module = load_module()
    encrypt_key = "test-encrypt-key"
    verification_token = "verification-token"
    encrypted = encrypt_feishu_payload(
        {
            "type": "url_verification",
            "token": verification_token,
            "challenge": "challenge-test",
        },
        encrypt_key,
    )
    monkeypatch.setattr(module, "FEISHU_ENCRYPT_KEY", encrypt_key)
    monkeypatch.setattr(
        module,
        "FEISHU_VERIFICATION_TOKEN",
        verification_token,
    )

    with TestClient(module.app) as client:
        response = client.post(
            "/webhooks/feishu",
            json={"encrypt": encrypted},
        )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-test"}


def test_background_processing_forwards_text_to_n8n_and_replies(monkeypatch):
    module = load_module()
    async_client_class = httpx.AsyncClient
    monkeypatch.setattr(module, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(module, "FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setattr(module, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/file")
    monkeypatch.setattr(module, "TIKA_BASE_URL", "https://tika.test")
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda **_: nullcontext("work"))
    n8n_payloads = []
    reply_texts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "t-test",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("/im/v1/messages/om_test/reply"):
            body = json.loads(request.content)
            reply_texts.append(json.loads(body["content"])["text"])
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"message_id": "om_reply"}},
            )
        if request.method == "GET" and "/resources/file_test" in request.url.path:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-test",
            )
        if request.url.host == "tika.test":
            return httpx.Response(200, text="parsed engineering document")
        if request.url.host == "n8n.test":
            n8n_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run_test():
        async with async_client_class(
            transport=httpx.MockTransport(handler)
        ) as client:
            monkeypatch.setattr(
                module.httpx,
                "AsyncClient",
                lambda **_: async_client_class(
                    transport=httpx.MockTransport(handler)
                ),
            )
            return await module.process_message_file(
                "event-test",
                "om_test",
                "file_test",
                "process-flow.pdf",
                client=client,
            )

    downloaded_file = Path("work/process-flow.pdf")
    try:
        result = asyncio.run(run_test())
    finally:
        downloaded_file.unlink(missing_ok=True)

    assert result is None
    assert reply_texts == [
        "收到文件 process-flow.pdf，正在提取文本...",
        "文件 process-flow.pdf 文本提取完成，已提交等待后续处理。",
    ]
    assert n8n_payloads == [
        {
            "status": "success",
            "event_id": "event-test",
            "message_id": "om_test",
            "file_name": "process-flow.pdf",
            "extracted_text": "parsed engineering document",
            "error": None,
        }
    ]


def test_processing_failure_replies_with_reason_and_notifies_n8n(monkeypatch):
    module = load_module()
    async_client_class = httpx.AsyncClient
    monkeypatch.setattr(module, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(module, "FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setattr(module, "N8N_WEBHOOK_URL", "https://n8n.test/webhook/file")
    n8n_payloads = []
    reply_texts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "t-test",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("/im/v1/messages/om_test/reply"):
            body = json.loads(request.content)
            reply_texts.append(json.loads(body["content"])["text"])
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"message_id": "om_reply"}},
            )
        if request.url.host == "n8n.test":
            n8n_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run_test():
        async with async_client_class(
            transport=httpx.MockTransport(handler)
        ) as client:
            monkeypatch.setattr(
                module.httpx,
                "AsyncClient",
                lambda **_: async_client_class(
                    transport=httpx.MockTransport(handler)
                ),
            )
            await module.process_message_file(
                "event-test",
                "om_test",
                "file_test",
                "payload.exe",
                client=client,
            )

    asyncio.run(run_test())

    assert reply_texts[0] == "收到文件 payload.exe，正在提取文本..."
    assert "不支持的文件类型" in reply_texts[1]
    assert n8n_payloads[0]["status"] == "failed"
    assert n8n_payloads[0]["message_id"] == "om_test"
    assert n8n_payloads[0]["file_name"] == "payload.exe"
    assert "不支持的文件类型" in n8n_payloads[0]["error"]


def load_tests(_loader, _tests, _pattern):
    return load_function_tests(globals())
