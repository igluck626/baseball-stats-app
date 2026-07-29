#!/usr/bin/env python3
"""Run the golden battery against a live /ask endpoint and capture the cells that
matter into a results JSON. tool_name is not in the /ask response, so it is joined
from the ask_log table (public DB URL via $ASK_DB_URL).

Usage:
  ASK_DB_URL="postgres://..." python3 run_battery.py --out results_A.json
  # base URL and device id default to production + a fixed battery identity so all
  # calls share ONE rate-limit bucket.

Captured per question (volatile fields — cached flag, timings, query_ms — are
deliberately NOT captured, so a diff isolates semantic change):
  tool_name (DB), status (DB), count, highlighted_stat, source, declined,
  out_of_scope, ambiguous, n_sample/n_leaders/n_splits, has_rates/milestone/
  streak/span/two_way, understood_as (post-guard, from the response), and the
  first 100 chars of answer and reason.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.request

BASE = os.getenv("ASK_BASE", "https://baseball-stats-app-production-0ef1.up.railway.app")
DEVICE = os.getenv("ASK_DEVICE", "phase0-golden-battery")
HERE = os.path.dirname(__file__)
BATTERY = os.getenv("ASK_BATTERY", os.path.join(HERE, "ask_battery.jsonl"))


def normalize(qtext):
    """Mirror api.main._normalize_question exactly (cache key + ask_log.normalized)."""
    s = (qtext or "").lower().strip().rstrip("?").strip().replace("'", "")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ask(q):
    body = json.dumps({"question": q}).encode()
    req = urllib.request.Request(f"{BASE}/ask", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-device-id": DEVICE})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"_http_error": e.read().decode()[:200]}
    except Exception as e:  # noqa: BLE001
        return -1, {"_error": str(e)}


def cell_from_response(http, d):
    def n(x):
        return len(x) if isinstance(x, list) else 0
    ua = d.get("understood_as")
    # VALUE capture (rates / split rows). Present in the response for the rate/split
    # tools, so scoping correctness is gate-VISIBLE, not just understood_as. NOTE:
    # these are store-snapshot-dependent — they gate a CODE change within one store,
    # and must be recaptured if the deployed plays store updates. diff_battery gates
    # only on keys the golden RECORDED, so adding these here never disturbs the
    # existing cells (whose golden has no rates_val/splits_val key).
    rates = d.get("rates") or None
    splits = d.get("splits") or None
    rates_val = (json.dumps({k: rates.get(k) for k in ("AVG", "OBP", "SLG", "AB", "H")},
                            sort_keys=True) if rates else None)
    splits_val = (json.dumps([[r.get("split_value"), r.get("AB"), r.get("AVG")]
                              for r in splits], sort_keys=True) if splits else None)
    return {
        "http": http,
        "count": d.get("count"),
        "highlighted_stat": d.get("highlighted_stat"),
        "source": d.get("source"),
        "declined": bool(d.get("declined")),
        "out_of_scope": bool(d.get("out_of_scope")),
        "ambiguous": bool(d.get("ambiguous")),
        "n_sample": n(d.get("sample")),
        "n_leaders": n(d.get("leaders")),
        "n_splits": n(d.get("splits")),
        "has_rates": bool(d.get("rates")),
        "rates_val": rates_val,
        "splits_val": splits_val,
        "has_milestone": bool(d.get("milestone")),
        "has_streak": bool(d.get("streak")),
        "has_span": bool(d.get("span")),
        "has_two_way": bool(d.get("two_way")),
        # post-guard injected params, canonicalised so key order can't cause a false diff
        "understood_as": json.dumps(ua, sort_keys=True) if ua is not None else None,
        "answer100": (d.get("answer") or "")[:100],
        "reason100": (d.get("reason") or "")[:100],
    }


def join_tool_names(items):
    """Fill tool_name + db_status from ask_log (latest row per normalized). Best
    effort — if the DB is unreachable, leaves them None and the diff still works on
    everything else."""
    url = os.getenv("ASK_DB_URL")
    if not url:
        print("  (no ASK_DB_URL — tool_name/db_status left null)", file=sys.stderr)
        return
    try:
        import psycopg2
        norms = list({it["norm"] for it in items})
        con = psycopg2.connect(url, connect_timeout=20)
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT ON (normalized) normalized, tool_name, status "
            "FROM ask_log WHERE normalized = ANY(%s) "
            "ORDER BY normalized, created_at DESC", (norms,))
        m = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.close(); con.close()
        for it in items:
            tn, st = m.get(it["norm"], (None, None))
            it["cell"]["tool_name"] = tn
            it["cell"]["db_status"] = st
    except Exception as e:  # noqa: BLE001
        print(f"  (ask_log join failed: {e})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    battery = [json.loads(l) for l in open(BATTERY) if l.strip()]
    print(f"running {len(battery)} questions against {BASE} as device={DEVICE!r}")

    results = {}
    items = []

    def run_one(rec):
        http, d = ask(rec["q"])
        return rec, http, d

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rec, http, d in ex.map(run_one, battery):
            cell = cell_from_response(http, d)
            results[rec["id"]] = {"q": rec["q"], "bucket": rec["bucket"], "cell": cell}
            items.append({"id": rec["id"], "norm": normalize(rec["q"]), "cell": cell})

    join_tool_names(items)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, sort_keys=True)
    n_calls = len(battery)
    print(f"wrote {len(results)} cells to {args.out}  ({n_calls} /ask calls)")


if __name__ == "__main__":
    main()
