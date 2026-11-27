#!/usr/bin/env python3
"""The season's first nights, replayed from 2026's real balldontlie data: the nightly must write from Opening Day.

Each replayed night keeps only the finals played by then (2026's own store, cut by Eastern date), and runs the nightly's
build, compute and checks. A refused night before any row exists would show "—" for every active player's career, so:

⚠️ THE CHECKS THAT MATTER:
  • no build or compute step crashes on a handful of games (one game, no steal attempts, ...)
  • no download-side check refuses any of these nights (2026's first nights had twelve Opening Day games with no
    lineups from balldontlie: a plain percentage limit refused every night until April 25)

The database's season totals are today's (end of 2026), not the replayed night's: a player who hadn't played yet still
carries his whole 2026 playing time. So the checks that use those totals (game logs, rated playing time, and the
fielding and leverage shares, which weigh players by it) are left out here and covered by test_war_provisional.py;
fielding is checked here instead with each player's plate appearances in the replayed games (as a real night sees it).
Needs this Mac's store (~/baseballstats-tools/data/war_store) and a read-only database connection; SKIPPED without.
Standalone, no pytest. Run: PGOPTIONS='-c default_transaction_read_only=on' <backend python> backend/tests/test_war_season_start_replay.py
"""
import datetime as dt
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results = []
STORE = os.path.expanduser('~/baseballstats-tools/data/war_store')


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


if not os.path.isdir(os.path.join(STORE, 'bdl2026')):
    print('  [SKIPPED] no local store')
    sys.exit(0)
from war import bdl_store as bs, provisional as pv, static_inputs  # noqa: E402
from war.compute import compute  # noqa: E402
from war.dbconn import conn  # noqa: E402

SRC = bs.season_dir(STORE, 2026)
GAMES = json.load(open(os.path.join(SRC, 'games.json')))
DB_SIDE = ('game logs', 'not rated', 'download ends', 'disagree', 'fielding covers', 'leverage')   # end-of-season totals
cur = conn().cursor()


def replay(label, keep, prev_venue=None):
    """Run the nightly's build, compute and checks on the regular finals in keep; None if a step crashed."""
    root = tempfile.mkdtemp(prefix='replay_')
    sdir = bs.season_dir(root, 2026)
    os.makedirs(os.path.join(sdir, 'games'))
    json.dump(keep, open(os.path.join(sdir, 'games.json'), 'w'))
    for g in keep:
        if bs.is_regular_final(g):
            os.symlink(os.path.join(SRC, 'games', f"{g['id']}.json.gz"), os.path.join(sdir, 'games', f"{g['id']}.json.gz"))
    try:
        inputs, diag = pv.build_inputs(root, 2026, cur)
        res = compute(cur, None, y0=2026, current=2026, inputs=inputs,
                      prev_venue=prev_venue or static_inputs.prev_venue(2026))
        rows = pv.to_rows(res, 2026, res['params'], 'p' * 64, 'replay', '2026-01-01T00:00:00')
        ref, warn, m = pv.check(res, rows, diag, cur, 2026, {}, False, None)
    except Exception as e:  # noqa: BLE001
        check(f'{label}: builds and computes', False, f'{type(e).__name__}: {e}')
        return None
    store_side = [r for r in ref if not any(w in r for w in DB_SIDE)]
    pbp = inputs['bdl26.pkl']['PBP']                                  # {player: [PA in the replayed games, ...]}
    pos = [(pbp.get(p, [0])[0], v['fielding_source']) for (p, y), v in res['hit'].items() if y == 2026 and not v['pitcher']]
    tot = sum(n for n, _ in pos)
    fld = sum(n for n, src in pos if src == 'balldontlie') / max(1, tot)
    return diag, m, store_side, fld, warn, rows


for night in ('2026-03-26', '2026-03-27', '2026-03-28', '2026-03-31', '2026-04-07', '2026-04-14', '2026-04-24', '2026-05-15'):
    day = dt.date.fromisoformat(night)
    out = replay(night, [g for g in GAMES if not bs.is_regular_final(g) or bs.eastern_day(g) <= day])
    if out is None:
        continue
    diag, m, store_side, fld, warn, rows = out
    expect = m['pa_share_in_span_games']
    check(f"{night}: {diag['stored']} games ({diag['no_lineups_or_pa']} without lineups/PA), fielding {fld:.0%} of "
          f"tonight's position-player PA vs {expect:.0%} of PA in games with positions: no refusal",
          not store_side and fld >= pv.FLD_RATIO * expect, (store_side, round(fld, 3), round(expect, 3)))

# an opening night of one game between two teams of the same league (2024 and 2025 opened with two NL teams abroad):
# the other league has no games at all yet
finals = [g for g in GAMES if bs.is_regular_final(g)]
for lg in ('National', 'American'):
    g1 = next(g for g in finals if g['home_team']['league'] == lg == g['away_team']['league'])
    out = replay(f'one {lg} League game', [g1])
    if out is not None:
        diag, m, store_side, fld, warn, rows = out
        check(f"a lone {lg} League opener ({g1['away_team']['abbreviation']} at {g1['home_team']['abbreviation']}, "
              f"{bs.eastern_day(g1)}): builds, computes, {len(rows)} rows, no refusal", not store_side and rows,
              (store_side, len(rows)))

# a team in a different ballpark than last season is reported all season with both names (it may be a new name only;
# then someone adds it to last season's venues and it borrows again). 2026 has one real case: the Rays, back at
# Tropicana Field after 2025 at Steinbrenner Field
out = replay('a moved team', [g for g in GAMES if not bs.is_regular_final(g) or bs.eastern_day(g) <= dt.date(2026, 4, 24)])
if out is not None:
    moved = [w for w in out[4] if 'different ballpark' in w]
    check('the Rays\' return to Tropicana is the one moved team, named with both parks',
          len(moved) == 1 and 'TBA' in moved[0] and 'Steinbrenner' in moved[0] and 'Tropicana' in moved[0], moved)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
