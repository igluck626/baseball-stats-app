//
//  TabOrderView.swift
//  BaseballStats
//
//  Pushed from Settings → Tab Order. Drag-to-reorder the five bottom tabs.
//  The first tab is also the app's launch tab. Reordering writes straight back
//  to TabOrderStore (persisted immediately) and the tab bar rebuilds.
//
//  Reorder only — no add/hide/delete. There is no `.onDelete`, and the store
//  reconciles every write, so all five tabs always remain.
//

import SwiftUI

struct TabOrderView: View {
    @ObservedObject private var tabOrder = TabOrderStore.shared
    @State private var showingResetConfirmation = false

    /// Hide the reset control when there's nothing to reset.
    private var isDefaultOrder: Bool {
        tabOrder.order == AppNavigation.Tab.defaultOrder
    }

    var body: some View {
        List {
            Section {
                ForEach(tabOrder.order) { tab in
                    HStack(spacing: 12) {
                        Image(systemName: tab.icon)
                            .font(.body)
                            .foregroundStyle(Color(.secondaryLabel))
                            .frame(width: 28, alignment: .center)
                        Text(tab.title)
                        Spacer()
                        if tab == tabOrder.order.first {
                            Text("Opens here")
                                .font(.caption)
                                .foregroundStyle(Color(.secondaryLabel))
                        }
                    }
                }
                .onMove { source, destination in
                    tabOrder.move(fromOffsets: source, toOffset: destination)
                }
            } footer: {
                Text("Drag to reorder. The top tab is where the app opens.")
            }

            // Only present when the order has actually been changed, so the row
            // isn't dead weight on the default arrangement.
            if !isDefaultOrder {
                Section {
                    Button("Reset to Default Order") {
                        showingResetConfirmation = true
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .confirmationDialog(
            "Reset tab order to default?",
            isPresented: $showingResetConfirmation,
            titleVisibility: .visible
        ) {
            Button("Reset to Default") {
                // Same path as a drag-reorder: persists and rebuilds the bar.
                tabOrder.setOrder(AppNavigation.Tab.defaultOrder)
            }
            Button("Cancel", role: .cancel) { }
        }
        // Always-on edit mode so the drag handles show without an Edit button.
        // Only .onMove is wired (no .onDelete), so rows show the reorder grip
        // and nothing can be removed.
        .environment(\.editMode, .constant(.active))
        .navigationTitle("Tab Order")
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    NavigationStack {
        TabOrderView()
    }
}
