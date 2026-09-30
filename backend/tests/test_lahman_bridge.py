#!/usr/bin/env python3
"""`lahman_load.judge_lahman_ids` — Lahman playerID -> MLBAM, judged per player.

⚠️ THE CHECKS THAT MATTER:
  • Every expected id below comes from INDEPENDENT evidence — the Chadwick
    register row with the same full birth date as Lahman's People row — never
    from "the playerID key wins" or "the bbrefID key wins". Each of those rules
    was tried and each misfiled real careers onto namesakes.
  • A Lahman row whose ids point at the next man over (Lahman shifted
    bbrefID AND retroID along the Ewing -> Fabelo -> Fabré chain) is never
    mapped to the neighbour: it is placed only when an exact-name candidate's
    first season AND full birth date agree, else HELD.
  • A man no id reaches is placed by exact name + full birth date, unique in
    the register and in Lahman — or held.
  • Two playerIDs judged onto one MLBAM id are both held.

Runs against the committed data files (backend/data/lahman/People.csv and the
register-refreshed chadwick_mlb.csv), plus synthetic rows for the rule itself.
Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_lahman_bridge.py
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import lahman_load as ll                                          # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


with open(ll.CHADWICK_CSV, newline="", encoding="utf-8-sig") as fh:
    CHADWICK = list(csv.DictReader(fh))
with open(ll.PEOPLE_CSV, newline="", encoding="utf-8-sig") as fh:
    PEOPLE = list(csv.DictReader(fh))
bridge, report = ll.judge_lahman_ids(CHADWICK, PEOPLE, ll._lahman_years())

# playerID -> (expected MLBAM, the evidence: the register row with Lahman's birth date)
EXPECTED = {
    # Lahman's kingbr01 is Bryan King; Baseball-Reference's kingbr01 is Brennan King.
    "kingbr01":  (687911, "Bryan King, b. 1996-11-05, retro kingb001"),
    "kingbr02":  (818605, "Brennan King, b. 1916-08-16, retro kingb103"),
    "brownbe01": (676962, "Ben Brown, b. 1999-09-09, retro browb006"),
    "brownbe02": (820350, "Benny Brown, b. 1911-10-08, retro browb108"),
    "mannima01": (666159, "Matt Manning, b. 1998-01-28, retro mannm001"),
    "mannima02": (818363, "Max Manning, b. 1918-11-18, retro mannm101"),
    "greenri01": (214294, "Rick Greene, b. 1971-01-02, retro greer002"),
    "greenri02": (682985, "Riley Greene, b. 2000-09-28, retro greer003"),
    "wilsoja03": (607111, "Jacob Wilson, b. 1990-07-29, retro wilsj005"),
    "wilsoja04": (805779, "Jacob Wilson, b. 2002-03-30, retro wilsj006"),
    "jonesja04": (150218, "Jacque Jones, b. 1975-04-25, retro jonej003"),
    "jonesja05": (429961, "Jason Jones, b. 1976-10-17, retro jonej004"),
    "harriwi01": (407483, "Willie Harris, b. 1978-06-22, retro harrw001"),
    "harriwi02": (501789, "Will Harris, b. 1984-08-28, retro harrw002"),
    "harriho01": (665048, "Hobie Harris, b. 1993-06-23, retro harrh001"),
    "harriho02": (663687, "Hogan Harris, b. 1996-12-26, retro harrh002"),
    "harriho03": (819157, "Horace Harris, b. 1911-05-11, retro harrh102"),
    "oneilpa01": (120028, "Paul O'Neill, b. 1963-02-25, retro oneip001"),
    "oneilty01": (641933, "Tyler O'Neill, b. 1995-06-22, retro oneit001"),
    # Lahman's retroID for Fabré (faceu101) has no MLBAM id; his playerID
    # candidate confirms by name and first season.
    "fabreis01": (819596, "Isidro Fabré, b. 1895-05-15"),
    # the shifted chain, placed past the retroID by name + first season + birth date
    "ewingbu02": (819599, "Buck Ewing, b. 1903-01-31 (retroID reads Columbus's)"),
    "ewingco01": (819597, "Columbus Ewing, b. 1902-07-14 (retroID reads Fabelo's)"),
    # no retroID or bbrefID reaches him; name + birth date does
    "williji06": (816533, "Jim Willis, b. 1906-06-11"),
}
print("named players, each against the register row with his birth date")
for pid, (want, why) in EXPECTED.items():
    check(f"{pid} -> {want} ({why})", bridge.get(pid) == want, f"got {bridge.get(pid)}; {report['held'].get(pid, '')}")

print("the Ewing -> Fabelo -> Fabré chain: never the next man")
for pid, neighbour in (("ewingbu02", 819597), ("ewingco01", 819598), ("fabelju01", 819596)):
    check(f"{pid} is not given the next man ({neighbour})", bridge.get(pid) != neighbour, bridge.get(pid))
check("fabelju01 (first season 1916 vs the register's 1923) stays held",
      "fabelju01" not in bridge and "fabelju01" in report["held"], bridge.get("fabelju01"))

print("a Lahman player with no id reaching him is held, not given a namesake")
check("wilsoja05 (James Wilson, 1947) is held, never Jacob Wilson 805779",
      "wilsoja05" not in bridge and "wilsoja05" in report["held"], bridge.get("wilsoja05"))

print("whole population")
claimed = {}
for pid, m in bridge.items():
    claimed.setdefault(m, []).append(pid)
dupes = {m: p for m, p in claimed.items() if len(p) > 1}
check("no MLBAM id is judged for two Lahman playerIDs", not dupes, list(dupes.items())[:5])
paths = [set(report[k]) for k in ("retro", "fallthrough", "fallback", "birth")]
check("every judged id comes from exactly one path",
      sum(map(len, paths)) == len(bridge) == len(set().union(*paths)))

print("the rule itself, on synthetic rows")
ch = [
    {"key_mlbam": "1", "key_retro": "r1", "key_bbref": "b1", "name_first": "Al", "name_last": "Doe", "mlb_played_first": "1920", "mlb_played_last": "1925"},
    {"key_mlbam": "2", "key_retro": "r2", "key_bbref": "b2", "name_first": "Bo", "name_last": "Doe", "mlb_played_first": "1931", "mlb_played_last": "1931"},
    {"key_mlbam": "3", "key_retro": "",   "key_bbref": "b3", "name_first": "Cy", "name_last": "Roe", "mlb_played_first": "1900", "mlb_played_last": "1901"},
    {"key_mlbam": "4", "key_retro": "",   "key_bbref": "b4", "name_first": "Cy", "name_last": "Roe", "mlb_played_first": "1905", "mlb_played_last": "1909"},
]
def one(person, years):
    b, r = ll.judge_lahman_ids(ch, [person], {person["playerID"]: years})
    return b.get(person["playerID"]), r
got, _ = one({"playerID": "x", "retroID": "r1", "bbrefID": "", "nameFirst": "Albert", "nameLast": "Doe"}, (1920, 1925))
check("a nickname confirms when the first season agrees", got == 1, got)
got, r = one({"playerID": "x", "retroID": "r2", "bbrefID": "", "nameFirst": "Al", "nameLast": "Doe"}, (1920, 1930))
check("a retroID pointing at a same-surname neighbour 11 years off is held", got is None and "x" in r["held"], got)
got, _ = one({"playerID": "b3", "retroID": "", "bbrefID": "b4", "nameFirst": "Cy", "nameLast": "Roe"}, (1905, 1909))
check("no retroID: two same-name candidates, the exact first season decides", got == 4, got)
got, r = one({"playerID": "zz", "retroID": "", "bbrefID": "", "nameFirst": "Cy", "nameLast": "Roe"}, (1905, 1909))
check("no candidate at all is held", got is None and "zz" in r["held"], got)
chb = [dict(c, birth_year="1900", birth_month="1", birth_day="2") for c in ch]
chb[1] = dict(chb[1], birth_year="1905")
def born(person, years, rows=chb):
    b, r = ll.judge_lahman_ids(rows, [person], {person["playerID"]: years})
    return b.get(person["playerID"]), r
shifted = {"playerID": "b2", "retroID": "r1", "bbrefID": "", "nameFirst": "Bo", "nameLast": "Doe",
           "birthYear": "1905", "birthMonth": "1", "birthDay": "2"}
got, _ = born(shifted, (1931, 1931))
check("a shifted retroID falls through to the exact-name candidate when the birth date agrees", got == 2, got)
got, _ = born(dict(shifted, birthYear="1906"), (1931, 1931))
check("  ...and is held when it doesn't", got is None, got)
nodob = dict(shifted, birthYear="", birthMonth="", birthDay="")
got, _ = born(nodob, (1931, 1931))
check("  ...with no Lahman birth date: placed when name + exact first season is unique in the register", got == 2, got)
twin = dict(chb[1], key_mlbam="9", key_retro="r9", key_bbref="b9")
got, _ = born(nodob, (1931, 1931), chb + [twin])
check("  ...and held when a second register row shares that name + first season", got is None, got)
lost = {"playerID": "none", "retroID": "", "bbrefID": "", "nameFirst": "Cy", "nameLast": "Roe",
        "birthYear": "1900", "birthMonth": "1", "birthDay": "2"}
got, r = born(lost, (1900, 1901), [c for c in chb if c["key_mlbam"] == "3"])
check("name + full birth date, unique in the register, places a man no id reaches", got == 3 and r["birth"] == ["none"], got)
got, _ = born(lost, (1900, 1901))
check("  ...but two register rows with that name + birth date hold him", got is None, got)
got, _ = born(dict(lost, birthDay=""), (1900, 1901), [c for c in chb if c["key_mlbam"] == "3"])
check("  ...and a partial birth date never places anyone", got is None, got)
b, r = ll.judge_lahman_ids(ch, [{"playerID": "p", "retroID": "r1", "bbrefID": "", "nameFirst": "Al", "nameLast": "Doe"},
                                {"playerID": "q", "retroID": "r1", "bbrefID": "", "nameFirst": "Al", "nameLast": "Doe"}],
                           {"p": (1920, 1925), "q": (1920, 1925)})
check("two playerIDs judged onto one MLBAM id are both held", not b and {"p", "q"} <= set(r["held"]), b)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
