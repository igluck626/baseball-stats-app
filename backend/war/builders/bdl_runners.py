# bdl26_runs.py — READ-ONLY. Follows baserunners by name through the BDL 2026 /plays text, for three things Retrosheet gives
# older seasons and BDL's plate-appearance feed does not:
#   BR   hitters' baserunning on singles/doubles: each runner on 1st/2nd, his destination (0 = out on the bases)
#   ARM  outfield arms: the same runners on singles/doubles hit to an outfielder, charged to that outfielder
#   DP   double plays: ground-ball chances (runner on 1st, < 2 outs, first fielded by an infielder) and who took part
# Definitions mirror fld2_build.py (arm, dp) and bat_build.py (BR) so weights fitted on Retrosheet seasons carry over.
# A half-inning whose text can't be followed consistently (a name not on base, two runners on one base) is dropped from
# that point on; the share dropped is printed.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))   # backend/, for `war.`
INPUTS = _os.environ.get('WAR_INPUTS', _os.path.expanduser('~/baseballstats-tools/scripts/war'))         # where input files go
RETRO = _os.environ.get('RETRO_DUCKDB', _os.path.expanduser('~/retrosheet-phase4/plays_v3.duckdb'))      # Retrosheet play-by-play
BDL_DIR = _os.environ.get('WAR_BDL_DIR', _os.path.expanduser('~/baseballstats-tools/data/bdl2026'))      # balldontlie season download
LAHMAN = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), 'data', 'lahman')
import json, os, re, sys, pickle, collections, unicodedata
fold = lambda t: ''.join(c for c in unicodedata.normalize('NFKD', t) if not unicodedata.combining(c))
OUT = BDL_DIR; W = INPUTS
B = pickle.load(open(os.path.join(W, 'bdl26.pkl'), 'rb')); SP = B['SPANS']
from war.dbconn import conn
cur = conn().cursor(); cur.execute('SELECT bdl_id, player_id FROM players WHERE bdl_id IS NOT NULL')
B2P = collections.defaultdict(list)
for b, p in cur.fetchall(): B2P[b].append(p)
pid_of = lambda b: B2P[b][0] if len(B2P.get(b, [])) == 1 else None
G = {g['id'] for g in json.load(open(os.path.join(OUT, 'games.json'))) if g.get('season_type') == 'regular' and g.get('status') == 'STATUS_FINAL'}
NAMES = collections.defaultdict(dict)                                  # game -> bdl id -> (team id, set of name forms)
for line in open(os.path.join(OUT, 'stats.jsonl')):
    j = json.loads(line)
    for r in j['data']:
        p = r['player']; f, l = (p.get('first_name') or ''), (p.get('last_name') or '')
        NAMES[j['game_id']][p['id']] = (r['team']['id'] if isinstance(r.get('team'), dict) else None,
                                        {fold(q) for q in (l, f'{f[:1]}. {l}', p.get('full_name') or '', f'{f} {l}')} - {'', '. '})
teams_of = collections.defaultdict(list)
for (gid, t) in SP: teams_of[gid].append(t)
FP = {'C': 2, '1B': 3, '2B': 4, '3B': 5, 'SS': 6, 'LF': 7, 'CF': 8, 'RF': 9}
WORD = {'pitcher': 1, 'catcher': 2, 'first': 3, 'second': 4, 'third': 5, 'shortstop': 6, 'left': 7, 'center': 8, 'right': 9}
BASE = {'first': 1, 'second': 2, 'third': 3, 'home': 4}
def fielder(gid, fteam, pos, inn):
    c = [(s, b) for b, p, s, e in SP.get((gid, fteam), []) if FP.get(p) == pos and s <= inn]
    return max(c)[1] if c else None
RUN = re.compile(r'^(?P<n>.+?) (?P<v>scored|to (?:first|second|third|home)|out at (?:first|second|third|home)|safe at (?:first|second|third|home)'
                 r'|stole (?:second|third|home)|caught stealing (?:second|third|home)|picked off(?: and caught stealing)?(?: at)?(?: first| second| third)?|thrown out at (?:first|second|third|home))')
