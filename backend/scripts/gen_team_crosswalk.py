#!/usr/bin/env python3
"""Generate backend/api/team_crosswalk.py from Lahman's Teams.csv.

The /ask team-name resolver needs to (a) render an opponent CODE as an
era-correct NAME and (b) resolve a NICKNAME in a question to the set of codes
to filter on. The opponent column in batting/pitching_gamelogs is, empirically:

  * Retrosheet codes for every COMPLETED season (the retrosheet_gamelogs
    "2000-2025 replacement" backfills modern years too) — e.g. a 2022 Dodgers
    game is 'LAN', a 2018 Angels game 'ANA', a 2023 Nationals game 'WAS'.
  * MLB-StatsAPI short codes for the IN-PROGRESS season only (not yet in the
    Retrosheet mirror) — e.g. a 2026 Royals game is 'KC', a Giants game 'SF'.

So codes come from THREE vocabularies: Retrosheet (bulk), Lahman teamID (the
postseason series table), and modern short codes (current season + any BR-style
rows). This script bakes all three into static lookups. Regenerate with:

    python3 backend/scripts/gen_team_crosswalk.py > backend/api/team_crosswalk.py
"""
import csv
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "..", "data", "lahman", "Teams.csv")
FLOOR = 1901  # the app's historical floor; 19th-century code collisions are moot

# StatsAPI / short abbreviations that DIFFER from the Baseball-Reference code,
# keyed to the BR code so we can find the franchise via the CSV. The current
# in-progress season surfaces these (e.g. 'KC' for the Royals). Identity cases
# (ATL, BOS, ...) need no entry — the BR code already matches.
_SHORT_TO_BR = {
    "AZ": "ARI", "KC": "KCR", "SF": "SFG", "SD": "SDP", "TB": "TBR",
    "WSH": "WSN", "CWS": "CHW", "ATH": "OAK",
}

# Nickname -> Lahman teamID seed (mirrors data_service._BDL_TEAM_NAME_TO_LAHMAN,
# which already carries historical aliases). Resolved to a franchID via the CSV.
# Extra spelling variants are normalised (lowercased, non-alphanumerics stripped)
# at lookup time, so "d-backs"/"dbacks" and "red sox"/"redsox" collapse together.
_NICK_TO_TEAMID = {
    "diamondbacks": "ARI", "dbacks": "ARI",
    "braves": "ATL", "orioles": "BAL", "redsox": "BOS",
    "cubs": "CHN", "whitesox": "CHA", "reds": "CIN",
    "guardians": "CLE", "indians": "CLE",
    "rockies": "COL", "tigers": "DET", "astros": "HOU",
    "royals": "KCA", "angels": "LAA", "dodgers": "LAN", "marlins": "MIA",
    "brewers": "MIL", "twins": "MIN", "mets": "NYN", "yankees": "NYA",
    "athletics": "ATH", "as": "ATH",
    "phillies": "PHI", "pirates": "PIT", "padres": "SDN", "giants": "SFN",
    "mariners": "SEA", "cardinals": "SLN", "cards": "SLN",
    "rays": "TBA", "devilrays": "TBA",
    "rangers": "TEX", "bluejays": "TOR", "nationals": "WAS", "nats": "WAS",
    "expos": "MON",
}

