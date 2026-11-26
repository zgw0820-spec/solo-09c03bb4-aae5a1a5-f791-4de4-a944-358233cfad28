"""Shared in-memory OCI registry fake for the offline test suite."""

from __future__ import annotations

import hashlib
import json
import sys
import os
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.admission.images import ImageReference  # noqa: E402
from app.admission.registry import ManifestNotFound  # noqa: E402

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


@dataclass
class FakeRegistry:
    """Maps (repository, reference) -> manifest dict.

    Tag entries and digest entries are stored separately so tag re-pointing
    does not rewrite digest history.  ``missing_digests`` simulates child
    manifests that the index promises but the registry can no longer serve.
    """

    tags: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    missing_digests: set[tuple[str, str]] = field(default_factory=set)
    call_log: list[tuple[str, str]] = field(default_factory=list)

    def add_multi_arch(
        self,
        repository: str,
        tag: str,
        architectures: list[str],
        *,
        omit_child: str | None = None,
        variant: str = "",
    ) -> str:
        descriptors = []
        for arch in architectures:
            # Distinct per-arch config content so the child manifests (and
            # therefore their digests) never collide between architectures.
            config_blob = canonical(
                {
                    "architecture": arch,
                    "os": "linux",
                    "config": {"variant": variant},
                }
            )
            child = {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": digest_of(config_blob),
                    "size": len(config_blob),
                },
                "layers": [],
            }
            child_digest = digest_of(canonical(child))
            if omit_child != arch:
                self.tags[(repository, child_digest)] = child
            else:
                self.missing_digests.add((repository, child_digest))
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
        self.tags[(repository, index_digest)] = index
        self.tags[(repository, tag)] = index
        return index_digest

    def retag(self, repository: str, old_tag: str, new_tag: str) -> None:
        self.tags[(repository, old_tag)] = self.tags[(repository, new_tag)]

    def fetcher(self, ref: ImageReference, reference: str):
        key = (ref.repository, reference)
        self.call_log.append(key)
        if key in self.missing_digests or key not in self.tags:
            raise ManifestNotFound(f"清单不存在: {ref.repository}:{reference}")
        manifest = self.tags[key]
        return manifest, digest_of(canonical(manifest))
