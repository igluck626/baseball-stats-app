//
//  LiveComponents.swift
//  BaseballStats
//
//  Shared building blocks for the live-game surfaces in the Scores
//  tab. Two visual primitives (`BaseRunnerView`, `LiveBadge`) plus
//  the `LiveFeedViewModel` that drives both the live game card on
//  ScoresView and the live header on BoxScoreView.
//
//  The view model owns its own 30-second poll loop so consumers can
//  drop it in with `@StateObject` and forget about lifecycle —
//  `.task { await vm.start(gameId:) }` kicks off the initial fetch
//  and timer; the timer self-cancels when the play stream shows the
//  game has finished (last play type == "End of Game").
//
//  Backed by BallDontLie's `/plays` + `/plate_appearances` streams
//  (Phase 3 of the MLB-Stats-API → BDL migration). The two streams
//  are synthesized into a legacy `LiveFeedResponse` via the
//  extension in `Scores.swift` so the live UI doesn't need to
//  branch on the data source.
//

import Combine
import SwiftUI

// MARK: - Live feed view model

@MainActor
final class LiveFeedViewModel: ObservableObject {
    @Published var live: LiveFeedResponse?
    @Published var error: String?

    private var task: Task<Void, Never>?
    private let bdl: BallDontLieClient
    private let api: APIClient

    init(bdl: BallDontLieClient = .shared, api: APIClient = .shared) {
        self.bdl = bdl
        self.api = api
    }

