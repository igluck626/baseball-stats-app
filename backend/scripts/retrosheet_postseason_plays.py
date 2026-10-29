"""Readable play-by-play for every Retrosheet postseason game, in balldontlie's shape and phrasing.

WHY THIS EXISTS. balldontlie's plays start in 2002 and skip the 2010-2014
postseasons outright, so most past postseason box scores had no plays. Retrosheet
has real play-by-play for every postseason game since 1903 (Isaac, 2026-10-29:
"we should definitely have play by play for every postseason game").

Source: the local Retrosheet event files (*WS/*LCS/*D?/*W?.EVE, never the
All-Star file), decoded by Chadwick's `cwevent` and `cwsub`, which must be on
PATH. Player names come from the event files' own start/sub lines.

Output: per game, rows shaped like balldontlie's /plays rows ("Start Inning",
"Start Batter/Pitcher", "Play Result", "End Batter/Pitcher") with the running
score AFTER each play, so the app's Plays section renders them unchanged.

CHECKED (2026-10-29): all 1,878 stored games translate, every game's last play
reaches its stored final, no play falls back to raw code; on 44 postseason
games 2015-2025 the scoring plays match balldontlie's in 43 (the 44th splits
one rally into two plays) and name the same batter in 267 of 267.

Known differences from balldontlie: no hit distances, no accents (Retrosheet's
spelling), and early games often record no hit direction ("Sheckard singled").

Run locally, once a year after the World Series (see
docs/annual_retrosheet_runbook.md); it is not run on the server, which has no
Chadwick. `python3 retrosheet_postseason_plays.py YEAR [GAME_ID]` prints a game.
"""
import csv, glob, io, os, re, subprocess, sys

SEASONS = os.environ.get('RETRO_EVENTS_DIR', os.path.expanduser('~/retrosheet-phase4/repo/seasons'))
F = {0: 'gid', 2: 'inn', 3: 'home_bat', 4: 'outs', 7: 'pitches', 8: 'away', 9: 'home', 10: 'bat', 14: 'pit',
     18: 'f2', 19: 'f3', 20: 'f4', 21: 'f5', 22: 'f6', 23: 'f7', 24: 'f8', 25: 'f9',
     26: 'r1', 27: 'r2', 28: 'r3', 29: 'tx', 34: 'cd', 35: 'bat_ev', 38: 'sh', 39: 'sf', 40: 'ev_outs', 41: 'dp',
     42: 'tp', 44: 'wp', 45: 'pb', 46: 'fld', 47: 'bb', 48: 'bunt', 49: 'foul', 50: 'loc', 51: 'errs',
     52: 'e1', 53: 'e1t', 58: 'bdest', 59: 'd1', 60: 'd2', 61: 'd3', 66: 'sb1', 67: 'sb2', 68: 'sb3',
     69: 'cs1', 70: 'cs2', 71: 'cs3', 72: 'po1', 73: 'po2', 74: 'po3', 96: 'ev'}
POS = {1: 'pitcher', 2: 'catcher', 3: 'first', 4: 'second', 5: 'third', 6: 'shortstop', 7: 'left', 8: 'center', 9: 'right'}
POS_FIELDER = {1: 'pitcher', 2: 'catcher', 3: 'first baseman', 4: 'second baseman', 5: 'third baseman',
               6: 'shortstop', 7: 'left fielder', 8: 'center fielder', 9: 'right fielder'}
SUB_POS = {1: 'pitching', 2: 'catching', 3: 'at first base', 4: 'at second base', 5: 'at third base', 6: 'at shortstop',
           7: 'in left field', 8: 'in center field', 9: 'in right field', 10: 'as designated hitter'}
BASE = {1: 'first', 2: 'second', 3: 'third', 4: 'home'}
ORD = lambda n: f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _csv(cmd, cwd):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).stdout
    return list(csv.DictReader(io.StringIO(out)))


def _names(path):
    """Player id -> full name, per game, from the event file's own start/sub lines."""
    names, gid = {}, None
    for line in open(path, encoding='latin-1'):
        if line.startswith('id,'): gid = line[3:].strip(); names[gid] = {}
        elif line.startswith(('start,', 'sub,')):
            p = next(csv.reader([line]))
            names[gid][p[1]] = p[2]
    return names


def _short(full_by_id):
    """Display names as balldontlie writes them: the surname, or an initial when two men in the game share it."""
    last = {i: n.split(' ', 1)[1] if ' ' in n else n for i, n in full_by_id.items()}
    count = {}
    for v in last.values(): count[v] = count.get(v, 0) + 1
    return {i: (f"{full_by_id[i][0]}. {v}" if count[v] > 1 else v) for i, v in last.items()}


