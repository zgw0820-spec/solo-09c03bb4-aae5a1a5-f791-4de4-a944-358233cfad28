"""In-process, thread-safe state store for batch reports and publications.

All state lives in a single process and is lost on restart (the task only
requires in-memory state; no persistence and no tag mutation).  One
:class:`MergeStore` instance is shared by the FastAPI dependency.

Concurrency model
-----------------
FastAPI runs sync endpoints in a worker thread pool, so every mutation and
every consistent snapshot takes :attr:`MergeStore._lock`.  Registry I/O
during a publish happens *outside* the lock so reports keep being accepted
while an index is being written; the publish then re-enters the lock to
check for racing higher-attempt reports and commit atomically.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .models import PlatformId, Report


class ReportConflict(Exception):
    """Same batch+platform+attempt reported again with a different digest."""

    def __init__(self, existing: Report, incoming: Report):
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"回报冲突: 批次 {incoming.batch!r} 平台 {incoming.platform_name} "
            f"尝试号 {incoming.attempt} 已登记摘要 {existing.digest}, "
            f"又收到不同摘要 {incoming.digest}"
        )


@dataclass
class PlatformSlot:
    """Latest valid report for one platform within a batch."""

    report: Optional[Report] = None


@dataclass
class BatchState:
    repository: str
    batch: str
    slots: dict[str, PlatformSlot] = field(default_factory=dict)
    # Digest of the last successfully published OCI index for this batch.
    last_published_index: Optional[str] = None
    # Report snapshots used by the last successful publish (for status).
    last_published_reports: Optional[dict[str, Report]] = None

    def slot_for(self, platform: PlatformId) -> PlatformSlot:
        slot = self.slots.get(str(platform))
        if slot is None:
            slot = PlatformSlot()
            self.slots[str(platform)] = slot
        return slot


@dataclass(frozen=True)
class BatchSnapshot:
    """Immutable view of a batch used by the publish service."""

    repository: str
    batch: str
    reports: dict[str, Report]
    last_published_index: Optional[str]


class MergeStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[tuple[str, str], BatchState] = None  # type: ignore[assignment]
        self._batches = {}

    # ------------------------------------------------------------------ reports
    def accept_report(self, report: Report) -> tuple[str, BatchSnapshot]:
        """Register *report*.

        Returns ``(outcome, snapshot)`` where outcome is one of:

        * ``"accepted"``  - stored as the new latest valid report;
        * ``"duplicate"`` - identical attempt+digest, no state change;
        * ``"superseded"`` - a higher attempt is already registered, so the
          late report is ignored;
        * raises :class:`ReportConflict` on same attempt, different digest.
        """
        with self._lock:
            state = self._state(report.repository, report.batch)
            slot = state.slot_for(report.platform)
            current = slot.report

            if current is not None:
                if report.attempt == current.attempt:
                    if report.digest == current.digest:
                        return "duplicate", self._snapshot(state)
                    raise ReportConflict(current, report)
                if report.attempt < current.attempt:
                    # Late report for an older attempt: the newest valid
                    # report always wins, ignore it without touching state.
                    return "superseded", self._snapshot(state)

            slot.report = report
            return "accepted", self._snapshot(state)

    def get_snapshot(self, repository: str, batch: str) -> BatchSnapshot:
        with self._lock:
            return self._snapshot(self._state(repository, batch))

    # ------------------------------------------------------------------ publish
    def commit_publication(
        self,
        repository: str,
        batch: str,
        basis: dict[str, Report],
        index_digest: str,
    ) -> tuple[bool, BatchSnapshot]:
        """Atomically validate the publish basis and record the new index.

        Returns ``(committed, snapshot)``.  ``committed`` is ``False`` when
        a higher-attempt report for any platform became effective after the
        publish basis was snapshotted (the caller must surface this as a
        conflict and keep the previous successful reference).
        """
        with self._lock:
            state = self._state(repository, batch)
            for platform_name, basis_report in basis.items():
                current = state.slot_for(
                    PlatformId.parse(platform_name)
                ).report
                if current is None:
                    return False, self._snapshot(state)
                # A higher attempt that landed during the write, or any
                # change of the basis digest, invalidates this publication.
                if (
                    current.attempt > basis_report.attempt
                    or current.digest != basis_report.digest
                ):
                    return False, self._snapshot(state)

            state.last_published_index = index_digest
            state.last_published_reports = dict(basis)
            return True, self._snapshot(state)

    # ------------------------------------------------------------------- helpers
    def _state(self, repository: str, batch: str) -> BatchState:
        key = (repository, batch)
        state = self._batches.get(key)
        if state is None:
            state = BatchState(repository=repository, batch=batch)
            self._batches[key] = state
        return state

    def _snapshot(self, state: BatchState) -> BatchSnapshot:
        reports = {
            platform_name: slot.report
            for platform_name, slot in state.slots.items()
            if slot.report is not None
        }
        return BatchSnapshot(
            repository=state.repository,
            batch=state.batch,
            reports=dict(reports),
            last_published_index=state.last_published_index,
        )
