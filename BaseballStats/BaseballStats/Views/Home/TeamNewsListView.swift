//
//  TeamNewsListView.swift
//  BaseballStats
//
//  The full "See all" news screen for the Home news section. Pushed from that
//  section's header in either scope:
//    • .team   — the favorite team's full feed ("{Team} News").
//    • .league — the league-wide feed, deduped, with a per-article team badge
//                ("League News").
//  Tapping a row opens the article in the same in-app SafariView reader the
//  Home carousel uses (which honors the autoReaderMode setting itself).
//

import SwiftUI

/// Which news feed a surface is showing. Raw String so it persists cleanly via
/// @AppStorage; Hashable so it can ride inside a navigation destination.
enum NewsScope: String, Hashable {
    case team
    case league
}

/// Value-based navigation destination for the news list. Pushed onto Home's
/// `navigationPath`. For `.team` it carries the team's Lahman code + display
/// name; for `.league` those are nil (the title is fixed and the tint/badge
/// are derived per-article).
struct TeamNewsDestination: Hashable {
    let scope: NewsScope
    let lahmanCode: String?
    let teamName: String?
}

/// Small team chip — a team-colored capsule with the app's CONVENTIONAL
/// abbreviation. Both the abbreviation and the color route through the app's
/// existing team metadata (`teamAbbreviation(for:)` + `TeamColors.color(for:)`,
/// the same source every other surface uses), so the badge matches the codes
/// shown in standings, box scores, etc. — never the raw Lahman code.
struct NewsTeamBadge: View {
    /// The article's `teamCode` (a Lahman storage code, e.g. "LAN").
    let teamCode: String

    var body: some View {
        Text(teamAbbreviation(for: teamCode))   // "LAN" → "LAD", "SLN" → "STL"
            .font(.caption2.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(TeamColors.color(for: teamCode) ?? .accentColor, in: Capsule())
    }
}

struct TeamNewsListView: View {
    let scope: NewsScope
    let lahmanCode: String?
    let teamName: String?
    /// Favorite-team tint, used for image fallbacks in `.team` scope. In
    /// `.league` scope each row derives its own tint from the article's team.
    let tint: Color

    @State private var articles: [NewsArticle] = []
    @State private var isLoading = true
    @State private var selectedArticle: NewsArticle?

    private var navTitle: String {
        switch scope {
        case .league: return "League News"
        case .team:   return teamName.map { "\($0) News" } ?? "Team News"
        }
    }

    private var showsTeamBadge: Bool { scope == .league }

    var body: some View {
        content
            .navigationTitle(navTitle)
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            // Reuse the carousel's in-app reader. SafariView reads
            // autoReaderMode itself; appearanceOverride keeps the sheet in the
            // user's chosen light/dark mode.
            .sheet(item: $selectedArticle) { article in
                if let url = URL(string: article.url) {
                    SafariView(url: url)
                        .ignoresSafeArea()
                        .appearanceOverride()
                }
            }
    }

    @ViewBuilder
    private var content: some View {
        if isLoading {
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if articles.isEmpty {
            // Reached intentionally, so a brief message is fine here (unlike
            // the Home carousel, which hides itself when empty).
            VStack(spacing: 8) {
                Image(systemName: "newspaper")
                    .font(.largeTitle)
                    .foregroundStyle(.secondary)
                Text("No news available")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List(articles) { article in
                Button { selectedArticle = article } label: {
                    TeamNewsRow(article: article, tint: tint, showsTeamBadge: showsTeamBadge)
                }
                .buttonStyle(.plain)
                .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
            }
            .listStyle(.plain)
        }
    }

    private func load() async {
        isLoading = true
        switch scope {
        case .team:
            // limit=25 to show the full set the backend holds per team.
            articles = (try? await APIClient.shared.getNews(team: lahmanCode, limit: 25)) ?? []
        case .league:
            // Team omitted → league-wide. The feed is heavily syndicated (one
            // wire story stored per team), so pull the endpoint max (50), dedup
            // the cross-team copies, then keep the newest 25 distinct stories.
            let raw = (try? await APIClient.shared.getNews(team: nil, limit: 50)) ?? []
            articles = Array(NewsArticle.deduplicated(raw).prefix(25))
        }
        isLoading = false
    }
}

/// One compact news row — leading 16:9 thumbnail, headline (2 lines), and a
/// "{source} · {relative time}" caption. In league scope a team badge sits at
/// the thumbnail's bottom-right. Mirrors the carousel NewsCard's image
/// fallbacks (gray while loading; team-tinted newspaper block on missing/failed
/// image — never a broken-image icon).
private struct TeamNewsRow: View {
    let article: NewsArticle
    let tint: Color
    let showsTeamBadge: Bool

    private static let thumbWidth: CGFloat = 96
    private static let thumbHeight: CGFloat = thumbWidth * 9 / 16   // 16:9

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated   // "2h ago", "1d ago"
        return f
    }()

    /// In league scope, the row tint comes from the article's own team; in
    /// team scope it's the favorite-team tint passed down.
    private var effectiveTint: Color {
        showsTeamBadge ? (TeamColors.color(for: article.teamCode) ?? .accentColor) : tint
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            image
                .frame(width: Self.thumbWidth, height: Self.thumbHeight)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(alignment: .bottomTrailing) {
                    if showsTeamBadge {
                        NewsTeamBadge(teamCode: article.teamCode)
                            .padding(4)
                    }
                }

            VStack(alignment: .leading, spacing: 4) {
                Text(article.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text("\(article.sourceName) · \(Self.relativeFormatter.localizedString(for: article.publishedAt, relativeTo: Date()))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
    }

    // MARK: Image + fallbacks (same graceful behavior as the carousel card)

    @ViewBuilder
    private var image: some View {
        if let urlStr = article.imageUrl, let url = URL(string: urlStr) {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFill()
                case .empty:
                    placeholder                 // loading: neutral gray
                case .failure:
                    fallback                    // broken/unreachable: team block
                @unknown default:
                    fallback
                }
            }
        } else {
            fallback                            // no image url
        }
    }

    /// Neutral loading fill.
    private var placeholder: some View {
        Rectangle().fill(Color(.secondarySystemFill))
    }

    /// Graceful no-image block — faint team-tint wash with a newspaper glyph.
    private var fallback: some View {
        ZStack {
            Rectangle().fill(effectiveTint.opacity(0.18))
            Image(systemName: "newspaper.fill")
                .font(.title3)
                .foregroundStyle(effectiveTint.opacity(0.55))
        }
    }
}