def dest(v):
    if v == 'scored': return 4
    if v.startswith(('out at', 'caught stealing', 'picked off', 'thrown out')): return 0
    return BASE[v.split()[-1]]
BR, ARM, DP = [], [], []
stat = collections.Counter()
for line in open(os.path.join(OUT, 'plays.jsonl')):
    j = json.loads(line); gid = j['game_id']
    if gid not in G or len(teams_of.get(gid, [])) != 2: continue
    nm = NAMES.get(gid, {}); last_bat = {}
    plays = sorted((x for x in j['data'] if x['type'] in ('Play Result', 'Start Inning')), key=lambda x: x['order'])
    bases, ok, outs, bteam = {}, True, 0, None
    for x in plays:
        if x['type'] == 'Start Inning':
            bases, ok, outs, bteam = {}, True, 0, None; stat['halves'] += 1; continue
        txt = fold((x.get('text') or '').strip().rstrip('.')); b = x.get('batter_id')
        if bteam is None and b is not None:
            bteam = nm.get(b, (None,))[0]
            if x['inning'] >= 10 and bteam in last_bat: bases[2] = last_bat[bteam]      # extra-inning runner on second
        if not ok or not txt: continue
        clauses = [c.strip() for part in txt.split(', ') for c in part.split(' and ')]
        first = clauses[0]; bname = nm.get(b, (None, set()))[1]
        batter_clause = b is not None and any(first.startswith(n + ' ') for n in bname)
        pre = dict(bases); o0 = outs
        moves = []
        for c in (clauses[1:] if batter_clause else clauses):
            m = RUN.match(c.removeprefix('and '))
            if not m: continue
            n = m.group('n')
            if n.split()[0] in WORD: continue                                                # fielding chain, not a runner
            who = [k for k, v in bases.items() if n in nm.get(v, (None, set()))[1]]
            if b is not None and n in bname and not who: continue                          # batter's own later move
            if not who:                                                                    # pinch runner / unlisted way on base
                cand = [q for q, (t_, ns_) in nm.items() if t_ == bteam and n in ns_]
                if len(cand) == 1:
                    d_ = dest(m.group('v')); named = set()
                    for c2 in (clauses[1:] if batter_clause else clauses):
                        m2 = RUN.match(c2.removeprefix('and '))
                        if m2: named |= {k for k, v in bases.items() if m2.group('n') in nm.get(v, (None, set()))[1]}
                    free = [q for q in (1, 2, 3) if q not in named and q not in [w for w, _ in moves]]
                    vv = m.group('v'); want = BASE[vv.split()[-1]] - 1 if vv.startswith(('out at', 'thrown out', 'caught stealing', 'to ')) else None
                    orig = want if want in free else (max(free) if free and want is None else None)
                    if orig:
                        bases[orig] = cand[0]; pre[orig] = cand[0]; who = [orig]; stat['inferred runners'] += 1
            if len(who) != 1: ok = False; stat['dropped halves'] += 1; break
            moves.append((who[0], dest(m.group('v'))))
        if not ok: continue
        if batter_clause:
            v = first[len(next(n for n in bname if first.startswith(n + ' '))) + 1:]
            cd = 'S' if re.match(r'(singled|reached on infield single|bunt singled)', v) else 'D' if re.match(r'(doubled|ground-rule double)', v) else None
            hitpos = next((WORD[w] for w in re.findall(r' to (left|center|right|pitcher|catcher|first|second|third|shortstop)', v)[:1]), None)
            fteam = next((t for t in teams_of[gid] if t != bteam), None)
            if cd and fteam is not None:
                for base_ in (1, 2):
                    if base_ not in pre: continue
                    dd = next((d_ for w_, d_ in moves if w_ == base_), None)
                    if dd is None: dd = 3 if cd == 'D' else (base_ + 1 if (base_ == 1 or 1 in pre) else base_)   # runner not mentioned: forced along (a double clears 1st and 2nd)
                    adv, wo = (0 if dd == 0 else min(dd, 4) - base_), int(dd == 0)
                    BR.append((pid_of(pre[base_]), cd, base_, o0, adv, wo))
                    if hitpos in (7, 8, 9):
                        f_ = fielder(gid, fteam, hitpos, x['inning'])
                        x_ = int(dd >= 3) if (cd == 'S' and base_ == 1) else int(dd == 4)
                        if not (cd == 'D' and base_ == 2): ARM.append((pid_of(f_) if f_ else None, hitpos, cd, base_, o0, x_, wo))
            if 1 in pre and o0 < 2 and v.startswith('grounded') and fteam is not None:
                chain = [WORD[w] for w in re.findall(r'\b(pitcher|catcher|first|second|third|shortstop)\b', ', '.join(clauses[1:2]) if 'double play' in v else '')]
                f0 = next((WORD[w] for w in re.findall(r'\b(pitcher|first|second|third|shortstop)\b', v + ' ' + ' '.join(clauses[1:2]))[:1]), None)
                if f0 in (1, 3, 4, 5, 6):
                    stat['dp chances'] += 1; stat['dp turned'] += 'double play' in v
                    for p_ in (3, 4, 5, 6):
                        f_ = fielder(gid, fteam, p_, x['inning'])
                        if f_: DP.append((pid_of(f_), p_, int('double play' in v and p_ in chain)))
        # apply moves (runners first, lead runner first), then place the batter
        new = dict(bases)
        for w_, d_ in sorted(moves, key=lambda t: -t[0]):
            r_ = new.pop(w_, None)
            if d_ in (1, 2, 3) and r_ is not None: new[d_] = r_
        if batter_clause:
            v = first
            to = 4 if 'homered' in v else 3 if 'tripled' in v else 2 if 'doubled' in v or 'ground-rule double' in v else \
                 1 if re.search(r'singled|walked|hit by pitch|reached|safe at first|fielder\'s choice|single', v) else None
            bm = [d_ for c in clauses[1:] for m in [RUN.match(c)] if m and m.group('n') in bname for d_ in [dest(m.group('v'))]]
            if bm: to = bm[-1]
            if 'double play' in v or 'out' in v.split(' on ')[0] and not re.search(r'reached|safe', v): to = to if bm else None
            if to in (1, 2, 3):
                if to in new: ok = False; stat['dropped halves'] += 1; continue
                new[to] = b
            last_bat[bteam] = b
        bases = new; outs = x.get('outs') or outs
