#!/usr/bin/env python3
"""Verify Retrosheet postseason totals against Lahman, 1903 onward.

Lahman is a TEST REFERENCE here and nothing else — the app never reads it
for postseason stats. Every player-season Retrosheet produces (via
`retrosheet_postseason.parse_year`) is compared with Lahman's postseason
tables, stat by stat.

⚠️ TWO EXEMPTIONS, each derived from the data and each LISTED, never silent:
  1. NEGRO LEAGUES POSTSEASON (league not AL/NL: NNL, NAL, ECL, NN2...).
     Lahman carries it; retrosplits does not. A documented known gap, pending
     a product decision — not a disagreement.
  2. TRUNCATED LAHMAN SERIES. Where Lahman's most games played for a team in
     a round is FEWER than the games Retrosheet has for that team in that
     round, Lahman is short (2023's ALCS stops at 4 of 7 games), so that
     team-round's players are exempt. Retrosheet's count is the one checked
     against the games actually played.

Needs the downloaded retrosplits files (--cache, as the ingest's dry run
leaves them) and read access to the database ($DATABASE_URL). Not part of the
offline suite for that reason.

With --from-tables the Retrosheet side is read back from the WRITTEN
postseason_batting_gamelogs / postseason_pitching_gamelogs instead of parsed
from the files — the check after `retrosheet_postseason.py --write`.

Run: python backend/tests/verify_postseason_vs_lahman.py --cache DIR [--from 1903 --to 2025] [--from-tables]
"""
import argparse
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(HERE, ".."))
import retrosheet_postseason as rp                                # noqa: E402
import retrosheet_gamelogs as rg                                  # noqa: E402

