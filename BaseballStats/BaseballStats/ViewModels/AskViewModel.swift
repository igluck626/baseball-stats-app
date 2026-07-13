//
//  AskViewModel.swift
//  BaseballStats
//
//  Drives the Ask screen. Each question is INDEPENDENT — there is no
//  conversational memory — so the model keeps a flat, append-only list of
//  exchanges (question + its own result state). Asking again never mutates
//  an earlier exchange, which is what lets the transcript read as a HISTORY
//  of separate lookups rather than a chat thread.
//

import Combine
import Foundation

/// One question and the state of its answer. Immutable question, mutable
/// state as the request resolves.
struct AskExchange: Identifiable {
    let id: UUID
    let question: String
    var state: State

    enum State {
        case loading
        case loaded(AskResponse)
        case failed(String)

        var isLoading: Bool {
            if case .loading = self { return true }
            return false
        }
    }
}

@MainActor
final class AskViewModel: ObservableObject {
    /// The append-only transcript, oldest first.
    @Published var exchanges: [AskExchange] = []
    /// Bound to the input field.
    @Published var draft: String = ""

    private let api: APIClient

    /// Defaults to the shared client; inject a custom one in tests. `.shared`
    /// is resolved in the initializer BODY (a main-actor-isolated context)
    /// rather than as a `= .shared` default argument — a default argument is
    /// evaluated in a nonisolated context, which warns under strict
    /// concurrency now and is an error in the Swift 6 language mode. (The
    /// app's older view models still use the default-argument form and carry
    /// that warning; this is the clean version.)
    init(api: APIClient? = nil) {
        self.api = api ?? .shared
    }

    /// Whether any exchange is still in flight (input can stay usable —
    /// questions are independent, so we allow concurrent asks).
    var isBusy: Bool { exchanges.contains { $0.state.isLoading } }

    /// Send whatever's in the input field, then clear it.
    func submitDraft() {
        let question = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { return }
        draft = ""
        ask(question)
    }

    /// Ask `question`, optionally pinned to a specific player (from an
    /// ambiguous-name candidate tap). Appends a new exchange and resolves it
    /// independently — earlier exchanges are never touched, so their indices
    /// stay valid for the whole session.
    func ask(_ question: String, playerId: Int? = nil) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        let exchange = AskExchange(id: UUID(), question: trimmed, state: .loading)
        exchanges.append(exchange)
        let id = exchange.id

        Task {
            do {
                let response = try await api.ask(question: trimmed, playerId: playerId)
                update(id, .loaded(response))
            } catch let error as APIError {
                update(id, .failed(error.errorDescription ?? "Something went wrong."))
            } catch {
                update(id, .failed(error.localizedDescription))
            }
        }
    }

    private func update(_ id: UUID, _ state: AskExchange.State) {
        guard let index = exchanges.firstIndex(where: { $0.id == id }) else { return }
        exchanges[index].state = state
    }
}