print(dict(stat), 'BR runners', len(BR), 'ARM chances', len(ARM), 'DP chance-fielder rows', len(DP))
# league expectations (2026) and per-player totals in the Retrosheet-feature form
ea = collections.defaultdict(lambda: [0.0, 0, 0.0, 0])
for p, cd, b_, o, adv, wo in BR: e = ea[(cd, b_, o)]; e[2] += wo; e[3] += 1; e[0] += adv if not wo else 0; e[1] += 0 if wo else 1
BRX = collections.defaultdict(lambda: [0.0, 0.0])
for p, cd, b_, o, adv, wo in BR:
    if p is None: continue
    e = ea[(cd, b_, o)]; BRX[p][0] += (adv - e[0] / e[1]) if not wo and e[1] else 0; BRX[p][1] += wo - e[2] / e[3]
ra = collections.defaultdict(lambda: [0, 0, 0])
for f, pos, cd, b_, o, x_, k in ARM: r = ra[(cd, b_, o)]; r[0] += x_; r[1] += k; r[2] += 1
AX = collections.defaultdict(lambda: [0.0, 0.0])
for f, pos, cd, b_, o, x_, k in ARM:
    if f is None: continue
    r = ra[(cd, b_, o)]; AX[(f, pos)][0] += x_ - r[0] / r[2]; AX[(f, pos)][1] += k - r[1] / r[2]
dr = {q: sum(i for _, p_, i in DP if p_ == q) / max(1, sum(1 for _, p_, _i in DP if p_ == q)) for q in (3, 4, 5, 6)}; DX = collections.defaultdict(float)
for f, p_, inv in DP:
    if f is not None: DX[(f, p_)] += inv - dr[p_]
pickle.dump(dict(BRX=dict(BRX), ARM=dict(AX), DP=dict(DX)), open(os.path.join(W, 'bdl26_runs.pkl'), 'wb'))
print('runners with BR', len(BRX), 'OF with arm', len(AX), 'IF with DP', len(DX))
