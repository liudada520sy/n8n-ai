import base64
import os
from functools import lru_cache
from typing import Literal

import numpy as np
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sentence_transformers import SentenceTransformer
from starlette.concurrency import run_in_threadpool


MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL_NAME", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")

EmbeddingInput = str | list[str] | list[int] | list[list[int]]


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input: EmbeddingInput
    model: str = Field(min_length=1)
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(default=None, ge=1)
    user: str | None = None

    @field_validator("input")
    @classmethod
    def reject_empty_input(cls, value: EmbeddingInput) -> EmbeddingInput:
        if isinstance(value, str):
            if not value:
                raise ValueError("input must not be empty")
            return value

        if not value:
            raise ValueError("input must not be empty")

        if all(isinstance(item, str) for item in value):
            if any(not item for item in value):
                raise ValueError("input strings must not be empty")
            return value

        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return value

        if all(isinstance(item, list) for item in value):
            if any(not item for item in value):
                raise ValueError("token arrays must not be empty")
            return value

        raise ValueError("input must contain only strings or token arrays")


class InvalidRequestError(Exception):
    def __init__(self, message: str, param: str | None = None):
        self.message = message
        self.param = param


def openai_error(
    message: str,
    error_type: str,
    param: str | None = None,
    code: str | None = None,
) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, device=DEVICE)


def normalize_input(
    value: EmbeddingInput, model: SentenceTransformer
) -> tuple[list[str], list[int] | None]:
    if isinstance(value, str):
        return [value], None

    if all(isinstance(item, str) for item in value):
        return list(value), None

    tokenizer = model.tokenizer
    if all(isinstance(item, int) for item in value):
        token_ids = list(value)
        return [tokenizer.decode(token_ids, skip_special_tokens=True)], [len(token_ids)]

    token_batches = [list(item) for item in value]
    texts = [
        tokenizer.decode(token_ids, skip_special_tokens=True)
        for token_ids in token_batches
    ]
    return texts, [len(token_ids) for token_ids in token_batches]


def count_tokens(texts: list[str], model: SentenceTransformer) -> int:
    return sum(
        len(model.tokenizer.encode(text, add_special_tokens=True)) for text in texts
    )


def encode_base64(vector: np.ndarray) -> str:
    little_endian_float32 = np.asarray(vector, dtype="<f4")
    return base64.b64encode(little_endian_float32.tobytes()).decode("ascii")


app = FastAPI(title="Local OpenAI-Compatible Embedding Service", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    first_error = exc.errors()[0]
    location = [str(part) for part in first_error["loc"] if part != "body"]
    param = ".".join(location) or None
    message = first_error["msg"]
    return JSONResponse(
        status_code=400,
        content=openai_error(message, "invalid_request_error", param),
    )


@app.exception_handler(InvalidRequestError)
async def handle_invalid_request(
    _request: Request, exc: InvalidRequestError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=openai_error(exc.message, "invalid_request_error", exc.param),
    )


@app.exception_handler(Exception)
async def handle_server_error(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=openai_error(
            "The embedding service encountered an internal error.",
            "server_error",
        ),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    await run_in_threadpool(get_model)
    return {"status": "ok", "model": MODEL_NAME}


async def create_embeddings(payload: EmbeddingRequest) -> dict:
    model = await run_in_threadpool(get_model)
    texts, supplied_token_counts = normalize_input(payload.input, model)
    embeddings = await run_in_threadpool(
        model.encode,
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    embeddings = np.asarray(embeddings, dtype=np.float32)

    native_dimensions = int(embeddings.shape[1])
    if payload.dimensions is not None:
        if payload.dimensions > native_dimensions:
            raise InvalidRequestError(
                f"dimensions must be less than or equal to {native_dimensions}",
                "dimensions",
            )
        embeddings = embeddings[:, : payload.dimensions]
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.maximum(norms, np.finfo(np.float32).eps)

    data = []
    for index, vector in enumerate(embeddings):
        embedding = (
            encode_base64(vector)
            if payload.encoding_format == "base64"
            else vector.tolist()
        )
        data.append({"object": "embedding", "index": index, "embedding": embedding})

    prompt_tokens = (
        sum(supplied_token_counts)
        if supplied_token_counts is not None
        else count_tokens(texts, model)
    )
    return {
        "object": "list",
        "data": data,
        "model": payload.model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "total_tokens": prompt_tokens,
        },
    }


app.add_api_route(
    "/v1/embeddings",
    create_embeddings,
    methods=["POST"],
    response_model=None,
)
app.add_api_route(
    "/embeddings",
    create_embeddings,
    methods=["POST"],
    response_model=None,
)
