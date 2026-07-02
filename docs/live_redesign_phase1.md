# Live-Snapshot Redesign — Phase 1 Design

**Status:** design only (no code). Supersedes the current ad-hoc field sourcing in
`backend/api/live_service.py::assemble_unified`.

**Principle:** *the `/plays` feed is the single source of truth for live game
**state**.* Everything that describes "what the game looks like right now" —
inning, half, outs, count, score, the per-inning linescore grid, hits, scoring
plays, and the current batter/pitcher — is derived from `/plays`. Four things
that `/plays` genuinely cannot supply stay on their existing feeds, each for a
verified reason (Phase 0): **errors**, **player names**, **base runners**, and
**full batting/pitching stat lines + batting order**.

Phase 0 evidence (game 5059068, PHI@PIT, full 9 innings): per-inning runs and
per-team hits derive **exactly** from `/plays` (665/665 plays carry a running
score); errors appear only in free-text; base state has no structured field in a
play row. See `docs` history / the Phase 0 investigation for the raw comparison.

---

## (1) Source map — the "after" column

Field-by-field authoritative source under the redesign. "Changes" = the source
feed moves vs. the current implementation; "unchanged" = same source as today.

| Payload field | NEW authoritative source | vs. current |
|---|---|---|
| `game_id`, `season`, `season_type` | games (schedule) | unchanged |
| `status`, `summary.is_live` | games (`_norm_status`) | unchanged |
| `summary.{away,home}.bdl_id / abbreviation / name / location / league / division` | games (team objects) | unchanged |
| `summary.{away,home}.team_code` | derived (BDL→Lahman map) | unchanged |
| `summary.{away,home}.runs` | **plays** (running score on latest play) | unchanged *(already plays-derived; hardened — see §2)* |
| `summary.{away,home}.hits` | **plays** (count hit-type plays per batting half) | **changes** (was games `team_data.hits`) |
| `summary.{away,home}.errors` | games (`team_data.errors`) | unchanged — **KEEP**: errors exist only in `Play Result` free-text; count/attribution unreliable |
| `summary.inning` | **plays** (`sit_play.inning`) | unchanged |
| `summary.inning_half` | **plays** (`sit_play.inning_type`) | unchanged |
| `summary.outs` | **plays** (`sit_play.outs`) | unchanged |
| `summary.balls` / `summary.strikes` | **plays** (`sit_play`) | unchanged |
| `linescore.innings[]` (per-inning grid) | **plays** (half-inning running-score deltas) | **changes** (was games `inning_scores`) |
| `linescore.{away,home}_runs` | **plays** (running score on latest play) | unchanged |
| `linescore.{away,home}_hits` | **plays** (count hit-type plays per batting half) | **changes** (was games `team_data.hits`) |
| `linescore.{away,home}_errors` | games (`team_data.errors`) | unchanged — **KEEP** (same reason as `summary.errors`) |
| `linescore.scheduled_innings` | constant `9` | unchanged |
| `situation.batter.id` / `situation.pitcher.id` | **plays** (`sit_play.batter_id` / `pitcher_id`) | unchanged |
| `situation.batter.name` / `situation.pitcher.name` | stats (id→name map) | unchanged — **KEEP**: plays carry only ids |
| `situation.on_first / on_second / on_third` | plate_appearances (last PA runner flags, gated on inning+half match) | unchanged — **KEEP**: no structured base field in a play row |
| `plays[]`, `scoring_plays[]` | **plays** | unchanged |
| `batting.{away,home}[]` (stat lines) | stats (+ lineups for batting order) | unchanged — **KEEP**: per-player AB/H/R/RBI live only in stats |
| `pitching.{away,home}[]` (stat lines) | stats (+ plays for appearance order) | unchanged — **KEEP** |

**Net changes:** three fields (or six counting both teams) move from the
laggy games/schedule feed to plays: **`summary.hits`**, **`linescore.hits`**,
and the **`linescore.innings[]` grid**. Everything else keeps its current
source; the redesign is mostly a *consolidation and hardening*, not a rewrite.

---

## (2) Derivation spec

All derivations operate on `plays_sorted = sorted(plays, key=order)`. Define a
helper that classifies a play into a real half-inning, or `None` for the
`Mid`/`End`/transition markers:

```
def half_of(play):
    it = play.inning_type or ""
    if "Top" in it:    return (play.inning, "top")
    if "Bottom" in it: return (play.inning, "bottom")
    return None                      # Start/Mid/End markers — skip
```

### 2a. Per-inning runs grid (verified exact in Phase 0)

Walk the plays once, tracking the running (away, home) score at the **last**
play of each real half-inning; per-inning runs for the batting team = the delta
from the previous half's ending score. Track a `played` set so we can tell a
**scoreless-but-played** inning (`0`) apart from an **un-batted** one (`x`).

