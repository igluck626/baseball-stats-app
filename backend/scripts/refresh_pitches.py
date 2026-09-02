"""Recompute `pitching_gamelogs.pitches` from Retrosheet's own event files.

WHY THIS EXISTS. The column was ingested from the Chadwick retrosplits daybyday
aggregate (`P_PITCH`), and that aggregate is wrong in five seasons:

  * 2023 and 2025 — badly LOW, and scattered. Agreement with the event files is
    44.7% and 32.0%. In 2025, 97.9% of STARTER rows are wrong by a mean of 13.6
    pitches, worst -46; 93-95% of games have some pitchers right and some wrong,
    so no team, pitcher or game-level exception list helps. Measured against a
    fresh download, so re-ingesting the same source does not fix it.
  * 2000, 2001, 2002 — HIGH by a median of 3. When a plate appearance is split
    across two play records the second REPEATS the first's pitches as its
    prefix, and in those three seasons the aggregator summed both. Our shipped
    value matches that double-counting in 99.6-99.9% of pitcher-games and the
    correct one in 76%. Clemens's 2002 is inflated by 108 pitches.

Recomputing straight from the event files fixes all five and leaves the healthy
seasons alone: across 1988-2025 the recount agrees with what we already hold in
97.5-99.9% of pitcher-games in every season other than those five, and where
the two disagree BallDontLie — a third, independent opinion — sides with this
recount 84.5% of the time against 6.5% for the daybyday.

SCOPE IS 1988-2025 AND THAT IS NOT A COMPROMISE. Pitch sequences effectively
begin in 1988 (98.5% of that season's games carry them; under 7.3% of any
earlier season's do), and the daybyday's own `pitches` column is only 1-6%
populated before then, drawn from the same scattered files. There is no
daybyday-only pitch data in the earlier years for this pass to destroy.

Usage:
    DATABASE_URL=... python -m scripts.refresh_pitches --events /path/to/seasons
    ... --from 2023 --to 2025          # a subset
    ... --dry-run                      # count what would change, write nothing

`--events` is the directory holding one subdirectory per season of Retrosheet
event files (`2023/2023NYN.EVN`, ...). Nothing is downloaded: these files are
large, versioned, and the whole point of this pass is to read the source rather
than a redistribution of it.
"""

import argparse
import csv
import glob
import logging
import os
import sys
from collections import defaultdict

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

from sqlalchemy import inspect as _sa_inspect, text as _sa_text        # noqa: E402

from database import connection, crud                                  # noqa: E402

_BRIDGE = os.path.join(_BACKEND_DIR, "data", "retrosheet",
                       "chadwick_retro_bridge.csv")

# The first season whose event files carry pitch sequences at all. Below this
# the files exist and the pitch field is empty, which is not the same thing as
# a zero — see the module docstring.
FIRST_PITCH_SEASON = 1988

# Rows per UPDATE. Deliberately its own constant, NOT the ingest's `_BATCH`:
# that one is sized for a multi-column INSERT of the whole gamelog row, while
# this statement carries three parallel arrays of scalars and is bounded by the
# PK lookup rather than by row width.
_BATCH = 5000

# Retrosheet's pitch-sequence alphabet. Everything absent from it is a marker
# rather than a pitch: '.' (a play not involving the batter), '1' / '2' / '3'
# (a pickoff throw to a base), '+' (a pickoff throw by the catcher), '>' (a
# runner going), '*' (the NEXT pitch was blocked). 'N' is an explicit no-pitch
# and 'V' a ball charged without one being thrown, so neither counts either.
PITCH_CHARS = set("BCFHIKLMOPQRSTUXY")

log = logging.getLogger("refresh_pitches")


# ---------------------------------------------------------------------------
# Event-file parsing
# ---------------------------------------------------------------------------

