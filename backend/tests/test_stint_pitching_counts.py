#!/usr/bin/env python3
"""The nightly's 2026 per-team (stint) pitching lines carry starts, complete
games, shutouts, games finished and balks (Isaac, 2026-10-27).

⚠️ THE CHECKS THAT MATTER:
  • A 2026 traded pitcher's club rows showed "—" for GS, CG, SHO, GF and BK on
    the player page, though the season row had them: the stint derivation never
    summed them, so the per-team split had nothing to verify. The game logs have
    carried all five since the 2026-10-22 backfill.
  • Skubal (DET → LAN, 26 starts): each club's GS, CG, SHO, GF and BK, summed
    from his game logs, add up to his season row, so /season-teams sends them.
  • One unknown game makes the club's value unknown, never 0 (the activity
    rule): Hoby Milner's one game with GS and BK unknown leaves that club's
    GS and BK null in the stints; his CG, SHO, GF are still numbers.
  • Milner's season row says 44 games, his logs hold 45 (CHN 39 + SEA 6), so
    his 2026 has no exact breakdown — the clubs alone, every value null — as
    it already does in production. Unchanged by this fix.
  • IBB stays null: the pitching game logs don't carry it. BF is null here
    because these fixture rows predate the BF column (test_bfp_gamelog covers it).

Real rows (fixtures/stint_pitching_counts.json, read from production): both
pitchers' 2026 pitching logs, one row from the other side of each game, their
season rows. Needs an embedded Postgres (`pip install pgserver`); FAILS without it.

Standalone, no pytest. Run: <backend python> backend/tests/test_stint_pitching_counts.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.pop("DATABASE_URL", None)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


FX = json.load(open(os.path.join(HERE, "fixtures", "stint_pitching_counts.json")))
SKUBAL, MILNER = 669373, 571948
NEW = ("GS", "CG", "SHO", "GF", "BK")

try:
    import pgserver
except ImportError:
    pgserver = None
if pgserver is None:
    check("pgserver installed (the derivation is Postgres SQL)", False, "pip install pgserver")
else:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="stint_pitching_counts_"), cleanup_mode="stop")
    os.environ["DATABASE_URL"] = srv.get_uri()
    from sqlalchemy import create_engine                            # noqa: E402
    from sqlalchemy.orm import Session                               # noqa: E402
    from database.models import (Base, BattingGameLog, PitchingGameLog,  # noqa: E402
                                 PitcherSeason, PitcherSeasonStint, PitcherSeasonTeam)
    from database import crud                                        # noqa: E402
    import nightly_update as nu                                      # noqa: E402
    import season_teams                                              # noqa: E402
    eng = create_engine(srv.get_uri())
    Base.metadata.create_all(eng, tables=[m.__table__ for m in (BattingGameLog, PitchingGameLog, PitcherSeason,
                                                                PitcherSeasonStint, PitcherSeasonTeam)])
    with eng.begin() as c:
        for m, rows in ((PitchingGameLog, FX["own_pitching"]), (BattingGameLog, FX["opponent_rows"]),
                        (PitcherSeason, FX["pitcher_seasons"])):
            cols = {col.name for col in m.__table__.columns}
            for r in rows:
                c.execute(m.__table__.insert().values({k: v for k, v in r.items() if k in cols}))
    season = {r["player_id"]: r for r in FX["pitcher_seasons"]}

    print("the stint derivation")
    with Session(eng) as db:
        pit, unmapped = nu._derive_stints(db, 2026, True)
    by = {}
    for r in pit:
        by.setdefault(r["player_id"], []).append(r)
    sk = sorted(by.get(SKUBAL, []), key=lambda r: r["stint_order"])
    check("Skubal: two club rows, DET then LAN", [r["team"] for r in sk] == ["DET", "LAN"], [r["team"] for r in sk])
    check("Skubal: each club row carries GS, CG, SHO, GF, BK (they were missing)",
          sk and all(r.get(c) is not None for r in sk for c in NEW), [{c: r.get(c) for c in NEW} for r in sk])
    check("Skubal: the clubs' GS, CG, SHO, GF, BK add up to his season row",
          all(sum(r[c] for r in sk) == season[SKUBAL][c] for c in NEW),
          ({c: sum(r[c] or 0 for r in sk) for c in NEW}, {c: season[SKUBAL][c] for c in NEW}))
    check("Skubal: 26 starts in 26 games, as his season row says",
          sum(r["GS"] for r in sk) == 26 and sum(r["G"] for r in sk) == 26, [(r["G"], r["GS"]) for r in sk])
    mil = by.get(MILNER, [])
    unknown_game = [g for g in FX["own_pitching"] if g["player_id"] == MILNER and g["GS"] is None]
    check("Milner: one real game with GS and BK unknown", len(unknown_game) == 1 and unknown_game[0]["BK"] is None,
          len(unknown_game))
    check("Milner: the club with that game has GS and BK unknown — never a short 0",
          any(r["GS"] is None and r["BK"] is None for r in mil), [(r["team"], r["GS"], r["BK"]) for r in mil])
    check("Milner: CG, SHO and GF are still numbers on every club row",
          mil and all(r[c] is not None for r in mil for c in ("CG", "SHO", "GF")),
          [(r["team"], r["CG"], r["SHO"], r["GF"]) for r in mil])
    check("no team value went unmapped", not unmapped, unmapped)

    print("what /season-teams sends")
    with Session(eng) as db:
        crud.save_pitcher_season_stints(db, pit)
        db.commit()
        sent = {pid: season_teams.seasons(db, pid, "pit") for pid in (SKUBAL, MILNER)}
    s26 = next((s for s in sent[SKUBAL] if s["year"] == 2026), None)
    check("Skubal 2026 is a traded season from the nightly stints", s26 and s26["source"] == "nightly", s26)
    clubs = s26["teams"] if s26 else []
    check("Skubal: GS, CG, SHO, GF, BK now sent on both club rows (the page showed '—')",
          clubs and all(t["stats"].get(c) is not None for t in clubs for c in NEW),
          [{c: t["stats"].get(c) for c in NEW} for t in clubs])
    check("Skubal: IBB null (not in the game logs); BF null (these fixture rows have none)",
          clubs and all(t["stats"].get("IBB") is None and t["stats"].get("BFP") is None for t in clubs))
    m26 = next((s for s in sent[MILNER] if s["year"] == 2026), None)
    mclubs = m26["teams"] if m26 else []
    g_logs = sum(r["G"] for r in mil)
    check(f"Milner: season G {season[MILNER]['G']} vs {g_logs} in his logs — no exact breakdown, "
          "the clubs alone with every value null (as production sends today)",
          m26 and m26["source"] is None and season[MILNER]["G"] != g_logs
          and [t["team"] for t in mclubs] == ["CHN", "SEA"]
          and all(v is None for t in mclubs for v in t["stats"].values()), m26)
    eng.dispose()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
