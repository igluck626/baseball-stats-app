# Cade Smith pitcher fix — plan (Part A: targeted, Part B: systemic)

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



**Status:** design only. No code changed. Review **Part A** first; execute + verify
it for Cade Smith; then review **Part B** as its own step.

## Diagnosis recap (confirmed by API + code read)

- **One id, `671922`** (bbref `smithca06`, `bdl_id 841`, team CLE). No ID split.
- **Misclassified:** he lives in the **`Player` (batter) bio table** as `position "SS"`,
  so the API derives `is_pitcher: false`. He is really a Cleveland RHP.
- **38 pitching game logs** (2026) on `671922` — correct and untouched. 0 batting logs.
- **No `PitcherSeason` row** → `/pitching/current` and `/pitching/career` return
  *"No … pitching stats found."*
- **Bad hitting rows exist:** an empty 2026 `BatterSeason` (G:37, PA:0, all zeros)
  **plus a conflated 1938 Negro-League `PlayerSeason`** (team "BBB", league "NAL")
  pulled from Lahman via the `smithca06` bbref.
- **Root of the gap:** `nightly_update._update_pitchers` iterates only
  `crud.get_all_pitcher_ids` = `SELECT DISTINCT player_id FROM pitcher_seasons`
  — i.e. players who **already** have a `PitcherSeason` row. A pitcher with game
  logs but no seed row is invisible to it forever. Season stats are built from
  **BDL** (keyed by `bdl_id`) + bwar, **not** by summing game logs.
- **Classification mechanics:** `is_pitcher` is derived from which bio table the
  player is in; `data_service._choose_active_bio_row` arbitrates when a player
  has rows in **both** `Player` and `Pitcher`. `PitcherSeason` PK is
  `(player_id, year)`; every stat column is nullable.

---

# PART A — Targeted repair (fix Cade Smith now, lower risk)

Goal: get `671922` classified as a pitcher, remove the bad batting rows, and seed
a `PitcherSeason` so the next nightly fills his 2026 line from BDL — **without**
reloading Lahman (which writes nothing for a 2024-debut player and would risk
re-injecting the 1938 conflation).

## A1. Reclassify batter → pitcher — reuse `repair-swapped-player` phase C/D, SKIP phase E

`repair-swapped-player` already contains exactly the reclassify + cleanup we want
in phases **C** (create the `Pitcher` bio) and **D** (delete the wrong-side
`Player` bio + **all** `PlayerSeason` + **all** `PitcherSeason` rows on the id).
The only problem is phase **E** (Lahman reload) and its required `bogus_mlbam`.

**Proposed minimal change to `repair-swapped-player`** (small, reuses tested logic):
- Add `skip_lahman_reload: bool = Query(False)`. When true, run C and D, then
  **return before phase E** — no Lahman fetch, no `save_pitcher_seasons` from a
  conflated bbref.
- Make `bogus_mlbam: int | None = Query(None)`. When `None`, skip phases A and F
  (there is no split/bogus record for a straightforwardly-misclassified modern
  player). The existing guard logic already tolerates "bogus already absent."

With those two params, for Cade Smith the call does exactly:
- **C** → create a `Pitcher` bio for `671922`: `position "P"`, `bbref smithca06`,
  carrying `bdl_id 841`, `throws "R"`, birth/height/weight from the current bio.
- **D** → delete the `Player` (batter) bio, delete all `PlayerSeason` rows
  (the empty 2026 **and** the 1938 conflation), delete all `PitcherSeason` rows
  (none exist). **This is A2 — it falls out of phase D for free.**
- **E skipped** → no Lahman, no 1938 re-injection (see A4).

> Alternative if we'd rather not touch the tested endpoint: extract phases C/D
> into a shared helper (`_reclassify_bio_side(real_mlbam, correct_bbref,
> position_type)`) and expose a new `POST /admin/reclassify-to-pitcher/{id}` that
> calls only that helper. Same effect; slightly more surface. Recommendation:
> the `skip_lahman_reload` + optional `bogus_mlbam` flag is the smaller diff.

## A2. Delete the bad batting rows

**Handled by phase D above** — it deletes the `Player` bio and every `PlayerSeason`
row on `671922`, which removes both the empty 2026 batter line and the conflated
1938 Negro-League season in one step. No separate call needed.

## A3. New reusable capability — seed a stub `PitcherSeason`

This is the missing primitive. Design it as a **small, reusable admin endpoint**,
the `PitcherSeason` analog of `reset-player-season`, but with a **non-destructive
"ensure exists" semantic** (see idempotency note — we must not clobber real stats
on re-run):

```
POST /admin/seed-pitcher-season/{player_id}?season=<year>&team_code=<code>
```
Behavior:
1. If a `PitcherSeason` row already exists for `(player_id, season)` → **no-op**
   (leave it — it may already hold real nightly-filled stats).
2. Else INSERT a stub `PitcherSeason(player_id, year=season, team=team_code)` with
   every stat column left NULL.

