"""Offline unit tests for OCI index resolution against the fake registry."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.config import Settings  # noqa: E402
from app.admission.images import parse_image_reference  # noqa: E402
from app.admission.registry import (  # noqa: E402
    NotAnIndex,
    Platform,
    PlatformMissing,
    RegistryClient,
    RegistryError,
)
from tests.fake_registry import FakeRegistry  # noqa: E402

SETTINGS = Settings(
    allowed_registries=("localhost:5000",),
    registry_upstream="http://registry.invalid",
    default_platforms=("linux/amd64", "linux/arm64"),
    request_timeout=1.0,
)
AMD64 = Platform("linux", "amd64")
ARM64 = Platform("linux", "arm64")
BOTH = (AMD64, ARM64)


class RegistryResolutionTests(unittest.TestCase):
    def _client(self, fake: FakeRegistry) -> RegistryClient:
        return RegistryClient(SETTINGS, fetcher=fake.fetcher)

    def test_full_index_resolves_to_index_digest(self):
        fake = FakeRegistry()
        index_digest = fake.add_multi_arch(
            "apps/demo", "v1", ["amd64", "arm64"]
        )
        ref = parse_image_reference("localhost:5000/apps/demo:v1")
        resolved = self._client(fake).resolve_index_digest(ref, BOTH)
        self.assertEqual(resolved, index_digest)
        # tag + two child fetches
        self.assertEqual(len(fake.call_log), 3)

    def test_missing_platform_descriptor_denied(self):
        fake = FakeRegistry()
        fake.add_multi_arch("apps/arm-only", "v1", ["arm64"])
        ref = parse_image_reference("localhost:5000/apps/arm-only:v1")
        with self.assertRaises(PlatformMissing) as ctx:
            self._client(fake).resolve_index_digest(ref, BOTH)
        self.assertIn("linux/amd64", str(ctx.exception))

    def test_arch_selector_checks_only_selected_platform(self):
        fake = FakeRegistry()
        index_digest = fake.add_multi_arch(
            "apps/arm-only", "v1", ["arm64"]
        )
        ref = parse_image_reference("localhost:5000/apps/arm-only:v1")
        resolved = self._client(fake).resolve_index_digest(ref, (ARM64,))
        self.assertEqual(resolved, index_digest)
        # tag + one child fetch only
        self.assertEqual(len(fake.call_log), 2)

    def test_child_manifest_unreachable_by_digest_denied(self):
        fake = FakeRegistry()
        fake.add_multi_arch(
            "apps/broken", "v1", ["amd64", "arm64"], omit_child="amd64"
        )
        ref = parse_image_reference("localhost:5000/apps/broken:v1")
        with self.assertRaises(RegistryError) as ctx:
            self._client(fake).resolve_index_digest(ref, BOTH)
        self.assertIn("清单不存在", str(ctx.exception))

    def test_non_index_manifest_rejected(self):
        fake = FakeRegistry()
        ref = parse_image_reference("localhost:5000/apps/single:v1")
        fake.tags[("apps/single", "v1")] = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {},
            "layers": [],
        }
        with self.assertRaises(NotAnIndex):
            self._client(fake).resolve_index_digest(ref, BOTH)

    def test_digest_mismatch_rejected(self):
        fake = FakeRegistry()
        index_digest = fake.add_multi_arch(
            "apps/demo", "v1", ["amd64", "arm64"]
        )
        # Tamper: serve a different child body under the recorded digest.
        repo_key = ("apps/demo",)
        for key in list(fake.tags):
            manifest = fake.tags[key]
            if manifest.get("mediaType") == "application/vnd.oci.image.manifest.v1+json":
                fake.tags[key] = dict(manifest, tampered=True)
        ref = parse_image_reference("localhost:5000/apps/demo:v1")
        with self.assertRaises(RegistryError) as ctx:
            self._client(fake).resolve_index_digest(ref, BOTH)
        self.assertIn("摘要不一致", str(ctx.exception))
        _ = index_digest, repo_key


if __name__ == "__main__":
    unittest.main()
