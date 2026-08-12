import base64
import importlib.util
import struct
import sys
import types
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from tests._unittest_compat import load_function_tests


APP_PATH = Path(r"D:\n8n_AI\local-embedding-service\app.py")


class FakeTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text.split()) + (2 if add_special_tokens else 0)))

    def decode(self, token_ids, skip_special_tokens=True):
        return " ".join(f"token-{token_id}" for token_id in token_ids)


class FakeModel:
    tokenizer = FakeTokenizer()

    def encode(self, texts, **kwargs):
        vectors = []
        for index, _ in enumerate(texts):
            vector = np.array([index + 1.0, 2.0, 3.0, 4.0], dtype=np.float32)
            vector /= np.linalg.norm(vector)
            vectors.append(vector)
        return np.stack(vectors)


def load_app_module():
    assert APP_PATH.exists(), f"missing implementation: {APP_PATH}"
    fake_package = types.ModuleType("sentence_transformers")
    fake_package.SentenceTransformer = FakeModel
    sys.modules["sentence_transformers"] = fake_package
    spec = importlib.util.spec_from_file_location("embedding_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.get_model.cache_clear()
    module.get_model = lambda: FakeModel()
    return module


def test_openai_float_response_preserves_input_order():
    module = load_app_module()
    with TestClient(module.app) as client:
        response = client.post(
            "/v1/embeddings",
            json={"model": "local-test", "input": ["first text", "second text"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["model"] == "local-test"
    assert [item["index"] for item in body["data"]] == [0, 1]
    assert all(item["object"] == "embedding" for item in body["data"])
    assert all(len(item["embedding"]) == 4 for item in body["data"])
    assert body["usage"] == {"prompt_tokens": 8, "total_tokens": 8}


def test_short_route_supports_base64_and_dimensions():
    module = load_app_module()
    with TestClient(module.app) as client:
        response = client.post(
            "/embeddings",
            json={
                "model": "local-test",
                "input": "one two",
                "encoding_format": "base64",
                "dimensions": 2,
            },
        )

    assert response.status_code == 200
    encoded = response.json()["data"][0]["embedding"]
    values = struct.unpack("<2f", base64.b64decode(encoded))
    assert np.isclose(np.linalg.norm(values), 1.0)


def test_token_id_input_is_decoded_with_model_tokenizer():
    module = load_app_module()
    with TestClient(module.app) as client:
        response = client.post(
            "/v1/embeddings",
            json={"model": "local-test", "input": [[11, 12], [21, 22, 23]]},
        )

    assert response.status_code == 200
    assert len(response.json()["data"]) == 2


def test_empty_input_and_invalid_dimensions_are_rejected():
    module = load_app_module()
    with TestClient(module.app) as client:
        empty = client.post(
            "/v1/embeddings", json={"model": "local-test", "input": []}
        )
        oversized = client.post(
            "/v1/embeddings",
            json={"model": "local-test", "input": "text", "dimensions": 99},
        )

    assert empty.status_code == 400
    assert oversized.status_code == 400
    assert empty.json()["error"]["type"] == "invalid_request_error"
    assert oversized.json()["error"]["type"] == "invalid_request_error"


def test_health_endpoint_reports_model_name():
    module = load_app_module()
    with TestClient(module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model": module.MODEL_NAME}


def load_tests(_loader, _tests, _pattern):
    return load_function_tests(globals())
