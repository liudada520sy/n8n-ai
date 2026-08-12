import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import BackgroundTasks, FastAPI, HTTPException


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-middleware")

FEISHU_BASE_URL = os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn/open-apis")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
TIKA_BASE_URL = os.getenv("TIKA_BASE_URL", "http://tika:9998")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(100 * 1024 * 1024)))
CHUNK_SIZE = 1024 * 1024
TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60
EVENT_DEDUP_TTL_SECONDS = float(
    os.getenv("EVENT_DEDUP_TTL_SECONDS", "600")
)

# Token 为进程内缓存；多 worker 部署时每个 worker 各自维护一份。
_tenant_access_token: str | None = None
_tenant_access_token_expires_at = 0.0
_tenant_access_token_lock = asyncio.Lock()

# 飞书事件可能重复投递；MVP 单进程内按 event_id 做 TTL 去重。
_processed_event_ids: dict[str, float] = {}
_processed_event_ids_lock = asyncio.Lock()

SUPPORTED_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
}


class FeishuAuthError(RuntimeError):
    pass


class UnsupportedFileTypeError(ValueError):
    pass


class FeishuReplyError(RuntimeError):
    pass


async def claim_event_once(event_id: str) -> bool:
    """首次接收事件返回 True；TTL 内重复事件返回 False。"""
    now = time.monotonic()
    async with _processed_event_ids_lock:
        expired_event_ids = [
            stored_event_id
            for stored_event_id, expires_at in _processed_event_ids.items()
            if expires_at <= now
        ]
        for expired_event_id in expired_event_ids:
            _processed_event_ids.pop(expired_event_id, None)

        if event_id in _processed_event_ids:
            return False

        _processed_event_ids[event_id] = (
            now + EVENT_DEDUP_TTL_SECONDS
        )
        return True


def ensure_supported_file_type(file_name: str) -> None:
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"unsupported file type: {suffix or '<no extension>'}"
        )


def verify_webhook_token(received_token: str | None) -> None:
    if not FEISHU_VERIFICATION_TOKEN:
        logger.error("FEISHU_VERIFICATION_TOKEN is not configured")
        raise HTTPException(status_code=503, detail="webhook token is not configured")

    if not received_token or not hmac.compare_digest(
        received_token,
        FEISHU_VERIFICATION_TOKEN,
    ):
        logger.warning("rejected webhook with an invalid verification token")
        raise HTTPException(status_code=403, detail="invalid verification token")


def decrypt_feishu_payload(encrypted_payload: str) -> dict[str, Any]:
    """按飞书 AES-256-CBC 规则解密 encrypt 字段。"""
    try:
        encrypted_bytes = base64.b64decode(encrypted_payload, validate=True)
        if (
            len(encrypted_bytes) <= 16
            or (len(encrypted_bytes) - 16) % 16 != 0
        ):
            raise ValueError("invalid encrypted payload length")

        # 飞书规则：Encrypt Key 经 SHA-256 得到 AES-256 密钥，
        # 密文字节的前 16 字节为 CBC IV。
        key = hashlib.sha256(FEISHU_ENCRYPT_KEY.encode("utf-8")).digest()
        iv = encrypted_bytes[:16]
        ciphertext = encrypted_bytes[16:]

        decryptor = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
        ).decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        logger.exception("failed to decrypt Feishu webhook payload")
        raise ValueError("invalid encrypted Feishu payload") from exc

    if not isinstance(payload, dict):
        raise ValueError("decrypted Feishu payload must be a JSON object")

    return payload


def decode_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # 配置 Encrypt Key 后，生产回调必须从 encrypt 字段解密。
    if not FEISHU_ENCRYPT_KEY:
        return payload

    encrypted_payload = payload.get("encrypt")
    if not isinstance(encrypted_payload, str) or not encrypted_payload:
        raise ValueError("encrypted Feishu payload is missing encrypt")

    return decrypt_feishu_payload(encrypted_payload)


