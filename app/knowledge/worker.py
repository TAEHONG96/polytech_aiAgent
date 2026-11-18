import logging
import time
from contextlib import asynccontextmanager
from threading import Event, Thread
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.knowledge.embedding import MAX_QUERY_LENGTH, EmbeddingFailure, get_embedder
from app.knowledge.indexing import clean_orphan_files, process_next

logger = logging.getLogger(__name__)


def process_queue(stop: Event) -> None:
    next_cleanup = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + 900
                clean_orphan_files()
            if process_next():
                continue
        except Exception as exc:
            logger.warning("knowledge worker iteration failed type=%s", type(exc).__name__)
        stop.wait(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = Event()
    worker = Thread(target=process_queue, args=(stop,), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class QueryEmbedding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH)]
    profile: Annotated[str, StringConstraints(min_length=1, max_length=120)]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/query-embedding")
def query_embedding(body: QueryEmbedding):
    try:
        provider = get_embedder()
        if provider.profile.fingerprint != body.profile:
            raise HTTPException(409, "Embedding profile mismatch")
        return {"profile": provider.profile.fingerprint, "vector": provider.query(body.query)}
    except EmbeddingFailure as exc:
        status = 429 if exc.code == "EMBEDDING_RATE_LIMIT" else 503
        raise HTTPException(status, exc.code) from exc
