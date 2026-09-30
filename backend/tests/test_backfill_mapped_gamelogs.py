#!/usr/bin/env python3
"""`backfill_mapped_gamelogs.classify_row` — which rows a late-mapped player's
backfill may write.

⚠️ THE CHECKS THAT MATTER: the legacy MLB path owns 2026 before the 05-19
cutover under bare gamePks, so the rule is PER PLAYER AND EASTERN DATE for
every player — a game is written only if he has no legacy row that date; a
doubleheader date holding one legacy row is HELD (which half it is can't be
told from the date); a stored row is skipped.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_backfill_mapped_gamelogs.py
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import backfill_mapped_gamelogs as bm                            # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


D = datetime.date
early, late = D(2026, 4, 10), D(2026, 7, 4)
row = lambda gid, d: {"game_id": gid, "game_date": d}                   # noqa: E731


def verdict(r, pid=1, have=(), bare=(), games_on=None):
    return bm.classify_row(r, pid, have=set(have), bare=set(bare), games_on=games_on or {})


check("a new post-cutover game is written", verdict(row("5070001", late)) == "new")
check("a pre-cutover game with no legacy row that date is written", verdict(row("5060001", early)) == "new")
check("a legacy row that date: not written (Kayfus, France)",
      verdict(row("5060001", early), bare={(1, early)}) == "legacy_on_date")
check("  ...a player with legacy rows on OTHER dates still gets this one",
      verdict(row("5060002", D(2026, 4, 11)), bare={(1, early)}) == "new")
check("a doubleheader date holding one legacy row is held",
      verdict(row("5060001", early), bare={(1, early)}, games_on={(1, early): 2}) == "doubleheader_held")
check("the same rule after the cutover", verdict(row("5070001", late), bare={(1, late)}) == "legacy_on_date")
check("a stored (player, game) is skipped", verdict(row("5070001", late), have={(1, "5070001")}) == "present")
check("another player's legacy row doesn't touch this one",
      verdict(row("5060001", early), pid=2, bare={(1, early)}) == "new")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