def _loc(loc, fld):
    """'78XD' -> 'deep left center'; an infield spot or none -> the fielder's position."""
    digits = re.match(r'\d+', loc or '')
    d = digits.group(0) if digits else ''
    words = {'7': 'left', '8': 'center', '9': 'right', '78': 'left center', '89': 'right center',
             '1': 'pitcher', '2': 'catcher', '3': 'first', '4': 'second', '5': 'third', '6': 'shortstop',
             '34': 'second', '56': 'shortstop', '15': 'third', '13': 'first', '25': 'third', '23': 'first'}.get(d)
    if not words:
        return POS.get(int(fld), '') if fld and fld.isdigit() and int(fld) else ''
    rest = (loc or '')[len(d):]
    if d in ('7', '8', '9', '78', '89'):
        if 'D' in rest: words = 'deep ' + words
        elif 'S' in rest: words = 'shallow ' + words
    return words


def _chain(tx):
    """'463(1)/GDP' -> 'second to shortstop to first'."""
    head = re.sub(r'\(.*?\)', '', tx.split('/')[0].split('.')[0])
    ds = [int(c) for c in head if c.isdigit()]
    return ' to '.join(POS[d] for d in ds if d in POS)


def _batter_phrase(e, nm, fielders):
    cd, tx, bb, fld = int(e['cd']), e['tx'], e['bb'], e['fld']
    to = lambda: (' to ' + _loc(e['loc'], fld)) if _loc(e['loc'], fld) else ''
    if cd == 3:
        last = (e['pitches'] or '').replace('>', '').rstrip('.123+*')[-1:]
        return 'struck out swinging' if last in ('S', 'M', 'Q', 'W') else ('struck out looking' if last == 'C' else 'struck out')
    if cd == 14: return 'walked'
    if cd == 15: return 'intentionally walked'
    if cd == 16: return 'hit by pitch'
    if cd == 17: return "reached on catcher's interference"
    if cd in (20, 21, 22, 23):
        # who fielded the hit decides infield or outfield; the location only refines the direction.
        # S9/G3 went past first into right, S8/F6MD was a bloop the center fielder picked up.
        m = re.match(r'(?:HR|[SDTH])(\d)', tx)
        by = int(m.group(1)) if m else (int(fld) if fld.isdigit() else 0)
        where = _loc(e['loc'], '')
        if by >= 7 and where in POS.values() and where in ('pitcher', 'catcher', 'first', 'second', 'third', 'shortstop'):
            where = POS[by]
        if not where and by: where = POS[by]
        to = lambda: (' to ' + where) if where else ''
    if cd == 20:
        if e['bunt'] == 'T': return 'reached on bunt single' + to()
        if 1 <= by <= 6: return 'reached on infield single' + to()
        return 'singled' + to()
    if cd == 21:
        return ('hit a ground rule double' + to()) if tx.startswith('DGR') else ('doubled' + to())
    if cd == 22: return 'tripled' + to()
    if cd == 23: return 'homered' + to()
    if cd == 18:
        pos = int(e['e1']) if e['e1'].isdigit() else 0
        who = fielders.get(pos)
        kind = 'throwing error' if e['e1t'] == 'T' else 'error'
        return f"safe at first on {kind} by {POS_FIELDER.get(pos, 'fielder')}" + (f" {who}" if who else '')
    if cd == 19: return "grounded into fielder's choice" + to()
    if cd == 2:
        if e['tp'] == 'T': return 'hit into triple play, ' + _chain(tx)
        if e['dp'] == 'T':
            if bb in ('L', 'F', 'P'):        # caught in the air: name the catcher, the runner clause says who was doubled off
                head = re.sub(r'\(.*?\)', '', tx.split('/')[0].split('.')[0])
                first = next((int(ch) for ch in head if ch.isdigit()), 0)
                verb = {'L': 'lined', 'F': 'flied', 'P': 'popped'}[bb]
                return f"{verb} into double play" + (f" to {POS[first]}" if first in POS else '')
            return 'grounded into double play, ' + _chain(tx)
        if e['sf'] == 'T': return 'hit sacrifice fly' + to()
        if e['sh'] == 'T': return 'sacrificed' + to()
        if 1 <= int(e['bdest'] or 0) <= 3:      # the batter is safe and a runner is out: a fielder's choice
            return "grounded into fielder's choice" + to() if bb in ('G', '') else "reached on fielder's choice" + to()
        if e['foul'] == 'T' and bb in ('F', 'P', ''): return 'fouled out' + to()
        verb = {'G': 'grounded out', 'F': 'flied out', 'L': 'lined out', 'P': 'popped out'}.get(bb)
        if not verb:      # the older files often record no ball type: infer it from who made the play
            head = re.sub(r'\(.*?\)', '', tx.split('/')[0].split('.')[0])
            ds = [c for c in head if c.isdigit()]
            verb = 'grounded out' if len(ds) > 1 else ('flied out' if ds and ds[0] in '789' else 'popped out')
            if len(ds) > 1: return f"{verb} to {POS[int(ds[0])]}"
        return verb + to()
    return None