def extract_file_event(
    payload: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    header = payload.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None

    message = (payload.get("event") or {}).get("message") or {}
    if message.get("message_type") != "file":
        return None

    raw_content = message.get("content")
    if not isinstance(raw_content, str):
        raise ValueError("message content must be a JSON string")

    content = json.loads(raw_content)
    event_id = header.get("event_id")
    message_id = message.get("message_id")
    file_key = content.get("file_key")
    file_name = content.get("file_name")

    if not all(
        isinstance(value, str) and value
        for value in (event_id, message_id, file_key, file_name)
    ):
        raise ValueError("file event is missing required identifiers")

    return event_id, message_id, file_key, file_name


async def get_tenant_access_token(client: httpx.AsyncClient) -> str:
    global _tenant_access_token
    global _tenant_access_token_expires_at

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.error("Feishu app credentials are not configured")
        raise FeishuAuthError("missing FEISHU_APP_ID or FEISHU_APP_SECRET")

    now = time.monotonic()
    if (
        _tenant_access_token
        and _tenant_access_token_expires_at - now
        > TOKEN_REFRESH_BUFFER_SECONDS
    ):
        return _tenant_access_token

    # 锁内二次检查，避免并发文件事件同时刷新 Token。
    async with _tenant_access_token_lock:
        now = time.monotonic()
        if (
            _tenant_access_token
            and _tenant_access_token_expires_at - now
            > TOKEN_REFRESH_BUFFER_SECONDS
        ):
            return _tenant_access_token

        url = (
            f"{FEISHU_BASE_URL.rstrip('/')}"
            "/auth/v3/tenant_access_token/internal"
        )
        try:
            response = await client.post(
                url,
                json={
                    "app_id": FEISHU_APP_ID,
                    "app_secret": FEISHU_APP_SECRET,
                },
                headers={
                    "Content-Type": "application/json; charset=utf-8"
                },
            )
            response.raise_for_status()
            result = response.json()
        except httpx.TimeoutException:
            logger.exception("timed out while requesting tenant_access_token")
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.exception("Feishu authentication request failed")
            raise FeishuAuthError(
                "unable to authenticate with Feishu"
            ) from exc

        token = result.get("tenant_access_token")
        if result.get("code") != 0 or not isinstance(token, str) or not token:
            logger.error(
                "Feishu authentication rejected: code=%s msg=%s",
                result.get("code"),
                result.get("msg"),
            )
            raise FeishuAuthError(
                str(result.get("msg") or "Feishu authentication failed")
            )

        try:
            expires_in = int(result.get("expire", 0))
        except (TypeError, ValueError) as exc:
            raise FeishuAuthError("invalid token expiry from Feishu") from exc

        if expires_in <= 0:
            raise FeishuAuthError("invalid token expiry from Feishu")

        _tenant_access_token = token
        _tenant_access_token_expires_at = time.monotonic() + expires_in
        return token


async def send_feishu_reply(
    message_id: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
    tenant_access_token: str | None = None,
) -> None:
    """回复原始飞书消息；只传 message_id 和 text 也可独立调用。"""
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        follow_redirects=True,
    )

    try:
        token = tenant_access_token or await get_tenant_access_token(
            active_client
        )
        safe_message_id = quote(message_id, safe="")
        url = (
            f"{FEISHU_BASE_URL.rstrip('/')}/im/v1/messages/"
            f"{safe_message_id}/reply"
        )
        response = await active_client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "content": json.dumps(
                    {"text": text},
                    ensure_ascii=False,
                ),
                "msg_type": "text",
                "reply_in_thread": False,
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise FeishuReplyError(
                str(result.get("msg") or "Feishu reply failed")
            )
    except httpx.TimeoutException:
        logger.exception("timed out while replying to Feishu message")
        raise
    except FeishuAuthError:
        raise
    except (httpx.HTTPError, ValueError, FeishuReplyError):
        logger.exception(
            "failed to reply to Feishu message: message_id=%s",
            message_id,
        )
        raise
    finally:
        if owns_client:
            await active_client.aclose()


