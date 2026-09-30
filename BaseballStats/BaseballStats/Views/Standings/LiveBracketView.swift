//
//  LiveBracketView.swift
//  BaseballStats
//
//  The current postseason's bracket on the Standings tab: every slot from the
//  Wild Card round to the World Series, at any stage — "NYY/BOS" before a Wild
//  Card series is decided, the series state while it runs, the winner once
//  it's over. Drawn on the same `BracketTreeLayout` as Playoff History.
//
//  Tapping a slot with a series opens `SeriesGamesSheet`: its games, each
//  opening its box score.
//

import Combine
import SwiftUI

@MainActor
final class LiveBracketViewModel: ObservableObject {
    enum State: Equatable {
        case idle
        case loading
        case loaded(LiveBracket)
        case failed(String)
    }

    /// The Standings tab's default, once resolved (`standingsDefault`).
    @Published private(set) var defaultView: StandingsDefault?
    @Published private(set) var state: State = .idle

    private let fetch: (Int) async throws -> LiveBracket

    init(fetch: @escaping (Int) async throws -> LiveBracket = { season in
        try await APIClient.shared.getPostseasonBracket(season: season)
    }) {
        self.fetch = fetch
    }

    var season: Int? {
        if case .bracket(let season) = defaultView { return season }
        return nil
    }

    /// Decide the default and, when it is the bracket, load that season's.
    /// The current year's bracket is fetched first because its first pitch is
    /// one of the rule's inputs; a failure there reads as "no postseason yet".
    func resolveDefault(now: Date = Date(), currentYear: Int, currentYearGamesPlayed: Bool?) async {
        var current: LiveBracket?
        if currentYear >= LiveBracketRules.firstSeason {
            current = try? await fetch(currentYear)
        }
        let rule = standingsDefault(now: now, currentYear: currentYear,
                                    currentYearFirstPitch: current?.firstPitch,
                                    currentYearGamesPlayed: currentYearGamesPlayed)
        defaultView = rule
        guard case .bracket(let season) = rule else { return }
        if season == currentYear, let current {
            state = .loaded(current)
        } else {
            await load(quiet: false)
        }
    }

    /// Reload the bracket's season. `quiet` — the periodic tick — keeps what
    /// is on screen when the fetch fails.
    func load(quiet: Bool) async {
        guard let season else { return }
        if !quiet { state = .loading }
        do {
            state = .loaded(try await fetch(season))
        } catch {
            if !quiet { state = .failed(error.localizedDescription) }
        }
    }
}

// MARK: - The bracket

struct LiveBracketView: View {
    let bracket: LiveBracket
    let onSelect: (BracketSlot) -> Void
    /// Everything in the bracket scales with the text, so a larger Dynamic
    /// Type size makes a larger bracket to scroll rather than text that
    /// overflows fixed boxes.
    @ScaledMetric(relativeTo: .subheadline) private var scale: CGFloat = 1

    var body: some View {
        if let layout = Self.layout(bracket, metrics: BracketMetrics.live.scaled(scale)) {
            BracketTreeCanvas(layout: layout) { item in
                Button { onSelect(item.cell) } label: {
                    SlotBox(slot: item.cell, isFinal: item.isFinal, width: layout.metrics.boxW,
                            height: layout.metrics.boxH)
                }
                .buttonStyle(.plain)
                // Not `.disabled`: that greys the whole box, known teams too.
                .allowsHitTesting(item.cell.series != nil)
                .accessibilityHint(item.cell.series == nil ? "" : "Shows the games")
            }
        } else {
            UntrustedSeedsList(bracket: bracket, onSelect: onSelect)
        }
    }

    /// Slots in bracket order: in each league DS-1 (seed 1) beside the 4/5 Wild
    /// Card that feeds it, DS-2 (seed 2) beside the 3/6. nil when the backend
    /// withheld a league's slots.
    static func layout(_ b: LiveBracket, metrics: BracketMetrics) -> BracketTreeLayout<BracketSlot>? {
        guard b.isDrawable, let al = b.al, let nl = b.nl, let ws = b.worldSeries else { return nil }
        func half(_ lg: LiveBracketLeague) -> BracketHalf<BracketSlot>? {
            guard let ds1 = lg.slot("DS-1"), let ds2 = lg.slot("DS-2"), let cs = lg.slot("CS") else { return nil }
            return BracketHalf(division: [(ds1, lg.slot("WC-4v5")), (ds2, lg.slot("WC-3v6"))], championship: cs)
        }
        guard let alHalf = half(al), let nlHalf = half(nl) else { return nil }
        return .make(al: alHalf, nl: nlHalf, worldSeries: ws, hasWC: true, hasDS: true,
                     metrics: metrics, id: { $0.id })
    }
}