def parse_event_file(path: str) -> dict:
    """`{game_key: ({retro_person: pitches}, info_pitches)}` for one .EV* file.

    `info_pitches` is the game's own declaration of whether its pitch data is
    complete ("pitches") or absent ("none"). A game that says "none" is not
    offered to the database at all — its pitchers keep whatever they carry.
    """
    games: dict = {}
    gid = None
    cur: dict = {}
    pitches: dict = {}
    info_pitches = None
    prev_batter = None
    prev_seq = ""

    def flush():
        if gid is not None:
            games[gid] = (dict(pitches), info_pitches)

    for line in open(path, encoding="latin-1"):
        f = line.rstrip("\r\n").split(",")
        t = f[0]
        if t == "id":
            flush()
            gid = f[1]
            cur = {}
            pitches = defaultdict(int)
            info_pitches = None
            prev_batter, prev_seq = None, ""
        elif t == "info" and len(f) > 2 and f[1] == "pitches":
            info_pitches = f[2]
        elif t in ("start", "sub") and len(f) > 5 and f[5].strip() == "1":
            # `side` is the team the player is ON (0 away, 1 home), so he
            # pitches to the OTHER side's batters. Keyed by the batting side
            # the play records carry, so the lookup below is direct.
            cur[1 - int(f[3])] = f[1]
        elif t == "play" and len(f) > 6:
            p = cur.get(int(f[2]))
            seq = f[5]
            # ⚠️ AN `NP` RECORD IS NOT AN EMPTY ONE. When a pitcher is pulled
            # mid-batter, Retrosheet closes his line with an `NP` record
            # carrying the pitches he HAD thrown, and the reliever's record
            # then repeats them as its cumulative prefix. Skipping `NP` loses
            # those pitches from the departing pitcher AND, because the skip
            # leaves the continuation state stale, charges them to the
            # reliever. In 2024, 37,652 `NP` records carry a sequence.
            #
            # ⚠️ AND THE PREFIX TEST COMPARES PITCHES, NOT RAW STRINGS. The
            # repeat is a repeat of the PITCHES; the markers around them are
            # not stable. Aceves closes a plate appearance with `C` and Logan's
            # finishing record reads `.C.FS` — same pitch prefix, but
            # `".C.FS".startswith("C")` is false, so a raw comparison charged
            # Logan for a called strike he never threw (Boston, 2010-05-08).
            pit_seq = [c for c in seq if c in PITCH_CHARS]
            pit_prev = [c for c in prev_seq if c in PITCH_CHARS]
            cont = (f[3] == prev_batter and pit_prev
                    and pit_seq[:len(pit_prev)] == pit_prev)
            tail = pit_seq[len(pit_prev):] if cont else pit_seq
            # An empty sequence on the SAME batter is a substitution marker,
            # not the end of his plate appearance — hold the state, or the
            # record that finishes the PA loses its prefix and counts twice.
            if seq or f[3] != prev_batter:
                prev_batter, prev_seq = f[3], seq
            if p is None:
                continue
            pitches[p] += len(tail)
    flush()
    return games


def parse_season(events_dir: str, year: int) -> dict:
    out: dict = {}
    for path in sorted(glob.glob(os.path.join(events_dir, str(year), "*.EV*"))):
        out.update(parse_event_file(path))
    return out


# ---------------------------------------------------------------------------
# ⚠️ THE ONE CLASS THIS PASS CANNOT GET RIGHT, RECORDED SO NOBODY RE-DERIVES IT
#
# A pitcher pulled MID-BATTER where Retrosheet's closing `NP` record does not
# record how many pitches he had thrown. The reliever's record then carries the
# whole cumulative sequence and there is nothing in the file that says where the
# handover fell, so every pitch of that plate appearance lands on the reliever.
#
# Milwaukee at St. Louis, 2010-07-04: Gallardo is pulled having faced Greene,
# his closing record reads `cnt=00 seq=` (empty), and Villanueva's finishing
# record reads `SFB.BBX`. This pass gives Villanueva all six; BallDontLie splits
# them three and three. The daybyday gives Villanueva three and Gallardo none of
# them, losing three pitches from the game altogether.
#
# IT IS BOUNDED AND IT IS THE ONLY ONE LEFT. Of the 11 pitcher-games in the
# healthy-season audit where this recount still disagrees with BOTH other
# sources, 11 of 11 are this. It is an ATTRIBUTION question, not a counting one
# — every pitch is counted exactly once, it just lands on one side of a
# pitching change or the other — which is why the whole-game totals still match
# BallDontLie in 96-97% of games while the per-pitcher rows match in ~98.5%.
#
# Do not "fix" it by splitting on the `.` marker in the reliever's sequence.
# That marker means "a play not involving the batter" and a pitching change is
# only sometimes what it marks; the file does not distinguish them.
# ---------------------------------------------------------------------------


