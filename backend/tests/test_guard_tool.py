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
import re
import sys

logging.disable(logging.WARNING)   # silence the guard's inject telemetry during tests

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.join(HERE, os.pardir, "api")
sys.path.insert(0, API)
MAIN = os.path.join(API, "main.py")

# ---- extract the constraint subsystem from main.py by name -------------------
NEEDED = [
    "_UNSUPPORTED_CONSTRAINTS", "_detect_constraints", "_detect_month",
    "_MONTH_NAMES", "_MONTH_SCOPE_RE", "_MONTH_TO_NUM", "_unsupported_reason",
    "_detect_season_scope", "_OPP_TEAM_ALT", "_OPP_NICK_RE", "_OPP_CITY_ALT",
    "_OPP_CITY_RE", "_OPP_UNAMBIG_CITY_ALT", "_OPP_UNAMBIG_CITY_RE",
    "_OPP_UNAMBIG_CITY_BARE", "_detect_opponent", "_OPP_NICK_BARE",
    "_OPP_CITY_BARE", "_classify_opponent", "_norm_team",
    "_resolve_opponent_codes", "_franchise_label", "_question_opponent",
    "_TOOL_SCOPE_CAPS", "_SCOPE_DECLINE", "_guard_scoped_tool",
    "_GUARD_ANSWERABLE", "_GUARD_CAPS", "_guard_tool",
    # fast-path normalization invariants (the served-answer-gated /ask templates depend on
    # these to make the model's variant tool_inputs converge to one served answer)
    "_canon_event", "_CANON_EVENT", "_PITCHING_ONLY_EVENTS", "_BATTING_ONLY_FORCE",
    "_asked_stat", "_RATE_STAT_NOUN", "_STAT_NOUN", "_TOTAL_ONLY_EVENTS",
    # RBI-vs-runs-scored verb discriminator (the noun is 'run(s)' either way)
    "_RBI_VERB", "_RBI_BOUND", "_SCORING_VERB", "_RISP_PHRASE",
    "_asks_rbi_not_runs", "_fix_rbi_verb_event",
    # fast-path stat whitelist (2026-08-14 widening pass)
    "_FP_COUNT", "_FP_PITCH_ONLY", "_FP_RATE", "_FP_PITCH_RATE", "_FP_RATE_WORDS",
    "_FP_RATE_RE", "_FP_COUNT_RE", "_fp_name_season", "_FP_NAME_RE", "_FP_STOP",
    "_FP_TAIL_VERB", "_asked_season_rate", "_QUESTION_RATE",
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
# Phase 3: opponent now RESOLVES for query_splits too (runner filter added).
a = copy.deepcopy(ti); d = guard_tool("What is Aaron Judge's batting average against the Red Sox by handedness?", "query_splits", a)
check("[phase3 opponent decline None on query_splits]", d, None)
check("[phase3 opponent_codes injected on query_splits]", a.get("opponent_codes"), ["BOS"])
# Phase 4: opponent now RESOLVES for query_rate_leaderboard too (runner filter added).
a = copy.deepcopy(ti); d = guard_tool("Who has the best batting average against the Red Sox?", "query_rate_leaderboard", a)
check("[phase4 opponent decline None on query_rate_leaderboard]", d, None)
check("[phase4 opponent_codes injected on query_rate_leaderboard]", a.get("opponent_codes"), ["BOS"])
# Phase 5: opponent now RESOLVES for query_leaderboard too (runner filter added).
a = copy.deepcopy(ti); d = guard_tool("Who has the most home runs against the Red Sox?", "query_leaderboard", a)
check("[phase5 opponent decline None on query_leaderboard]", d, None)
check("[phase5 opponent_codes injected on query_leaderboard]", a.get("opponent_codes"), ["BOS"])

# 3d. ambiguous city declines on ANY tool that reaches opponent (situational here)
a = copy.deepcopy(ti); d = guard_tool("How many home runs has Aaron Judge hit against Washington?", "query_situational", a)
check("[ambiguous-city declines on situational]", (d is not None and "Washington" in d), True)

# ============================================================================
# 4. CAPS DISCIPLINE — a tool declares opponent ONLY once its runner has the filter.
# Phase 1: query_situational. Phase 2: query_rates. The rest must still NOT (yet).
# ============================================================================
# Phase 5: ALL five answerable tools now declare opponent (every runner has the filter).
for tool in ["query_situational", "query_rates", "query_splits", "query_rate_leaderboard", "query_leaderboard"]:
    check(f"[caps: {tool} DOES declare opponent (filter exists)]", "opponent" in NS["_GUARD_CAPS"][tool], True)
# scoped caps byte-identical to _TOOL_SCOPE_CAPS
for tool, caps in NS["_TOOL_SCOPE_CAPS"].items():
    check(f"[caps: {tool} scoped caps unchanged]", set(NS["_GUARD_CAPS"][tool]), set(caps))

# ---- FAST-PATH NORMALIZATION INVARIANTS --------------------------------------
# The /ask deterministic fast path emits ONE canonical tool_input and relies on these
# server-side normalizations to make the model's OTHER (equally-valid) variant tool_inputs
# converge to the SAME served answer — the premise of the "served-answer-identical" gate.
# If any of these breaks, a served-answer-gated template would silently serve something the
# model would not, so pin them here where the test suite runs.
_canon_event = NS["_canon_event"]
_asked_stat = NS["_asked_stat"]
_PITCH_ONLY = NS["_PITCHING_ONLY_EVENTS"]
_BAT_FORCE = NS["_BATTING_ONLY_FORCE"]
# PITCHER templates emit role:pit — the counting decisions (SV/W/L/ER) and the rate stats
# (ERA/WHIP) — and the model's no-role / wrong-role variants converge ONLY because these are
# pitching-only events whose role is force-set to 'pit' downstream.
for _ev in ("SV", "W", "L", "ER", "ERA", "WHIP"):
    check(f"[fast-path invariant: {_ev} pitching-only -> role:pit forced]",
          _canon_event(_ev) in _PITCH_ONLY, True)
# BATTING-ONLY counting templates (RBI/SB/TB) emit {event,player} with NO role; they converge to
# batting ONLY because these events are force-set to role:bat downstream. If any dropped out of
# the force set, a stray model role:pit would send it to the empty pitching column silently.
for _ev in ("RBI", "SB", "TB"):
    check(f"[fast-path invariant: {_ev} batting-only -> role:bat forced]",
          _canon_event(_ev) in _BAT_FORCE, True)
# RATE templates emit query_rates{player}; the model's {player,stat:<S>} and {player,role:bat}
# variants converge ONLY because the highlighted stat is derived from the QUESTION (via
# _asked_stat), not from the tool_input. One assertion per rate phrasing the template accepts,
# each keyed to the stat its served card must highlight — if any phrasing stopped mapping to its
# stat, the served answer would silently diverge and this test says so.
for _q, _want in (
    ("What is Aaron Judge's batting average?", "AVG"),
    ("What's Mike Trout's average?", "AVG"),
    ("What is Aaron Judge's on-base percentage?", "OBP"),
    ("What is Aaron Judge's on base percentage?", "OBP"),
    ("What's Mike Trout's OBP?", "OBP"),
    ("What is Aaron Judge's slugging percentage?", "SLG"),
    ("What's Mike Trout's slugging?", "SLG"),
    ("What is Aaron Judge's SLG?", "SLG"),
    ("What is Aaron Judge's OPS?", "OPS"),
):
    check(f"[fast-path invariant: rate phrasing {_q!r} -> {_want} highlight from question]",
          _asked_stat(_q), _want)

# ---- OPPONENT fast-path template: seam invariants ---------------------------
# The opponent template emits the SAME plain {player,event}/{player} the model does and lets the
# downstream guard resolve the club from the question. Its correctness rests entirely on the SHARED
# resolver behaving as below — so pin those invariants here (they need no DB, only team_crosswalk).
_detect_opponent = NS["_detect_opponent"]
_resolve_opponent_codes = NS["_resolve_opponent_codes"]
# (1) a nickname resolves; a single-club city resolves; the three multi-club cities are ambiguous.
check("[opp: nickname 'the Red Sox' -> nick]",
      _detect_opponent("home runs against the Red Sox"), ("nick", "red sox"))
check("[opp: nick wins over bare city ('the Chicago Cubs')]",
      _detect_opponent("home runs against the Chicago Cubs"), ("nick", "cubs"))
check("[opp: single-club city 'Detroit' -> city_ok]",
      _detect_opponent("home runs against Detroit"), ("city_ok", "detroit"))
for _city in ("New York", "Chicago", "Washington"):
    _r = _detect_opponent(f"home runs against {_city}")
    check(f"[opp: ambiguous city {_city!r} -> ('city', ...) so the template FALLS THROUGH]",
          (_r is not None and _r[0] == "city"), True)
# (2) franchise span — the Dodgers fold Brooklyn (BRO) with both retro (LAN) and modern (LAD) codes.
check("[opp: Dodgers franchise span folds BRO+LAD+LAN]",
      {"BRO", "LAD", "LAN"}.issubset(set(_resolve_opponent_codes("dodgers"))), True)
# (3) 'lefties' is NOT a club -> None -> the base keeps the 'against' clause and the model sets
# batter_side (never mis-read as an opponent).
check("[opp: 'against lefties' is not an opponent -> None]",
      _detect_opponent("home runs against lefties"), None)

# (4) the TAIL-CHECK + strip — the invariant that keeps composed filters out. Mirrors the mk17
# block in _ask_fast_path exactly: fire only when a resolvable club sits at the very END; a filter
# after the club ('at home') prevents the strip so the template falls through to the model.
def _opp_gate(q):
    """Returns (fires: bool, base_after_strip). Replica of the _ask_fast_path opponent block."""
    q0 = re.sub(r"\s{2,}", " ", (q or "")).strip()
    _o = _detect_opponent(q0)
    if _o is None:
        return (False, q0)                       # no opponent clause (normal path)
    if _o[0] not in ("nick", "city_ok"):
        return (False, q0)                       # ambiguous/unknown -> fall through
    if not q0.lower().rstrip(" ?").endswith(_o[1].lower()):
        return (False, q0)                       # club not at the tail -> fall through
    base = re.sub(r"^(.*)\s+(?:against|versus|vs\.?|facing|off)\s+.*$", r"\1", q0, flags=re.I).strip()
    return (True, base)

_fires, _base = _opp_gate("How many home runs has Aaron Judge hit against the Red Sox?")
check("[opp tail-check: pure 'against the Red Sox' FIRES]", _fires, True)
check("[opp tail-check: strip leaves the bare count shape]",
      _base, "How many home runs has Aaron Judge hit")
check("[opp tail-check: 'against the Red Sox at home' does NOT fire (composed filter)]",
      _opp_gate("How many home runs has Aaron Judge hit against the Red Sox at home?")[0], False)
check("[opp tail-check: 'against the Red Sox in 2019' does NOT fire (composed filter)]",
      _opp_gate("What is Aaron Judge's batting average against the Red Sox in 2019?")[0], False)
check("[opp tail-check: bare New York does NOT fire (ambiguous)]",
      _opp_gate("How many home runs has Aaron Judge hit against New York?")[0], False)
check("[opp tail-check: 'as a Yankee' does NOT fire (team-scoped, no 'against')]",
      _opp_gate("How many home runs has Aaron Judge hit as a Yankee?")[0], False)

# (5) EVENT gate — the opponent template must fire ONLY on situationally-splittable batting counts.
# H/RBI/SB/TB live only in season-stats (_TOTAL_ONLY_EVENTS) and CANNOT be split by opponent — the
# runner declines them, so the template excludes them (never fire into a decline). HR/2B/3B/1B/BB
# ARE plays events and stay. This pins the exact set the exclusion depends on.
_TOTAL_ONLY = NS["_TOTAL_ONLY_EVENTS"]
for _ev in ("H", "RBI", "SB", "TB"):
    check(f"[opp event-gate: {_ev} is total-only -> template excludes it (falls through)]",
          _ev in _TOTAL_ONLY, True)
for _ev in ("HR", "2B", "3B", "1B", "BB"):
    check(f"[opp event-gate: {_ev} is a plays event -> template keeps it (serves by opponent)]",
          _ev in _TOTAL_ONLY, False)

# ---- RBI vs RUNS SCORED — the verb decides, not the noun ---------------------
# Both stats are asked with the noun 'run(s)', so 'his 2000th run' alone is genuinely
# ambiguous and the model resolved it to R in EVERY milestone phrasing (measured live
# 2026-08-13), including the count form 'runs ... batted in' which served Aaron's 2174
# RUNS as his 2297 RBI. The detector fires on the VERB only, so the seam holds by
# construction: Henderson's 'scored his 2000th run' must stay R.
_asks_rbi = NS["_asks_rbi_not_runs"]
_fix_rbi = NS["_fix_rbi_verb_event"]

for _q in ("When did Hank Aaron drive in his 2000th run?",
           "When did Hank Aaron knock in his 2000th run?",
           "When did Hank Aaron bat in his 2000th run?",
           "How many runs has Hank Aaron batted in?",
           "How many runs did Hank Aaron drive in in 1970?",
           "How many runs has Aaron Judge driven in?",
           "How many runs has Hank Aaron knocked in?",
           "How many runs has Hank Aaron plated?",
           "How many runs did Aaron knock in during 1971?",
           # a drive-in verb still wins when the question ALSO scopes to RISP, whose
           # 'scoring position' must not be read as the verb 'to score'
           "How many runs has Judge driven in with runners in scoring position?"):
    check(f"[rbi-verb: FIRES on {_q!r}]", _asks_rbi(_q), True)

# THE SEAM — 'runs SCORED' keeps event R. Includes the trap where a scoring question
# contains a lineup-position 'batting in ...', which must NOT read as 'batted in'.
for _q in ("When did Rickey Henderson score his 2000th run?",
           "When did Rickey Henderson reach his 2000th run?",
           "How many runs has Rickey Henderson scored?",
           "How many runs did Rickey Henderson score in 1990?",
           "How many times has Rickey Henderson scored a run?",
           "What are Rickey Henderson's career runs scored?",
           "How many runs has Judge scored batting in the leadoff spot?",
           "How many runs has Judge scored while batting in the 2 hole?",
           "How many runs has Gerrit Cole allowed?",
           "What is Aaron Judge's batting average?",
           "How many home runs has Aaron Judge hit?",
           "How many home runs has Gerrit Cole given up?",
           "How many walks has Aaron Judge drawn?",
           "When did Barry Bonds hit his 756th home run?"):
    check(f"[rbi-verb: SILENT on {_q!r}]", _asks_rbi(_q), False)

# The correction itself: only an R is rewritten, and only on a drive-in verb.
_ti = {"player": "Hank Aaron", "event": "R", "n": 2000}
check("[rbi-verb: corrects R -> RBI]",
      (_fix_rbi("When did Hank Aaron drive in his 2000th run?", _ti), _ti["event"]),
      (True, "RBI"))
_ti = {"player": "Rickey Henderson", "event": "R", "n": 2000}
check("[rbi-verb: leaves a scored-run R alone]",
      (_fix_rbi("When did Rickey Henderson score his 2000th run?", _ti), _ti["event"]),
      (False, "R"))
_ti = {"player": "Hank Aaron", "event": "RBI", "n": 2000}
check("[rbi-verb: no-op on an already-correct RBI]",
      (_fix_rbi("When did Hank Aaron drive in his 2000th run?", _ti), _ti["event"]),
      (False, "RBI"))
_ti = {"player": "Aaron Judge", "event": "HR"}
check("[rbi-verb: never rewrites a non-R event]",
      (_fix_rbi("How many home runs has Aaron Judge driven in?", _ti), _ti["event"]),
      (False, "HR"))
check("[rbi-verb: tolerates an empty tool_input]", _fix_rbi("anything", None), False)

# ---- fast-path STAT WHITELIST (2026-08-14 widening) --------------------------
# Each stat here was measured SERVED by the model against mk19 before being added, so the
# template is pure latency. The blocked half is the mk18 rule restated: a fast-path match
# must end in an answer, so a stat that DECLINES must never reach a template — and adding
# neighbours to the whitelist is exactly how a blocked one sneaks in.
_FPC = NS["_FP_COUNT"]
_FPPO = NS["_FP_PITCH_ONLY"]
_FPRR = NS["_FP_RATE_RE"]
_FPCR = NS["_FP_COUNT_RE"]
_FPPR = NS["_FP_PITCH_RATE"]
_FPR = NS["_FP_RATE"]
_PITCH_FORCE = NS["_PITCHING_ONLY_EVENTS"]
_BAT_FORCE = NS["_BATTING_ONLY_FORCE"]

for _noun, _ev in (("runs", "R"), ("run", "R"), ("at-bats", "AB"), ("at bats", "AB"),
                   ("extra-base hits", "XBH"), ("extra base hits", "XBH"),
                   ("wins above replacement", "WAR"), ("war", "WAR"),
                   ("hit by pitch", "HBP"), ("innings pitched", "IP"), ("innings", "IP"),
                   ("complete games", "CG"), ("shutouts", "SHO")):
    check(f"[fp-stat: count noun {_noun!r} -> {_ev}]", _FPC.get(_noun), _ev)

# The pre-existing nouns must not have shifted underneath the additions.
for _noun, _ev in (("home runs", "HR"), ("hits", "H"), ("earned runs", "ER"),
                   ("runs batted in", "RBI"), ("total bases", "TB"), ("walks", "BB")):
    check(f"[fp-stat: existing noun {_noun!r} still -> {_ev}]", _FPC.get(_noun), _ev)

# ROLE GATING. A stat is only safe to fast-path when its role is force-set downstream or the
# branch it lands in resolves one side. IP/CG/SHO are pitcher-only; XBH is batter-only.
for _ev in ("IP", "CG", "SHO", "W", "L", "SV", "ER"):
    check(f"[fp-stat: {_ev} is pitcher-force + routed to the pitcher branch]",
          (_ev in _PITCH_FORCE, _ev in _FPPO), (True, True))
check("[fp-stat: XBH is batter-force]", "XBH" in _BAT_FORCE, True)
# R/AB/WAR/HBP are NOT force-set — they rely on the plain batting branch resolving a POSITION
# player, so this pins that they are NOT in the pitcher-branch set (which would emit role:pit).
for _ev in ("R", "AB", "WAR", "HBP"):
    check(f"[fp-stat: {_ev} not force-set, so it must NOT be in the pitcher branch]",
          (_ev in _PITCH_FORCE, _ev in _BAT_FORCE, _ev in _FPPO), (False, False, False))

# RATE FORM — each added rate word must be reachable at the end-anchor.
for _q, _want in (("What is Wade Boggs's BABIP?", "babip"),
                  ("What is Babe Ruth's ISO?", "iso"),
                  ("What is Babe Ruth's isolated power?", "isolated power"),
                  ("What is Barry Bonds's walk rate?", "walk rate"),
                  ("What is Reggie Jackson's strikeout rate?", "strikeout rate"),
                  ("What is Randy Johnson's K/9?", "k/9"),
                  ("What is Greg Maddux's BB/9?", "bb/9"),
                  ("What is Nolan Ryan's HR/9?", "hr/9"),
                  ("What is Aaron Judge's OPS?", "ops")):
    _m = _FPRR.match(_q)
    check(f"[fp-stat: rate form reads {_want!r}]", _m and _m.group(2).strip().lower(), _want)
# 'isolated power' must win over the shorter 'iso' (longest-alternative-first ordering).
check("[fp-stat: 'isolated power' not truncated to 'iso']",
      _FPR.index("isolated power") < _FPR.index("iso"), True)
# The per-nine family routes to the PITCHER branch, not query_rates.
for _w, _ev in (("k/9", "K9"), ("bb/9", "BB9"), ("hr/9", "HR9"), ("era", "ERA"), ("whip", "WHIP")):
    check(f"[fp-stat: {_w!r} is a pitcher rate -> {_ev}]", _FPPR.get(_w), _ev)
# _canon_event folds the slash spelling the model emits onto the same event.
for _a, _b in (("K/9", "K9"), ("BB/9", "BB9"), ("HR/9", "HR9")):
    check(f"[fp-stat: _canon_event folds {_a} -> {_b}]", NS["_canon_event"](_a), _b)

# BLOCKED — these DECLINE or go out of scope, so no template may reach them.
# FIP is the sharp one: a CAREER FIP declines ('normalized to each season's league and park')
# while a single-season FIP serves, so the common phrasing would be a decline-fire.
for _w in ("fip", "woba", "ops+", "era+", "winning percentage", "percentage",
           "plate appearances", "extra bases", "sacrifice flies", "holds", "caught stealing"):
    check(f"[fp-stat: {_w!r} is NOT a fast-path rate word]",
          _w in set(NS["_FP_RATE_WORDS"]), False)
for _noun in ("plate appearances", "extra bases", "sacrifice flies", "holds",
              "caught stealing", "times", "fip", "woba"):
    check(f"[fp-stat: {_noun!r} is NOT a fast-path count noun]", _noun in _FPC, False)
for _q in ("What is Randy Johnson's FIP?", "What was Randy Johnson's FIP in 2001?",
           "What was Barry Bonds's wOBA in 2004?", "What was Barry Bonds's OPS+ in 2002?",
           "What is Barry Bonds's OPS plus?", "What was Pedro Martinez's ERA+ in 2000?",
           "What is Greg Maddux's winning percentage?"):
    check(f"[fp-stat: BLOCKED rate question does not match the template — {_q[:38]!r}]",
          _FPRR.match(_q) is None, True)
for _q in ("How many plate appearances does Barry Bonds have?",
           "How many extra bases does Hank Aaron have?",
           "How many sacrifice flies does Hank Aaron have?",
           "How many holds does Josh Hader have?"):
    _m = _FPCR.match(_q)
    check(f"[fp-stat: BLOCKED count question hits no whitelist entry — {_q[:38]!r}]",
          _m and _FPC.get(_m.group(1).strip().lower()), None)

# RUNS is the special case: addable ONLY because the verb discriminator separates
# runs-scored from runs-driven-in. Both directions must survive the whitelist entry.
_m = _FPCR.match("How many runs has Rickey Henderson scored?")
_nm, _sn = NS["_fp_name_season"](_m.group(2).strip())
check("[fp-stat: 'runs ... scored' parses to the player, event R]",
      (_FPC.get(_m.group(1).strip().lower()), _nm), ("R", "Rickey Henderson"))
_ti = {"player": "Rickey Henderson", "event": "R"}
check("[fp-stat: and the discriminator leaves that R alone]",
      (_fix_rbi("How many runs has Rickey Henderson scored?", _ti), _ti["event"]), (False, "R"))
# 'batted in' never reaches the template at all — 'in' is a stop-word in the name parse — and
# even if it did, the discriminator corrects R -> RBI before the runner sees it.
_m = _FPCR.match("How many runs has Hank Aaron batted in?")
check("[fp-stat: 'runs ... batted in' fails the name parse (stop-word 'in')]",
      NS["_fp_name_season"](_m.group(2).strip())[0], None)
_ti = {"player": "Hank Aaron", "event": "R"}
check("[fp-stat: belt and braces — discriminator still flips that R to RBI]",
      (_fix_rbi("How many runs has Hank Aaron batted in?", _ti), _ti["event"]), (True, "RBI"))
# A pitcher's runs-allowed must not reach the batting branch either.
_m = _FPCR.match("How many runs has Gerrit Cole allowed?")
check("[fp-stat: 'runs ... allowed' fails the name parse (stop-word 'allowed')]",
      NS["_fp_name_season"](_m.group(2).strip())[0], None)

# ---- report -----------------------------------------------------------------
print(f"ran {N} assertions across {len(SCOPED)} scoped + {len(ANSWERABLE)} answerable tools")
if FAILS:
    print(f"\n{len(FAILS)} FAILURES:")
    for f in FAILS[:40]:
        print("  " + f)
    sys.exit(1)
print("ALL PASS — _guard_tool matches current systems on parity; divergences are the intended fixes.")
