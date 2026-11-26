"""Offline unit tests for image reference parsing (stdlib unittest)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.images import ImageReferenceError, parse_image_reference


class ParseImageReferenceTests(unittest.TestCase):
    def test_local_registry_with_tag(self):
        ref = parse_image_reference("localhost:5000/apps/demo:v1")
        self.assertEqual(ref.registry, "localhost:5000")
        self.assertEqual(ref.repository, "apps/demo")
        self.assertEqual(ref.tag, "v1")
        self.assertIsNone(ref.digest)
        self.assertFalse(ref.is_pinned)

    def test_default_tag_is_latest(self):
        ref = parse_image_reference("localhost:5000/apps/demo")
        self.assertEqual(ref.tag, "latest")

    def test_nested_repository(self):
        ref = parse_image_reference("registry:5000/a/b/c:1.2.3")
        self.assertEqual((ref.registry, ref.repository, ref.tag),
                         ("registry:5000", "a/b/c", "1.2.3"))

    def test_digest_pinned(self):
        digest = "sha256:" + "a" * 64
        ref = parse_image_reference(f"localhost:5000/apps/demo@{digest}")
        self.assertTrue(ref.is_pinned)
        self.assertIsNone(ref.tag)
        self.assertEqual(ref.digest, digest)
        self.assertEqual(str(ref), f"localhost:5000/apps/demo@{digest}")

    def test_with_digest_renders_same_host(self):
        ref = parse_image_reference("localhost:5000/apps/demo:v1")
        digest = "sha256:" + "b" * 64
        self.assertEqual(
            ref.with_digest(digest),
            f"localhost:5000/apps/demo@{digest}",
        )

    def test_docker_hub_host_is_kept(self):
        ref = parse_image_reference("docker.io/library/nginx:1.27")
        self.assertEqual(ref.registry, "docker.io")
        self.assertEqual(ref.repository, "library/nginx")

    def test_unsupported_digest_algorithm_rejected(self):
        with self.assertRaises(ImageReferenceError):
            parse_image_reference("localhost:5000/apps/demo@sha512:abc")

    def test_empty_and_garbage_rejected(self):
        for raw in ("", "   ", "localhost:5000/", "localhost:5000/apps/:v1"):
            with self.assertRaises(ImageReferenceError, msg=raw):
                parse_image_reference(raw)

    def test_tag_with_illegal_character_rejected(self):
        with self.assertRaises(ImageReferenceError):
            parse_image_reference("localhost:5000/apps/demo:v 1")


if __name__ == "__main__":
    unittest.main()
