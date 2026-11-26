"""Domain types and input validation for the batch merge feature.

A *report* (回报) is what a per-platform CI job sends after pushing its
child manifest to the shared local registry.  A *batch* (构建批次) is the
unit that must collect exactly two valid reports (linux/amd64 and
linux/arm64) before an OCI index can be published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The two platforms this service knows how to merge.  Reports for any
# other platform are rejected at the API boundary.
SUPPORTED_PLATFORMS: tuple[str, ...] = ("linux/amd64", "linux/arm64")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Distribution spec repository name: path segments of [a-z0-9] plus . _ -,
# one or more segments separated by single slashes.
_REPO_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
# Batch identifiers are intentionally conservative (CI build ids / uuids).
_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MergeValidationError(ValueError):
    """Raised for malformed merge API input (surfaced as HTTP 422)."""


@dataclass(frozen=True)
class PlatformId:
    os: str
    architecture: str

    @classmethod
    def parse(cls, raw: str) -> "PlatformId":
        if not isinstance(raw, str) or raw not in SUPPORTED_PLATFORMS:
            raise MergeValidationError(
                f"平台 {raw!r} 不受支持, 仅支持: {', '.join(SUPPORTED_PLATFORMS)}"
            )
        os_name, _, arch = raw.partition("/")
        return cls(os=os_name, architecture=arch)

    def __str__(self) -> str:
        return f"{self.os}/{self.architecture}"


def validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or not repository.strip():
        raise MergeValidationError("仓库路径不能为空")
    repository = repository.strip()
    if len(repository) > 512 or any(
        not _REPO_SEGMENT_RE.fullmatch(segment)
        for segment in repository.split("/")
    ):
        raise MergeValidationError(f"仓库路径格式非法: {repository!r}")
    return repository


def validate_batch(batch: str) -> str:
    if not isinstance(batch, str) or not batch.strip():
        raise MergeValidationError("构建批次不能为空")
    batch = batch.strip()
    if not _BATCH_RE.fullmatch(batch):
        raise MergeValidationError(
            f"构建批次格式非法: {batch!r} (字母数字开头, 允许 . _ -, 最长 128)"
        )
    return batch


def validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest.strip()):
        raise MergeValidationError(
            f"子清单摘要格式非法: {digest!r} (应为 sha256: 加 64 位十六进制)"
        )
    return digest.strip()


def validate_attempt(attempt: int) -> int:
    # bool is a subclass of int; reject it explicitly to avoid True == 1.
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise MergeValidationError(
            f"尝试号必须是 >= 1 的整数, 收到: {attempt!r}"
        )
    return attempt


@dataclass(frozen=True)
class Report:
    """One validated per-platform child-manifest report."""

    repository: str
    batch: str
    platform: PlatformId
    attempt: int
    digest: str

    @property
    def platform_name(self) -> str:
        return str(self.platform)

    def as_dict(self) -> dict:
        return {
            "repository": self.repository,
            "batch": self.batch,
            "platform": self.platform_name,
            "attempt": self.attempt,
            "digest": self.digest,
        }
