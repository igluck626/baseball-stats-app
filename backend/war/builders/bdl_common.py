# bdl_common.py — shared by the balldontlie-season builders (bdl_season, bdl_fielding, bdl_runners, bdl_leverage).
# Each builder is a function of the season's games in memory, called by the nightly update (war/provisional.py) and by
# its own command line, which reads a raw download folder (WAR_BDL_DIR) and writes the bdlYY*.pkl input files.
import collections
import json
import os


def bdl_maps(cur):
    """balldontlie id → [our player_ids]: (from players only, from players and pitchers). Callers use an id only when
    it maps to exactly one player, so the lists' order doesn't matter."""
    out = []
    for sql in ('SELECT bdl_id, player_id FROM players WHERE bdl_id IS NOT NULL',
                'SELECT bdl_id, player_id FROM players WHERE bdl_id IS NOT NULL UNION '
                'SELECT bdl_id, player_id FROM pitchers WHERE bdl_id IS NOT NULL'):
        cur.execute(sql)
        m = collections.defaultdict(list)
        for b, p in cur.fetchall():
            m[b].append(p)
        out.append(m)
    return tuple(out)


def load_raw(bdl_dir):
    """A raw download folder (games.json + lineups/pa/stats/plays .jsonl) as (G, LU, PA, ST, PL): G = {id: game} for
    regular-season finals; the others {id: rows} in file order, for those games only, cut down to the fields the
    builders read (war.bdl_store's compact rows: the same values, much less memory)."""
    from war import bdl_store as bs
    G = {g['id']: g for g in json.load(open(os.path.join(bdl_dir, 'games.json'))) if bs.is_regular_final(g)}

    def load(kind, fn, keep=lambda x: True):
        f = os.path.join(bdl_dir, kind + '.jsonl')
        if not os.path.exists(f):
            return {}
        return {j['game_id']: [fn(x) for x in j['data'] if keep(x)] for j in map(json.loads, open(f)) if j['game_id'] in G}
    return (G, load('lineups', bs.compact_lineup), load('pa', bs.compact_pa), load('stats', bs.compact_stat),
            load('plays', bs.compact_play, lambda x: not bs.is_pitch_call(x.get('type'))))
