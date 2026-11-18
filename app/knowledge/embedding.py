import hashlib
import math
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx2
from google import genai
from google.genai import errors, types

from app.knowledge.models import EMBEDDING_DIM

GEMINI_MODEL = "gemini-embedding-2"
MAX_QUERY_LENGTH = 1000
EMBEDDING_BATCH_SIZE = 12


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    fingerprint: str


def embedding_profile() -> EmbeddingProfile:
    provider = os.environ.get("AGENT_PROVIDER", "fake")
    if provider == "fake":
        return EmbeddingProfile("fake", "fake-lexical-v1", f"fake:lexical-v1:{EMBEDDING_DIM}:chunks-v1")
    if provider == "gemini":
        model = os.environ.get("GEMINI_EMBEDDING_MODEL", GEMINI_MODEL)
        if model == GEMINI_MODEL:
            return EmbeddingProfile("gemini", model, f"gemini:{model}:{EMBEDDING_DIM}:search-v1:chunks-v1")
    raise EmbeddingFailure("EMBEDDING_CONFIG", "지원하는 임베딩 공급자와 모델 설정을 확인하세요.")


class EmbeddingFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Embedder(Protocol):
    profile: EmbeddingProfile

    def documents(self, title: str, texts: Sequence[str]) -> list[list[float]]: ...

    def query(self, query: str) -> list[float]: ...


def checked_vector(values: Sequence[float] | None) -> list[float]:
    if (
        values is None
        or len(values) != EMBEDDING_DIM
        or any(not isinstance(value, (float, int)) or not math.isfinite(value) for value in values)
        or not any(values)
    ):
        raise EmbeddingFailure("EMBEDDING_INVALID", "임베딩 차원이나 값이 올바르지 않습니다.")
    return list(values)


class FakeEmbedder:
    profile = EmbeddingProfile("fake", "fake-lexical-v1", f"fake:lexical-v1:{EMBEDDING_DIM}:chunks-v1")

    def query(self, query: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for token in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", query).casefold()):
            features = [token]
            features.extend(token[index:index + 2] for index in range(len(token) - 1))
            for feature in features:
                digest = hashlib.blake2b(feature.encode(), digest_size=4).digest()
                vector[int.from_bytes(digest, "little") % EMBEDDING_DIM] += 1
        if not any(vector):
            digest = hashlib.blake2b(query.encode(), digest_size=4).digest()
            vector[int.from_bytes(digest, "little") % EMBEDDING_DIM] = 1
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def documents(self, title: str, texts: Sequence[str]) -> list[list[float]]:
        return [self.query(f"{title}\n{text}") for text in texts]


class GeminiEmbedder:
    def __init__(self, client: genai.Client, profile: EmbeddingProfile):
        self.client = client
        self.profile = profile

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        contents = [types.Content(parts=[types.Part.from_text(text=text)]) for text in texts]
        try:
            response = self.client.models.embed_content(
                model=self.profile.model,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
            )
        except errors.APIError as exc:
            if exc.code == 429:
                raise EmbeddingFailure("EMBEDDING_RATE_LIMIT", "Gemini 호출 한도를 초과했습니다.", retryable=True) from exc
            if exc.code in (408, 500, 502, 503, 504):
                raise EmbeddingFailure("EMBEDDING_UNAVAILABLE", "Gemini API가 일시적으로 응답하지 않습니다.", retryable=True) from exc
            raise EmbeddingFailure("EMBEDDING_CONFIG", "Gemini 모델 접근과 요청 설정을 확인하세요.") from exc
        except (httpx2.TransportError, TimeoutError) as exc:
            raise EmbeddingFailure("EMBEDDING_UNAVAILABLE", "Gemini에 연결할 수 없습니다.", retryable=True) from exc
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise EmbeddingFailure("EMBEDDING_INVALID", "Gemini 임베딩 개수가 입력 개수와 다릅니다.")
        return [checked_vector(embedding.values) for embedding in embeddings]

    def documents(self, title: str, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([f"title: {title} | text: {text}" for text in texts])

    def query(self, query: str) -> list[float]:
        return self._embed([f"task: search result | query: {query}"])[0]


@lru_cache
def get_embedder() -> Embedder:
    profile = embedding_profile()
    if profile.provider == "fake":
        return FakeEmbedder()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EmbeddingFailure("EMBEDDING_CONFIG", "워커의 Gemini API 키를 설정하세요.")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=15_000, retry_options=types.HttpRetryOptions(attempts=1)),
    )
    return GeminiEmbedder(client, profile)
