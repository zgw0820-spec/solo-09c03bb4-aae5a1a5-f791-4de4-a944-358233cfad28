"""Offline tests for the batch merge (合版) feature.

Covers the store rules (highest-attempt wins, same-attempt digest conflict,
late reports ignored), config-blob platform verification, publish
preconditions, last-good fallback, and the two required race scenarios:

* a late lower-attempt report arriving after a higher one is registered;
* a higher-attempt report landing while the OCI index is being written.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.config import Settings  # noqa: E402
from app.merger.models import (  # noqa: E402
    MergeValidationError,
    PlatformId,
    Report,
    validate_attempt,
    validate_batch,
    validate_digest,
    validate_repository,
)
from app.merger.registry_gateway import (  # noqa: E402
    GatewayError,
    ReportRejected,
    build_index_descriptor,
    canonical_json,
    digest_of,
    verify_report_platform,
)
from app.merger.service import MergeService  # noqa: E402
from app.merger.store import MergeStore, ReportConflict  # noqa: E402
from tests.fake_gateway import FakeGateway, OCI_MANIFEST  # noqa: E402

SETTINGS = Settings(
    allowed_registries=("localhost:5000", "registry:5000"),
    registry_upstream="http://registry.invalid",
    public_registry_host="localhost:5000",
    default_platforms=("linux/amd64", "linux/arm64"),
    request_timeout=1.0,
)
AMD = PlatformId.parse("linux/amd64")
ARM = PlatformId.parse("linux/arm64")
REPO = "apps/demo"
BATCH = "build-42"


def make_service() -> tuple[MergeService, FakeGateway]:
    gateway = FakeGateway()
    service = MergeService(MergeStore(), gateway, SETTINGS)
    return service, gateway


def report(platform: PlatformId, attempt: int, digest: str,
           repository: str = REPO, batch: str = BATCH) -> Report:
    return Report(
        repository=repository, batch=batch, platform=platform,
        attempt=attempt, digest=digest,
    )


def seed_two_children(gateway: FakeGateway,
                      repository: str = REPO) -> dict[str, str]:
    """Push one amd64 and one arm64 child; return platform -> digest."""
    amd_digest, _ = gateway.add_child(repository, AMD)
    arm_digest, _ = gateway.add_child(repository, ARM)
    return {"linux/amd64": amd_digest, "linux/arm64": arm_digest}


class ValidationTests(unittest.TestCase):
    def test_repository_paths(self):
        self.assertEqual(validate_repository("apps/demo"), "apps/demo")
        self.assertEqual(validate_repository("  a/b-c/1 "), "a/b-c/1")
        for bad in ("", "  ", "apps//demo", "apps/Demo", "apps/demo!",
                    "/apps/demo", "apps/demo/"):
            with self.assertRaises(MergeValidationError):
                validate_repository(bad)

    def test_batch_ids(self):
        self.assertEqual(validate_batch("b1"), "b1")
        self.assertEqual(validate_batch("BUILD.2026-11_26"),
                         "BUILD.2026-11_26")
        for bad in ("", " ", "-bad", "bad id", "x" * 129):
            with self.assertRaises(MergeValidationError):
                validate_batch(bad)

    def test_digest_format(self):
        good = "sha256:" + "a" * 64
        self.assertEqual(validate_digest(" " + good + " "), good)
        for bad in ("sha256:" + "a" * 63, "sha256:" + "g" * 64,
                    "sha512:" + "a" * 64, ""):
            with self.assertRaises(MergeValidationError):
                validate_digest(bad)

    def test_attempt_and_platform(self):
        self.assertEqual(validate_attempt(1), 1)
        for bad in (0, -1, 1.5, True, False, "2"):
            with self.assertRaises(MergeValidationError):
                validate_attempt(bad)
        self.assertEqual(str(PlatformId.parse("linux/arm64")), "linux/arm64")
        for bad in ("linux/riscv64", "windows/amd64", "", "amd64"):
            with self.assertRaises(MergeValidationError):
                PlatformId.parse(bad)


class StoreRulesTests(unittest.TestCase):
    def setUp(self):
        self.store = MergeStore()

    def _accept(self, *args, **kwargs):
        return self.store.accept_report(report(*args, **kwargs))

    def test_first_report_accepted(self):
        outcome, snapshot = self._accept(AMD, 1, "sha256:" + "1" * 64)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(snapshot.reports["linux/amd64"].attempt, 1)

    def test_higher_attempt_replaces_lower(self):
        d1 = "sha256:" + "1" * 64
        d2 = "sha256:" + "2" * 64
        self.assertEqual(self._accept(AMD, 1, d1)[0], "accepted")
        self.assertEqual(self._accept(AMD, 2, d2)[0], "accepted")
        snap = self.store.get_snapshot(REPO, BATCH)
        self.assertEqual(snap.reports["linux/amd64"].digest, d2)
        self.assertEqual(snap.reports["linux/amd64"].attempt, 2)

    def test_late_lower_attempt_is_superseded_and_ignored(self):
        """迟到回报: 更大尝试号已生效, 旧号回报不能覆盖它。"""
        d1 = "sha256:" + "1" * 64
        d2 = "sha256:" + "2" * 64
        self._accept(AMD, 2, d2)
        outcome, snapshot = self._accept(AMD, 1, d1)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(snapshot.reports["linux/amd64"].digest, d2)
        self.assertEqual(snapshot.reports["linux/amd64"].attempt, 2)

    def test_same_attempt_same_digest_is_duplicate(self):
        d1 = "sha256:" + "1" * 64
        self._accept(AMD, 1, d1)
        outcome, _ = self._accept(AMD, 1, d1)
        self.assertEqual(outcome, "duplicate")

    def test_same_attempt_different_digest_conflicts(self):
        d1 = "sha256:" + "1" * 64
        d2 = "sha256:" + "2" * 64
        self._accept(AMD, 1, d1)
        with self.assertRaises(ReportConflict) as ctx:
            self._accept(AMD, 1, d2)
        self.assertEqual(ctx.exception.existing.digest, d1)
        self.assertEqual(ctx.exception.incoming.digest, d2)
        # Conflict must not mutate stored state.
        self.assertEqual(
            self.store.get_snapshot(REPO, BATCH).reports["linux/amd64"].digest,
            d1,
        )

    def test_platforms_are_independent_slots(self):
        d_amd = "sha256:" + "a" * 64
        d_arm = "sha256:" + "b" * 64
        self._accept(AMD, 1, d_amd)
        self._accept(ARM, 3, d_arm)
        snap = self.store.get_snapshot(REPO, BATCH)
        self.assertEqual(snap.reports["linux/amd64"].attempt, 1)
        self.assertEqual(snap.reports["linux/arm64"].attempt, 3)

    def test_batches_and_repositories_are_isolated(self):
        self._accept(AMD, 2, "sha256:" + "1" * 64, batch=BATCH)
        self._accept(AMD, 1, "sha256:" + "2" * 64, batch="other")
        self.assertEqual(
            self.store.get_snapshot(REPO, BATCH).reports["linux/amd64"].attempt,
            2,
        )
        self.assertEqual(
            self.store.get_snapshot(REPO, "other")
            .reports["linux/amd64"].attempt,
            1,
        )


class PlatformVerificationTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()

    def test_config_blob_matching_platform_passes(self):
        digest, manifest = self.gateway.add_child(REPO, AMD)
        returned, raw = verify_report_platform(self.gateway, REPO, AMD, digest)
        self.assertIs(returned, manifest)
        self.assertEqual(digest_of(raw), digest)

    def test_config_blob_mismatch_is_rejected(self):
        # Store an arm64 config blob but report the digest as amd64.
        digest, _ = self.gateway.add_child(REPO, ARM)
        with self.assertRaises(ReportRejected) as ctx:
            verify_report_platform(self.gateway, REPO, AMD, digest)
        self.assertIn("平台核对失败", str(ctx.exception))

    def test_missing_manifest_is_rejected(self):
        missing = "sha256:" + "f" * 64
        with self.assertRaises(ReportRejected) as ctx:
            verify_report_platform(self.gateway, REPO, AMD, missing)
        self.assertIn("不存在", str(ctx.exception))

    def test_unsupported_manifest_media_type_rejected(self):
        index_like = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [],
        }
        # Hand-craft raw bytes whose digest equals the lookup key while the
        # media type is still an unsupported one for a *child* manifest.
        raw = canonical_json(index_like)
        with self.assertRaises(ReportRejected):
            verify_report_platform(
                self.gateway, REPO, AMD, digest_of(raw)
            )

    def test_descriptor_built_from_verified_manifest(self):
        digest, manifest = self.gateway.add_child(REPO, ARM)
        descriptor = build_index_descriptor(
            manifest, digest, len(canonical_json(manifest)), ARM
        )
        self.assertEqual(descriptor["digest"], digest)
        self.assertEqual(
            descriptor["platform"], {"os": "linux", "architecture": "arm64"}
        )
        self.assertEqual(descriptor["mediaType"], OCI_MANIFEST)


class RegistrationTests(unittest.TestCase):
    def test_valid_report_is_verified_then_registered(self):
        service, gateway = make_service()
        digest, _ = gateway.add_child(REPO, AMD)
        outcome, snapshot = service.register_report(report(AMD, 1, digest))
        self.assertEqual(outcome, "accepted")
        self.assertEqual(snapshot.reports["linux/amd64"].digest, digest)

    def test_invalid_report_is_rejected_and_not_registered(self):
        service, gateway = make_service()
        # arm64 content reported as amd64 -> config blob platform mismatch.
        arm_digest, _ = gateway.add_child(REPO, ARM)
        with self.assertRaises(ReportRejected):
            service.register_report(report(AMD, 1, arm_digest))
        # Nothing was stored for the amd64 slot.
        snap = service.status(REPO, BATCH)
        self.assertNotIn("linux/amd64", snap.reports)

    def test_missing_manifest_report_is_rejected_and_not_registered(self):
        service, _gateway = make_service()
        ghost = "sha256:" + "0" * 64
        with self.assertRaises(ReportRejected):
            service.register_report(report(AMD, 1, ghost))
        self.assertEqual(service.status(REPO, BATCH).reports, {})

    def test_registry_connectivity_error_is_gateway_error_not_rejected(self):
        class BrokenGateway(FakeGateway):
            def fetch_manifest(self, repository, digest):
                raise GatewayError("连接被拒绝")

        gateway = BrokenGateway()
        service = MergeService(MergeStore(), gateway, SETTINGS)
        with self.assertRaises(GatewayError):
            service.register_report(
                report(AMD, 1, "sha256:" + "1" * 64)
            )
        # A transport failure must not register anything either.
        self.assertEqual(service.status(REPO, BATCH).reports, {})

    def test_conflict_after_verification_keeps_original(self):
        service, gateway = make_service()
        first, _ = gateway.add_child(REPO, AMD)
        second, _ = gateway.add_child(
            REPO, AMD, config_extra={"build": "again"}
        )
        service.register_report(report(AMD, 1, first))
        with self.assertRaises(ReportConflict):
            service.register_report(report(AMD, 1, second))
        self.assertEqual(
            service.status(REPO, BATCH).reports["linux/amd64"].digest, first
        )


class PublishTests(unittest.TestCase):
    def test_publish_blocked_until_both_reports_present(self):
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "not_ready")
        self.assertIn("linux/arm64", result.reason)
        self.assertIsNone(result.reference)
        self.assertEqual(gateway.pushed_indexes.get(REPO), None)

    def test_happy_path_writes_index_and_returns_pullable_reference(self):
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))

        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "published")
        self.assertTrue(result.reference.startswith(
            f"localhost:5000/{REPO}@sha256:"
        ))
        self.assertEqual(
            result.reference,
            f"localhost:5000/{REPO}@{result.index_digest}",
        )

        index = gateway.last_index(REPO)
        self.assertEqual(
            index["mediaType"], "application/vnd.oci.image.index.v1+json"
        )
        self.assertEqual(len(index["manifests"]), 2)
        platforms = {
            (m["platform"]["os"], m["platform"]["architecture"])
            for m in index["manifests"]
        }
        self.assertEqual(platforms, {("linux", "amd64"),
                                     ("linux", "arm64")})
        recorded = {m["digest"] for m in index["manifests"]}
        self.assertEqual(recorded, set(digests.values()))
        # The pushed index digest must match the pullable reference.
        self.assertEqual(result.index_digest,
                         digest_of(canonical_json(index)))
        # Status now remembers the last good publication.
        snap = service.status(REPO, BATCH)
        self.assertEqual(snap.last_published_index, result.index_digest)

    def test_publish_verifies_platform_against_config_blob(self):
        service, gateway = make_service()
        arm_digest, _ = gateway.add_child(REPO, ARM)
        real_amd, _ = gateway.add_child(REPO, AMD)
        # Lie: report the arm64 child under the amd64 slot.
        service._store.accept_report(report(AMD, 1, arm_digest))
        service._store.accept_report(report(ARM, 1, real_amd))
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "failed")
        self.assertIn("平台核对失败", result.reason)
        self.assertNotIn(REPO, gateway.pushed_indexes)

    def test_missing_child_manifest_fails_and_keeps_last_good(self):
        service, gateway = make_service()
        first = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, first["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, first["linux/arm64"]))
        published = service.publish(REPO, BATCH)
        self.assertEqual(published.status, "published")
        last_good = published.reference

        # Attempt 2: amd64 reports a digest that was never pushed.
        ghost = "sha256:" + "0" * 64
        service._store.accept_report(report(AMD, 2, ghost))
        service._store.accept_report(report(ARM, 2, first["linux/arm64"]))
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.reference)
        self.assertEqual(result.last_good_reference, last_good)
        # The previously published index digest stays recorded.
        self.assertEqual(
            service.status(REPO, BATCH).last_published_index,
            published.index_digest,
        )

    def test_retry_after_failure_merges_latest_valid_reports(self):
        service, gateway = make_service()
        first = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, first["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, first["linux/arm64"]))
        first_result = service.publish(REPO, BATCH)
        self.assertEqual(first_result.status, "published")

        # Attempt 2 amd64 is broken, publish fails.
        ghost = "sha256:" + "0" * 64
        service._store.accept_report(report(AMD, 2, ghost))
        service._store.accept_report(report(ARM, 2, first["linux/arm64"]))
        failed = service.publish(REPO, BATCH)
        self.assertEqual(failed.status, "failed")

        # Attempt 3 with a real child: retry succeeds using latest reports.
        new_amd, _ = gateway.add_child(REPO, AMD, config_extra={"build": "newer"})
        service._store.accept_report(report(AMD, 3, new_amd))
        retried = service.publish(REPO, BATCH)
        self.assertEqual(retried.status, "published")
        index = gateway.last_index(REPO)
        digests_in_index = {m["digest"] for m in index["manifests"]}
        self.assertIn(new_amd, digests_in_index)
        self.assertIn(first["linux/arm64"], digests_in_index)

    def test_put_index_failure_returns_last_good(self):
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))
        first = service.publish(REPO, BATCH)
        self.assertEqual(first.status, "published")

        # New attempt so a second publication would produce a new index.
        new_amd, _ = gateway.add_child(REPO, AMD, config_extra={"build": "newer"})
        service._store.accept_report(report(AMD, 2, new_amd))
        service._store.accept_report(report(ARM, 2, digests["linux/arm64"]))
        gateway.fail_put = True
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.last_good_reference, first.reference)

    def test_higher_attempt_during_index_write_conflicts(self):
        """写索引期间换报: 更高尝试号在提交前生效 -> 本次发布冲突。"""
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))
        first = service.publish(REPO, BATCH)
        self.assertEqual(first.status, "published")

        new_amd, _ = gateway.add_child(REPO, AMD, config_extra={"build": "newer"})

        def swap_report(_repository: str) -> None:
            # Fires while publish() is inside put_index, before commit.
            outcome, _ = service._store.accept_report(
                report(AMD, 2, new_amd)
            )
            self.assertEqual(outcome, "accepted")

        gateway.before_put_index = swap_report
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "conflict")
        self.assertIn("更高尝试号", result.reason)
        # The conflicting publication is not recorded; last good survives.
        self.assertEqual(result.last_good_reference, first.reference)
        snap = service.status(REPO, BATCH)
        self.assertEqual(snap.last_published_index, first.index_digest)
        self.assertEqual(snap.reports["linux/amd64"].attempt, 2)

        # Retry with the latest reports succeeds.
        gateway.before_put_index = None
        retried = service.publish(REPO, BATCH)
        self.assertEqual(retried.status, "published")
        index = gateway.last_index(REPO)
        self.assertIn(new_amd, {m["digest"] for m in index["manifests"]})

    def test_same_attempt_swap_during_write_also_conflicts(self):
        """Defence in depth: commit rejects any digest change in the basis."""
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))
        snapshot = service.status(REPO, BATCH)

        replacement, _ = gateway.add_child(
            REPO, AMD, config_extra={"build": "replacement"}
        )
        # Simulate a slot mutation that bypassed accept_report (the API
        # path itself rejects same-attempt digest swaps with 409).
        state = service._store._state(REPO, BATCH)
        state.slot_for(AMD).report = report(AMD, 1, replacement)
        self.assertNotEqual(
            replacement, snapshot.reports["linux/amd64"].digest
        )

        committed, current = service._store.commit_publication(
            REPO, BATCH, dict(snapshot.reports), "sha256:" + "c" * 64
        )
        self.assertFalse(committed)
        self.assertIsNone(current.last_published_index)

    def test_report_accepted_concurrently_during_index_write(self):
        """Reports keep flowing while an index is being written (lock split)."""
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))

        accepted_during_write = threading.Event()

        def unrelated_report(_repository: str) -> None:
            # A different batch must be writable during the I/O window.
            other_digest, _ = gateway.add_child("apps/other", AMD)
            outcome, _ = service._store.accept_report(
                report(AMD, 1, other_digest, repository="apps/other",
                       batch="b-9")
            )
            self.assertEqual(outcome, "accepted")
            accepted_during_write.set()

        gateway.before_put_index = unrelated_report
        result = service.publish(REPO, BATCH)
        self.assertEqual(result.status, "published")
        self.assertTrue(accepted_during_write.wait(2))

    def test_two_publishers_only_first_commits_when_report_changes(self):
        service, gateway = make_service()
        digests = seed_two_children(gateway)
        service._store.accept_report(report(AMD, 1, digests["linux/amd64"]))
        service._store.accept_report(report(ARM, 1, digests["linux/arm64"]))
        new_amd, _ = gateway.add_child(REPO, AMD, config_extra={"build": "newer"})

        results: list = []
        swap_done = threading.Event()

        def publisher() -> None:
            results.append(service.publish(REPO, BATCH))

        def swap_once(_repository: str) -> None:
            # The first publisher to reach the registry write observes a
            # higher-attempt report landing before its commit.
            if not swap_done.is_set():
                service._store.accept_report(report(AMD, 2, new_amd))
                swap_done.set()

        gateway.before_put_index = swap_once
        t1 = threading.Thread(target=publisher)
        t2 = threading.Thread(target=publisher)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        statuses = sorted(r.status for r in results)
        # One publication conflicts on the racing report, the other (a
        # retry against the latest state) may publish; neither may raise.
        self.assertIn("conflict", statuses)
        self.assertTrue(swap_done.is_set())


if __name__ == "__main__":
    unittest.main()
