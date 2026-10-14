"""Per-task reservations that keep app-owned task changes out of an in-flight publication.

Forge is one process with one event loop, so an in-memory registry that is
checked and updated with no ``await`` in between is atomic for every request
and background task. A reservation is not a lock: nothing ever waits for one.
A conflicting request is refused at once, and a reservation covers only the
app's own short admission or recording step, never an SDK turn or its callbacks.

Rules, per task:

- ``PUBLICATION`` is exclusive. It is refused while anything else holds the
  task, and while it is held every other purpose except ``FINALIZATION`` is
  refused with ``publication_in_progress``.
- ``FINALIZATION`` is never refused: it records an agent turn that has already
  ended (terminal status through candidate recording). A publication that sees
  it refuses with ``task_busy``, including at its push-boundary revalidation.
- Other purposes (continuation admission, starting checks or a candidate
  review, approval) may overlap each other; their own guards cover that.

There is no cross-process or distributed coordination: a second service
process against the same database is not supported.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


class Purpose:
    PUBLICATION = "publication"
    # Follow-up or recovery: from the admission checks until the attempt is committed and the task marked active.
    ADMISSION = "admission"
    CHECKS = "checks"
    CANDIDATE_REVIEW = "candidate_review"
    APPROVAL = "approval"
    # A finished turn's terminal status through its candidate recording.
    FINALIZATION = "finalization"


PUBLICATION_IN_PROGRESS = "publication_in_progress"
TASK_BUSY = "task_busy"

_held: dict[int, Counter[str]] = {}


class TaskReservedError(Exception):
    """A request refused because a conflicting reservation holds the task."""

    status_code = 409

    def __init__(self, task_id: int, purpose: str, holders: list[str]):
        self.task_id = task_id
        self.purpose = purpose
        self.holders = holders
        if Purpose.PUBLICATION in holders:
            self.detail = PUBLICATION_IN_PROGRESS
            reason = "this task's candidate is being published; retry when publication finishes"
        else:
            self.detail = TASK_BUSY
            reason = f"this task is busy ({', '.join(holders)}); retry when it finishes"
        self.extra: dict[str, Any] = {"reason": reason, "reserved_by": holders}
        super().__init__(self.detail)


class Reservation:
    """A held reservation; release it exactly once, normally with ``with``."""

    def __init__(self, task_id: int, purpose: str):
        self.task_id = task_id
        self.purpose = purpose
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        counts = _held.get(self.task_id)
        if counts is None:
            return
        counts[self.purpose] -= 1
        if counts[self.purpose] <= 0:
            del counts[self.purpose]
        if not counts:
            _held.pop(self.task_id, None)

    def others(self) -> list[str]:
        """Purposes held on this task besides this reservation itself."""
        counts = Counter(_held.get(self.task_id, {}))
        if not self._released:
            counts[self.purpose] -= 1
        return sorted(p for p, n in counts.items() if n > 0)

    def __enter__(self) -> Reservation:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def holders(task_id: int) -> list[str]:
    return sorted(_held.get(task_id, {}))


def claim(task_id: int, purpose: str) -> Reservation:
    """Reserve ``task_id`` for ``purpose`` or raise ``TaskReservedError``; see the module rules."""
    current = holders(task_id)
    if purpose == Purpose.PUBLICATION:
        conflict = bool(current)
    elif purpose == Purpose.FINALIZATION:
        conflict = False
    else:
        conflict = Purpose.PUBLICATION in current
    if conflict:
        raise TaskReservedError(task_id, purpose, current)
    _held.setdefault(task_id, Counter())[purpose] += 1
    return Reservation(task_id, purpose)


def reset_for_tests() -> None:
    _held.clear()


__all__ = [
    "PUBLICATION_IN_PROGRESS",
    "TASK_BUSY",
    "Purpose",
    "Reservation",
    "TaskReservedError",
    "claim",
    "holders",
    "reset_for_tests",
]
