#!/usr/bin/env python3
"""Propose MLBAM ids for unmapped balldontlie players from a fresh Chadwick
register. READ-ONLY: this proposes, it never writes.

⚠️ THE MATCHING RULE (Isaac, 2026-09-30). A balldontlie player is ACCEPTED
only when ALL of these hold:
  1. the name matches the register EXACTLY once accents, case, punctuation
     and a Jr./Sr./II/III/IV suffix are folded away ("J.P. France" = "JP
     France", "José" = "Jose") — first name against `name_first`, last against
     `name_last`, no nicknames or given-name fallbacks;
  2. the FULL birth date matches exactly;
  3. that name + birth date is UNIQUE in the register AND among the balldontlie
     players considered — where balldontlie uniqueness is judged on LAST name
     + birth date, because its duplicate records can differ in first name
     ("Bo" and "Chanteyon" Davidson, 2002-07-05);
  4. the target MLBAM id isn't already mapped to a DIFFERENT bdl_id.
Debut year is a sanity check only: missing on either side, or off by one, is
fine; off by two or more HOLDS. Anything else holds — never guess.

Three bugs the first dry run had, each pinned by a test:
  • the birth date was compared as a TUPLE rendered to a string, so no date
    ever matched — it is compared as an ISO date;
  • a PARTIAL birth date (a None in the tuple) crashed — it is "no birth date";
  • duplicate balldontlie records were only caught within one bucket — any two
    ids sharing a name + birth date are both held, wherever they fell.

WRITING (`--approved FILE --write`): only the ids in FILE ({bdl_id: mlbam}),
each re-checked against a FRESH classification first (`plan_writes`) — an id
that no longer classifies ACCEPT to the same MLBAM id is held, never written.
An existing row with an empty bdl_id gets it stamped (only where bdl_id IS
NULL); a player with no row gets a new bio from balldontlie through
`data_service._insert_bio_for_mlbam`, which never overwrites a bio and makes NO
MLB Stats API call. Every approved id is then read back; stored must equal
submitted or the run exits non-zero. Without --write, --approved prints the
plan and writes nothing.

Usage:
    python backend/scripts/chadwick_catchup.py --register 'DIR/people-*.csv' \
        --players players.json [--fetch] --out proposals.json \
        [--approved approved.json [--write]]
`--fetch` pulls the unmapped players from balldontlie (BDL_KEY, GET only) into
--players; without it the cached file is re-classified. DATABASE_URL is read
read-only for the ids we already map (and written only with --write).
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys
import unicodedata
from typing import Optional

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)

ACCEPT = "ACCEPT"
HOLD_NO_DOB = "HOLD: no balldontlie birth date"
HOLD_NO_MATCH = "HOLD: no register match"
HOLD_REG_DUP = "HOLD: name + birth date not unique in the register"
HOLD_BDL_DUP = "HOLD: duplicate balldontlie record (investigate split stats)"
HOLD_TAKEN = "HOLD: MLBAM id already mapped to a different bdl_id"
HOLD_DEBUT = "HOLD: debut year differs by 2+"
CLASSES = (ACCEPT, HOLD_NO_DOB, HOLD_NO_MATCH, HOLD_REG_DUP, HOLD_BDL_DUP, HOLD_TAKEN, HOLD_DEBUT)


def norm_name(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def iso_dob(raw: Optional[str]) -> Optional[str]:
    """balldontlie's birth date as ISO, or None when missing OR PARTIAL. The
    parser returns (None, None, None) — a truthy tuple — for an unparseable
    date, and zeros for "1991-00-00"; both are "no birth date"."""
    from data_service import _parse_bdl_dob          # the app's own three-format parser
    t = _parse_bdl_dob(raw) if raw else None
    if not t or not all(t):
        return None
    return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}"


def _reg_dob(r: dict) -> Optional[str]:
    try:
        return f"{int(r['birth_year']):04d}-{int(r['birth_month']):02d}-{int(r['birth_day']):02d}"
    except (KeyError, TypeError, ValueError):
        return None


def _reg_debut(r: dict) -> Optional[int]:
    try:
        return int(r.get("mlb_played_first") or "")
    except ValueError:
        return None


def classify(players: list[dict], register: list[dict], held: dict) -> dict:
    """{class: [info]} for each balldontlie player.

    `players`: balldontlie player records (id, first_name, last_name, dob,
    debut_year). `register`: Chadwick rows with a key_mlbam. `held`: MLBAM id
    -> set of bdl_ids our tables already carry for it. Pure."""
    by_key = collections.defaultdict(list)
    for r in register:
        if r.get("key_mlbam"):
            by_key[(norm_name(r.get("name_first")), norm_name(r.get("name_last")), _reg_dob(r))].append(r)

    keyed = []
    for p in players:
        dob = iso_dob(p.get("dob"))
        keyed.append((p, dob, (norm_name(p.get("first_name")), norm_name(p.get("last_name")), dob)))
    # balldontlie duplicates on (last name, birth date): stricter than the
    # register key, since balldontlie's own twin records vary the first name.
    bdl_count = collections.Counter((k[1], k[2]) for _, dob, k in keyed if dob)

    out = {c: [] for c in CLASSES}
    for p, dob, key in keyed:
        info = {"bdl_id": p["id"], "name": f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip(),
                "bdl_dob": dob, "bdl_debut": p.get("debut_year")}
        if dob is None:
            out[HOLD_NO_DOB].append(info)
            continue
        cands = by_key.get(key, [])
        if not cands:
            out[HOLD_NO_MATCH].append(info)
            continue
        if len({c["key_mlbam"] for c in cands}) > 1:
            info["candidates"] = sorted({c["key_mlbam"] for c in cands})
            out[HOLD_REG_DUP].append(info)
            continue
        r = cands[0]
        mlbam = int(r["key_mlbam"])
        rdeb = _reg_debut(r)
        info.update(mlbam=mlbam, register_name=f"{r.get('name_first')} {r.get('name_last')}",
                    register_debut=rdeb)
        if bdl_count[(key[1], key[2])] > 1:
            info["others"] = sorted(q["id"] for q, _, k in keyed
                                    if (k[1], k[2]) == (key[1], key[2]) and q["id"] != p["id"])
            out[HOLD_BDL_DUP].append(info)
            continue
        other = {int(b) for b in held.get(mlbam, set()) if b not in (None, "") and int(b) != p["id"]}
        if other:
            info["held_by"] = sorted(other)
            out[HOLD_TAKEN].append(info)
            continue
        bdeb = p.get("debut_year")
        if rdeb is not None and bdeb is not None and abs(rdeb - int(bdeb)) >= 2:
            out[HOLD_DEBUT].append(info)
            continue
        info["debut_check"] = ("missing" if rdeb is None or bdeb is None
                               else "exact" if rdeb == int(bdeb) else "±1")
        info["already_in_db"] = mlbam in held
        out[ACCEPT].append(info)
    return out


STAMP = "stamp"                    # existing row, empty bdl_id
CREATE = "create"                  # no row: new bio from balldontlie
ALREADY = "already mapped"         # nothing to do: our row already has this bdl_id
HOLD_GONE = "hold: no longer ACCEPT"
HOLD_CHANGED = "hold: classifies to a different MLBAM id"
HOLD_ELSEWHERE = "hold: bdl_id already on a different player"


def plan_writes(approved: dict, accepted: dict, mapped_to: dict, rows: dict) -> list[dict]:
    """What a write would do for each approved id. Pure.

    `approved`: {bdl_id: mlbam} as approved. `accepted`: the FRESH
    classification's ACCEPT list, {bdl_id: mlbam}. `mapped_to`: {bdl_id:
    set(player_id)} for bdl_ids already on a row. `rows`: {mlbam: set(bdl_id
    or None)} for every player_id with a row.

    ⚠️ Approval is not enough on its own: the world moves between the dry run
    and the write (the nightly mapped eight of these overnight). An id acts
    only if it is still ACCEPT, to the SAME MLBAM id, right now."""
    plan = []
    for bdl_id, mlbam in sorted(approved.items()):
        entry = {"bdl_id": bdl_id, "mlbam": mlbam}
        on = mapped_to.get(bdl_id)
        if on:
            entry["action"] = ALREADY if on == {mlbam} else HOLD_ELSEWHERE
            if entry["action"] == HOLD_ELSEWHERE:
                entry["on"] = sorted(on)
        elif bdl_id not in accepted:
            entry["action"] = HOLD_GONE
        elif accepted[bdl_id] != mlbam:
            entry["action"] = HOLD_CHANGED
            entry["now"] = accepted[bdl_id]
        elif mlbam in rows:
            entry["action"] = STAMP
        else:
            entry["action"] = CREATE
        plan.append(entry)
    return plan


# ── I/O (not under test) ──────────────────────────────────────────────────────

def _bdl_get(path: str, **params) -> dict:
    import urllib.parse
    import urllib.request
    url = f"https://api.balldontlie.io/mlb/v1/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": os.environ["BDL_KEY"]})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception:                          # noqa: BLE001 - retried, then raised
            if attempt == 2:
                raise
    return {}


def _paged(path: str, **params):
    cursor = None
    while True:
        d = _bdl_get(path, per_page=100, **params, **({"cursor": cursor} if cursor else {}))
        yield from d.get("data", [])
        cursor = d.get("meta", {}).get("next_cursor")
        if not cursor:
            return


def fetch_unmapped(mapped: set, season: int) -> list[dict]:
    """Active roster plus anyone with a season_stats row, minus the mapped,
    each as its full /players/{id} record (the list endpoints lack dob)."""
    ids = {p["id"] for p in _paged("players/active") if p["id"] not in mapped}
    ids |= {r["player"]["id"] for r in _paged("season_stats", season=season)
            if r["player"]["id"] not in mapped}
    return [_bdl_get(f"players/{i}").get("data") for i in sorted(ids)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--register", required=True, help="glob of Chadwick people-*.csv")
    ap.add_argument("--players", required=True, help="cached balldontlie players JSON")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--out", required=True)
    ap.add_argument("--approved", help="JSON {bdl_id: mlbam} approved for writing")
    ap.add_argument("--write", action="store_true", help="with --approved: write. Without: plan only")
    args = ap.parse_args()
    if args.write and not args.approved:
        ap.error("--write needs --approved")
    import psycopg2
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    con.set_session(readonly=True)
    cur = con.cursor()
    cur.execute("SELECT player_id, bdl_id FROM players UNION ALL SELECT player_id, bdl_id FROM pitchers")
    held = collections.defaultdict(set)
    mapped = set()
    mapped_to = collections.defaultdict(set)
    for pid, bid in cur.fetchall():
        held[pid].add(bid)
        if bid is not None:
            mapped.add(int(bid))
            mapped_to[int(bid)].add(pid)
    if args.fetch:
        json.dump([p for p in fetch_unmapped(mapped, args.season) if p], open(args.players, "w"))
    players = [p for p in json.load(open(args.players)) if p["id"] not in mapped]
    register = [r for f in glob.glob(args.register) for r in csv.DictReader(open(f, encoding="utf-8"))
                if r.get("key_mlbam")]
    out = classify(players, register, held)
    json.dump(out, open(args.out, "w"), indent=1, default=str)
    print(f"unmapped balldontlie players: {len(players)}")
    for c in CLASSES:
        print(f"  {c}: {len(out[c])}")
    if not args.approved:
        print("DRY RUN — nothing written.")
        return 0

    approved = {int(k): int(v) for k, v in json.load(open(args.approved)).items()}
    accepted = {x["bdl_id"]: x["mlbam"] for x in out[ACCEPT]}
    plan = plan_writes(approved, accepted, mapped_to, held)
    by_action = collections.Counter(e["action"] for e in plan)
    print(f"\napproved: {len(approved)} — " + ", ".join(f"{k}: {n}" for k, n in sorted(by_action.items())))
    creates = [e for e in plan if e["action"] == CREATE]
    for e in plan:
        if e["action"] != CREATE:
            print(f"  {e['action']:<42} bdl {e['bdl_id']:>8} -> {e['mlbam']}"
                  + (f"  {e.get('on') or e.get('now') or ''}" if e["action"].startswith("hold") else ""))
    sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
    import data_service
    for e in creates:
        bio = data_service.fetch_bdl_player_bio(e["bdl_id"]) or {}
        pos = (bio.get("position") or "").strip().lower()
        e["side"] = "pitcher" if pos in data_service._BDL_PITCHER_POSITIONS else "batter"
        e["team"] = bio.get("_team_code")
        print(f"  {CREATE:<42} bdl {e['bdl_id']:>8} -> {e['mlbam']}  {bio.get('name')!r} "
              f"{e['side']} {e['team']}")
    if not args.write:
        print("PLAN ONLY — nothing written (add --write).")
        return 0
    return _write(plan, cur, args.season)


def _write(plan: list[dict], cur, season: int) -> int:
    """Apply the plan, then read every actionable id back. Stored must equal
    submitted: an id counts as stored only if its bdl_id now sits on its
    approved MLBAM id and on no other."""
    sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
    import data_service
    from database import connection
    from sqlalchemy import text

    def counts():
        cur.execute("SELECT (SELECT count(*) FROM players), (SELECT count(*) FROM pitchers), "
                    "(SELECT count(*) FROM players WHERE bdl_id IS NOT NULL), "
                    "(SELECT count(*) FROM pitchers WHERE bdl_id IS NOT NULL)")
        return dict(zip(("players", "pitchers", "players_mapped", "pitchers_mapped"), cur.fetchone()))

    act = [e for e in plan if e["action"] in (STAMP, CREATE)]
    before = counts()
    for e in act:
        with connection.get_session() as db:
            if e["action"] == STAMP:
                n = 0
                for table in ("players", "pitchers"):
                    n += db.execute(text(f"UPDATE {table} SET bdl_id = :b "
                                         "WHERE player_id = :m AND bdl_id IS NULL"),
                                    {"b": e["bdl_id"], "m": e["mlbam"]}).rowcount
                e["result"] = f"stamped {n} row(s)"
            else:
                side = data_service._insert_bio_for_mlbam(
                    db, bdl_id=e["bdl_id"], mlbam_id=e["mlbam"], lahman_code=None,
                    current_year=season, context="Chadwick matcher", team_from_bio=True)
                e["result"] = f"created {side}" if side else "NOT created"
            db.commit()
    cur.connection.rollback()                       # fresh snapshot for the read-back
    after = counts()
    stored = 0
    for e in act:
        cur.execute("SELECT player_id FROM players WHERE bdl_id = %s "
                    "UNION SELECT player_id FROM pitchers WHERE bdl_id = %s", (e["bdl_id"], e["bdl_id"]))
        on = {r[0] for r in cur.fetchall()}
        e["stored"] = on == {e["mlbam"]}
        stored += e["stored"]
        if not e["stored"]:
            print(f"  NOT STORED: bdl {e['bdl_id']} -> {e['mlbam']} ({e['result']}; now on {sorted(on)})")
    for k in before:
        print(f"{k}: before {before[k]}, after {after[k]}, added {after[k] - before[k]}")
    print(f"submitted {len(act)}, stored {stored}")
    if stored != len(act):
        print("⚠️ STORED ≠ SUBMITTED — investigate before trusting the mapping.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