# Cities that hosted MORE THAN ONE AL/NL franchise across history — nickname-only
# resolution declines these, because resolving the bare city to one club silently
# drops the city's other franchises (e.g. "Washington" -> Nationals would erase 60
# years of Senators). Derived from Teams.csv (lgID in AL/NL, 1901+): every city
# with >1 distinct franchID. A city that only ever hosted one franchise is NOT here
# and resolves normally. Keys are lowercased to match the detector's capture.
_ST_LOUIS = "St. Louis could mean the Cardinals or the Browns (who became the Orioles) — name the club."
_AMBIG_CITY = {
    "washington": ("Washington could mean the Senators (who became the Twins, or the "
                   "later club that became the Rangers) or the Nationals — name the club."),
    "new york": "New York could mean the Yankees, the Mets, or the Giants — name the club.",
    "los angeles": "Los Angeles could mean the Dodgers or the Angels — name the club.",
    "chicago": "Chicago could mean the Cubs or the White Sox — name the club.",
    "st. louis": _ST_LOUIS,
    "st louis": _ST_LOUIS,
    "boston": "Boston could mean the Red Sox or the Braves (who moved to Atlanta) — name the club.",
    "philadelphia": "Philadelphia could mean the Phillies or the Athletics — name the club.",
    "baltimore": ("Baltimore could mean the Orioles or the early-1900s club that became "
                  "the Yankees — name the club."),
    "kansas city": ("Kansas City could mean the Royals or the Athletics (who moved to "
                    "Oakland) — name the club."),
    "milwaukee": "Milwaukee could mean the Brewers or the Braves (who moved to Atlanta) — name the club.",
    "seattle": "Seattle could mean the Mariners or the Pilots (who became the Brewers) — name the club.",
}


def _norm(s):
    return "".join(c for c in s.lower() if c.isalnum())


