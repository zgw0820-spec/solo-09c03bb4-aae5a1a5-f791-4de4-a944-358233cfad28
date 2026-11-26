"""Minimal OCI Distribution API client used by the webhook.

Only the subset needed to resolve a tagged image index is implemented:

* ``GET /v2/<name>/manifests/<reference>`` (tag -> index, digest -> child)
* verification that every requested platform is present in one index and
  that the child manifest can be fetched by the digest recorded there

The client speaks plain HTTP because the bundled registry is a local,
unauthenticated Docker Compose service.  ``httpx`` is imported lazily so
that the policy modules remain importable (and unit-testable with a fake
manifest fetcher) without third-party packages installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import Settings
from .images import ImageReference

# Media types we accept as multi-platform indexes / manifest lists.
INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
# Media types we accept as per-platform child manifests.
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
ACCEPT_HEADER = ", ".join(sorted(INDEX_MEDIA_TYPES | MANIFEST_MEDIA_TYPES))


class RegistryError(Exception):
    """Base class for registry resolution failures (surfaced in denial)."""


class ManifestNotFound(RegistryError):
    pass


class NotAnIndex(RegistryError):
    pass


class PlatformMissing(RegistryError):
    pass


@dataclass(frozen=True)
class Platform:
    os: str
    architecture: str

    @classmethod
    def parse(cls, value: str) -> "Platform":
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise RegistryError(f"平台标识非法: {value}")
        return cls(os=parts[0], architecture=parts[1])

    def matches(self, manifest_descriptor: dict[str, Any]) -> bool:
        platform = manifest_descriptor.get("platform") or {}
        return (
            platform.get("os") == self.os
            and platform.get("architecture") == self.architecture
        )

    def __str__(self) -> str:
        return f"{self.os}/{self.architecture}"


class ManifestFetcher(Protocol):
    """Callable returning (manifest body, content digest) for a reference."""

    def __call__(
        self, ref: ImageReference, reference: str
    ) -> tuple[dict[str, Any], str]: ...


class HttpxManifestFetcher:
    """Production fetcher talking to the registry over HTTP(S)."""

    def __init__(self, settings: Settings):
        self._base_url = settings.registry_upstream.rstrip("/")
        self._timeout = settings.request_timeout

    def __call__(
        self, ref: ImageReference, reference: str
    ) -> tuple[dict[str, Any], str]:
        import httpx  # lazy import: keeps unit tests dependency-free

        url = f"{self._base_url}/v2/{ref.repository_url_path}/manifests/{reference}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers={"Accept": ACCEPT_HEADER})
        except httpx.HTTPError as exc:
            raise RegistryError(f"访问仓库失败: {exc}") from exc

        if response.status_code == 404:
            raise ManifestNotFound(f"清单不存在: {ref.repository}:{reference}")
        if response.status_code != 200:
            raise RegistryError(
                f"仓库返回 HTTP {response.status_code}: "
                f"{ref.repository}:{reference}"
            )

        digest = response.headers.get("Docker-Content-Digest", "")
        if not digest.startswith("sha256:"):
            raise RegistryError("仓库响应缺少 Docker-Content-Digest 摘要头")
        try:
            body = response.json()
        except ValueError as exc:
            raise RegistryError("仓库返回的清单不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise RegistryError("仓库返回的清单结构非法")
        return body, digest


class RegistryClient:
    def __init__(
        self,
        settings: Settings,
        fetcher: ManifestFetcher | Callable[..., Any] | None = None,
    ):
        self._fetch = fetcher or HttpxManifestFetcher(settings)

    def resolve_index_digest(
        self, ref: ImageReference, platforms: tuple[Platform, ...]
    ) -> str:
        """Resolve a tagged reference and verify all *platforms*.

        Returns the digest of the multi-platform index.  Raises
        :class:`RegistryError` with a human readable Chinese reason on any
        failure; the caller turns that into an admission denial.
        """
        if not platforms:
            raise RegistryError("未指定需要核对的平台")

        index, index_digest = self._fetch(ref, ref.tag or "latest")
        media_type = index.get("mediaType")
        manifests = index.get("manifests")
        if media_type not in INDEX_MEDIA_TYPES or not isinstance(manifests, list):
            raise NotAnIndex(
                "标签指向的清单不是 OCI 镜像索引(或 Docker manifest list)"
            )

        for platform in platforms:
            descriptor = next(
                (
                    entry
                    for entry in manifests
                    if isinstance(entry, dict) and platform.matches(entry)
                ),
                None,
            )
            if descriptor is None:
                raise PlatformMissing(
                    f"索引 {index_digest} 中缺少平台 {platform} 的子清单描述符"
                )

            child_digest = descriptor.get("digest", "")
            if not child_digest.startswith("sha256:"):
                raise RegistryError(f"平台 {platform} 的描述符缺少合法摘要")

            # Every required platform must be reachable through the *same*
            # index: fetch the child manifest by the digest recorded in it.
            child, returned_digest = self._fetch(ref, child_digest)
            if returned_digest != child_digest:
                raise RegistryError(
                    f"平台 {platform} 子清单摘要不一致: "
                    f"索引记录 {child_digest}, 仓库返回 {returned_digest}"
                )
            child_type = child.get("mediaType")
            if child_type not in MANIFEST_MEDIA_TYPES:
                # Nested indexes are not needed for the local test registry
                # and would complicate digest pinning semantics.
                raise RegistryError(
                    f"平台 {platform} 的子清单类型不受支持: {child_type!r}"
                )

        return index_digest
