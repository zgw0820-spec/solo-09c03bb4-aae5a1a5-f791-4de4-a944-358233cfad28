"""Admission policy: platform selection, per-request resolution, patching."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .images import ImageReferenceError, parse_image_reference
from .registry import Platform, RegistryClient, RegistryError

ARCH_LABEL = "kubernetes.io/arch"
CONTAINER_LIST_PATHS = (
    "/spec/template/spec/containers",
    "/spec/template/spec/initContainers",
)


class AdmissionFailure(Exception):
    """A policy failure that must deny the whole request."""


def select_platforms(pod_spec: dict[str, Any], settings: Settings) -> tuple[Platform, ...]:
    """Pick the platforms to verify for a pod template spec.

    A ``nodeSelector`` entry for ``kubernetes.io/arch`` pins the
    architecture; without one both default platforms are checked.
    """
    node_selector = pod_spec.get("nodeSelector")
    if isinstance(node_selector, dict) and ARCH_LABEL in node_selector:
        arch = str(node_selector[ARCH_LABEL]).strip()
        if not arch:
            raise AdmissionFailure(f"节点选择器 {ARCH_LABEL} 的值为空")
        return (Platform(os="linux", architecture=arch),)
    return tuple(Platform.parse(item) for item in settings.default_platforms)


def _iter_containers(pod_spec: dict[str, Any]):
    for field_name, json_path in (
        ("initContainers", CONTAINER_LIST_PATHS[1]),
        ("containers", CONTAINER_LIST_PATHS[0]),
    ):
        containers = pod_spec.get(field_name)
        if containers is None:
            continue
        if not isinstance(containers, list):
            raise AdmissionFailure(f"Pod 模板字段 {field_name} 不是数组")
        for index, container in enumerate(containers):
            if not isinstance(container, dict):
                raise AdmissionFailure(
                    f"Pod 模板字段 {field_name}[{index}] 不是对象"
                )
            yield container, f"{json_path}/{index}/image"


def _extract_pod_spec(request_object: dict[str, Any]) -> dict[str, Any]:
    template = (request_object.get("spec") or {}).get("template")
    if not isinstance(template, dict):
        raise AdmissionFailure("Deployment 缺少 spec.template")
    pod_spec = template.get("spec")
    if not isinstance(pod_spec, dict):
        raise AdmissionFailure("Deployment 缺少 spec.template.spec")
    return pod_spec


def build_patches(
    request_object: dict[str, Any],
    client: RegistryClient,
    settings: Settings,
) -> list[dict[str, str]]:
    """Build JSON Patch entries pinning tagged images to index digests.

    The same tag is resolved at most once per admission call.  Any failure
    raises :class:`AdmissionFailure`; the caller denies without a patch.
    """
    pod_spec = _extract_pod_spec(request_object)
    platforms = select_platforms(pod_spec, settings)

    # Per-admission cache: (registry, repository, tag) -> resolved digest.
    # Nothing here survives the request, so the next admission re-queries.
    resolved: dict[tuple[str, str, str], str] = {}
    patches: list[dict[str, str]] = []
    seen_patch_paths: set[str] = set()

    for container, patch_path in _iter_containers(pod_spec):
        if "image" not in container:
            continue
        raw_image = container["image"]
        if not isinstance(raw_image, str) or not raw_image.strip():
            raise AdmissionFailure(f"容器镜像引用为空 ({patch_path})")

        try:
            ref = parse_image_reference(raw_image)
        except ImageReferenceError as exc:
            raise AdmissionFailure(f"镜像 {raw_image!r} 无法解析: {exc}") from exc

        if ref.registry not in settings.allowed_registries:
            raise AdmissionFailure(
                f"镜像 {raw_image!r} 的仓库 {ref.registry} 不在受支持的本地仓库"
                f" {sorted(settings.allowed_registries)} 内"
            )

        # Already pinned to a digest: leave untouched, no registry call.
        if ref.is_pinned:
            continue

        cache_key = (ref.registry, ref.repository, ref.tag or "")
        if cache_key not in resolved:
            try:
                resolved[cache_key] = client.resolve_index_digest(ref, platforms)
            except RegistryError as exc:
                raise AdmissionFailure(
                    f"镜像 {str(ref)!r} 核对失败: {exc}"
                ) from exc

        new_image = ref.with_digest(resolved[cache_key])
        if new_image == raw_image:
            continue
        if patch_path in seen_patch_paths:
            continue
        seen_patch_paths.add(patch_path)
        patches.append({"op": "replace", "path": patch_path, "value": new_image})

    return patches
