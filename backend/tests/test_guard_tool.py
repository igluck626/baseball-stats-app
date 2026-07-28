#!/usr/bin/env python3
"""Unit-test _guard_tool IN ISOLATION, without importing the FastAPI app (which
pulls pandas/uvicorn/etc). The constraint subsystem is pure (only re + a log stub
+ the static team_crosswalk), so we AST-extract those exact top-level defs from
api/main.py and exec them in a clean namespace — a single source of truth that
can't drift from the code under test.

Two kinds of assertion:
  PARITY   — for the SCOPED tools, _guard_tool must be BYTE-IDENTICAL to the live
             _guard_scoped_tool (same decline string, same injected tool_input).
             For the ANSWERABLE tools, its inject/decline for handedness / base /
             count / home_away / unsupported must match the _ANSWERABLE block
             (replicated here as an oracle, since that block is inline in ask()).
  DIVERGENCE — the two behaviors the merge intentionally ADDS (a fix), which the
             current answerable path does NOT do: season-drop repair for
             answerable tools, and opponent for query_situational. Asserted
             explicitly and labelled, so the divergence is intentional and visible.

Run: python3 test_guard_tool.py    (exit 0 = all pass)
"""
import ast
import copy
import logging
import os
import sys

logging.disable(logging.WARNING)   # silence the guard's inject telemetry during tests

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.join(HERE, os.pardir, "api")
sys.path.insert(0, API)
MAIN = os.path.join(API, "main.py")

# ---- extract the constraint subsystem from main.py by name -------------------
NEEDED = [
    "_UNSUPPORTED_CONSTRAINTS", "_detect_constraints", "_unsupported_reason",
    "_detect_season_scope", "_OPP_TEAM_ALT", "_OPP_NICK_RE", "_OPP_CITY_ALT",
    "_OPP_CITY_RE", "_OPP_UNAMBIG_CITY_ALT", "_OPP_UNAMBIG_CITY_RE",
    "_OPP_UNAMBIG_CITY_BARE", "_detect_opponent", "_OPP_NICK_BARE",
    "_OPP_CITY_BARE", "_classify_opponent", "_norm_team",
    "_resolve_opponent_codes", "_franchise_label", "_question_opponent",
    "_TOOL_SCOPE_CAPS", "_SCOPE_DECLINE", "_guard_scoped_tool",
    "_GUARD_ANSWERABLE", "_GUARD_CAPS", "_guard_tool",
]


def load_subsystem():
    import re
    import team_crosswalk
    src = open(MAIN).read()
    tree = ast.parse(src)
    seg = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef,)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for nm in names:
            if nm in NEEDED:
                seg[nm] = ast.get_source_segment(src, node)
    missing = [n for n in NEEDED if n not in seg]
    assert not missing, f"could not extract from main.py: {missing}"
    ns = {"re": re, "team_crosswalk": team_crosswalk,
          "log": logging.getLogger("guard-test")}
    # exec in source order so forward refs resolve
    ordered = sorted(seg, key=lambda n: src.index(seg[n]))
    for nm in ordered:
        exec(seg[nm], ns)
    return ns


NS = load_subsystem()
guard_tool = NS["_guard_tool"]
guard_scoped = NS["_guard_scoped_tool"]
detect_constraints = NS["_detect_constraints"]
detect_season = NS["_detect_season_scope"]
question_opponent = NS["_question_opponent"]
unsupported_reason = NS["_unsupported_reason"]

FAILS = []
N = 0


def check(name, got, want):
    global N
    N += 1
    if got != want:
        FAILS.append(f"{name}\n    got:  {got!r}\n    want: {want!r}")


