"""Runtime configuration for the mutating admission webhook.

Only images whose registry host is in ``ALLOWED_REGISTRIES`` are ever
resolved or patched.  The registry that the webhook queries can be a
different host than the one used inside the cluster (for example the
in-container Docker Compose name ``registry:5000`` while the workloads
keep using ``localhost:5000``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_hosts(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # Host:port values that are considered "local repositories". Any image
    # whose reference points at another host is denied.
    allowed_registries: tuple[str, ...] = field(
        default_factory=lambda: _split_hosts(
            os.getenv("ALLOWED_REGISTRIES", "localhost:5000,registry:5000")
        )
    )
    # Host used when talking to the registry from inside the webhook pod.
    registry_upstream: str = os.getenv("REGISTRY_UPSTREAM", "http://registry:5000")
    # Platforms checked when the pod template does not pin an architecture.
    default_platforms: tuple[str, ...] = ("linux/amd64", "linux/arm64")
    request_timeout: float = float(os.getenv("REGISTRY_TIMEOUT", "5"))


settings = Settings()
