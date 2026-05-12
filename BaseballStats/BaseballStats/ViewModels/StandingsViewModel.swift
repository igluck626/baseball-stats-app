//
//  StandingsViewModel.swift
//  BaseballStats
//
//  Drives the Standings tab. One fetch per year selection, then partition
//  the 30 rows into AL/NL × E/C/W buckets sorted by win_pct.
//

import Combine
import Foundation

@MainActor
final class StandingsViewModel: ObservableObject {
    /// AL standings keyed by single-letter division code ("E", "C", "W").
    /// Each bucket is sorted by win_pct desc with rank as a tiebreaker.
    @Published var alStandings: [String: [TeamStanding]] = [:]
    @Published var nlStandings: [String: [TeamStanding]] = [:]
    /// Wildcard race per league — all non-division-leader teams sorted
    /// by win_pct desc. The Standings view highlights the top 3 as
    /// "in" via the rank position; everything below is "out / chasing."
    @Published var alWildcard: [TeamStanding] = []
    @Published var nlWildcard: [TeamStanding] = []
    @Published var selectedYear: Int
    @Published var isLoading = false
    @Published var error: String?
    /// ISO-8601 timestamp from the response — surfaced as "Updated …"
    /// at the bottom of the view.
    @Published var lastUpdated: String?

    private let api: APIClient

    static var currentYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    init(api: APIClient = .shared) {
        self.api = api
        self.selectedYear = Self.currentYear
    }

    func loadStandings() async {
        isLoading = true
        error = nil
        do {
            let response = try await api.getStandings(year: selectedYear)
            partition(response)
            lastUpdated = response?.last_updated
        } catch {
            self.error = error.localizedDescription
            alStandings = [:]
            nlStandings = [:]
            lastUpdated = nil
        }
        isLoading = false
    }

    /// Split the flat array into AL/NL × division buckets and sort each
    /// bucket by win_pct desc. Teams with unknown league/division (very
    /// old Lahman pre-divisional years) get dropped from the buckets.
    /// Also builds the wildcard list per league (non-division-leaders
    /// ranked by win_pct desc).
    private func partition(_ response: StandingsResponse?) {
        let teams = response?.standings ?? []
        var al: [String: [TeamStanding]] = [:]
        var nl: [String: [TeamStanding]] = [:]
        var alWC: [TeamStanding] = []
        var nlWC: [TeamStanding] = []
        for team in teams {
            guard let div = team.division else { continue }
            switch team.league {
            case "AL":
                al[div, default: []].append(team)
                if team.division_leader != true { alWC.append(team) }
            case "NL":
                nl[div, default: []].append(team)
                if team.division_leader != true { nlWC.append(team) }
            default:
                continue
            }
        }
        for key in al.keys { al[key]?.sort(by: Self.standingsSort) }
        for key in nl.keys { nl[key]?.sort(by: Self.standingsSort) }
        alStandings = al
        nlStandings = nl
        // If the backend hasn't populated division_leader yet (historical
        // years), both wildcard arrays come back empty — the view falls
        // back to hiding the tab in that case.
        alWildcard = alWC.sorted(by: Self.standingsSort)
        nlWildcard = nlWC.sorted(by: Self.standingsSort)
    }

    /// Best record first. win_pct is the primary key; if two teams are
    /// tied (rare exact decimal collision), fall back to the backend's
    /// `rank` field, then to wins.
    private static func standingsSort(_ a: TeamStanding, _ b: TeamStanding) -> Bool {
        let aPct = a.win_pct ?? 0
        let bPct = b.win_pct ?? 0
        if aPct != bPct { return aPct > bPct }
        let aRank = a.rank ?? Int.max
        let bRank = b.rank ?? Int.max
        if aRank != bRank { return aRank < bRank }
        return (a.W ?? 0) > (b.W ?? 0)
    }
}