```
played = set()                       # (inning, half) that had >=1 real play
last_score_by_half = ordered list of (inning, half, away_score, home_score)
                                     # each = score at that half's LAST play

prev_a = prev_h = 0
away_runs_by_inning = defaultdict(int)
home_runs_by_inning = defaultdict(int)
for (inning, half, la, lh) in last_score_by_half:
    played.add((inning, half))
    if half == "top":    away_runs_by_inning[inning] += (la - prev_a)   # away bats top
    else:                home_runs_by_inning[inning] += (lh - prev_h)   # home bats bottom
    prev_a, prev_h = la, lh

n_inn = max(9, max(inning for (inning, _half) in played))
innings = []
for i in 1..n_inn:
    away_cell = away_runs_by_inning[i] if (i, "top")    in played else None
    home_cell = home_runs_by_inning[i] if (i, "bottom") in played else None
    innings.append({"num": i, "away": away_cell, "home": home_cell})
```

**The `x`/blank condition (confirmed edge case):** a cell is `None` (→ client
renders blank/`x`) **iff no play exists for that `(inning, half)`**. This is the
*only* distinction between `0` and `x`:

- `0`  = the half-inning **was played** and scored no runs.
- `None` (`x`) = the half-inning **was never batted** — e.g. the home team leads
  entering the bottom of the final inning (game `final`, no `(N, "bottom")`
  plays). Verified: PHI had no bottom-9th → home cell 9 must be `x`, not `0`.

Do **not** emit the string `"x"` — emit JSON `null`. `LiveInningRow.away/home`
is `Int?` on the client; `null` decodes cleanly and the view layer renders the
blank/`x`. A string would break decoding (see §5).

**Cross-check available:** summing `score_value` per half yields the same totals
(PIT 6 / PHI 10 in Phase 0) — useful as an assertion in tests, not the primary
path.

### 2b. Hits (verified exact in Phase 0)

```
HIT_TYPES = {"Single", "Double", "Triple", "Home Run"}
away_hits = count(p in plays where p.type in HIT_TYPES and half_of(p).half == "top")
home_hits = count(p in plays where p.type in HIT_TYPES and half_of(p).half == "bottom")
```

Feeds both `summary.{side}.hits` and `linescore.{side}_hits`. (Phase 0: PIT 12 /
PHI 11 — exact.)

### 2c. Runs totals

`away_runs` / `home_runs` = the running `away_score` / `home_score` on the
**latest** real play (equivalently, the last entry of `last_score_by_half`, and
equal to `sum(away_runs_by_inning)`). This replaces the current
`sit_play`-first-with-`is None`-fallback logic, which could let a literal `0` on
an in-progress at-bat win over the true total. **Use the max/last running score
across plays, never a single possibly-zero row.**

### 2d. Extra innings / walk-offs

The loop keys off **actual `inning` numbers** from the play rows, not a fixed 9:

- `n_inn = max(9, max observed inning)` — extra innings extend the grid
  automatically.
- A walk-off is just the bottom half's last-play delta; no special case. The
  losing top half and the winning bottom half are both `played`, so both render
  their real cells.
- The un-batted-final-half rule (§2a) is what produces the correct `x` for a
  home team that never needed to bat.

---

## (3) Enriched list payload (`_summary_from_unified`)

The per-game cache (`_game_key(gid)`) already stores the **full `unified`
snapshot**, and `_summary_from_unified(unified)` is called with that dict in
hand. So the enriched list entry is **assembly-only — zero new BDL calls, no new
cache, no extra quota.**

Each `/live/games` entry gains these fields, pulled straight from the cached
`unified`:

| New list field | Pulled from `unified` |
|---|---|
| `batter` `{id, name}` | `unified.situation.batter` |
| `pitcher` `{id, name}` | `unified.situation.pitcher` |
| `last_play` (text) | `unified.plays[-1].text` (latest play row) |

Existing list fields (`game_id`, `fetched_at`, `status`, `is_live`, `inning`,
`inning_half`, `outs`, `away/home` with `team_code/abbreviation/name/runs`) are
unchanged. Because `summary.{side}.runs` and the inning/half/outs are now fully
plays-derived, the list card and the detail banner read the **same** numbers —
the banner-vs-plays disagreement class of bug goes away by construction.

> Optional: also surface `away.hits` / `home.hits` on the list entry if the
> Scores/Home cards ever want R-H inline. Additive, same rule (safe — see §5).

---

## (4) Assembly refactor shape

Reorganize `assemble_unified` so **each field has exactly one clearly-labeled
source** and the function reads top-to-bottom as: *fetch feeds → id→name map →
derive live-state from plays → attach names/bases/lines from other feeds →
assemble*. Extract the derivations into named helpers (each individually
testable against the Phase 0 fixture):

