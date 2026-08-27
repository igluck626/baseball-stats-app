"""Per-GAME facts for historical games, from Retrosheet's game logs.

A DIFFERENT SOURCE from the daybyday `playing-YYYY.csv` files that fill
batting_gamelogs / pitching_gamelogs. Those are one row per player per game;
`gl{year}.zip` is one row per game, 161 positional fields, no header. It is
where the per-inning linescore and the ballpark live — neither is anywhere in
daybyday, which is why the box score has been drawing no linescore at all.

Runtime download on Railway, same as the gamelog backfill: nothing committed
but the park-code lookup, which is small and static.

THE POSITIONAL READ IS THE RISK. 161 unnamed fields mean an off-by-one silently
returns passed balls where errors were wanted, and nothing complains. The guard
is arithmetic, not care: every parsed linescore must sum to the final score in
the same row, checked per game as it is read, and a year that fails is reported
rather than written.
"""

import csv
import datetime
import io
import logging
import os
import sys
import urllib.error
import urllib.request
import zipfile
from typing import Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from database import connection                                            # noqa: E402
from database.models import RetroGameInfo                                  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert             # noqa: E402

_GL_URL = "https://www.retrosheet.org/gamelogs/gl{year}.zip"
# ⚠️ COMMITTED WITH `git add -f`. `.gitignore` ignores backend/data/retrosheet/*
# wholesale, so this file — like the six alongside it — is only in the repo
# because it was force-added. Add it the ordinary way and git accepts the
# command, says nothing, and ships a build where every park name is null.
_PARKS = os.path.join(_BACKEND_DIR, "data", "retrosheet", "parkcode.csv")
_BATCH = 1000

# Field offsets into the 161-column row. Named here rather than inline so the
# read is checkable against Retrosheet's published layout in one place.
# (Their documentation is 1-indexed; these are 0-indexed.)
F_DATE, F_GAMENUM = 0, 1
F_HOME = 6
F_AWAY_SCORE, F_HOME_SCORE = 9, 10
F_PARK = 16
F_ATTENDANCE, F_TIME = 17, 18
F_AWAY_LINE, F_HOME_LINE = 19, 20
# HP, 1B, 2B, 3B, LF, RF — id then name, so the NAMES are the odd offsets.
F_UMPS = ((78, "ump_hp"), (80, "ump_1b"), (82, "ump_2b"),
          (84, "ump_3b"), (86, "ump_lf"), (88, "ump_rf"))
F_AWAY_HITS, F_HOME_HITS = 22, 50
F_AWAY_LOB, F_HOME_LOB = 37, 65
F_AWAY_ERR, F_HOME_ERR = 45, 73
_MIN_FIELDS = 99

log = logging.getLogger(__name__)


def _i(v) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _attendance(v) -> Optional[int]:
    """NULL for an unrecorded gate, never 0.

    Retrosheet writes BOTH "" and "0" when nobody wrote the figure down — 720
    such games in a nine-year sample, mostly deadball era. "Unrecorded" and
    "nobody came" are different facts and a stored 0 would assert the second.
    Nothing true is lost: the one game genuinely played to an empty park,
    Baltimore on 2015-04-29, carries a blank rather than a zero."""
    n = _i(v)
    return None if n in (None, 0) else n


def _ump(v) -> Optional[str]:
    """Umpire name, or None. Absence in the source is the literal "(none)",
    which stored raw would put a man called "(none)" on the field."""
    v = (v or "").strip()
    if not v or v.lower() in ("(none)", "none", "unknown"):
        return None
    return v


def parse_linescore(s: str) -> list:
    """Retrosheet's per-inning runs: one character per inning, double-digit
    innings parenthesised ("102(10)250x"), and a trailing "x" where the home
    side did not bat because it was already ahead.

    The "x" contributes no inning — a home team that did not bat in the ninth
    played eight half-innings, and inventing a ninth with 0 runs would put a
    zero where the box score prints nothing."""
    out: list = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            j = s.index(")", i)
            out.append(int(s[i + 1:j]))
            i = j + 1
        elif ch in "xX":
            i += 1
        elif ch.isdigit():
            out.append(int(ch))
            i += 1
        else:
            i += 1          # stray character: skip rather than abort the year
    return out


def _load_parks() -> dict:
    """Retrosheet park code -> name. Missing codes are LEFT MISSING: the file
    lags the schedule (BIR01, MEX02 and SEO01 all appear in 2024 logs and not
    in the lookup), and a venue rendered as "SEO01" is worse than a venue the
    view simply omits."""
    if not os.path.exists(_PARKS):
        log.warning("park code file missing at %s — names left null", _PARKS)
        return {}
    with open(_PARKS, newline="", encoding="latin-1") as fh:
        return {r["PARKID"]: (r["NAME"] or "").strip()
                for r in csv.DictReader(fh) if r.get("PARKID")}


def _download(year: int) -> Optional[str]:
    url = _GL_URL.format(year=year)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            blob = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        log.warning("year %d: %s — skipped", year, exc)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if name is None:
                log.warning("year %d: zip holds no .txt", year)
                return None
            return z.read(name).decode("latin-1")
    except zipfile.BadZipFile:
        log.warning("year %d: not a zip", year)
        return None


