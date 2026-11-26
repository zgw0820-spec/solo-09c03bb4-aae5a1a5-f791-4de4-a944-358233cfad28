#!/usr/bin/env python3
"""Send a Kubernetes AdmissionReview v1 request to the webhook.

Usage:
    python3 scripts/send_admission.py samples/deployment-create.json
    python3 scripts/send_admission.py samples/deployment-missing-platform.json

Prints the verdict and, when present, the base64 JSONPatch decoded for
inspection.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:8080/mutate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_file", help="AdmissionReview JSON file")
    parser.add_argument("--url", default=WEBHOOK_URL)
    args = parser.parse_args()

    with open(args.request_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        args.url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            review = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"webhook HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 2

    response = review.get("response") or {}
    print(f"uid     : {response.get('uid')}")
    print(f"allowed : {response.get('allowed')}")

    if not response.get("allowed"):
        status = response.get("status") or {}
        print(f"denied  : {status.get('message')}")
        return 1

    if response.get("patch"):
        print(f"patchType: {response.get('patchType')}")
        patch = json.loads(base64.b64decode(response["patch"]))
        print("patch   :")
        for operation in patch:
            print(
                f"  {operation['op']:7s} {operation['path']} -> {operation['value']}"
            )
    else:
        print("patch   : <none>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
