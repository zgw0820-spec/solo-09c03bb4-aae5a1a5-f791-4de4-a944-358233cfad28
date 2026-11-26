"""Report registration and publish orchestration.

Reporting
---------
Every report is verified against the registry *before* it is registered:
the child manifest must exist, its digest must match, and the manifest's
config blob must record the reported platform.  Only valid reports reach
the in-memory store.  Invalid content raises :class:`ReportRejected`
(HTTP 422); registry connectivity failures stay :class:`GatewayError`
(HTTP 502).

Publishing and the locking split
---------------------------------
1. snapshot the latest valid reports under the store lock;
2. perform all registry HTTP I/O **without** holding the lock, so reports
   keep being accepted while the index is being written;
3. re-enter the lock and commit atomically, rejecting the publication if a
   higher-attempt report (or any basis digest change) became effective.

Any failure or conflict leaves the previous successful reference intact
and returns it to the caller (when one exists).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..admission.config import Settings
from .models import SUPPORTED_PLATFORMS, PlatformId, Report
from .registry_gateway import (
    OCI_INDEX_MEDIA_TYPE,
    GatewayError,
    RegistryGateway,
    build_index_descriptor,
    verify_report_platform,
)
from .store import BatchSnapshot, MergeStore


@dataclass(frozen=True)
class PublishResult:
    status: str  # "published" | "not_ready" | "conflict" | "failed"
    repository: str
    batch: str
    reference: str | None
    index_digest: str | None
    last_good_reference: str | None
    reason: str | None
    reports: dict[str, Report]


class MergeService:
    def __init__(self, store: MergeStore, gateway: RegistryGateway,
                 settings: Settings):
        self._store = store
        self._gateway = gateway
        self._settings = settings

    # ------------------------------------------------------------- reporting
    def register_report(self, report: Report) -> tuple[str, BatchSnapshot]:
        """Verify *report* against the registry, then register it.

        Returns ``(outcome, snapshot)`` with the same outcomes as
        :meth:`MergeStore.accept_report` (``accepted`` / ``duplicate`` /
        ``superseded``).  Raises :class:`ReportRejected` for content that
        fails platform verification and :class:`ReportConflict` for a
        same-attempt digest mismatch.
        """
        # Registry I/O first, without holding the store lock.
        verify_report_platform(
            self._gateway, report.repository, report.platform, report.digest
        )
        return self._store.accept_report(report)

    def status(self, repository: str, batch: str) -> BatchSnapshot:
        return self._store.get_snapshot(repository, batch)

    def pull_reference(self, repository: str, index_digest: str) -> str:
        return f"{self._settings.public_registry_host}/{repository}@{index_digest}"

    def last_good_reference(self, snapshot: BatchSnapshot) -> str | None:
        if snapshot.last_published_index is None:
            return None
        return self.pull_reference(snapshot.repository,
                                   snapshot.last_published_index)

    # --------------------------------------------------------------- publish
    def publish(self, repository: str, batch: str) -> PublishResult:
        snapshot = self._store.get_snapshot(repository, batch)
        last_good = self.last_good_reference(snapshot)

        missing = [
            name for name in SUPPORTED_PLATFORMS if name not in snapshot.reports
        ]
        if missing:
            reason = (
                f"批次 {batch!r} 尚未收齐两路有效回报, 缺少: {', '.join(missing)}"
            )
            return PublishResult(
                status="not_ready", repository=repository, batch=batch,
                reference=None, index_digest=None,
                last_good_reference=last_good, reason=reason,
                reports=dict(snapshot.reports),
            )

        basis = dict(snapshot.reports)
        try:
            # Re-verify on every publish: a retry must validate the latest
            # reports rather than trusting what registration once saw.
            descriptors = self._build_descriptors(repository, basis)
            index = {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX_MEDIA_TYPE,
                "manifests": descriptors,
            }
            # Registry I/O happens without the store lock: concurrent
            # reports are accepted normally during the write.
            index_digest = self._gateway.put_index(repository, index)
        except GatewayError as exc:
            return PublishResult(
                status="failed", repository=repository, batch=batch,
                reference=None, index_digest=None,
                last_good_reference=last_good, reason=str(exc),
                reports=dict(basis),
            )

        committed, current = self._store.commit_publication(
            repository, batch, basis, index_digest
        )
        refreshed_last_good = self.last_good_reference(current)
        if not committed:
            newer = self._describe_newer_reports(current, basis)
            reason = (
                f"发布冲突: 写索引期间批次 {batch!r} 有更高尝试号的回报生效 "
                f"({newer}); 本次合版作废, 请按最新回报重试"
            )
            return PublishResult(
                status="conflict", repository=repository, batch=batch,
                reference=None, index_digest=None,
                last_good_reference=refreshed_last_good, reason=reason,
                reports=dict(current.reports),
            )

        reference = self.pull_reference(repository, index_digest)
        return PublishResult(
            status="published", repository=repository, batch=batch,
            reference=reference, index_digest=index_digest,
            last_good_reference=reference, reason=None,
            reports=dict(basis),
        )

    # ---------------------------------------------------------------- helpers
    def _build_descriptors(
        self, repository: str, basis: dict[str, Report]
    ) -> list[dict]:
        descriptors: list[dict] = []
        for platform_name in SUPPORTED_PLATFORMS:
            report = basis[platform_name]
            platform = PlatformId.parse(platform_name)
            manifest, raw_manifest = verify_report_platform(
                self._gateway, repository, platform, report.digest
            )
            descriptors.append(
                build_index_descriptor(
                    manifest, report.digest, len(raw_manifest), platform
                )
            )
        return descriptors

    @staticmethod
    def _describe_newer_reports(
        current: BatchSnapshot, basis: dict[str, Report]
    ) -> str:
        parts = []
        for platform_name, basis_report in basis.items():
            newer = current.reports.get(platform_name)
            if newer is not None and (
                newer.attempt > basis_report.attempt
                or newer.digest != basis_report.digest
            ):
                parts.append(
                    f"{platform_name} 尝试号 {basis_report.attempt} -> "
                    f"{newer.attempt} (摘要 {newer.digest})"
                )
        return "; ".join(parts) or "回报集合已变化"
