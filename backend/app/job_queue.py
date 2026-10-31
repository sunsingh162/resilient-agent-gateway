"""Phase 4, Step 4: in-process worker pool for async analysis jobs.

A ThreadPoolExecutor, not Celery/RQ: the swarm and its SQLite checkpointer
already live in this process, and the durability that matters for the
checklist (result recoverable after the client disconnects) comes from
job_store, not from the queue. The trade-off is documented in the README —
a broker-backed queue would also let a job survive the *worker* dying.

The job's work is passed in as a callable so this module stays independent
of the HTTP layer: main.py hands over a closure that runs the turn and
returns the same payload /analyze would.
"""

import logging
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from . import job_store

logger = logging.getLogger("job_queue")

JOB_WORKERS = int(os.getenv("JOB_WORKERS", "4"))

_executor: ThreadPoolExecutor | None = None


def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="analyze-job")
    return _executor


def _run(job_id: str, work: Callable[[], dict]) -> None:
    job_store.mark_running(job_id)
    try:
        result = work()
    except Exception as exc:  # noqa: BLE001 — any failure must land in the job record, not vanish in a thread
        logger.exception("[job_queue] %s failed", job_id)
        job_store.mark_finished(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    job_store.mark_finished(job_id, result.get("status", "completed"), result=result)
    logger.info("[job_queue] %s finished: %s", job_id, result.get("status"))


def submit(thread_id: str, message: str, work: Callable[[], dict]) -> tuple[dict, Future]:
    job = job_store.create_job(thread_id, message)
    future = get_executor().submit(_run, job["job_id"], work)
    return job, future
