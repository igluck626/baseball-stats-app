//
//  ContentView.swift
//  BaseballStats
//
//  Created by Isaac Gluck on 5/4/26.
//

import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            SearchView()
                .tabItem {
                    Label("Search", systemImage: "magnifyingglass")
                }

            StandingsView()
                .tabItem {
                    Label("Standings", systemImage: "list.bullet")
                }
        }
        .toolbarBackground(.ultraThinMaterial, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
    }
}

#Preview {
    ContentView()
}