def ingest_year(year: int, parks: dict) -> dict:
    text = _download(year)
    if text is None:
        return {"year": year, "status": "missing", "games": 0, "linescore_mismatch": 0}

    rows = []
    mismatch = 0
    for r in csv.reader(io.StringIO(text)):
        if len(r) < _MIN_FIELDS:
            continue
        date_s = r[F_DATE].strip()
        home = r[F_HOME].strip()
        if len(date_s) != 8 or not home:
            continue
        gamenum = (r[F_GAMENUM].strip() or "0")
        away_line = r[F_AWAY_LINE].strip()
        home_line = r[F_HOME_LINE].strip()
        away_runs = _i(r[F_AWAY_SCORE])
        home_runs = _i(r[F_HOME_SCORE])

        # THE OFF-BY-ONE DETECTOR. If the positional offsets ever slip, the
        # innings stop summing to the score, and the year is reported instead
        # of written — a wrong linescore is worse than none.
        if away_line and away_runs is not None and sum(parse_linescore(away_line)) != away_runs:
            mismatch += 1
            continue
        if home_line and home_runs is not None and sum(parse_linescore(home_line)) != home_runs:
            mismatch += 1
            continue

        park_id = r[F_PARK].strip()
        umps = {name: (_ump(r[i]) if len(r) > i else None) for i, name in F_UMPS}
        rows.append({
            # Captured, never read — see the warning on `models.RetroGameInfo`.
            "attendance":       _attendance(r[F_ATTENDANCE]),
            "time_of_game_min": _i(r[F_TIME]) or None,
            **umps,
            "game_id":     f"retro-{home}{date_s}{gamenum}",
            "game_date":   datetime.date(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8])),
            "season":      int(date_s[:4]),
            "park_id":     park_id or None,
            "park_name":   parks.get(park_id) or None,
            "away_line":   away_line or None,
            "home_line":   home_line or None,
            "away_runs":   away_runs,
            "home_runs":   home_runs,
            "away_hits":   _i(r[F_AWAY_HITS]),
            "home_hits":   _i(r[F_HOME_HITS]),
            "away_errors": _i(r[F_AWAY_ERR]),
            "home_errors": _i(r[F_HOME_ERR]),
            "away_lob":    _i(r[F_AWAY_LOB]),
            "home_lob":    _i(r[F_HOME_LOB]),
        })

    # THE DOUBLEHEADER COLLAPSE CHECK, made loud. Two halves of a doubleheader
    # differ only in the trailing game number; a key built without it maps both
    # onto one row and ON CONFLICT quietly keeps one. Distinct keys must equal
    # row count, and a year where they do not is reported, not written.
    distinct = len({x["game_id"] for x in rows})
    if distinct != len(rows):
        log.error("year %d: %d rows collapse to %d keys — NOT WRITTEN",
                  year, len(rows), distinct)
        return {"year": year, "status": "key_collision", "games": 0,
                "rows": len(rows), "distinct": distinct,
                "linescore_mismatch": mismatch}

    written = _write(rows)
    log.info("year %d: %d games, %d linescore mismatches", year, written, mismatch)
    return {"year": year, "status": "ok", "games": written,
            "linescore_mismatch": mismatch}


def _write(rows: list) -> int:
    """Upsert on game_id. DO UPDATE rather than DO NOTHING so a re-run actually
    re-writes — the gamelog refresh learned that the hard way, where a re-run
    through a DO NOTHING writer reported every row written and changed none."""
    if not rows:
        return 0
    cols = [c for c in rows[0] if c != "game_id"]
    written = 0
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        with connection.get_session() as db:
            if (db.bind.dialect.name if db.bind is not None else "") == "postgresql":
                stmt = pg_insert(RetroGameInfo).values(chunk)
                db.execute(stmt.on_conflict_do_update(
                    index_elements=["game_id"],
                    set_={c: getattr(stmt.excluded, c) for c in cols},
                ))
            else:
                for r in chunk:
                    db.merge(RetroGameInfo(**r))
        written += len(chunk)
    return written


def run(year_from: int = 1898, year_to: int = 2025, state=None, lock=None) -> dict:
    connection.init_db()
    parks = _load_parks()
    log.info("=" * 52)
    log.info("Retrosheet game-info ingest — %d..%d (%d parks)",
             year_from, year_to, len(parks))
    per_year = []
    total = 0
    for year in range(year_from, year_to + 1):
        if state is not None and lock is not None:
            with lock:
                state["current_year"] = year
        try:
            res = ingest_year(year, parks)
        except Exception as exc:  # noqa: BLE001
            log.exception("year %d FAILED: %s", year, exc)
            res = {"year": year, "status": "error", "error": str(exc),
                   "games": 0, "linescore_mismatch": 0}
        per_year.append(res)
        total += res["games"]
        if state is not None and lock is not None:
            with lock:
                state["games_written"] = total
                state["years_done"] = len(per_year)
    bad = [y for y in per_year if y["status"] != "ok"]
    summary = {"year_from": year_from, "year_to": year_to,
               "games_written": total, "years": per_year,
               "years_not_ok": [y["year"] for y in bad]}
    if state is not None and lock is not None:
        with lock:
            state["phase"] = "done"
            state["summary"] = summary
    log.info("game-info ingest done: %d games, %d years not ok", total, len(bad))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    a = int(sys.argv[1]) if len(sys.argv) > 1 else 1898
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
    run(a, b)
