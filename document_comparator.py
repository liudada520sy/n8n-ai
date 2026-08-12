import asyncio
import math
import random
import re
import uuid
from typing import Any

import httpx
from diff_match_patch import diff_match_patch
from qdrant_client import AsyncQdrantClient, models


class DocumentComparator:
    """通过 Embedding + Qdrant 语义对齐两个版本的文档，再生成 Markdown Diff。"""

    DIFF_DELETE = -1
    DIFF_EQUAL = 0
    DIFF_INSERT = 1

    def __init__(
        self,
        *,
        embedding_url: str = (
            "http://local-embedding-service:8000/v1/embeddings"
        ),
        embedding_model: str = "local-embedding",
        qdrant_url: str = "http://qdrant:6333",
        chunk_size: int = 1000,
        similarity_threshold: float = 0.5,
        request_timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
        qdrant_client: AsyncQdrantClient | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold 必须在 0 到 1 之间")

        self.embedding_url = embedding_url
        self.embedding_model = embedding_model
        self.qdrant_url = qdrant_url
        self.chunk_size = chunk_size
        self.similarity_threshold = similarity_threshold
        self.request_timeout = request_timeout
        self._http_client = http_client
        self._qdrant_client = qdrant_client
        self._dmp = diff_match_patch()

    def chunk_text(self, text: str) -> list[dict[str, Any]]:
        """
        优先按空行切分段落；当单个段落超过固定长度时继续切片。

        每个 chunk 都包含从 0 开始的顺序索引，后续最终输出严格按照
        新版本 chunk 的该索引重新拼装。
        """
        if not text or not text.strip():
            return []

        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = re.split(r"\n\s*\n", normalized_text)
        chunks: list[dict[str, Any]] = []

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            for start in range(0, len(paragraph), self.chunk_size):
                chunk_text = paragraph[start : start + self.chunk_size]
                chunks.append(
                    {
                        "index": len(chunks),
                        "text": chunk_text,
                    }
                )

        return chunks

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """调用兼容 OpenAI 规范的本地 Embedding 服务。"""
        if not texts:
            return []

        payload = {
            "model": self.embedding_model,
            "input": texts,
            "encoding_format": "float",
        }

        if self._http_client is not None:
            response = await self._http_client.post(
                self.embedding_url,
                json=payload,
            )
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.request_timeout)
            ) as client:
                response = await client.post(
                    self.embedding_url,
                    json=payload,
                )

        response.raise_for_status()
        result = response.json()
        data = result.get("data")

        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Embedding 服务返回的向量数量与输入数量不一致")

        try:
            ordered_data = sorted(data, key=lambda item: item["index"])
        except (KeyError, TypeError) as exc:
            raise ValueError("Embedding 响应缺少合法的 index") from exc

        expected_indices = list(range(len(texts)))
        actual_indices = [item.get("index") for item in ordered_data]
        if actual_indices != expected_indices:
            raise ValueError("Embedding 响应的 index 不连续或存在重复")

        embeddings: list[list[float]] = []
        vector_size: int | None = None

        for item in ordered_data:
            embedding = item.get("embedding")
            if (
                not isinstance(embedding, list)
                or not embedding
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    for value in embedding
                )
            ):
                raise ValueError("Embedding 响应包含非法向量")

            vector = [float(value) for value in embedding]
            if vector_size is None:
                vector_size = len(vector)
            elif len(vector) != vector_size:
                raise ValueError("Embedding 响应中的向量维度不一致")

            embeddings.append(vector)

        return embeddings

    def build_markdown_diff(
        self,
        old_text: str,
        new_text: str,
    ) -> str:
        """
        对已经语义对齐的两个 chunk 计算字符级 Diff。

        diff-match-patch 的操作值约定：
        -1 表示删除，0 表示相同，1 表示新增。
        """
        diffs = self._dmp.diff_main(old_text, new_text)
        self._dmp.diff_cleanupSemantic(diffs)

        rendered_parts: list[str] = []
        for operation, content in diffs:
            if operation == self.DIFF_DELETE:
                rendered_parts.append(f"<del>{content}</del>")
            elif operation == self.DIFF_INSERT:
                rendered_parts.append(f"<ins>{content}</ins>")
            elif operation == self.DIFF_EQUAL:
                rendered_parts.append(content)
            else:
                raise ValueError(f"未知的 Diff 操作类型：{operation}")

        rendered_diff = "".join(rendered_parts)

        # diff-match-patch 对空格、换行等也非常敏感。
        # 纯空白的新增/删除标签保留原空白，但移除高亮标签；
        # 空的 <ins></ins>、<del></del> 会被直接清除。
        return re.sub(
            r"<(ins|del)>(\s*)</\1>",
            lambda match: match.group(2),
            rendered_diff,
        )

    def assemble_output(
        self,
        rendered_new_chunks: list[str],
        old_chunks: list[dict[str, Any]],
        matched_old_indices: set[int],
    ) -> str:
        """拼装新版本 Diff，并在末尾追加完全删除的旧段落。"""
        main_diff = "\n\n".join(rendered_new_chunks)
        deleted_chunks = [
            chunk
            for chunk in old_chunks
            if chunk["index"] not in matched_old_indices
        ]

        if not deleted_chunks:
            return main_diff

        deleted_section = (
            "### 🔴 完全删除的段落\n\n"
            + "\n\n".join(
                f"<del>{chunk['text']}</del>"
                for chunk in deleted_chunks
            )
        )

        if not main_diff:
            return deleted_section

        return f"{main_diff}\n\n{deleted_section}"

    async def compare(
        self,
        text_old: str,
        text_new: str,
    ) -> str:
        """执行分块、向量化、Qdrant 对齐、Diff 和最终拼装。"""
        old_chunks = self.chunk_text(text_old)
        new_chunks = self.chunk_text(text_new)

        # 旧版本为空时不需要调用 Embedding 和 Qdrant，
        # 新版本的所有段落都直接标记为纯新增。
        if not old_chunks:
            return "\n\n".join(
                f"<ins>{chunk['text']}</ins>" for chunk in new_chunks
            )

        # 新版本为空意味着旧版本的所有段落都被完全删除。
        if not new_chunks:
            return self.assemble_output([], old_chunks, set())

        old_vectors = await self.embed_texts(
            [chunk["text"] for chunk in old_chunks]
        )
        new_vectors = await self.embed_texts(
            [chunk["text"] for chunk in new_chunks]
        )

        if not old_vectors or not new_vectors:
            raise ValueError("非空文档未能生成 Embedding 向量")

        vector_size = len(old_vectors[0])
        if any(len(vector) != vector_size for vector in new_vectors):
            raise ValueError("新旧版本的 Embedding 向量维度不一致")

        collection_name = f"document_compare_{uuid.uuid4().hex}"
        owns_qdrant_client = self._qdrant_client is None
        qdrant = self._qdrant_client or AsyncQdrantClient(
            url=self.qdrant_url
        )
        collection_created = False

        try:
            # 为单次比对创建临时 Collection，使用余弦相似度。
            await qdrant.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            collection_created = True

            # 将旧版本向量、原文和顺序索引作为 Payload 一并写入。
            await qdrant.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(
                        id=chunk["index"],
                        vector=vector,
                        payload={
                            "chunk_index": chunk["index"],
                            "text": chunk["text"],
                        },
                    )
                    for chunk, vector in zip(old_chunks, old_vectors)
                ],
                wait=True,
            )

            rendered_chunks: list[str] = []
            matched_old_indices: set[int] = set()

            # 按新版本原始顺序逐个检索，因此最终天然保持新文档顺序。
            for new_chunk, new_vector in zip(new_chunks, new_vectors):
                query_result = await qdrant.query_points(
                    collection_name=collection_name,
                    query=new_vector,
                    limit=1,
                    with_payload=True,
                )
                matched_points = query_result.points

                # 无结果或低于阈值时，整个新 chunk 都属于纯新增。
                if (
                    not matched_points
                    or matched_points[0].score
                    < self.similarity_threshold
                ):
                    rendered_chunks.append(
                        f"<ins>{new_chunk['text']}</ins>"
                    )
                    continue

                old_payload = matched_points[0].payload or {}
                old_chunk_text = old_payload.get("text")
                old_chunk_index = old_payload.get("chunk_index")
                if not isinstance(old_chunk_text, str):
                    raise ValueError("Qdrant 命中结果缺少旧版本原文 Payload")
                if not isinstance(old_chunk_index, int):
                    raise ValueError("Qdrant 命中结果缺少旧版本顺序索引")

                matched_old_indices.add(old_chunk_index)
                rendered_chunks.append(
                    self.build_markdown_diff(
                        old_chunk_text,
                        new_chunk["text"],
                    )
                )

            # 所有未进入 matched_old_indices 的旧 chunk 都是完全删除。
            return self.assemble_output(
                rendered_chunks,
                old_chunks,
                matched_old_indices,
            )

        finally:
            # 无论对齐或 Diff 是否成功，都清理本次任务的临时 Collection。
            if collection_created:
                await qdrant.delete_collection(collection_name)

            if owns_qdrant_client:
                await qdrant.close()