# ---- ORACLE for the _ANSWERABLE inline block (ask(), ~10607) -----------------
# Replicates exactly what that block does: unsupported -> _unsupported_reason and
# STOP; else inject handedness (role-aware, only if neither side set) + the other
# _detect_constraints injectables on a true drop. It does NOT touch season/opponent.
def answerable_oracle(q, ti):
    ti = copy.deepcopy(ti)
    inj, unsup = detect_constraints(q)
    if unsup:
        return unsupported_reason(unsup), ti
    hand = inj.pop("handedness", None)
    if hand and ti.get("pitcher_hand") is None and ti.get("batter_side") is None:
        side = "batter_side" if ti.get("role") == "pit" else "pitcher_hand"
        ti[side] = hand
    for p, v in inj.items():
        if ti.get(p) is None:
            ti[p] = v
    return None, ti


SCOPED = ["query_milestone", "query_streak", "query_span",
          "query_streak_leaderboard", "query_span_leaderboard",
          "query_game_achievement"]
ANSWERABLE = ["query_situational", "query_rates", "query_splits",
              "query_rate_leaderboard", "query_leaderboard"]

# questions spanning every constraint type + combinations + already-set cases
QS = [
    "How many home runs has Aaron Judge hit off left-handed pitchers?",
    "How many strikeouts does Gerrit Cole have against left-handed batters?",
    "How many home runs has Aaron Judge hit with the bases loaded?",
    "How many home runs has Aaron Judge hit with runners in scoring position?",
    "How many home runs has Aaron Judge hit in a full count?",
    "How many home runs has Aaron Judge hit at home?",
    "How many home runs has Aaron Judge hit on the road?",
    "How many home runs did Aaron Judge hit in 2019?",
    "How many home runs has Aaron Judge hit since 2020?",
    "How many home runs did Babe Ruth hit before 1930?",
    "How many home runs did Aaron Judge hit between 2017 and 2019?",
    "How many home runs has Aaron Judge hit against the Red Sox?",
    "How many home runs has Aaron Judge hit against Washington?",
    "How many home runs has Aaron Judge hit against Detroit?",
    "How many home runs has Aaron Judge hit against the Chicago Cubs?",
    "How many home runs has Aaron Judge hit in day games?",
    "How many home runs has Aaron Judge hit in one-run games?",
    "How many home runs has Aaron Judge hit off lefties at home in 2022?",
    "How many home runs has Aaron Judge hit against the Red Sox at home since 2020?",
    "How many home runs has Aaron Judge hit?",
]
# tool_input variants: empty (true drop) + some already-set (must NOT double-inject)
TIS = [
    {"player": "Aaron Judge", "role": "bat"},
    {"player": "Aaron Judge", "role": "pit"},
    {"player": "Aaron Judge", "role": "bat", "home_away": "away"},   # venue already set
    {"player": "Aaron Judge", "role": "bat", "season": 2021},        # season already set
    {"player": "Aaron Judge", "role": "bat", "pitcher_hand": "R"},   # side already set
]

# ============================================================================
# 1. PARITY — scoped tools: _guard_tool ≡ _guard_scoped_tool (byte for byte)
# ============================================================================
for tool in SCOPED:
    for q in QS:
        for ti in TIS:
            a = copy.deepcopy(ti); da = guard_tool(q, tool, a)
            b = copy.deepcopy(ti); db = guard_scoped(q, tool, b)
            check(f"[scoped-parity decline] {tool} :: {q[:40]} :: {sorted(ti)}", da, db)
            check(f"[scoped-parity inject ] {tool} :: {q[:40]} :: {sorted(ti)}", a, b)

# ============================================================================
# 2. PARITY — answerable tools: injectables/unsupported match the _ANSWERABLE oracle
#    (restrict to questions WITHOUT season/opponent, where the oracle is complete)
# ============================================================================
NO_SEASON_OPP = [q for q in QS if not detect_season(q) and question_opponent(q, {}) is None]
for tool in ANSWERABLE:
    for q in NO_SEASON_OPP:
        for ti in TIS:
            a = copy.deepcopy(ti); da = guard_tool(q, tool, a)
            do, oti = answerable_oracle(q, ti)
            check(f"[answerable-parity decline] {tool} :: {q[:40]} :: {sorted(ti)}", da, do)
            check(f"[answerable-parity inject ] {tool} :: {q[:40]} :: {sorted(ti)}", a, oti)

