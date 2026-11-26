"""End-to-end smoke test over real HTTP (stdlib only).

Spins up a local HTTP server implementing the tiny subset of the
Distribution v2 manifest API that HttpxManifestFetcher uses, then runs
the full policy through it.  This validates URL construction, headers and
Docker-Content-Digest handling without Docker or third-party packages.
"""

import hashlib
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.config import Settings  # noqa: E402
from app.admission.images import parse_image_reference  # noqa: E402
from app.admission.policy import AdmissionFailure, build_patches  # noqa: E402
from app.admission.registry import (  # noqa: E402
    RegistryClient,
    RegistryError,
)

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def make_index(architectures, *, missing_child=None):
    missing_child = missing_child or set()
    descriptors = []
    children = {}
    for arch in architectures:
        config = canonical({"architecture": arch, "os": "linux"})
        child = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": digest_of(config),
                "size": len(config),
            },
            "layers": [],
        }
        child_digest = digest_of(canonical(child))
        if arch not in missing_child:
            children[child_digest] = child
        descriptors.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": child_digest,
                "size": len(canonical(child)),
                "platform": {"os": "linux", "architecture": arch},
            }
        )
    index = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": descriptors,
    }
    index_digest = digest_of(canonical(index))
    children[index_digest] = index
    return index, index_digest, children


class _RegistryState:
    def __init__(self):
        # reference -> (manifest dict, digest)
        self.content: dict[tuple[str, str], tuple[dict, str]] = {}
        self.tags: dict[tuple[str, str], str] = {}

    def put_tag(self, repo, tag, index, index_digest, children):
        self.tags[(repo, tag)] = index_digest
        for digest, manifest in children.items():
            self.content[(repo, digest)] = (manifest, digest)


STATE = _RegistryState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test server
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        # /v2/<name>/manifests/<reference>
        if len(parts) >= 4 and parts[0] == "v2" and parts[-2] == "manifests":
            repo = "/".join(parts[1:-2])
            reference = parts[-1]
            if (repo, reference) in STATE.tags:
                digest = STATE.tags[(repo, reference)]
                manifest, _ = STATE.content[(repo, digest)]
            elif (repo, reference) in STATE.content:
                manifest, digest = STATE.content[(repo, reference)]
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = canonical(manifest)
            self.send_response(200)
            self.send_header("Content-Type", manifest["mediaType"])
            self.send_header("Docker-Content-Digest", digest)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class UrllibFetcher:
    """Same contract as HttpxManifestFetcher, implemented with urllib."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def __call__(self, ref, reference):
        import urllib.error
        import urllib.request

        url = f"{self.base_url}/v2/{ref.repository_url_path}/manifests/{reference}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                digest = resp.headers["Docker-Content-Digest"]
                manifest = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RegistryError(
                    f"清单不存在: {ref.repository}:{reference}"
                ) from exc
            raise RegistryError(f"HTTP {exc.code}") from exc
        return manifest, digest


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.settings = Settings(
            allowed_registries=("localhost:5000",),
            registry_upstream=cls.base_url,
            default_platforms=("linux/amd64", "linux/arm64"),
            request_timeout=5.0,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _client(self):
        return RegistryClient(self.settings,
                              fetcher=UrllibFetcher(self.base_url))

    def _deployment(self, image, node_selector=None):
        spec = {"containers": [{"name": "app", "image": image}]}
        if node_selector:
            spec["nodeSelector"] = node_selector
        return {
            "spec": {
                "template": {
                    "spec": spec,
                }
            }
        }

    def test_full_flow_tag_pinned_over_http(self):
        index, index_digest, children = make_index(["amd64", "arm64"])
        STATE.put_tag("apps/demo", "v1", index, index_digest, children)

        patches = build_patches(
            self._deployment("localhost:5000/apps/demo:v1"),
            self._client(), self.settings,
        )
        self.assertEqual(len(patches), 1)
        self.assertEqual(
            patches[0]["value"],
            f"localhost:5000/apps/demo@{index_digest}",
        )

    def test_missing_platform_denied_over_http(self):
        index, index_digest, children = make_index(["arm64"])
        STATE.put_tag("apps/arm", "v1", index, index_digest, children)
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(
                self._deployment("localhost:5000/apps/arm:v1"),
                self._client(), self.settings,
            )
        self.assertIn("linux/amd64", str(ctx.exception))

    def test_missing_child_manifest_denied_over_http(self):
        index, index_digest, children = make_index(
            ["amd64", "arm64"], missing_child={"amd64"}
        )
        STATE.put_tag("apps/broken", "v1", index, index_digest, children)
        with self.assertRaises(AdmissionFailure) as ctx:
            build_patches(
                self._deployment("localhost:5000/apps/broken:v1"),
                self._client(), self.settings,
            )
        self.assertIn("apps/broken:v1", str(ctx.exception))

    def test_arch_selector_narrows_query_over_http(self):
        index, index_digest, children = make_index(["arm64"])
        STATE.put_tag("apps/arm", "v2", index, index_digest, children)
        patches = build_patches(
            self._deployment(
                "localhost:5000/apps/arm:v2",
                node_selector={"kubernetes.io/arch": "arm64"},
            ),
            self._client(), self.settings,
        )
        self.assertEqual(
            patches[0]["value"], f"localhost:5000/apps/arm@{index_digest}"
        )

    def test_tag_repoint_changes_digest_on_next_admission(self):
        index1, digest1, children1 = make_index(["amd64", "arm64"])
        index2, digest2, children2 = make_index(
            ["amd64", "arm64"], missing_child=set()
        )
        # Make index2 content genuinely different.
        index2["annotations"] = {"released": "v2"}
        digest2 = digest_of(canonical(index2))
        STATE.put_tag("apps/moving", "v1", index1, digest1, children1)

        client = self._client()
        first = build_patches(
            self._deployment("localhost:5000/apps/moving:v1"),
            client, self.settings,
        )
        self.assertIn(digest1, first[0]["value"])

        STATE.tags[("apps/moving", "v1")] = digest2
        STATE.content[("apps/moving", digest2)] = (index2, digest2)
        for child_digest, child in children2.items():
            STATE.content.setdefault(("apps/moving", child_digest),
                                     (child, child_digest))

        second = build_patches(
            self._deployment("localhost:5000/apps/moving:v1"),
            self._client(), self.settings,
        )
        self.assertIn(digest2, second[0]["value"])
        self.assertNotEqual(first[0]["value"], second[0]["value"])


if __name__ == "__main__":
    unittest.main()
