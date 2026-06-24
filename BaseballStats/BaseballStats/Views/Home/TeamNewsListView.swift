//
//  TeamNewsListView.swift
//  BaseballStats
//
//  The full "See all" news screen for the Home Team News section. Pushed from
//  that section's header; shows the team's complete news feed (newest first) as
//  compact thumbnail rows. Tapping a row opens the article in the same in-app
//  SafariView reader the Home carousel uses (which honors the autoReaderMode
//  setting itself).
//

import SwiftUI

/// Value-based navigation destination for the news list. Pushed onto Home's
/// `navigationPath`; carries the exact same Lahman code the carousel uses plus
/// the team's display name for the title.
struct TeamNewsDestination: Hashable {
    let lahmanCode: String
    let teamName: String?
}

struct TeamNewsListView: View {
    let lahmanCode: String
    let teamName: String?
    let tint: Color

    @State private var articles: [NewsArticle] = []
    @State private var isLoading = true
    @State private var selectedArticle: NewsArticle?

    private var navTitle: String {
        if let teamName { return "\(teamName) News" }
        return "Team News"
    }

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
                    TeamNewsRow(article: article, tint: tint)
                }
                .buttonStyle(.plain)
                .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
            }
            .listStyle(.plain)
        }
    }

    private func load() async {
        isLoading = true
        // limit=25 to show the full set the backend holds per team.
        let result = (try? await APIClient.shared.getNews(team: lahmanCode, limit: 25)) ?? []
        articles = result
        isLoading = false
    }
}

/// One compact news row — leading 16:9 thumbnail, headline (2 lines), and a
/// "{source} · {relative time}" caption. Mirrors the carousel NewsCard's image
/// fallbacks (gray while loading; team-tinted newspaper block on missing/failed
/// image — never a broken-image icon).
private struct TeamNewsRow: View {
    let article: NewsArticle
    let tint: Color

    private static let thumbWidth: CGFloat = 96
    private static let thumbHeight: CGFloat = thumbWidth * 9 / 16   // 16:9

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated   // "2h ago", "1d ago"
        return f
    }()

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            image
                .frame(width: Self.thumbWidth, height: Self.thumbHeight)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

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
            Rectangle().fill(tint.opacity(0.18))
            Image(systemName: "newspaper.fill")
                .font(.title3)
                .foregroundStyle(tint.opacity(0.55))
        }
    }
}
