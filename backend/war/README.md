# Our WAR (versions 1 and 2)

This package computes wins above replacement from our own data. It holds the calculation, its frozen settings and the
scripts that build its inputs. It never reads the stored Baseball-Reference WAR columns, and
`tests/test_war_v1_package.py` checks that no file in this package names them.

**Status (2026-11-17): code only.** It stores nothing, the server doesn't read it, and there's no nightly job yet. Each of
those is a later step that needs its own approval. Three independent reviews (2026-11-16 and 11-17) found problems; all
are fixed here except the nightly freeze rules, which belong to the storage step, and the database fixes listed under
Known limits.

## Versions

- **v1** (`params/war_params_v1.json`, stored 2026-11-18): the method described below.
- **v2** (`params/war_params_v2.json`, approved 2026-11-19): v1 with one change. A pitcher's runs saved are turned
  into wins at the runs per win of *his own games*, using the published pitcher runs-per-win formula:
  ((18 − IP/G) × league RA9 × park + (IP/G) × his RA9) ÷ 18, + 2, × 1.5. The league rate is put in his parks
  (`dyn_rpw_park_adjust`) because his own RA9 is measured there; against a neutral-park league rate, pitchers in
  hitters' parks would get too little credit and pitchers in pitchers' parks too much.
  v1 used the league's runs per win blended 50/50 with Pythagenpat, which left the best starters short: aces since
  2010 came out about 0.8 wins below Baseball-Reference. With v2, aces within one win rise from 68% to 89% (2010–2026)
  and from 84% to 96% (1950–2009); modern aces' careers land within about a win (Maddux, Pedro Martínez, Kershaw).
  The cost is in the heaviest pre-1920 workloads, which were already high and go higher (Alexander, Mathewson,
  Cy Young). Hitters are identical in both versions, and replacement credit is unchanged.

`build_history.py` builds v2 by default (the version the app serves since 2026-11-20); `--version v1` builds v1. `tests/test_war_v2_package.py` checks that v2 is v1 plus exactly this change.

## What it produces

There's one figure per player-season, from 1871 to the current season. Negro Leagues seasons aren't covered; they're to
be hidden.

### Hitters

- The pieces, all in runs: batting, baserunning, double plays, replacement, position and fielding.
- **Offense** = (batting + baserunning + double plays + replacement) ÷ runs per win.
- **Defense** = (position + fielding) ÷ runs per win.
- **WAR** = Offense + Defense.

A player's plate appearances made as the pitcher are measured against the average hitting pitcher. Those plate
appearances get no replacement, position or fielding credit. A season counts as "pitcher batting" when that share is half
or more.

### Pitchers

**WAR** = wins above average + replacement wins.

Wins above average come from comparing the runs a pitcher allowed with what an average pitcher would allow in the same
parks, in the same league, behind the same defense. Three adjustments follow:

- the result is converted to wins: v1 with a 50/50 blend of linear and Pythagorean wins, v2 at the runs per win of his own games (see Versions);
- each league-season is re-centered so an average pitcher is worth 0;
- for innings pitched in relief, the result is multiplied by (1 + entry leverage) ÷ 2.

### Rows that can't be rated

- **Players with no plate appearances** (pinch runners, defensive substitutes) are rated on their baserunning, position
  and fielding, with no replacement credit.
- **A pitcher who never batted** gets no hitting row, because there is nothing to rate.
- **Rows with no playing time at all** (no games and no plate appearances; or no innings and no runs) come back in
  `empty`, with a reason.
- **Any other row that can't be rated** comes back in `skipped`, with a reason. That includes six seasons copied under a
  wrong player id, left out until the database is cleaned up (approved 2026-11-17).

## How each piece works

