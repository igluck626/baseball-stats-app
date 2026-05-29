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

    init(bdl: BallDontLieClient = .shared) {
        self.bdl = bdl
    }

    /// One-shot fetch + start a 30s polling loop. Idempotent — if
    /// the loop is already running it cancels the old one first so
    /// changing gameId (rare, but possible across re-mounts) doesn't
    /// leak a stale poller.
    func start(gameId: Int) async {
        stop()
        await fetch(gameId: gameId)
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
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
        do {
            // Fetch the play and PA streams in parallel; combine
            // via the synthesizer in Scores.swift to produce a
            // legacy LiveFeedResponse the UI already knows.
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
    /// `true` once the user has tapped the header, so subsequent
    /// scoring-play arrivals don't fight whatever state they chose.
    @State private var hasUserToggled = false
    /// `true` after we've auto-populated `expandedHalfInnings` with
    /// the most-recent half-inning. Prevents re-collapsing the
    /// user's manual expansions on every live-poll tick.
    @State private var didAutoExpandHalfInning = false
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
                    .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
            }
        }
        .onChange(of: plays) { _, new in
            let newScoringCount = new.filter(\.scoringPlay).count
            defer { prevScoringCount = newScoringCount }
            // Auto-populate the All-mode expansion ONCE so the most
            // recent inning is visible without scrolling.
            if !didAutoExpandHalfInning, !new.isEmpty,
               let last = new.last, let type = last.inningType {
                expandedHalfInnings = [Self.halfInningKey(inningType: type, inning: last.inning)]
                didAutoExpandHalfInning = true
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
        let scoring = plays.filter(\.scoringPlay)
        return Group {
            if scoring.isEmpty {
                Text("No scoring plays yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(scoring, id: \.order) { p in
                        scoringPlayRow(p)
                    }
                }
            }
        }
    }

    private func scoringPlayRow(_ p: BDLPlay) -> some View {
        let arrow = (p.inningType ?? "").hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(p.inning)
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .top, spacing: 8) {
                Text("\(arrow)\(ord)")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.red)
                    .frame(width: 38, alignment: .leading)
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

    // MARK: All mode

    private var allPlaysList: some View {
        let groups = Self.groupedHalfInnings(plays)
        return LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(groups, id: \.id) { half in
                halfInningSection(half: half)
            }
        }
    }

    private func halfInningSection(half: HalfInning) -> some View {
        let isHalfExpanded = expandedHalfInnings.contains(half.id)
        let arrow = half.inningType.hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(half.inning)
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
                    Text("(\(half.plays.count) play\(half.plays.count == 1 ? "" : "s"))")
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
                ForEach(half.plays, id: \.order) { play in
                    Text(play.text ?? "")
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.leading, 12)
                        .padding(.bottom, 4)
                }
            }
            Divider().opacity(0.3)
        }
    }

    // MARK: Helpers

    private func scoreLineText(awayScore: Int, homeScore: Int) -> String {
        "\(awayAbbr) \(awayScore), \(homeAbbr) \(homeScore)"
    }

    private struct HalfInning: Identifiable, Hashable {
        let id: String
        let inning: Int
        let inningType: String
        let plays: [BDLPlay]
    }

    private static func groupedHalfInnings(_ plays: [BDLPlay]) -> [HalfInning] {
        var order: [String] = []
        var bucket: [String: (inning: Int, type: String, plays: [BDLPlay])] = [:]
        for p in plays {
            let type = p.inningType ?? "?"
            let key = halfInningKey(inningType: type, inning: p.inning)
            if bucket[key] == nil {
                order.append(key)
                bucket[key] = (p.inning, type, [p])
            } else {
                bucket[key]?.plays.append(p)
            }
        }
        return order.compactMap { key in
            guard let b = bucket[key] else { return nil }
            return HalfInning(id: key, inning: b.inning, inningType: b.type, plays: b.plays)
        }
    }

    fileprivate static func halfInningKey(inningType: String, inning: Int) -> String {
        "\(inningType)-\(inning)"
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
