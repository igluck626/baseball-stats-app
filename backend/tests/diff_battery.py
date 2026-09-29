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

Two more kinds of drift-tolerant key, for the same live season:
  • `leaders_val` — an open-ended leaderboard ("career", "since 2015"). Judged
    POSITION BY POSITION, never by who holds the spot: the k-th count may only
    rise (by at most DRIFT_MAX_JUMP) — on a board nobody's total can fall from,
    the k-th largest cannot fall either — and the k-th rate stays within 2% of
    its value (floor 0.010 below 1.0, 0.075 above). Names may reorder (a tie at
    .300 re-sorts as averages move).
  • `answer100` — an active player's answer in prose. The WORDS must match
    exactly with every number blanked out (a different player, stat or leader
    is a real change); then each number in order: whole numbers by the counting
    rule, decimals within 2% of their value, floor 0.010 below 1.0 and 0.075
    above (WAR moves both ways: 64.29 -> 64.21).

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
import re
import sys

HERE = os.path.dirname(__file__)
DRIFT_MAX_JUMP = 500   # up-by-a-few is the season; +500 (or a decrease) is a bug
ROUTING_KEYS = ("tool_name", "understood_as")  # advisory-by-rule; see module docstring

# Rate-line drift (rates_val / splits_val for an ACTIVE player — flagged by
# build_golden). A rate line splits into two kinds of component, judged differently:
#   * COUNTING (AB, H, IP, SO, BB, PA, ER, R, …): grows with the season -> same
#     direction rule as `count` (bounded monotonic increase; decrease/vanish/jump fails).
#   * RATE (AVG, OBP, SLG, OPS, ERA, WHIP, …): moves BOTH ways — an 0-for-4 lowers an
#     average — so direction can't gate it. Judged by TOLERANCE instead: a move within
#     RATE_TOL is a season; a move at/over it (or to non-numeric) is a regression.
# RATE_TOL = 0.075 sits above the worst single-season drift observed on the smallest
# live bucket in the battery (~.056, Judge's ~44-AB vs-one-team line) and below the
# .100 "real regression" threshold — so it tolerates a full season yet still catches a
# rate that jumps .100 or collapses toward zero (a .282 AVG -> 0 is a .282 move).
RATE_TOL = 0.075
# Rates on a leaderboard and decimals quoted in an answer (AVG .305, ERA 3.19, WAR
# 64.29): within 2% of the golden value, floored at 0.010 below 1.0 and at 0.075
# from 1.0 up. The floor below 1.0 is what keeps a batting average honest — at a
# flat 0.075 a .305 could read .380 and pass. A season moves Cole's ERA 0.02 and
# Judge's WAR 0.08; a broken figure moves far more. (`rates_val` keeps its own
# RATE_TOL, sized for small split samples — see above.)
DRIFT_REL_TOL = 0.02
DRIFT_FLOOR_UNDER_ONE = 0.010
DRIFT_FLOOR = 0.075
# A number in prose, INCLUDING a leading-dot decimal: a batting average is
# written ".305", and read as the whole number 305 it would pass as a counting
# stat that rose.
_NUM = re.compile(r"\d*\.\d+|\d+")
VALUE_KEYS = ("rates_val", "splits_val")
RATE_COMPONENTS = {"AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "wOBA",
                   "ERA", "WHIP", "K%", "BB%"}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _component_ok(key, g, n):
    """One rate-line component: (ok, reason). RATE keys gate on tolerance (both ways);
    everything else is a COUNTING component gated on bounded increase (like `count`)."""
    if not _num(g):                       # non-numeric baseline -> demand equality
        return (n == g), (f"{key} {g!r}->{n!r}" if n != g else "")
    if not _num(n):                       # value vanished / went non-numeric -> regression
        return False, f"{key} {g!r}->{n!r} (vanished)"
    if key in RATE_COMPONENTS:
        return (abs(n - g) < RATE_TOL), (f"{key} {g}->{n} (|Δ|>={RATE_TOL})"
                                         if abs(n - g) >= RATE_TOL else "")
    ok = g <= n <= g + DRIFT_MAX_JUMP     # counting component: bounded monotonic increase
    return ok, ("" if ok else f"{key} {g}->{n} (decrease/jump)")


