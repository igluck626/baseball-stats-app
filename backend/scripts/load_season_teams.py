#!/usr/bin/env python3
"""Per-club rows for traded seasons — player_season_teams / pitcher_season_teams —
for the player page's "2 Teams" row and the club rows under it.

THE RULE (Isaac, 2026-10-16; see the per-team split rule in the notes):
  1. Retrosheet's per-team rows, where they add up exactly to our season row;
  2. otherwise Lahman's per-team rows, if THEY add up exactly;
  3. otherwise no breakdown (the season reads "2 Teams" alone).
"Add up" compares only the columns that BOTH the season row and every club row
record. A column one side doesn't record is left out of the check and shows "—"
on the club rows. A missing value is NEVER read as 0.

Only finished seasons are loaded (the current season is checked live, from the
nightly stints, by the endpoint). A row's `verified` (`|`-separated) lists the columns that are
recorded on both sides and add up — the check columns that qualified the season,
plus any other stored column that also adds up (HBP, SF, ... for the rates).

Usage (the database comes from DATABASE_URL):
    load_season_teams.py --dry-run OUT_DIR     read-only: the rows, the coverage
                                                table and checksums, in OUT_DIR
    load_season_teams.py --write DRY_RUN_DIR   the guarded write: one transaction
                                                per table, refused unless the
                                                sources and the rows equal the
                                                dry run's exactly
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAHMAN = os.path.join(HERE, "..", "data", "lahman")
JUDGMENTS = os.path.join(HERE, "..", "data", "lahman_bridge_judgments.json")
LAST_LOADED_SEASON = 2025          # finished seasons only; the current one is live

# The columns a club row shows — what "adds up" is checked on.
CHECK = {
    "bat": ["G", "PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "SB", "CS", "BB", "SO"],
    "pit": ["G", "GS", "CG", "SHO", "GF", "W", "L", "SV", "IP", "H", "R", "ER", "HR", "BB", "IBB"],
}
# Everything stored on a club row (the check columns plus what the rates need).
STORE = {
    "bat": CHECK["bat"] + ["IBB", "HBP", "SH", "SF", "GIDP"],
    "pit": CHECK["pit"] + ["SO", "HBP", "WP", "BK", "BFP", "SH", "SF", "GIDP"],
}
TABLES = {
    "bat": dict(season="player_seasons", stints="player_season_stints", bio="players",
                out="player_season_teams", lahman="Batting.csv",
                lmap={"G": "G", "AB": "AB", "R": "R", "H": "H", "doubles": "2B", "triples": "3B",
                      "HR": "HR", "RBI": "RBI", "SB": "SB", "CS": "CS", "BB": "BB", "SO": "SO",
                      "IBB": "IBB", "HBP": "HBP", "SH": "SH", "SF": "SF", "GIDP": "GIDP"}),
    "pit": dict(season="pitcher_seasons", stints="pitcher_season_stints", bio="pitchers",
                out="pitcher_season_teams", lahman="Pitching.csv",
                lmap={"G": "G", "GS": "GS", "CG": "CG", "SHO": "SHO", "GF": "GF", "W": "W", "L": "L",
                      "SV": "SV", "IP": "IPouts", "H": "H", "R": "R", "ER": "ER", "HR": "HR", "BB": "BB",
                      "IBB": "IBB", "SO": "SO", "HBP": "HBP", "WP": "WP", "BK": "BK", "BFP": "BFP",
                      "SH": "SH", "SF": "SF", "GIDP": "GIDP"}),
}


# ---------------------------------------------------------------------------
# The rule — pure functions
# ---------------------------------------------------------------------------

def num(v):
    """A stored or CSV value as an int, or None when it isn't recorded. Never 0 for missing."""
    if v is None or v == "":
        return None
    return int(round(float(v)))


def outs(ip):
    """Decimal-thirds innings (6.333) as outs; None stays None."""
    return None if ip is None else int(round(float(ip) * 3))


def compared(rows, season, cols):
    """The columns recorded on both sides: non-missing in the season row and in every club row."""
    return [c for c in cols if season.get(c) is not None and all(r.get(c) is not None for r in rows)]


