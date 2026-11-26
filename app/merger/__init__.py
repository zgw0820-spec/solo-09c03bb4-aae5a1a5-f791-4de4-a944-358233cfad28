"""Batch merge (合版) feature: collect per-platform child-manifest reports
from two CI jobs and publish a single OCI multi-platform index.

The package only depends on the Python standard library so its logic stays
unit-testable without FastAPI/httpx installed; the HTTP layer in
``app.main`` and the registry gateway in ``registry_gateway`` are thin
adapters around it.
"""
