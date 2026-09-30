#!/usr/bin/env python3
"""`chadwick_catchup.classify` — the MLBAM matching rule for unmapped
balldontlie players, and the three bugs the first dry run had.

⚠️ THE RULE: exact name (accent-folded) + exact full birth date, unique in
the register AND in balldontlie, target MLBAM not already mapped to another
bdl_id. Debut year: missing or ±1 is fine, 2+ holds. Never guess.

Standalone, no pytest. Needs the backend's Python (3.10+): it imports
data_service's birth-date parser.
Run: <backend python> backend/tests/test_chadwick_catchup.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import chadwick_catchup as cc                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def reg(mlbam, first, last, y, m, d, debut=""):
    return {"key_mlbam": str(mlbam), "name_first": first, "name_last": last,
            "birth_year": str(y), "birth_month": str(m), "birth_day": str(d),
            "mlb_played_first": str(debut)}


def bdl(i, first, last, dob, debut=None):
    return {"id": i, "first_name": first, "last_name": last, "dob": dob, "debut_year": debut}


def cls(players, register, held=None):
    out = cc.classify(players, register, held or {})
    return {p["bdl_id"]: c for c, rows in out.items() for p in rows}, out


print("accept")
c, out = cls([bdl(1, "José", "Quero", "1998-12-03", 2026)], [reg(691620, "Jose", "Quero", 1998, 12, 3, 2026)])
check("exact name (accent-folded) + birth date + debut", c[1] == cc.ACCEPT and out[cc.ACCEPT][0]["mlbam"] == 691620)
c, out = cls([bdl(2, "J.P.", "France", "1995-04-04", 2023)], [reg(641585, "JP", "France", 1995, 4, 4, 2023)])
check("punctuation folds: 'J.P.' = 'JP'", c[2] == cc.ACCEPT)
c, out = cls([bdl(3, "Bobby", "Witt Jr.", "2000-06-14", 2022)], [reg(677951, "Bobby", "Witt", 2000, 6, 14, 2022)])
check("a Jr. suffix folds", c[3] == cc.ACCEPT)
c, out = cls([bdl(4, "Ann", "Able", "2000-01-02", None)], [reg(9, "Ann", "Able", 2000, 1, 2, "")])
check("debut missing on both sides is fine", c[4] == cc.ACCEPT and out[cc.ACCEPT][0]["debut_check"] == "missing")
c, out = cls([bdl(5, "Ann", "Able", "2000-01-02", 2025)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("debut ±1 is fine", c[5] == cc.ACCEPT and out[cc.ACCEPT][0]["debut_check"] == "±1")

print("hold")
c, _ = cls([bdl(6, "Ann", "Able", "2000-01-02", 2024)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("debut off by 2 holds", c[6] == cc.HOLD_DEBUT)
c, _ = cls([bdl(7, "Ann", "Able", "2000-01-03", 2026)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("birth date one day off: no register match", c[7] == cc.HOLD_NO_MATCH)
c, _ = cls([bdl(8, "Annie", "Able", "2000-01-02", 2026)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("a nickname is not the name", c[8] == cc.HOLD_NO_MATCH)
c, _ = cls([bdl(10, "Ann", "Able", None, 2026)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("no balldontlie birth date holds (Balcazar)", c[10] == cc.HOLD_NO_DOB)
c, _ = cls([bdl(11, "Ann", "Able", "2000-01-02", 2026)],
           [reg(9, "Ann", "Able", 2000, 1, 2, 2026), reg(19, "Ann", "Able", 2000, 1, 2, 2026)])
check("two register people with the same name + birth date hold", c[11] == cc.HOLD_REG_DUP)
c, out = cls([bdl(12, "Ann", "Able", "2000-01-02", 2026)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)], {9: {"555"}})
check("MLBAM already mapped to a different bdl_id holds (Furman, Kempner, Warren)",
      c[12] == cc.HOLD_TAKEN and out[cc.HOLD_TAKEN][0]["held_by"] == [555])
c, out = cls([bdl(13, "Ann", "Able", "2000-01-02", 2026)], [reg(9, "Ann", "Able", 2000, 1, 2, 2026)], {9: {None}})
check("an existing row with an EMPTY bdl_id accepts (J.P. France's case)",
      c[13] == cc.ACCEPT and out[cc.ACCEPT][0]["already_in_db"])

print("the three dry-run bugs")
# 1. the birth date was compared as a tuple rendered to a string, so nothing matched.
c, _ = cls([bdl(20, "Ann", "Able", "08/07/91", 2014)], [reg(9, "Ann", "Able", 1991, 8, 7, 2014)])
check("bug 1: a M/D/YY birth date matches as an ISO date", c[20] == cc.ACCEPT)
c, _ = cls([bdl(21, "Ann", "Able", "18/2/2000", 2020)], [reg(9, "Ann", "Able", 2000, 2, 18, 2020)])
check("  ...and a D/M/YYYY one", c[21] == cc.ACCEPT)
# 2. a partial birth date crashed.
try:
    c, _ = cls([bdl(22, "Ann", "Able", "1991", 2014)], [reg(9, "Ann", "Able", 1991, 8, 7, 2014)])
    check("bug 2: a partial birth date is 'no birth date', not a crash", c[22] == cc.HOLD_NO_DOB, c)
except Exception as e:                                           # noqa: BLE001
    check("bug 2: a partial birth date is 'no birth date', not a crash", False, repr(e))
c, _ = cls([bdl(23, "Ann", "Able", "1991-00-00", 2014)], [reg(9, "Ann", "Able", 1991, 8, 7, 2014)])
check("  ...and so is a zeroed one ('1991-00-00')", c[23] == cc.HOLD_NO_DOB, c)
# 3. duplicates were caught only within one bucket: here one record has a
#    debut year and the other doesn't, which used to land them apart.
c, out = cls([bdl(30, "Ann", "Able", "2000-01-02", 2026), bdl(31, "Ann", "Able", "2000-01-02", None)],
             [reg(9, "Ann", "Able", 2000, 1, 2, 2026)])
check("bug 3: two balldontlie ids with one name + birth date both hold, across buckets",
      c[30] == c[31] == cc.HOLD_BDL_DUP and out[cc.HOLD_BDL_DUP][0]["others"] == [31])

c, _ = cls([bdl(32, "Bo", "Davidson", "2002-07-05", 2026), bdl(33, "Chanteyon", "Davidson", "2002-07-05", None)],
           [reg(815589, "Bo", "Davidson", 2002, 7, 5, "")])
check("  ...including twins whose FIRST names differ (Bo / Chanteyon Davidson)",
      c[32] == cc.HOLD_BDL_DUP, c)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