def adds_up(rows, season, cols):
    """True when the club rows add up exactly on every compared column (and at least one is compared)."""
    comp = compared(rows, season, cols)
    return bool(comp) and all(sum(r[c] for r in rows) == season[c] for c in comp)


def verified(rows, season, cols):
    """Each column recorded on both sides that adds up — the ones a club row may show."""
    return [c for c in compared(rows, season, cols) if sum(r[c] for r in rows) == season[c]]


def choose(role, season, retro_rows, lahman_rows):
    """('retrosheet' | 'lahman' | None, the club rows) for one traded season."""
    if len(retro_rows) > 1 and adds_up(retro_rows, season, CHECK[role]):
        return "retrosheet", retro_rows
    if len(lahman_rows) > 1 and adds_up(lahman_rows, season, CHECK[role]):
        return "lahman", lahman_rows
    return None, []


def era(year):
    return ("1871-1897" if year < 1898 else "1898-1919" if year < 1920 else "1920-1949" if year < 1950
            else "1950-1979" if year < 1980 else "1980-1999" if year < 2000
            else "2000-2025" if year < 2026 else str(year))


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _q(cols):
    return ", ".join(c if c in ("doubles", "triples") else f'"{c}"' for c in cols)


def _row(cols, values, role):
    out = {c: num(v) for c, v in zip(cols, values)}
    if role == "pit" and "IP" in out:
        out["IP"] = outs(values[cols.index("IP")])
    return out


def lahman_bridge():
    """Lahman playerID -> our player id: lahman_load's judged bridge, plus the
    hand judgments in data/lahman_bridge_judgments.json for ids the judge holds.
    Never People's bbrefID: along a few chains Lahman shifted it onto the next
    man, so keying by it files one man's club rows under his namesake."""
    sys.path.insert(0, HERE)
    import lahman_load
    bridge = dict(lahman_load._load_chadwick_bridge())
    for j in json.load(open(JUDGMENTS))["judgments"]:
        have = bridge.get(j["playerID"])
        if have is not None and have != j["mlbam"]:
            raise SystemExit(f"judgment {j['playerID']} -> {j['mlbam']} contradicts the judged bridge ({have})")
        bridge[j["playerID"]] = j["mlbam"]
    return bridge


def load_sources(cur, role, bridge=None):
    t = TABLES[role]
    store = STORE[role]
    bridge = lahman_bridge() if bridge is None else bridge
    cur.execute(f"SELECT s.player_id, b.bbref_id, s.year, s.source, {_q(store)} "
                f"FROM {t['season']} s JOIN {t['bio']} b USING (player_id) ORDER BY s.player_id, s.year")
    seasons = {}
    for r in cur.fetchall():
        seasons[(r[0], r[2])] = (r[1], r[3], _row(store, r[4:], role))
    cur.execute(f"SELECT player_id, year, team, stint_order, source, {_q(store)} "
                f"FROM {t['stints']} ORDER BY player_id, year, stint_order, team")
    stints = collections.defaultdict(list)
    for r in cur.fetchall():
        row = _row(store, r[5:], role)
        row.update(_team=r[2], _order=r[3], _source=r[4])
        stints[(r[0], r[1])].append(row)
    lahman = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(LAHMAN, t["lahman"]))):
        pid = bridge.get(r["playerID"])
        if pid is None or (pid, int(r["yearID"])) not in seasons:
            continue
        row = {c: num(r[lc]) for c, lc in t["lmap"].items()}
        row["PA"] = None if role == "bat" else row.get("PA")
        row.update(_team=r["teamID"], _order=int(r["stint"]), _source="lahman")
        lahman[(pid, int(r["yearID"]))].append(row)
    return seasons, stints, lahman


