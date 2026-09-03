# Annual Retrosheet ingest — the one manual step

> **⚠️ Every `/admin` route now requires a token.** Send `X-Admin-Token:
> $ADMIN_TOKEN` on every curl below, or the request answers **404** — the guard
> returns "not found" rather than "unauthorized" so an unauthenticated caller
> cannot tell a real route from a typo. Export it first:
>
> ```
> export ADMIN_TOKEN='...'   # the value set in Railway
> ```
> and add `-H "X-Admin-Token: $ADMIN_TOKEN"` to each `curl` that targets
> `/admin/...`. Reads that are not under `/admin/` (`/players/...`, etc.) are
> unaffected.



The boundary between our own record and the provider is **self-managing**: the
client reads it from `GET /meta/coverage`, which derives it from
`retro_game_info` rather than from a constant anyone has to remember to move.

That leaves exactly one thing a person must do, once a year. **This document
exists because that makes forgetting it the single remaining way the boundary
silently stops advancing.** Nothing will break, error, or look wrong — the app
will simply keep serving a season from the provider that we hold better data
for, and will go on doing so indefinitely.

## When

Retrosheet publishes a season some months after it ends — typically in the
spring following. So the window is roughly **March–June of year N+1** for
season N. There is no harm in trying early: a season Retrosheet has not
published yet fails to download and the run reports the year as `missing`
rather than writing anything.

## What to run

Two ingests, in this order. Both are idempotent — re-running is safe.

1. **Per-player game logs** (batting/pitching lines, lineup slots, positions,
   decisions, pitch counts):

   ```
   POST /admin/ingest-retrosheet-gamelogs?year_from=<N>&year_to=<N>&refresh_lineup=true&appearance_gate=true
   ```

   `appearance_gate=true` is required for 2000 and later — those seasons were
   ingested under it, and refreshing without it silently misses the
   empty-appearance rows.

2. **Per-game facts** (linescore, park, attendance, time, umpires):

   ```
   POST /admin/ingest-retrosheet-gameinfo?year_from=<N>&year_to=<N>
   ```

Watch each with its `/status` sibling.

## How to check it worked

⚠️ **Read coverage back out of the tables. Do not trust the written count** —
a bulk writer returns the size of its input, which is a plan rather than a
result, and this project has been bitten by that three times.

```sql
-- every row of the new season should carry a lineup slot
SELECT COUNT(*), COUNT(slot), COUNT(pos)
FROM batting_gamelogs WHERE season = <N> AND game_id LIKE 'retro-%';

-- and every game should have a line score
SELECT COUNT(*) FROM retro_game_info WHERE season = <N> AND away_line IS NOT NULL;
```

A count that is a **round multiple of the batch size** (2,000 for gamelogs,
1,000 for game info) means a batch aborted and the year is part-written. That
is the signature, not a coincidence.

Then confirm the boundary has actually moved:

```
GET /meta/coverage   ->   {"retrosheet_last_season": <N>, "source": "retro_game_info", ...}
```

If it still reports N-1, the completeness floor rejected the season — the
response says which floor and what it was checked against. The usual cause is
an ingest that did not finish.

## What the floor does

`/meta/coverage` promotes a season only when **both** hold:

1. The season is over — strictly before the current calendar year. Retrosheet
   does not publish a season in progress, and this stops a January ingest that
   has written its first two hundred games from flipping the boundary mid-run
   and routing the rest of that season to tables which do not yet hold it.
2. It is as big as its neighbours — at least 90% of the median game count of
   the three seasons before it (~2,187 against recent seasons' ~2,430).

**A genuinely shortened season fails the second gate**, and that is deliberate.
1981 and 1994 were struck; 2020 was cut to 898 games. Such a season would hold
the boundary back a year, leaving it on the provider — the safe direction,
since the provider covers recent seasons well. If it ever happens, the fix is
a one-line override here rather than a scramble.

## If you skip a year

Nothing breaks and nothing complains. That season stays on the provider, which
means: no substitution sequence, no multi-position lines, substitutes labelled
with their career position and appended after the starters rather than sitting
in their batting slot, and no park, attendance or umpires. Running the ingest
later fixes it retroactively — the boundary will advance the next time the app
launches.