Effect: `671922` now appears in `get_all_pitcher_ids`, so the **next nightly
pitcher phase** fetches his BDL stats (`bdl_id 841`) and upserts the real 2026
line into that row (`save_pitcher_seasons` UPSERTs on `(player_id, year)`).

> Why "ensure exists" rather than `reset-player-season`'s delete-then-insert:
> `reset-player-season` is *destructive by design* (it wipes stats to let the
> nightly re-fill, for the team-repin use case). For **seeding** we want the
> opposite — creating a row where none exists, and never wiping a row the nightly
> has since populated. So `seed-pitcher-season` is insert-if-absent. (If we ever
> also want the destructive team-repin behavior on the pitcher side, add a
> separate `reset-pitcher-season` later; keep them distinct.)

## A4. Avoiding the `smithca06` → 1938 Lahman trap (the specific danger)

The trap is **phase E**: `repair-swapped-player` normally reloads seasons via
`_lahman_pitching_seasons_for_bbref(correct_bbref)`. For `smithca06` that bbref is
conflated with a 1938 Negro-League player, so a Lahman reload could write a bogus
1938 **pitcher** season (and, on the batter side, is exactly where the 1938 batting
row came from).

**Part A sidesteps it by construction:**
- We run `repair-swapped-player` with **`skip_lahman_reload=true`** → phase E never
  executes → **no Lahman rows of any kind are loaded** from `smithca06`.
- Phase D has already **deleted** the existing 1938 `PlayerSeason`, and we do not
  reload it.
- His pitching data comes **only** from the seeded stub + the nightly **BDL** fill
  (`bdl_id 841`), which is 2026-and-forward MLB data — never Lahman, never 1938.
- The `Pitcher` bio keeps `bbref smithca06` (his real bbref), but nothing in Part A
  ever *reads Lahman through it*. `/pitching/career` reads `PitcherSeason` rows
  (BDL-filled 2026), not Lahman — so no 1938 reappears.

**Do NOT** run `reload-player-lahman/671922` or any Lahman-reload path on him — that
would reintroduce the conflation. (Broader conflation fix: see Follow-up.)

## A5. Exact curl sequence + verification

Base: `https://baseball-stats-app-production-0ef1.up.railway.app`

**Repair sequence (run in order, once the two endpoint changes above are shipped):**
```bash
BASE=https://baseball-stats-app-production-0ef1.up.railway.app

# 1) Reclassify batter→pitcher: create Pitcher bio + delete Player bio & all
#    PlayerSeason rows (empty 2026 + conflated 1938). NO Lahman reload, no bogus id.
curl -s -X POST "$BASE/admin/repair-swapped-player?real_mlbam=671922&correct_bbref=smithca06&position_type=pitcher&position=P&skip_lahman_reload=true"

# 2) Seed a stub 2026 PitcherSeason so the nightly pitcher phase picks him up.
curl -s -X POST "$BASE/admin/seed-pitcher-season/671922?season=2026&team_code=CLE"

# 3) Let the next nightly run fill his 2026 pitching line from BDL (bdl_id 841).
#    (If we want an immediate fill instead of waiting for cron, that needs a
#    per-player pitcher-stats refresh trigger — note as optional, see below.)
```

**Verification (after step 2 immediately, then again after the nightly):**
```bash
# Now classified as a pitcher:
curl -s "$BASE/players/by-mlb-id/671922" | python3 -c "import sys,json;d=json.load(sys.stdin);print({k:d.get(k) for k in ('is_pitcher','position','throws','bbref_id')})"
#   expect: is_pitcher=True, position="P", throws="R"

# Pitching current: a 2026 row exists (stub right after step 2; real stats after nightly)
curl -s "$BASE/players/671922/pitching/current" | python3 -m json.tool

# Pitching career: 2026 only, NO 1938 season
curl -s "$BASE/players/671922/pitching/career" | python3 -m json.tool

# Hitting gone: no current batting row, no 1938 season, no batting career totals
curl -s "$BASE/players/671922/stats/current" | python3 -m json.tool
curl -s "$BASE/players/671922/stats/career"  | python3 -m json.tool

# Game logs intact: still 38 pitching rows, keyed to 671922
curl -s "$BASE/players/671922/gamelogs/pitching" | python3 -c "import sys,json;print('pitching gamelogs:',len(json.load(sys.stdin)))"
```
Pass criteria: Overview + Career show his **2026 pitching** stats (stub → filled),
**no batting rows**, **no 1938 season**, and the 38 game logs are unchanged.

> Optional immediate fill: if we don't want to wait for the cron, we'd add a
> per-player pitcher refresh (fetch BDL for `bdl_id 841`, `_build_current_pitcher_entry`,
> upsert). Not required for correctness — the stub already makes Overview show a
> 2026 pitching row, and the nightly fills the real numbers. Flagged, not designed.

## A6. Idempotency (safe to re-run?)

- **Step 1 (`repair-swapped-player`, skip_lahman):** phase C is create-or-update
  the `Pitcher` bio; phase D deletes are re-run-safe (nothing left to delete on a
  second run). **Idempotent.**