```
# --- pure helpers (unit-testable) ---
_half_of(play)                      -> (inning, half) | None
_id_name_map(stats)                 -> {player_id: full_name}
_derive_state(plays_sorted)         -> {inning, half, outs, balls, strikes,
                                        batter_id, pitcher_id,
                                        away_runs, home_runs}      # PLAYS ONLY
_derive_grid(plays_sorted)          -> [{num, away, home}]         # §2a, x-aware
_derive_hits(plays_sorted)          -> (away_hits, home_hits)      # §2b
_derive_scoring(plays_sorted)       -> [play_block ...]            # scoring subset
_bases_from_pas(pas, inning, half)  -> (on_first, on_second, on_third)  # KEEP: PA-gated
_box_lines(stats, lineup, plays)    -> (batting, pitching)         # KEEP: stats(+order joins)
```

`assemble_unified` becomes a thin orchestrator:

```
def assemble_unified(game, stats, plays, pas, lineup):
    teams/status         = parse from game                      # games feed
    plays_sorted         = sort(plays)
    names                = _id_name_map(stats)

    state                = _derive_state(plays_sorted)          # PLAYS: state + runs
    grid                 = _derive_grid(plays_sorted)           # PLAYS
    away_hits, home_hits = _derive_hits(plays_sorted)           # PLAYS
    scoring              = _derive_scoring(plays_sorted)        # PLAYS
    full_plays           = [play_block(p) for p in plays_sorted]

    bases                = _bases_from_pas(pas, state.inning, state.half)   # PAs (gated)
    batter_name          = names.get(state.batter_id)           # stats join
    pitcher_name         = names.get(state.pitcher_id)          # stats join
    batting, pitching    = _box_lines(stats, lineup, plays_sorted)         # stats(+lineup/plays)
    away_err, home_err   = game.away_team_data.errors, game.home_team_data.errors  # games (KEEP)

    return { ...same shape...                                   # §5: contract unchanged
             summary:   {..., runs=state.*_runs, hits=derived, errors=games},
             linescore: {innings=grid, *_runs=state, *_hits=derived, *_errors=games},
             situation: {batter/pitcher = state.id + name, bases},
             plays=full_plays, scoring_plays=scoring,
             batting=batting, pitching=pitching }
```

Guiding rules for the refactor:

- **One source per field.** No more "plays → games → last-play" fallback chains
  that silently mix feeds. `_derive_state` owns state+runs from plays; games
  supplies only errors + team identity; stats supplies only names + stat lines;
  PAs supply only bases.
- **Derivations are pure** (take play/stat lists, return values) so each can be
  unit-tested against the saved Phase 0 fixture (expected grid, hits, x-cell).
- **Order encodes provenance** — plays block first, other-feed attachments after,
  so a reviewer can see exactly what each feed contributes.

---

## (5) Backward compatibility

The contract shape stays the same; we are changing **sources**, plus **additive**
list fields. Swift `Codable` on the client **ignores unknown JSON keys**, so
additions never break decoding. Detail (`LiveGameDetail`) and its
`summary`/`linescore`/`situation` blocks are non-optional and keep every existing
key, so no required key disappears.

Field-by-field client impact:

| Change | Client decode risk | Sequencing |
|---|---|---|
| `linescore.innings[]` now plays-derived, **always present** | None — already non-optional & always emitted; `away/home` are `Int?` | ship backend first |
| Un-batted half emitted as `null` (not `0`, not `"x"`) | None if `null` — `Int?` decodes. **Must NOT be a string** — `"x"` would throw | backend: emit `null`; client view renders blank/`x` |
| `summary.hits` / `linescore.hits` source moves to plays | None — same `Int?` shape, just fresher values | ship backend first |
| Runs totals hardened (max running score) | None — same `Int?`, corrects stale `0` | ship backend first |
| **List entry gains** `batter`, `pitcher`, `last_play` | None — Swift ignores unknown keys until `LiveGameSummary` opts in | backend first; client adds props later, independently |

**Nothing in this redesign removes or renames a field or changes a type**, so
the existing client keeps decoding the new payload unchanged. The only hard
rule: the un-batted-half cell must serialize as JSON `null`, never the string
`"x"` (client renders the `x` presentation-side from `null`).

**Recommended sequence:**
1. Ship the backend redesign (sources move; list fields added). Existing client
   keeps working — it just gets fresh grid/hits/runs and ignores the new list
   keys.
2. Separately, update the client: add `batter`/`pitcher`/`last_play` to
   `LiveGameSummary`; ensure `LiveInningRow` renders `null` as blank/`x`; and
   (per the earlier banner investigation) point the box-score banner + linescore
   at the live payload rather than the frozen `vm.game`.

The two steps are independent and safe to land in either order because the
payload contract is preserved.
