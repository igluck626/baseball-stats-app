"""The nightly's Chadwick step: map new balldontlie players to MLBAM ids from the
Chadwick register, under Isaac's standing approval for the ACCEPT class only
(2026-10-16).

Why daily: the register publishes about once a month (05-01, 06-01, 07-01,
08-02, 09-01, 10-02 in 2026), but 189 of the 242 players who debuted in 2026
were already in it before their first game — a daily check maps most new
players on their first night, and the rest at the next monthly snapshot (a
median of 24 days after their first game, 36 at most, measured 2026-10-16).

What it does, each run:
  1. lists the balldontlie players our tables don't map (active rosters plus
     anyone with a season line) — nothing else runs when there are none;
  2. refreshes the register only when its latest commit changed, keeping a slim
     copy (rows with a key_mlbam, five columns) on the volume;
  3. classifies them with `chadwick_catchup.classify` — the full rule: exact
     folded name + full birth date, unique in the register AND among
     balldontlie players, MLBAM id not already taken;
  4. writes ONLY the ACCEPTs, through `chadwick_catchup.plan_writes` and
     `_write`, which re-check each one and read every write back (stored must
     equal submitted);
  5. returns what it wrote and every HOLD, for the nightly log.

⚠️ Nothing but ACCEPT is ever written. A HOLD — including "not in the register
yet" — is listed for Isaac and waits.

The newly mapped players' earlier games are not fetched here: `mapped_ids` in
the summary goes to the nightly's catch-up phase (`catch_up_player_gamelogs`).
"""
from __future__ import annotations

import collections
import csv
import glob
import io
import json
import logging
import os
import sys
import urllib.request
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import chadwick_catchup as cc                                        # noqa: E402

log = logging.getLogger(__name__)

CACHE_DIR = os.environ.get("CHADWICK_CACHE_DIR", "/data/chadwick")
REPO = "chadwickbureau/register"
FILES = [f"people-{c}.csv" for c in "0123456789abcdef"]
KEEP = ("key_mlbam", "name_first", "name_last", "birth_year", "birth_month", "birth_day", "mlb_played_first")


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-stats-app/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_sha() -> Optional[str]:
    """The register's newest commit, or None if GitHub can't be asked."""
    try:
        return json.loads(_get(f"https://api.github.com/repos/{REPO}/commits?per_page=1", 20))[0]["sha"]
    except Exception as exc:                                         # noqa: BLE001
        log.warning("chadwick: latest commit lookup failed: %s", exc)
        return None


def slim(rows) -> list[dict]:
    """Only rows with an MLBAM id, only the columns the matcher reads."""
    return [{k: r.get(k, "") for k in KEEP} for r in rows if r.get("key_mlbam")]


def load_register(sha: Optional[str], cache_dir: str = CACHE_DIR,
                  fetch: Callable[[str], bytes] = _get) -> tuple[list[dict], str]:
    """(rows, version). Uses the cached slim copy for `sha` when present; downloads
    and caches it otherwise. If `sha` is unknown, falls back to the newest cached
    copy, and only downloads `master` when there is none."""
    os.makedirs(cache_dir, exist_ok=True)
    cached = sorted(glob.glob(os.path.join(cache_dir, "register-*.csv")), key=os.path.getmtime)
    if sha is None and cached:
        path = cached[-1]
    else:
        ref = sha or "master"
        path = os.path.join(cache_dir, f"register-{ref}.csv")
        if not os.path.exists(path):
            rows = []
            for f in FILES:
                body = fetch(f"https://raw.githubusercontent.com/{REPO}/{ref}/data/{f}").decode("utf-8")
                rows += slim(csv.DictReader(io.StringIO(body)))
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=KEEP); w.writeheader(); w.writerows(rows)
            os.replace(tmp, path)
            for old in cached:                                      # keep only the current copy
                if old != path:
                    os.remove(old)
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows, os.path.basename(path)[len("register-"):-len(".csv")]


def decide(players: list[dict], register: list[dict], held: dict, mapped_to: dict) -> tuple[list[dict], dict]:
    """(plan, classification). The standing approval IS the fresh ACCEPT list, so
    `plan_writes` gets it as both the approved and the accepted set. Pure."""
    out = cc.classify(players, register, held)
    accepted = {x["bdl_id"]: x["mlbam"] for x in out[cc.ACCEPT]}
    return cc.plan_writes(accepted, accepted, mapped_to, held), out


def run(season: int) -> dict:
    """The nightly entry point. Returns a summary for the log; raises nothing
    the caller must handle beyond its own non-fatal wrapper."""
    import psycopg2
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    con.set_session(readonly=True)
    cur = con.cursor()
    cur.execute("SELECT player_id, bdl_id FROM players UNION ALL SELECT player_id, bdl_id FROM pitchers")
    held = collections.defaultdict(set)
    mapped, mapped_to = set(), collections.defaultdict(set)
    for pid, bid in cur.fetchall():
        held[pid].add(bid)
        if bid is not None:
            mapped.add(int(bid))
            mapped_to[int(bid)].add(pid)
    players = [p for p in cc.fetch_unmapped(mapped, season) if p and p["id"] not in mapped]
    summary = {"unmapped": len(players), "written": 0, "stored": 0, "holds": {}}
    if not players:
        con.close()
        return summary
    register, version = load_register(latest_sha())
    plan, out = decide(players, register, held, mapped_to)
    summary["register"] = version
    summary["holds"] = {c: [f"{x['name']} (bdl {x['bdl_id']})" for x in out[c]]
                        for c in cc.CLASSES if c != cc.ACCEPT and out[c]}
    act = [e for e in plan if e["action"] in (cc.STAMP, cc.CREATE)]
    if act:
        rc = cc._write(plan, cur, season)    # writes via its own session; reads back on `cur`
        summary["written"] = len(act)
        summary["stored"] = sum(1 for e in act if e.get("stored"))
        summary["ok"] = rc == 0
        summary["mapped"] = [f"bdl {e['bdl_id']} -> {e['mlbam']} ({e.get('result')})" for e in act]
        summary["mapped_ids"] = [(int(e["bdl_id"]), int(e["mlbam"])) for e in act if e.get("stored")]
    con.close()
    return summary
