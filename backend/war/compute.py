"""The WAR v1 calculation: database counting stats + input files + frozen settings → per player-season WAR pieces.

Pure: it reads, it never writes. Hitters and pitchers come back as dicts keyed by (player_id, season):

  hitters:  bat, br (baserunning), dp, rep (replacement), pos (position), fld (fielding), all in runs; rpw (runs per win);
            offense = (bat + br + dp + rep) / rpw;  defense = (pos + fld) / rpw;  war = offense + defense
  pitchers: waa (wins above average), rep (replacement wins), war = waa + rep, plus the pieces behind waa
  skipped:  every season row that could not be rated, with the reason; empty: rows with no playing time, with the reason

How each piece works, and why, is in README.md. Every tuned number comes from the settings file; nothing here reads the
stored Baseball-Reference columns.

Inputs (one directory, built by builders/): posPA.pkl, bat_raw.pkl, roe.pkl, coverage.json, coverage_league.json,
lgstr.pkl, lgstr_pre1900.pkl, fld2_raw.pkl, zfld_raw.pkl, cat_raw.pkl, tz_raw.pkl, rb_raw.pkl, tder_raw.pkl, li_raw.pkl,
and for the balldontlie season bdlYY.pkl, bdlYY_runs.pkl, bdlYY_fld.pkl, bdlYY_li.pkl (YY = the season's last two digits).
"""
import csv
import json
import math
import os
import pickle
from collections import defaultdict

from war import parks as _parks
from war.common import norm_team
from war.fld_totals import features as tot_features, runs as tot_runs

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARAMS = os.path.join(HERE, 'params', 'war_params_v1.json')
DEFAULT_TEAMS = os.path.join(os.path.dirname(HERE), 'data', 'lahman', 'Teams.csv')
# Negro Leagues seasons are not covered (they are to be hidden); these league codes are skipped.
NEL = ('NN2', 'NNL', 'NAL', 'ECL', 'NSL', 'ANL', 'EWL', 'IND', 'EAS', 'WES', 'NAC', 'INT')
# Retrosheet team code → Lahman team_seasons code (the two vocabularies differ for a few franchises)
ALIAS = {'MLN': ['ML1'], 'MIL': ['ML4', 'MIL'], 'ANA': ['LAA', 'ANA', 'CAL'], 'WSN': ['WAS', 'WSN', 'MON']}
# Season rows that are copies of another player's seasons under a wrong id (a database cleanup is pending; approved
# 2026-11-17 to leave them out until then). Each is reported in `skipped` with this reason.
_COPY = 'copy of another player\'s season under a wrong id (database cleanup pending)'
EXCLUDE = {**{(817009, y): _COPY for y in (2023, 2024, 2025)},       # copies of Samad Taylor's seasons
           **{(817503, y): _COPY for y in (2022, 2024, 2025)}}       # copies of Chuckie Robinson's seasons
BDL2RETRO = {'CHC': 'CHN', 'CHW': 'CHA', 'KC': 'KCA', 'LAD': 'LAN', 'NYM': 'NYN', 'NYY': 'NYA', 'OAK': 'ATH',
             'SD': 'SDN', 'SF': 'SFN', 'STL': 'SLN', 'TB': 'TBA', 'WSH': 'WAS', 'LAA': 'ANA'}


def _first_word(name):
    w = ''.join(c if c.isalnum() else ' ' for c in (name or '').lower()).split()
    return w[0] if w else None