def _rate_line_drift_ok(gv, nv):
    """True iff a rates_val (dict) or splits_val (list of [label, N, rate]) blob moved
    only as a live season would. Returns (ok, reason). New splits labels (a fresh month)
    are tolerated; a vanished label is not."""
    try:
        gj = json.loads(gv) if isinstance(gv, str) else gv
        nj = json.loads(nv) if isinstance(nv, str) else nv
    except Exception:
        return False, "unparseable blob"
    if isinstance(gj, dict) and isinstance(nj, dict):
        for key, g in gj.items():
            ok, why = _component_ok(key, g, nj.get(key))
            if not ok:
                return False, why
        return True, ""
    if isinstance(gj, list) and isinstance(nj, list):
        nmap = {row[0]: row for row in nj if isinstance(row, (list, tuple)) and row}
        for row in gj:
            if not (isinstance(row, (list, tuple)) and len(row) >= 3):
                return False, "malformed split row"
            label, gN, gR = row[0], row[1], row[2]
            nr = nmap.get(label)
            if nr is None:
                return False, f"split '{label}' vanished"
            ok, why = _component_ok(f"'{label}' N", gN, nr[1])   # bucket size: counting
            if not ok:
                return False, why
            ok, why = _component_ok("AVG", gR, nr[2])            # bucket rate: tolerance
            if not ok:
                return False, f"'{label}' rate {gR}->{nr[2]}"
        return True, ""
    return False, "blob type mismatch"


def _rate_drift_ok(g, n):
    """(ok, reason) for one rate or decimal: within DRIFT_REL_TOL of the golden
    value, floored per DRIFT_FLOOR_UNDER_ONE / DRIFT_FLOOR."""
    if not (_num(g) and _num(n)):
        return (g == n), ("" if g == n else f"{g!r}->{n!r}")
    floor = DRIFT_FLOOR_UNDER_ONE if abs(g) < 1.0 else DRIFT_FLOOR
    tol = max(DRIFT_REL_TOL * abs(g), floor)
    ok = abs(n - g) <= tol
    return ok, ("" if ok else f"{g}->{n} (|Δ|>{tol:.3f})")


def _leaders_drift_ok(gv, nv):
    """(ok, reason) for a `leaders_val` blob: a list of [label, ?, rate, count]
    rows. Same length; position by position, rates (index 2) by `_rate_drift_ok`
    and every other number by the counting rule; labels are not compared."""
    try:
        gj = json.loads(gv) if isinstance(gv, str) else gv
        nj = json.loads(nv) if isinstance(nv, str) else nv
    except Exception:
        return False, "unparseable leaderboard"
    if not (isinstance(gj, list) and isinstance(nj, list)) or len(gj) != len(nj):
        return False, "leaderboard length changed"
    for pos, (grow, nrow) in enumerate(zip(gj, nj), start=1):
        if len(grow) != len(nrow):
            return False, f"#{pos} row shape changed"
        for j, (g, n) in enumerate(zip(grow, nrow)):
            if j == 0 or (g is None and n is None):
                continue                         # the label: who holds the spot may change
            ok, why = _rate_drift_ok(g, n) if j == 2 else _component_ok(f"#{pos}", g, n)
            if not ok:
                return False, f"#{pos} {why}"
    return True, ""


def _prose_drift_ok(gv, nv):
    """(ok, reason) for an `answer100` sentence: identical words with numbers
    blanked; then each number in order — whole numbers by the counting rule,
    decimals by `_rate_drift_ok`."""
    if not (isinstance(gv, str) and isinstance(nv, str)):
        return False, "answer vanished"
    if _NUM.sub("#", gv) != _NUM.sub("#", nv):
        return False, "wording changed"
    for g, n in zip(_NUM.findall(gv), _NUM.findall(nv)):
        if "." in g or "." in n:
            ok, why = _rate_drift_ok(float(g), float(n))
            if not ok:
                return False, why
        else:
            ok, why = _component_ok("count", int(g), int(n))
            if not ok:
                return False, why
    return True, ""


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
            # DRIFT-TOLERANT: pass a season's worth of movement, still FAIL a real
            # regression. A rate-line value key (rates_val/splits_val) is judged
            # structurally — counting components by direction, rate components by
            # tolerance; a scalar (`count`) by bounded monotonic increase.
            if k in drift_ok:
                if k in ("leaders_val", "answer100"):
                    ok, why = (_leaders_drift_ok if k == "leaders_val" else _prose_drift_ok)(gv, nv)
                    if ok:
                        advisory += 1
                        continue
                    gated.append((k, gv, nv, f"  [drift-tolerant: {why}]"))
                    continue
                if k in VALUE_KEYS:
                    ok, why = _rate_line_drift_ok(gv, nv)
                    if ok:
                        advisory += 1
                        continue
                    gated.append((k, gv, nv, f"  [rate-line: {why}]"))
                    continue
                if _num(gv) and _num(nv) and gv <= nv <= gv + DRIFT_MAX_JUMP:
                    advisory += 1
                    continue  # scalar drift within bound — tolerated
                gated.append((k, gv, nv, "  [drift-tolerant: DECREASE/VANISH/JUMP]"))
                continue
            gated.append((k, gv, nv, ""))

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