- **Step 2 (`seed-pitcher-season`, insert-if-absent):** on re-run it's a **no-op**
  when a row already exists — so it will **not** wipe a nightly-filled 2026 line.
  **Idempotent and non-destructive.** (This is the key reason to use insert-if-absent
  rather than delete-then-insert.)
- Net: the whole sequence is safe to re-run.

---

# PART B — Systemic fix (prevent recurrence) — REVIEW SEPARATELY, do not implement with Part A

## B1. Root cause
`_update_pitchers` (nightly) walks only `get_all_pitcher_ids` =
`DISTINCT player_id FROM pitcher_seasons`. A player who has **pitching game logs**
but **no `PitcherSeason` row** — because he was mis-filed as a position player —
is never seeded, so his logs are never aggregated. He can't self-heal.

## B2. The fix + the mop-up-inning guard (the crux, and the main risk)

**Fix shape:** add a **discovery pass** to the nightly (before/alongside the
pitcher phase): find players with **current-season pitching game logs** but **no
`PitcherSeason` row**, and — for those that pass the guard — seed a stub
`PitcherSeason` (reusing the A3 primitive), so the pitcher phase fills them.

**The danger:** naively seeding "anyone with a pitching game log" would mint fake
pitcher profiles for **position players who threw a mop-up inning** in a blowout
(a handful happen every season). We must distinguish a *real pitcher misclassified
in our DB* from a *position player who threw once*.

**Proposed guard — authoritative position, not a raw IP threshold:**
- **Primary signal (the discriminator): BDL/MLB primary position.** We already
  have `data_service._bdl_is_pitcher` (used at data_service.py:2351 to compare BDL
  vs DB classification). Seed a `PitcherSeason` **only if BDL/MLB classifies the
  player as a pitcher** (primary position P). A position player who threw mop-up is
  classified by BDL as a position player, so he is **not** seeded. This is the
  right signal — a real reliever early in the year may have very few IP, so an IP
  floor alone is unreliable; the position source is authoritative.
- **Secondary guard (defense in depth, optional):** additionally require a modest
  workload, e.g. pitched in ≥ 2 games **or** total IP ≥ ~3, to catch any case where
  BDL position is missing/ambiguous. Treat this as a backstop, not the primary
  test — and log any player that has pitching logs but fails the position check so
  we can eyeball true two-way / edge cases rather than silently ignoring them.
- **Explicitly do NOT** seed on "has ≥1 pitching game log" alone.

Net rule: *seed a `PitcherSeason` for a current-season pitching-log player iff
BDL/MLB says he's a pitcher (with an optional small-workload backstop).* Mop-up
position players fail the position test and are skipped.

## B3. Interaction with bio classification (don't leave a split identity)

A player mis-filed on the season side is usually **also** mis-filed in the **bio**
table (Cade Smith: `Player`/SS bio). If Part B seeds only the *pitcher season* and
not the *bio*, the result is a split identity: `PitcherSeason` rows exist, but
`_choose_active_bio_row` may still surface the stale `Player` bio (`is_pitcher:false`),
and the **batter phase keeps rebuilding an empty batter season** every night.

So Part B's self-heal must also **reconcile the bio**: when it seeds a pitcher for a
BDL-classified pitcher, it should ensure a `Pitcher` bio exists and retire/deprefer
the stale `Player` bio (or make `_choose_active_bio_row` prefer the side that has
BDL classification + game logs). Otherwise we fix the season but not the profile,
and the empty batter row churns back. This bio reconciliation is why Part B is
higher-stakes than Part A and needs its own review — it touches the
`_choose_active_bio_row` arbitration and the batter phase, and must handle genuine
**two-way players** (Ohtani-type, legitimately in both tables) without demoting them.

## B4. Review separately
Do not implement B alongside A. Part A is a bounded, idempotent, verifiable repair
of one player. Part B changes nightly behavior for **every** player with pitching
logs and touches bio arbitration — it needs its own design pass, its own guard
review (B2), and its own test against known two-way players before shipping.

---

# Follow-up (flag only — do NOT fix now)

**`smithca06` → 1938 Chadwick bridge conflation.** The bbref `smithca06` is
cross-linked to a 1938 Negro-Leagues player's Lahman career (the source of the
bogus 1938 batting season, and the reason a Lahman reload is dangerous here). This
is a **broader Chadwick-bridge data-quality issue** from the 2024 Negro-Leagues
merge — likely affecting other players with shared surname stems (`harriho`,
`rodrijo`, and the "swapped-Chadwick split" cohort already noted in
`repair-swapped-player`). Track separately:
- Audit which modern player bbrefs map to conflated (pre-integration / Negro-League)
  Lahman careers.
- Decide a durable mapping correction (or a "don't reload Lahman for post-2015
  debuts" rule) so repairs like this don't have to sidestep it case-by-case.

Part A does not depend on this being fixed — it avoids the conflation entirely by
never reloading Lahman for Cade Smith.
