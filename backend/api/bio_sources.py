"""Bio fields for a player we have season rows for but no bio row.

Precedence (Isaac, 2026-10-01), per unit — a birth date, a death date and a
birthplace each come WHOLE from one source, never stitched from two:
  1. Retrosheet biofile      — when the man has a retro_id
  2. balldontlie player      — current players with no retro_id (the caller
                               supplies the record; this module never fetches)
  3. Lahman People           — only men whose seasons come from Lahman
EXCEPT final_game, which means his last REGULAR-SEASON game: our stored
regular-season game logs first, then Lahman People (Lahman-sourced men only),
else None. Never the biofile — its PLAY LASTGAME includes the postseason (CC
Sabathia: 2019-10-17, ALCS Game 4; his last regular-season game was 2019-09-24).
And only for a man whose career is over: inactive per balldontlie, or with no
bdl_id at all (a historical player). An active player has no final game, even
when his last game was last season.
A unit no source has stays None. Pure: callers load the files and pass rows in.

Biofile dates come as 'MM/DD/YYYY', 'MM/  /YYYY' (month known, day not) or
'YYYY' (year only). Its birth states are full names ("California"); ours are
Lahman's ("CA"), so `place_maps` learns the translation from the men both files
describe instead of a hand-written table.
"""
import re
from collections import Counter, defaultdict
from typing import Optional

FIELDS = ("bats", "throws", "height", "weight",
          "birth_year", "birth_month", "birth_day",
          "birth_city", "birth_state", "birth_country",
          "death_year", "death_month", "death_day",
          "debut", "final_game")

UNITS = (("bats",), ("throws",), ("height",), ("weight",),
         ("birth_year", "birth_month", "birth_day"),
         ("birth_city", "birth_state", "birth_country"),
         ("death_year", "death_month", "death_day"),
         ("debut",), ("final_game",))


def _int(v) -> Optional[int]:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def biofile_date(s) -> Optional[tuple]:
    """(year, month, day), unknown parts None. Anything unparseable -> None."""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}", s):
        return int(s), None, None
    m = re.fullmatch(r"(\d{1,2})?\s*/\s*(\d{1,2})?\s*/\s*(\d{4})", s)
    if not m:
        return None
    y = int(m[3])
    mo = int(m[1]) if m[1] else None
    d = int(m[2]) if m[2] else None
    if (mo is not None and not 1 <= mo <= 12) or (d is not None and not 1 <= d <= 31):
        return None
    return y, mo, d


def _iso(s) -> Optional[str]:
    t = biofile_date(s)
    return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}" if t and t[1] and t[2] else None


def place_maps(biofile: dict, people: list) -> tuple[dict, dict]:
    """(state, country) maps from biofile spellings to Lahman's, by majority
    over every man both files describe (joined on retroID)."""
    states, countries = defaultdict(Counter), defaultdict(Counter)
    for p in people:
        b = biofile.get((p.get("retroID") or "").strip())
        if not b:
            continue
        if (b.get("BIRTH STATE") or "").strip() and (p.get("birthState") or "").strip():
            states[b["BIRTH STATE"].strip()][p["birthState"].strip()] += 1
        if (b.get("BIRTH COUNTRY") or "").strip() and (p.get("birthCountry") or "").strip():
            countries[b["BIRTH COUNTRY"].strip()][p["birthCountry"].strip()] += 1
    return ({k: v.most_common(1)[0][0] for k, v in states.items()},
            {k: v.most_common(1)[0][0] for k, v in countries.items()})


def from_biofile(b: dict, state_map: dict = None, country_map: dict = None) -> dict:
    state_map, country_map = state_map or {}, country_map or {}
    by, bm, bd = biofile_date(b.get("BIRTHDATE")) or (None, None, None)
    dy, dm, dd = biofile_date(b.get("DEATHDATE")) or (None, None, None)
    h = (b.get("HEIGHT") or "").strip()
    hm = re.fullmatch(r"(\d)-(\d{1,2})", h)
    st = (b.get("BIRTH STATE") or "").strip()
    co = (b.get("BIRTH COUNTRY") or "").strip()
    return {
        "bats": (b.get("BATS") or "").strip() or None,
        "throws": (b.get("THROWS") or "").strip() or None,
        "height": int(hm[1]) * 12 + int(hm[2]) if hm else None,
        "weight": _int(b.get("WEIGHT")),
        "birth_year": by, "birth_month": bm, "birth_day": bd,
        "birth_city": (b.get("BIRTH CITY") or "").strip() or None,
        "birth_state": state_map.get(st, st) or None,
        "birth_country": country_map.get(co, co) or None,
        "death_year": dy, "death_month": dm, "death_day": dd,
        "debut": _iso(b.get("PLAY DEBUT")),
        # no final_game: PLAY LASTGAME includes the postseason (module docstring)
    }


def from_lahman(p: dict) -> dict:
    s = lambda k: (p.get(k) or "").strip() or None   # noqa: E731
    return {
        "bats": s("bats"), "throws": s("throws"),
        "height": _int(p.get("height")), "weight": _int(p.get("weight")),
        "birth_year": _int(p.get("birthYear")), "birth_month": _int(p.get("birthMonth")),
        "birth_day": _int(p.get("birthDay")),
        "birth_city": s("birthCity"), "birth_state": s("birthState"), "birth_country": s("birthCountry"),
        "death_year": _int(p.get("deathYear")), "death_month": _int(p.get("deathMonth")),
        "death_day": _int(p.get("deathDay")),
        "debut": s("debut"), "final_game": s("finalGame"),
    }


def merge(existing: dict, candidates: list) -> tuple[dict, dict]:
    """Fill the units `existing` has nothing for, each from the first candidate
    (name, fields) that has any of it. Returns (fields to set, source per field).
    Never proposes a field `existing` already holds."""
    existing = existing or {}
    out, src = {}, {}
    for unit in UNITS:
        if any(existing.get(f) is not None for f in unit):
            continue
        for name, vals in candidates:
            if vals and any(vals.get(f) not in (None, "") for f in unit):
                for f in unit:
                    if vals.get(f) not in (None, ""):
                        out[f], src[f] = vals[f], name
                break
    return out, src


def candidates(*, retro_id: Optional[str], biofile: dict, bdl_fields: Optional[dict],
               lahman_row: Optional[dict], lahman_seasons: bool,
               state_map: dict = None, country_map: dict = None,
               last_regular_game: Optional[str] = None,
               bdl_active: Optional[bool] = None) -> list:
    """The eligible sources for one man, in precedence order. `last_regular_game`
    is the date of his last game in our regular-season game logs ('YYYY-MM-DD'),
    which outranks every source for final_game and only for final_game.
    `bdl_active`: True = active per balldontlie, False = inactive, None = he has
    no bdl_id (historical). No source supplies final_game while he is active."""
    out = []
    if last_regular_game and not bdl_active:
        out.append(("gamelogs", {"final_game": last_regular_game}))
    if retro_id and retro_id in biofile:
        out.append(("biofile", from_biofile(biofile[retro_id], state_map, country_map)))
    if not retro_id and bdl_fields:
        out.append(("balldontlie", {k: v for k, v in bdl_fields.items() if k != "final_game"}))
    if lahman_seasons and lahman_row:
        lahman = from_lahman(lahman_row)
        if bdl_active:
            lahman.pop("final_game", None)
        out.append(("lahman", lahman))
    return out
