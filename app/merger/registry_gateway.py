"""Registry gateway for the merge feature.

The gateway is the only component that talks to the OCI Distribution v2
registry.  Three operations are needed:

* :meth:`RegistryGateway.fetch_manifest` - pull a child manifest by digest,
  returning the exact bytes the registry hashed;
* :meth:`RegistryGateway.fetch_blob` - pull a config blob so its
  ``os`` / ``architecture`` can be checked against the reported platform;
* :meth:`RegistryGateway.put_index` - push the merged OCI index and return
  its digest.

``httpx`` is imported lazily, exactly like the admission registry client,
so unit tests can inject a fake gateway with only the standard library.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from ..admission.config import Settings
from ..admission.registry import (
    ACCEPT_HEADER,
    MANIFEST_MEDIA_TYPES,
    ManifestNotFound,
)
from .models import PlatformId

OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCTET_STREAM = "application/octet-stream"


class GatewayError(Exception):
    """Registry failure while verifying a report or publishing an index."""


class ReportRejected(GatewayError):
    """A report references content that fails verification (HTTP 422).

    Covers missing manifests/blobs, wrong media types and config-blob
    platform mismatches: the report itself is invalid and must not be
    registered.  Pure connectivity / upstream failures stay plain
    :class:`GatewayError` and surface as HTTP 502.
    """


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(payload: dict[str, Any]) -> bytes:
    # Deterministic encoding; identical bytes must produce identical digests
    # across the demo scripts, the tests and the registry.
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class RegistryGateway(Protocol):
    def fetch_manifest(
        self, repository: str, digest: str
    ) -> tuple[dict[str, Any], bytes, str]: ...

    def fetch_blob(self, repository: str, digest: str) -> tuple[dict[str, Any], bytes]: ...

    def put_index(self, repository: str, index: dict[str, Any]) -> str: ...


class HttpxRegistryGateway:
    """Production gateway speaking plain HTTP to the local registry."""

    def __init__(self, settings: Settings):
        self._base_url = settings.registry_upstream.rstrip("/")
        self._timeout = settings.request_timeout

    def _get(self, path: str, accept: str) -> tuple[bytes, str, str]:
        import httpx  # lazy import: keeps the module importable without deps

        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers={"Accept": accept})
        except httpx.HTTPError as exc:
            raise GatewayError(f"访问仓库失败: {exc}") from exc

        if response.status_code == 404:
            raise ManifestNotFound(f"仓库对象不存在: {path}")
        if response.status_code != 200:
            raise GatewayError(f"仓库返回 HTTP {response.status_code}: {path}")

        digest = response.headers.get("Docker-Content-Digest", "")
        content_type = response.headers.get("Content-Type", "")
        if not digest.startswith("sha256:"):
            raise GatewayError("仓库响应缺少 Docker-Content-Digest 摘要头")
        return response.content, digest, content_type

    def fetch_manifest(
        self, repository: str, digest: str
    ) -> tuple[dict[str, Any], bytes, str]:
        body, returned_digest, _content_type = self._get(
            f"/v2/{repository}/manifests/{digest}", ACCEPT_HEADER
        )
        if returned_digest != digest:
            raise GatewayError(
                f"子清单摘要不一致: 请求 {digest}, 仓库返回 {returned_digest}"
            )
        if digest_of(body) != digest:
            raise GatewayError(f"子清单内容与摘要 {digest} 不匹配")
        try:
            manifest = json.loads(body)
        except ValueError as exc:
            raise GatewayError("仓库返回的子清单不是合法 JSON") from exc
        if not isinstance(manifest, dict):
            raise GatewayError("仓库返回的子清单结构非法")
        return manifest, body, content_type

    def fetch_blob(self, repository: str, digest: str) -> tuple[dict[str, Any], bytes]:
        body, returned_digest, _ = self._get(
            f"/v2/{repository}/blobs/{digest}",
            f"{OCI_CONFIG_MEDIA_TYPE}, {OCTET_STREAM}",
        )
        if returned_digest != digest:
            raise GatewayError(
                f"配置 blob 摘要不一致: 请求 {digest}, 仓库返回 {returned_digest}"
            )
        if digest_of(body) != digest:
            raise GatewayError(f"配置 blob 内容与摘要 {digest} 不匹配")
        try:
            blob = json.loads(body)
        except ValueError as exc:
            raise GatewayError("仓库返回的配置 blob 不是合法 JSON") from exc
        if not isinstance(blob, dict):
            raise GatewayError("仓库返回的配置 blob 结构非法")
        return blob, body

    def put_index(self, repository: str, index: dict[str, Any]) -> str:
        import httpx

        body = canonical_json(index)
        computed = digest_of(body)
        url = f"{self._base_url}/v2/{repository}/manifests/{computed}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.put(
                    url,
                    content=body,
                    headers={"Content-Type": OCI_INDEX_MEDIA_TYPE},
                )
        except httpx.HTTPError as exc:
            raise GatewayError(f"访问仓库失败: {exc}") from exc
        if response.status_code not in (200, 201):
            raise GatewayError(
                f"写入 OCI 索引失败, 仓库返回 HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        # The registry must hash the exact bytes we sent.
        returned = response.headers.get("Docker-Content-Digest", "")
        if returned.startswith("sha256:") and returned != computed:
            raise GatewayError(
                f"写入索引摘要不一致: 本地计算 {computed}, 仓库返回 {returned}"
            )
        return computed


def verify_report_platform(
    gateway: RegistryGateway,
    repository: str,
    platform: PlatformId,
    digest: str,
) -> tuple[dict[str, Any], bytes]:
    """Fetch the child manifest and its config blob and verify the platform.

    The platform reported by CI must match the ``os`` / ``architecture``
    recorded in the manifest's *config blob* (not merely the index
    descriptor, which the reporter controls).  Returns the child manifest
    and its exact registry bytes so the publisher can build the index
    descriptor without fetching it a second time.
    """
    try:
        manifest, raw_manifest, _ = gateway.fetch_manifest(repository, digest)
    except ManifestNotFound as exc:
        raise ReportRejected(
            f"平台 {platform} 的子清单 {digest} 在仓库 {repository} 中不存在"
        ) from exc

    media_type = manifest.get("mediaType")
    if media_type not in MANIFEST_MEDIA_TYPES:
        raise ReportRejected(
            f"平台 {platform} 的子清单类型不受支持: {media_type!r}"
        )

    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ReportRejected(f"子清单 {digest} 缺少 config 描述符")
    config_digest = config.get("digest", "")
    if not config_digest.startswith("sha256:"):
        raise ReportRejected(f"子清单 {digest} 的 config 描述符缺少合法摘要")

    try:
        config_blob, _ = gateway.fetch_blob(repository, config_digest)
    except ManifestNotFound as exc:
        raise ReportRejected(
            f"子清单 {digest} 引用的配置 blob {config_digest} 不存在"
        ) from exc
    blob_os = config_blob.get("os")
    blob_arch = config_blob.get("architecture")
    if blob_os != platform.os or blob_arch != platform.architecture:
        raise ReportRejected(
            f"平台核对失败: 回报声明 {platform}, 但子清单 {digest} 的配置 blob "
            f"记录 os={blob_os!r} architecture={blob_arch!r}"
        )
    return manifest, raw_manifest


def build_index_descriptor(
    manifest: dict[str, Any], digest: str, raw_manifest_size: int,
    platform: PlatformId,
) -> dict[str, Any]:
    """Build the OCI index descriptor for a registry-verified child."""
    return {
        "mediaType": manifest.get("mediaType"),
        "digest": digest,
        "size": raw_manifest_size,
        "platform": {"os": platform.os, "architecture": platform.architecture},
    }
