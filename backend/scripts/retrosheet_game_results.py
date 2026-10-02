"""Load `team_game_results`: every historical regular-season game with each
side's record entering and after it, from Retrosheet's GAME LOGS.

The same `gl{year}.zip` files `retrosheet_gameinfo` reads, downloaded the same
way; the counting rules are `api/game_records.py`'s, so the loader and the
endpoint cannot disagree about what a forfeit or a suspended game is.

DRY RUN BY DEFAULT: parses and reports, writes nothing. `--write` upserts
(DO UPDATE, so a re-run re-writes) and then reads every season's rows back,
comparing stored to submitted — a writer's own row count is not evidence.

Run once a year, with the game-info ingest (docs/annual_retrosheet_runbook.md).
"""
import csv
import io
import logging
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, os.path.join(_BACKEND_DIR, "api"))
sys.path.insert(0, _BACKEND_DIR)
sys.path.insert(0, _SCRIPTS_DIR)

import game_records                                                        # noqa: E402
from retrosheet_gameinfo import _download                                  # noqa: E402

log = logging.getLogger(__name__)
_BATCH = 1000


def build_year(year: int, text: str = None) -> list[dict]:
    """One season's rows with records, from the published file (or `text`)."""
    text = text if text is not None else _download(year)
    if text is None:
        return []
    return game_records.season_from_game_logs(
        [r for r in csv.reader(io.StringIO(text)) if len(r) > game_records.F_HOME_LINE])


def _write(rows: list) -> int:
    from database import connection
    from database.models import TeamGameResult
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    cols = [c for c in rows[0] if c != "game_id"]
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        with connection.get_session() as db:
            stmt = pg_insert(TeamGameResult).values(chunk)
            db.execute(stmt.on_conflict_do_update(
                index_elements=["game_id"], set_={c: getattr(stmt.excluded, c) for c in cols}))
    return len(rows)


def _read_back(season: int, rows: list) -> int:
    """Rows whose stored values differ from what was submitted (0 = all equal)."""
    from database import connection
    from database.models import TeamGameResult
    want = {r["game_id"]: r for r in rows}
    bad = 0
    with connection.get_session() as db:
        stored = db.query(TeamGameResult).filter(TeamGameResult.season == season).all()
        got = {s.game_id: s for s in stored}
        for gid, r in want.items():
            s = got.get(gid)
            if s is None or any(getattr(s, c) != v for c, v in r.items()):
                bad += 1
        bad += len(set(got) - set(want))          # rows stored that this build does not have
    return bad


def run(year_from: int = 1898, year_to: int = 2025, write: bool = False) -> dict:
    per_year = []
    for year in range(year_from, year_to + 1):
        rows = build_year(year)
        res = {"year": year, "games": len(rows)}
        if write and rows:
            res["written"] = _write([{k: v for k, v in r.items() if k != "counts"} for r in rows])
            res["read_back_mismatches"] = _read_back(year, [{k: v for k, v in r.items() if k != "counts"} for r in rows])
        per_year.append(res)
        log.info("%s", res)
    return {"write": write, "years": per_year,
            "games": sum(y["games"] for y in per_year),
            "years_missing": [y["year"] for y in per_year if not y["games"]],
            "read_back_mismatches": sum(y.get("read_back_mismatches", 0) for y in per_year)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    a = int(args[0]) if args else 1898
    b = int(args[1]) if len(args) > 1 else 2025
    print(run(a, b, write="--write" in sys.argv))
