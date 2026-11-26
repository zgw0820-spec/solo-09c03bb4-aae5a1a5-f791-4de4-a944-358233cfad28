"""End-to-end merge tests over real HTTP (stdlib only, no Docker).

Spins up two in-process HTTP servers:

* a tiny Distribution v2 registry supporting blob uploads, manifest
  GET/PUT and the ``Docker-Content-Digest`` header;
* the merge service itself is exercised directly (FastAPI is not needed
  for the registry contract) through an urllib-based gateway that matches
  :class:`~app.merger.registry_gateway.HttpxRegistryGateway`.

This validates URL construction, digest headers, config-blob platform
verification and the "report changes while the index is being written"
race over a real socket.
"""

import hashlib
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.config import Settings  # noqa: E402
from app.merger.models import PlatformId, Report  # noqa: E402
from app.merger.registry_gateway import (  # noqa: E402
    OCI_CONFIG_MEDIA_TYPE,
    OCI_INDEX_MEDIA_TYPE,
    ReportRejected,
    canonical_json,
    digest_of,
)
from app.merger.service import MergeService  # noqa: E402
from app.merger.store import MergeStore  # noqa: E402

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCTET = "application/octet-stream"


def _http(method, url, data=None, headers=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class UrllibRegistryGateway:
    """urllib twin of HttpxRegistryGateway (same method contract)."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _get(self, path, accept):
        status, headers, body = _http(
            "GET", f"{self.base_url}{path}",
            headers={"Accept": accept},
        )
        if status == 404:
            from app.admission.registry import ManifestNotFound

            raise ManifestNotFound(f"仓库对象不存在: {path}")
        if status != 200:
            from app.merger.registry_gateway import GatewayError

            raise GatewayError(f"HTTP {status}: {path}")
        digest = headers.get("Docker-Content-Digest", "")
        if not digest.startswith("sha256:"):
            from app.merger.registry_gateway import GatewayError

            raise GatewayError("缺少摘要头")
        return body, digest

    def fetch_manifest(self, repository, digest):
        from app.admission.registry import ACCEPT_HEADER

        body, returned = self._get(
            f"/v2/{repository}/manifests/{digest}", ACCEPT_HEADER
        )
        if returned != digest or digest_of(body) != digest:
            from app.merger.registry_gateway import GatewayError

            raise GatewayError("子清单摘要不一致")
        return json.loads(body), body, OCI_MANIFEST

    def fetch_blob(self, repository, digest):
        body, returned = self._get(
            f"/v2/{repository}/blobs/{digest}",
            f"{OCI_CONFIG_MEDIA_TYPE}, {OCTET}",
        )
        if returned != digest or digest_of(body) != digest:
            from app.merger.registry_gateway import GatewayError

            raise GatewayError("配置 blob 摘要不一致")
        return json.loads(body), body

    def put_index(self, repository, index):
        body = canonical_json(index)
        computed = digest_of(body)
        status, headers, _ = _http(
            "PUT",
            f"{self.base_url}/v2/{repository}/manifests/{computed}",
            data=body,
            headers={"Content-Type": OCI_INDEX_MEDIA_TYPE},
        )
        if status not in (200, 201):
            from app.merger.registry_gateway import GatewayError

            raise GatewayError(f"写入索引失败 HTTP {status}")
        returned = headers.get("Docker-Content-Digest", "")
        if returned.startswith("sha256:") and returned != computed:
            from app.merger.registry_gateway import GatewayError

            raise GatewayError("写入索引摘要不一致")
        return computed


class _RegistryData:
    def __init__(self):
        # repository -> digest -> (media_type, bytes)
        self.manifests: dict[str, dict[str, tuple[str, bytes]]] = {}
        self.blobs: dict[str, dict[str, bytes]] = {}


DATA = _RegistryData()
# Optional hook fired on every manifest PUT (for the write-window race).
PUT_HOOK = {"fn": None}


class RegistryHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, headers=(), body=b""):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        # /v2/<name>/manifests/<digest> or /v2/<name>/blobs/<digest>
        if len(parts) >= 4 and parts[0] == "v2" and parts[1] != "":
            kind = parts[-2]
            digest = parts[-1]
            repo = "/".join(parts[1:-2])
            if kind == "blobs":
                blob = DATA.blobs.get(repo, {}).get(digest)
                if blob is None:
                    return self._send(404)
                return self._send(
                    200,
                    [("Content-Type", OCTET),
                     ("Docker-Content-Digest", digest)],
                    blob,
                )
            if kind == "manifests":
                entry = DATA.manifests.get(repo, {}).get(digest)
                if entry is None:
                    return self._send(404)
                media_type, raw = entry
                return self._send(
                    200,
                    [("Content-Type", media_type),
                     ("Docker-Content-Digest", digest)],
                    raw,
                )
        self._send(404)

    def do_PUT(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if len(parts) >= 4 and parts[-2] == "manifests":
            repo = "/".join(parts[1:-2])
            reference = parts[-1]
            media_type = self.headers.get("Content-Type", OCTET)
            digest = digest_of(body)
            if reference != digest:
                return self._send(400, body=b"reference must be a digest")
            DATA.manifests.setdefault(repo, {})[digest] = (media_type, body)
            # Only index publications represent the merge write window.
            if media_type == OCI_INDEX_MEDIA_TYPE and PUT_HOOK["fn"]:
                PUT_HOOK["fn"](repo)
            return self._send(
                201, [("Docker-Content-Digest", digest),
                      ("Location", f"/v2/{repo}/manifests/{digest}")]
            )
        # Monolithic blob upload: /v2/<name>/blobs/uploads/ (+ ?digest=)
        if "blobs" in parts and "uploads" in parts:
            query = urlparse(self.path).query
            digest = dict(
                pair.split("=", 1) for pair in query.split("&") if "=" in pair
            ).get("digest", "")
            if not digest.startswith("sha256:") or digest_of(body) != digest:
                return self._send(400, body=b"bad blob digest")
            repo = "/".join(parts[1:parts.index("blobs")])
            DATA.blobs.setdefault(repo, {})[digest] = body
            return self._send(201, [("Docker-Content-Digest", digest)])
        self._send(404)

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if "uploads" in parts:
            repo = "/".join(parts[1:parts.index("uploads")])
            # The demo uploader then PUTs to this location with ?digest=.
            location = f"/v2/{repo}/blobs/uploads/"
            return self._send(202, [("Location", location)])
        self._send(404)


def push_child_via_http(base_url, repository, platform, *, build=""):
    """Mimic a CI job: upload config blob + child manifest over HTTP."""
    config = {
        "os": platform.os,
        "architecture": platform.architecture,
        "config": {"build": build},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    config_raw = canonical_json(config)
    config_digest = digest_of(config_raw)
    _http("POST", f"{base_url}/v2/{repository}/blobs/uploads/")
    _http(
        "PUT",
        f"{base_url}/v2/{repository}/blobs/uploads/?digest={config_digest}",
        data=config_raw,
        headers={"Content-Type": OCI_CONFIG_MEDIA_TYPE},
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_raw),
        },
        "layers": [],
    }
    raw = canonical_json(manifest)
    digest = digest_of(raw)
    status, _, _ = _http(
        "PUT", f"{base_url}/v2/{repository}/manifests/{digest}",
        data=raw, headers={"Content-Type": OCI_MANIFEST},
    )
    assert status in (200, 201)
    return digest


class MergeHttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A threaded server so a PUT hook that itself performs HTTP
        # requests does not deadlock (a real registry is concurrent too).
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), RegistryHandler
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.settings = Settings(
            allowed_registries=("localhost:5000",),
            registry_upstream=cls.base_url,
            public_registry_host="localhost:5000",
            default_platforms=("linux/amd64", "linux/arm64"),
            request_timeout=5.0,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        DATA.manifests.clear()
        DATA.blobs.clear()
        PUT_HOOK["fn"] = None
        self.gateway = UrllibRegistryGateway(self.base_url)
        self.service = MergeService(
            MergeStore(), self.gateway, self.settings
        )
        self.repo = "apps/http-demo"
        self.batch = "ci-77"

    def _report(self, platform, attempt, digest):
        return Report(
            repository=self.repo, batch=self.batch,
            platform=platform, attempt=attempt, digest=digest,
        )

    def test_full_merge_over_real_http(self):
        amd = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/amd64"),
            build="1",
        )
        arm = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/arm64"),
            build="1",
        )
        self.assertEqual(
            self.service.register_report(
                self._report(PlatformId.parse("linux/amd64"), 1, amd)
            )[0],
            "accepted",
        )
        self.assertEqual(
            self.service.register_report(
                self._report(PlatformId.parse("linux/arm64"), 1, arm)
            )[0],
            "accepted",
        )
        result = self.service.publish(self.repo, self.batch)
        self.assertEqual(result.status, "published")
        self.assertEqual(
            result.reference,
            f"localhost:5000/{self.repo}@{result.index_digest}",
        )
        # The merged index must now be fetchable from the registry itself.
        status, headers, body = _http(
            "GET",
            f"{self.base_url}/v2/{self.repo}/manifests/{result.index_digest}",
            headers={"Accept": OCI_INDEX_MEDIA_TYPE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Docker-Content-Digest"], result.index_digest
        )
        index = json.loads(body)
        self.assertEqual(
            {m["digest"] for m in index["manifests"]}, {amd, arm}
        )

    def test_platform_mismatch_rejected_over_http(self):
        # Push an arm64 child but report it as amd64.
        arm = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/arm64")
        )
        with self.assertRaises(ReportRejected) as ctx:
            self.service.register_report(
                self._report(PlatformId.parse("linux/amd64"), 1, arm)
            )
        self.assertIn("平台核对失败", str(ctx.exception))
        self.assertEqual(self.service.status(self.repo, self.batch).reports,
                         {})

    def test_higher_attempt_during_index_write_over_http(self):
        amd = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/amd64")
        )
        arm = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/arm64")
        )
        self.service.register_report(
            self._report(PlatformId.parse("linux/amd64"), 1, amd)
        )
        self.service.register_report(
            self._report(PlatformId.parse("linux/arm64"), 1, arm)
        )
        first = self.service.publish(self.repo, self.batch)
        self.assertEqual(first.status, "published")

        amd2 = push_child_via_http(
            self.base_url, self.repo, PlatformId.parse("linux/amd64"),
            build="retry",
        )

        def swap(_repo):
            self.service.register_report(
                self._report(PlatformId.parse("linux/amd64"), 2, amd2)
            )

        PUT_HOOK["fn"] = swap
        result = self.service.publish(self.repo, self.batch)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.last_good_reference, first.reference)

        # Retry against the latest reports publishes the new index.
        PUT_HOOK["fn"] = None
        retried = self.service.publish(self.repo, self.batch)
        self.assertEqual(retried.status, "published")
        index = DATA.manifests[self.repo][retried.index_digest][1]
        self.assertIn(
            amd2, {m["digest"] for m in json.loads(index)["manifests"]}
        )


if __name__ == "__main__":
    unittest.main()
