"""Batting and pitching park factors from our own game results (the Total Baseball method: separate batting and pitching
factors, inning-pitched correction, other-parks corrector, own-pitchers iteration, intraleague games only).

Ported unchanged in arithmetic from the tools script that produced our OPS+/ERA+ park factors (plus/dry3.py), so WAR and
OPS+/ERA+ use the same parks. Seasons up to 2025 come from team_game_results (Retrosheet game logs); the current season is
rebuilt from batting_gamelogs, and so is the season before it while Retrosheet hasn't published that one yet (in 2027,
2026 comes from our game logs, so the current season can borrow last season's factor from Opening Day instead of
waiting for ten home and ten road games). gamelog_years forces the game-log route for given seasons (a check of it on
a season Retrosheet has, tests/test_war_parks_fallback.py).

build(cur, teams_csv) -> (A, one_year)
  A[(year, team)]      per team-season home/road aggregates, including 'parks' (park id → home games) and 'hg'
  one_year(y, t)       (batting factor, pitching factor) for that single season, or None
"""
import csv
from collections import Counter, defaultdict

BDL2RETRO = {'CHC': 'CHN', 'CHW': 'CHA', 'KC': 'KCA', 'LAD': 'LAN', 'NYM': 'NYN', 'NYY': 'NYA', 'OAK': 'ATH',
             'SD': 'SDN', 'SF': 'SFN', 'STL': 'SLN', 'TB': 'TBA', 'WSH': 'WAS', 'LAA': 'ANA'}


def build(cur, teams_csv, y0=1898, current=2026, gamelog_years=None):
    def q(sql, *a):
        cur.execute(sql, a)
        return cur.fetchall()

    bat = q('SELECT player_id, year, team, league FROM player_seasons WHERE year BETWEEN %s AND %s', y0, current)
    games = q('SELECT t.season, t.home_team, t.away_team, t.home_score, t.away_score, g.park_id FROM team_game_results t '
              'LEFT JOIN retro_game_info g ON g.game_id = t.game_id '
              'WHERE t.played AND t.forfeit IS NULL AND t.home_score IS NOT NULL AND t.season BETWEEN %s AND %s',
              y0 - 1, current - 1)
    have = {g[0] for g in games}
    if gamelog_years is None:
        gamelog_years = [current] + ([current - 1] if current - 1 not in have else [])
    else:
        games = [g for g in games if g[0] not in gamelog_years]
    tsc = {}
    for yy in gamelog_years:
        glc = q('SELECT game_id, home_away, opponent, team_score, opp_score FROM batting_gamelogs WHERE season = %s '
                'AND team_score IS NOT NULL GROUP BY 1, 2, 3, 4, 5', yy)
        tsc[yy] = q('SELECT team_id, league FROM team_seasons WHERE year = %s', yy)
        # games from the game logs: an away side's opponent is the home team and vice versa
        sides = defaultdict(dict)
        for gid, ha, opp, ts, os_ in glc:
            sides[gid][ha] = (BDL2RETRO.get(opp, opp), ts, os_)
        for gid, s in sides.items():
            if 'H' in s and 'A' in s:
                games.append((yy, s['A'][0], s['H'][0], s['H'][1], s['H'][2], None))

    team_lg = {}
    for r in csv.DictReader(open(teams_csv, encoding='utf-8-sig')):
        y = int(r['yearID'])
        if y0 - 1 <= y <= current - 1:
            team_lg[(y, r['teamIDretro'])] = r['lgID']
    for yy, rows in tsc.items():
        for t, L in rows:
            team_lg[(yy, 'ANA' if t == 'LAA' else t)] = L
    votes = defaultdict(Counter)
    for r in bat:
        if r[2] and r[3]:
            votes[(r[1], r[2])][r[3]] += 1
    for k, v in votes.items():
        team_lg.setdefault(k, v.most_common(1)[0][0])
    NT = defaultdict(int, {(y, L): 0 for (y, t), L in team_lg.items()})
    for (y, t), L in team_lg.items():
        NT[(y, L)] += 1

    A = defaultdict(lambda: dict(hg=0, hrs=0, hra=0, hw=0, rg=0, rrs=0, rra=0, rl=0, parks=Counter()))
    LG = defaultdict(lambda: [0, 0])
    for y, h, a, hs, as_, park in games:
        Lh, La = team_lg.get((y, h)), team_lg.get((y, a))
        if Lh is None or Lh != La:
            continue
        H = A[(y, h)]
        R = A[(y, a)]
        H['hg'] += 1; H['hrs'] += hs; H['hra'] += as_; H['hw'] += hs > as_
        if park:
            H['parks'][park] += 1
        R['rg'] += 1; R['rrs'] += as_; R['rra'] += hs; R['rl'] += hs > as_
        LG[(y, Lh)][0] += hs + as_; LG[(y, Lh)][1] += 1

    def one_year(y, t, ipc_on=True):
        a = A.get((y, t)); L = team_lg.get((y, t))
        if not a or a['hg'] < 10 or a['rg'] < 10 or not L or not LG[(y, L)][1]:
            return None
        n = NT[(y, L)] or 8
        raw = ((a['hrs'] + a['hra']) / a['hg']) / ((a['rrs'] + a['rra']) / a['rg'])
        if ipc_on:
            hw, rl = a['hw'] / a['hg'], a['rl'] / a['rg']
            raw /= (18.5 - hw) / (18.5 - rl)
        sf = raw * n / (n - 1 + raw)
        sf1 = 1 - (sf - 1) / (n - 1)
        RHT, RAT = a['hrs'] / a['hg'], a['rrs'] / a['rg']
        OHT, OAT = a['hra'] / a['hg'], a['rra'] / a['rg']
        RAL = LG[(y, L)][0] / LG[(y, L)][1]
        tpr = 1.0
        for _ in range(4):
            tbr = (RAT / sf1 + RHT / sf) * (1 + (tpr - 1) / (n - 1)) / RAL
            tpr = (OAT / sf1 + OHT / sf) * (1 + (tbr - 1) / (n - 1)) / RAL
        bpf = (sf + sf1) / (2 * (1 + (tpr - 1) / (n - 1)))
        ppf = (sf + sf1) / (2 * (1 + (tbr - 1) / (n - 1)))
        return bpf, ppf

    return A, one_year