def _runner_clauses(e, nm, skip_batter_dest=False):
    out, scored = [], []
    for b in (3, 2, 1):
        rid = e[f'r{b}']
        if not rid: continue
        d = int(e[f'd{b}'] or 0)
        if e[f'sb{b}'] == 'T' or e[f'cs{b}'] == 'T': continue
        if d >= 4: scored.append(f"{nm(rid)} scored")
        elif d > b: out.append(f"{nm(rid)} to {BASE[d]}")
        elif d == 0 and int(e['ev_outs'] or 0) and e[f'po{b}'] != 'T' and int(e['cd']) not in (3,) or (d == 0 and int(e['cd']) == 3 and int(e['ev_outs'] or 0) > 1):
            out.append(f"{nm(rid)} out at {BASE[b + 1]}")
    bd = int(e['bdest'] or 0)
    if not skip_batter_dest and e['bat_ev'] == 'T' and int(e['cd']) in (20, 21, 22, 14, 15, 16, 18, 19) :
        natural = {20: 1, 21: 2, 22: 3}.get(int(e['cd']), 1)
        if 1 <= bd <= 3 and bd > natural: out.append(f"{nm(e['bat'])} to {BASE[bd]}")
    s = scored[0] if len(scored) == 1 else (', '.join(scored[:-1]) + ' and ' + scored[-1] if scored else '')
    return ([s] if s else []) + out


def _runner_event(e, nm, pitcher_nm, catcher_nm):
    """Steals, caught stealing, pickoffs, wild pitches, passed balls, balks: one sentence per runner."""
    cd, parts = int(e['cd']), []
    for b in (3, 2, 1):
        rid = e[f'r{b}']
        if not rid: continue
        d = int(e[f'd{b}'] or 0)
        if e[f'sb{b}'] == 'T': parts.append(f"{nm(rid)} stole {BASE[min(b + 1, 4)]}"); continue
        if e[f'cs{b}'] == 'T' and d == 0: parts.append(f"{nm(rid)} caught stealing {BASE[min(b + 1, 4)]}"); continue
        if e[f'po{b}'] == 'T' and d == 0: parts.append(f"{nm(rid)} picked off {BASE[b]}"); continue
        if d == 0 or d == b: continue
        how = {9: f"wild pitch by {pitcher_nm}", 10: f"passed ball by {catcher_nm}", 11: f"balk by {pitcher_nm}",
               5: "defensive indifference", 4: "the throw", 6: "the throw", 8: "the throw"}.get(cd, "the throw")
        parts.append(f"{nm(rid)} scored on {how}" if d >= 4 else f"{nm(rid)} to {BASE[d]} on {how}")
    return ', '.join(parts) if parts else None