| Piece | Method | Settings (`params/war_params_v1.json`) |
|---|---|---|
| Batting | Each event is worth its run value, measured from that season's play-by-play. League average is non-pitchers. Adjusted for park, with reached-on-error credit and small per-era corrections to event values, then re-centred so position players' batting sums to zero in each league-season. Players traded between leagues use each league's baseline, weighted by their plate appearances there (team-by-team lines from Lahman before 1898, matched by Retrosheet id and confirmed against our season rows). | `bat_delta`, `ibb_weight`, `roe_weight`, `sf_as_out` |
| Baserunning | Steals and caught stealing, plus extra bases taken and outs on the bases on singles and doubles (not on outs in play). | `sb_runs`, `cs_runs`, `xb_runs`, `out_on_bases_runs` |
| Double plays | Grounded into double plays compared with the league rate on chances. | `gidp_runs_pbp`, `gidp_runs_season` |
| Replacement | About 1,000 WAR per 2,430 games, split hitters 58.6% and pitchers 40.9%. It's spread per plate appearance across both leagues; weaker leagues get less (measured from players who switched leagues). Relief innings (the estimated share of his innings in relief) get 0.85 of a starter's replacement credit, and a pitcher traded between leagues gets each league's credit for his innings there. | `hit_rep_share`, `pit_rep_share`, `rep_pool_leagues`, `league_strength_k`, `league_strength_k_1900`, `pit_league_strength_k`, `reliever_rep_weight` |
| Position | Runs per 1,458 innings by position for each decade from the 1870s, interpolated between decades. Re-centered so each league-season sums to 0. | `positions`, `center_positions` |
| Fielding 1910–2025 | Total Zone-style play counting from Retrosheet: plays made vs chances, with hits charged to the responsible fielder, adjusted for park. Also uses the batter × pitcher out pattern, team hits saved on balls in play, arms, double plays and catchers' running game. Weights are held to sensible signs. Seasons from 2003 use the 1989–2002 weights (option B). Re-centered so each league-season sums to 0. | `fld_retro`, `center_fielding` |
| Fielding, current season | The same idea from balldontlie: ball coordinates, totals, double plays and outfield throws from play text, and team hits saved. Calibrated to our own Total Zone values for 2015–2025, and centered per position. | `fld_bdl` |
| Fielding without play-by-play | Season totals (putouts, assists, errors, double plays) compared with league per game. | `fld_totals` |
| Pitchers | League runs allowed per 9 × park, plus a reliever adjustment, minus the defense behind him (the team's fielding runs, catchers included, shared by his balls in play). Team balls in play come from each pitcher's team-by-team lines (stints where they add up; Lahman before 1898). A blank runs-allowed falls back to earned runs. Impossibly low batters-faced counts (some 1901–07 rows) are replaced by 3 × innings + hits + walks + hit batsmen. Before 1910, team hits saved is added directly at half weight, because season-totals fielding doesn't carry it. | `reliever_ra9_shift`, `team_der_weight`, `pythag_blend` (v1), `pitcher_wins_model` + `dyn_rpw_add` + `dyn_rpw_mult` + `dyn_rpw_park_adjust` (v2), `center_pitcher_waa`, `pre1910_team_def_scale` |
| Reliever leverage | Entry leverage index, from a win-expectancy table measured on Retrosheet and applied to balldontlie plays for the current season. | `reliever_leverage`, `start_to_relief_ip_ratio` |
| Runs per win | 10 × √(runs per inning, both teams). | — |

## Where the settings came from

The settings were tuned in November 2026 so that our figures land close to the stored Baseball-Reference ones:

- 94% of regular hitters' seasons within one win;
- 97% of starters' seasons within one win (93% before 1910);
- all but two reliever seasons with 40+ innings within one win (Mike Marshall 1973 +1.0, Lindy McDaniel 1968 −1.5);
- league totals within about 1% in most decades, and every season within about 1% of the 1,000-WAR target.

The tuning scripts read those stored columns, so they live outside this repository, in
`~/baseballstats-tools/scripts/war/v1/`:

- `freeze_params.py`
- `fit_retro.py` and `fit_retro_signed.py` (fielding)
- `fit_bdl.py`
- `fit_totals_eval.py` and `fit_totals_signed.py` (season-totals fielding)
- `fit_bat_delta2.py` (batting corrections)
- `fit_pos_pre1910.py` (1870s–1900s positions)
- `fit_rep_shares.py`

The full record is the audit page linked from the project notes. Once the stored columns are deleted, these scripts can't
run again; the frozen settings here are what remain.

## When a season's figures change (approved 2026-11-16)

- **The current season is provisional.** It's built from balldontlie inputs and recomputed every night.
- **When Retrosheet publishes that season (each winter),** it's recomputed once from Retrosheet and becomes final.
- **A finished season changes only when we deliberately release a new formula version.** The storage step will hold
  versions side by side.

Rebuilding inputs can still nudge recent final seasons. Park factors use the season after, and league strength uses a
±4-year window. So final seasons must be stored, not recomputed. The storage step enforces this.

## Running it

Everything here is read-only.

```bash
export WAR_INPUTS=~/baseballstats-tools/scripts/war        # where the input files go
export WAR_SEASON=2026                                     # the balldontlie (current) season
# 1. inputs (needs the Retrosheet DuckDB and the balldontlie download on this machine)
for b in retro_coverage retro_positions retro_batting retro_roe retro_fielding retro_zone retro_catchers \
         retro_total_zone retro_batter_range retro_team_hits_saved retro_leverage league_strength \
         league_strength_pre1900 bdl_season bdl_fielding bdl_runners bdl_leverage; do <backend python> war/builders/$b.py; done
# 2. compute every season to a local file
<backend python> -m war.build_history
# 3. check it still matches the reviewed numbers
<backend python> tests/test_war_v1_package.py
# once Retrosheet publishes the balldontlie season: recompute every season from Retrosheet only (the 'final' run)
<backend python> -m war.build_history --season 0 --through 2026
```

`builders/bdl_download.py` and `builders/bdl_plays.py` download a season from balldontlie. That's about 15,000 requests,
one at a time, and needs `BDL_KEY`. Both are resumable.

| Input file | Built by | From |
|---|---|---|
| `coverage.json`, `coverage_league.json` | `retro_coverage.py` | Retrosheet, Lahman |
| `posPA.pkl` | `retro_positions.py` | Retrosheet |
| `bat_raw.pkl` (run values, baserunning, double plays) | `retro_batting.py` | Retrosheet |
| `roe.pkl` | `retro_roe.py` | Retrosheet |
| `fld2_raw.pkl` (range, double plays, arms, catchers) | `retro_fielding.py` | Retrosheet |
| `zfld_raw.pkl` | `retro_zone.py` | Retrosheet |
| `cat_raw.pkl` | `retro_catchers.py` | Retrosheet |
| `tz_raw.pkl` | `retro_total_zone.py` | Retrosheet |
| `rb_raw.pkl` | `retro_batter_range.py` | Retrosheet |
| `tder_raw.pkl` | `retro_team_hits_saved.py` | Retrosheet |
| `li_raw.pkl` (leverage table, reliever entrances) | `retro_leverage.py` | Retrosheet |
| `lgstr.pkl` | `league_strength.py` | our database |
| `lgstr_pre1900.pkl` | `league_strength_pre1900.py` | Lahman |
| `bdlYY.pkl`, `bdlYY_fld.pkl`, `bdlYY_runs.pkl`, `bdlYY_li.pkl` | `bdl_season.py`, `bdl_fielding.py`, `bdl_runners.py`, `bdl_leverage.py` | balldontlie |

## Known limits

- **Individual fielding** matches Baseball-Reference less closely than everything else: a correlation of 0.84–0.90 before
  2003. From 2003, their figures come from a licensed video-scouting source we don't have.
- **Starters before 1910** are the weakest pitching group: 93% within one win.
- **A few heavy-workload starters from the 1910s** come out above Baseball-Reference: Pete Alexander +9.6, Christy
  Mathewson +7. Walter Johnson comes out below (−7.7 pitching).
- **Cap Anson** (−7 wins) is the largest remaining 19th-century batting gap.
- **Database fixes pending** (each its own approved write): three 2026 pitcher rows with no player or team (skipped); six
  seasons copied under wrong ids 817009 and 817503 (skipped); Retrosheet id mismatches that cost a few players their
  play-by-play inputs (Curtis Roberts 1954–56 `robec103` vs `robec102`; Hal Jones 1961–62 `joneh106` vs `joneh103`).
- **Outfielders with no left/centre/right split** (1910s–1940s seasons without full play-by-play) get that era's fitted
  unsplit outfield value, which sits a little below the left/centre/right average. It was fitted that way on purpose.
- **The golden test runs against the live database.** It will need regenerating when 2026 rows change or new Retrosheet
  ids are mapped. That's expected, and one more reason final seasons are stored rather than recomputed.
