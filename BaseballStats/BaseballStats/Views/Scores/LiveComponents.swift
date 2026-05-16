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
//  `.task { await vm.start(gamePk:) }` kicks off the initial fetch
//  and timer; the timer self-cancels when the response shows the
//  game has gone final.
//

import Combine
import SwiftUI

// MARK: - Live feed view model

@MainActor
final class LiveFeedViewModel: ObservableObject {
    @Published var live: LiveFeedResponse?
    @Published var error: String?

    private var task: Task<Void, Never>?
    private let api: MLBStatsAPIClient

    init(api: MLBStatsAPIClient = .shared) {
        self.api = api
    }

    /// One-shot fetch + start a 30s polling loop. Idempotent — if
    /// the loop is already running it cancels the old one first so
    /// changing gamePk (rare, but possible across re-mounts) doesn't
    /// leak a stale poller.
    func start(gamePk: Int) async {
        stop()
        await fetch(gamePk: gamePk)
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.fetch(gamePk: gamePk)
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

    private func fetch(gamePk: Int) async {
        do {
            live = try await api.getLiveFeed(gamePk: gamePk)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// True iff the linescore reports no live inning state — i.e. the
    /// game finished between the last poll and this one. Used to
    /// terminate the polling loop.
    private var isGameOver: Bool {
        let state = live?.liveData.linescore?.inningState?.lowercased()
        return state == nil || state == "final" || state == "game over"
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

/// Logo cell used across every Scores-tab card. Resolves
/// `midfield.mlbstatic.com/v1/team/{id}/spots/120` via `TeamInfo`'s
/// `id`, falling back to a styled abbreviation circle when the CDN
/// 404s (rare: All-Star team IDs, certain minor-league rehab
/// assignments) so the row never collapses to a featureless dot.
///
/// Failure cases print the offending URL once via `.onAppear` so
/// future logo gaps surface in console logs instead of staying
/// silent behind the placeholder.
struct TeamLogoView: View {
    let team: TeamInfo
    var size: CGFloat = 28

    var body: some View {
        AsyncImage(url: team.logoURL) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFit()
            case .failure(let error):
                fallback
                    .onAppear {
                        let url = team.logoURL?.absoluteString ?? "nil"
                        let label = team.abbreviation ?? team.name
                        print("[team-logo] FAILED \(label) (id=\(team.id)) url=\(url) error=\(error)")
                    }
            case .empty:
                placeholder
            @unknown default:
                fallback
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
