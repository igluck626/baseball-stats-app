"""The WAR v1 calculation: database counting stats + input files + frozen settings → per player-season WAR pieces.

Pure: it reads, it never writes. Hitters and pitchers come back as dicts keyed by (player_id, season):

  hitters:  bat, br (baserunning), dp, rep (replacement), pos (position), fld (fielding), all in runs; rpw (runs per win);
            offense = (bat + br + dp + rep) / rpw;  defense = (pos + fld) / rpw;  war = offense + defense
  pitchers: waa (wins above average), rep (replacement wins), war = waa + rep

How each piece works, and why, is in README.md. Every tuned number comes from the settings file; nothing here reads the
stored Baseball-Reference columns.

Inputs (one directory, built by builders/): posPA.pkl, bat_raw.pkl, roe.pkl, coverage.json, lgstr.pkl, lgstr_pre1900.pkl,
fld2_raw.pkl, zfld_raw.pkl, cat_raw.pkl, tz_raw.pkl, rb_raw.pkl, tder_raw.pkl, and for the balldontlie season bdl26.pkl,
bdl26_runs.pkl, bdl26_fld.pkl.
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
# Retrosheet team code → Lahman team_seasons code, for the few season rows stored without a league
ALIAS = {'MLN': ['ML1'], 'MIL': ['ML4', 'MIL'], 'ANA': ['LAA', 'ANA', 'CAL'], 'WSN': ['WAS', 'WSN', 'MON']}
BDL_SEASON = 2026          # the season whose fielding/baserunning inputs come from balldontlie (bdl26*.pkl)


def compute(cur, inputs_dir, params_path=DEFAULT_PARAMS, teams_csv=DEFAULT_TEAMS, y0=1871):
    PRM = json.load(open(params_path))
    S = PRM['scalars']
    n = lambda v: float(v or 0)
    pk = lambda name: pickle.load(open(os.path.join(inputs_dir, name), 'rb'))

    # ---------- park factors ----------
    A, one_year = _parks.build(cur, teams_csv)

    def parkset(y, t):
        p = A.get((y, t), {}).get('parks')
        return frozenset(x for x, c in p.items() if c >= 5) if p else None

    LAHPF = {}
    for r in csv.DictReader(open(teams_csv, encoding='utf-8-sig')):
        if r['BPF'] and r['PPF']:
            LAHPF[(int(r['yearID']), norm_team(r['teamID'], r['lgID']))] = (int(r['BPF']) / 100, int(r['PPF']) / 100)
    _pc = {}

    def park(y, t, i):                       # i = 0 batting factor, 1 pitching factor
        t = 'ANA' if (y == BDL_SEASON and t == 'LAA') else t
        if (y, t, i) in _pc:
            return _pc[(y, t, i)]
        ys = [BDL_SEASON - 1, BDL_SEASON] if y == BDL_SEASON else \
            [yy for yy in (y - 1, y, y + 1) if yy == y or (parkset(yy, t) is not None and parkset(yy, t) == parkset(y, t))]
        vals = [(one_year(yy, t, True), A[(yy, t)]['hg']) for yy in ys if (yy, t) in A]
        vals = [(r, w) for r, w in vals if r]
        v = sum(r[i] * w for r, w in vals) / sum(w for _, w in vals) if vals else None
        if v is None:
            v = LAHPF.get((y, t), (None, None))[i]          # before our play-by-play park factors: Lahman BPF/PPF
        _pc[(y, t, i)] = v
        return v

    # ---------- database inputs (counting stats only) ----------
    cur.execute('SELECT player_id, year, team, league, "PA","AB","H",doubles,triples,"HR","BB","IBB","HBP","SF","SH","SB","CS","GIDP","G","SO" '
                'FROM player_seasons WHERE year BETWEEN %s AND %s AND "PA" > 0 AND coalesce(league, \'\') NOT IN %s', (y0, BDL_SEASON, NEL))
    _hr = cur.fetchall()
    cur.execute('SELECT year, team_id, league FROM team_seasons')
    TEAMLG = {(y, t): L for y, t, L in cur.fetchall()}

    def fixlg(y, t, L):                       # a few season rows have no league: take the team's
        if L:
            return L
        for c in [t] + ALIAS.get(t, []):
            if (y, c) in TEAMLG:
                return TEAMLG[(y, c)]
        return None

    HROWS = [(r[0], r[1], norm_team(r[2], fixlg(r[1], r[2], r[3])), fixlg(r[1], r[2], r[3])) + tuple(r[4:]) for r in _hr]
    HTEAM0 = {(r[0], r[1]): r[2] for r in HROWS}
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
    cur.execute('SELECT player_id, year, team, coalesce("PA", "AB") FROM player_season_stints WHERE year >= 1871')
    HSTN = defaultdict(list)
    for a_, b_, t_, pa_ in cur.fetchall():
        HSTN[(a_, b_)].append((t_, float(pa_ or 0)))

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
    B26 = pk('bdl26.pkl')
    _lr = sum(c[2] for c in B26['PBP'].values()) / max(1, sum(c[3] for c in B26['PBP'].values()))
    for pid_, (c, roe_, g_, ch_) in B26['PBP'].items():
        PBPPA[(pid_, BDL_SEASON)] = c; ROEX[(pid_, BDL_SEASON)] = roe_; DPX[(pid_, BDL_SEASON)] = g_ - _lr * ch_
    R26 = pk('bdl26_runs.pkl')
    for pid_, (xb_, ob_) in R26['BRX'].items():
        BRX[(pid_, BDL_SEASON)] = [xb_, ob_]
    LGSTR = pk('lgstr.pkl'); LGSTR.update(pk('lgstr_pre1900.pkl'))
    COV = {int(k): v for k, v in json.load(open(os.path.join(inputs_dir, 'coverage.json'))).items()}
    LGPA = defaultdict(int)
    for (bid, s_), m in POSPA.items():
        LGPA[s_] += sum(m.values())
    TG = defaultdict(int)
    for (y_, L_), (gm_, r_) in TS.items():
        if L_ in ('AL', 'NL', 'FL'): TG[y_] += gm_ or 0

    # ---------- league baselines (non-pitchers) ----------
    pitcher_row = lambda pid, y, G: PG.get((pid, y), 0) >= max(1, n(G) / 2)
    TOT = defaultdict(lambda: [0] * 14); GDP_PA = defaultdict(lambda: [0, 0])
    for r in HROWS:
        if pitcher_row(r[0], r[1], r[18]): continue
        v = [n(x) for x in r[4:18]]; k = (r[1], r[3]); TOT[k] = [a + b for a, b in zip(TOT[k], v)]
        if r[17] is not None: GDP_PA[k][0] += r[17]; GDP_PA[k][1] += n(r[4])

    def LW(y):
        l = LWS[min(max(y, min(LWS)), max(LWS))]
        return {e: l[e] for e in ('S', 'D', 'T', 'HR', 'BB', 'HBP')}

    def lw(v, out_w, y):
        PA, AB, H, D, T, HR, BB, IBB, HBP, SF, SH, SB, CS, GIDP = v; w = LW(y)
        return (w['S'] * (H - D - T - HR) + w['D'] * D + w['T'] * T + w['HR'] * HR + w['BB'] * (BB - IBB) + S['ibb_weight'] * w['BB'] * IBB
                + w['HBP'] * HBP - out_w * (AB - H + S['sf_as_out'] * SF))

    OUTW = {k: lw(v, 0, k[0]) / (v[1] - v[2] + S['sf_as_out'] * v[9]) for k, v in TOT.items() if v[1] - v[2] > 0}
    # pitchers' own hitting is measured against the average hitting pitcher (replacement is counted once, in pitching WAR)
    TOTP = defaultdict(lambda: [0] * 14)
    for r in HROWS:
        if pitcher_row(r[0], r[1], r[18]):
            k = (r[1], r[3]); TOTP[k] = [a + b for a, b in zip(TOTP[k], [n(x) for x in r[4:18]])]
    _ra = defaultdict(lambda: [0, 0])
    for (p_, s_), c in ROEX.items(): _ra[s_][0] += c
    for (p_, s_), c in PBPPA.items(): _ra[s_][1] += c
    LGROE = {s_: a / b for s_, (a, b) in _ra.items() if b}

    # ---------- innings by position ----------
    FC = {1: 'P', 2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'OF', 8: 'OF', 9: 'OF'}
    PN = {2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF', 10: 'DH'}

    def innings(pid, y):
        f = FLDI.get((pid, y), {}); gg = FG.get((pid, y), {})
        if sum(f.values()) >= 3 * sum(gg.values()) and sum(f.values()) > 0: return dict(f)
        pp = POSPA_P.get((pid, y), {})
        if pp and y in COV and LGPA.get(y):
            ipp = 81 * TG[y] * COV[y] / LGPA[y]; out = defaultdict(float)
            for code, c in pp.items():
                if code in FC: out[FC[code]] += c * ipp / COV[y]
            if out: return dict(out)
        return {pos: gm_ * 8.5 for pos, gm_ in gg.items()}

    def splits(pid, y):
        if y == BDL_SEASON:
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
        if pp.get(10) and not finn and y in COV and LGPA.get(y): out['DH'] = pp[10] * 81 * TG[y] * COV[y] / LGPA[y]
        return out

    POS = PRM['positions']

    def rpos(pid, y):
        e0 = min(int(e) for e in POS['by_era'])                               # before 1910: the 1910s values
        t = POS['by_era'].get(str(max(e0, y // POS['era_years'] * POS['era_years'])), {})
        return sum(t.get(pos, 0) * inn / 1458 for pos, inn in splits(pid, y).items())

    def pf_mix(pid, y, team):
        sts = HSTN.get((pid, y), [])
        if len({t_ for t_, _ in sts}) < 2: return park(y, team, 0)
        num = den = 0.0
        for t_, pa_ in sts:
            v_ = park(y, t_, 0)
            if v_ is not None and pa_ > 0: num += v_ * pa_; den += pa_
        return num / den if den else park(y, team, 0)

    # ---------- fielding runs ----------
    _ts = {}

    def team_saved():
        """team hits saved on balls in play vs league, per (season, team): pitching-line totals × Retrosheet park factor
        from 1953 (a season without Retrosheet yet borrows the previous season's factor for the same park)"""
        if _ts: return _ts
        TDR = pk('tder_raw.pkl')
        cur.execute('SELECT year, team, league, sum("BFP" - "SO" - "BB" - coalesce("HBP",0) - "HR"), sum("H" - "HR") FROM pitcher_seasons '
                    'WHERE year BETWEEN 1910 AND %s AND "BFP" IS NOT NULL GROUP BY 1,2,3', (BDL_SEASON,))
        td = defaultdict(lambda: [0.0, 0.0]); tl = {}
        for y_, t_, L_, b_, h_ in cur.fetchall():
            L_ = L_ or fixlg(y_, t_, None)
            if b_ and L_: kk = (y_, norm_team(t_, L_)); td[kk][0] += float(b_); td[kk][1] += float(h_); tl[kk] = L_
        lgd = defaultdict(lambda: [0.0, 0.0])
        for kk, (b_, h_) in td.items(): lgd[(kk[0], tl[kk])][0] += b_; lgd[(kk[0], tl[kk])][1] += h_

        def pfac(kk):
            if kk[0] < 1953: return 1.0
            for yy in (kk[0], kk[0] - 1):
                v = TDR.get((yy, kk[1]))
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
        X = {k: {p: dict(v) for p, v in m.items()} for k, m in pk('bdl26_fld.pkl').items()}
        for (pid, p), v in R26['DP'].items():
            if (pid, BDL_SEASON) in X and p in X[(pid, BDL_SEASON)]: X[(pid, BDL_SEASON)][p]['dp'] = v
        for (pid, p), (xb, kl) in R26['ARM'].items():
            if (pid, BDL_SEASON) in X and p in X[(pid, BDL_SEASON)]: X[(pid, BDL_SEASON)][p]['xb'] = xb; X[(pid, BDL_SEASON)][p]['kill'] = kl
        add_tder_bdl(X)
        coef = PRM['fld_bdl']['coef']
        return {k: sum(c_ * v.get(f, 0) for p, v in m.items() for f, c_ in coef[str(p)].items()) for k, m in X.items()}

    FLD = {k: v for k, v in fielding_retro().items() if k[1] < BDL_SEASON}; FLD_SRC_TOT = set()
    FLD.update(fielding_bdl())
    for k, v in tot_runs(tot_features(cur, y0, BDL_SEASON), PRM['fld_totals']['coef']).items():
        if k not in FLD: FLD[k] = v; FLD_SRC_TOT.add(k)        # season totals where play-by-play has no fielder

    # ---------- hitters ----------
    HIT = {}; HTEAM = {(r[0], r[1]): r[2] for r in HROWS}
    FCEN = {}
    if S.get('center_fielding', 0):                              # average fielder = 0 each season
        _a = defaultdict(lambda: [0.0, 0.0])
        for r in HROWS:
            if (r[0], r[1]) in FLD and not pitcher_row(r[0], r[1], r[18]): _a[r[1]][0] += FLD[(r[0], r[1])]; _a[r[1]][1] += n(r[4])
        FCEN = {y_: a_ / b_ for y_, (a_, b_) in _a.items() if b_}

    def lg_mult(y, L, k_old, k_new):
        return max(0.0, 1 + (k_old if y < 1900 else k_new) * LGSTR.get((y, L), 0.0))

    for r in HROWS:
        pid, y, team, L = r[:4]; v = [n(x) for x in r[4:18]]; G = r[18]
        k = (y, L); p = pf_mix(pid, y, team); R = rpw(k)
        if k not in OUTW or p is None or R is None: continue
        PA = v[0]; gm, runs = TS[k]
        rbat = lw(v, OUTW[k], y) - PA * (runs / TOT[k][0]) * (p - 1)
        pb = PBPPA.get((pid, y), 0)
        if pb and pb >= 0.5 * PA: rbat += S['roe_weight'] * (ROEX[(pid, y)] - pb * LGROE.get(y, 0)) * PA / pb
        sbw, csw = S['sb_runs'], S['cs_runs']
        rbr = sbw * v[11] + csw * v[12] - (sbw * TOT[k][11] + csw * TOT[k][12]) / TOT[k][0] * PA
        rdp = S['gidp_runs_season'] * (r[17] - GDP_PA[k][0] / GDP_PA[k][1] * PA) if r[17] is not None and GDP_PA[k][1] else 0.0
        if pb:
            sc = PA / pb if pb >= 0.5 * PA else 0.0
            rbr += sc * (S['xb_runs'] * BRX[(pid, y)][0] + S['out_on_bases_runs'] * BRX[(pid, y)][1])
            rdp = S['gidp_runs_pbp'] * DPX[(pid, y)] * sc
        mult = lg_mult(y, L, S['league_strength_k'], S.get('league_strength_k_1900', S['league_strength_k']))
        if S.get('rep_pool_leagues', 0) and L in ('AL', 'NL'):   # replacement spread per PA across both leagues
            gm_p = sum(TS[(y, l_)][0] for l_ in ('AL', 'NL') if (y, l_) in TS); pa_p = sum(TOT[(y, l_)][0] for l_ in ('AL', 'NL') if (y, l_) in TOT)
            rrep = S['hit_rep_share'] * S['war_per_season_games'] / 2 * gm_p * R / pa_p * PA * mult
        else:
            rrep = S['hit_rep_share'] * S['war_per_season_games'] / 2 * gm * R / TOT[k][0] * PA * mult
        BD = PRM.get('bat_delta')
        if BD:                                                   # per-era event-value corrections
            e_ = next((d_ for k_, d_ in BD.items() if int(k_[:4]) <= y <= int(k_[5:])), None)
            if e_:
                PA_, AB_, H_, D_, T_, HR_, BB_, IBB_, HBP_, SF_, SH_, SB_, CS_, GIDP_ = v; SO_ = n(r[19])
                xx = dict(zip(['1B', '2B', '3B', 'HR', 'uBB', 'IBB', 'HBP', 'SO', 'OIP', 'SF', 'GIDP', 'PA'],
                              [H_ - D_ - T_ - HR_, D_, T_, HR_, BB_ - IBB_, IBB_, HBP_, SO_, AB_ - H_ - SO_, SF_, GIDP_, PA_]))
                rbat += sum(e_[k_] * xx[k_] for k_ in e_)
        rp = rpos(pid, y); rf = FLD.get((pid, y), 0.0) - (FCEN.get(y, 0.0) * PA if (pid, y) in FLD else 0.0)
        if pitcher_row(pid, y, G):
            tp = TOTP[k]
            rbat = lw(v, OUTW[k], y) - PA * lw(tp, OUTW[k], y) / tp[0] - PA * (runs / TOT[k][0]) * (p - 1)
            rrep = rp = rf = 0.0; rbr = rdp = 0.0
        HIT[(pid, y)] = dict(bat=rbat, br=rbr, dp=rdp, rep=rrep, pos=rp, fld=rf, rpw=R, PA=PA, pitcher=pitcher_row(pid, y, G),
                             has_fld=(pid, y) in FLD, fld_totals=(pid, y) in FLD_SRC_TOT, league=L,
                             offense=(rbat + rbr + rdp + rrep) / R, defense=(rp + rf) / R, war=(rbat + rbr + rdp + rrep + rp + rf) / R)

    # ---------- pitchers ----------
    cur.execute('SELECT player_id, year, team, league, "G", "GS", "IP", "R", "BFP", "SO", "BB", "HBP", "HR" FROM pitcher_seasons '
                'WHERE year BETWEEN %s AND %s AND "IP" > 0 AND coalesce(league, \'\') NOT IN %s', (y0, BDL_SEASON, NEL))
    PROWS = [(r[0], r[1], norm_team(r[2], fixlg(r[1], r[2], r[3])), fixlg(r[1], r[2], r[3])) + tuple(r[4:8])
             + ((r[8] if r[8] is not None else 4.25 * float(r[6] or 0)),) + tuple(r[9:]) for r in cur.fetchall()]
    lg = defaultdict(lambda: [0.0] * 6)
    for r in PROWS:
        k = (r[1], r[3]); ip, R_ = n(r[6]), n(r[7]); st = n(r[5]) / max(1, n(r[4])); L_ = lg[k]
        L_[0] += R_; L_[1] += ip; L_[2] += R_ * st; L_[3] += ip * st; L_[4] += R_ * (1 - st); L_[5] += ip * (1 - st)
    TEAMF = defaultdict(float); TEAMB = defaultdict(float); share = defaultdict(lambda: defaultdict(float))
    raw = pk('fld2_raw.pkl')
    for (kind, p), rr in raw.items():
        if kind == 'range' and p == 3:
            for s_, fid, team, made, ex, nb in rr: TEAMB[(s_, team)] += nb
    for (kind, p), rr in raw.items():
        if kind == 'range':
            for s_, fid, team, made, ex, nb in rr:
                if fid in R2P: share[(R2P[fid], s_)][team] += nb
    for k, v in FLD.items():
        if k[1] >= BDL_SEASON: continue             # the balldontlie season: team defense from hits on balls in play only
        t_ = sum(share[k].values())
        for team, nb in share[k].items(): TEAMF[(k[1], team)] += v * nb / t_
        if not t_ and k in FLD_SRC_TOT and k in HTEAM:
            TEAMF[(k[1], HTEAM[k])] += v                # no play-by-play: the season-totals fielder counts for his main team
    cur.execute('SELECT year, team, league, sum("BFP" - "SO" - "BB" - coalesce("HBP",0) - "HR"), sum("H" - "HR") FROM pitcher_seasons '
                'WHERE year BETWEEN %s AND %s AND "BFP" IS NOT NULL GROUP BY 1,2,3', (y0, BDL_SEASON))
    TD = defaultdict(lambda: [0.0, 0.0]); TLD = {}; LDER = defaultdict(lambda: [0.0, 0.0])
    for y_, t_, L_, b_, h_ in cur.fetchall():
        if b_ and L_: kk = (y_, norm_team(t_, L_)); TD[kk][0] += float(b_); TD[kk][1] += float(h_); TLD[kk] = L_
    for kk, (b_, h_) in TD.items():
        if b_: LDER[(kk[0], TLD[kk])][0] += b_; LDER[(kk[0], TLD[kk])][1] += h_
    cur.execute('SELECT year, team, league FROM pitcher_seasons GROUP BY 1,2,3')
    TL = {(a, norm_team(b, c)): c for a, b, c in cur.fetchall() if c}
    for kk, (b_, h_) in TD.items():
        if not b_: continue
        L_ = LDER[(kk[0], TL[kk])]
        derw = S['team_der_weight'] if kk[0] < BDL_SEASON else S.get('team_der_weight_no_fielding', S['team_der_weight'])
        TEAMF[kk] = S['our_team_fld_weight'] * TEAMF.get(kk, 0.0) + derw * (b_ * L_[1] / L_[0] - h_) * S['der_runs_per_hit']
        if not TEAMB.get(kk): TEAMB[kk] = b_
    cs = defaultdict(lambda: [0.0, 0.0])                       # average defense = 0 within each league-season
    for kk in TD:
        if kk in TL and TEAMB.get(kk): cs[(kk[0], TL[kk])][0] += TEAMF.get(kk, 0.0); cs[(kk[0], TL[kk])][1] += TEAMB[kk]
    for kk in TD:
        if kk in TL and TEAMB.get(kk) and cs[(kk[0], TL[kk])][1]: TEAMF[kk] -= TEAMB[kk] * cs[(kk[0], TL[kk])][0] / cs[(kk[0], TL[kk])][1]
    RELW = S['reliever_rep_weight']; wt = defaultdict(float)
    for r in PROWS:
        st = n(r[5]) / max(1, n(r[4])); wt[(r[1], r[3])] += n(r[6]) * (st + RELW * (1 - st))
    cur.execute('SELECT player_id, year, team, "IP", "BFP", "SO", "BB", "HBP", "HR" FROM pitcher_season_stints WHERE year BETWEEN %s AND %s', (y0, BDL_SEASON))
    STN = defaultdict(list)
    for r_ in cur.fetchall():
        STN[(r_[0], r_[1])].append((norm_team(r_[2], None) if r_[2] and len(r_[2]) <= 3 else r_[2],) + tuple(r_[3:]))
    TLG = dict(TEAMLG)
    PIT = {}
    for r in PROWS:
        pid, y, team, L, G, GS, IP, R_, BFP, SO, BB, HBP, HR = r
        k = (y, L); R = rpw(k); p = park(y, team, 1)
        if R is None or p is None or k not in TS: continue
        IP, R_ = n(IP), n(R_); st = n(GS) / max(1, n(G)); L_ = lg[k]
        lgra9 = 9 * L_[0] / L_[1]
        xra9 = (lgra9 + S['reliever_ra9_shift'] * (st - L_[3] / L_[1])) * p
        bip = max(0.0, n(BFP) - n(SO) - n(BB) - n(HBP) - n(HR))
        rdef = TEAMF.get((y, team), 0.0) * bip / TEAMB[(y, team)] if TEAMB.get((y, team)) else 0.0
        sts = STN.get((pid, y), [])
        if len({x[0] for x in sts}) > 1 and IP > 0:
            xs = rd = ipw = 0.0
            for t_, ip_, bf_, so_, bb_, hb_, hr_ in sts:
                ip_ = n(ip_); p_ = park(y, t_, 1); Lk = lg.get((y, TLG.get((y, t_), L)))
                if p_ is None or not Lk or not Lk[1]: p_, Lk = p, L_
                xs += ip_ * (9 * Lk[0] / Lk[1] + S['reliever_ra9_shift'] * (st - Lk[3] / Lk[1])) * p_; ipw += ip_
                b_ = max(0.0, n(bf_) - n(so_) - n(bb_) - n(hb_) - n(hr_))
                rd += TEAMF.get((y, t_), 0.0) * b_ / TEAMB[(y, t_)] if TEAMB.get((y, t_)) else 0.0
            if ipw > 0: xra9 = xs / ipw; rdef = rd
        if y < 1910: rdef *= S.get('pre1910_team_def_scale', 1.0)
        raa = IP / 9 * xra9 - R_ - rdef
        rep_w = S['pit_rep_share'] * S['war_per_season_games'] / 2 * TS[k][0] * IP * (st + RELW * (1 - st)) / wt[k]
        rep_w *= max(0.0, 1 + S.get('pit_league_strength_k', 0.0) * LGSTR.get((y, L), 0.0))
        waa_w = raa / R
        if IP > 0:                                               # blend linear and Pythagenpat wins
            ra9 = max(S['min_ra9'], xra9 - 9 * raa / IP); x = (xra9 + ra9) ** S['pythag_exponent']
            waa_w = raa / R + S.get('pythag_blend', 1.0) * ((xra9 ** x / (xra9 ** x + ra9 ** x) - 0.5) * IP / 9 - raa / R)
        PIT[(pid, y)] = dict(waa=waa_w, lin=raa / R, xra9=xra9, R_=R_, rep=rep_w, war=waa_w + rep_w, IP=IP, st=st, league=L)
    return dict(hit=HIT, pit=PIT, params=PRM['version'])