def main():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if int(r["yearID"]) >= FLOOR]

    # (scheme, code) -> ordered (start_year, name) runs, compressed where the name
    # is unchanged. scheme in {'retro','lahman'}.
    def build_runs(code_col):
        by_code = defaultdict(list)  # code -> [(year, name)]
        for r in rows:
            code = (r[code_col] or "").strip()
            if code:
                by_code[code].append((int(r["yearID"]), r["name"]))
        out = {}
        for code, pairs in by_code.items():
            pairs.sort()
            runs, last = [], None
            for yr, nm in pairs:
                if nm != last:
                    runs.append((yr, nm))
                    last = nm
            out[code] = tuple(runs)
        return out

    retro_runs = build_runs("teamIDretro")
    lahman_runs = build_runs("teamID")

    # franchID -> current (latest) name, and -> latest BR code.
    latest_year = defaultdict(lambda: -1)
    franch_name, franch_br = {}, {}
    teamidbr_to_franch = {}
    for r in rows:
        fr, yr = r["franchID"], int(r["yearID"])
        if r.get("teamIDBR"):
            teamidbr_to_franch[r["teamIDBR"]] = fr
        if yr > latest_year[fr]:
            latest_year[fr] = yr
            franch_name[fr] = r["name"]
            franch_br[fr] = (r.get("teamIDBR") or "").strip()

    # franchID -> ordered (retro_code, year_lo, year_hi) segments, the era-correct
    # codes the daily table actually stores (Retrosheet), compressed over time.
    seg_by_fr = defaultdict(list)  # fr -> [(year, retro_code)]
    for r in rows:
        rc = (r["teamIDretro"] or "").strip()
        if rc:
            seg_by_fr[r["franchID"]].append((int(r["yearID"]), rc))
    franch_segments = {}
    for fr, pairs in seg_by_fr.items():
        pairs.sort()
        segs, cur_code, lo, hi = [], None, None, None
        for yr, code in pairs:
            if code == cur_code and yr == hi + 1:
                hi = yr
            else:
                if cur_code is not None:
                    segs.append((cur_code, lo, hi))
                cur_code, lo, hi = code, yr, yr
        if cur_code is not None:
            segs.append((cur_code, lo, hi))
        franch_segments[fr] = tuple(segs)

    # Modern short/BR codes per franchise (current season + any BR-style rows),
    # and the inverse for code->name. Only CURRENT-era codes — never a historical
    # BR code like KCA-for-Athletics, so nothing collides with a retro code.
    latest_max = max(latest_year.values())
    active = {fr for fr, y in latest_year.items() if y >= latest_max - 1}
    franch_modern = defaultdict(set)
    for fr in active:
        if franch_br.get(fr):
            franch_modern[fr].add(franch_br[fr])
    for short, br in _SHORT_TO_BR.items():
        fr = teamidbr_to_franch.get(br)
        if fr in active:
            franch_modern[fr].add(short)
    modern_to_franch = {}
    for fr, codes in franch_modern.items():
        for c in codes:
            modern_to_franch[c] = fr

    # nickname (normalised) -> franchID
    teamid_to_franch = {}
    for r in rows:
        teamid_to_franch[r["teamID"]] = r["franchID"]  # last wins (latest franchID)
    nick_to_franch = {}
    for nick, tid in _NICK_TO_TEAMID.items():
        fr = teamid_to_franch.get(tid)
        if fr:
            nick_to_franch[_norm(nick)] = fr

    # CITY/REGION -> franchID, but ONLY for cities that hosted exactly ONE AL/NL
    # franchise across 1901+ (so "against Detroit" resolves to the Tigers, while a
    # city in _AMBIG_CITY never appears here). Derived, not hand-listed: we split
    # each AL/NL team name into its city prefix (the known-city list below, longest
    # match wins) and keep cities with a single franchID. A name with no recognised
    # city prefix (the 2025 city-less "Athletics") is skipped — the franchise is
    # still reached through its city-named seasons ("Oakland Athletics" -> Oakland).
    _KNOWN_CITIES = [
        "New York", "Los Angeles", "San Francisco", "San Diego", "Kansas City",
        "St. Louis", "Tampa Bay", "Washington", "Chicago", "Boston", "Philadelphia",
        "Baltimore", "Milwaukee", "Seattle", "Cleveland", "Detroit", "Pittsburgh",
        "Cincinnati", "Houston", "Atlanta", "Minnesota", "Texas", "Toronto", "Miami",
        "Florida", "Colorado", "Arizona", "Oakland", "Anaheim", "California",
        "Brooklyn", "Montreal",
    ]
    _cities_by_len = sorted(_KNOWN_CITIES, key=len, reverse=True)

    def city_of(name):
        for c in _cities_by_len:
            if name.startswith(c):
                return c
        return None

    city_franch = defaultdict(set)  # city (lower) -> {franchID}
    for r in rows:
        if r["lgID"] not in ("AL", "NL"):
            continue
        c = city_of(r["name"])
        if c:
            city_franch[c.lower()].add(r["franchID"])
    city_to_franch = {c: next(iter(frs)) for c, frs in city_franch.items()
                      if len(frs) == 1}
    # A city that resolves must NOT also be an ambiguous city — they are built from
    # the same >1-vs-1 franchID split, so this can't happen, but assert it anyway.
    _amb = {_norm(k) for k in _AMBIG_CITY}
    clash = {c for c in city_to_franch if _norm(c) in _amb}
    assert not clash, f"city in BOTH resolve and ambiguous maps: {clash}"

    def dump(name, obj):
        print(f"{name} = {obj!r}\n")

    print('"""GENERATED by backend/scripts/gen_team_crosswalk.py — do not edit by hand.')
    print("")
    print("Static team-code crosswalk for the /ask team-name resolver, derived from")
    print("backend/data/lahman/Teams.csv (seasons >= %d). See the generator for the" % FLOOR)
    print("three code vocabularies (Retrosheet / Lahman teamID / modern short) and why.")
    print('"""')
    print("")
    dump("SCHEME_SPLIT_NOTE", "opponent column is Retrosheet for completed seasons, "
         "MLB-StatsAPI short codes for the in-progress season")
    dump("RETRO_NAME_RUNS", retro_runs)
    dump("LAHMAN_NAME_RUNS", lahman_runs)
    dump("FRANCH_CURRENT_NAME", franch_name)
    dump("FRANCH_RETRO_SEGMENTS", franch_segments)
    dump("MODERN_CODE_TO_FRANCH", modern_to_franch)
    dump("FRANCH_MODERN_CODES", {k: tuple(sorted(v)) for k, v in franch_modern.items()})
    dump("NICK_TO_FRANCH", nick_to_franch)
    dump("CITY_TO_FRANCH", city_to_franch)
    dump("AMBIG_CITY", _AMBIG_CITY)


if __name__ == "__main__":
    main()
