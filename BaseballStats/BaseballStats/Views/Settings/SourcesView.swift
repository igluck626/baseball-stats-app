//
//  SourcesView.swift
//  BaseballStats
//
//  Pushed from Settings → About → Sources & Licenses. Credits each data source
//  in the words its own terms ask for, checked on each source's site
//  (2026-10-21):
//
//    • Retrosheet — notice.txt: the notice must appear "prominently"; its text
//      is quoted verbatim below. Any use, commercial included.
//    • SABR Lahman Baseball Database — the 2025 release's readme, §0.1:
//      CC BY-SA 3.0 Unported. Credit, a link to the license, and a note that
//      the data was changed (we combine and correct it). Its Negro Leagues
//      statistics are licensed by SABR from Seamheads.com.
//    • Chadwick Baseball Bureau Register — ODC Attribution License 1.0; §4.3
//      asks a Produced Work to carry "Contains information from DATABASE NAME
//      which is made available under the ODC Attribution License.", with both
//      names linked.
//    • balldontlie — Terms §6: no attribution required (credited anyway);
//      §7: never state or imply the data is official league data.
//    • Baseball-Reference — WAR and the WAR-derived columns. Stored values,
//      no longer fetched or updated (backend: test_no_baseball_reference);
//      credited as Isaac worded it (2026-10-21). Its terms are on the
//      pre-release review queue.
//
//  The quoted wording is the sources' own: change it only from their sites.
//

import SwiftUI

struct SourcesView: View {
    var body: some View {
        Form {
            Section {
                Text("BaseballStats is an independent app. It is not affiliated with or endorsed by Major League Baseball or any club, and its statistics are not official league data.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            ForEach(Credit.all) { credit in
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(credit.use)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Text(credit.notice)
                            .font(.subheadline)
                            .textSelection(.enabled)
                    }
                    .padding(.vertical, 2)
                    ForEach(credit.links) { link in
                        if let url = link.url {
                            Link(destination: url) {
                                // Not LabeledContent: at accessibility sizes it
                                // stacks, and the arrow drops under a long title.
                                HStack(alignment: .firstTextBaseline) {
                                    Text(link.title)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    Image(systemName: "arrow.up.right")
                                        .font(.footnote)
                                        .accessibilityHidden(true)
                                }
                            }
                            .accessibilityHint("Opens \(link.host) in your browser.")
                        }
                    }
                } header: {
                    Text(credit.name)
                }
            }
        }
        .navigationTitle("Sources & Licenses")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// One source's credit: what it supplies, the notice in its own words, links.
struct Credit: Identifiable {
    let name: String
    let use: String
    let notice: String
    let links: [CreditLink]
    var id: String { name }

    static let all: [Credit] = [
        Credit(
            name: "Retrosheet",
            use: "Historical game logs, box scores and per-club season lines.",
            notice: "The information used here was obtained free of charge from and is copyrighted by Retrosheet. Interested parties may contact Retrosheet at \"www.retrosheet.org\".",
            links: [CreditLink("Retrosheet", "https://www.retrosheet.org")]),
        Credit(
            name: "SABR Lahman Baseball Database",
            use: "Season and career statistics, awards and biographical data, 1871 to the most recent completed season.",
            notice: "Contains data from the SABR Lahman Baseball Database, copyright 1996–2025 by SABR, via generous donation from Sean Lahman, licensed under the Creative Commons Attribution-ShareAlike 3.0 Unported License. BaseballStats combines it with the other sources here and corrects some records. Its Negro Leagues statistics come from Seamheads.com.",
            links: [CreditLink("SABR Lahman Baseball Database", "https://sabr.org/lahman-database/"),
                    CreditLink("CC BY-SA 3.0 license", "https://creativecommons.org/licenses/by-sa/3.0/"),
                    CreditLink("Seamheads Negro Leagues Database", "https://www.seamheads.com/NegroLgs/index.php")]),
        Credit(
            name: "Chadwick Baseball Bureau Register",
            use: "Player identifiers that connect the other sources.",
            notice: "Contains information from the Chadwick Baseball Bureau Register, which is made available under the ODC Attribution License.",
            links: [CreditLink("Chadwick Baseball Bureau Register", "https://github.com/chadwickbureau/register"),
                    CreditLink("ODC Attribution License", "https://opendatacommons.org/licenses/by/1-0/")]),
        Credit(
            name: "balldontlie",
            use: "Live scores, play-by-play, and the current season's games and statistics.",
            notice: "Live and current-season data is provided by balldontlie.",
            links: [CreditLink("balldontlie", "https://www.balldontlie.io")]),
        Credit(
            name: "Baseball-Reference",
            use: "WAR and the statistics built on it (WAR components, OPS+, ERA+).",
            notice: "WAR: Baseball-Reference (stored, not updated).",
            links: [CreditLink("Baseball-Reference", "https://www.baseball-reference.com")]),
    ]
}

/// "Data: Retrosheet" under a screen built from Retrosheet's data (historical
/// box scores and game logs), opening Sources & Licenses. Retrosheet asks for
/// its notice to appear prominently; this puts it one tap from the data.
struct RetrosheetCredit: View {
    var body: some View {
        NavigationLink {
            SourcesView()
        } label: {
            HStack(spacing: 4) {
                Text("Data: Retrosheet")
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .accessibilityHidden(true)
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows the sources and licenses.")
        .accessibilityIdentifier("retrosheetCredit")
    }
}

struct CreditLink: Identifiable {
    let title: String
    let urlString: String
    init(_ title: String, _ urlString: String) { self.title = title; self.urlString = urlString }
    var id: String { urlString }
    var url: URL? { URL(string: urlString) }
    var host: String { url?.host() ?? title }
}

#Preview {
    NavigationStack {
        SourcesView()
    }
}