def build(cur):
    """Every role's rows to store, and the coverage table, from the current sources."""
    rows, coverage = {}, {}
    bridge = lahman_bridge()
    for role in ("bat", "pit"):
        seasons, stints, lahman = load_sources(cur, role, bridge)
        cov = collections.defaultdict(collections.Counter)
        out = []
        traded = {k for k, v in stints.items() if len(v) > 1} | {k for k, v in lahman.items() if len(v) > 1}
        for key in sorted(traded):
            if key not in seasons:
                continue
            _bb, _src, season = seasons[key]
            if role == "bat" and not (season.get("AB") or season.get("PA")):
                continue                     # a pitcher's empty batting row: nothing to split
            c = cov[era(key[1])]
            c["traded"] += 1
            source, clubs = choose(role, season, stints.get(key, []), lahman.get(key, []))
            if source == "retrosheet" and key[1] > LAST_LOADED_SEASON:
                source = "nightly"
            c[source or "none"] += 1
            if source not in ("retrosheet", "lahman") or key[1] > LAST_LOADED_SEASON:
                continue
            ok = verified(clubs, season, STORE[role])
            for i, club in enumerate(sorted(clubs, key=lambda r: (r["_order"] or 0, r["_team"])), 1):
                rec = {"player_id": key[0], "year": key[1], "stint_order": i, "team": club["_team"],
                       "source": source, "verified": "|".join(ok)}
                for col in STORE[role]:
                    v = club.get(col)
                    rec[col] = (round(v / 3.0, 3) if v is not None else None) if col == "IP" else v
                out.append(rec)
        rows[role], coverage[role] = out, cov
    return rows, coverage


def canonical(rows, role):
    """The rows as sorted CSV text — what the checksum is taken over."""
    cols = ["player_id", "year", "stint_order", "team", "source", "verified"] + STORE[role]
    lines = [",".join(cols)]
    for r in sorted(rows, key=lambda r: (r["player_id"], r["year"], r["stint_order"])):
        lines.append(",".join("" if r[c] is None else str(r[c]) for c in cols))
    return "\n".join(lines) + "\n"


def source_checksums(cur):
    """A hash of every source row the rows depend on (finished seasons), plus the Lahman files."""
    out = {}
    for role, t in TABLES.items():
        store = STORE[role]
        for name, tbl, extra in (("season", t["season"], "source"), ("stints", t["stints"], "team, stint_order, source")):
            cur.execute(f"SELECT player_id, year, {extra}, {_q(store)} FROM {tbl} "
                        f"WHERE year <= {LAST_LOADED_SEASON} ORDER BY 1, 2, 3")
            h = hashlib.sha256()
            for r in cur.fetchall():
                h.update(repr(tuple(r)).encode())
            out[f"{role}_{name}"] = h.hexdigest()
    for f in ("People.csv", "Batting.csv", "Pitching.csv", "chadwick_mlb.csv"):
        out[f] = hashlib.sha256(open(os.path.join(LAHMAN, f), "rb").read()).hexdigest()
    out["lahman_bridge_judgments.json"] = hashlib.sha256(open(JUDGMENTS, "rb").read()).hexdigest()
    return out


def coverage_text(coverage):
    lines = []
    for role in ("bat", "pit"):
        lines.append(f"== {role}")
        for e in sorted(coverage[role]):
            c = coverage[role][e]
            t = c["traded"]
            cov = c["retrosheet"] + c["nightly"] + c["lahman"]
            lines.append(f"  {e}: traded {t} | retrosheet {c['retrosheet']} nightly {c['nightly']} "
                         f"lahman {c['lahman']} none {c['none']} | covered {100 * cov / max(t, 1):.1f}%")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Dry run and the guarded write
# ---------------------------------------------------------------------------

def _connect(readonly):
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.set_session(readonly=readonly, autocommit=False)
    return conn


def dry_run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    conn = _connect(readonly=True)
    cur = conn.cursor()
    rows, coverage = build(cur)
    manifest = {"sources": source_checksums(cur), "tables": {}}
    for role in ("bat", "pit"):
        text = canonical(rows[role], role)
        path = os.path.join(out_dir, TABLES[role]["out"] + ".csv")
        open(path, "w").write(text)
        manifest["tables"][TABLES[role]["out"]] = {
            "rows": len(rows[role]), "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "seasons": len({(r["player_id"], r["year"]) for r in rows[role]}),
            "by_source": dict(collections.Counter(r["source"] for r in rows[role]))}
    open(os.path.join(out_dir, "coverage.txt"), "w").write(coverage_text(coverage))
    json.dump(manifest, open(os.path.join(out_dir, "manifest.json"), "w"), indent=1, sort_keys=True)
    conn.close()
    print(coverage_text(coverage))
    print(json.dumps(manifest["tables"], indent=1, sort_keys=True))