if __name__ == "__main__":

    class MockDocumentComparator(DocumentComparator):
        """
        纯内存测试版本。

        不调用 HTTP Embedding 服务，也不连接 Qdrant。字符会被映射为
        确定性的伪随机向量，相似文本会因共享字符而获得较高余弦相似度。
        """

        MOCK_VECTOR_SIZE = 64

        async def embed_texts(
            self,
            texts: list[str],
        ) -> list[list[float]]:
            vectors: list[list[float]] = []

            for text in texts:
                vector = [0.0] * self.MOCK_VECTOR_SIZE

                for character in text:
                    # 相同字符始终产生相同的伪随机方向。
                    generator = random.Random(
                        f"document-comparator:{character}"
                    )
                    for index in range(self.MOCK_VECTOR_SIZE):
                        vector[index] += generator.uniform(-1.0, 1.0)

                norm = math.sqrt(
                    sum(value * value for value in vector)
                )
                if norm == 0:
                    vectors.append(vector)
                else:
                    vectors.append(
                        [value / norm for value in vector]
                    )

            return vectors

        @staticmethod
        def cosine_similarity(
            left: list[float],
            right: list[float],
        ) -> float:
            left_norm = math.sqrt(
                sum(value * value for value in left)
            )
            right_norm = math.sqrt(
                sum(value * value for value in right)
            )

            if left_norm == 0 or right_norm == 0:
                return 0.0

            dot_product = sum(
                left_value * right_value
                for left_value, right_value in zip(left, right)
            )
            return dot_product / (left_norm * right_norm)

        async def compare(
            self,
            text_old: str,
            text_new: str,
        ) -> str:
            old_chunks = self.chunk_text(text_old)
            new_chunks = self.chunk_text(text_new)

            if not old_chunks:
                return "\n\n".join(
                    f"<ins>{chunk['text']}</ins>"
                    for chunk in new_chunks
                )

            if not new_chunks:
                return self.assemble_output([], old_chunks, set())

            old_vectors = await self.embed_texts(
                [chunk["text"] for chunk in old_chunks]
            )
            new_vectors = await self.embed_texts(
                [chunk["text"] for chunk in new_chunks]
            )

            rendered_chunks: list[str] = []
            matched_old_indices: set[int] = set()

            # 使用内存中的余弦相似度 Top-1 检索模拟 Qdrant。
            for new_chunk, new_vector in zip(
                new_chunks,
                new_vectors,
            ):
                similarities = [
                    self.cosine_similarity(
                        new_vector,
                        old_vector,
                    )
                    for old_vector in old_vectors
                ]
                best_old_position = max(
                    range(len(similarities)),
                    key=similarities.__getitem__,
                )
                best_score = similarities[best_old_position]

                if best_score < self.similarity_threshold:
                    rendered_chunks.append(
                        f"<ins>{new_chunk['text']}</ins>"
                    )
                    continue

                old_chunk = old_chunks[best_old_position]
                matched_old_indices.add(old_chunk["index"])
                rendered_chunks.append(
                    self.build_markdown_diff(
                        old_chunk["text"],
                        new_chunk["text"],
                    )
                )

            return self.assemble_output(
                rendered_chunks,
                old_chunks,
                matched_old_indices,
            )

    async def main() -> None:
        # 旧版本包含一个将在新版本中被完全删除的轮胎规格段落。
        text_old = """
发动机最大功率为 150 kW，峰值扭矩为 320 N·m。

车身长度为 4800 mm，轴距为 2850 mm。

制动系统采用前通风盘、后实心盘结构。

轮胎规格为 235/50 R19。
""".strip()

        # 新版本：段落发生移动，同时功率和制动系统描述发生修改，
        # 新增智能驾驶配置，并完全删除旧版本的轮胎规格段落。
        text_new = """
制动系统采用前后通风盘结构，并增加制动能量回收功能。

车身长度为 4800 mm，轴距为 2850 mm。

发动机最大功率为 160 kW，峰值扭矩为 320 N·m。

新增高速领航辅助和自动泊车功能。
""".strip()

        # 纯内存 Mock：直接运行本文件，不需要启动 Docker。
        comparator = MockDocumentComparator(
            similarity_threshold=0.35,
            chunk_size=1000,
        )
        markdown_diff = await comparator.compare(
            text_old,
            text_new,
        )

        print(markdown_diff)

    asyncio.run(main())
