//
//  AboutView.swift
//  BaseballStats
//
//  Pushed from Settings → About. A grouped Form (matching the Settings style)
//  with the app version (read live from the bundle — never hardcoded), a short
//  description, data-source acknowledgments that open in the browser, and a
//  feedback mailto link.
//
//  NOTE: `feedbackEmail` below is currently a personal address used as a
//  placeholder — swap it for a dedicated feedback/support address before
//  App Store release.
//

import SwiftUI

struct AboutView: View {
    // NOTE: this is a personal address used as a placeholder. Swap it for a
    // dedicated feedback/support address before App Store release.
    // The "Send Feedback" row builds its mailto: link from this constant.
    private let feedbackEmail = "igluck626@gmail.com"

    @Environment(\.openURL) private var openURL

    /// "Version {CFBundleShortVersionString} ({CFBundleVersion})", read live
    /// from the built bundle so it always matches the actual build.
    private var versionString: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "—"
        let build = info?["CFBundleVersion"] as? String ?? "—"
        return "Version \(short) (\(build))"
    }

    /// mailto: link with a prefilled subject. Built via URLComponents so the
    /// subject is percent-encoded correctly. nil only if the address is empty.
    private var feedbackURL: URL? {
        var components = URLComponents()
        components.scheme = "mailto"
        components.path = feedbackEmail
        components.queryItems = [URLQueryItem(name: "subject", value: "BaseballStats Feedback")]
        return components.url
    }

    private let dataSources: [DataSource] = [
        DataSource(name: "Baseball Reference",
                   descriptor: "Statistics and WAR benchmarking",
                   urlString: "https://www.baseball-reference.com"),
        DataSource(name: "Lahman Baseball Database",
                   descriptor: "Historical statistics (1871–present)",
                   urlString: "https://www.seanlahman.com"),
        DataSource(name: "balldontlie",
                   descriptor: "Live and recent game data",
                   urlString: "https://www.balldontlie.io"),
        DataSource(name: "MLB.com",
                   descriptor: "Team news",
                   urlString: "https://www.mlb.com"),
    ]

    var body: some View {
        Form {
            // App name + live version, floated like a header.
            Section {
                VStack(spacing: 6) {
                    Text("BaseballStats")
                        .font(.title2.bold())
                    Text(versionString)
                        .font(.subheadline)
                        .foregroundStyle(Color(.secondaryLabel))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
                .listRowBackground(Color.clear)
            }

            Section {
                Text("BaseballStats brings you comprehensive Major League Baseball statistics, standings, scores, and news — from the 1871 season to today. Player and team stats are benchmarked against Baseball Reference for accuracy.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Section {
                ForEach(dataSources) { source in
                    if let url = source.url {
                        Link(destination: url) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(source.name)
                                    .foregroundStyle(.primary)
                                Text(source.descriptor)
                                    .font(.caption)
                                    .foregroundStyle(Color(.secondaryLabel))
                            }
                        }
                    }
                }
            } header: {
                Text("Data Sources")
            }

            Section {
                Button {
                    // openURL fails gracefully: if no mail client can handle the
                    // mailto: link, the completion reports false and nothing
                    // happens (no crash).
                    if let feedbackURL {
                        openURL(feedbackURL) { _ in }
                    }
                } label: {
                    Text("Send Feedback")
                }
                .disabled(feedbackURL == nil)
            } header: {
                Text("Feedback")
            }
        }
        .navigationTitle("About")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// One acknowledged data source. `id` is the name (unique), so no UUID needed.
private struct DataSource: Identifiable {
    let name: String
    let descriptor: String
    let urlString: String

    var id: String { name }
    var url: URL? { URL(string: urlString) }
}

#Preview {
    NavigationStack {
        AboutView()
    }
}
