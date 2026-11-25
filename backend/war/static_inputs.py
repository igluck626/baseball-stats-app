"""The small, readable static inputs the nightly current-season WAR needs (war/static/current_season.json).

compute(y0=S, current=S) reads only these pieces of the Retrosheet-derived input files (the rest concern earlier seasons,
so the nightly passes them empty; tests/test_war_static_inputs.py checks the result is identical to a run on the full
files):
  lw        run values of each event (bat_raw.pkl's LW) for the last Retrosheet season: a later season uses that year's
  tder      team hits-saved park factors (tder_raw.pkl) for the last Retrosheet season: the season after it borrows them
            for a team in the same main ballpark; two or more seasons after it, the factor is 1.0 (no park adjustment
            to team defense) until Retrosheet publishes
  li_table  the leverage index of each game state (li_raw.pkl's TABLE), for reliever entry leverage
  lgstr     league strength (lgstr.pkl) for seasons after the last Retrosheet season (computed from our database); a
            season not in the file counts as 0, meaning no AL/NL adjustment (for 2026 that moves any player by at most
            0.005 WAR)

Rebuild the file (after Retrosheet publishes a season and the input files are rebuilt):
    <backend python> -m war.static_inputs export --inputs ~/baseballstats-tools/scripts/war
"""
import argparse
import hashlib
import json
import os
import pickle

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, 'static', 'current_season.json')
SOURCES = ('bat_raw.pkl', 'tder_raw.pkl', 'li_raw.pkl', 'lgstr.pkl')


def export(inputs_dir, path=PATH):
    pk = lambda n: pickle.load(open(os.path.join(inputs_dir, n), 'rb'))
    lw = pk('bat_raw.pkl')['LW']
    last = max(lw)
    tder = {k: v for k, v in pk('tder_raw.pkl').items() if k[0] == last}
    if max(k[0] for k in pk('tder_raw.pkl')) != last:
        raise ValueError('bat_raw and tder_raw end in different seasons')
    doc = {
        '_about': 'Static inputs for the nightly current-season WAR; see war/static_inputs.py. Do not edit by hand.',
        'retrosheet_through': last,
        'source_sha256': {n: hashlib.sha256(open(os.path.join(inputs_dir, n), 'rb').read()).hexdigest() for n in SOURCES},
        'lw': lw[last],
        'tder': {f'{y}|{t}': list(v) for (y, t), v in sorted(tder.items())},
        'li_table': {'|'.join(map(str, k)): v for k, v in sorted(pk('li_raw.pkl')['TABLE'].items())},
        'lgstr': {f'{y}|{L}': v for (y, L), v in sorted(pk('lgstr.pkl').items()) if y > last},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(doc, f, indent=0, sort_keys=True)
        f.write('\n')
    return doc


def load(path=PATH):
    """The static part of compute()'s `inputs`: the pieces above in the files' own shapes, everything else empty."""
    d = json.load(open(path))
    last = d['retrosheet_through']
    split = lambda k: (int(k.split('|')[0]), k.split('|')[1])
    return {
        'bat_raw.pkl': {'LW': {last: d['lw']}, 'PA': [], 'BR': [], 'DP': []},
        'tder_raw.pkl': {split(k): tuple(v) for k, v in d['tder'].items()},
        'li_raw.pkl': {'TABLE': {tuple(int(x) for x in k.split('|')): v for k, v in d['li_table'].items()}},
        'lgstr.pkl': {split(k): v for k, v in d['lgstr'].items()},
        'lgstr_pre1900.pkl': {}, 'posPA.pkl': {}, 'roe.pkl': [], 'coverage_league.json': {},
        'fld2_raw.pkl': {}, 'zfld_raw.pkl': {}, 'cat_raw.pkl': {}, 'tz_raw.pkl': [], 'rb_raw.pkl': [],
    }


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Write war/static/current_season.json from the input files')
    ap.add_argument('command', choices=('export',))
    ap.add_argument('--inputs', required=True)
    a = ap.parse_args()
    d = export(a.inputs)
    print(f"{PATH}: retrosheet_through {d['retrosheet_through']}, tder {len(d['tder'])}, li_table {len(d['li_table'])}, "
          f"lgstr {len(d['lgstr'])}, {os.path.getsize(PATH) // 1024} KB")