BAT = ("AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "SO", "SB", "CS")
PIT = ("W", "L", "SV", "H", "ER", "HR", "BB", "SO", "IP_OUTS")
ROUND_OF = (("WC", "WC"), ("DS", "DS"), ("DIV", "DS"), ("CS", "CS"), ("WS", "WS"))


def round_type(lahman_round: str):
    for needle, rt in ROUND_OF:
        if needle in lahman_round:
            return rt
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None)
    ap.add_argument("--from", dest="y0", type=int, default=1903)
    ap.add_argument("--to", dest="y1", type=int, default=2025)
    ap.add_argument("--from-tables", action="store_true",
                    help="read the Retrosheet side from the written postseason tables")
    args = ap.parse_args()
    import psycopg2
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    con.set_session(readonly=True)
    cur = con.cursor()
    bridge = rg._load_bridge()

    counts = collections.Counter()
    stat_miss = collections.Counter()
    examples = collections.defaultdict(list)
    exempt_negro = collections.Counter()
    exempt_trunc = []
    def written(table: str, cols: tuple, year: int) -> list[dict]:
        sel = ", ".join(f'"{c}"' if c not in ("doubles", "triples") else c for c in cols)
        cur.execute(f"SELECT player_id, game_id, team, round, {sel} FROM {table} "
                    "WHERE season=%s AND source='retrosheet'", (year,))
        return [dict(zip(("player_id", "game_id", "team", "round") + cols, r)) for r in cur.fetchall()]

    for year in range(args.y0, args.y1 + 1):
        if args.from_tables:
            bat = written("postseason_batting_gamelogs", BAT, year)
            pit = written("postseason_pitching_gamelogs",
                          ("W", "L", "SV", "H", "ER", "HR", "BB", "SO", "IP"), year)
        else:
            path = os.path.join(args.cache, f"playing-{year}.csv")
            if not os.path.exists(path):
                continue
            teams = os.path.join(args.cache, f"teams-{year}.csv")
            bat, pit, _ = rp.parse_year(year, open(path).read(),
                                        open(teams).read() if os.path.exists(teams) else None, bridge)
        if not bat and not pit:
            continue
        # Retrosheet games per (team, round) — the count Lahman is held to.
        r_games = collections.Counter()
        for gid, team, rnd in {(r["game_id"], r["team"], r["round"]) for r in bat + pit}:
            r_games[(team, rnd)] += 1

        rb = collections.defaultdict(collections.Counter)
        for r in bat:
            for s in BAT:
                rb[r["player_id"]][s] += r[s] or 0
        rpit = collections.defaultdict(collections.Counter)
        for r in pit:
            for s in ("W", "L", "SV", "H", "ER", "HR", "BB", "SO"):
                rpit[r["player_id"]][s] += r[s] or 0
            rpit[r["player_id"]]["IP_OUTS"] += round((r["IP"] or 0) * 3)

        # Truncated team-rounds, judged on BATTING — an everyday player appears
        # in every game, a pitcher never does — and exempted on both sides.
        cur.execute('SELECT team, round, max("G") FROM player_postseason_batting '
                    "WHERE year=%s AND league IN ('AL','NL') GROUP BY 1,2", (year,))
        short = []
        for t, rd, mg in cur.fetchall():
            n = r_games.get((t, round_type(rd or "")), 0)
            if round_type(rd or "") and (mg or 0) < n:
                short.append((t, rd))
                exempt_trunc.append((year, t, rd, mg, n))

        for side, table, stats, retro in (("bat", "player_postseason_batting", BAT, rb),
                                          ("pit", "player_postseason_pitching", PIT, rpit)):
            cols = ", ".join(("round(\"IP\"::numeric * 3)" if s == "IP_OUTS" else
                              (s if s in ("doubles", "triples") else f'"{s}"')) for s in stats)
            cur.execute(f'SELECT player_id, round, team, league, "G", {cols} FROM {table} WHERE year=%s', (year,))
            lah = collections.defaultdict(collections.Counter)
            exempt = set()
            for row in cur.fetchall():
                pid, rnd, team, league, g = row[:5]
                if league not in ("AL", "NL"):
                    exempt_negro[(year, rnd, league)] += 1
                    exempt.add(pid)
                    continue
                for s, v in zip(stats, row[5:]):
                    lah[pid][s] += int(v or 0)
            if short:
                cur.execute(f"SELECT DISTINCT player_id FROM {table} WHERE year=%s AND ({' OR '.join(['(team=%s AND round=%s)'] * len(short))})",
                            (year, *[v for t, rd in short for v in (t, rd)]))
                exempt |= {x[0] for x in cur.fetchall()}
                # ...and from Retrosheet's side: a truncated round can drop a
                # player's Lahman row entirely (2023 Dunning has no ALCS row).
                short_rt = {(t, round_type(rd)) for t, rd in short}
                exempt |= {r["player_id"] for r in bat + pit if (r["team"], r["round"]) in short_rt}
            for pid in set(lah) | {p for p, c in retro.items() if any(c.values())}:
                if pid in exempt:
                    counts[(side, "exempt")] += 1
                    continue
                L, R = lah.get(pid), retro.get(pid)
                if R is None and L is not None and not any(L.values()):
                    # An all-zero Lahman line with no Retrosheet row: from 2022 a
                    # pitcher's empty batting line is dropped by design (the
                    # appearance rule), so this is not a missing player.
                    counts[(side, "empty in Lahman")] += 1
                    continue
                if L is None or R is None:
                    counts[(side, "only in Lahman" if R is None else "only in Retrosheet")] += 1
                    examples[(side, "only in Lahman" if R is None else "only in Retrosheet")].append((year, pid))
                    continue
                counts[(side, "compared")] += 1
                bad = [(s, L[s], R[s]) for s in stats if L[s] != R[s]]
                if bad:
                    counts[(side, "mismatch")] += 1
                    for s, lv, rv in bad:
                        stat_miss[(side, s)] += 1
                        examples[(side, s)].append((year, pid, lv, rv))

    for side in ("bat", "pit"):
        c = counts[(side, "compared")]
        m = counts[(side, "mismatch")]
        print(f"{side}: compared {c}, exact {c - m} ({(c - m) / c:.2%}), mismatched {m}, "
              f"exempt {counts[(side, 'exempt')]}, empty-in-Lahman {counts[(side, 'empty in Lahman')]}, "
              f"only-Lahman {counts[(side, 'only in Lahman')]}, "
              f"only-Retrosheet {counts[(side, 'only in Retrosheet')]}")
        for (sd, s), n in sorted(stat_miss.items()):
            if sd == side:
                print(f"    {s:8} {n:4}   e.g. {examples[(sd, s)][:3]}")
    print("\nEXEMPT — Negro Leagues postseason (known gap):",
          f"{sum(exempt_negro.values())} player-rows in {len(exempt_negro)} series,",
          ", ".join(f"{y} {rnd}/{lg}" for (y, rnd, lg) in sorted(exempt_negro)))
    print("EXEMPT — truncated Lahman team-rounds (Lahman max G < Retrosheet games):")
    for row in sorted(set(exempt_trunc)):
        print("   ", row)
    print("\nonly-in-Lahman examples:", examples[("bat", "only in Lahman")][:8])
    # Known: our Lahman load has no rows for some players whose bbref id carries
    # an apostrophe (o'brija02 ...) — a gap in the TEST reference, listed in full.
    for side in ("bat", "pit"):
        print(f"only-in-Retrosheet ({side}):", examples[(side, "only in Retrosheet")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
