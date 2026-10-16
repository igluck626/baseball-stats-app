//
//  TeamPageView.swift
//  BaseballStats
//
//  Any club's page, pushed onto whichever stack the user is in: Standings →
//  Dodgers → a player → a game → the Giants, and Back unwinds it in that tab.
//  The page itself is `TeamPageLayout`, the same view Home shows for the
//  favourite team, so the two never drift apart.
//

import SwiftUI

/// Pushes a team's page. Codable, like every value pushed onto a scene-saved
/// path (NavigationDestinationCodableTests), so a resize keeps it.
struct TeamDestination: Hashable, Codable {
    let bdlTeamId: Int
}

enum TeamLink {
    /// Open a team's page on this stack — or, when it is the team whose page is
    /// already this stack's root (the favourite, on Home), go back to that root
    /// rather than pushing a second copy of it.
    static func open(_ bdlTeamId: Int, path: Binding<NavigationPath>, rootTeamId: Int?) {
        if bdlTeamId == rootTeamId {
            path.wrappedValue = NavigationPath()
        } else {
            path.wrappedValue.append(TeamDestination(bdlTeamId: bdlTeamId))
        }
    }
}

struct TeamPageView: View {
    let bdlTeamId: Int
    let context: BoxScoreContext
    /// This page's own model: Home's is the favourite's.
    @StateObject private var vm = HomeViewModel()

    var body: some View {
        if let entry = MLBTeamCatalog.entry(forBDLId: bdlTeamId) {
            let tint = TeamColors.color(for: entry.lahmanCode) ?? .accentColor
            ZStack {
                TeamPageBackground(tint: tint)
                if vm.isLoading && !vm.didLoad {
                    ProgressView()
                } else {
                    TeamPageLayout(entry: entry, vm: vm, path: context.path,
                                   navigation: context.navigation,
                                   liveStore: context.liveStore) { EmptyView() }
                }
            }
            // The title behind the custom one below: what Back says on the next
            // screen, and what VoiceOver and UI tests find.
            .navigationTitle(entry.fullName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { TeamPageTitle(entry: entry) }
            }
            // Coming back to this page from a game doesn't reload it all; the
            // periodic refresh keeps it current.
            .teamPageLifecycle(bdlTeamId: bdlTeamId, vm: vm, owningTab: context.owningTab,
                               navigation: context.navigation, liveStore: context.liveStore,
                               reloadsOnReturn: false)
            // The hero card reads these from the environment, which a sheet's
            // stack doesn't reliably carry (see StackDestinations), so this page
            // supplies them wherever it's pushed.
            .environmentObject(context.navigation)
            .environmentObject(context.liveStore)
        } else {
            ContentUnavailableView("Team not found", systemImage: "questionmark.circle")
        }
    }
}