async def forward_result_to_n8n(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
) -> bool:
    """将成功结果或错误结果交给 n8n，失败时记录日志但不再抛出。"""
    if not N8N_WEBHOOK_URL:
        logger.error("N8N_WEBHOOK_URL is not configured")
        return False

    try:
        response = await client.post(N8N_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        logger.info(
            "processing result forwarded to n8n: message_id=%s status=%s",
            payload.get("message_id"),
            payload.get("status"),
        )
        return True
    except httpx.TimeoutException:
        logger.exception(
            "timed out while forwarding result to n8n: message_id=%s",
            payload.get("message_id"),
        )
    except httpx.HTTPError:
        logger.exception(
            "failed to forward result to n8n: message_id=%s",
            payload.get("message_id"),
        )

    return False


async def download_message_file(
    client: httpx.AsyncClient,
    tenant_access_token: str,
    message_id: str,
    file_key: str,
    destination: Path,
) -> str:
    safe_message_id = quote(message_id, safe="")
    safe_file_key = quote(file_key, safe="")
    url = (
        f"{FEISHU_BASE_URL.rstrip('/')}/im/v1/messages/"
        f"{safe_message_id}/resources/{safe_file_key}"
    )

    try:
        async with client.stream(
            "GET",
            url,
            params={"type": "file"},
            headers={
                "Authorization": f"Bearer {tenant_access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        ) as response:
            if response.status_code in {401, 403}:
                await response.aread()
                logger.error(
                    "Feishu rejected file download authorization: status=%s",
                    response.status_code,
                )
                raise FeishuAuthError("Feishu rejected file download authorization")

            response.raise_for_status()

            declared_size = int(response.headers.get("Content-Length", "0"))
            if declared_size > MAX_FILE_BYTES:
                raise ValueError("downloaded file exceeds MAX_FILE_BYTES")

            total_bytes = 0
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FILE_BYTES:
                        raise ValueError("downloaded file exceeds MAX_FILE_BYTES")
                    output.write(chunk)

            return response.headers.get(
                "Content-Type",
                "application/octet-stream",
            ).split(";", 1)[0]
    except httpx.TimeoutException:
        logger.exception(
            "timed out while downloading message file: message_id=%s",
            message_id,
        )
        raise
    except FeishuAuthError:
        raise
    except httpx.HTTPError:
        logger.exception(
            "failed to download message file: message_id=%s file_key=%s",
            message_id,
            file_key,
        )
        raise


async def iter_file_chunks(file_path: Path):
    with file_path.open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, CHUNK_SIZE):
            yield chunk


async def extract_text_with_tika(
    client: httpx.AsyncClient,
    file_path: Path,
    content_type: str,
) -> str:
    url = f"{TIKA_BASE_URL.rstrip('/')}/tika"
    try:
        response = await client.put(
            url,
            content=iter_file_chunks(file_path),
            headers={
                "Accept": "text/plain",
                "Content-Type": content_type,
            },
        )
        response.raise_for_status()
        return response.text.strip()
    except httpx.TimeoutException:
        logger.exception("timed out while parsing file with Apache Tika")
        raise
    except httpx.HTTPError:
        logger.exception("Apache Tika parsing request failed")
        raise


def describe_processing_error(exc: Exception) -> str:
    if isinstance(exc, UnsupportedFileTypeError):
        return f"不支持的文件类型：{exc}"
    if isinstance(exc, httpx.TimeoutException):
        return "网络请求超时，请稍后重试"
    if isinstance(exc, FeishuAuthError):
        return f"飞书鉴权失败：{exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"外部服务请求失败：{exc}"
    if isinstance(exc, OSError):
        return f"本地文件处理失败：{exc}"
    return f"文件处理失败：{exc}"


async def process_message_file(
    event_id: str,
    message_id: str,
    file_key: str,
    file_name: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        follow_redirects=True,
        trust_env=True,
    )
    local_client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        follow_redirects=True,
        trust_env=False,
    )
    tenant_access_token: str | None = None

    try:
        tenant_access_token = await get_tenant_access_token(active_client)

        # 重要：后台任务启动后先给用户即时反馈，Webhook 主请求仍可快速 ACK。
        try:
            await send_feishu_reply(
                message_id,
                f"收到文件 {file_name}，正在提取文本...",
                client=active_client,
                tenant_access_token=tenant_access_token,
            )
        except (
            httpx.HTTPError,
            FeishuAuthError,
            FeishuReplyError,
            ValueError,
        ):
            logger.exception(
                "initial Feishu progress reply failed: message_id=%s",
                message_id,
            )

        ensure_supported_file_type(file_name)
        safe_file_name = Path(file_name).name

        with tempfile.TemporaryDirectory(prefix="feishu-file-") as temp_dir:
            local_path = Path(temp_dir) / safe_file_name
            content_type = await download_message_file(
                active_client,
                tenant_access_token,
                message_id,
                file_key,
                local_path,
            )
            extracted_text = await extract_text_with_tika(
                local_client,
                local_path,
                content_type,
            )

        logger.info(
            "file parsed successfully: event_id=%s message_id=%s file=%s chars=%d",
            event_id,
            message_id,
            safe_file_name,
            len(extracted_text),
        )

        # 重要：BackgroundTasks 不消费返回值，必须显式把文本推送给 n8n。
        forwarded = await forward_result_to_n8n(
            local_client,
            {
                "status": "success",
                "event_id": event_id,
                "message_id": message_id,
                "file_name": file_name,
                "extracted_text": extracted_text,
                "error": None,
            },
        )
        if forwarded:
            try:
                await send_feishu_reply(
                    message_id,
                    (
                        f"文件 {file_name} 文本提取完成，"
                        "已提交等待后续处理。"
                    ),
                    client=active_client,
                    tenant_access_token=tenant_access_token,
                )
            except (
                httpx.HTTPError,
                FeishuAuthError,
                FeishuReplyError,
                ValueError,
            ):
                logger.exception(
                    "Feishu completion reply failed: message_id=%s",
                    message_id,
                )

    except Exception as exc:
        error_reason = describe_processing_error(exc)
        logger.exception(
            "file processing failed: event_id=%s message_id=%s reason=%s",
            event_id,
            message_id,
            error_reason,
        )

        # 无论处理成功还是失败，都让 n8n 得到一个可记录的终态事件。
        await forward_result_to_n8n(
            local_client,
            {
                "status": "failed",
                "event_id": event_id,
                "message_id": message_id,
                "file_name": file_name,
                "extracted_text": None,
                "error": error_reason,
            },
        )

        try:
            await send_feishu_reply(
                message_id,
                f"文件 {file_name} 处理失败：{error_reason}",
                client=active_client,
                tenant_access_token=tenant_access_token,
            )
        except Exception:
            logger.exception(
                "failed to send Feishu error reply: message_id=%s",
                message_id,
            )
    finally:
        await local_client.aclose()
        if owns_client:
            await active_client.aclose()


app = FastAPI(title="Feishu File Middleware", version="0.1.0")


@app.post("/webhooks/feishu")
async def receive_feishu_event(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    try:
        # 重要：配置 Encrypt Key 时，先解密，再执行 challenge 或事件解析。
        payload = decode_webhook_payload(payload)
    except ValueError as exc:
        logger.warning("rejected invalid encrypted Feishu webhook")
        raise HTTPException(
            status_code=400,
            detail="invalid encrypted payload",
        ) from exc

    if payload.get("type") == "url_verification":
        verify_webhook_token(payload.get("token"))
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise HTTPException(status_code=400, detail="missing challenge")
        return {"challenge": challenge}

    header = payload.get("header") or {}
    verify_webhook_token(header.get("token"))

    try:
        file_event = extract_file_event(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.exception("invalid im.message.receive_v1 file event")
        return {"code": 0}

    if file_event is None:
        return {"code": 0}

    event_id = file_event[0]
    if not await claim_event_once(event_id):
        logger.info("duplicate Feishu event ignored: event_id=%s", event_id)
        return {"code": 0}

    background_tasks.add_task(process_message_file, *file_event)
    return {"code": 0}
