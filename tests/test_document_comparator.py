import asyncio
import importlib.util
import json
import math
import runpy
import sys
import types
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from tests._unittest_compat import load_function_tests


APP_PATH = Path(r"D:\n8n_AI\document_comparator.py")


class FakeVectorParams:
    def __init__(self, size, distance):
        self.size = size
        self.distance = distance


class FakePointStruct:
    def __init__(self, id, vector, payload):
        self.id = id
        self.vector = vector
        self.payload = payload


class FakeDistance:
    COSINE = "Cosine"


class FakeDiffMatchPatch:
    DIFF_DELETE = -1
    DIFF_EQUAL = 0
    DIFF_INSERT = 1

    def diff_main(self, old_text, new_text):
        matcher = SequenceMatcher(a=old_text, b=new_text)
        diffs = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                diffs.append((self.DIFF_DELETE, old_text[old_start:old_end]))
            if tag in {"replace", "insert"}:
                diffs.append((self.DIFF_INSERT, new_text[new_start:new_end]))
            if tag == "equal":
                diffs.append((self.DIFF_EQUAL, old_text[old_start:old_end]))
        return diffs

    def diff_cleanupSemantic(self, _diffs):
        return None


def install_fake_dependencies():
    qdrant_module = types.ModuleType("qdrant_client")
    qdrant_module.AsyncQdrantClient = object
    models = types.SimpleNamespace(
        Distance=FakeDistance,
        VectorParams=FakeVectorParams,
        PointStruct=FakePointStruct,
    )
    qdrant_module.models = models
    sys.modules["qdrant_client"] = qdrant_module

    dmp_module = types.ModuleType("diff_match_patch")
    dmp_module.diff_match_patch = FakeDiffMatchPatch
    sys.modules["diff_match_patch"] = dmp_module


def load_module():
    assert APP_PATH.exists(), f"missing implementation: {APP_PATH}"
    install_fake_dependencies()
    spec = importlib.util.spec_from_file_location("document_comparator", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeQueryResponse:
    def __init__(self, points):
        self.points = points


class FakeScoredPoint:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


class FakeQdrantClient:
    def __init__(self):
        self.points = []
        self.created = []
        self.deleted = []

    async def create_collection(self, collection_name, vectors_config):
        self.created.append((collection_name, vectors_config))

    async def upsert(self, collection_name, points, wait):
        assert collection_name == self.created[0][0]
        assert wait is True
        self.points.extend(points)

    async def query_points(
        self,
        collection_name,
        query,
        limit,
        with_payload,
    ):
        assert collection_name == self.created[0][0]
        assert limit == 1
        assert with_payload is True
        scored = []
        query_norm = math.sqrt(sum(value * value for value in query))
        for point in self.points:
            point_norm = math.sqrt(sum(value * value for value in point.vector))
            dot_product = sum(
                left * right for left, right in zip(query, point.vector)
            )
            score = dot_product / (query_norm * point_norm)
            scored.append(FakeScoredPoint(score, point.payload))
        scored.sort(key=lambda item: item.score, reverse=True)
        return FakeQueryResponse(scored[:1])

    async def delete_collection(self, collection_name):
        self.deleted.append(collection_name)

    async def close(self):
        raise AssertionError("injected Qdrant client must not be closed")


def embedding_vector(text):
    vectors = {
        "发动机功率为 100 kW。": [1.0, 0.0, 0.0],
        "发动机功率为 120 kW。": [0.99, 0.1, 0.0],
        "车身颜色为蓝色。": [0.0, 1.0, 0.0],
        "新增自动驾驶配置。": [0.0, 0.0, 1.0],
        "保留段落。": [1.0, 0.0],
        "完全删除段落。": [0.0, 1.0],
    }
    return vectors[text]


def test_chunking_preserves_order_and_splits_long_paragraphs():
    module = load_module()
    comparator = module.DocumentComparator(chunk_size=5)

    chunks = comparator.chunk_text("第一段\n\n123456789\n\n第三段")

    assert chunks == [
        {"index": 0, "text": "第一段"},
        {"index": 1, "text": "12345"},
        {"index": 2, "text": "6789"},
        {"index": 3, "text": "第三段"},
    ]


def test_embedding_response_is_restored_to_input_order():
    module = load_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "local-embedding"
        assert body["encoding_format"] == "float"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                ],
                "model": "local-embedding",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            comparator = module.DocumentComparator(http_client=http_client)
            return await comparator.embed_texts(["old", "new"])

    assert asyncio.run(run_test()) == [[1.0, 0.0], [0.0, 1.0]]


