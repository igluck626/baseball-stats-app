//
//  TabOrderStore.swift
//  BaseballStats
//
//  Single source of truth for the bottom tab bar's order. The FIRST element is
//  also the app's launch tab (AppNavigation seeds selectedTab from it), so this
//  one control drives both reorder and default-landing-tab behavior.
//
//  Persisted under "tabOrder" as the tabs' stable Int raw values (e.g.
//  [4,0,2,3,1]). Decoding is always reconciled against the full set of cases,
//  so a missing/stale/duplicate saved order can never drop a tab or crash.
//

import Combine
import SwiftUI

@MainActor
final class TabOrderStore: ObservableObject {
    static let shared = TabOrderStore()

    private let key = "tabOrder"

    /// The bottom tab bar's order. `order.first` is the launch tab.
    @Published private(set) var order: [AppNavigation.Tab]

    private init() {
        let raw = UserDefaults.standard.array(forKey: key) as? [Int]
        order = AppNavigation.Tab.reconciledOrder(fromRawValues: raw)
    }

    /// Persist a new order. Reconciled first so the stored value is always a
    /// complete, valid list of all five tabs.
    func setOrder(_ newOrder: [AppNavigation.Tab]) {
        let reconciled = AppNavigation.Tab.reconciledOrder(from: newOrder)
        order = reconciled
        UserDefaults.standard.set(reconciled.map(\.rawValue), forKey: key)
    }

    /// Drag-to-reorder hook for the Settings list's `.onMove`.
    func move(fromOffsets source: IndexSet, toOffset destination: Int) {
        var newOrder = order
        newOrder.move(fromOffsets: source, toOffset: destination)
        setOrder(newOrder)
    }
}
