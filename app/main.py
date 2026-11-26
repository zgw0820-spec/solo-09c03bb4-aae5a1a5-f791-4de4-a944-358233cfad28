"""FastAPI entrypoint: mutating admission webhook + batch merge API.

Endpoints:

* ``POST /mutate``           - Kubernetes AdmissionReview (unchanged);
* ``POST /api/v1/reports``   - CI per-platform child-manifest report;
* ``POST /api/v1/publish``   - merge a batch into one OCI index;
* ``GET  /api/v1/batches/{repository}/{batch}`` - in-memory batch status;
* ``GET  /healthz``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .admission.config import settings
from .admission.policy import AdmissionFailure, build_patches
from .admission.registry import RegistryClient
from .merger.models import (
    PlatformId,
    Report,
    validate_attempt,
    validate_batch,
    validate_digest,
    validate_repository,
)
from .merger.registry_gateway import HttpxRegistryGateway, ReportRejected, GatewayError
from .merger.service import MergeService
from .merger.store import MergeStore, ReportConflict

logger = logging.getLogger("image-digest-webhook")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="image-digest-admission", version="2.0.0")

ADMISSION_API_VERSION = "admission.k8s.io/v1"
ADMISSION_KIND = "AdmissionReview"
MUTATING_OPERATIONS = frozenset({"CREATE", "UPDATE"})

# Single-process shared state (task requirement: in-memory only).
merge_store = MergeStore()
merge_service = MergeService(
    store=merge_store,
    gateway=HttpxRegistryGateway(settings),
    settings=settings,
)


def get_merge_service() -> MergeService:
    return merge_service


# --------------------------------------------------------------------------- #
# Admission webhook (unchanged behaviour)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Batch merge API
# --------------------------------------------------------------------------- #
class ReportRequest(BaseModel):
    # Reject unknown fields so a typo (e.g. "attempts") cannot be silently
    # dropped and cause confusing registry-side failures.
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(..., description="仓库路径, 如 apps/demo")
    batch: str = Field(..., description="构建批次标识")
    platform: str = Field(..., description="linux/amd64 或 linux/arm64")
    attempt: int = Field(..., description="尝试号, >= 1, 同平台越大越新")
    digest: str = Field(..., description="已推送子清单的 sha256 摘要")


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    batch: str


def _report_payload(report: Report) -> dict[str, Any]:
    return report.as_dict()


def _snapshot_payload(snapshot, service: MergeService) -> dict[str, Any]:
    return {
        "repository": snapshot.repository,
        "batch": snapshot.batch,
        "reports": {
            platform: _report_payload(report)
            for platform, report in sorted(snapshot.reports.items())
        },
        "last_published_index": snapshot.last_published_index,
        "last_good_reference": service.last_good_reference(snapshot),
    }


@app.post("/api/v1/reports")
def submit_report(
    payload: ReportRequest,
    service: MergeService = Depends(get_merge_service),
) -> dict[str, Any]:
    """Receive one per-platform CI report.

    Same batch+platform keeps the highest-attempt valid report; a repeated
    attempt with a different digest is a 409 conflict.
    """
    try:
        repository = validate_repository(payload.repository)
        batch = validate_batch(payload.batch)
        platform = PlatformId.parse(payload.platform)
        attempt = validate_attempt(payload.attempt)
        digest = validate_digest(payload.digest)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    report = Report(
        repository=repository,
        batch=batch,
        platform=platform,
        attempt=attempt,
        digest=digest,
    )
    try:
        # Verify against the registry (config-blob platform check) before
        # the report is allowed to enter in-memory state.
        outcome, snapshot = service.register_report(report)
    except ReportRejected as exc:
        # Invalid content: missing manifest/blob, wrong type, platform
        # mismatch. Nothing was registered.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GatewayError as exc:
        # Registry itself is unreachable / misbehaving.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ReportConflict as exc:
        last_good = service.last_good_reference(
            service.status(repository, batch)
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "report_conflict",
                "message": str(exc),
                "existing": exc.existing.as_dict(),
                "incoming": exc.incoming.as_dict(),
                "last_good_reference": last_good,
            },
        ) from exc

    return {
        "status": outcome,
        "message": {
            "accepted": "回报已登记为该平台最新有效回报",
            "duplicate": "回报与已登记内容一致, 无需重复处理",
            "superseded": "已有更大尝试号的有效回报, 本次迟到回报已忽略",
        }[outcome],
        "report": report.as_dict(),
        "batch": _snapshot_payload(snapshot, service),
    }


@app.post("/api/v1/publish")
def publish_batch(
    payload: PublishRequest,
    service: MergeService = Depends(get_merge_service),
) -> dict[str, Any]:
    """Merge the two latest valid reports of a batch into one OCI index."""
    try:
        repository = validate_repository(payload.repository)
        batch = validate_batch(payload.batch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = service.publish(repository, batch)
    body = {
        "status": result.status,
        "repository": result.repository,
        "batch": result.batch,
        "reference": result.reference,
        "index_digest": result.index_digest,
        "last_good_reference": result.last_good_reference,
        "reason": result.reason,
        "reports": {
            platform: report.as_dict()
            for platform, report in sorted(result.reports.items())
        },
    }
    if result.status == "published":
        return body
    if result.status == "not_ready":
        raise HTTPException(status_code=422, detail=body)
    if result.status == "conflict":
        raise HTTPException(status_code=409, detail=body)
    # Registry verification / index write failure.
    raise HTTPException(status_code=502, detail=body)


@app.get("/api/v1/batches")
def get_batch(
    repository: str,
    batch: str,
    service: MergeService = Depends(get_merge_service),
) -> dict[str, Any]:
    """Return the in-memory state of one (repository, batch).

    Query parameters are used because repository paths may contain slashes.
    """
    try:
        repository = validate_repository(repository)
        batch = validate_batch(batch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot_payload(service.status(repository, batch), service)
