#!/usr/bin/env python3
"""Trigger a batch merge (合版) and show the pullable reference.

Calls ``POST /api/v1/publish`` for a repository + batch.  The merge
service only succeeds when both platform reports (linux/amd64 and
linux/arm64) for the batch are present and still current; otherwise it
answers ``not_ready`` / ``conflict`` / ``failed`` and includes the last
good reference when one exists.

Usage:
    python3 scripts/publish_batch.py --repository apps/merged --batch b-2026-1
    python3 scripts/publish_batch.py ... --status   # only query batch state

Environment:
    MERGE_URL  merge API base (default http://localhost:8080)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MERGE_URL = os.getenv("MERGE_URL", "http://localhost:8080").rstrip("/")


def http(method: str, url: str, data: bytes | None = None):
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        # FastAPI encodes error details in the body; keep them visible.
        return exc.code, exc.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument(
        "--status", action="store_true",
        help="只查询批次状态, 不触发合版",
    )
    args = parser.parse_args()

    if args.status:
        query = urllib.parse.urlencode(
            {"repository": args.repository, "batch": args.batch}
        )
        status, body = http(
            "GET", f"{MERGE_URL}/api/v1/batches?{query}"
        )
        print(f"GET /api/v1/batches -> HTTP {status}")
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
        return 0 if 200 <= status < 300 else 1

    payload = json.dumps(
        {"repository": args.repository, "batch": args.batch}
    ).encode()
    status, body = http(
        "POST", f"{MERGE_URL}/api/v1/publish", data=payload
    )
    print(f"POST /api/v1/publish -> HTTP {status}")
    detail = json.loads(body)
    print(json.dumps(detail, ensure_ascii=False, indent=2))

    # The error envelope nests the same body under "detail".
    result = detail.get("detail", detail)
    if status == 200 and result.get("reference"):
        print("\n可拉取引用:")
        print(f"  docker pull {result['reference']}")
        return 0
    if isinstance(result, dict) and result.get("last_good_reference"):
        print("\n本次未发布, 上次成功引用仍可拉取:")
        print(f"  docker pull {result['last_good_reference']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
