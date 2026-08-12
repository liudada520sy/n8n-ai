from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


logger = logging.getLogger("report-pipeline-api")


def _load_report_pipeline() -> Any:
    """允许本适配器放在 D:\\n8n_AI 外运行，便于 Codex 侧生成后再安装。"""
    try:
        import report_pipeline  # type: ignore

        return report_pipeline
    except ImportError:
        module_path = Path(
            os.getenv("REPORT_PIPELINE_PATH", r"D:\n8n_AI\report_pipeline.py")
        )
        if not module_path.is_file():
            raise
        spec = importlib.util.spec_from_file_location("report_pipeline", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 report_pipeline: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["report_pipeline"] = module
        spec.loader.exec_module(module)
        return module


report_pipeline = _load_report_pipeline()


@dataclass(slots=True)
class FeishuTenantTokenProvider:
    """通过自建应用凭证获取并缓存 tenant_access_token。"""

    app_id: str | None = None
    app_secret: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 20.0
    refresh_buffer_seconds: float = 300.0
    http_client: Any | None = None
    _token: str = field(default="", init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.app_id = self.app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = self.app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.base_url = (
            self.base_url
            or os.getenv("FEISHU_BASE_URL")
            or "https://open.feishu.cn/open-apis"
        )

    def _has_valid_token(self) -> bool:
        return bool(
            self._token
            and self._expires_at - time.monotonic() > self.refresh_buffer_seconds
        )

    async def __call__(self) -> str:
        if self._has_valid_token():
            return self._token
        if not self.app_id or not self.app_secret:
            raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")

        async with self._lock:
            if self._has_valid_token():
                return self._token

            url = (
                f"{str(self.base_url).rstrip('/')}"
                "/auth/v3/tenant_access_token/internal"
            )
            request_kwargs = {
                "json": {
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                },
                "headers": {"Content-Type": "application/json; charset=utf-8"},
            }
            if self.http_client is not None:
                response = await self.http_client.post(url, **request_kwargs)
            else:
                timeout = httpx.Timeout(self.timeout_seconds)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, **request_kwargs)

            response.raise_for_status()
            data = response.json()
            token = data.get("tenant_access_token")
            if data.get("code") != 0 or not isinstance(token, str) or not token:
                raise RuntimeError(
                    f"飞书鉴权失败: code={data.get('code')} msg={data.get('msg')}"
                )
            try:
                expires_in = int(data.get("expire", 0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("飞书 Token 过期时间无效") from exc
            if expires_in <= 0:
                raise RuntimeError("飞书 Token 过期时间无效")

            self._token = token
            self._expires_at = time.monotonic() + expires_in
            return self._token


feishu_token_provider = FeishuTenantTokenProvider()


class N8NFilePayload(BaseModel):
    """n8n 从飞书中间件收到的单文件文本提取结果。"""

    status: str = Field(..., description="success 或 failed")
    message_id: str = Field(..., description="飞书原始消息 ID，用于最终回复卡片")
    file_name: str
    extracted_text: str | None = None
    event_id: str | None = None
    error: str | None = None
    task_id: str | None = Field(
        default=None,
        description="同一次版本比对任务 ID；MVP 可先固定为 vehicle-demo-001",
    )
    version_role: str = Field(
        default="auto",
        description="old/new/auto；auto 会根据文件名或接收顺序判断",
    )


@dataclass(slots=True)
class VersionItem:
    message_id: str
    file_name: str
    extracted_text: str
    event_id: str | None
    received_at: float


@dataclass(slots=True)
class PairResult:
    task_id: str
    received_role: str
    ready: bool
    old: VersionItem | None = None
    new: VersionItem | None = None


def infer_version_role(file_name: str) -> str | None:
    """从文件名粗略判断旧版/新版；判断不了就交给接收顺序处理。"""
    normalized = Path(file_name).stem.lower()
    old_patterns = (
        r"(^|[_\-\s])v?1($|[_\-\s])",
        r"旧版",
        r"old",
        r"before",
        r"previous",
    )
    new_patterns = (
        r"(^|[_\-\s])v?2($|[_\-\s])",
        r"新版",
        r"new",
        r"after",
        r"current",
    )
    if any(re.search(pattern, normalized) for pattern in old_patterns):
        return "old"
    if any(re.search(pattern, normalized) for pattern in new_patterns):
        return "new"
    return None


class VersionPairStore:
    """MVP 内存配对缓存：第一份先等，第二份触发分析。"""

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self.ttl_seconds = ttl_seconds or float(
            os.getenv("VERSION_PAIR_TTL_SECONDS", "3600")
        )
        self._pairs: dict[str, dict[str, VersionItem]] = {}
        self._updated_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _cleanup(self, now: float) -> None:
        expired = [
            task_id
            for task_id, updated_at in self._updated_at.items()
            if now - updated_at > self.ttl_seconds
        ]
        for task_id in expired:
            self._pairs.pop(task_id, None)
            self._updated_at.pop(task_id, None)

    async def add(self, payload: N8NFilePayload) -> PairResult:
        now = time.time()
        task_id = payload.task_id or "default"
        role = payload.version_role.strip().lower() if payload.version_role else "auto"

        if role not in {"old", "new", "auto"}:
            raise ValueError("version_role 只能是 old/new/auto")
        if role == "auto":
            role = infer_version_role(payload.file_name) or "auto"

        item = VersionItem(
            message_id=payload.message_id,
            file_name=payload.file_name,
            extracted_text=payload.extracted_text or "",
            event_id=payload.event_id,
            received_at=now,
        )

        async with self._lock:
            self._cleanup(now)
            pair = self._pairs.setdefault(task_id, {})

            if role == "auto":
                role = "old" if "old" not in pair else "new"
            pair[role] = item
            self._updated_at[task_id] = now

            if "old" in pair and "new" in pair:
                old_item = pair["old"]
                new_item = pair["new"]
                self._pairs.pop(task_id, None)
                self._updated_at.pop(task_id, None)
                return PairResult(
                    task_id=task_id,
                    received_role=role,
                    ready=True,
                    old=old_item,
                    new=new_item,
                )

            return PairResult(task_id=task_id, received_role=role, ready=False)

    async def restore(self, pair_result: PairResult) -> None:
        """分析失败时恢复配对，便于 n8n 重试新版请求。"""
        if pair_result.old is None or pair_result.new is None:
            return
        async with self._lock:
            self._pairs[pair_result.task_id] = {
                "old": pair_result.old,
                "new": pair_result.new,
            }
            self._updated_at[pair_result.task_id] = time.time()


async def _mock_analyze_pair(pair: PairResult) -> dict[str, Any]:
    """本地联调模式：不调用 Qwen/飞书，只验证 n8n 触发逻辑。"""
    assert pair.old is not None and pair.new is not None
    detail_diff = (
        f"旧版文件：{pair.old.file_name}\n\n"
        f"<del>{pair.old.extracted_text[:1000]}</del>\n\n"
        f"新版文件：{pair.new.file_name}\n\n"
        f"<ins>{pair.new.extracted_text[:1000]}</ins>"
    )
    return {
        "file_name": pair.new.file_name,
        "change_type": "mock 文本配对检查",
        "summary_markdown": (
            "【变更评级 (高/中/低)】\n低\n"
            "【核心工程变更清单】\n- 已成功收到旧版与新版文件，n8n 到分析 API 的链路正常。\n"
            "【潜在影响提示】\n- 当前为 MOCK 模式，尚未调用 Qwen 与飞书卡片接口。"
        ),
        "detail_diff": detail_diff,
        "feishu_result": {"mock": True},
    }


async def analyze_pair(pair: PairResult) -> dict[str, Any]:
    """真实分析：语义 Diff -> Qwen 总结 -> 飞书卡片回复。"""
    assert pair.old is not None and pair.new is not None

    if os.getenv("PIPELINE_MOCK_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return await _mock_analyze_pair(pair)

    from document_comparator import DocumentComparator

    comparator = DocumentComparator(
        embedding_url=os.getenv(
            "EMBEDDING_URL",
            "http://local-embedding-service:8000/v1/embeddings",
        ),
        qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.5")),
    )
    detail_diff = await comparator.compare(
        pair.old.extracted_text,
        pair.new.extracted_text,
    )

    summarizer = report_pipeline.QwenSummarizer(
        timeout_seconds=float(os.getenv("QWEN_TIMEOUT_SECONDS", "90")),
        max_input_chars=int(os.getenv("QWEN_MAX_INPUT_CHARS", "120000")),
        max_tokens=int(os.getenv("QWEN_MAX_TOKENS", "2000")),
    )
    summary = await summarizer.summarize(
        detail_diff,
        pair.new.file_name,
        "普通文档语义对齐 Markdown Diff",
    )

    sender = report_pipeline.FeishuCardSender(
        token_provider=feishu_token_provider,
        base_url=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn/open-apis"),
        timeout_seconds=float(os.getenv("FEISHU_TIMEOUT_SECONDS", "20")),
        max_detail_chars=int(os.getenv("CARD_DETAIL_MAX_CHARS", "4000")),
        max_summary_chars=int(os.getenv("CARD_SUMMARY_MAX_CHARS", "3000")),
    )
    card = sender.build_card(
        file_name=pair.new.file_name,
        summary_markdown=summary,
        detail_diff=detail_diff,
    )
    feishu_result = await sender.reply_card(pair.new.message_id, card)

    return {
        "file_name": pair.new.file_name,
        "change_type": "普通文档语义对齐 Markdown Diff",
        "summary_markdown": summary,
        "detail_diff": detail_diff,
        "card": card,
        "feishu_result": feishu_result,
    }


Analyzer = Callable[[PairResult], Awaitable[dict[str, Any]]]


def create_app(
    *,
    store: VersionPairStore | None = None,
    analyzer: Analyzer | None = None,
) -> FastAPI:
    pair_store = store or VersionPairStore()
    active_analyzer = analyzer or analyze_pair
    app = FastAPI(title="Vehicle Report Pipeline API", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/n8n/version-file")
    async def receive_version_file(payload: N8NFilePayload) -> dict[str, Any]:
        if payload.status != "success":
            logger.warning(
                "n8n forwarded failed file event: file=%s error=%s",
                payload.file_name,
                payload.error,
            )
            return {
                "status": "ignored_failed_file",
                "file_name": payload.file_name,
                "error": payload.error,
            }

        if not payload.extracted_text or not payload.extracted_text.strip():
            raise HTTPException(status_code=400, detail="extracted_text 不能为空")

        try:
            pair = await pair_store.add(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not pair.ready:
            return {
                "status": "waiting_for_pair",
                "task_id": pair.task_id,
                "received_role": pair.received_role,
                "file_name": payload.file_name,
            }

        try:
            analysis = await active_analyzer(pair)
        except Exception as exc:
            await pair_store.restore(pair)
            logger.exception("version analysis failed: task_id=%s", pair.task_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "status": "completed",
            "task_id": pair.task_id,
            "old_file": pair.old.file_name if pair.old else None,
            "new_file": pair.new.file_name if pair.new else None,
            "analysis": analysis,
        }

    return app


app = create_app()
