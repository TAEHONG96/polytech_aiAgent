"""QA-only ASGI entry point that measures API, database and worker latency."""

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import event

from app.db import engine
from app.knowledge import routes as knowledge_routes
from main import app as app

if os.environ.get("QA_API_PROFILE") != "1" or os.environ.get("AGENT_PROVIDER") != "fake":
    raise RuntimeError("Use the timing server only with an isolated Fake QA stack.")


@dataclass
class Timings:
    sql_count: int = 0
    sql_ms: float = 0
    pool_ms: float = 0
    embedding_worker_ms: float = 0


current: ContextVar[Timings | None] = ContextVar("qa_timings", default=None)
db_engine = engine()
checkout = db_engine.pool.connect
query_vector = knowledge_routes.worker_query_vector


def timed_checkout():
    timing = current.get()
    started = perf_counter()
    try:
        return checkout()
    finally:
        if timing is not None:
            timing.pool_ms += (perf_counter() - started) * 1000


def timed_query_vector(query, profile):
    timing = current.get()
    started = perf_counter()
    try:
        return query_vector(query, profile)
    finally:
        if timing is not None:
            timing.embedding_worker_ms += (perf_counter() - started) * 1000


db_engine.pool.connect = timed_checkout
knowledge_routes.worker_query_vector = timed_query_vector


@event.listens_for(db_engine, "before_cursor_execute")
def before_cursor_execute(_connection, _cursor, _statement, _parameters, context, _executemany):
    if current.get() is not None:
        context.qa_started_at = perf_counter()


@event.listens_for(db_engine, "after_cursor_execute")
def after_cursor_execute(_connection, _cursor, _statement, _parameters, context, _executemany):
    timing = current.get()
    if timing is not None:
        timing.sql_count += 1
        timing.sql_ms += (perf_counter() - context.qa_started_at) * 1000


@app.middleware("http")
async def report_timings(request, call_next):
    if "x-qa-operation" not in request.headers:
        return await call_next(request)
    timing = Timings()
    token = current.set(timing)
    started = perf_counter()
    try:
        response = await call_next(request)
        print("QA_TIMING " + json.dumps({
            "request_id": response.headers.get("x-request-id"),
            "fastapi_ms": round((perf_counter() - started) * 1000, 3),
            "sql_count": timing.sql_count,
            "sql_ms": round(timing.sql_ms, 3),
            "pool_ms": round(timing.pool_ms, 3),
            "embedding_worker_ms": round(timing.embedding_worker_ms, 3),
        }), flush=True)
        return response
    finally:
        current.reset(token)
