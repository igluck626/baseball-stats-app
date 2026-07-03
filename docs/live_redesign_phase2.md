# Live-Snapshot Redesign — Phase 2 Design (client): LiveGameStore

**Status:** design only (no code). Builds on the committed Phase 1 (backend
single-source-of-truth) and the committed client work (lifecycle-aware polling +
box-score banner reading the live payload).

**Goal:** consolidate all client live-data fetching into ONE root-injected
`LiveGameStore` so the Home favorite card, the Scores list, each live card, and
the box score read the SAME live data and can't disagree — the client mirror of
the backend's single source of truth. This eliminates the duplication mapped in
the Phase 2 investigation: the `/live/games` list held in two places
(`HomeViewModel` + `ScoresViewModel`), and `/live/games/{id}` polled 2–3× for the
same game id (per-card `LiveFeedViewModel` + the box score pushed over it + the
Home favorite's own detail loop).

**Principle:** the store owns *fetching + caching*; it does NOT own tab/lifecycle
logic. Surfaces drive it through the existing `AppNavigation.shouldPoll(on:)`
`.onChange` wiring already committed — we reuse that lifecycle work, not
duplicate it.

---

## (1) Store API

State it holds:
- **`liveList`** — every currently-live game keyed by game id, from ONE
  `/live/games` loop. Shared by the Home card and the Scores list.
- **`detail`** — the full `LiveGameDetail` per *subscribed* game id, from
  `/live/games/{id}` loops. One loop per distinct id, refcounted.

Both are `@Published` so SwiftUI views re-render on update.

```swift
@MainActor
final class LiveGameStore: ObservableObject {

    // MARK: Published state (views read these)

    /// All live games from the shared /live/games loop, keyed by gamePk.
    /// Empty when nothing is live or the list loop isn't running.
    @Published private(set) var liveList: [Int: LiveGameSummary] = [:]

    /// Full snapshot per SUBSCRIBED game id, from /live/games/{id}.
    /// Present only while at least one surface is subscribed to that id.
    @Published private(set) var detail: [Int: LiveGameDetail] = [:]

    /// Optional: last error per scope, for surfaces that show a retry state.
    @Published private(set) var listError: String?

    // MARK: List loop (shared by Home card + Scores list)

    /// Start the single /live/games poll loop. Idempotent — a running loop is
    /// cancelled and replaced (self-cancelling, like the existing start* methods).
    /// `immediate` does one leading fetch before the sleep loop (foreground/
    /// visible RESUME path), matching the committed lifecycle pattern.
    func startListLoop(immediate: Bool = false)
    func stopListLoop()

    // MARK: Detail loops (refcounted by DISTINCT subscriber token per game id)

    /// Stable per-surface identity. Each subscribing view holds one in @State
    /// (`@State private var liveToken = LiveGameStore.SubscriberID()`), so the
    /// SAME view instance always presents the SAME token — which is what makes
    /// subscribe/unsubscribe idempotent (see below).
    typealias SubscriberID = UUID

    /// Register `owner`'s interest in a game's full snapshot. Idempotent:
    /// subscribing when (owner, gameId) is ALREADY active is a no-op — it can't
    /// double-count. The FIRST distinct owner for an id (set 0→1) starts that
    /// id's /live/games/{id} loop; later distinct owners share it. `immediate`
    /// gives the new subscriber an instant fetch instead of waiting an interval.
    func subscribeDetail(_ gameId: Int, owner: SubscriberID, immediate: Bool = false)

    /// Release `owner`'s interest. Idempotent: unsubscribing when (owner, gameId)
    /// is NOT active is a no-op — it can't drive the count negative or stop a
    /// loop someone else needs. The LAST distinct owner leaving (set 1→0) stops
    /// the loop and evicts detail[gameId].
    func unsubscribeDetail(_ gameId: Int, owner: SubscriberID)

    // MARK: Internals (sketch — not public)
    // private var listTask: Task<Void, Never>?
    // private var detailTasks: [Int: Task<Void, Never>] = [:]
    // private var detailOwners: [Int: Set<SubscriberID>] = [:]   // refcount = set.count
    // private let api: APIClient = .shared
}
```

### Refcounting — leak-proof by construction (token-set model)

The refcount is NOT a raw integer that two code paths increment/decrement — it is
the **cardinality of a set of distinct subscriber tokens**:
`detailOwners: [Int: Set<SubscriberID>]`, and the effective refcount for an id is
`detailOwners[id]?.count ?? 0`. Because a `Set` dedups, the count can only ever
move by adding/removing DISTINCT `(owner, gameId)` membership — so both operations
are structurally idempotent and the count can never go negative.

- **`subscribeDetail(id, owner:)`**: `detailOwners[id, default: []].insert(owner)`.
  `Set.insert` returns `(inserted: Bool, _)`. If `inserted == false`, the owner
  was already subscribed → **no-op**. If `inserted == true` and the set went 0→1,
  start that id's detail loop. If it went n→n+1 (n≥1) the loop already runs; if
  `immediate`, kick a one-shot fetch so the new subscriber doesn't wait.
- **`unsubscribeDetail(id, owner:)`**: `detailOwners[id]?.remove(owner)`.
  `Set.remove` returns the removed element or `nil`. If it returns `nil`, the
  owner wasn't subscribed → **no-op**. If the set is now empty (1→0),
  `detailTasks[id]?.cancel(); detailTasks[id] = nil; detailOwners[id] = nil;
  detail[id] = nil` (stop loop + evict snapshot). Otherwise the loop keeps running.

### Idempotency guarantees (why both cleanup paths are safe)

- **subscribe-while-subscribed = no-op.** The `.onChange(true)` and `.task` arm
  paths can both fire for the same (owner, id) without double-counting — the
  second `insert` returns `inserted == false`.
- **unsubscribe-while-unsubscribed = no-op.** The `.onChange(false)` and
  `.onDisappear` paths can BOTH fire for the same (owner, id) — the second
  `remove` returns `nil` and does nothing. No premature stop, no negative count.
- **Clamp at 0 is automatic** — an empty set is evicted; there is no negative
  state to reach. (We still treat "not present" as 0 defensively.)

Because each surface owns exactly ONE stable token, a surface can call subscribe
in one place and unsubscribe in two (or vice versa) and the store still ends in
the correct state — the leak/double-stop failure modes are eliminated by the data
model, not by careful call-site pairing.

**Invariant (unchanged):** a detail loop exists for id **iff**
`detailOwners[id]` is non-empty (refcount ≥ 1).

### Box-score-over-card 0↔1↔2 under the token model

Card token `C`, box-score token `B`, same game id `g`:

1. Card appears → `subscribeDetail(g, owner: C)` → `{C}` (0→1) → **loop starts**.
2. Box score pushed over it (card stays alive, `.scores` still visible) →
   `subscribeDetail(g, owner: B)` → `{C, B}` (1→2) → loop already running, no
   second loop. If either fires its `.task`/`.onChange` again, re-inserting `C`
   or `B` is a no-op.
3. Box score closes → `unsubscribeDetail(g, owner: B)` → `{C}` (2→1) → loop KEEPS
   running (card still needs it). A stray double `unsubscribe(g, B)` from both
   `.onChange(false)` and `.onDisappear` is a no-op the second time.
4. Leave the Scores tab → card also `unsubscribeDetail(g, owner: C)` → `{}` (1→0)
   → **loop stops**, `detail[g]` evicted.

One shared loop throughout; correct start/stop at the 0↔1↔2 boundaries; no path
can leak the loop or stop it early.

### How surfaces read

- Scores list / Home card: read `store.liveList[gamePk]` (a `LiveGameSummary`).
  They can keep applying `Game.merging(live:)` (`LiveGame.swift:508`, already
  keyed by `gamePk`) to fold score/inning into their `[Game]`, OR read fields
  directly — either way the DATA now comes from the store, not their own loop.
- LiveGameCard / box score / Home situation: read `store.detail[gamePk]` (a full
  `LiveGameDetail`). The box score additionally derives `plays` / `boxScore` from
  that same `LiveGameDetail` (it already knows how — `toBoxScoreResponse()` /
  `playsAsBDL` / `toLiveFeedResponse()`).

Because `liveList` and `detail` are `@Published` dictionaries, any view holding
the store via `@EnvironmentObject`/`@ObservedObject` re-renders when the store
replaces the dictionary value (see Risks §6 on republishing).

---

## (2) Lifecycle integration (reuse `shouldPoll(on:)`, don't duplicate)

The store exposes only `startListLoop` / `stopListLoop` / `subscribeDetail` /
`unsubscribeDetail`. **Surfaces** decide when to call them, driven by the SAME
`.onChange(of: navigation.shouldPoll(on:))` pattern already committed. The store
never reads `AppNavigation`.

Per surface (mirrors the committed wiring, just calling store methods instead of
per-VM start/stop):

```swift
// Scores list (owning tab .scores)
.task { if navigation.shouldPoll(on: .scores) { store.startListLoop() } }
.onChange(of: navigation.shouldPoll(on: .scores)) { _, on in
    on ? store.startListLoop(immediate: true) : store.stopListLoop()
}
.onDisappear { store.stopListLoop() }

// A LiveGameCard / box score (subscribe its id, gated the same way).
// `liveToken` is a stable per-view identity: @State private var liveToken = UUID()
.task { if navigation.shouldPoll(on: owningTab) { store.subscribeDetail(pk, owner: liveToken) } }
.onChange(of: navigation.shouldPoll(on: owningTab)) { _, on in
    on ? store.subscribeDetail(pk, owner: liveToken, immediate: true)
       : store.unsubscribeDetail(pk, owner: liveToken)
}
.onDisappear { store.unsubscribeDetail(pk, owner: liveToken) }
```

**When a tab hides** (`shouldPoll` flips false): that tab's surfaces call
`stopListLoop()` and `unsubscribeDetail(id)` for their ids. So both the list loop
and that tab's detail subscriptions pause. **On return** (`shouldPoll` true):
`startListLoop(immediate: true)` and `subscribeDetail(id, immediate: true)` re-arm
with an instant refresh — no waiting a full interval, no reactive re-arm loop
(the `immediate` flag is opt-in exactly as in the committed work).

**Pause vs unsubscribe — the key decision:** on hide, a surface **unsubscribes**
(decrements refcount) rather than "pausing" its loop. Rationale: unsubscribe is
already the correct primitive — if the LAST interested surface hides, the loop
should stop (refcount 0→1 inverse); if ANOTHER surface still wants the id (e.g. a
box score open over a card, both `.scores`), the refcount stays ≥1 and the loop
keeps running. "Pause the loop" would need a second axis of state; refcount
already expresses "does anyone visible still need this id." The list loop has no
refcount — it's a single shared loop, so hide = `stopListLoop()`, show =
`startListLoop()`. (Only one tab is ever visible, so exactly one caller drives the
list loop at a time.)

Edge: a surface pushed over another in the SAME tab (box score over card) does
NOT get an `onDisappear` when covered, and `shouldPoll(.scores)` stays true for
both — so both remain subscribed, refcount = 2, one shared loop. Correct by
construction (see §6).

---

## (3) Per-surface migration

| Surface | Reads from store AFTER | Code deleted |
|---|---|---|
| **Scores list** (`ScoresViewModel`) | `store.liveList` → fold into `games` via `merging(live:)` (or read directly) | `ScoresViewModel.startAutoRefresh` / `stopAutoRefresh` / `refreshLive`'s `getLiveGames()` call + its `refreshTask` list loop (`ScoresView.swift:87,266,287`) |
| **Home card** (`HomeViewModel`) | `store.liveList` → fold into `recentAndUpcoming`/`nextGame` via `merging(live:)` | `HomeViewModel.startAutoRefresh` / `stopAutoRefresh` / `refreshLive`'s `getLiveGames()` + `refreshTask` (`HomeViewModel.swift:275,302,318`) |
| **Home situation panel** | `store.detail[favPk]` (bases/outs/matchup) | `TeamHeroCard.liveVM` (`LiveFeedViewModel`) + its `.task`/`.onChange` start/stop (`HomeView.swift:438,489–504`) |
| **LiveGameCard** | `store.detail[pk]` (situation + linescore grid) | per-card `feed` (`LiveFeedViewModel`) + its `.task`/`.onChange` (`ScoresView.swift:1364,1384–1399`) |
| **Box score** (`BoxScoreViewModel`) | `store.detail[pk]` → derive `live`/`plays`/`boxScore` from the shared `LiveGameDetail` | `BoxScoreViewModel.startLivePolling` / `stopLivePolling` / `loadLiveFromBackend`'s own `getLiveGame(id:)` loop (`BoxScoreView.swift:478`) — the display accessors stay, now fed by the store |

**Sheet-boundary caveat (already hit in Phase 1):** `@EnvironmentObject` does NOT
cross a `.sheet`. The box score pushed from `ScheduleSheet` (a sheet) already
receives `navigation` explicitly for this reason; `LiveGameStore` must be passed
the SAME way to any sheet-hosted surface (add an explicit `@ObservedObject var
liveStore` init param to `BoxScoreView`, forwarded by `ScheduleSheet`, exactly as
`navigation` is). In-tab surfaces (Scores list, LiveGameCard, HomeView) read it
via `@EnvironmentObject` normally.

---

## (4) Injection

Follow the `AppNavigation` precedent exactly (`ContentView.swift:16,48`):

```swift
// ContentView
@StateObject private var liveStore = LiveGameStore()
…
    .environmentObject(navigation)
    .environmentObject(liveStore)      // NEW — same root injection
```

Read via `@EnvironmentObject private var liveStore: LiveGameStore` in in-tab
views. Pass **explicitly** to sheet-hosted surfaces (`BoxScoreView` from
`ScheduleSheet`) as an `@ObservedObject` init param, mirroring how `navigation`
is already threaded there.

---

## (5) Migration order + per-step verification (the safety spine)

Each step is independently shippable and leaves every surface functional (a
surface reads the store OR its old loop, never neither). Verify on a LIVE game
before proceeding; watch Railway logs to confirm call volume moves as expected.

1. **Build `LiveGameStore` + inject at root.** Nothing reads it yet.
   *Verify:* app builds; no behavior change; store is reachable via
   `@EnvironmentObject` (a throwaway `print` on a view proves injection). Railway
   call volume unchanged.

2. **Migrate Scores list** → `ScoresViewModel` reads `store.liveList`; start the
   store's list loop from `ScoresView`'s lifecycle wiring; delete the VM's list
   loop. Cards still use their own `feed`.
   *Verify:* on the Scores tab with a live game, scores/inning still update every
   ~15s; background/tab-switch still pauses (list loop stops); return refreshes
   immediately. Railway: `/live/games` still ~1 call/15s while Scores visible.

3. **Migrate Home card** → `HomeViewModel` reads `store.liveList`; delete its list
   loop.
   *Verify:* Home hero score/inning still update; switching Home↔Scores does not
   double-fetch the list (only the visible tab drives `startListLoop`). Railway:
   no second `/live/games` loop.

4. **Migrate `LiveGameCard`** → subscribe `store.detail[pk]`; delete each card's
   `LiveFeedViewModel`.
   *Verify:* each live card's situation + linescore still update; N live games →
   Railway shows N distinct `/live/games/{id}` (one per id, not per render);
   tab-hide unsubscribes all N (loops stop).

5. **Migrate box score** → subscribe `store.detail[pk]`; `BoxScoreViewModel`
   derives `live`/`plays`/`boxScore` from the shared `LiveGameDetail`; delete its
   own detail loop. Thread the store explicitly through `ScheduleSheet`.
   *Verify:* banner/linescore/situation/plays still track live (the Phase 1
   behavior); crucially, opening a box score OVER its Scores card does NOT create
   a second `/live/games/{id}` for that id (refcount 2 → ONE shared loop —
   confirm via Railway); closing the box score leaves the card's loop running
   (refcount 2→1), and leaving the tab stops it (→0).

6. **Delete `LiveFeedViewModel`** and any now-dead per-VM list loops once no
   surface references them.
   *Verify:* build succeeds with the type removed; grep shows zero references;
   full live regression on all three surfaces once more.

---

## (6) Risks (the tricky parts)

- **Refcount leaks / double-unsubscribe — solved structurally (token-set
  model).** The failure modes are a leaked loop (subscribe with no matching
  unsubscribe) and a premature stop / negative count (double unsubscribe). We
  eliminate BOTH by design rather than by call-site discipline: refcount is
  `detailOwners[id].count`, a `Set<SubscriberID>`, and each surface owns exactly
  ONE stable token (`@State` UUID). `Set.insert`/`Set.remove` make
  subscribe-while-subscribed and unsubscribe-while-unsubscribed no-ops (see §1
  "Idempotency guarantees"), so a surface can fire subscribe from `.task` and
  `.onChange(true)`, and unsubscribe from `.onChange(false)` AND `.onDisappear`,
  in any interleaving, and the store still lands in the correct state. The count
  is a set cardinality, so it can never go negative; clamp-at-0 is automatic
  (empty set → evicted). What STILL must hold: the token is stable per view
  instance (use `@State`, not a fresh UUID per render) and unique per surface
  (don't share one token across two surfaces that must be counted separately).
  Both are easy to verify at each migration step.

- **Box-score-over-card concurrency (the case the store is meant to fix).** Card
  and box score are both under `.scores` and both alive; both subscribe the same
  id → refcount 2 → ONE shared `/live/games/{id}` loop. Must verify: (a) opening
  the box score does NOT start a second loop; (b) the box score `unsubscribe` on
  close/hide decrements to 1 and the card's loop KEEPS running; (c) only leaving
  the tab (both unsubscribe) drops to 0 and stops it. This is exactly the
  0↔1↔2 refcount transition to test on a live game with Railway logs.

- **SwiftUI republishing.** Views must re-render when `store.detail[id]` (or
  `liveList`) changes. `@Published var detail: [Int: LiveGameDetail]` republishes
  when the store REPLACES the dictionary value (`detail[id] = newSnapshot`) — that
  is a mutation of the published property, so it fires `objectWillChange` and any
  `@EnvironmentObject`/`@ObservedObject` consumer re-renders. Confirm the store
  always assigns a fresh value (it does — each poll decodes a new
  `LiveGameDetail` and assigns), and that surfaces read `store.detail[id]`
  in `body` (so they're registered as dependents). Watch for over-rendering: a
  single `@Published` dict means ANY id's update re-renders EVERY view reading the
  store; if that shows up as jank with many live games, split detail into
  per-id observable holders later — note it, don't pre-optimize.

- **List-loop single-owner assumption.** `startListLoop` is called by whichever
  tab is visible; since only one tab is visible at a time, there's one caller. But
  Home↔Scores transitions must not leave two list loops: `startListLoop` must be
  self-cancelling (cancel+replace), and the leaving tab's `stopListLoop` must fire.
  Verify no lingering list loop after rapid tab switching (Railway: exactly one
  `/live/games` cadence at all times, zero when no live-tab is visible).

- **Backend load if detail is polled for ALL live games.** If every `LiveGameCard`
  subscribes detail, a big slate (say 10 live games) = 10 concurrent
  `/live/games/{id}` loops. That's the same as today's per-card `LiveFeedViewModel`
  count (no worse), but the store makes it easy to accidentally subscribe more.
  Keep subscription scoped to on-screen cards; if load is a concern, a later phase
  could serve card situations from the LIST payload (Phase 1 already enriched it
  with batter/pitcher/last_play) and reserve detail loops for the box score only —
  note as a follow-up, not part of this phase.
