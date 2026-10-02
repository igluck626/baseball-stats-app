#!/usr/bin/env python3
"""`bio_sources` — the bio a backfilled player is inserted with.

⚠️ THE CHECKS THAT MATTER: CC Sabathia (season stats, no bio row) used to be
inserted with a name and ids only — no birth date, nothing physical. He now gets
his biofile bio. The biofile's partial dates ('1868', '02/  /1842') are a year,
or a year and month — never a month of 1868. A birth date, a death date and a
birthplace each come whole from ONE source, never stitched from two.
final_game is the last REGULAR-SEASON game — never the biofile's, which counts
the postseason (Sabathia: 2019-09-24, not ALCS Game 4 on 2019-10-17), and
only for a man whose career is over (inactive per balldontlie, or no bdl_id).
A source
the man isn't eligible for is never read: no biofile without a retro_id, no
balldontlie when he has one, no Lahman unless his seasons came from Lahman.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_bio_sources.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import bio_sources as bs                                          # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# Sabathia as the two files publish him (Retrosheet biofile; Lahman People).
SAB_BIO = {"PLAYERID": "sabac001", "LAST": "Sabathia", "FIRST": "Carsten Charles", "BIRTHDATE": "07/21/1980",
           "BIRTH CITY": "Vallejo", "BIRTH STATE": "California", "BIRTH COUNTRY": "USA",
           "PLAY DEBUT": "04/08/2001", "PLAY LASTGAME": "10/17/2019", "DEATHDATE": "",
           "BATS": "L", "THROWS": "L", "HEIGHT": "6-07", "WEIGHT": "260"}
SAB_LAHMAN = {"playerID": "sabatcc01", "retroID": "sabac001", "birthYear": "1980", "birthMonth": "7", "birthDay": "21",
              "birthCity": "Vallejo", "birthState": "CA", "birthCountry": "USA", "deathYear": "", "deathMonth": "",
              "deathDay": "", "bats": "L", "throws": "L", "height": "78", "weight": "300",
              "debut": "2001-04-08", "finalGame": "2019-09-24"}
BIOFILE = {"sabac001": SAB_BIO}
STATES, COUNTRIES = bs.place_maps(BIOFILE, [SAB_LAHMAN])

print("biofile dates")
check("MM/DD/YYYY", bs.biofile_date("07/21/1980") == (1980, 7, 21))
check("'1868' is a year, not month 1868", bs.biofile_date("1868") == (1868, None, None))
check("'02/  /1842' is February 1842, day unknown", bs.biofile_date("02/  /1842") == (1842, 2, None))
check("blank is None", bs.biofile_date("") is None and bs.biofile_date(None) is None)
check("month 13 is rejected, not stored", bs.biofile_date("13/01/1900") is None)
check("garbage is rejected", bs.biofile_date("unknown") is None)

print("Sabathia")
sab, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None,
                                      lahman_row=None, lahman_seasons=False,
                                      state_map=STATES, country_map=COUNTRIES))
check("born 1980-07-21", (sab["birth_year"], sab["birth_month"], sab["birth_day"]) == (1980, 7, 21))
check("Vallejo, CA — the state in our two-letter form, learned from Lahman",
      (sab["birth_city"], sab["birth_state"], sab["birth_country"]) == ("Vallejo", "CA", "USA"))
check("bats/throws L/L, 6-07 is 79 inches, 260 lb",
      (sab["bats"], sab["throws"], sab["height"], sab["weight"]) == ("L", "L", 79, 260))
check("debut 2001-04-08", sab["debut"] == "2001-04-08")
check("no death date invented", "death_year" not in sab)
check("every field says it came from the biofile", set(src.values()) == {"biofile"})

print("precedence and eligibility")
both, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None,
                                       lahman_row=SAB_LAHMAN, lahman_seasons=True))
check("biofile outranks Lahman (weight 260, not 300)", both["weight"] == 260 and src["weight"] == "biofile")
none_, _ = bs.merge({}, bs.candidates(retro_id=None, biofile=BIOFILE, bdl_fields=None,
                                      lahman_row=SAB_LAHMAN, lahman_seasons=False))
check("no retro_id and no Lahman seasons: nothing proposed", none_ == {})
only_lahman, src = bs.merge({}, bs.candidates(retro_id=None, biofile=BIOFILE, bdl_fields=None,
                                              lahman_row=SAB_LAHMAN, lahman_seasons=True))
check("Lahman seasons, no retro_id: Lahman fills it", only_lahman["weight"] == 300 and set(src.values()) == {"lahman"})
bdl = {"bats": "R", "birth_year": 2001, "birth_month": 5, "birth_day": 13}
cands = bs.candidates(retro_id=None, biofile=BIOFILE, bdl_fields=bdl, lahman_row=None, lahman_seasons=False)
check("no retro_id: balldontlie is read", [n for n, _ in cands] == ["balldontlie"])
cands = bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=bdl, lahman_row=None, lahman_seasons=False)
check("with a retro_id: balldontlie is NOT read", [n for n, _ in cands] == ["biofile"])

print("units come whole from one source")
year_only = {"sabac001": dict(SAB_BIO, BIRTHDATE="1980")}
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=year_only, bdl_fields=None,
                                      lahman_row=SAB_LAHMAN, lahman_seasons=True))
check("biofile has only the year: month/day are NOT taken from Lahman",
      (out["birth_year"], out.get("birth_month"), out.get("birth_day")) == (1980, None, None)
      and src["birth_year"] == "biofile" and "birth_month" not in src)
no_dob = {"sabac001": dict(SAB_BIO, BIRTHDATE="")}
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=no_dob, bdl_fields=None,
                                      lahman_row=SAB_LAHMAN, lahman_seasons=True))
check("biofile has no birth date at all: the whole date falls through to Lahman",
      (out["birth_year"], out["birth_month"], out["birth_day"]) == (1980, 7, 21) and src["birth_day"] == "lahman")

print("final_game is the last regular-season game")
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None,
                                      lahman_seasons=False, last_regular_game="2019-09-24"))
check("Sabathia: 2019-09-24 from our game logs, not the biofile's 2019-10-17",
      out["final_game"] == "2019-09-24" and src["final_game"] == "gamelogs")
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None,
                                      lahman_seasons=False))
check("no game logs, not Lahman-sourced: final_game stays blank (the biofile is never used)", "final_game" not in out)
check("the biofile never yields a final_game at all", "final_game" not in bs.from_biofile(SAB_BIO))
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=SAB_LAHMAN,
                                      lahman_seasons=True))
check("no game logs, Lahman-sourced: Lahman's finalGame 2019-09-24",
      out["final_game"] == "2019-09-24" and src["final_game"] == "lahman")
out, src = bs.merge({}, bs.candidates(retro_id=None, biofile={}, bdl_fields={"final_game": "2025-09-28", "bats": "R"},
                                      lahman_row=None, lahman_seasons=False))
check("balldontlie never supplies final_game either", "final_game" not in out and out["bats"] == "R")
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None,
                                      lahman_seasons=False, last_regular_game="2019-09-24"))
check("the game logs fill final_game only — every other field still from the biofile",
      src["final_game"] == "gamelogs" and {v for k, v in src.items() if k != "final_game"} == {"biofile"})

print("final_game only once his career is over")
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=SAB_LAHMAN,
                                      lahman_seasons=True, last_regular_game="2025-09-28", bdl_active=True))
check("ACTIVE per balldontlie, last game LAST season: no final_game from the logs or from Lahman",
      "final_game" not in out and out["birth_year"] == 1980)
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None,
                                      lahman_seasons=False, last_regular_game="2019-09-24", bdl_active=False))
check("RETIRED per balldontlie: final_game 2019-09-24 from the logs", out["final_game"] == "2019-09-24")
out, src = bs.merge({}, bs.candidates(retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None,
                                      lahman_seasons=False, last_regular_game="2019-09-24", bdl_active=None))
check("no bdl_id (historical): final_game 2019-09-24 from the logs", out["final_game"] == "2019-09-24")

print("never overwrite")
out, _ = bs.merge({"birth_year": 1979, "weight": 250}, bs.candidates(
    retro_id="sabac001", biofile=BIOFILE, bdl_fields=None, lahman_row=None, lahman_seasons=False))
check("a unit the row already has is left alone",
      "birth_year" not in out and "birth_month" not in out and "weight" not in out and out["height"] == 79)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
