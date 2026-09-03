#!/usr/bin/env python3
"""Trigger the catch-up update via HTTP and wait for it to finish.

Designed to run as a Railway cron service a few hours after the morning
nightly. The catch-up refreshes season stats for players whose late West
Coast games slipped past BDL's data-lag window when the morning nightly
ran.

The schedule itself lives in the Railway dashboard, not in this repo, so
treat this note as observation rather than definition: the service named
"catchup-update-cron-12PM PT" was seen firing at 19:01 UTC. Railway crons
are UTC, so that is noon Pacific only while daylight time is in effect —
the same UTC hour is 11am Pacific in winter. Check the dashboard before
relying on the local time in the service name.

Reads the API base URL from BASEBALL_API_URL. POSTs to
`/admin/catchup-update` to spawn the background worker, then polls
`/admin/catchup-update/status` every 30 seconds until the
`running` flag clears. Prints the final state and exits 0 on
success, 1 on any error (network failure, non-OK HTTP, worker
raised, or wall-clock timeout).

Usage:
    BASEBALL_API_URL=https://your-domain backend/venv/bin/python backend/scripts/run_catchup.py
"""

import json
import os
import sys
import time

import requests


_POLL_INTERVAL_SECONDS = 30
# Cap total runtime so a wedged worker doesn't block a Railway
# cron job indefinitely. 15 minutes is well past the expected
# 1-2 minute runtime and well under the API-side 30-min stale
# threshold so a timeout here doesn't overlap the auto-reset.
_MAX_POLL_SECONDS = 15 * 60


def _admin_headers() -> dict:
    """The admin token, read from the SAME environment this script runs in.

    ⚠️ EXITS RATHER THAN CALLING WITHOUT IT. The endpoint answers 404 to an
    unauthenticated caller, so a missing token would surface as "route not
    found" — which reads like a deploy problem and would send the next person
    looking in the wrong place entirely.
    """
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        print("ERROR: ADMIN_TOKEN is not set", file=sys.stderr)
        raise SystemExit(1)
    return {"X-Admin-Token": token}


def main() -> int:
    api_url = os.getenv("BASEBALL_API_URL", "").strip().rstrip("/")
    if not api_url:
        print("ERROR: BASEBALL_API_URL is not set", file=sys.stderr)
        return 1

    start_endpoint  = f"{api_url}/admin/catchup-update"
    status_endpoint = f"{api_url}/admin/catchup-update/status"

    print(f"POST {start_endpoint}")
    try:
        r = requests.post(start_endpoint, headers=_admin_headers(), timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR: start request failed: {exc}", file=sys.stderr)
        return 1
    print(f"HTTP {r.status_code}")
    print(r.text)
    if not r.ok:
        return 1

    # Poll until the running flag clears or we exceed the cap.
    elapsed = 0
    while elapsed < _MAX_POLL_SECONDS:
        time.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        try:
            s = requests.get(status_endpoint, headers=_admin_headers(), timeout=30)
        except requests.RequestException as exc:
            # Transient — keep polling. A persistent failure will
            # eventually trip the wall-clock cap below.
            print(
                f"WARNING: status poll failed (will retry): {exc}",
                file=sys.stderr,
            )
            continue
        if not s.ok:
            print(
                f"WARNING: status poll HTTP {s.status_code} (will retry)",
                file=sys.stderr,
            )
            continue
        try:
            state = s.json()
        except ValueError:
            print(
                f"WARNING: status poll returned non-JSON (will retry): "
                f"{s.text[:200]}",
                file=sys.stderr,
            )
            continue
        running = state.get("running", False)
        phase   = state.get("phase")
        print(f"[{elapsed}s] running={running} phase={phase}")
        if not running:
            print("Final state:")
            print(json.dumps(state, indent=2))
            # `error` set means the worker raised. The endpoint
            # also auto-resets `running=False` on stale lock and
            # leaves an "auto-reset:" prefix — treat both as cron
            # failures so Railway flags the run.
            if state.get("error"):
                return 1
            return 0

    print(
        f"ERROR: catch-up did not complete within {_MAX_POLL_SECONDS}s",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
