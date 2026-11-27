#!/usr/bin/env python3
"""Park factors before Retrosheet publishes last season (needed from Opening Day 2027).

Retrosheet's 2026 game results won't exist in 2027 (team_game_results ends with 2025), and team_seasons has no 2026 park
names. Without a fallback, every 2027 team would wait for ten home and ten road games of 2027 before getting a park
factor, so every 2027 player would go unrated for about three weeks.

⚠️ THE CHECKS THAT MATTER:
  • the game-log route (batting_gamelogs) gives the same factors as Retrosheet's results on a season both have (2025)
  • a 2027 run builds a 2026 factor for all 30 teams from our game logs, the Angels (ANA) and Athletics included
  • "same ballpark as last season" falls back to balldontlie's 2026 venues when team_seasons has no park name: an
    unchanged park borrows, a moved team doesn't; with no record at all it borrows and says so (never "moved")
  • a current-season team never ends up with no park factor (that would leave the whole team unrated and refuse the
    night): a moved team uses last season's factor until ten home and ten road games, else neutral 1.0, labelled

The two database checks need a read-only connection (SKIPPED without one); the ballpark checks need nothing.
Standalone, no pytest. Run: PGOPTIONS='-c default_transaction_read_only=on' <backend python> backend/tests/test_war_parks_fallback.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


from war import static_inputs  # noqa: E402
from war.compute import DEFAULT_TEAMS, current_park, same_parks  # noqa: E402

print('same ballpark as last season')
v26 = static_inputs.prev_venue(2027)
check('the static file has balldontlie\'s 2026 venues for all 30 teams', v26 and len(v26) == 30, v26 and len(v26))
blank = {t: None for t in ('ARI', 'ATH', 'LAA', 'CHA', 'NYA')}                     # team_seasons 2026: no park names
now = dict(v26)
check('with no record of last season\'s park, it is unknown (None), never "moved"',
      set(same_parks(now, blank).values()) == {None}, set(same_parks(now, blank).values()))
sp = same_parks(now, blank, v26)
check('with it, every team in the same park borrows last season', all(sp.values()) and len(sp) == 30,
      [t for t, ok in sp.items() if not ok])
check('Angels keyed as Retrosheet\'s ANA, Athletics as ATH', sp.get('ANA') is True and sp.get('ATH') is True,
      sorted(sp)[:5])
moved = dict(now, OAK='Las Vegas Ballpark')
check('a team in a new ballpark does not borrow', same_parks(moved, blank, v26)['ATH'] is False)
check('team_seasons\' own park name still wins when it has one',
      same_parks(now, {'ATH': 'sutter'}, {'OAK': 'Somewhere Else'})['ATH'] is True)

print('the current season\'s factor')
NOW, PREV = ((1.10, 1.08), 40), ((1.00, 0.98), 81)
v, note = current_park(0, True, NOW, PREV)
check('same park: both seasons weighted by home games', abs(v - (1.00 * 81 + 1.10 * 40) / 121) < 1e-12 and note is None, (v, note))
check('unknown park (None) borrows the same way', current_park(0, None, NOW, PREV) == (v, None))
check('moved, with its own ten-and-ten games: this season alone', current_park(1, False, NOW, PREV) == (1.08, None))
v, note = current_park(0, False, (None, 4), PREV)
check('moved, too few games yet: last season\'s factor, labelled', v == 1.00 and 'last season' in note, (v, note))
v, note = current_park(0, True, (None, 4), (None, 0))
check('no factor this season or last: neutral 1.0, labelled (never None)', v == 1.0 and 'neutral' in note, (v, note))

try:
    from war.dbconn import conn
    cur = conn().cursor()
except Exception as e:  # noqa: BLE001
    print(f'  [SKIPPED] database checks ({type(e).__name__})')
    cur = None
if cur is not None:
    from war import parks
    print('the game-log route vs Retrosheet, 2025')
    A1, f1 = parks.build(cur, DEFAULT_TEAMS, current=2026)
    A2, f2 = parks.build(cur, DEFAULT_TEAMS, current=2026, gamelog_years=[2025, 2026])
    teams = sorted(t for (y, t) in A1 if y == 2025 and A1[(y, t)]['hg'])
    pairs = [(t, f1(2025, t), f2(2025, t)) for t in teams]
    check('all 30 teams get a factor from our game logs', len(teams) == 30 and all(b for _, _, b in pairs),
          [t for t, _, b in pairs if not b])
    worst = max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for _, a, b in pairs if a and b)
    check('batting and pitching factors within 0.003 of Retrosheet\'s', worst <= 0.003, round(worst, 4))
    check('same home-game counts', all(A1[(2025, t)]['hg'] == A2[(2025, t)]['hg'] for t in teams))
    print('a 2027 run')
    A3, f3 = parks.build(cur, DEFAULT_TEAMS, current=2027)
    t26 = sorted(t for (y, t) in A3 if y == 2026 and A3[(y, t)]['hg'])
    check('2026 factors for all 30 teams from our game logs', len(t26) == 30 and all(f3(2026, t) for t in t26),
          [t for t in t26 if not f3(2026, t)])
    check('the Angels (ANA) and Athletics (ATH) among them', 'ANA' in t26 and 'ATH' in t26, t26)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
