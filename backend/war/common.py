"""Shared helpers: team-code normalising and balldontlie batted-ball classes (used by compute.py and the builders)."""
CITY = {('Arizona', 'NL'): 'ARI', ('Atlanta', 'NL'): 'ATL', ('Baltimore', 'AL'): 'BAL', ('Boston', 'AL'): 'BOS', ('Chicago', 'AL'): 'CHA',
        ('Chicago', 'NL'): 'CHN', ('Cincinnati', 'NL'): 'CIN', ('Cleveland', 'AL'): 'CLE', ('Colorado', 'NL'): 'COL', ('Detroit', 'AL'): 'DET',
        ('Houston', 'AL'): 'HOU', ('Kansas City', 'AL'): 'KCA', ('Los Angeles', 'AL'): 'LAA', ('Los Angeles', 'NL'): 'LAN', ('Miami', 'NL'): 'MIA',
        ('Milwaukee', 'NL'): 'MIL', ('Minnesota', 'AL'): 'MIN', ('New York', 'AL'): 'NYA', ('New York', 'NL'): 'NYN', ('Philadelphia', 'NL'): 'PHI',
        ('Pittsburgh', 'NL'): 'PIT', ('San Diego', 'NL'): 'SDN', ('San Francisco', 'NL'): 'SFN', ('Seattle', 'AL'): 'SEA', ('St. Louis', 'NL'): 'SLN',
        ('Tampa Bay', 'AL'): 'TBA', ('Texas', 'AL'): 'TEX', ('Toronto', 'AL'): 'TOR', ('Washington', 'NL'): 'WAS', ('Athletics', 'AL'): 'ATH',
        ('Sacramento', 'AL'): 'ATH', ('Oakland', 'AL'): 'ATH'}
def norm_team(team, league):
    """2026 rows store some teams as BDL city names (audit queue item); map them to our codes using the league."""
    if team and len(team) > 3: return CITY.get((team, league), team)
    return team
def area(phi, dist, la):
    if la is not None and la >= 10 and (dist or 0) >= 150:           # outfield air ball
        return 7 if phi < -15 else 8 if phi <= 15 else 9
    if la is None or la < 10:                                          # ground ball
        return 5 if phi < -28 else 6 if phi < -3 else 4 if phi < 20 else 3
    return None                                                        # infield air (pop-ups, short liners): little range skill
OUTS = {'Groundout', 'Flyout', 'Lineout', 'Pop Out', 'Forceout', 'GIDP', 'Double Play', 'Sac Fly', 'Fielders Choice Out',
        'Bunt Groundout', 'Bunt Pop Out', 'Bunt Lineout', 'Sac Fly Double Play', 'Triple Play', 'Sac Bunt'}
INPLAY = OUTS | {'Single', 'Double', 'Triple', 'Field Error', 'Fielders Choice'}     # HR excluded (not fieldable)
import math as _m
def bip_class(x):
    """BDL in-play pitch fields → (responsible position, out-probability bin) or None. Shared by 2026 and the backfill."""
    if not x or x.get('hit_coordinate_x') is None or x.get('hit_coordinate_y') is None: return None
    phi = _m.degrees(_m.atan2(x['hit_coordinate_x'] - 125.42, 198.27 - x['hit_coordinate_y']))
    pos = area(phi, x.get('hit_distance'), x.get('launch_angle'))
    if pos is None or abs(phi) > 50: return None
    la, ev, d = x.get('launch_angle'), x.get('exit_velocity'), x.get('hit_distance') or 0
    typ = 'GB' if la is None or la < 10 else 'LD' if la < 25 else 'FB'
    return pos, (typ, int((phi + 50) // 5), min(int(d // 25), 20) if typ != 'GB' else 0, min(int((ev or 85) // 5), 23))
def out_rates(bips):
    """bips: iterable of (out 0/1, bin). Returns P(out | bin), thin bins shrunk toward the angle-only bin (20 balls)."""
    import collections as _c
    r, c = _c.defaultdict(lambda: [0, 0]), _c.defaultdict(lambda: [0, 0])
    for o, k in bips: r[k][0] += o; r[k][1] += 1; c[k[:2]][0] += o; c[k[:2]][1] += 1
    return lambda k: (r[k][0] + 20 * c[k[:2]][0] / c[k[:2]][1]) / (r[k][1] + 20)
