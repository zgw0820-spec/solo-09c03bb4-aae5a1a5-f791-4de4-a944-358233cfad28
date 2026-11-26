#!/usr/bin/env python3
"""Simulate one platform's CI job for the batch merge (合版) demo.

The script plays the whole per-platform pipeline without a container
runtime:

1. build a tiny OCI image (config blob + empty tar layer + child manifest)
   for one platform and push it to the shared local registry;
2. POST the child manifest digest as a *report* (回报) to the merge API.

The child manifest's config blob records ``os`` / ``architecture``, which
the merge service re-fetches to verify that the report's platform claim is
honest.

Usage:
    python3 scripts/ci_build_report.py --repository apps/merged \\
        --batch b-2026-1 --platform linux/amd64 --attempt 1
    python3 scripts/ci_build_report.py ... --platform linux/arm64 --attempt 1
    # retry with a higher attempt number (produces a different digest):
    python3 scripts/ci_build_report.py ... --platform linux/amd64 --attempt 2

Environment:
    REGISTRY_URL  registry used for pushing (default http://localhost:5000)
    MERGE_URL     merge API base    (default http://localhost:8080)
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
MERGE_URL = os.getenv("MERGE_URL", "http://localhost:8080").rstrip("/")

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCTET_STREAM = "application/octet-stream"
SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def http(method: str, url: str, data: bytes | None = None,
         headers: dict[str, str] | None = None):
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def upload_blob(repository: str, data: bytes, media_type: str) -> dict:
    digest = digest_of(data)
    status, headers, _ = http(
        "POST", f"{REGISTRY_URL}/v2/{repository}/blobs/uploads/"
    )
    if status not in (200, 202):
        raise RuntimeError(f"初始化 blob 上传失败 HTTP {status}")
    location = urllib.parse.urljoin(REGISTRY_URL, headers["Location"])
    sep = "&" if "?" in location else "?"
    status, _, _ = http(
        "PUT", f"{location}{sep}digest={digest}", data=data,
        headers={"Content-Type": media_type},
    )
    if status not in (200, 201, 204):
        raise RuntimeError(f"上传 blob {digest} 失败 HTTP {status}")
    return {"mediaType": media_type, "digest": digest, "size": len(data)}


def empty_layer(platform: str, attempt: int) -> bytes:
    # Include platform/attempt metadata so each retry gets distinct layer
    # and config content (and therefore distinct digests).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="build-info.json")
        payload = json.dumps(
            {"platform": platform, "attempt": attempt}
        ).encode()
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def build_and_push(repository: str, platform: str, attempt: int) -> str:
    os_name, _, arch = platform.partition("/")
    layer = empty_layer(platform, attempt)
    layer_desc = upload_blob(repository, layer, OCI_LAYER)

    config = canonical({
        "os": os_name,
        "architecture": arch,
        "config": {"platform": platform, "attempt": attempt},
        "rootfs": {"type": "layers", "diff_ids": [digest_of(layer)]},
    })
    config_desc = upload_blob(repository, config, OCI_CONFIG)

    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": config_desc,
        "layers": [layer_desc],
    }
    raw = canonical(manifest)
    digest = digest_of(raw)
    status, _, body = http(
        "PUT", f"{REGISTRY_URL}/v2/{repository}/manifests/{digest}",
        data=raw, headers={"Content-Type": OCI_MANIFEST},
    )
    if status not in (200, 201):
        raise RuntimeError(f"推送子清单失败 HTTP {status}: {body!r}")
    return digest


def submit_report(repository: str, batch: str, platform: str,
                  attempt: int, digest: str) -> int:
    payload = json.dumps({
        "repository": repository,
        "batch": batch,
        "platform": platform,
        "attempt": attempt,
        "digest": digest,
    }).encode()
    status, _, body = http(
        "POST", f"{MERGE_URL}/api/v1/reports", data=payload,
        headers={"Content-Type": "application/json"},
    )
    print(f"POST /api/v1/reports -> HTTP {status}")
    print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="仓库路径, 如 apps/merged")
    parser.add_argument("--batch", required=True, help="构建批次标识")
    parser.add_argument("--platform", required=True, choices=SUPPORTED_PLATFORMS)
    parser.add_argument("--attempt", type=int, required=True, help="尝试号 >= 1")
    args = parser.parse_args()
    if args.attempt < 1:
        parser.error("--attempt 必须 >= 1")

    print(f"[CI] 平台 {args.platform} 尝试号 {args.attempt} 开始构建")
    digest = build_and_push(args.repository, args.platform, args.attempt)
    print(f"[CI] 子清单已推送: {digest}")
    status = submit_report(
        args.repository, args.batch, args.platform, args.attempt, digest
    )
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
