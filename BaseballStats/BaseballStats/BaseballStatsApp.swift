//
//  BaseballStatsApp.swift
//  BaseballStats
//
//  Created by Isaac Gluck on 5/4/26.
//

import SwiftUI

@main
struct BaseballStatsApp: App {
    var body: some Scene {
        WindowGroup {
            #if DEBUG
            ContentView()
                .sizeClassFlipHarness()   // a UI-test hook; see SizeClassFlipHarness
            #else
            ContentView()
            #endif
        }
    }
}
