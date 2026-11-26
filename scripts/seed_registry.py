#!/usr/bin/env python3
"""Seed the local Docker registry with OCI multi-arch test fixtures.

Pure standard library: talks the Distribution v2 API over HTTP with
urllib.  Run after `docker compose up`::

    python3 scripts/seed_registry.py seed
    python3 scripts/seed_registry.py retag-demo   # move demo:v1 to a new index
    python3 scripts/seed_registry.py reset-demo   # move demo:v1 back

Fixtures (all under localhost:5000):

* apps/demo:v1 / apps/demo:v2 - full indexes with linux/amd64 + linux/arm64
  children (v1 and v2 contain different manifests so the index digests
  differ; used to demonstrate tag re-pointing).
* apps/arm-only:v1 - index containing only the arm64 child; a default
  admission (both platforms) is denied for the missing amd64 child.
* apps/broken:v1 - index listing both platforms, but the amd64 child
  manifest is deleted afterwards, so fetching it by digest returns 404.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:5000").rstrip("/")

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCTET_STREAM = "application/octet-stream"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request(method: str, url: str, data: bytes | None = None,
            headers: dict[str, str] | None = None) -> "urllib.response.addinfourl":
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"{method} {url} -> HTTP {exc.code}: {body}"
        ) from exc


def canonical(payload: dict) -> bytes:
    # Stable encoding; the registry hashes the exact received bytes.
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


class RepoPusher:
    def __init__(self, name: str):
        self.name = name
        self._blob_cache: dict[str, bytes] = {}

    def upload_blob(self, data: bytes) -> dict:
        digest = digest_of(data)
        if digest in self._blob_cache:
            return {"mediaType": OCTET_STREAM, "digest": digest, "size": len(data)}
        resp = request("POST", f"{REGISTRY_URL}/v2/{self.name}/blobs/uploads/")
        location = resp.headers["Location"]
        upload_url = urllib.parse.urljoin(REGISTRY_URL, location)
        sep = "&" if "?" in upload_url else "?"
        put_url = f"{upload_url}{sep}digest={digest}"
        request("PUT", put_url, data=data,
                headers={"Content-Type": OCTET_STREAM})
        self._blob_cache[digest] = data
        return {"mediaType": OCTET_STREAM, "digest": digest, "size": len(data)}

    def put_manifest(self, reference: str, media_type: str,
                     payload: dict) -> str:
        body = canonical(payload)
        digest = digest_of(body)
        request(
            "PUT",
            f"{REGISTRY_URL}/v2/{self.name}/manifests/{reference}",
            data=body,
            headers={"Content-Type": media_type},
        )
        return digest

    def delete_manifest(self, digest: str) -> None:
        request("DELETE",
                f"{REGISTRY_URL}/v2/{self.name}/manifests/{digest}")


def empty_layer_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    return buf.getvalue()


def build_child(pusher: RepoPusher, arch: str, layer: bytes) -> tuple[dict, str]:
    config = canonical({
        "architecture": arch,
        "os": "linux",
        "config": {},
        "rootfs": {"type": "layers", "diff_ids": [digest_of(layer)]},
    })
    config_desc = pusher.upload_blob(config)
    config_desc["mediaType"] = OCI_CONFIG
    layer_desc = pusher.upload_blob(layer)
    layer_desc["mediaType"] = OCI_LAYER
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": config_desc,
        "layers": [layer_desc],
    }
    digest = pusher.put_manifest(
        manifest_digest_tag(manifest), OCI_MANIFEST, manifest
    )
    descriptor = {
        "mediaType": OCI_MANIFEST,
        "digest": digest,
        "size": len(canonical(manifest)),
        "platform": {"os": "linux", "architecture": arch},
    }
    return descriptor, digest


def manifest_digest_tag(manifest: dict) -> str:
    # Children are pushed by digest first (required so the index can
    # reference them); digest is recomputed identically on the second call.
    return digest_of(canonical(manifest))


def push_index(repo: str, architectures: list[str], layer: bytes,
               delete_arch: str | None = None) -> tuple[str, dict[str, str]]:
    pusher = RepoPusher(repo)
    descriptors = []
    child_digests: dict[str, str] = {}
    for arch in architectures:
        descriptor, child_digest = build_child(pusher, arch, layer)
        descriptors.append(descriptor)
        child_digests[arch] = child_digest

    index = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": descriptors,
    }
    index_digest = pusher.put_manifest(
        digest_of(canonical(index)), OCI_INDEX, index
    )
    if delete_arch is not None:
        # Simulate a child manifest that the index promises but which can no
        # longer be fetched by digest (registry delete is enabled).
        pusher.delete_manifest(child_digests[delete_arch])
    return index_digest, child_digests


def tag_manifest(repo: str, tag: str, index_digest: str) -> None:
    resp = request("GET",
                   f"{REGISTRY_URL}/v2/{repo}/manifests/{index_digest}",
                   headers={"Accept": OCI_INDEX})
    payload = json.loads(resp.read())
    request(
        "PUT",
        f"{REGISTRY_URL}/v2/{repo}/manifests/{tag}",
        data=canonical(payload),
        headers={"Content-Type": OCI_INDEX},
    )


def cmd_seed(_args: argparse.Namespace) -> None:
    layer = empty_layer_bytes()
    summary = []

    # Two distinct indexes for apps/demo so re-pointing a tag changes digest.
    digest_v1, _ = push_index("apps/demo", ["amd64", "arm64"], layer)
    # A different layer blob makes v2's children (and thus the index) differ.
    layer_v2 = layer + b"\x00"
    digest_v2, _ = push_index("apps/demo", ["amd64", "arm64"], layer_v2)
    tag_manifest("apps/demo", "v1", digest_v1)
    tag_manifest("apps/demo", "v2", digest_v2)
    summary.append(("apps/demo:v1", digest_v1))
    summary.append(("apps/demo:v2", digest_v2))

    digest_arm, _ = push_index("apps/arm-only", ["arm64"], layer)
    tag_manifest("apps/arm-only", "v1", digest_arm)
    summary.append(("apps/arm-only:v1", digest_arm))

    digest_broken, _ = push_index(
        "apps/broken", ["amd64", "arm64"], layer, delete_arch="amd64"
    )
    tag_manifest("apps/broken", "v1", digest_broken)
    summary.append(("apps/broken:v1", digest_broken))

    print("seeded fixtures at", REGISTRY_URL)
    for ref, digest in summary:
        print(f"  {ref:24s} -> {digest}")


def cmd_retag(_args: argparse.Namespace) -> None:
    # Point demo:v1 at the v2 index (simulates a tag re-point).
    resp = request("GET",
                   f"{REGISTRY_URL}/v2/apps/demo/manifests/v2",
                   headers={"Accept": OCI_INDEX})
    digest_v2 = resp.headers["Docker-Content-Digest"]
    tag_manifest("apps/demo", "v1", digest_v2)
    print(f"apps/demo:v1 re-pointed to {digest_v2}")


def cmd_reset(args: argparse.Namespace) -> None:
    # Re-seeding is idempotent (identical bytes keep identical digests) and
    # restores the demo:v1 tag to its original multi-arch index.
    cmd_seed(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    sub.add_parser("retag-demo")
    sub.add_parser("reset-demo")
    args = parser.parse_args()

    handlers = {
        "seed": cmd_seed,
        "retag-demo": cmd_retag,
        "reset-demo": cmd_reset,
    }
    try:
        handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001 - CLI surface
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
