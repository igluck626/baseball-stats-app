#!/usr/bin/env python3
"""Trigger the nightly update via HTTP. Designed to run as a Railway cron service.

Reads the API base URL from the BASEBALL_API_URL env var (e.g.
"https://baseball.up.railway.app"), POSTs to /admin/nightly-update, prints
the response, and exits 0 on success / 1 on failure.

Usage:
    BASEBALL_API_URL=https://your-domain backend/venv/bin/python backend/scripts/run_nightly.py

The endpoint kicks the update off in a background thread and returns
immediately, so this script's HTTP call completes in well under a second.
Watch /admin/nightly-update/status afterward for live progress + errors.
"""

import os
import sys

import requests


def main() -> int:
    api_url = os.getenv("BASEBALL_API_URL", "").strip().rstrip("/")
    if not api_url:
        print("ERROR: BASEBALL_API_URL is not set", file=sys.stderr)
        return 1

    endpoint = f"{api_url}/admin/nightly-update"
    print(f"POST {endpoint}")

    try:
        r = requests.post(endpoint, timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR: request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {r.status_code}")
    print(r.text)

    if not r.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
