from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class Task(Generic[T]):
    def __init__(self, work: Callable[[], T], name: str = "task") -> None:
        self._work = work
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self._value = self._work()
        except BaseException as e:  # noqa: BLE001
            self._error = e
        finally:
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def result(self, timeout: float | None = None) -> T:
        if not self._done.wait(timeout):
            raise TimeoutError
        if self._error is not None:
            raise self._error
        return self._value


def run_all(jobs: Sequence[Callable[[], T]]) -> list[T]:
    if len(jobs) <= 1:
        return [job() for job in jobs]
    tasks = [Task(job) for job in jobs[1:]]
    first = jobs[0]()
    return [first, *(task.result() for task in tasks)]