def compute(cur, inputs_dir, params_path=DEFAULT_PARAMS, teams_csv=DEFAULT_TEAMS, y0=1871, current=2026, through=None, features_only=False):
    """current = the season whose fielding/baserunning inputs come from balldontlie (the season in progress, or the last
    one Retrosheet has not published yet). current=None computes every season from Retrosheet only, up to `through`
    (the 'final' recompute once Retrosheet publishes a season)."""
    if current is None and through is None:
        raise ValueError('give current (a balldontlie season) or through (the last Retrosheet season)')
    y1 = current or through
    PRM = json.load(open(params_path))
    S = PRM['scalars']
    n = lambda v: float(v or 0)
    pk = lambda name: pickle.load(open(os.path.join(inputs_dir, name), 'rb'))
    tag = f'bdl{current % 100:02d}' if current else None
    SKIP = []                                  # (player_id, season, role, reason): rows that could not be rated
    EMPTY = []                                 # (player_id, season, role, reason): rows with no playing time to rate

    # ---------- park factors ----------
    A, one_year = _parks.build(cur, teams_csv, current=current or through + 1)
    B26 = pk(f'{tag}.pkl') if current else {'INN': {}, 'PBP': {}, 'VENUE': {}}

    def parkset(y, t):
        p = A.get((y, t), {}).get('parks')
        return frozenset(x for x, c in p.items() if c >= 5) if p else None

    LAHPF = {}
    for r in csv.DictReader(open(teams_csv, encoding='utf-8-sig')):
        if r['BPF'] and r['PPF']:
            LAHPF[(int(r['yearID']), norm_team(r['teamID'], r['lgID']))] = (int(r['BPF']) / 100, int(r['PPF']) / 100)
    # the current season borrows last season's factor only when the team's main ballpark is the same
    cur.execute('SELECT team_id, park_name FROM team_seasons WHERE year = %s', ((current or 0) - 1,))
    prev_park = {t: _first_word(p) for t, p in cur.fetchall()}
    same_park = {}
    for abbr, venue in (B26.get('VENUE') or {}).items():
        t = BDL2RETRO.get(abbr, abbr)
        prev = prev_park.get(t) or prev_park.get({'ANA': 'LAA'}.get(t, t))
        same_park[t] = prev is not None and prev == _first_word(venue)
    _pc = {}

    def park(y, t, i):                       # i = 0 batting factor, 1 pitching factor
        t = 'ANA' if (current and y == current and t == 'LAA') else t
        if (y, t, i) in _pc:
            return _pc[(y, t, i)]
        if current and y == current:
            ys = [current - 1, current] if same_park.get(t, True) else [current]
        else:
            ys = [yy for yy in (y - 1, y, y + 1) if yy == y or (parkset(yy, t) is not None and parkset(yy, t) == parkset(y, t))]
        vals = [(one_year(yy, t, True), A[(yy, t)]['hg']) for yy in ys if (yy, t) in A]
        vals = [(r, w) for r, w in vals if r]
        v = sum(r[i] * w for r, w in vals) / sum(w for _, w in vals) if vals else None
        if v is None:
            v = LAHPF.get((y, t), (None, None))[i]          # before our play-by-play park factors: Lahman BPF/PPF
        _pc[(y, t, i)] = v
        return v

    # ---------- database inputs (counting stats only) ----------
    cur.execute('SELECT player_id, year, team, league, CASE WHEN coalesce("PA", 0) = 0 AND "AB" > 0 THEN "AB" + coalesce("BB",0) + coalesce("HBP",0) + coalesce("SF",0) + coalesce("SH",0) ELSE "PA" END,'
                '"AB","H",doubles,triples,"HR","BB","IBB","HBP","SF","SH","SB","CS","GIDP","G","SO" '
                'FROM player_seasons WHERE year BETWEEN %s AND %s AND coalesce(league, \'\') NOT IN %s', (y0, y1, NEL))
    _hr = cur.fetchall()
    cur.execute('SELECT year, team_id, league FROM team_seasons')
    TEAMLG = {(y, t): L for y, t, L in cur.fetchall()}

    def team_league(y, t):
        for c in [t] + ALIAS.get(t, []):
            if (y, c) in TEAMLG:
                return TEAMLG[(y, c)]
        return None

    def fixlg(y, t, L):                       # the team's league wins when we know it (a few rows are missing or mis-tagged)
        return team_league(y, t) or team_league(y, norm_team(t, L)) or L

    HROWS = []                                # every season row, plate appearances or not (pinch runners, defensive subs)
    for r in _hr:
        if (r[0], r[1]) in EXCLUDE:
            SKIP.append((r[0], r[1], 'hit', EXCLUDE[(r[0], r[1])])); continue
        if not r[4] and not r[18]:                       # no games and no plate appearances: nothing happened
            EMPTY.append((r[0], r[1], 'hit', 'no games and no plate appearances recorded')); continue
        L = fixlg(r[1], r[2], r[3])
        HROWS.append((r[0], r[1], norm_team(r[2], L), L, r[4] or 0) + tuple(r[5:]))
    HTEAM0 = {(r[0], r[1]): r[2] for r in HROWS}
    HLEAGUE = {(r[0], r[1]): r[3] for r in HROWS}
    cur.execute('SELECT player_id, year, sum("G") FROM pitcher_seasons GROUP BY 1, 2')
    PG = {(a, b): c or 0 for a, b, c in cur.fetchall()}
    cur.execute('SELECT year, league, sum("G"), sum(runs_scored) FROM team_seasons GROUP BY 1, 2')
    TS = {(y, L): (gm, r) for y, L, gm, r in cur.fetchall()}
    cur.execute('SELECT player_id, year, position, sum(innings_outs), sum("G"), sum("PO"), sum("A"), sum("E") FROM player_fielding GROUP BY 1, 2, 3')
    FLDI = defaultdict(dict); FG = defaultdict(dict); TOTS = defaultdict(dict)
    for p_, y, pos, io_, gg, po, a_, e_ in cur.fetchall():
        FLDI[(p_, y)][pos] = (io_ or 0) / 3; FG[(p_, y)][pos] = gg or 0; TOTS[(p_, y)][pos] = (po or 0, a_ or 0, e_ or 0)
    cur.execute('SELECT retro_id, player_id FROM players WHERE retro_id IS NOT NULL')
    R2P = dict(cur.fetchall())
    cur.execute('SELECT retro_id, player_id FROM pitchers WHERE retro_id IS NOT NULL')
    for rid_, pid_ in cur.fetchall(): R2P.setdefault(rid_, pid_)       # pitchers with no players row keep their Retrosheet inputs
    cur.execute('SELECT player_id, year, team, coalesce("PA", "AB") FROM player_season_stints WHERE year >= 1871')
    HSTN = defaultdict(list)
    for a_, b_, t_, pa_ in cur.fetchall():
        HSTN[(a_, b_)].append((t_, float(pa_ or 0)))

    # before 1898 our stint tables are empty: team-by-team lines come from Lahman. The Lahman player is matched by his
    # Retrosheet id and confirmed against our season row (its team is one of his Lahman teams and the games agree).
    LAHDIR = os.path.dirname(teams_csv)
    l2r = {r['playerID']: r['retroID'] for r in csv.DictReader(open(os.path.join(LAHDIR, 'People.csv'), encoding='utf-8-sig')) if r['retroID']}
    cur.execute('SELECT player_id, year, team, "G" FROM player_seasons WHERE year < 1898')
    _ps0 = {(a_, b_): (t_, g_ or 0) for a_, b_, t_, g_ in cur.fetchall()}
    cur.execute('SELECT player_id, year, team, "G", "IP" FROM pitcher_seasons WHERE year < 1898')
    _pp0 = {(a_, b_): (t_, g_ or 0, n(ip_)) for a_, b_, t_, g_, ip_ in cur.fetchall()}
    LAH_ST = dict(bat_confirmed=0, bat_unconfirmed=0, pit_confirmed=0, pit_unconfirmed=0)
    i_ = lambda x: int(x) if x not in (None, '') else 0
    _lb = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(LAHDIR, 'Batting.csv'), encoding='utf-8-sig')):
        if int(r['yearID']) < 1898: _lb[(r['playerID'], int(r['yearID']))].append(r)
    for (lid, y), rs in _lb.items():
        pid_ = R2P.get(l2r.get(lid))
        if len(rs) < 2 or pid_ is None or (pid_, y) in HSTN: continue
        ours = _ps0.get((pid_, y))
        if not ours or ours[0] not in {r['teamID'] for r in rs} or abs(sum(i_(r['G']) for r in rs) - ours[1]) > 2:
            LAH_ST['bat_unconfirmed'] += 1; continue
        LAH_ST['bat_confirmed'] += 1
        for r in rs:
            HSTN[(pid_, y)].append((r['teamID'], float(sum(i_(r[c]) for c in ('AB', 'BB', 'HBP', 'SH', 'SF')))))
    _lp = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(LAHDIR, 'Pitching.csv'), encoding='utf-8-sig')):
        if int(r['yearID']) < 1898: _lp[(r['playerID'], int(r['yearID']))].append(r)
    LPST = {}                                  # (pid, y) → stint lines in pitcher_season_stints' column order
    for (lid, y), rs in _lp.items():
        pid_ = R2P.get(l2r.get(lid))
        if len(rs) < 2 or pid_ is None: continue
        ours = _pp0.get((pid_, y))
        if not ours or ours[0] not in {r['teamID'] for r in rs} or abs(sum(i_(r['IPouts']) for r in rs) / 3 - ours[2]) > 1:
            LAH_ST['pit_unconfirmed'] += 1; continue
        LAH_ST['pit_confirmed'] += 1
        LPST[(pid_, y)] = [(r['teamID'], i_(r['IPouts']) / 3, i_(r['BFP']) or None, i_(r['SO']), i_(r['BB']), i_(r['HBP']), i_(r['HR']), i_(r['H']))
                           for r in rs]

    def rpw(k):                                # runs per win from the league's scoring
        if k not in TS or not TS[k][0]:
            return None
        return 10 * math.sqrt(2 * (TS[k][1] / TS[k][0]) / 9)

    # ---------- Retrosheet / balldontlie derived inputs ----------
    POSPA = pk('posPA.pkl'); POSPA_P = defaultdict(dict)
    for (bid, s), m in POSPA.items():
        if bid in R2P:
            for f, c in m.items():
                POSPA_P[(R2P[bid], s)][f] = POSPA_P[(R2P[bid], s)].get(f, 0) + c
    BRAW = pk('bat_raw.pkl'); LWS = BRAW['LW']
    PBPPA = defaultdict(int); BRX = defaultdict(lambda: [0.0, 0.0]); DPX = defaultdict(float); ROEX = defaultdict(int)
    for s_, bid, c in BRAW['PA']:
        if bid in R2P: PBPPA[(R2P[bid], s_)] += c
    for s_, bid, xb, ob, c in BRAW['BR']:
        if bid in R2P: BRX[(R2P[bid], s_)][0] += xb; BRX[(R2P[bid], s_)][1] += ob
    for s_, bid, dd, c in BRAW['DP']:
        if bid in R2P: DPX[(R2P[bid], s_)] += dd
    for s_, bid, c in pk('roe.pkl'):
        if bid in R2P: ROEX[(R2P[bid], s_)] += c
    _lr = sum(c[2] for c in B26['PBP'].values()) / max(1, sum(c[3] for c in B26['PBP'].values()))
    for pid_, (c, roe_, g_, ch_) in B26['PBP'].items():
        PBPPA[(pid_, current)] = c; ROEX[(pid_, current)] = roe_; DPX[(pid_, current)] = g_ - _lr * ch_
    R26 = pk(f'{tag}_runs.pkl') if current else {'BRX': {}, 'DP': {}, 'ARM': {}}
    for pid_, (xb_, ob_) in R26['BRX'].items():
        BRX[(pid_, current)] = [xb_, ob_]
    LGSTR = pk('lgstr.pkl'); LGSTR.update(pk('lgstr_pre1900.pkl'))
    COV = {int(k): v for k, v in json.load(open(os.path.join(inputs_dir, 'coverage.json'))).items()}
    COVL = {int(k): v for k, v in json.load(open(os.path.join(inputs_dir, 'coverage_league.json'))).items()}
    # play-by-play plate appearances per (season, league), the league taken from each player's season row
    LGPA = defaultdict(int)
    for (pid_, s_), m in POSPA_P.items():
        L_ = HLEAGUE.get((pid_, s_))
        if L_: LGPA[(s_, L_)] += sum(m.values())
    TG = defaultdict(int)                      # team games per (season, league): AL and NL, the leagues Retrosheet covers
    for (y_, L_), (gm_, r_) in TS.items():
        if L_ in ('AL', 'NL'): TG[(y_, L_)] += gm_ or 0

    # ---------- who is a pitcher at the plate ----------
    def pit_share(pid, y, G):
        """share of his plate appearances made as the pitcher: play-by-play where it exists, games before 1910"""
        pp = POSPA_P.get((pid, y))
        if pp and sum(pp.values()):
            return pp.get(1, 0) / sum(pp.values())
        pg = PG.get((pid, y), 0)
        if y < 1910 and pg:
            return min(1.0, pg / max(1.0, n(G)))
        return 1.0 if pg >= max(1, n(G) / 2) else 0.0
    PSH = {(r[0], r[1]): pit_share(r[0], r[1], r[18]) for r in HROWS}
    is_pit = lambda pid, y: PSH.get((pid, y), 0) >= 0.5

    # ---------- league baselines ----------
    TOT = defaultdict(lambda: [0] * 14); GDP_PA = defaultdict(lambda: [0, 0]); TOTP = defaultdict(lambda: [0] * 14)
    for r in HROWS:
        v = [n(x) for x in r[4:18]]; k = (r[1], r[3])
        if is_pit(r[0], r[1]):
            TOTP[k] = [a + b for a, b in zip(TOTP[k], v)]; continue
        TOT[k] = [a + b for a, b in zip(TOT[k], v)]
        if r[17] is not None: GDP_PA[k][0] += r[17]; GDP_PA[k][1] += n(r[4])
    y_lw0, y_lw1 = min(LWS), max(LWS)

    def LW(y):
        l = LWS[min(max(y, y_lw0), y_lw1)]
        return {e: l[e] for e in ('S', 'D', 'T', 'HR', 'BB', 'HBP')}

    def lw(v, out_w, y):
        PA, AB, H, D, T, HR, BB, IBB, HBP, SF, SH, SB, CS, GIDP = v; w = LW(y)
        return (w['S'] * (H - D - T - HR) + w['D'] * D + w['T'] * T + w['HR'] * HR + w['BB'] * (BB - IBB) + S['ibb_weight'] * w['BB'] * IBB
                + w['HBP'] * HBP - out_w * (AB - H + S['sf_as_out'] * SF))

    OUTW = {k: lw(v, 0, k[0]) / (v[1] - v[2] + S['sf_as_out'] * v[9]) for k, v in TOT.items() if v[1] - v[2] > 0}
    _ra = defaultdict(lambda: [0, 0])
    for (p_, s_), c in ROEX.items(): _ra[s_][0] += c
    for (p_, s_), c in PBPPA.items(): _ra[s_][1] += c
    LGROE = {s_: a / b for s_, (a, b) in _ra.items() if b}

    def lg_mult(y, L, k_old, k_new):
        return max(0.0, 1 + (k_old if y < 1900 else k_new) * LGSTR.get((y, L), 0.0))

    def consts(y, L):
        """everything a hitter's runs depend on in his league-season, or None when the league-season can't be rated"""
        k = (y, L)
        if k not in OUTW or rpw(k) is None or not TOT[k][0]:
            return None
        R = rpw(k); gm = TS[k][0]
        mult = lg_mult(y, L, S['league_strength_k'], S.get('league_strength_k_1900', S['league_strength_k']))
        if S.get('rep_pool_leagues', 0) and L in ('AL', 'NL'):       # replacement spread per PA across both leagues
            gm_p = sum(TS[(y, l_)][0] for l_ in ('AL', 'NL') if (y, l_) in TS); pa_p = sum(TOT[(y, l_)][0] for l_ in ('AL', 'NL') if (y, l_) in TOT)
            rep = S['hit_rep_share'] * S['war_per_season_games'] / 2 * gm_p * R / pa_p * mult
        else:
            rep = S['hit_rep_share'] * S['war_per_season_games'] / 2 * gm * R / TOT[k][0] * mult
        tp = TOTP.get(k)
        return dict(outw=OUTW[k], rpa=TS[k][1] / TOT[k][0], R=R, rep=rep,
                    gdp=GDP_PA[k][0] / GDP_PA[k][1] if GDP_PA[k][1] else None,
                    brb=(S['sb_runs'] * TOT[k][11] + S['cs_runs'] * TOT[k][12]) / TOT[k][0],
                    pit=(lw(tp, OUTW[k], y) / tp[0]) if tp and tp[0] else None)

    def blended(pid, y, L):
        """league constants weighted by the plate appearances he had in each league (players traded between leagues)"""
        sts = HSTN.get((pid, y), [])
        w = defaultdict(float)
        for t_, pa_ in sts:
            l_ = team_league(y, t_)
            if l_ and pa_ > 0: w[l_] += pa_
        if len(w) < 2:
            return consts(y, L)
        cs = {l_: consts(y, l_) for l_ in w}
        if any(c is None for c in cs.values()):
            return consts(y, L)
        tw = sum(w.values()); out = {}
        for key in ('outw', 'rpa', 'R', 'rep', 'brb'):
            out[key] = sum(cs[l_][key] * w[l_] for l_ in w) / tw
        for key in ('gdp', 'pit'):
            vals = [(cs[l_][key], w[l_]) for l_ in w if cs[l_][key] is not None]
            out[key] = sum(a * b for a, b in vals) / sum(b for _, b in vals) if vals else None
        return out

    # ---------- innings by position ----------
    FC = {1: 'P', 2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'OF', 8: 'OF', 9: 'OF'}
    PN = {2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF', 10: 'DH'}

    def ipp(y, L):                             # innings per play-by-play plate appearance in his league-season
        c = COVL.get(y, {}).get(L)
        if not c or not LGPA.get((y, L)) or not TG.get((y, L)):
            return None, None
        return 81 * TG[(y, L)] * c / LGPA[(y, L)], c

    def innings(pid, y):
        f = FLDI.get((pid, y), {}); gg = FG.get((pid, y), {})
        if sum(f.values()) >= 3 * sum(gg.values()) and sum(f.values()) > 0: return dict(f)
        pp = POSPA_P.get((pid, y), {}); i_, c_ = ipp(y, HLEAGUE.get((pid, y)))
        if pp and i_:
            out = defaultdict(float)
            for code, c in pp.items():
                if code in FC: out[FC[code]] += c * i_ / c_
            if out: return dict(out)
        return {pos: gm_ * 8.5 for pos, gm_ in gg.items()}

    def splits(pid, y):
        if current and y == current:
            m = B26['INN'].get(pid)
            if m: return {PN[q]: v for q, v in m.items()}
        f = innings(pid, y); pp = POSPA_P.get((pid, y), {}); out = {}
        for pos in ('C', 'SS', '2B', '3B', '1B'):
            if f.get(pos): out[pos] = f[pos]
        of = f.get('OF', 0); s3 = [pp.get(7, 0), pp.get(8, 0), pp.get(9, 0)]
        if of:
            if sum(s3): out.update({p_: of * x / sum(s3) for p_, x in zip(('LF', 'CF', 'RF'), s3) if x})
            else: out['OF'] = of
        fpa = sum(pp.get(i, 0) for i in range(1, 10)); finn = sum(f.get(p_, 0) for p_ in ('C', 'SS', '2B', '3B', '1B', 'OF', 'P'))
        if pp.get(10): out['DH'] = pp[10] * (finn / fpa if fpa > 50 and finn else 9 / 4.2)
        if pp.get(10) and not finn:
            i_, c_ = ipp(y, HLEAGUE.get((pid, y)))
            if i_: out['DH'] = pp[10] * i_
        return out

    POS = PRM['positions']
    _eras = sorted(int(e) for e in POS['by_era'])
    PL_ALL = sorted({p for d in POS['by_era'].values() for p in d})

    _dh0 = next((POS['by_era'][str(e)].get('DH') for e in _eras if POS['by_era'][str(e)].get('DH')), 0.0)

    def era_value(e, pos):
        """an era's value; a 0.0 there means 'not fitted' (no split outfield before the 1910s, no DH before 1973), so
        LF/CF/RF fall back to the era's unsplit OF value and DH to its first fitted value"""
        d = POS['by_era'][str(e)]; v = d.get(pos, 0.0)
        if pos == 'OF': return d.get('OF') or sum(d.get(q, 0.0) for q in ('LF', 'CF', 'RF')) / 3
        if not v and pos in ('LF', 'CF', 'RF'): return d.get('OF', 0.0)
        if not v and pos == 'DH': return _dh0
        return v

    def pos_value(y, pos):
        """runs per 1,458 innings: interpolated between decade midpoints, held flat before the first and after the last"""
        ctr = [(e + POS['era_years'] / 2, era_value(e, pos)) for e in _eras]
        if y <= ctr[0][0]: return ctr[0][1]
        if y >= ctr[-1][0]: return ctr[-1][1]
        for (c0, v0), (c1, v1) in zip(ctr, ctr[1:]):
            if c0 <= y <= c1: return v0 + (v1 - v0) * (y - c0) / (c1 - c0)

    def rpos(pid, y):
        return sum(pos_value(y, pos) * inn / 1458 for pos, inn in splits(pid, y).items())

    def pf_mix(pid, y, team):
        sts = HSTN.get((pid, y), [])
        if len({t_ for t_, _ in sts}) < 2: return park(y, team, 0)
        num = den = 0.0
        for t_, pa_ in sts:
            v_ = park(y, t_, 0)
            if v_ is not None and pa_ > 0: num += v_ * pa_; den += pa_
        return num / den if den else park(y, team, 0)

    # ---------- pitching lines (season rows, split by team where the stints add up) ----------
    def bfpx(ip, bfp, h, bb, hbp):
        """batters faced, or an estimate (3 outs an inning + hits + walks + hit batsmen) when the stored count is
        impossibly low — some 1901–07 rows hold a fraction of the real figure"""
        est = 3 * n(ip) + n(h) + n(bb) + n(hbp)
        return n(bfp) if bfp and n(bfp) >= 0.95 * est else est

    def tcode(y, t, L):
        t2 = norm_team(t, L)
        return 'LAA' if (current and y == current and t2 == 'ANA') else t2    # the current season's stints say ANA

    # runs allowed: a blank R falls back to earned runs (some current-season rows carry ER only); never read as zero
    cur.execute('SELECT player_id, year, team, league, "G", "GS", "IP", coalesce("R", "ER"), "BFP", "SO", "BB", "HBP", "HR", "H" FROM pitcher_seasons '
                'WHERE year BETWEEN %s AND %s AND coalesce(league, \'\') NOT IN %s', (y0, y1, NEL))
    PSEASON = {}
    for r in cur.fetchall():
        if (r[0], r[1]) in EXCLUDE:
            SKIP.append((r[0], r[1], 'pit', EXCLUDE[(r[0], r[1])])); continue
        if not (n(r[6]) > 0 or n(r[7]) > 0):
            EMPTY.append((r[0], r[1], 'pit', 'no innings and no runs recorded')); continue
        if n(r[6]) > 0 and r[7] is None:
            SKIP.append((r[0], r[1], 'pit', 'innings but no runs or earned runs recorded')); continue
        team = r[2] or HTEAM0.get((r[0], r[1]))
        L = fixlg(r[1], team, r[3]) if team else r[3]
        if not team or not L:
            SKIP.append((r[0], r[1], 'pit', 'no team or league recorded')); continue
        PSEASON[(r[0], r[1])] = (tcode(r[1], team, L), L) + tuple(r[4:])
    cur.execute('SELECT player_id, year, team, "IP", "BFP", "SO", "BB", "HBP", "HR", "H" FROM pitcher_season_stints WHERE year BETWEEN %s AND %s', (y0, y1))
    _st = defaultdict(list)
    for r in cur.fetchall(): _st[(r[0], r[1])].append(r[2:])
    for k_, lines_ in LPST.items():
        if k_ not in _st: _st[k_] = lines_
    PLINES = {}                                   # (pid, y) → [(team, league, IP, balls in play, hits other than HR)]
    for k, (team, L, G, GS, IP, R_, BFP, SO, BB, HBP, HR, H) in PSEASON.items():
        sts = _st.get(k, [])
        if sts and abs(sum(n(x[1]) for x in sts) - n(IP)) <= 1:
            lines = []
            for t_, ip_, bf_, so_, bb_, hb_, hr_, h_ in sts:
                tc = tcode(k[1], t_, None); lt = team_league(k[1], tc) or L
                lines.append((tc, lt, n(ip_), max(0.0, bfpx(ip_, bf_, h_, bb_, hb_) - n(so_) - n(bb_) - n(hb_) - n(hr_)), n(h_) - n(hr_)))
        else:
            lines = [(team, L, n(IP), max(0.0, bfpx(IP, BFP, H, BB, HBP) - n(SO) - n(BB) - n(HBP) - n(HR)), n(H) - n(HR))]
        PLINES[k] = lines
    TD = defaultdict(lambda: [0.0, 0.0]); TLD = {}            # team balls in play and hits on them, from the lines
    for k, lines in PLINES.items():
        for tc, lt, ip_, b_, h_ in lines:
            TD[(k[1], tc)][0] += b_; TD[(k[1], tc)][1] += h_; TLD.setdefault((k[1], tc), lt)

    # ---------- fielding runs ----------
    _ts = {}

    def team_saved():
        """team hits saved on balls in play vs league, per (season, team): pitching-line totals × Retrosheet park factor
        from 1953 (a season without Retrosheet yet borrows the previous season's factor for the same park)"""
        if _ts: return _ts
        TDR = pk('tder_raw.pkl')
        td = {kk: v for kk, v in TD.items() if kk[0] >= 1910 and v[0]}; tl = TLD
        lgd = defaultdict(lambda: [0.0, 0.0])
        for kk, (b_, h_) in td.items(): lgd[(kk[0], tl[kk])][0] += b_; lgd[(kk[0], tl[kk])][1] += h_

        def pfac(kk):
            if kk[0] < 1953: return 1.0
            t_ = 'ANA' if (current and kk[0] == current and kk[1] == 'LAA') else kk[1]     # Retrosheet's code for the Angels
            borrow = not (current and kk[0] == current) or same_park.get(t_, True)      # only for the same main ballpark
            for yy in ((kk[0], kk[0] - 1) if borrow else (kk[0],)):
                v = TDR.get((yy, t_))
                if v and v[2]: return v[3] / v[2]
            return 1.0
        _ts.update({kk: b_ * lgd[(kk[0], tl[kk])][1] / lgd[(kk[0], tl[kk])][0] * pfac(kk) - h_ for kk, (b_, h_) in td.items()})
        return _ts

    def features_retro():
        raw = pk('fld2_raw.pkl'); X = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for (kind, p), rows in raw.items():
            for r in rows:
                pid = R2P.get(r[1])
                if pid is None: continue
                F = X[(pid, r[0])][p]
                if kind == 'range': F['range'] += r[3] - r[4]; F['n'] += r[5]
                elif kind == 'dp': F['dp'] += r[3] - r[4]
                elif kind == 'arm': F['xb'] += r[3] - r[4]; F['kill'] += r[5] - r[6]
                elif kind == 'catch': F['cs'] += r[3] - r[4]; F['pb'] += r[5] - r[6]; F['n'] += r[7]
        PC = {3: '1B', 4: '2B', 5: '3B', 6: 'SS'}; lg = defaultdict(lambda: [0.0, 0.0, 0.0])
        for (pid, s_), m in X.items():
            t = TOTS.get((pid, s_), {}); ofn = sum(m[q]['n'] for q in (7, 8, 9) if q in m)
            for p_, F in m.items():
                if p_ in PC and PC[p_] in t: po, a_, e_ = t[PC[p_]]; w = 1
                elif p_ in (7, 8, 9) and 'OF' in t and ofn: po, a_, e_ = t['OF']; w = F['n'] / ofn
                else: continue
                L_ = lg[(s_, p_)]; L_[0] += F['n']; L_[1] += a_ * w if p_ <= 6 else po * w; L_[2] += e_ * w
                F['_t'] = (a_ * w if p_ <= 6 else po * w, e_ * w)
        for (pid, s_), m in X.items():
            for p_, F in m.items():
                if '_t' in F and lg[(s_, p_)][0]:
                    L_ = lg[(s_, p_)]; F['tot'] = F['_t'][0] - F['n'] * L_[1] / L_[0]; F['err'] = F['_t'][1] - F['n'] * L_[2] / L_[0]
        for (s_, fid, q), (r_, e_, c) in pk('zfld_raw.pkl').items():
            pid = R2P.get(fid)
            if pid is not None and q >= 3 and q in X[(pid, s_)]: X[(pid, s_)][q]['zr'] += (r_ or 0)
        for (s_, fid), v in pk('cat_raw.pkl').items():
            pid = R2P.get(fid)
            if pid is not None and 2 in X[(pid, s_)]:
                F_ = X[(pid, s_)][2]; F_['catt'] += v[1] or 0; F_['ccs'] += v[2] or 0; F_['cwp'] += v[3] or 0; F_['cpo'] += v[5] or 0
        for s_, fid, team, q, ty, made, chn, exp, expp in pk('tz_raw.pkl'):          # Total Zone plays above average
            pid = R2P.get(fid)
            if pid is not None and q in X[(pid, s_)]: X[(pid, s_)][q]['tz'] += made - exp; X[(pid, s_)][q]['tzp'] += made - expp
        for s_, fid, team, q, made, nb, exp, expp in pk('rb_raw.pkl'):              # batter × pitcher out-distribution range
            pid = R2P.get(fid)
            if pid is not None and q in X[(pid, s_)]: X[(pid, s_)][q]['rb'] += made - exp; X[(pid, s_)][q]['rbp'] += made - expp
        saved = team_saved()                                                         # team hits saved, shared by chances
        tmn = defaultdict(float); share = defaultdict(float)
        for (kind, q), rr in raw.items():
            if kind != 'range': continue
            for s_, fid, team, made, ex, nb in rr:
                tmn[(s_, team)] += nb
                pid = R2P.get(fid)
                if pid is not None: share[(pid, s_, q, team)] += nb
        for (pid, s_, q, team), nb in share.items():
            if (s_, team) in saved and tmn[(s_, team)] and q in X[(pid, s_)]:
                X[(pid, s_)][q]['tder'] += saved[(s_, team)] * nb / tmn[(s_, team)]
        return X

    def fielding_retro():
        X = features_retro()
        eras = PRM['fld_retro']['eras']; coef = PRM['fld_retro']['coef']
        era0 = lambda s: next((i for i, (a, b) in enumerate(eras) if a <= s <= b), len(eras) - 1)
        # option B (approved 2026-11-16): seasons from 2003 use the 1989–2002 Total Zone weights — one scale for every era
        tz_from = int(PRM['fld_retro'].get('use_tz_weights_from_2003', 1))
        era = lambda s: 2 if (tz_from and s >= 2003) else era0(s)
        return {k: sum(c_ * v.get(f, 0) for p, v in m.items() for f, c_ in coef[f'{era(k[1])}|{p}'].items()) for k, m in X.items()}

    def add_tder_bdl(X):
        sv = team_saved(); tot = defaultdict(float)
        for (pid, s_), m in X.items():
            t_ = HTEAM0.get((pid, s_))
            if t_: tot[(s_, t_)] += sum(v.get('n', 0) for q, v in m.items() if q >= 3)
        for (pid, s_), m in X.items():
            t_ = HTEAM0.get((pid, s_))
            if not t_ or (s_, t_) not in sv or not tot[(s_, t_)]: continue
            for q, v in m.items():
                if q >= 3: v['tder'] = sv[(s_, t_)] * v.get('n', 0) / tot[(s_, t_)]

    def fielding_bdl():
        X = {k: {p: dict(v) for p, v in m.items()} for k, m in pk(f'{tag}_fld.pkl').items()}
        for (pid, p), v in R26['DP'].items():
            if (pid, current) in X and p in X[(pid, current)]: X[(pid, current)][p]['dp'] = v
        for (pid, p), (xb, kl) in R26['ARM'].items():
            if (pid, current) in X and p in X[(pid, current)]: X[(pid, current)][p]['xb'] = xb; X[(pid, current)][p]['kill'] = kl
        add_tder_bdl(X)
        coef = PRM['fld_bdl']['coef']
        part = {k: {p: sum(c_ * v.get(f, 0) for f, c_ in coef[str(p)].items()) for p, v in m.items()} for k, m in X.items()}
        # centre each position (per ball in play there), so no position as a whole reads above or below average
        tot = defaultdict(lambda: [0.0, 0.0])
        for k, m in part.items():
            for p, r in m.items(): tot[p][0] += r; tot[p][1] += X[k][p].get('n', 0)
        mean = {p: a / b for p, (a, b) in tot.items() if b}
        return {k: sum(r - mean.get(p, 0) * X[k][p].get('n', 0) for p, r in m.items()) for k, m in part.items()}

    if features_only == 'splits':                               # for refitting position values (calibration only)
        return {(r[0], r[1]): (splits(r[0], r[1]), rpw((r[1], r[3])), PSH[(r[0], r[1])]) for r in HROWS}
    if features_only:                                           # for refitting the fielding weights (calibration only)
        return features_retro()
    FLD = {k: v for k, v in fielding_retro().items() if k[1] < (current or through + 1)}; FLD_SRC = {k: 'play_by_play' for k in FLD}
    if current:
        for k, v in fielding_bdl().items():
            FLD[k] = v; FLD_SRC[k] = 'balldontlie'
    for k, v in tot_runs(tot_features(cur, y0, y1), PRM['fld_totals']['coef']).items():
        if k not in FLD: FLD[k] = v; FLD_SRC[k] = 'season_totals'   # season totals where play-by-play has no fielder

    # ---------- hitters ----------
    HIT = {}
    raw_h = {}
    for r in HROWS:
        pid, y, team, L = r[:4]; v = [n(x) for x in r[4:18]]; G = r[18]
        c = blended(pid, y, L); p = pf_mix(pid, y, team)
        if c is None or p is None:
            SKIP.append((pid, y, 'hit', 'no park factor' if c else f'league {L!r} {y} cannot be rated')); continue
        PA = v[0]; f = PSH[(pid, y)]
        if PA == 0 and f >= 0.5: continue                   # a pitcher who never batted: no hitting row
        park_runs = PA * c['rpa'] * (p - 1)
        rbat = lw(v, c['outw'], y) - park_runs
        pb = PBPPA.get((pid, y), 0)
        if pb and pb >= 0.5 * PA: rbat += S['roe_weight'] * (ROEX[(pid, y)] - pb * LGROE.get(y, 0)) * PA / pb
        rbr = S['sb_runs'] * v[11] + S['cs_runs'] * v[12] - c['brb'] * PA
        rdp = S['gidp_runs_season'] * (r[17] - c['gdp'] * PA) if r[17] is not None and c['gdp'] is not None else 0.0
        if pb:
            sc = PA / pb if pb >= 0.5 * PA else 0.0
            rbr += sc * (S['xb_runs'] * BRX[(pid, y)][0] + S['out_on_bases_runs'] * BRX[(pid, y)][1])
            if sc: rdp = S['gidp_runs_pbp'] * DPX[(pid, y)] * sc
        BD = PRM.get('bat_delta')
        if BD:                                                   # per-era event-value corrections; the last era runs on
            last = max(BD, key=lambda k_: int(k_[5:]))
            e_ = next((d_ for k_, d_ in BD.items() if int(k_[:4]) <= y <= int(k_[5:])), BD[last] if y > int(last[5:]) else None)
            if e_:
                PA_, AB_, H_, D_, T_, HR_, BB_, IBB_, HBP_, SF_, SH_, SB_, CS_, GIDP_ = v; SO_ = n(r[19])
                xx = dict(zip(['1B', '2B', '3B', 'HR', 'uBB', 'IBB', 'HBP', 'SO', 'OIP', 'SF', 'GIDP', 'PA'],
                              [H_ - D_ - T_ - HR_, D_, T_, HR_, BB_ - IBB_, IBB_, HBP_, SO_, AB_ - H_ - SO_, SF_, GIDP_, PA_]))
                rbat += sum(e_[k_] * xx[k_] for k_ in e_)
        rrep = c['rep'] * PA
        if f > 0 and c['pit'] is not None:                       # his PA as the pitcher: measured against hitting pitchers
            rbat_p = lw(v, c['outw'], y) - PA * c['pit'] - park_runs
            rbat = (1 - f) * rbat + f * rbat_p; rbr *= 1 - f; rdp *= 1 - f; rrep *= 1 - f
        raw_h[(pid, y)] = dict(bat=rbat, br=rbr, dp=rdp, rep=rrep, pos=rpos(pid, y), fld=FLD.get((pid, y)), R=c['R'], PA=PA, f=f,
                               L=L, park=p)
    # batting runs re-centred so they sum to zero in each league-season (an average hitter = 0): the per-era event
    # corrections, the park adjustment and reached-on-error credit shift hitters relative to each other, but must not move
    # the league as a whole. Summed over position players, shifted per plate appearance not made as the pitcher.
    bdc = defaultdict(lambda: [0.0, 0.0])
    for (pid, y), h in raw_h.items():
        if h['f'] < 0.5: bdc[(y, h['L'])][0] += h['bat']; bdc[(y, h['L'])][1] += h['PA'] * (1 - h['f'])
    for (pid, y), h in raw_h.items():
        c_ = bdc[(y, h['L'])]
        if c_[1]: h['bat'] -= c_[0] / c_[1] * h['PA'] * (1 - h['f'])
    # average fielder = 0 and position values sum to 0, within each league-season (per position-player PA)
    cen = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for (pid, y), h in raw_h.items():
        if h['f'] >= 0.5: continue
        cc = cen[(y, h['L'])]; w = h['PA'] * (1 - h['f'])
        cc[0] += h['pos']; cc[1] += w
        if h['fld'] is not None: cc[2] += h['fld']; cc[3] += w
    for (pid, y), h in raw_h.items():
        cc = cen[(y, h['L'])]; w = h['PA'] * (1 - h['f'])
        rp = h['pos'] - (cc[0] / cc[1] * w if S.get('center_positions', 0) and cc[1] else 0.0)
        rf = (h['fld'] - (cc[2] / cc[3] * w if S.get('center_fielding', 0) and cc[3] else 0.0)) if h['fld'] is not None else 0.0
        if h['f'] >= 0.5: rp = rf = 0.0                         # a pitcher at the plate: no position or fielding credit
        R = h['R']; t = h['bat'] + h['br'] + h['dp'] + h['rep']
        HIT[(pid, y)] = dict(bat=h['bat'], br=h['br'], dp=h['dp'], rep=h['rep'], pos=rp, fld=rf, rpw=R, PA=h['PA'],
                             pitcher=h['f'] >= 0.5, pit_share=h['f'], league=h['L'], park=h['park'],
                             has_fld=h['fld'] is not None, fielding_source=FLD_SRC.get((pid, y)) if h['f'] < 0.5 else None,
                             offense=t / R, defense=(rp + rf) / R, war=(t + rp + rf) / R)

    # ---------- pitchers ----------
    PROWS = [(k[0], k[1]) + v[:2] + v[2:6] + (bfpx(v[4], v[6], v[11], v[8], v[9]),) + v[7:11] for k, v in PSEASON.items()]
    lg = defaultdict(lambda: [0.0] * 6)
    for r in PROWS:
        k = (r[1], r[3]); ip, R_ = n(r[6]), n(r[7]); st = n(r[5]) / max(1, n(r[4])); L_ = lg[k]
        L_[0] += R_; L_[1] += ip; L_[2] += R_ * st; L_[3] += ip * st; L_[4] += R_ * (1 - st); L_[5] += ip * (1 - st)
    # team fielding runs behind the pitchers: each fielder's runs, split by his chances with each team
    TEAMF = defaultdict(float); share = defaultdict(lambda: defaultdict(float))
    for (kind, p), rr in pk('fld2_raw.pkl').items():
        if kind == 'range':                              # fielders: balls in play they had a chance at, per team
            for s_, fid, team, made, ex, nb in rr:
                if fid in R2P: share[(R2P[fid], s_)][team] += nb
        elif kind == 'catch':                            # catchers: plays caught, per team
            for s_, fid, team, cs_, ecs, pb_, epb, nb, att in rr:
                if fid in R2P: share[(R2P[fid], s_)][team] += nb
    for k, v in FLD.items():
        t_ = sum(share[k].values())
        for team, nb in share[k].items(): TEAMF[(k[1], team)] += v * nb / t_
        if not t_ and k in HTEAM0:
            TEAMF[(k[1], HTEAM0[k])] += v                # no Retrosheet chances: the fielder counts for his main team
    LDER = defaultdict(lambda: [0.0, 0.0])
    for kk, (b_, h_) in TD.items():
        if b_: LDER[(kk[0], TLD[kk])][0] += b_; LDER[(kk[0], TLD[kk])][1] += h_
    TEAMB = {kk: b_ for kk, (b_, h_) in TD.items() if b_}        # team balls in play, from the same lines as each pitcher's
    for kk, (b_, h_) in TD.items():
        if not b_: continue
        # before 1910 the season-totals fielders carry no team hits-saved signal, so it is added here; from 1910 the fielders'
        # own runs already include it (counting it again would charge pitchers twice)
        derw = S['team_der_weight'] if kk[0] < 1910 else 0.0
        L_ = LDER[(kk[0], TLD[kk])]
        TEAMF[kk] = S['our_team_fld_weight'] * TEAMF.get(kk, 0.0) + derw * (b_ * L_[1] / L_[0] - h_) * S['der_runs_per_hit']
    cs = defaultdict(lambda: [0.0, 0.0])                       # average defense = 0 within each league-season
    for kk in TEAMB:
        cs[(kk[0], TLD[kk])][0] += TEAMF.get(kk, 0.0); cs[(kk[0], TLD[kk])][1] += TEAMB[kk]
    for kk in TEAMB:
        c_ = cs[(kk[0], TLD[kk])]
        if c_[1]: TEAMF[kk] -= TEAMB[kk] * c_[0] / c_[1]
    RELW = S['reliever_rep_weight']; wt = defaultdict(float)
    # share of his innings pitched in relief: relief outings run about a third as long as starts (season lines carry no split)
    ris = lambda G, GS: (n(G) - n(GS)) / ((n(G) - n(GS)) + S['start_to_relief_ip_ratio'] * n(GS)) if n(G) > 0 else 0.0
    repf = lambda G, GS: 1 - ris(G, GS) + RELW * ris(G, GS)          # relief innings earn RELW of a starter's replacement
    STN = {k: v for k, v in PLINES.items() if len({x[0] for x in v}) > 1}

    def lglines(pid, y, L, IP):                                    # (league, innings) for each league he pitched in
        if (pid, y) in STN:
            out = [(lt_, ip_) for t_, lt_, ip_, b_, h_ in STN[(pid, y)] if (y, lt_) in TS]
            if out: return out
        return [(L, n(IP))]
    for r in PROWS:
        for lt_, ip_ in lglines(r[0], r[1], r[3], r[6]): wt[(r[1], lt_)] += ip_ * repf(r[4], r[5])
    PIT = {}
    for r in PROWS:
        pid, y, team, L, G, GS, IP, R_, BFP, SO, BB, HBP, HR = r
        k = (y, L); R = rpw(k); p = park(y, team, 1)
        if R is None or p is None or k not in TS or not lg[k][1]:
            SKIP.append((pid, y, 'pit', 'no park factor' if R is not None else f'league {L!r} {y} cannot be rated')); continue
        IP, R_ = n(IP), n(R_); st = n(GS) / max(1, n(G)); L_ = lg[k]
        lgra9 = 9 * L_[0] / L_[1]
        xra9 = (lgra9 + S['reliever_ra9_shift'] * (st - L_[3] / L_[1])) * p
        bip = max(0.0, n(BFP) - n(SO) - n(BB) - n(HBP) - n(HR))
        rdef = TEAMF.get((y, team), 0.0) * bip / TEAMB[(y, team)] if TEAMB.get((y, team)) else 0.0
        sts = STN.get((pid, y), [])
        if sts and IP > 0:
            xs = rd = ipw = 0.0
            for t_, lt_, ip_, b_, h_ in sts:
                p_ = park(y, t_, 1); Lk = lg.get((y, lt_))
                if p_ is None or not Lk or not Lk[1]: p_, Lk = p, L_
                xs += ip_ * (9 * Lk[0] / Lk[1] + S['reliever_ra9_shift'] * (st - Lk[3] / Lk[1])) * p_; ipw += ip_
                rd += TEAMF.get((y, t_), 0.0) * b_ / TEAMB[(y, t_)] if TEAMB.get((y, t_)) else 0.0
            if ipw > 0: xra9 = xs / ipw; rdef = rd
        if y < 1910: rdef *= S['pre1910_team_def_scale']
        raa = IP / 9 * xra9 - R_ - rdef
        rep_w = 0.0                                              # replacement, in each league he pitched in
        for lt_, ip_ in lglines(pid, y, L, IP):
            if wt[(y, lt_)]:
                rep_w += (S['pit_rep_share'] * S['war_per_season_games'] / 2 * TS[(y, lt_)][0] * ip_ * repf(G, GS) / wt[(y, lt_)]
                          * max(0.0, 1 + S.get('pit_league_strength_k', 0.0) * LGSTR.get((y, lt_), 0.0)))
        waa_lin = raa / R; waa_w = waa_lin
        model = S.get('pitcher_wins_model', 'blend')
        if IP > 0 and model == 'blend':                          # v1: blend linear and Pythagenpat wins
            ra9 = max(S['min_ra9'], xra9 - 9 * raa / IP); x = (xra9 + ra9) ** S['pythag_exponent']
            waa_w = waa_lin + S.get('pythag_blend', 1.0) * ((xra9 ** x / (xra9 ** x + ra9 ** x) - 0.5) * IP / 9 - waa_lin)
        elif IP > 0 and model == 'game_pythag':
            # each appearance as a game: his innings at his run rate, the rest at the average rate, an average
            # offense; Pythagenpat win chance in that game's own scoring level, minus .500, times appearances
            ra9 = max(S['min_ra9'], xra9 - 9 * raa / IP); ipg = min(9.0, IP / max(1.0, n(G)))
            rag = (ipg * ra9 + (9 - ipg) * xra9) / 9; x = (xra9 + rag) ** S['pythag_exponent']
            waa_w = (xra9 ** x / (xra9 ** x + rag ** x) - 0.5) * max(1.0, n(G))
        elif IP > 0 and model == 'dynamic_rpw':
            # runs per win in the scoring level of his own games: ((18 - IP/G) x league RA9 + (IP/G) x his RA9) / 18,
            # + 2, x 1.5 (the published pitcher runs-per-win formula); league RA9 is the league's, before park
            ra9 = max(S['min_ra9'], xra9 - 9 * raa / IP); ipg = min(9.0, IP / max(1.0, n(G)))
            # ra9 is his actual rate, in his parks; with dyn_rpw_park_adjust the league rate is put in the same parks
            lg_env = lgra9 * p if S.get('dyn_rpw_park_adjust', 0) else lgra9
            rpw_p = (((18 - ipg) * lg_env + ipg * ra9) / 18 + S.get('dyn_rpw_add', 2.0)) * S.get('dyn_rpw_mult', 1.5)
            waa_w = raa / rpw_p
        elif IP > 0 and model != 'blend':
            raise ValueError(f'unknown pitcher_wins_model {model!r}')     # a typo must not silently mean linear wins
        relief_ip_share = ris(G, GS)
        PIT[(pid, y)] = dict(waa=waa_w, lin=waa_lin, relief_ip_share=relief_ip_share, xra9=xra9, R_=R_, rdef=rdef, park=p, rep=rep_w, IP=IP, st=st, league=L, rpw=R)
    if S.get('center_pitcher_waa', 0):                          # an average pitcher = 0 wins above average in each league-season
        cw = defaultdict(lambda: [0.0, 0.0])
        for (pid, y), v in PIT.items():
            if v['IP'] > 0: cw[(y, v['league'])][0] += v['waa']; cw[(y, v['league'])][1] += v['IP']
        for (pid, y), v in PIT.items():
            c_ = cw[(y, v['league'])]
            if v['IP'] > 0 and c_[1]: v['waa'] -= c_[0] / c_[1] * v['IP']
    if S.get('reliever_leverage', 0):                           # relievers: wins above average × (1 + entry leverage) / 2
        GLI = {}
        for k_, v_ in pk('li_raw.pkl').items():
            if k_ != 'TABLE' and k_[0] in R2P and k_[1] < (current or through + 1) and v_[0]: GLI[(R2P[k_[0]], k_[1])] = v_[1] / v_[0]
        for pid_, (napp, sli) in (pk(f'{tag}_li.pkl').items() if current else []):
            if napp: GLI[(pid_, current)] = sli / napp
        for k_, v in PIT.items():
            g_ = GLI.get(k_)
            v['gmli'] = g_
            if g_ is not None and v['IP'] > 0:
                v['waa'] *= 1 + v['relief_ip_share'] * (g_ - 1) / 2
        if S.get('center_pitcher_waa', 0):                      # keep each league-season's pitcher WAA at zero after leverage
            cw = defaultdict(lambda: [0.0, 0.0])
            for (pid, y), v in PIT.items():
                if v['IP'] > 0: cw[(y, v['league'])][0] += v['waa']; cw[(y, v['league'])][1] += v['IP']
            for (pid, y), v in PIT.items():
                c_ = cw[(y, v['league'])]
                if v['IP'] > 0 and c_[1]: v['waa'] -= c_[0] / c_[1] * v['IP']
    for v in PIT.values():
        v['war'] = v['waa'] + v['rep']
    return dict(hit=HIT, pit=PIT, skipped=SKIP, empty=EMPTY, lahman_stints=LAH_ST, params=PRM['version'], current=current, through=y1)