# ============================================================================
# 3. DIVERGENCE (intended fixes) — the merge ADDS these; current answerable = no-op
# ============================================================================
# 3a. season-drop repair now applies to answerable tools
ti = {"player": "Aaron Judge", "role": "bat"}
a = copy.deepcopy(ti); guard_tool("How many home runs did Aaron Judge hit in 2019?", "query_rates", a)
check("[divergence season-single injected on query_rates]", a.get("season"), 2019)
_, oti = answerable_oracle("How many home runs did Aaron Judge hit in 2019?", ti)
check("[divergence season: current oracle does NOT inject]", oti.get("season"), None)

a = copy.deepcopy(ti); guard_tool("How many home runs has Aaron Judge hit since 2020?", "query_splits", a)
check("[divergence season-range injected on query_splits]", a.get("season_start"), 2020)

# 3b. opponent now resolves for query_situational (runner has the filter)
a = copy.deepcopy(ti); d = guard_tool("How many home runs has Aaron Judge hit against the Red Sox?", "query_situational", a)
check("[divergence opponent decline None on situational]", d, None)
check("[divergence opponent_codes injected on situational]", bool(a.get("opponent_codes")), True)
check("[divergence opponent_label injected on situational]", bool(a.get("opponent_label")), True)

# 3c. Phase 2: opponent now RESOLVES for query_rates too (runner filter added) —
# it injects opponent_codes (no label needed; rates has no self-team note), where
# it previously silently widened to all teams.
a = copy.deepcopy(ti); d = guard_tool("What is Aaron Judge's batting average against the Red Sox?", "query_rates", a)
check("[phase2 opponent decline None on query_rates]", d, None)
check("[phase2 opponent_codes injected on query_rates]", a.get("opponent_codes"), ["BOS"])
# opponent still NOT in caps for splits/rate_leaderboard/leaderboard -> scope-decline
a = copy.deepcopy(ti); d = guard_tool("What is Aaron Judge's batting average against the Red Sox?", "query_splits", a)
check("[opponent scope-declined on query_splits (no filter yet)]",
      d, NS["_SCOPE_DECLINE"].format(cond="a specific opponent (team)"))

# 3d. ambiguous city declines on ANY tool that reaches opponent (situational here)
a = copy.deepcopy(ti); d = guard_tool("How many home runs has Aaron Judge hit against Washington?", "query_situational", a)
check("[ambiguous-city declines on situational]", (d is not None and "Washington" in d), True)

# ============================================================================
# 4. CAPS DISCIPLINE — a tool declares opponent ONLY once its runner has the filter.
# Phase 1: query_situational. Phase 2: query_rates. The rest must still NOT (yet).
# ============================================================================
for tool in ["query_splits", "query_rate_leaderboard", "query_leaderboard"]:
    check(f"[caps: {tool} must NOT declare opponent (no filter yet)]", "opponent" in NS["_GUARD_CAPS"][tool], False)
for tool in ["query_situational", "query_rates"]:
    check(f"[caps: {tool} DOES declare opponent (filter exists)]", "opponent" in NS["_GUARD_CAPS"][tool], True)
# scoped caps byte-identical to _TOOL_SCOPE_CAPS
for tool, caps in NS["_TOOL_SCOPE_CAPS"].items():
    check(f"[caps: {tool} scoped caps unchanged]", set(NS["_GUARD_CAPS"][tool]), set(caps))

# ---- report -----------------------------------------------------------------
print(f"ran {N} assertions across {len(SCOPED)} scoped + {len(ANSWERABLE)} answerable tools")
if FAILS:
    print(f"\n{len(FAILS)} FAILURES:")
    for f in FAILS[:40]:
        print("  " + f)
    sys.exit(1)
print("ALL PASS — _guard_tool matches current systems on parity; divergences are the intended fixes.")