DDL = {
    "bat": """CREATE TABLE IF NOT EXISTS player_season_teams (
        player_id integer NOT NULL, year integer NOT NULL, stint_order integer NOT NULL,
        team varchar NOT NULL, source varchar NOT NULL, verified varchar NOT NULL,
        "G" integer, "PA" integer, "AB" integer, "R" integer, "H" integer, doubles integer,
        triples integer, "HR" integer, "RBI" integer, "SB" integer, "CS" integer, "BB" integer,
        "SO" integer, "IBB" integer, "HBP" integer, "SH" integer, "SF" integer, "GIDP" integer,
        PRIMARY KEY (player_id, year, stint_order))""",
    "pit": """CREATE TABLE IF NOT EXISTS pitcher_season_teams (
        player_id integer NOT NULL, year integer NOT NULL, stint_order integer NOT NULL,
        team varchar NOT NULL, source varchar NOT NULL, verified varchar NOT NULL,
        "G" integer, "GS" integer, "CG" integer, "SHO" integer, "GF" integer, "W" integer,
        "L" integer, "SV" integer, "IP" double precision, "H" integer, "R" integer, "ER" integer,
        "HR" integer, "BB" integer, "IBB" integer, "SO" integer, "HBP" integer, "WP" integer,
        "BK" integer, "BFP" integer, "SH" integer, "SF" integer, "GIDP" integer,
        PRIMARY KEY (player_id, year, stint_order))""",
}


def write(dry_dir):
    """One transaction per table. Refuses unless the table is empty, the sources hash
    to what the dry run saw, and the rows rebuilt now equal the dry run's exactly;
    reads the rows back inside the transaction before committing."""
    manifest = json.load(open(os.path.join(dry_dir, "manifest.json")))
    for role in ("bat", "pit"):
        table = TABLES[role]["out"]
        conn = _connect(readonly=False)
        cur = conn.cursor()
        try:
            if source_checksums(cur) != manifest["sources"]:
                raise SystemExit(f"{table}: the sources changed since the dry run — nothing written")
            rows, _ = build(cur)
            text = canonical(rows[role], role)
            want = open(os.path.join(dry_dir, table + ".csv")).read()
            if text != want:
                raise SystemExit(f"{table}: the rows rebuilt now differ from the dry run's — nothing written")
            cur.execute(DDL[role])
            cur.execute(f"SELECT count(*) FROM {table}")
            if cur.fetchone()[0] != 0:
                raise SystemExit(f"{table} is not empty — nothing written")
            cols = ["player_id", "year", "stint_order", "team", "source", "verified"] + STORE[role]
            qcols = ", ".join(c if c in ("player_id", "year", "stint_order", "team", "source", "verified",
                                         "doubles", "triples") else f'"{c}"' for c in cols)
            from psycopg2.extras import execute_values
            execute_values(cur, f"INSERT INTO {table} ({qcols}) VALUES %s",
                           [tuple(r[c] for c in cols) for r in rows[role]], page_size=1000)
            cur.execute(f"SELECT {qcols} FROM {table}")
            back = [dict(zip(cols, r)) for r in cur.fetchall()]
            if canonical(back, role) != want:
                raise SystemExit(f"{table}: read-back inside the transaction differs — rolled back")
            conn.commit()
            print(f"{table}: {len(back)} rows written and read back inside the transaction")
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", metavar="OUT_DIR")
    g.add_argument("--write", metavar="DRY_RUN_DIR")
    a = ap.parse_args()
    if a.dry_run:
        dry_run(a.dry_run)
    else:
        write(a.write)
