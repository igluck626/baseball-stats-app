//
//  AskView.swift
//  BaseballStats
//
//  The Ask screen, presented full-screen from the floating button on the
//  tab root. Ask a plain-English question about any player's stats or
//  situational numbers and get one phrased answer back.
//
//  The transcript is a HISTORY, not a chat: each question is answered
//  independently (no memory), so every Q&A is its own CARD — the question
//  is a muted header at the top of the card, the answer is the body. No
//  bubbles, no alignment tricks, no avatars. It reads as "here's what you
//  asked, here's what I found," top to bottom.
//

import SwiftUI

struct AskView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var viewModel = AskViewModel()
    @State private var path = NavigationPath()
    @FocusState private var inputFocused: Bool
    @Environment(\.dynamicTypeSize) private var typeSize

    /// Tappable starters, grouped — shown ONLY in the empty state. Once the
    /// user has asked anything, the transcript takes over and these disappear.
    ///
    /// ⚠️ THE CATEGORIES ARE DERIVED FROM THE TOOLS, not invented to look
    /// complete. Each maps to one or more of the sixteen the backend exposes:
    /// a player's numbers (situational / rates / splits), leaders (leaderboard
    /// / rate leaderboard), comparisons, streaks and spans (four tools),
    /// milestones and single-game feats, awards, and teams (team /
    /// head-to-head). If a tool is added, a category or a sample belongs here
    /// with it — this list going stale is exactly what happened between
    /// 2026-07-13 and 2026-09-09, when the page still described three
    /// capabilities and the backend had grown to sixteen.
    ///
    /// ⚠️ EVERY QUESTION HERE WAS RUN AGAINST THE LIVE ENDPOINT and returned a
    /// real answer, 2026-09-09. A sample that declines teaches a reader the
    /// feature does not work, which is worse than showing no sample. Re-run
    /// them if the store or the prompt changes.
    ///
    /// Phrasings favour the shapes the parser handles most reliably — "How
    /// many X does <player> have?" and "What is <player>'s <rate>?" are the
    /// fast path's own templates, so they are both the best-served questions
    /// and the ones worth teaching.
    private struct ExampleGroup: Hashable {
        let title: String
        let questions: [String]
    }

    private let exampleGroups: [ExampleGroup] = [
        .init(title: "A player's numbers", questions: [
            "How many home runs does Aaron Judge have?",
            "What's Aaron Judge's batting average with runners in scoring position?",
        ]),
        .init(title: "Leaders and records", questions: [
            "Who has the most career grand slams?",
            "Who has the lowest career ERA?",
        ]),
        .init(title: "Player against player", questions: [
            "Who has more career home runs, Bonds or Aaron?",
            "Did Judge or Ohtani hit more home runs in 2024?",
        ]),
        .init(title: "Streaks and stretches", questions: [
            "Who has the longest hitting streak?",
            "Most home runs in any 30-game span?",
        ]),
        .init(title: "Milestones and feats", questions: [
            "When did Barry Bonds hit his 756th home run?",
            "Has anyone hit 4 home runs in a game?",
        ]),
        .init(title: "Awards", questions: [
            "How many Cy Youngs does Roger Clemens have?",
            "Who has the most MVPs?",
        ]),
        .init(title: "Teams", questions: [
            "How many World Series have the Yankees won?",
            "What's the Dodgers' record against the Giants?",
        ]),
    ]

    var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                backgroundGradient.ignoresSafeArea()
                content
            }
            .navigationTitle("Ask")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .safeAreaInset(edge: .bottom) { inputBar }
        }
        .appearanceOverride()
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        if viewModel.exchanges.isEmpty {
            emptyState
        } else {
            transcript
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 14) {
                    ForEach(viewModel.exchanges) { exchange in
                        AskExchangeCard(
                            exchange: exchange,
                            onSelectCandidate: { candidate in
                                if let id = candidate.mlbam_id {
                                    viewModel.ask(exchange.question, playerId: id)
                                }
                            },
                            onSelectPlayer: { mlbamId in resolveAndPush(mlbamId) }
                        )
                        .id(exchange.id)
                    }
                }
                .padding(.horizontal)
                .padding(.top, 12)
                .padding(.bottom, 12)
            }
            // Keep the newest exchange in view as it's added / resolves.
            .onChange(of: viewModel.exchanges.count) {
                if let last = viewModel.exchanges.last {
                    withAnimation(.easeOut) { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    private var emptyState: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 10) {
                    // ⚠️ DECORATION ONLY, and it is dropped at the
                    // accessibility sizes. At AX5 the icon, a two-line title
                    // and one sentence fill the screen between them, so every
                    // sample — the entire point of this page — sits below the
                    // fold and the reader meets a wall of prose instead of a
                    // list of what they can ask. Nothing is lost by hiding a
                    // sparkle; a great deal is lost by hiding the samples.
                    if !typeSize.isAccessibilitySize {
                        Image(systemName: "sparkles")
                            .font(.largeTitle)
                            .foregroundStyle(.tint)
                    }
                    Text("Ask about baseball")
                        .font(.title2.weight(.bold))
                    // ⚠️ THE ERA CLAIM HAS TWO FLOORS, and the old copy gave
                    // only the later one — "from 1910 to today" — which is the
                    // play-by-play floor and false for season and career
                    // figures, which reach 1876. A reader told 1910 never asks
                    // about Cap Anson, so the sentence was suppressing
                    // questions the system answers correctly.
                    Text("Ask about a player, a team, or the record book.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 24)

                ForEach(exampleGroups, id: \.self) { group in
                  VStack(alignment: .leading, spacing: 10) {
                    Text(group.title)
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(group.questions, id: \.self) { example in
                        Button {
                            inputFocused = false
                            viewModel.ask(example)
                        } label: {
                            HStack(spacing: 10) {
                                Text(example)
                                    .font(.subheadline)
                                    .foregroundStyle(.primary)
                                    .multilineTextAlignment(.leading)
                                Spacer(minLength: 8)
                                Image(systemName: "arrow.up.right")
                                    .font(.footnote.weight(.semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .padding(.horizontal, 14)
                            .padding(.vertical, 13)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                RoundedRectangle(cornerRadius: 12)
                                    .fill(Color(.secondarySystemBackground))
                            )
                        }
                        .buttonStyle(.plain)
                    }
                  }
                }

                // ⚠️ BELOW THE SAMPLES, deliberately. This is reference —
                // which modifiers exist, how far back each store reaches —
                // and it was above them, where at the accessibility sizes it
                // filled the screen on its own and pushed every sample off.
                // The samples are what tell a reader what to ask; the floors
                // are a footnote they consult once. Ordering by that reading
                // helps every size, not only the large ones.
                Text("Narrow by season, opponent, handedness, count, or the postseason. Season and career figures from 1876; pitch-by-pitch situations from 1910. Each question is answered on its own.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
            }
            .padding(.horizontal)
            .padding(.bottom, 12)
        }
    }

    // MARK: - Input

    private var inputBar: some View {
        HStack(spacing: 10) {
            TextField("Ask a question…", text: $viewModel.draft)
                .textFieldStyle(.plain)
                .submitLabel(.send)
                .focused($inputFocused)
                .onSubmit(viewModel.submitDraft)
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                .background(Capsule().fill(Color(.secondarySystemBackground)))

            Button(action: viewModel.submitDraft) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 30))
                    .foregroundStyle(canSend ? Color.accentColor : Color(.tertiaryLabel))
            }
            .disabled(!canSend)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private var canSend: Bool {
        !viewModel.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    // MARK: - Navigation

    /// Resolve a leaderboard row's MLBAM id to a full profile record and push
    /// it. Silent no-op if the id doesn't resolve (rare — pre-integration
    /// players without a Lahman row).
    private func resolveAndPush(_ mlbamId: Int) {
        Task { @MainActor in
            if let player = try? await APIClient.shared.getPlayerByMlbId(mlbamId) {
                path.append(player)
            }
        }
    }

    private var backgroundGradient: LinearGradient {
        LinearGradient(
            colors: [Color(.systemGroupedBackground), Color(.systemBackground)],
            startPoint: .top,
            endPoint: .bottom
        )
    }
}

// MARK: - Exchange card

/// One Q&A in the transcript. The question is a muted header at the top of
/// the card; the answer (or its loading / failure state) is the body. This
/// is the styling that makes the screen read as a log of independent lookups
/// rather than a conversation.
struct AskExchangeCard: View {
    let exchange: AskExchange
    let onSelectCandidate: (AskCandidate) -> Void
    let onSelectPlayer: (Int) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "questionmark.circle")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Text(exchange.question)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider()

            stateBody
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color(.secondarySystemGroupedBackground))
        )
    }

    @ViewBuilder
    private var stateBody: some View {
        switch exchange.state {
        case .loading:
            HStack(spacing: 10) {
                ProgressView()
                Text("Looking that up…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 2)

        case .loaded(let response):
            AskAnswerView(
                response: response,
                onSelectCandidate: onSelectCandidate,
                onSelectPlayer: onSelectPlayer
            )

        case .failed(let message):
            // A genuine transport / quota failure — kept calm (secondary
            // text, a plain circle-i), not an alarming red banner.
            Label {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "exclamationmark.circle")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Floating button

/// The always-available entry point, overlaid on the tab root above the tab
/// bar. Uses the app's accent color.
struct AskFloatingButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "sparkles")
                .font(.title2.weight(.semibold))
                .foregroundStyle(.white)
                .frame(width: 56, height: 56)
                .background(Circle().fill(Color.accentColor.gradient))
                .shadow(color: .black.opacity(0.25), radius: 8, y: 4)
        }
        .accessibilityLabel("Ask a question")
    }
}