def test_semantic_alignment_keeps_new_order_and_highlights_diff():
    module = load_module()
    fake_qdrant = FakeQdrantClient()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": embedding_vector(text),
            }
            for index, text in enumerate(body["input"])
        ]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": body["model"],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    old_text = "发动机功率为 100 kW。\n\n车身颜色为蓝色。"
    new_text = (
        "车身颜色为蓝色。\n\n"
        "发动机功率为 120 kW。\n\n"
        "新增自动驾驶配置。"
    )

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            comparator = module.DocumentComparator(
                similarity_threshold=0.5,
                http_client=http_client,
                qdrant_client=fake_qdrant,
            )
            return await comparator.compare(old_text, new_text)

    result = asyncio.run(run_test())

    assert result == (
        "车身颜色为蓝色。\n\n"
        "发动机功率为 1<del>0</del><ins>2</ins>0 kW。\n\n"
        "<ins>新增自动驾驶配置。</ins>"
    )
    assert len(fake_qdrant.created) == 1
    assert fake_qdrant.deleted == [fake_qdrant.created[0][0]]


def test_empty_old_document_marks_every_new_chunk_as_added():
    module = load_module()
    comparator = module.DocumentComparator()

    result = asyncio.run(
        comparator.compare("", "新增段落一。\n\n新增段落二。")
    )

    assert result == "<ins>新增段落一。</ins>\n\n<ins>新增段落二。</ins>"


def test_unmatched_old_chunk_is_appended_as_fully_deleted():
    module = load_module()
    fake_qdrant = FakeQdrantClient()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": embedding_vector(text),
                    }
                    for index, text in enumerate(body["input"])
                ],
                "model": body["model"],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    async def run_test():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            comparator = module.DocumentComparator(
                http_client=http_client,
                qdrant_client=fake_qdrant,
            )
            return await comparator.compare(
                "保留段落。\n\n完全删除段落。",
                "保留段落。",
            )

    assert asyncio.run(run_test()) == (
        "保留段落。\n\n"
        "### 🔴 完全删除的段落\n\n"
        "<del>完全删除段落。</del>"
    )


def test_empty_new_document_marks_all_old_chunks_as_deleted():
    module = load_module()
    comparator = module.DocumentComparator()

    result = asyncio.run(
        comparator.compare(
            "旧段落一。\n\n旧段落二。",
            "",
        )
    )

    assert result == (
        "### 🔴 完全删除的段落\n\n"
        "<del>旧段落一。</del>\n\n"
        "<del>旧段落二。</del>"
    )


def test_whitespace_only_diff_tags_are_removed():
    module = load_module()
    comparator = module.DocumentComparator()

    result = comparator.build_markdown_diff("A B", "A  B")

    assert result == "A  B"
    assert "<ins>" not in result
    assert "<del>" not in result


def test_main_example_runs_without_external_services(capsys):
    source = APP_PATH.read_text(encoding="utf-8")
    assert "class MockDocumentComparator" in source

    install_fake_dependencies()
    runpy.run_path(str(APP_PATH), run_name="__main__")

    output = capsys.readouterr().out
    assert "### 🔴 完全删除的段落" in output
    assert "<ins>" in output
    assert "<del>" in output


def load_tests(_loader, _tests, _pattern):
    return load_function_tests(globals())