    /// One-shot fetch + start a 15s polling loop. Idempotent — if
    /// the loop is already running it cancels the old one first so
    /// changing gameId (rare, but possible across re-mounts) doesn't
    /// leak a stale poller.
    func start(gameId: Int) async {
        stop()
        await fetch(gameId: gameId)
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.fetch(gameId: gameId)
                // Stop polling once the game is final — no point
                // burning network on a frozen response.
                if self.isGameOver { return }
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    private func fetch(gameId: Int) async {
        // Phase 2: read the unified snapshot from our backend live proxy
        // (shared cache, consistent state). Falls back to the direct
        // balldontlie path only if the proxy has no snapshot yet (e.g. a
        // just-started game) or once the game has gone final (the proxy stops
        // serving it, so `isGameOver` can fire and the loop terminates).
        if let detail = try? await api.getLiveGame(id: gameId) {
            live  = detail.toLiveFeedResponse()
            error = nil
            return
        }
        do {
            async let playsTask = bdl.getPlays(gameId: gameId)
            async let pasTask   = bdl.getPlateAppearances(gameId: gameId)
            let plays = try await playsTask
            let pas   = (try? await pasTask) ?? []
            live  = plays.toLiveFeedResponse(plateAppearances: pas)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// True once the synthesized linescore signals the game is
    /// done — the synthesizer maps BDL's "End Inning"/"End of Game"
    /// hints into the legacy `inningState` field where this getter
    /// expects them.
    private var isGameOver: Bool {
        let state = live?.liveData.linescore?.inningState?.lowercased()
        return state == "final" || state == "game over"
    }
}

// MARK: - Base runner diamond

/// Three filled/outlined squares arranged in a baseball diamond:
/// second at top, third at left, first at right (home plate is the
/// implicit bottom-center anchor). Each square is rotated 45° to
/// read as a diamond. Fill = runner on that base.
struct BaseRunnerView: View {
    let first: Bool
    let second: Bool
    let third: Bool

    /// Side length of the bounding square. Bases are sized
    /// proportionally so the view scales cleanly between the live
    /// game card (compact) and the box score header (larger).
    var size: CGFloat = 32

    var body: some View {
        ZStack {
            base(filled: second).offset(y: -size * 0.32)
            base(filled: third).offset(x: -size * 0.32)
            base(filled: first).offset(x: size * 0.32)
        }
        .frame(width: size, height: size)
    }

    private func base(filled: Bool) -> some View {
        Rectangle()
            .fill(filled ? Color.accentColor : Color.clear)
            .overlay(
                Rectangle()
                    .stroke(Color.primary.opacity(0.6), lineWidth: 1)
            )
            .frame(width: size * 0.28, height: size * 0.28)
            .rotationEffect(.degrees(45))
    }
}

// MARK: - Team logo

/// In-memory cache of successfully-loaded team logos, keyed by MLB
/// team id. Lives at the app level so individual `TeamLogoView`
/// instances can come and go (the Scores tab's 30s auto-refresh
/// recreates the list views, which previously cancelled in-flight
/// `AsyncImage` downloads with NSURLErrorCancelled / -999 and never
/// completed). The cache's own `Task` owns the network call, so a
/// view tear-down no longer interrupts the download.
///
/// Concurrent requests for the same team coalesce: the first view
/// to ask kicks off the loader, every subsequent view becomes a
/// no-op observer of the same `@Published` `images` dict.
@MainActor
final class TeamLogoCache: ObservableObject {
    static let shared = TeamLogoCache()

    /// Successfully-loaded logo images, keyed by MLB team id.
    /// `@Published` so views re-render the moment a logo lands.
    @Published private(set) var images: [Int: Image] = [:]
    /// Team ids whose load failed. Surfaces the abbreviation
    /// fallback without retrying the (likely-still-bad) URL on
    /// every view rebuild.
    @Published private(set) var failed: Set<Int> = []

    /// Active loader tasks per team id. Owned by the cache (not
    /// the views) so they survive view recreation. Keyed for
    /// dedupe — if a load is already running, additional `ensure`
    /// calls are no-ops.
    private var loaders: [Int: Task<Void, Never>] = [:]

    private init() {}

    /// Kick off a logo fetch if not already cached or in flight.
    /// Returns immediately — observers re-render via `images` once
    /// the load completes.
    func ensureLoaded(team: TeamInfo) {
        if images[team.id] != nil { return }
        if failed.contains(team.id) { return }
        if loaders[team.id] != nil { return }
        guard let url = team.logoURL else { return }

        loaders[team.id] = Task { @MainActor [weak self] in
            defer { self?.loaders[team.id] = nil }
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                guard let uiImage = UIImage(data: data) else {
                    self?.failed.insert(team.id)
                    return
                }
                self?.images[team.id] = Image(uiImage: uiImage)
            } catch {
                let label = team.abbreviation ?? team.name
                print("[team-logo] FAILED \(label) (id=\(team.id)) url=\(url) error=\(error)")
                self?.failed.insert(team.id)
            }
        }
    }
}

/// Logo cell used across every Scores-tab card. Reads from
/// `TeamLogoCache.shared` so once a logo lands, every subsequent
/// instance (across navigation, auto-refresh ticks, etc.) renders
/// from memory instead of re-fetching. Falls back to a styled
/// abbreviation circle when the CDN won't serve the team.
struct TeamLogoView: View {
    let team: TeamInfo
    var size: CGFloat = 28
    @ObservedObject private var cache = TeamLogoCache.shared

    var body: some View {
        Group {
            if let cached = cache.images[team.id] {
                cached.resizable().scaledToFit()
            } else if cache.failed.contains(team.id) {
                fallback
            } else {
                placeholder.onAppear { cache.ensureLoaded(team: team) }
            }
        }
        .frame(width: size, height: size)
    }

    /// In-flight placeholder — a plain muted circle, matching the
    /// surface tone of the cards while the network round-trip is
    /// outstanding.
    private var placeholder: some View {
        Circle().fill(Color(.secondarySystemFill))
    }

    /// Permanent fallback when the CDN never returns an image —
    /// abbreviation centered in the same circle so the user still
    /// sees a useful team identifier instead of an anonymous dot.
    private var fallback: some View {
        let abbr = team.abbreviation ?? String(team.name.prefix(3)).uppercased()
        return Circle()
            .fill(Color(.secondarySystemFill))
            .overlay(
                Text(abbr)
                    .font(.system(size: max(8, size * 0.32), weight: .semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            )
    }
}

// MARK: - LIVE badge

/// Small red "LIVE" capsule with a pulsing dot. The pulse runs
/// while the view is on screen — `.onAppear` flips the animatable
/// state once and the `repeatForever` modifier carries it from
/// there.
struct LiveBadge: View {
    @State private var pulse = false

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(Color.white)
                .frame(width: 6, height: 6)
                .opacity(pulse ? 0.4 : 1.0)
                .animation(
                    .easeInOut(duration: 0.9).repeatForever(autoreverses: true),
                    value: pulse
                )
            Text("LIVE")
                .font(.caption2.weight(.heavy))
                .foregroundStyle(.white)
                .kerning(0.5)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(Capsule().fill(Color.red))
        .onAppear { pulse = true }
        .accessibilityLabel("Live")
    }
}

// MARK: - Plays

/// Expandable plays section for the box score. Shows a `Plays ▾`
/// header row that toggles between collapsed (just the header) and
/// expanded (segmented Scoring/All picker + the play list).
///
/// All plays-related state lives here — mode pick, set of expanded
/// half-innings in All-mode, the "user has manually toggled" guard.
/// The parent passes in the raw `BDLPlay` stream and the two team
/// abbreviations for the score-line formatting.
///
/// `autoExpandOnScoring` (true for live games): when a new scoring
/// play arrives we auto-expand AND flip to Scoring mode — once. The
/// user-toggle guard prevents the section from re-popping open
/// after they've collapsed it.
struct PlaysView: View {
    let plays: [BDLPlay]
    let awayAbbr: String
    let homeAbbr: String
    let autoExpandOnScoring: Bool
    /// When true, render the header + expanded body inline without
    /// the standalone glass-card wrapper. The caller is responsible
    /// for providing the container (live situation / linescore card).
    var isEmbedded: Bool = false

    @State private var isExpanded = false
    @State private var playsMode: PlaysMode = .scoring
    @State private var expandedHalfInnings: Set<String> = []
    /// Set of `AtBat.id` keys whose individual pitch list is
    /// currently expanded inside the All-mode view. Empty by
    /// default — every at-bat starts collapsed to just the
    /// outcome headline.
    @State private var expandedAtBats: Set<String> = []
    /// `true` once the user has tapped the header, so subsequent
    /// scoring-play arrivals don't fight whatever state they chose.
    @State private var hasUserToggled = false
    /// The half-inning key we last auto-expanded. Initial fetch
    /// auto-expands the latest half-inning; subsequent ticks only
    /// auto-expand when the latest half-inning key *changes* (i.e.
    /// the game advances to Top of the next inning, or to Bottom
    /// after the half flips), preserving the user's manual collapse
    /// of any inning that doesn't change.
    @State private var lastAutoExpandedHalfInningKey: String?
    /// Tracks the previous scoring-play count so we only react to
    /// the LATCH from N → N+1, not to every plays update.
    @State private var prevScoringCount = 0

    enum PlaysMode: String, Hashable, Identifiable, CaseIterable {
        case scoring = "Scoring"
        case all     = "All"
        var id: String { rawValue }
    }

    var body: some View {
        let content = VStack(alignment: .leading, spacing: 8) {
            Button {
                hasUserToggled = true
                withAnimation(.easeInOut(duration: 0.2)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack {
                    Text("Plays")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Spacer()
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider().opacity(0.4)
                Picker("", selection: $playsMode) {
                    ForEach(PlaysMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)

                if plays.isEmpty {
                    Text("Play-by-play not yet available")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    switch playsMode {
                    case .scoring: scoringPlaysList
                    case .all:     allPlaysList
                    }
                }
            }
        }
        return Group {
            if isEmbedded {
                content
            } else {
                content
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
                    .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
            }
        }
        .onChange(of: plays) { _, new in
            let newScoringCount = new.filter(\.scoringPlay).count
            defer { prevScoringCount = newScoringCount }
            // Auto-expand the latest half-inning. Triggers on every
            // transition to a new half-inning so the action stays
            // on-screen without scrolling; same-inning updates do
            // not touch the expansion set (so a user collapse of
            // the current inning stays collapsed). Prior auto-
            // expansions are left in place rather than reset — if
            // the user opened earlier innings, we don't fight them.
            if let last = new.last, let type = last.inningType {
                let latestKey = Self.halfInningKey(inningType: type, inning: last.inning)
                if latestKey != lastAutoExpandedHalfInningKey {
                    expandedHalfInnings.insert(latestKey)
                    lastAutoExpandedHalfInningKey = latestKey
                }
            }
            // Auto-expand on scoring-play arrival for live games,
            // unless the user has already toggled the section.
            if autoExpandOnScoring,
               !hasUserToggled,
               newScoringCount > prevScoringCount {
                withAnimation(.easeInOut(duration: 0.2)) {
                    isExpanded = true
                    playsMode  = .scoring
                }
            }
        }
    }

    // MARK: Scoring mode

    private var scoringPlaysList: some View {
        let rows = scoringRowsWithDelta()
        return Group {
            if rows.isEmpty {
                Text("No scoring plays yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(rows) { row in
                        scoringPlayRow(row)
                    }
                }
            }
        }
    }

    private func scoringPlayRow(_ row: ScoringRow) -> some View {
        let p = row.play
        let arrow = (p.inningType ?? "").hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(p.inning)
        // Pick the scoring team's abbreviation. Both-sides scoring
        // is rare (a single play that scores for both teams) but
        // possible on extremely weird sequences — fall back to a
        // generic label if neither side's delta resolves.
        let scoringAbbr: String? = {
            if row.scoredHome { return homeAbbr }
            if row.scoredAway { return awayAbbr }
            return nil
        }()
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .top, spacing: 8) {
                Text("\(arrow)\(ord)")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.red)
                    .frame(width: 38, alignment: .leading)
                if let abbr = scoringAbbr {
                    Text(abbr)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            Capsule().fill(
                                row.scoredHome
                                    ? Color.blue.opacity(0.8)
                                    : Color.orange.opacity(0.85)
                            )
                        )
                }
                Text(p.text ?? "")
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(scoreLineText(awayScore: p.awayScore, homeScore: p.homeScore))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.leading, 46)
                .monospacedDigit()
        }
    }

    /// Two-pass scorer attribution.
    ///
    /// Pass 1 builds a MONOTONIC score-increase timeline. BDL
    /// ships intermediate plays with peek-then-revert scores
    /// (a "Start Batter/Pitcher" play often previews the AB's
    /// eventual score, then mid-AB pitches dip back to the
    /// pre-AB score, then the result play matches the peek).
    /// The `max(...)` filter drops the noise — we only record a
    /// timeline entry when either side's score actually exceeds
    /// the running max.
    ///
    /// Pass 2 matches each `scoringPlay` to the LATEST timeline
    /// entry at-or-BEFORE this scoring play's index. BDL
    /// typically lands the score increment on the
    /// "Start Batter/Pitcher" play preceding the at-bat-result
    /// play that gets the scoring flag, so the right match is
    /// the most recent backward change. Forward fallback covers
    /// the rare case where BDL delays the score update; the
    /// scoring play's own score is the final fallback.
    private func scoringRowsWithDelta() -> [ScoringRow] {
        guard !plays.isEmpty else { return [] }

        // Pass 1: monotonic score-increase timeline.
        var scoreChanges: [(index: Int, home: Int, away: Int)] = []
        var maxHome = 0
        var maxAway = 0
        for (i, play) in plays.enumerated() {
            let newHome = max(maxHome, play.homeScore)
            let newAway = max(maxAway, play.awayScore)
            if newHome > maxHome || newAway > maxAway {
                scoreChanges.append((i, newHome, newAway))
                maxHome = newHome
                maxAway = newAway
            }
        }

        // Pass 2: prefer backward, then forward, then scoring
        // play's own score.
        var rows: [ScoringRow] = []
        var prevHome = 0
        var prevAway = 0
        for (idx, play) in plays.enumerated() where play.scoringPlay {
            let backward = scoreChanges.last(where: { $0.index <= idx })
            let forward = scoreChanges.first(where: { $0.index > idx })

            let resolvedHome: Int
            let resolvedAway: Int
            if let b = backward, b.home > prevHome || b.away > prevAway {
                resolvedHome = b.home
                resolvedAway = b.away
            } else if let f = forward, f.home > prevHome || f.away > prevAway {
                resolvedHome = f.home
                resolvedAway = f.away
            } else {
                resolvedHome = max(prevHome, play.homeScore)
                resolvedAway = max(prevAway, play.awayScore)
            }
            let scoredHome = resolvedHome > prevHome
            let scoredAway = resolvedAway > prevAway
            rows.append(ScoringRow(
                id:         play.order,
                play:       play,
                scoredHome: scoredHome,
                scoredAway: scoredAway,
            ))
            prevHome = resolvedHome
            prevAway = resolvedAway
        }
        return rows
    }

    private struct ScoringRow: Identifiable, Hashable {
        let id: Int          // BDLPlay.order — unique within a game
        let play: BDLPlay
        let scoredHome: Bool
        let scoredAway: Bool
    }

    // MARK: All mode (PA-grouped)

    private var allPlaysList: some View {
        // Reverse the half-inning groupings so the most-recent
        // half-inning lands at the top of the list. `groupedHalfInnings`
        // preserves BDL's chronological order (oldest first); reading
        // a long live game from the top would otherwise require
        // scrolling all the way down to see what just happened.
        let groups = Array(Self.groupedHalfInnings(plays).reversed())
        return LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(groups) { half in
                halfInningSection(half: half)
            }
        }
    }