/// One slot: a row per side (colour chip, seed, team or "NYY/BOS", series
/// wins) over the series' status line.
struct SlotBox: View {
    let slot: BracketSlot
    var isFinal = false
    let width: CGFloat
    let height: CGFloat
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(slot.sides.enumerated()), id: \.offset) { _, side in
                sideRow(side)
            }
            Text(slot.statusLine)
                .font(.caption2.weight(.medium))
                .foregroundStyle(slot.state == .inProgress ? Color.accentColor : .secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(width: width, height: height, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(isFinal
                      ? AnyShapeStyle(Color.accentColor.opacity(colorScheme == .dark ? 0.18 : 0.10))
                      : AnyShapeStyle(Color(.secondarySystemGroupedBackground)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .strokeBorder(isFinal ? Color.accentColor.opacity(0.85) : Color(.separator).opacity(0.6),
                              lineWidth: isFinal ? 2 : 1)
        )
        .shadow(color: .black.opacity(0.08), radius: 6, y: 2)
        .accessibilityElement(children: .combine)
    }

    /// The loser of a finished series is muted; everyone else reads at full
    /// strength, since nobody has lost yet.
    private func emphasis(_ side: BracketSide) -> (weight: Font.Weight, style: HierarchicalShapeStyle) {
        guard let winner = slot.winner else { return (.semibold, .primary) }
        return side.team == winner ? (.bold, .primary) : (.regular, .secondary)
    }

    private func sideRow(_ side: BracketSide) -> some View {
        let e = emphasis(side)
        return HStack(spacing: 6) {
            if let team = side.team {
                TeamColorSwatch(code: lahmanCode(forBDLAbbreviation: team), height: 16)
            } else {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(Color(.separator), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                    .frame(width: 6, height: 16)
            }
            label(side, emphasis: e)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Spacer(minLength: 2)
            if let wins = slot.wins(for: side) {
                Text("\(wins)")
                    .font(.subheadline.weight(e.weight))
                    .monospacedDigit()
                    .foregroundStyle(e.style)
            }
        }
    }

    /// A small seed number and the team ("1 TB"), or the undecided side's
    /// candidates. One `Text` by concatenation — an HStack of Texts breaks
    /// mid-word at the accessibility sizes.
    private func label(_ side: BracketSide, emphasis e: (weight: Font.Weight, style: HierarchicalShapeStyle)) -> Text {
        guard let team = side.team else {
            return Text(side.tbdLabel).font(.subheadline).foregroundStyle(.secondary)
        }
        let name = Text(team).font(.subheadline.weight(e.weight)).foregroundStyle(e.style)
        guard let seed = side.seed else { return name }
        return Text("\(seed) ").font(.caption2.weight(.semibold)).foregroundStyle(.secondary) + name
    }
}

/// A league whose seeds can't be trusted gets no slots from the backend, so
/// there is no bracket to draw: its series as a list instead. Better than a
/// bracket with teams in the wrong places.
private struct UntrustedSeedsList: View {
    let bracket: LiveBracket
    let onSelect: (BracketSlot) -> Void

    var body: some View {
        List {
            Section {
                Text("The bracket can't be drawn until the playoff seeds are confirmed.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            ForEach(["AL", "NL"], id: \.self) { lg in
                if let league = bracket.leagues[lg] {
                    Section(lg == "AL" ? "American League" : "National League") {
                        let rows = league.trusted ? league.slots.compactMap(\.series) : league.series
                        ForEach(rows, id: \.self) { s in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.teams.joined(separator: " vs "))
                                    .font(.subheadline.weight(.semibold))
                                if let line = s.games.last(where: { $0.status == "STATUS_FINAL" })?.seriesStatus {
                                    Text(line).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }
}

/// balldontlie abbreviation ("LAD", "CHW") → the Lahman code the colour and
/// abbreviation tables key on ("LAN", "CHA"). The inverse of
/// `BDLRetroMatch.retroToBDL`, minus its franchise-history aliases (FLO, MON,
/// ANA), which would otherwise claim MIA, WSH and LAA.
func lahmanCode(forBDLAbbreviation abbr: String) -> String {
    bdlToLahmanAbbreviation[abbr.uppercased()] ?? abbr.uppercased()
}

private let bdlToLahmanAbbreviation: [String: String] = Dictionary(
    BDLRetroMatch.retroToBDL
        .filter { !["FLO", "MON", "ANA"].contains($0.key) }
        .map { ($0.value, $0.key) },
    uniquingKeysWith: { first, _ in first }
)

// MARK: - Tap-through: a series' games

/// One series' games — date, score or start time, final / live, "(if
/// necessary)" — each opening its box score in this sheet's own stack.
struct SeriesGamesSheet: View {
    let slot: BracketSlot
    /// Passed in, not read from the environment: environment objects don't
    /// reliably cross the `.sheet` boundary (see `StackDestinations`).
    @ObservedObject var navigation: AppNavigation
    @ObservedObject var liveStore: LiveGameStore
    @Environment(\.dismiss) private var dismiss
    @State private var path = NavigationPath()
    @State private var opening: Int?
    @State private var openError: String?

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let series = slot.series {
                    Section {
                        ForEach(series.games, id: \.gameId) { game in
                            Button { Task { await open(game) } } label: {
                                GameRow(game: game, opening: opening == game.gameId)
                            }
                            .buttonStyle(.plain)
                            .disabled(!game.hasBoxScore)
                        }
                    } footer: {
                        if let openError { Text(openError) }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle(slot.roundName ?? "Series")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
            .stackDestinations(BoxScoreContext(
                path: $path,
                owningTab: .standings,
                navigation: navigation,
                liveStore: liveStore,
            ))
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(.ultraThinMaterial)
    }

    /// Resolve the balldontlie game to the app's `Game` through the same
    /// day-slate fetch the Scores tab uses, then push its box score.
    private func open(_ game: PostseasonSeriesGame) async {
        guard let date = game.easternDate else { return }
        opening = game.gameId
        defer { opening = nil }
        do {
            let slate = try await BallDontLieClient.shared.getGames(date: date)
            if let match = slate.first(where: { $0.id == game.gameId }) {
                openError = nil
                path.append(match.toGame())
            } else {
                openError = "That game couldn't be found."
            }
        } catch {
            openError = "Couldn't open the box score. \(error.localizedDescription)"
        }
    }
}

private struct GameRow: View {
    let game: PostseasonSeriesGame
    let opening: Bool

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(game.isLive ? Color.accentColor : .secondary)
            }
            Spacer(minLength: 4)
            if opening {
                ProgressView()
            } else if game.hasBoxScore {
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        .contentShape(Rectangle())
    }

    private var title: String {
        "Game \(game.gameNumber)" + (game.ifNecessary ? " (if necessary)" : "")
    }

    /// "ATL 3, PHI 1 · Final", "Live · ATL 2, PHI 0", or the start date.
    private var detail: String {
        let score: String? = {
            guard let away = game.away, let home = game.home,
                  let ar = game.awayRuns, let hr = game.homeRuns else { return nil }
            return "\(away) \(ar), \(home) \(hr)"
        }()
        switch game.status {
        case "STATUS_FINAL":
            return [score, "Final"].compactMap { $0 }.joined(separator: " · ")
        case "STATUS_POSTPONED":
            return "Postponed"
        case "STATUS_CANCELED":
            return "Canceled"
        default:
            if game.isLive { return ["Live", score].compactMap { $0 }.joined(separator: " · ") }
            guard let start = game.startDate else { return "Scheduled" }
            return start.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().hour().minute())
        }
    }
}

extension PostseasonSeriesGame {
    /// A game that has started has a box score to open.
    var hasBoxScore: Bool {
        isLive || status == "STATUS_FINAL"
    }

    /// The MLB schedule day (US Eastern) of the game — the key the day-slate
    /// fetch files it under.
    var easternDate: String? {
        guard let start = startDate else { return nil }
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York")
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: start)
    }
}
