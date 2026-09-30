//
//  RecentPostseasonGamesSection.swift
//  BaseballStats
//
//  Overview section shown in Recent Games' place while the league's
//  postseason is in progress and the player has appeared in it
//  (`recentGamesMode` == .postseason): his latest games of this postseason,
//  newest first — Recent Games' card and table style, one row per game, the
//  round and game ("WC G1") where the window's G column sits.
//

import Combine
import SwiftUI

@MainActor
final class RecentPostseasonGamesViewModel: ObservableObject {
    let playerId: Int
    let season: Int
    @Published var logs: PostseasonGameLogs?
    @Published var error: String?
    private let api: APIClient

    init(playerId: Int, season: Int, api: APIClient = .shared) {
        self.playerId = playerId
        self.season = season
        self.api = api
    }

    func load() async {
        do {
            logs = try await api.getPlayerPostseasonGameLogs(playerId: playerId, season: season)
            error = nil
        } catch {
            if logs == nil { self.error = error.localizedDescription }
        }
    }
}

/// His latest postseason games, newest first, at most `limit`. Pure.
func recentPostseasonLines<L: PostseasonGameLine>(_ lines: [L]?, limit: Int = 5) -> [L] {
    Array((lines ?? []).reversed().prefix(limit))       // the endpoint sends oldest first
}

struct RecentPostseasonGamesSection: View {
    let playerId: Int
    let isPitcher: Bool
    let season: Int
    @StateObject private var vm: RecentPostseasonGamesViewModel

    private static let gameWidth: CGFloat = 56

    init(playerId: Int, isPitcher: Bool, season: Int) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.season = season
        _vm = StateObject(wrappedValue: RecentPostseasonGamesViewModel(playerId: playerId, season: season))
    }

    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Text("Recent Postseason Games").font(.headline)
                Spacer()
            }
            content
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 18))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .task { await vm.load() }
    }

    @ViewBuilder
    private var content: some View {
        if let logs = vm.logs {
            if isPitcher, !recentPostseasonLines(logs.pitching).isEmpty {
                table { pitchingRows(recentPostseasonLines(logs.pitching)) }
            } else if !isPitcher, !recentPostseasonLines(logs.batting).isEmpty {
                table { battingRows(recentPostseasonLines(logs.batting)) }
            } else {
                placeholder("No postseason games yet")
            }
        } else if let error = vm.error {
            placeholder(error)
        } else {
            ProgressView().frame(maxWidth: .infinity, minHeight: SplitsLayout.rowHeight * 2)
        }
    }

    private func table<Rows: View>(@ViewBuilder _ rows: () -> Rows) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(spacing: 0) {
                header
                Divider()
                rows()
            }
        }
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private var header: some View {
        HStack(spacing: 0) {
            Text("Game").frame(width: Self.gameWidth, alignment: .leading).padding(.horizontal, 2)
            if isPitcher {
                ForEach([("IP", SplitsLayout.ip), ("H", SplitsLayout.h), ("R", SplitsLayout.r), ("ER", SplitsLayout.er),
                         ("BB", SplitsLayout.bb), ("SO", SplitsLayout.so), ("HR", SplitsLayout.hr),
                         ("ERA", SplitsLayout.era)], id: \.0) { cell($0.0, width: $0.1) }
            } else {
                ForEach([("AB", SplitsLayout.ab), ("H", SplitsLayout.h), ("HR", SplitsLayout.hr), ("RBI", SplitsLayout.rbi),
                         ("BB", SplitsLayout.bb), ("SO", SplitsLayout.so), ("SB", SplitsLayout.sb),
                         ("AVG", SplitsLayout.rate), ("OBP", SplitsLayout.rate), ("SLG", SplitsLayout.rate),
                         ("OPS", SplitsLayout.rate)], id: \.0) { cell($0.0, width: $0.1) }
            }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .frame(height: SplitsLayout.rowHeight)
    }

    private func battingRows(_ lines: [PostseasonBattingGameLine]) -> some View {
        ForEach(lines) { l in
            let s = WindowSnapshot.computeBatting(games: [l.asGameLog])
            HStack(spacing: 0) {
                gameCell(l.seriesGameLabel)
                cell(String(l.AB), width: SplitsLayout.ab)
                cell(String(l.H), width: SplitsLayout.h)
                cell(String(l.HR), width: SplitsLayout.hr)
                cell(String(l.RBI), width: SplitsLayout.rbi)
                cell(String(l.BB), width: SplitsLayout.bb)
                cell(String(l.SO), width: SplitsLayout.so)
                cell(String(l.SB), width: SplitsLayout.sb)
                cell(rate3(s.avg), width: SplitsLayout.rate)
                cell(rate3(s.obp), width: SplitsLayout.rate)
                cell(rate3(s.slg), width: SplitsLayout.rate)
                cell(rate3(s.ops), width: SplitsLayout.rate)
            }
            .font(.caption)
            .frame(height: SplitsLayout.rowHeight)
        }
    }

    private func pitchingRows(_ lines: [PostseasonPitchingGameLine]) -> some View {
        ForEach(lines) { l in
            HStack(spacing: 0) {
                gameCell(l.seriesGameLabel)
                cell(l.IP, width: SplitsLayout.ip)
                cell(String(l.H), width: SplitsLayout.h)
                cell(String(l.R), width: SplitsLayout.r)
                cell(String(l.ER), width: SplitsLayout.er)
                cell(String(l.BB), width: SplitsLayout.bb)
                cell(String(l.SO), width: SplitsLayout.so)
                cell(String(l.HR), width: SplitsLayout.hr)
                cell(l.outs > 0 ? String(format: "%.2f", Double(l.ER) * 27 / Double(l.outs)) : "—", width: SplitsLayout.era)
            }
            .font(.caption)
            .frame(height: SplitsLayout.rowHeight)
        }
    }

    private func gameCell(_ label: String) -> some View {
        Text(label)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .frame(width: Self.gameWidth, alignment: .leading)
            .padding(.horizontal, 2)
    }

    private func cell(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
            .padding(.horizontal, 2)
    }

    private func placeholder(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: SplitsLayout.rowHeight * 2)
            .multilineTextAlignment(.center)
    }

    private func rate3(_ v: Double?) -> String {
        guard let v else { return "—" }
        let s = String(format: "%.3f", v)
        return s.hasPrefix("0.") ? String(s.dropFirst()) : s
    }
}
