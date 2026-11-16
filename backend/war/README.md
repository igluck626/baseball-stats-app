# Our WAR (version 1)

Wins above replacement computed from our own data. This package holds the calculation, its frozen settings and the
scripts that build its inputs. It never reads the stored Baseball-Reference WAR columns, and
`tests/test_war_v1_package.py` checks that no file here names them.

**Status (2026-11-16): code only.** It doesn't store results, the server doesn't read it yet, and there is no nightly job.
Those are later, separately approved steps.

## What it produces

Results are per player-season, 1871 to the current season. Negro Leagues seasons aren't covered; they're to be hidden.

- **Hitters**
  - The pieces, in runs: batting, baserunning, double plays, replacement, position and fielding.
  - **Offense** = (batting + baserunning + double plays + replacement) ÷ runs per win.
  - **Defense** = (position + fielding) ÷ runs per win.
  - **WAR** = Offense + Defense.
  - Pitchers' own hitting is measured against the average hitting pitcher, with no replacement or position credit.
- **Pitchers**
  - **WAR** = wins above average + replacement wins.
  - Wins above average come from runs allowed compared with what an average pitcher would allow in the same parks and
    league, behind the same defense, converted to wins with a 50/50 blend of linear and Pythagorean wins.

## How each piece works

| Piece | Method | Settings (`params/war_params_v1.json`) |
|---|---|---|
| Batting | Run value of each event, measured from that season's play-by-play; league average = non-pitchers; park-adjusted; reached-on-error credit; small per-era corrections to event values | `bat_delta`, `scalars.ibb_weight`, `roe_weight`, `sf_as_out` |
| Baserunning | Steals and caught stealing, plus extra bases taken and outs on the bases on singles and doubles | `sb_runs`, `cs_runs`, `xb_runs`, `out_on_bases_runs` |
| Double plays | Grounded into double plays vs league rate on chances | `gidp_runs_pbp`, `gidp_runs_season` |
| Replacement | 1,000 WAR per 2,430 games, split hitters/pitchers; spread per plate appearance across both leagues; weaker leagues get less (measured from players who switched leagues) | `hit_rep_share`, `pit_rep_share`, `rep_pool_leagues`, `league_strength_k`, `league_strength_k_1900`, `pit_league_strength_k` |
| Position | Runs per 1,458 innings by position and decade | `positions` |
| Fielding 1910–2025 | Total Zone-style play counting from Retrosheet: plays made vs chances, hits charged to the responsible fielder, park-adjusted; batter × pitcher out pattern; team hits saved on balls in play; arms, double plays, catchers' running game. Seasons from 2003 use the 1989–2002 weights, so every era is on one scale (option B). Each season is re-centered so an average fielder is worth 0. | `fld_retro`, `center_fielding` |
| Fielding, current season | The same idea from balldontlie: ball coordinates, totals, double plays and outfield throws from play text, team hits saved; calibrated to our own Total Zone values for 2015–2025 | `fld_bdl` |
| Fielding without play-by-play | Season totals (putouts, assists, errors, double plays) vs league per game | `fld_totals` |
| Pitchers | League runs allowed per 9 × park, a reliever adjustment, minus the defense behind him (our fielding plus team hits saved) | `reliever_ra9_shift`, `team_der_weight`, `pythag_blend`, `pythag_exponent` |
| Runs per win | 10 × √(runs per inning, both teams) | — |

## Where the settings came from

The settings were tuned once, in November 2026, so that our figures land close to the stored Baseball-Reference ones. Close
here means about 94% of regular hitters' seasons and 95% of starters' seasons within one win. The tuning scripts read those
stored columns, so they live outside this repository, in `~/baseballstats-tools/scripts/war/v1/`:
`freeze_params.py`, `fit_retro.py`, `fit_bdl.py`, `fit_totals_eval.py` and `bat_delta_eval.py`. The full record is the audit
page linked from the project notes. After the stored columns are deleted, those scripts can't run again, and the frozen
settings here are what remain.

## Running it

Everything below is read-only.

```bash
# 1. build the inputs (needs the Retrosheet DuckDB and the balldontlie download on this machine)
export WAR_INPUTS=~/baseballstats-tools/scripts/war        # where the input files go
for b in retro_coverage retro_positions retro_batting retro_roe retro_fielding retro_zone retro_catchers \
         retro_total_zone retro_batter_range retro_team_hits_saved league_strength league_strength_pre1900 \
         bdl_season bdl_fielding bdl_runners; do <backend python> war/builders/$b.py; done

# 2. compute every season and save to a local file
<backend python> -m war.build_history

# 3. check it still matches the reviewed numbers
<backend python> tests/test_war_v1_package.py
```

The balldontlie download for a season comes from `builders/bdl_download.py` and `builders/bdl_plays.py`. That's about 15,000
requests, one at a time, and needs `BDL_KEY`. Both are resumable.

| Input file | Built by | From |
|---|---|---|
| `coverage.json` | `retro_coverage.py` | Retrosheet |
| `posPA.pkl` | `retro_positions.py` | Retrosheet |
| `bat_raw.pkl` (run values, baserunning, double plays) | `retro_batting.py` | Retrosheet |
| `roe.pkl` | `retro_roe.py` | Retrosheet |
| `fld2_raw.pkl` (range, double plays, arms, catchers) | `retro_fielding.py` | Retrosheet |
| `zfld_raw.pkl` | `retro_zone.py` | Retrosheet |
| `cat_raw.pkl` | `retro_catchers.py` | Retrosheet |
| `tz_raw.pkl` | `retro_total_zone.py` | Retrosheet |
| `rb_raw.pkl` | `retro_batter_range.py` | Retrosheet |
| `tder_raw.pkl` | `retro_team_hits_saved.py` | Retrosheet |
| `lgstr.pkl` | `league_strength.py` | our database |
| `lgstr_pre1900.pkl` | `league_strength_pre1900.py` | Lahman |
| `bdl26.pkl`, `bdl26_fld.pkl`, `bdl26_runs.pkl` | `bdl_season.py`, `bdl_fielding.py`, `bdl_runners.py` | balldontlie |

Verified 2026-11-16:
- Rebuilding every input from scratch reproduces the reviewed figures for all 89,946 hitter seasons and 49,838 pitcher
  seasons, to within 0.00000000002 wins.
- Database sums can come back in a different order, which changes the last decimal places and nothing else.

## Known limits

- The balldontlie season is fixed at 2026 (`compute.BDL_SEASON`). The nightly job will generalize it.
- Individual fielding matches Baseball-Reference less closely than everything else. The correlation is 0.84–0.90 before
  2003. From 2003 their figures come from a licensed video-scouting source we don't have.
