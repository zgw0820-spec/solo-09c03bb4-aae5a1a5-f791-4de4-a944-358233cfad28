"""In-memory RegistryGateway fake for the merge test suite.

Mirrors the distribution registry semantics the merge feature relies on:
manifests and blobs keyed by digest, digest verification of stored bytes,
404s for missing objects and an optional hook to simulate a report landing
while ``put_index`` is in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.admission.registry import ManifestNotFound
from app.merger.registry_gateway import (
    OCI_CONFIG_MEDIA_TYPE,
    OCI_INDEX_MEDIA_TYPE,
    canonical_json,
    digest_of,
)
from app.merger.models import PlatformId

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


@dataclass
class FakeGateway:
    # repository -> digest -> (object dict, raw bytes)
    manifests: dict[str, dict[str, tuple[dict, bytes]]] = field(
        default_factory=dict
    )
    blobs: dict[str, dict[str, tuple[dict, bytes]]] = field(default_factory=dict)
    # repository -> list of pushed index dicts
    pushed_indexes: dict[str, list[dict]] = field(default_factory=dict)
    # Optional hook invoked at the start of every put_index (test injection).
    before_put_index: Optional[Callable[[str], None]] = None
    # When set, put_index raises instead of completing (registry failure).
    fail_put: bool = False

    def _repo_bucket(self, bucket: dict, repository: str) -> dict:
        return bucket.setdefault(repository, {})

    def add_child(
        self, repository: str, platform: PlatformId, *,
        digest: str | None = None, config_extra: dict | None = None,
    ) -> tuple[str, dict]:
        """Push a config blob + child manifest for *platform*.

        ``config_extra`` adds fields to the config blob so repeated builds
        of the same platform get genuinely different content and digest.
        """
        config = {
            "os": platform.os,
            "architecture": platform.architecture,
            "config": {},
            "rootfs": {"type": "layers", "diff_ids": []},
        }
        if config_extra:
            config.update(config_extra)
        config_raw = canonical_json(config)
        config_digest = digest_of(config_raw)
        self._repo_bucket(self.blobs, repository)[config_digest] = (
            config,
            config_raw,
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
        actual_digest = digest or digest_of(raw)
        self._repo_bucket(self.manifests, repository)[actual_digest] = (
            manifest,
            raw,
        )
        return actual_digest, manifest

    def add_raw_manifest(
        self, repository: str, digest: str, manifest: dict, raw: bytes
    ) -> None:
        self._repo_bucket(self.manifests, repository)[digest] = (manifest, raw)

    def add_raw_blob(self, repository: str, digest: str, blob: dict) -> None:
        raw = canonical_json(blob)
        self._repo_bucket(self.blobs, repository)[digest] = (blob, raw)

    def fetch_manifest(
        self, repository: str, digest: str
    ) -> tuple[dict[str, Any], bytes, str]:
        bucket = self.manifests.get(repository, {})
        if digest not in bucket:
            raise ManifestNotFound(f"清单不存在: {repository}:{digest}")
        manifest, raw = bucket[digest]
        if digest_of(raw) != digest:
            raise AssertionError("fake stored bytes disagree with digest")
        return manifest, raw, manifest.get("mediaType", "")

    def fetch_blob(
        self, repository: str, digest: str
    ) -> tuple[dict[str, Any], bytes]:
        bucket = self.blobs.get(repository, {})
        if digest not in bucket:
            raise ManifestNotFound(f"blob 不存在: {repository}:{digest}")
        blob, raw = bucket[digest]
        return blob, raw

    def put_index(self, repository: str, index: dict[str, Any]) -> str:
        if self.before_put_index is not None:
            self.before_put_index(repository)
        if self.fail_put:
            from app.merger.registry_gateway import GatewayError

            raise GatewayError("模拟仓库写入失败")
        raw = canonical_json(index)
        digest = digest_of(raw)
        self.pushed_indexes.setdefault(repository, []).append(index)
        # Also keep the index retrievable as a manifest, like a real registry.
        self._repo_bucket(self.manifests, repository)[digest] = (index, raw)
        return digest

    def last_index(self, repository: str) -> dict:
        return self.pushed_indexes[repository][-1]
