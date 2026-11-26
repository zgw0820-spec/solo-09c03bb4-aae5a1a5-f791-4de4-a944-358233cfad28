"""FastAPI mutating admission webhook for Kubernetes (AdmissionReview v1)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .admission.config import settings
from .admission.policy import AdmissionFailure, build_patches
from .admission.registry import RegistryClient

logger = logging.getLogger("image-digest-webhook")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="image-digest-admission", version="1.0.0")

ADMISSION_API_VERSION = "admission.k8s.io/v1"
ADMISSION_KIND = "AdmissionReview"
MUTATING_OPERATIONS = frozenset({"CREATE", "UPDATE"})


class AdmissionReviewPayload(BaseModel):
    # Keep the shape loose: Kubernetes guarantees admission.k8s.io/v1 but we
    # want to return a proper denial instead of a 422 for malformed payloads.
    apiVersion: str | None = None
    kind: str | None = None
    request: dict[str, Any] | None = None


def _allow(uid: str | None) -> dict[str, Any]:
    return {
        "apiVersion": ADMISSION_API_VERSION,
        "kind": ADMISSION_KIND,
        "response": {"allowed": True, "uid": uid},
    }


def _deny(uid: str | None, reason: str) -> dict[str, Any]:
    logger.warning("admission denied: %s", reason)
    return {
        "apiVersion": ADMISSION_API_VERSION,
        "kind": ADMISSION_KIND,
        "response": {"allowed": False, "uid": uid, "status": {"code": 403, "message": reason}},
    }


def _patch_response(uid: str | None, patches: list[dict[str, str]]) -> dict[str, Any]:
    patch_bytes = json.dumps(patches, separators=(",", ":")).encode("utf-8")
    return {
        "apiVersion": ADMISSION_API_VERSION,
        "kind": ADMISSION_KIND,
        "response": {
            "allowed": True,
            "uid": uid,
            "patchType": "JSONPatch",
            "patch": base64.b64encode(patch_bytes).decode("ascii"),
        },
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/mutate")
def mutate(review: AdmissionReviewPayload) -> dict[str, Any]:
    if review.apiVersion != ADMISSION_API_VERSION or review.kind != ADMISSION_KIND:
        return _deny(
            None,
            f"仅支持 {ADMISSION_API_VERSION} {ADMISSION_KIND}, "
            f"收到 apiVersion={review.apiVersion!r} kind={review.kind!r}",
        )

    request = review.request or {}
    uid = request.get("uid")
    operation = request.get("operation")
    resource = request.get("resource") or {}

    # Only Deployment create/update carries a pod template we understand.
    if resource.get("group") != "apps" or resource.get("resource") != "deployments":
        return _allow(uid)
    if operation not in MUTATING_OPERATIONS:
        return _allow(uid)

    request_object = request.get("object")
    if not isinstance(request_object, dict):
        return _deny(uid, "准入请求缺少 object 或 object 不是对象")

    try:
        patches = build_patches(
            request_object=request_object,
            client=RegistryClient(settings),
            settings=settings,
        )
    except AdmissionFailure as exc:
        # Any single image failure denies the whole request, no patch emitted.
        return _deny(uid, str(exc))
    except Exception:  # pragma: no cover - defensive last resort
        logger.exception("unexpected webhook error")
        return _deny(uid, "准入控制器内部错误, 详见 webhook 日志")

    if not patches:
        return _allow(uid)
    return _patch_response(uid, patches)
