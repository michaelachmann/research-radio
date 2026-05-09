"""Shared constants and utilities for the webui Flask apps."""

import json
import queue
import uuid

MODELS = [
    {"id": "claude-sonnet-4-6",        "label": "Sonnet 4.6",  "thinking": True},
    {"id": "claude-opus-4-7",           "label": "Opus 4.7",    "thinking": True},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",   "thinking": False},
]

_job_store: dict[str, dict] = {}


def create_job() -> tuple[str, queue.Queue]:
    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _job_store[job_id] = {"queue": q}
    return job_id, q


def get_job(job_id: str) -> dict | None:
    return _job_store.get(job_id)


def event_gen(job_id: str, terminal_types: tuple = ("done", "error")):
    """SSE generator — yields JSON-encoded data frames until a terminal message."""
    job = _job_store.get(job_id)
    if not job:
        return
    q = job["queue"]
    while True:
        try:
            msg = q.get(timeout=25)
            yield f"data: {json.dumps(msg)}\n\n"
            if msg["type"] in terminal_types:
                break
        except queue.Empty:
            yield 'data: {"type":"ping"}\n\n'
