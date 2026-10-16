#!/usr/bin/env python3
"""The nightly's Chadwick step (scripts/chadwick_daily.py).

⚠️ THE CHECKS THAT MATTER:
  • Only ACCEPTs are planned for writing. Every HOLD — no match yet, a
    register duplicate, an id already taken, a debut 2+ years off — is left
    out of the plan (and reported), never written.
  • A player whose id is already mapped to him is "already mapped", not a write.
  • The register is downloaded only when its latest commit changed; a cached
    copy is reused, and with GitHub unreachable the newest cached copy is used
    without any download. Only rows with a key_mlbam and five columns are kept.

Standalone, no pytest. Run: <backend python> backend/tests/test_chadwick_daily.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import chadwick_catchup as cc                                     # noqa: E402
import chadwick_daily as cd                                       # noqa: E402

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


print("only ACCEPT is written")
register = [reg(691620, "Jose", "Quero", 1998, 12, 3, 2026),          # accept, new player
            reg(641585, "JP", "France", 1995, 4, 4, 2023),            # accept, row exists unmapped
            reg(9, "Ann", "Able", 2000, 1, 2, 2026), reg(10, "Ann", "Able", 2000, 1, 2, 2026),  # register dup
            reg(11, "Bo", "Taken", 1999, 5, 5, 2025),                  # id held by another bdl_id
            reg(12, "Cy", "Debut", 1990, 1, 1, 2020)]                  # debut 2+ off
players = [bdl(1, "José", "Quero", "1998-12-03", 2026), bdl(2, "J.P.", "France", "1995-04-04", 2023),
           bdl(3, "Ann", "Able", "2000-01-02", 2026), bdl(4, "Bo", "Taken", "1999-05-05", 2025),
           bdl(5, "Cy", "Debut", "1990-01-01", 2025), bdl(6, "Not", "Registered", "2003-03-03", 2026),
           bdl(7, "No", "Birthday", None, 2026)]
held = {641585: {None}, 11: {999}}
plan, out = cd.decide(players, register, held, mapped_to={})
acts = {e["bdl_id"]: e["action"] for e in plan}
check("the two ACCEPTs are planned: a new bio and a stamp on the existing row",
      acts == {1: cc.CREATE, 2: cc.STAMP}, acts)
holds = {x["bdl_id"]: c for c, rows in out.items() if c != cc.ACCEPT for x in rows}
check("register duplicate, taken id, debut gap, no match, no birth date: all HOLD, none planned",
      holds == {3: cc.HOLD_REG_DUP, 4: cc.HOLD_TAKEN, 5: cc.HOLD_DEBUT, 6: cc.HOLD_NO_MATCH, 7: cc.HOLD_NO_DOB}
      and not set(holds) & set(acts), holds)
plan, _ = cd.decide([players[0]], register, {}, mapped_to={1: {691620}})
check("already carrying that bdl_id: 'already mapped', nothing to write",
      [e["action"] for e in plan] == [cc.ALREADY])

print("the register cache")
body = ("key_mlbam,name_first,name_last,birth_year,birth_month,birth_day,mlb_played_first,key_retro,name_suffix\n"
        "691620,Jose,Quero,1998,12,3,2026,querj001,\n"
        ",Nobody,Unkeyed,1900,1,1,,,\n").encode()
calls = []
fetch = lambda url: (calls.append(url), body)[1]
d = tempfile.mkdtemp(prefix="chadwick_cache_")
rows, ver = cd.load_register("abc123", cache_dir=d, fetch=fetch)
check("a new commit downloads all 16 people files once", len(calls) == 16 and ver == "abc123", (len(calls), ver))
check("kept: only rows with a key_mlbam, only the five matcher columns",
      len(rows) == 16 and set(rows[0]) == set(cd.KEEP) and rows[0]["key_mlbam"] == "691620", (len(rows), list(rows[0])))
calls.clear()
rows, ver = cd.load_register("abc123", cache_dir=d, fetch=fetch)
check("the same commit again: the cached copy, no download", not calls and ver == "abc123" and len(rows) == 16)
rows, ver = cd.load_register(None, cache_dir=d, fetch=fetch)
check("GitHub unreachable: the newest cached copy, no download", not calls and ver == "abc123")
rows, ver = cd.load_register("def456", cache_dir=d, fetch=fetch)
left = sorted(os.listdir(d))
check("a newer commit replaces the old copy", len(calls) == 16 and left == ["register-def456.csv"], left)
calls.clear()
rows, ver = cd.load_register(None, cache_dir=tempfile.mkdtemp(prefix="chadwick_empty_"), fetch=fetch)
check("unreachable and nothing cached: downloads master", len(calls) == 16 and "/master/" in calls[0] and ver == "master")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
