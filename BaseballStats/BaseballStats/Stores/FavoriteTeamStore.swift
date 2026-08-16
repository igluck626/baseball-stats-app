//
//  FavoriteTeamStore.swift
//  BaseballStats
//
//  UserDefaults-backed favorite-team selection for the Home tab.
//  Single shared instance so any view in the tree can observe
//  changes without prop-drilling. The catalog of 30 MLB teams
//  lives here too — same file because it's all "which team does
//  the user follow" data.
//

import Combine
import Foundation

@MainActor
final class FavoriteTeamStore: ObservableObject {
    static let shared = FavoriteTeamStore()

    /// BDL team id (1...30) of the user's favorite team. nil before
    /// first selection — the Home tab uses this signal to show the
    /// team picker instead of the hero card.
    @Published var bdlTeamId: Int?

    private let key = "favoriteTeamBDLId"

    init() {
        let raw = UserDefaults.standard.integer(forKey: key)
        // `integer(forKey:)` returns 0 for "key not set", which is
        // also a meaningful int. BDL ids start at 1 so 0 unambiguously
        // means "no favorite yet".
        self.bdlTeamId = raw > 0 ? raw : nil
    }

    func setFavorite(bdlTeamId: Int?) {
        self.bdlTeamId = bdlTeamId
        if let bdlTeamId {
            UserDefaults.standard.set(bdlTeamId, forKey: key)
        } else {
            UserDefaults.standard.removeObject(forKey: key)
        }
    }
}

// MARK: - MLB Team Catalog

/// Static catalog of all 30 MLB teams, derived from
/// `lahmanToBDLTeamId` + `teamFullName(for:)` + `teamAbbreviation(for:)`
/// + `lahmanTeamIdToMLBId(_:)`. Single source for the team picker
/// grid and any code that needs to bridge BDL ids to TeamInfo /
/// MLBAM ids without going through a live API call.
struct MLBTeamCatalog {
    struct Entry: Hashable, Identifiable {
        let bdlTeamId:    Int
        let lahmanCode:   String
        let mlbamId:      Int
        let fullName:     String
        let abbreviation: String
        var id: Int { bdlTeamId }
        /// `TeamInfo` shape — uses the MLBAM id so `TeamLogoView`
        /// can hit the existing MLBAM logo CDN.
        var teamInfo: TeamInfo {
            TeamInfo(id: mlbamId, name: fullName, abbreviation: abbreviation)
        }
    }

    /// All 30 entries, sorted alphabetically by full team name so
    /// the picker grid reads in a predictable order.
    static let all: [Entry] = {
        var out: [Entry] = []
        for (lahman, bdlId) in lahmanToBDLTeamId {
            guard let mlbam = lahmanTeamIdToMLBId(lahman),
                  let full  = teamFullName(for: lahman) else { continue }
            out.append(Entry(
                bdlTeamId:    bdlId,
                lahmanCode:   lahman,
                mlbamId:      mlbam,
                fullName:     full,
                abbreviation: teamAbbreviation(for: lahman),
            ))
        }
        return out.sorted { $0.fullName < $1.fullName }
    }()

    static func entry(forBDLId bdlId: Int) -> Entry? {
        all.first { $0.bdlTeamId == bdlId }
    }

    /// Lookup by MLBAM id — the id `TeamInfo` carries. Needed because the
    /// score columns and the box-score section header hold a `TeamInfo` and
    /// nothing else, but `TeamColors` is keyed on the Lahman code.
    static func entry(forMLBAMId mlbamId: Int) -> Entry? {
        all.first { $0.mlbamId == mlbamId }
    }

    /// Lahman code for an MLBAM id, or nil when the club isn't one of the 30
    /// (historical opponents in old box scores, mostly).
    static func lahmanCode(forMLBAMId mlbamId: Int) -> String? {
        entry(forMLBAMId: mlbamId)?.lahmanCode
    }
}
