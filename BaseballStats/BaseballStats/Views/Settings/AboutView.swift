//
//  AboutView.swift
//  BaseballStats
//
//  Pushed from Settings → About. A grouped Form (matching the Settings style)
//  with the app version (read live from the bundle — never hardcoded), a short
//  description, data-source acknowledgments that open in the browser (and push
//  Sources & Licenses, each source's required notice), and a feedback mailto
//  link.
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
        DataSource(name: "Baseball-Reference",
                   descriptor: "WAR (stored, not updated)",
                   urlString: "https://www.baseball-reference.com"),
        DataSource(name: "Retrosheet",
                   descriptor: "Historical game logs and box scores",
                   urlString: "https://www.retrosheet.org"),
        DataSource(name: "SABR Lahman Baseball Database",
                   descriptor: "Historical statistics (1871–present)",
                   urlString: "https://sabr.org/lahman-database/"),
        DataSource(name: "Chadwick Baseball Bureau Register",
                   descriptor: "Player identifiers",
                   urlString: "https://github.com/chadwickbureau/register"),
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
                Text("BaseballStats brings you comprehensive Major League Baseball statistics, standings, scores, and news — from the 1871 season to today.")
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
                // The notices and licenses each source asks for, in its own words.
                NavigationLink {
                    SourcesView()
                } label: {
                    Text("Sources & Licenses")
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
