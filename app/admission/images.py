"""Parsing of OCI image references.

Supports the common ``[host[:port]/]path[:tag][@digest]`` shape used for
local registries.  References that already carry a digest are returned
with ``digest is not None`` and are never rewritten by the webhook.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REGISTRY = "docker.io"
DEFAULT_TAG = "latest"
_SUPPORTED_DIGEST_PREFIXES = ("sha256:",)


class ImageReferenceError(ValueError):
    """Raised when an image reference cannot be parsed."""


@dataclass(frozen=True)
class ImageReference:
    registry: str
    """Registry host (possibly with port), e.g. ``localhost:5000``."""

    repository: str
    """Repository path, e.g. ``apps/nginx``."""

    tag: str | None
    """Tag when the reference is mutable; ``None`` for digest pins."""

    digest: str | None
    """Manifest digest when the reference is already pinned."""

    @property
    def is_pinned(self) -> bool:
        return self.digest is not None

    @property
    def repository_url_path(self) -> str:
        # Repository names may contain slashes but must be kept verbatim.
        return "/".join(segment for segment in self.repository.split("/"))

    def with_digest(self, digest: str) -> str:
        """Render the reference with the same host/repo, pinned to *digest*."""
        return f"{self.registry}/{self.repository}@{digest}"

    def __str__(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag or DEFAULT_TAG}"


def _looks_like_host(first_segment: str) -> bool:
    # A first path segment is treated as a registry host when it contains a
    # port separator or a dot (``localhost:5000``, ``registry.local``).
    return "." in first_segment or ":" in first_segment or first_segment == "localhost"


def parse_image_reference(raw: str) -> ImageReference:
    if not isinstance(raw, str) or not raw.strip():
        raise ImageReferenceError("镜像引用为空")
    ref = raw.strip()
    if ref.startswith("/") or ref.endswith("/"):
        raise ImageReferenceError(f"镜像引用格式非法: {raw}")

    digest: str | None = None
    if "@" in ref:
        ref, digest_part = ref.rsplit("@", 1)
        if not digest_part or not digest_part.startswith(_SUPPORTED_DIGEST_PREFIXES):
            raise ImageReferenceError(
                f"镜像摘要格式不受支持(仅支持 sha256): {raw}"
            )
        digest = digest_part

    first_slash = ref.find("/")
    if first_slash >= 0 and _looks_like_host(ref[:first_slash]):
        registry = ref[:first_slash]
        remainder = ref[first_slash + 1 :]
    else:
        registry = DEFAULT_REGISTRY
        remainder = ref

    if not remainder:
        raise ImageReferenceError(f"镜像引用缺少仓库名: {raw}")

    tag: str | None = None
    if digest is None:
        # A tag may only live in the final path segment.
        last_segment = remainder.rsplit("/", 1)[-1]
        if ":" in last_segment:
            remainder, tag = remainder.rsplit(":", 1)
            if not tag:
                raise ImageReferenceError(f"镜像标签为空: {raw}")
        else:
            tag = DEFAULT_TAG
    elif ":" in remainder.rsplit("/", 1)[-1]:
        # ``repo:tag@digest`` is legal but redundant; keep the digest only.
        remainder, maybe_tag = remainder.rsplit(":", 1)
        if "/" not in maybe_tag and maybe_tag:
            tag = None

    if not remainder or any(not segment for segment in remainder.split("/")):
        raise ImageReferenceError(f"镜像仓库名格式非法: {raw}")
    # Reject characters that cannot appear in a repository path / tag.
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
    if any(ch not in allowed for ch in remainder):
        raise ImageReferenceError(f"镜像仓库名含非法字符: {raw}")
    if tag is not None and any(ch not in allowed for ch in tag):
        raise ImageReferenceError(f"镜像标签含非法字符: {raw}")

    return ImageReference(
        registry=registry, repository=remainder, tag=tag, digest=digest
    )
