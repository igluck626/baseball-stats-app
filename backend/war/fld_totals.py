# fld_totals.py — fielding from season totals only (Lahman-style PO/A/E/DP by position), for seasons with no
# play-by-play: 1871–1909, the Federal League, and any later fielder Retrosheet does not cover.
# Features per (player, season, position group): each count minus the league rate per game at that position × his games.
#   C: A, E, DP     1B: A, E, DP     2B/3B/SS: A, E, DP     OF: PO, A, E, DP
# Weights (runs per unit) are fitted once in freeze_params.py and frozen in war_params_v1.json['fld_totals'].
from collections import defaultdict
GROUP = {'C': 'C', '1B': '1B', '2B': 'MI', 'SS': 'MI', '3B': '3B', 'OF': 'OF', 'LF': 'OF', 'CF': 'OF', 'RF': 'OF'}
FEATS = {'C': ['A', 'E', 'DP'], '1B': ['A', 'E', 'DP'], 'MI': ['A', 'E', 'DP'], '3B': ['A', 'E', 'DP'], 'OF': ['PO', 'A', 'E', 'DP']}
def features(cur, y0, y1):
    """X[(pid, y)][group] = {feature: count − league expectation, 'g': games}; league = the team's league that season."""
    cur.execute('SELECT year, team_id, league FROM team_seasons WHERE year BETWEEN %s AND %s', (y0, y1))
    TL = {(y, t): L for y, t, L in cur.fetchall()}
    cur.execute('SELECT player_id, year, team, position, sum("G"), sum("PO"), sum("A"), sum("E"), sum("DP") FROM player_fielding '
                'WHERE year BETWEEN %s AND %s GROUP BY 1, 2, 3, 4', (y0, y1))
    rows = []
    for pid, y, t, pos, g, po, a, e, dp in cur.fetchall():
        grp = GROUP.get(pos)
        if grp and g: rows.append((pid, y, TL.get((y, t)), grp, pos, float(g), dict(PO=float(po or 0), A=float(a or 0), E=float(e or 0), DP=float(dp or 0))))
    # 'OF' and 'LF/CF/RF' can both appear for the same season; prefer the split rows only when no 'OF' row exists
    has_of = {(r[0], r[1]) for r in rows if r[4] == 'OF'}
    rows = [r for r in rows if not (r[3] == 'OF' and r[4] != 'OF' and (r[0], r[1]) in has_of)]
    lg = defaultdict(lambda: defaultdict(float))
    for pid, y, L, grp, pos, g, c in rows:
        d = lg[(y, L, grp)]; d['g'] += g
        for f in FEATS[grp]: d[f] += c[f]
    X = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for pid, y, L, grp, pos, g, c in rows:
        d = lg[(y, L, grp)]; F = X[(pid, y)][grp]; F['g'] += g
        for f in FEATS[grp]: F[f] += c[f] - g * d[f] / d['g']
    return X
def runs(X, coef):
    return {k: sum(coef[grp].get(f, 0.0) * v.get(f, 0.0) for grp, v in m.items() for f in FEATS[grp]) for k, m in X.items()}
