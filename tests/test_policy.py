"""Offline unit tests for the admission policy and JSON patch building."""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.config import Settings  # noqa: E402
from app.admission.policy import (  # noqa: E402
    AdmissionFailure,
    build_patches,
    select_platforms,
)
from app.admission.registry import Platform, RegistryClient  # noqa: E402
from tests.fake_registry import FakeRegistry  # noqa: E402

SETTINGS = Settings(
    allowed_registries=("localhost:5000", "registry:5000"),
    registry_upstream="http://registry.invalid",
    default_platforms=("linux/amd64", "linux/arm64"),
    request_timeout=1.0,
)


def deployment(images, init_images=None, node_selector=None):
    containers = [
        {"name": f"c{i}", "image": image}
        for i, image in enumerate(images)
    ]
    spec = {"containers": containers}
    if init_images:
        spec["initContainers"] = [
            {"name": f"init{i}", "image": image}
            for i, image in enumerate(init_images)
        ]
    if node_selector:
        spec["nodeSelector"] = node_selector
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "demo"},
        "spec": {
            "selector": {"matchLabels": {"app": "demo"}},
            "template": {"metadata": {"labels": {"app": "demo"}}, "spec": spec},
        },
    }


class PlatformSelectionTests(unittest.TestCase):
    def test_defaults_to_both_platforms(self):
        self.assertEqual(
            select_platforms({}, SETTINGS),
            (Platform("linux", "amd64"), Platform("linux", "arm64")),
        )

    def test_arch_selector_pins_single_platform(self):
        platforms = select_platforms(
            {"nodeSelector": {"kubernetes.io/arch": "arm64"}}, SETTINGS
        )
        self.assertEqual(platforms, (Platform("linux", "arm64"),))

    def test_empty_arch_value_denied(self):
        with self.assertRaises(AdmissionFailure):
            select_platforms(
                {"nodeSelector": {"kubernetes.io/arch": "  "}}, SETTINGS
            )


class PatchBuildingTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRegistry()
        self.digest_v1 = self.fake.add_multi_arch(
            "apps/demo", "v1", ["amd64", "arm64"]
        )
        self.digest_arm = self.fake.add_multi_arch(
            "apps/arm-only", "v1", ["arm64"]
        )
        self.fake.add_multi_arch(
            "apps/broken", "v1", ["amd64", "arm64"], omit_child="amd64"
        )
        self.client = RegistryClient(SETTINGS, fetcher=self.fake.fetcher)

    def test_tagged_containers_and_init_containers_are_pinned(self):
        obj = deployment(
            ["localhost:5000/apps/demo:v1"],
            init_images=["localhost:5000/apps/demo:v1"],
        )
        patches = build_patches(obj, self.client, SETTINGS)
        paths = [patch["path"] for patch in patches]
        self.assertEqual(
            paths,
            [
                "/spec/template/spec/initContainers/0/image",
                "/spec/template/spec/containers/0/image",
            ],
        )
        for patch in patches:
            self.assertEqual(patch["op"], "replace")
            self.assertEqual(patch["value"],
                             f"localhost:5000/apps/demo@{self.digest_v1}")

    def test_same_tag_resolved_only_once_per_admission(self):
        obj = deployment(
            [
                "localhost:5000/apps/demo:v1",
                "localhost:5000/apps/demo:v1",
            ],
            init_images=["localhost:5000/apps/demo:v1"],
        )
        build_patches(obj, self.client, SETTINGS)
        tag_fetches = [
            key for key in self.fake.call_log if key[1] == "v1"
        ]
        self.assertEqual(tag_fetches, [("apps/demo", "v1")])

    def test_already_pinned_image_is_untouched_and_not_queried(self):
        digest = "sha256:" + "c" * 64
        obj = deployment([f"localhost:5000/apps/demo@{digest}"])
        patches = build_patches(obj, self.client, SETTINGS)
        self.assertEqual(patches, [])
        self.assertEqual(self.fake.call_log, [])

    def test_arch_selector_allows_arm_only_index(self):
        obj = deployment(
            ["localhost:5000/apps/arm-only:v1"],
            node_selector={"kubernetes.io/arch": "arm64"},
        )
        patches = build_patches(obj, self.client, SETTINGS)
        self.assertEqual(
            patches[0]["value"],
            f"localhost:5000/apps/arm-only@{self.digest_arm}",
        )

    def test_missing_platform_denies_whole_request_without_patch(self):
        obj = deployment(["localhost:5000/apps/arm-only:v1"])
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(obj, self.client, SETTINGS)
        self.assertIn("apps/arm-only:v1", str(ctx.exception))
        self.assertIn("linux/amd64", str(ctx.exception))

    def test_missing_child_manifest_denies_whole_request(self):
        obj = deployment(["localhost:5000/apps/broken:v1"])
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(obj, self.client, SETTINGS)
        self.assertIn("apps/broken:v1", str(ctx.exception))

    def test_one_bad_image_denies_even_when_other_is_good(self):
        obj = deployment(
            [
                "localhost:5000/apps/demo:v1",
                "localhost:5000/apps/broken:v1",
            ]
        )
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(obj, self.client, SETTINGS)
        self.assertIn("apps/broken:v1", str(ctx.exception))

    def test_non_local_registry_is_denied(self):
        obj = deployment(["docker.io/library/nginx:1.27"])
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(obj, self.client, SETTINGS)
        self.assertIn("docker.io", str(ctx.exception))

    def test_tag_repoint_is_observed_on_next_admission(self):
        # First admission resolves v1 -> digest_v1.
        first = build_patches(
            deployment(["localhost:5000/apps/demo:v1"]), self.client, SETTINGS
        )
        self.assertEqual(first[0]["value"],
                         f"localhost:5000/apps/demo@{self.digest_v1}")

        # Registry re-points the tag at a different index (distinct content).
        digest_v2 = self.fake.add_multi_arch(
            "apps/demo", "v2", ["amd64", "arm64"], variant="v2"
        )
        self.fake.retag("apps/demo", "v1", "v2")

        # A new admission (fresh per-request cache) must re-query.
        second = build_patches(
            deployment(["localhost:5000/apps/demo:v1"]), self.client, SETTINGS
        )
        self.assertEqual(second[0]["value"],
                         f"localhost:5000/apps/demo@{digest_v2}")
        self.assertNotEqual(first[0]["value"], second[0]["value"])

    def test_missing_pod_template_is_denied(self):
        with self.assertRaises(AdmissionFailure):
            build_patches({"spec": {}}, self.client, SETTINGS)


class AdmissionReviewEnvelopeTests(unittest.TestCase):
    """Exercise the FastAPI handler without third-party packages.

    The handler function is plain Python; pydantic/fastapi are only needed
    to import app.main, so replicate its envelope logic expectations here
    using build_patches directly and validate JSONPatch encodability.
    """

    def test_patch_payload_is_jsonpatch_encodable(self):
        fake = FakeRegistry()
        digest = fake.add_multi_arch("apps/demo", "v1", ["amd64", "arm64"])
        client = RegistryClient(SETTINGS, fetcher=fake.fetcher)
        patches = build_patches(
            deployment(["localhost:5000/apps/demo:v1"]), client, SETTINGS
        )
        encoded = base64.b64encode(
            json.dumps(patches, separators=(",", ":")).encode()
        ).decode()
        decoded = json.loads(base64.b64decode(encoded))
        self.assertEqual(
            decoded[0]["value"], f"localhost:5000/apps/demo@{digest}"
        )


if __name__ == "__main__":
    unittest.main()
