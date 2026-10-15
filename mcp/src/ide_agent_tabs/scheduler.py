from __future__ import annotations

import contextlib
import heapq
import itertools
import threading
import time
from typing import Callable


class Job:
    def __init__(self, scheduler: Scheduler, work: Callable[[], object], every_s: float | None) -> None:
        self._scheduler = scheduler
        self.work = work
        self.every_s = every_s
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self._scheduler.wake()


class Scheduler:
    def __init__(self, name: str = "agent-tabs-scheduler") -> None:
        self._queue: list[tuple[float, int, Job]] = []
        self._order = itertools.count()
        self._cond = threading.Condition()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def after(self, delay_s: float, work: Callable[[], object]) -> Job:
        return self._add(delay_s, work, None)

    def every(self, every_s: float, work: Callable[[], object]) -> Job:
        return self._add(every_s, work, every_s)

    def soon(self, work: Callable[[], object]) -> Job:
        return self._add(0, work, None)

    def _add(self, delay_s: float, work: Callable[[], object], every_s: float | None) -> Job:
        job = Job(self, work, every_s)
        with self._cond:
            heapq.heappush(self._queue, (time.monotonic() + max(0.0, delay_s), next(self._order), job))
            self._cond.notify()
        return job

    def wake(self) -> None:
        with self._cond:
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._queue.clear()
            self._cond.notify()

    def _next(self) -> Job | None:
        with self._cond:
            while not self._stopped:
                while self._queue and self._queue[0][2].cancelled:
                    heapq.heappop(self._queue)
                if not self._queue:
                    self._cond.wait()
                    continue
                at, _, job = self._queue[0]
                left = at - time.monotonic()
                if left > 0:
                    self._cond.wait(left)
                    continue
                heapq.heappop(self._queue)
                if job.every_s is not None:
                    heapq.heappush(self._queue, (time.monotonic() + job.every_s, next(self._order), job))
                return job
            return None

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            if job.cancelled:
                continue
            with contextlib.suppress(Exception):
                job.work()
