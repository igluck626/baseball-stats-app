#!/usr/bin/env python3
"""Build golden_baseline.json from N (>=2) independent battery runs against current
main. A cell KEY whose value differs across ANY pair of runs is UNSTABLE (model
non-determinism — chiefly questions the model sometimes routes to cannot_answer,
which is never cached and so re-translates every run; and tool_name/understood_as,
which record the model's RAW pick even when the guard makes the user-facing outcome
deterministic). Unstable keys are recorded per question and EXCLUDED from the diff
gate (advisory only). More runs -> a tighter estimate of the flaky set.

Usage: python3 build_golden.py results_A.json results_B.json [results_C.json ...]
Writes golden_baseline.json (last run's values) and prints the unstable summary.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "golden_baseline.json")


def flag_drift_tolerant(questions):
    """Mark a question's `count` cell drift_tolerant when it's an ACTIVE player's
    career (or current-inclusive) season-stats total — those grow nightly as the
    live season advances, so the gate must judge them by direction, not equality
    (see diff_battery). Computed HERE so a golden rebuild recomputes it rather than
    quietly losing the flags. NEEDS the DB (active-status) via $ASK_DB_URL — the same
    URL run_battery uses for tool_name. If it's absent, we WARN and skip rather than
    emit an unflagged golden that would then read the season as a fault."""
    cands = {}   # qid -> player, for gated season-stats CAREER/current-inclusive counts
    for qid, rec in questions.items():
        c = rec["cell"]
        if c.get("count") is None or "count" in set(rec.get("unstable", [])):
            continue
        if not str(c.get("source") or "").startswith("season_stats"):
            continue
        try:
            ua = json.loads(c["understood_as"]) if c.get("understood_as") else {}
        except Exception:
            ua = {}
        # a single past season or a bounded (season_end) range is FIXED, not drift-prone
        if ua.get("season") is not None or ua.get("season_end") is not None:
            continue
        if ua.get("player"):
            cands[qid] = ua["player"]
    if not cands:
        return 0
    url = os.getenv("ASK_DB_URL")
    if not url:
        print("  WARNING: no ASK_DB_URL — drift_tolerant flags NOT computed. Rebuild "
              "with DB access to restore them, or the gate will read the live season "
              "as a fault.", file=sys.stderr)
        return 0
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=25); cur = con.cursor()
        active = {}
        for p in set(cands.values()):
            base = p.split(" Jr")[0]      # DB stores names without a suffix
            yr = None
            for tbl, pl in (("player_seasons", "players"), ("pitcher_seasons", "pitchers")):
                cur.execute(f"SELECT max(ps.year) FROM {tbl} ps JOIN {pl} p "
                            f"ON p.player_id = ps.player_id WHERE p.name ILIKE %s", (base + "%",))
                y = cur.fetchone()[0]
                if y and (yr is None or y > yr):
                    yr = y
            active[p] = yr is not None and yr >= 2025   # played in the last ~2 seasons
        cur.close(); con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: drift-flag DB query failed ({exc}); flags NOT computed.", file=sys.stderr)
        return 0
    n = 0
    for qid, p in cands.items():
        if active.get(p):
            questions[qid]["drift_tolerant"] = ["count"]
            n += 1
    return n


def main():
    runs = [json.load(open(p)) for p in sys.argv[1:]]
    assert len(runs) >= 2, "need >=2 result files"
    ids = sorted(set().union(*[set(r) for r in runs]))
    last = runs[-1]
    questions = {}
    total_unstable = 0
    unstable_by_key = {}
    unstable_qs = []
    n_cannot_answer = 0
    for qid in ids:
        cells = [(r.get(qid) or {}).get("cell", {}) for r in runs]
        keys = sorted(set().union(*[set(c) for c in cells]))
        unstable = [k for k in keys if len({repr(c.get(k)) for c in cells}) > 1]
        rec = next(r[qid] for r in reversed(runs) if qid in r)
        goldcell = (last.get(qid) or {}).get("cell") or cells[0]
        # RULE (not observation): a question whose translation was cannot_answer in
        # ANY run is model-non-deterministic by construction — cannot_answer is never
        # cached, so it re-translates every run and can flip to a real tool at any
        # time (or vice versa). Flag ALL its cells advisory so the gate is stable
        # regardless of which flaky questions happen to wobble on a given run, not
        # just the ones observed to differ across these N baseline runs.
        cannot_answer = any(c.get("tool_name") == "cannot_answer" for c in cells)
        if cannot_answer:
            n_cannot_answer += 1
            unstable = keys
        questions[qid] = {"q": rec["q"], "bucket": rec["bucket"],
                          "cell": goldcell, "unstable": unstable,
                          "cannot_answer_class": cannot_answer}
        if unstable:
            total_unstable += len(unstable)
            unstable_qs.append((qid, rec["q"], unstable))
            for k in unstable:
                unstable_by_key[k] = unstable_by_key.get(k, 0) + 1

    n_drift = flag_drift_tolerant(questions)

    golden = {"meta": {"n_runs": len(runs), "n_questions": len(ids),
                       "n_cannot_answer_class": n_cannot_answer,
                       "n_gated_questions": len(ids) - len(unstable_qs),
                       "n_unstable_cells": total_unstable,
                       "n_unstable_questions": len(unstable_qs),
                       "n_drift_tolerant": n_drift,
                       "unstable_by_key": unstable_by_key},
              "questions": questions}
    json.dump(golden, open(OUT, "w"), indent=2, ensure_ascii=False, sort_keys=True)
    print(f"wrote {OUT}")
    print(f"questions: {len(ids)}   drift_tolerant count cells: {n_drift}")
    print(f"UNSTABLE questions: {len(unstable_qs)}  (cells: {total_unstable})")
    print(f"unstable by key: {unstable_by_key}")
    for qid, q, keys in unstable_qs:
        print(f"  {qid} {keys}  {q[:60]}")


if __name__ == "__main__":
    main()
