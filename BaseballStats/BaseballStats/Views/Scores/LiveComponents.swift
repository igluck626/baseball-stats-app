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