def load_bridge() -> dict:
    """Retrosheet person key -> MLBAM player_id, the same committed bridge the
    gamelog ingest keys on, so the ids here address the rows that ingest
    wrote."""
    bridge = {}
    with open(_BRIDGE, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("key_retro") and r.get("key_mlbam"):
                try:
                    bridge[r["key_retro"]] = int(r["key_mlbam"])
                except (TypeError, ValueError):
                    continue
    return bridge


def build_rows(events_dir: str, year: int, bridge: dict) -> tuple:
    """Rows ready for `bulk_update_pitch_counts`, plus what was dropped and why.

    Games declaring `info,pitches,none` are skipped whole: we have no count for
    them and NULL is the honest value, not 0.
    """
    games = parse_season(events_dir, year)
    by_key: dict = {}
    stats = {"games": len(games), "games_no_pitch_data": 0,
             "unbridged": 0, "collisions": 0}
    for gk, (per_pitcher, info) in games.items():
        if info != "pitches":
            stats["games_no_pitch_data"] += 1
            continue
        gid = "retro-" + gk
        for person, n in per_pitcher.items():
            mlbam = bridge.get(person)
            if mlbam is None:
                stats["unbridged"] += 1
                continue
            key = (mlbam, gid)
            if key in by_key and by_key[key] != n:
                # Two Retrosheet persons bridging to one MLBAM id in one game.
                # Reported, never silently resolved — an `UPDATE ... FROM` with
                # a duplicate key applies both in an unspecified order and
                # still reports success.
                stats["collisions"] += 1
                continue
            by_key[key] = n
    rows = [{"player_id": pid, "game_id": gid, "pitches": n}
            for (pid, gid), n in by_key.items()]
    return rows, stats


# ---------------------------------------------------------------------------
# Write + read-back
# ---------------------------------------------------------------------------

def assert_column_exists() -> None:
    """`Base.metadata.create_all` CREATES TABLES; it does not ALTER them. If
    `pitches` is missing on a deployment this pass writes nothing and there is
    no error to read, so check for it up front and say so plainly."""
    cols = {c["name"] for c in
            _sa_inspect(connection._engine).get_columns("pitching_gamelogs")}
    if "pitches" not in cols:
        raise SystemExit(
            "pitching_gamelogs has no `pitches` column on this database. "
            "create_all() will not add it — add the column, then re-run.")


def coverage(year: int) -> dict:
    """What the table actually holds for a season, read back after the write.
    The writer's own return value is not evidence: a batch can report success
    having matched nothing."""
    with connection.get_session() as db:
        row = db.execute(_sa_text(
            "SELECT count(*) AS n, "
            "       count(pitches) AS with_pitches, "
            "       COALESCE(sum(pitches), 0) AS total "
            "FROM pitching_gamelogs WHERE season = :y"
        ), {"y": year}).one()
    return {"rows": int(row.n), "with_pitches": int(row.with_pitches),
            "total": int(row.total)}


def run(events_dir: str, year_from: int, year_to: int,
        dry_run: bool = False) -> list[dict]:
    if year_from < FIRST_PITCH_SEASON:
        log.warning("%d is before %d — those event files carry no pitch "
                    "sequences and nothing would be offered.",
                    year_from, FIRST_PITCH_SEASON)
    if not dry_run:
        assert_column_exists()
    bridge = load_bridge()
    results = []
    for year in range(year_from, year_to + 1):
        rows, stats = build_rows(events_dir, year, bridge)
        if not rows:
            log.warning("%d: no rows built (events dir has %d games) — skipped",
                        year, stats["games"])
            continue
        before = coverage(year)
        offered = matched = changed = 0
        if dry_run:
            # Count what WOULD change without writing, by the same comparison
            # the writer makes.
            with connection.get_session() as db:
                for i in range(0, len(rows), _BATCH):
                    chunk = rows[i:i + _BATCH]
                    m, c = db.execute(_sa_text(
                        "WITH v AS (SELECT unnest(CAST(:pids AS integer[])) AS player_id, "
                        "                  unnest(CAST(:gids AS text[]))    AS game_id, "
                        "                  unnest(CAST(:pcs  AS integer[])) AS pitches) "
                        "SELECT count(*), "
                        "       count(*) FILTER (WHERE g.pitches IS DISTINCT FROM v.pitches) "
                        "FROM v JOIN pitching_gamelogs g "
                        "  ON g.player_id = v.player_id AND g.game_id = v.game_id"
                    ), {"pids": [r["player_id"] for r in chunk],
                        "gids": [r["game_id"] for r in chunk],
                        "pcs":  [r["pitches"] for r in chunk]}).one()
                    offered += len(chunk)
                    matched += int(m)
                    changed += int(c)
        else:
            for i in range(0, len(rows), _BATCH):
                chunk = rows[i:i + _BATCH]
                with connection.get_session() as db:
                    o, m, c = crud.bulk_update_pitch_counts(db, chunk)
                offered += o
                matched += m
                changed += c
        after = coverage(year)
        res = {"year": year, "offered": offered, "matched": matched,
               "changed": changed, "unmatched": offered - matched,
               "before": before, "after": after, **stats}
        results.append(res)
        log.info(
            "%d: offered=%d matched=%d (%.1f%%) changed=%d | coverage %d/%d -> "
            "%d/%d rows with a count | season total %d -> %d%s",
            year, offered, matched, 100 * matched / max(offered, 1), changed,
            before["with_pitches"], before["rows"],
            after["with_pitches"], after["rows"],
            before["total"], after["total"], "  [DRY RUN]" if dry_run else "")
        if stats["collisions"]:
            log.warning("%d: %d bridge collisions skipped", year,
                        stats["collisions"])
        # An UPDATE that matches nothing looks exactly like a season with no
        # changes to make. Say which it was.
        if matched == 0:
            log.error("%d: matched ZERO rows — the join is broken, not the "
                      "season clean. Check game_id prefixing and the bridge.",
                      year)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", required=True,
                    help="directory of Retrosheet event files, one dir per season")
    ap.add_argument("--from", dest="year_from", type=int, default=FIRST_PITCH_SEASON)
    ap.add_argument("--to", dest="year_to", type=int, default=2025)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not connection.db_available():
        raise SystemExit("DATABASE_URL is not configured")
    run(args.events, args.year_from, args.year_to, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