def games(year, only=None):
    d = os.path.join(SEASONS, str(year))
    files = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(d, f'{year}*.EVE'))) if not p.upper().endswith('AS.EVE')]
    fields = ','.join(str(k) for k in F)
    result = {}
    for f in files:
        names = _names(os.path.join(d, f))
        evs = _csv(['cwevent', '-q', '-n', '-y', str(year), '-f', fields, f], d)
        subs = _csv(['cwsub', '-q', '-n', '-y', str(year), f], d)
        subs_after = {}
        for s in subs: subs_after.setdefault((s['GAME_ID'], int(s['EVENT_ID'])), []).append(s)
        cols = list(F.values())
        by_game = {}
        for r in evs:
            row = dict(zip(cols, r.values()))
            by_game.setdefault(row['gid'], []).append(row)
        for gid, rows in by_game.items():
            if only and gid != only: continue
            full = names.get(gid, {})
            short = _short(full)
            nm = lambda i: short.get(i, i)
            out, order, half, pa_open, held = [], 0, None, None, []
            # balldontlie's batter_id is a number, and the app reads an at-bat's own result as the row carrying
            # one; Retrosheet's ids are text, so each man gets a number for this game, in order of appearance.
            bat_num = {}
            num = lambda rid: bat_num.setdefault(rid, len(bat_num) + 1) if rid else None
            def emit(typ, text, e, away, home, scoring=False, value=None, outs=None, batter=None):
                nonlocal order
                order += 1
                out.append({'order': order, 'type': typ, 'text': text, 'inning': int(e['inn']),
                            'inning_type': 'Bottom' if e['home_bat'] == '1' else 'Top',
                            'away_score': away, 'home_score': home, 'scoring_play': scoring,
                            'score_value': value, 'outs': outs, 'batter_id': num(batter)})
            for e in rows:
                away, home = int(e['away']), int(e['home'])
                h = (e['inn'], e['home_bat'])
                if h != half:
                    if pa_open: emit('End Batter/Pitcher', '', pa_open, away, home); pa_open = None
                    emit('Start Inning', f"{'Bottom' if e['home_bat'] == '1' else 'Top'} of the {ORD(int(e['inn']))} inning", e, away, home)
                    half = h
                    for text in held:          # changes made between innings open the next half
                        emit('Play Result', text, e, away, home)
                    held = []
                runs = sum(1 for k in ('bdest', 'd1', 'd2', 'd3') if (e[k] or '0').isdigit() and int(e[k]) >= 4
                           and (k == 'bdest' or e['r' + k[1]]))
                na, nh = (away, home + runs) if e['home_bat'] == '1' else (away + runs, home)
                outs_after = int(e['outs']) + int(e['ev_outs'] or 0)
                fielders = {p: nm(e[f'f{p}']) for p in range(2, 10) if e.get(f'f{p}')}
                fielders[1] = nm(e['pit'])
                if e['bat_ev'] == 'T':
                    if not pa_open or pa_open['bat'] != e['bat']:
                        if pa_open: emit('End Batter/Pitcher', '', pa_open, away, home)
                        emit('Start Batter/Pitcher', f"{full.get(e['pit'], e['pit'])} pitches to {full.get(e['bat'], e['bat'])}", e, away, home, batter=e['bat'])
                    phrase = _batter_phrase(e, nm, fielders) or e['tx']
                    if int(e['bdest'] or 0) >= 4 and int(e['cd']) != 23:   # the batter came all the way round
                        pos = int(e['e1']) if e['e1'].isdigit() else 0
                        phrase += (f" and scored on error by {POS_FIELDER.get(pos, 'fielder')} {fielders.get(pos, '')}".rstrip()
                                   if int(e['errs'] or 0) and pos else ' and scored')
                    text = f"{nm(e['bat'])} {phrase}"
                    clauses = _runner_clauses(e, nm)
                    if clauses: text += ', ' + ', '.join(clauses)
                    emit('Play Result', text + '.', e, na, nh, runs > 0, runs or None, outs_after, e['bat'])
                    emit('End Batter/Pitcher', '', e, na, nh)
                    pa_open = None
                else:
                    if not pa_open or pa_open['bat'] != e['bat']:
                        emit('Start Batter/Pitcher', f"{full.get(e['pit'], e['pit'])} pitches to {full.get(e['bat'], e['bat'])}", e, away, home, batter=e['bat'])
                        pa_open = e
                    text = _runner_event(e, nm, nm(e['pit']), fielders.get(2, 'catcher'))
                    if text:
                        emit('Play Result', text + '.', e, na, nh, runs > 0, runs or None, outs_after if int(e['ev_outs'] or 0) else None)
                for s in subs_after.get((gid, int(e['ev'])), []):
                    who, gone, pos, gpos = nm(s['SUB_ID']), nm(s['REMOVED_ID']), int(s['SUB_FLD_CD']), int(s['REMOVED_FLD_CD'] or 0)
                    if not s['REMOVED_ID']: text = f"{who} {SUB_POS.get(pos, 'in the game')}"
                    elif pos == 11: text = f"{who} hit for {gone}"
                    elif pos == 12: text = f"{who} ran for {gone}"
                    elif pos == 1 and gpos == 1: text = f"{who} relieved {gone}"
                    else: text = f"{who} {SUB_POS.get(pos, 'in the game')}" + (f", replacing {gone}" if gone and gone != who else '')
                    if outs_after >= 3: held.append(text)
                    else: emit('Play Result', text, e, na, nh)
            if pa_open: emit('End Batter/Pitcher', '', pa_open, na, nh)
            result['retro-' + gid] = out
    return result


if __name__ == '__main__':
    y = int(sys.argv[1]); g = sys.argv[2] if len(sys.argv) > 2 else None
    res = games(y, g)
    if g:
        for r in res.get('retro-' + g, []):
            if r['type'] in ('Play Result', 'Start Inning'):
                print(f"{r['inning_type'][:3]} {r['inning']:2} {'S' if r['scoring_play'] else ' '} {r['away_score']}-{r['home_score']} | {r['text']}")
    else:
        print(len(res), 'games', sum(len(v) for v in res.values()), 'rows')
