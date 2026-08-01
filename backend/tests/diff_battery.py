#!/usr/bin/env python3
"""Diff a fresh battery run against golden_baseline.json. Prints ONLY changed
cells, skipping keys marked unstable for that question. Exit code 1 if any
non-advisory change is found (so it can gate a merge phase in CI).

Usage: python3 diff_battery.py results_new.json [golden_baseline.json]

DRIFT-TOLERANT keys: a question may list keys in "drift_tolerant" (e.g. "count"
for an ACTIVE player's career season-stats total — it grows nightly as the live
season advances). Those gate on DIRECTION, not equality: a bounded INCREASE is
drift (pass); a DECREASE, a vanish, or a jump over DRIFT_MAX_JUMP is a real signal
(fail). The rule is the gate's own — no human waves anything off.

ROUTING keys (tool_name, understood_as) are ADVISORY-BY-RULE — never gated. They
record the model's RAW tool pick, which the server-side redirect/guard OVERRIDES,
so they re-roll on every prompt_version bump even when the answer is identical
(gating them cried wolf on pure re-translation). What a route actually PRODUCED
shows up in the OUTCOME fields the gate already checks — source ('game_logs' is
the cycle redirect's fingerprint; '*_leaderboard' a ranked path), out_of_scope
(a guard decline), count + highlighted_stat (a two-way role) — so a real routing
break still fails the gate through those. We keep CAPTURING the routing keys and,
when a question has a genuine outcome diff, print how its routing moved too as
diagnostic context (did the route break, or just the answer?).
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
DRIFT_MAX_JUMP = 500   # up-by-a-few is the season; +500 (or a decrease) is a bug
ROUTING_KEYS = ("tool_name", "understood_as")  # advisory-by-rule; see module docstring


def main():
    new = json.load(open(sys.argv[1]))
    golden_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "golden_baseline.json")
    golden = json.load(open(golden_path))
    gq = golden["questions"]

    changed = 0
    advisory = 0
    routing_advisory = 0
    for qid in sorted(gq):
        g = gq[qid]
        gcell = g["cell"]
        unstable = set(g.get("unstable", []))
        drift_ok = set(g.get("drift_tolerant", []))
        ncell = (new.get(qid) or {}).get("cell", {})
        # Gate ONLY on keys the golden RECORDED. A newer run_battery may capture extra
        # fields (e.g. rates_val/splits_val added later) that older golden cells lack —
        # those must not read as changes. Missing/changed golden keys are still caught.
        keys = sorted(gcell)
        gated = []    # (k, gv, nv, note) — real outcome diffs that fail the gate
        routing = []  # (k, gv, nv) — routing keys that moved (advisory; shown as context)
        for k in keys:
            gv, nv = gcell.get(k), ncell.get(k)
            if gv == nv:
                continue
            if k in unstable:
                advisory += 1
                continue
            # ROUTING keys are advisory-by-rule: the raw pick re-rolls under the
            # server-side redirect/guard override, so never gate it — but remember it,
            # to print as context if this question ALSO has a genuine outcome diff.
            if k in ROUTING_KEYS:
                advisory += 1
                routing_advisory += 1
                routing.append((k, gv, nv))
                continue
            # DRIFT-TOLERANT: pass a bounded monotonic increase (the live season),
            # but still FAIL a decrease / vanish / gross jump (a real regression).
            if k in drift_ok and isinstance(gv, (int, float)) and not isinstance(gv, bool):
                if isinstance(nv, (int, float)) and not isinstance(nv, bool) \
                        and gv <= nv <= gv + DRIFT_MAX_JUMP:
                    advisory += 1
                    continue  # drift within bound — tolerated
                # else falls through to CHANGED (decrease, None, or jump > bound)
            note = "  [drift-tolerant: DECREASE/VANISH/JUMP]" if k in drift_ok else ""
            gated.append((k, gv, nv, note))

        for k, gv, nv, note in gated:
            changed += 1
            print(f"CHANGED {qid} [{k}]{note}  {g['q'][:55]}")
            print(f"    golden: {gv!r}")
            print(f"    new:    {nv!r}")
        # Diagnostic context: only when a real outcome diff fired, reveal whether the
        # ROUTE moved too (did the path break, or just the answer?). Silent otherwise.
        if gated and routing:
            for k, gv, nv in routing:
                print(f"    routing moved [{k}]: {gv!r} -> {nv!r}")

    print(f"\n{changed} non-advisory change(s); {advisory} advisory diff(s) skipped "
          f"({routing_advisory} routing).")
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
