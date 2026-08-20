"""A bounded worker pool for jobs.

WHY BOUNDED
-----------
A job loads an embedding model and talks to a vector database. Letting every
HTTP request start one would let a handful of callers exhaust memory and
connections at once. The pool caps concurrency at a configured number of
workers; extra submissions queue rather than pile up.

WHY IT LIVES HERE AND NOT IN A ROUTE
------------------------------------
The route's job is to validate, persist and return 202. If the pipeline ran
inside the request handler, the caller would block for the length of an
embedding run and a disconnect would abandon a half-finished job.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

LOGGER = logging.getLogger("erp_pipeline.orchestration.executor")

DEFAULT_MAX_WORKERS = 2


class JobExecutor:
    """Runs job callables on a fixed-size pool, tracking live executions."""

    def __init__(
        self, max_workers: int = DEFAULT_MAX_WORKERS, thread_name_prefix: str = "erp-job"
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")

        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._lock = threading.Lock()
        self._active = 0
        self._peak_active = 0
        self._submitted = 0
        self._completed = 0
        self._closed = False

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def peak_active(self) -> int:
        """The high-water mark. This is what proves the bound held."""
        with self._lock:
            return self._peak_active

    @property
    def submitted(self) -> int:
        with self._lock:
            return self._submitted

    @property
    def completed(self) -> int:
        with self._lock:
            return self._completed

    def submit(self, work: Callable[[], Any]) -> "Future[Any]":
        if self._closed:
            raise RuntimeError("the job executor has been shut down")

        with self._lock:
            self._submitted += 1

        def runner() -> Any:
            with self._lock:
                self._active += 1
                self._peak_active = max(self._peak_active, self._active)

            try:
                return work()
            except Exception:
                # A job that dies must not take the worker with it, and the
                # traceback belongs in the log rather than in an HTTP response.
                LOGGER.exception("job execution raised")
                raise
            finally:
                with self._lock:
                    self._active -= 1
                    self._completed += 1

        return self._pool.submit(runner)

    def shutdown(self, wait: bool = True) -> None:
        self._closed = True
        self._pool.shutdown(wait=wait)

    def __enter__(self) -> "JobExecutor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


class InlineJobExecutor:
    """Runs work immediately on the calling thread.

    For tests that need a job to be finished by the time the call returns.
    Using the real pool there would mean sleeping and hoping, which makes for
    slow and flaky tests.
    """

    def __init__(self) -> None:
        self.max_workers = 1
        self._submitted = 0
        self._completed = 0
        self.peak_active = 1

    @property
    def active(self) -> int:
        return 0

    @property
    def submitted(self) -> int:
        return self._submitted

    @property
    def completed(self) -> int:
        return self._completed

    def submit(self, work: Callable[[], Any]) -> "Future[Any]":
        self._submitted += 1
        future: "Future[Any]" = Future()

        try:
            future.set_result(work())
        except Exception as error:  # noqa: BLE001 - mirrored into the future
            future.set_exception(error)
        finally:
            self._completed += 1

        return future

    def shutdown(self, wait: bool = True) -> None:
        return None


__all__ = ["DEFAULT_MAX_WORKERS", "JobExecutor", "InlineJobExecutor"]