    private func halfInningSection(half: HalfInning) -> some View {
        let isHalfExpanded = expandedHalfInnings.contains(half.id)
        let arrow = half.inningType.hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(half.inning)
        let atBatCount = half.atBats.count
        return VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.18)) {
                    if isHalfExpanded { expandedHalfInnings.remove(half.id) }
                    else              { expandedHalfInnings.insert(half.id) }
                }
            } label: {
                HStack(spacing: 8) {
                    Text("\(arrow) \(ord)")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.red)
                    Text("(\(atBatCount) AB\(atBatCount == 1 ? "" : "s"))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: isHalfExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 6)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if isHalfExpanded {
                ForEach(half.atBats) { ab in
                    atBatRow(ab)
                }
            }
            Divider().opacity(0.3)
        }
    }

    private func atBatRow(_ ab: AtBat) -> some View {
        let isPAExpanded = expandedAtBats.contains(ab.id)
        // Earlier pitches are the intermediate ball/strike/foul
        // calls; the LAST play in the PA is the outcome and gets
        // promoted to the headline. The expand-pitches button only
        // shows when there's something to expand beyond the
        // outcome row.
        let pitchCount = max(0, ab.plays.count - 1)
        let resultText = ab.plays.last?.text ?? ab.batterText
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    if let batter = ab.batterText {
                        Text(batter)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Text(resultText ?? "—")
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                if pitchCount > 0 {
                    Button {
                        withAnimation(.easeInOut(duration: 0.18)) {
                            if isPAExpanded { expandedAtBats.remove(ab.id) }
                            else            { expandedAtBats.insert(ab.id) }
                        }
                    } label: {
                        HStack(spacing: 2) {
                            Image(systemName: isPAExpanded ? "chevron.down" : "chevron.right")
                                .font(.caption2.weight(.semibold))
                            Text("\(pitchCount) pitch\(pitchCount == 1 ? "" : "es")")
                                .font(.caption2)
                        }
                        .foregroundStyle(.secondary)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.leading, 12)
            if isPAExpanded, pitchCount > 0 {
                // Show the leading intermediate pitches (everything
                // up to but not including the final outcome).
                ForEach(ab.plays.dropLast(), id: \.order) { pitch in
                    Text(pitch.text ?? "")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.leading, 28)
                }
            }
        }
        .padding(.vertical, 2)
    }

    // MARK: Helpers

    private func scoreLineText(awayScore: Int, homeScore: Int) -> String {
        "\(awayAbbr) \(awayScore), \(homeAbbr) \(homeScore)"
    }

    private struct HalfInning: Identifiable, Hashable {
        let id: String
        let inning: Int
        let inningType: String
        let atBats: [AtBat]
    }

    private struct AtBat: Identifiable, Hashable {
        /// Stable key combining the half-inning id with the order
        /// of the at-bat's first play, so SwiftUI's diffing keeps
        /// state aligned across live-poll updates.
        let id: String
        /// Parsed batter name from the "Start Batter/Pitcher" marker
        /// ("Quintana pitches to McCutchen" → "McCutchen"). nil if
        /// the marker text didn't follow the expected shape or if
        /// the half-inning began without one.
        let batterText: String?
        /// Every displayed play in this PA, in order. The LAST one
        /// is treated as the at-bat result; earlier ones are the
        /// intermediate pitches surfaced by the expand button.
        let plays: [BDLPlay]
    }

    private static func groupedHalfInnings(_ plays: [BDLPlay]) -> [HalfInning] {
        // First pass: bucket by (normalized inningType, inning),
        // preserving BDL's chronological order. Store the
        // NORMALIZED `Top` / `Bottom` so the half-inning header's
        // arrow lookup (`hasPrefix("Top")`) doesn't get fooled by
        // BDL casing variants or transition markers.
        var keys: [String] = []
        var buckets: [String: (inning: Int, type: String, plays: [BDLPlay])] = [:]
        for p in plays {
            let rawType = p.inningType ?? "?"
            let key = halfInningKey(inningType: rawType, inning: p.inning)
            let normalized = normalizedInningType(rawType)
            if buckets[key] == nil {
                keys.append(key)
                buckets[key] = (p.inning, normalized, [p])
            } else {
                buckets[key]?.plays.append(p)
            }
        }
        // Second pass: split each bucket into at-bats by walking
        // the play list and using "Start Batter/Pitcher" markers
        // as PA boundaries. Inning-transition plays and
        // start/end markers are dropped from the display set.
        return keys.compactMap { key in
            guard let b = buckets[key] else { return nil }
            let atBats = atBats(in: b.plays, halfInningKey: key)
            return HalfInning(
                id:         key,
                inning:     b.inning,
                inningType: b.type,
                atBats:     atBats,
            )
        }
    }

    private static func atBats(in plays: [BDLPlay], halfInningKey: String) -> [AtBat] {
        var result: [AtBat] = []
        var currentBatter: String? = nil
        var currentPlays: [BDLPlay] = []
        var firstOrder: Int? = nil

        func flush() {
            guard !currentPlays.isEmpty || currentBatter != nil else { return }
            let orderKey = firstOrder.map(String.init) ?? "x\(result.count)"
            result.append(AtBat(
                id:         "\(halfInningKey)/\(orderKey)",
                batterText: currentBatter,
                plays:      currentPlays,
            ))
            currentBatter = nil
            currentPlays = []
            firstOrder = nil
        }

        for p in plays {
            if p.type == "Start Batter/Pitcher" {
                flush()
                currentBatter = parseBatter(from: p.text)
                firstOrder = p.order
                continue
            }
            if !shouldDisplay(p) { continue }
            currentPlays.append(p)
            if firstOrder == nil { firstOrder = p.order }
        }
        flush()
        return result
    }

    /// True iff the play should appear in the All-mode display.
    /// Filters out inning markers and Start/End Batter/Pitcher
    /// transitions, plus the textual variants BDL sometimes ships
    /// without the structured type ("Middle of the 7th").
    private static func shouldDisplay(_ p: BDLPlay) -> Bool {
        let excludedTypes: Set<String> = [
            "Start Inning", "End Inning",
            "Start Batter/Pitcher", "End Batter/Pitcher",
        ]
        if let t = p.type, excludedTypes.contains(t) { return false }
        if let text = p.text {
            if text.hasPrefix("Middle of") { return false }
            if text.hasPrefix("End of")    { return false }
            if text.hasPrefix("Start of")  { return false }
        }
        return true
    }

    /// "Quintana pitches to McCutchen" → "McCutchen".
    /// Falls back to the raw text if the " to " separator isn't
    /// found.
    private static func parseBatter(from text: String?) -> String? {
        guard let text = text else { return nil }
        if let range = text.range(of: " to ") {
            return String(text[range.upperBound...])
        }
        return text
    }

    /// Normalize the `inningType` to a stable `"Top"` / `"Bottom"`
    /// label before keying. Without this, BDL's casing variants
    /// (`"Top"` vs `"TOP"` vs `"top"`) and mid-inning marker
    /// types (`"Mid-Top"`, `"End-Bottom"`, `""`) build separate
    /// buckets for what's really the same half-inning, producing
    /// three "▼ 1st" headers in the UI.
    fileprivate static func halfInningKey(inningType: String, inning: Int) -> String {
        "\(normalizedInningType(inningType))-\(inning)"
    }

    private static func normalizedInningType(_ raw: String) -> String {
        let lower = raw.lowercased()
        if lower.contains("top") { return "Top" }
        if lower.contains("bot") { return "Bottom" }
        // Mid-inning transition markers ("Middle of 7th") and
        // unknowns fall through to "Top" — they're filtered out of
        // the display by `shouldDisplay` anyway, so the bucket
        // membership only matters for grouping.
        return "Top"
    }

    private static func ordinalInning(_ n: Int) -> String {
        switch n {
        case 1:  return "1st"
        case 2:  return "2nd"
        case 3:  return "3rd"
        default: return "\(n)th"
        }
    }
}
