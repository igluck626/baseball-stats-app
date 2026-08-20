//
//  PeriodicRefresh.swift
//  BaseballStats
//
//  One place to say "re-read this while the user is looking at it".
//
//  Every time-sensitive screen in this app loaded once, in `.task`, and never
//  again. That is invisible for the first few minutes and wrong after an hour:
//  a Scores tab left open showed games at their start time long after they had
//  finished, because the only things that could update a game were the live
//  poll (which never mentions a finished game) and `finalize` (which only
//  rescues games the app happened to watch go live). A game that went live and
//  ended while the tab sat idle was touched by neither.
//
//  The fix is not interesting; the point is that it is written ONCE. The bug
//  has siblings — StandingsView, LeaderboardsView, TeamLeadersSheet all load in
//  `.task` with no refresh path — and fixing each with its own bespoke loop is
//  how the pattern keeps regenerating. Whatever the next screen needs, it
//  should adopt this rather than copy it.
//

import SwiftUI

/// The loop itself, as a plain async function so it can be tested without a
/// view. `PeriodicRefreshModifier` is a thin wrapper over this.
enum PeriodicRefreshLoop {
    /// Runs `action` immediately, then every `interval` until cancelled.
    ///
    /// LEADS with the action rather than sleeping first. This is the whole
    /// behaviour on resume: coming back to a tab after ten minutes should show
    /// fresh data now, not in ninety seconds. Sleeping first would mean the
    /// first thing a returning user sees is guaranteed stale.
    ///
    /// Returns immediately when `isActive` is false, so a backgrounded or
    /// switched-away tab costs nothing.
    static func run(
        interval: Duration,
        isActive: Bool,
        action: () async -> Void,
    ) async {
        guard isActive else { return }
        await action()                       // leading — see above
        while !Task.isCancelled {
            do { try await Task.sleep(for: interval) } catch { return }  // cancelled
            guard !Task.isCancelled else { return }
            await action()
        }
    }
}

private struct PeriodicRefreshModifier: ViewModifier {
    let interval: Duration
    let isActive: Bool
    let action: () async -> Void

    func body(content: Content) -> some View {
        // `.task(id:)` is doing the lifecycle work: it cancels and restarts the
        // task whenever `isActive` flips, and SwiftUI cancels it on disappear.
        // So "stop when the tab is hidden" and "start again on return, leading
        // with a fetch" both fall out of the id change — there is no separate
        // subscribe/unsubscribe pair to keep balanced.
        content.task(id: isActive) {
            await PeriodicRefreshLoop.run(
                interval: interval, isActive: isActive, action: action,
            )
        }
    }
}

extension View {
    /// Re-run `action` every `interval` for as long as `isActive` is true,
    /// starting immediately rather than after the first interval.
    ///
    /// `isActive` should be the screen's existing visibility signal — for the
    /// tabbed screens that is `navigation.shouldPoll(on:)`, the same gate the
    /// live-game store uses, so refreshing and polling stop together.
    ///
    /// The action must be QUIET and NON-DESTRUCTIVE: no loading spinner, and a
    /// failed fetch must leave the previous data on screen. A background tick
    /// that blanks the list on a transient blip is worse than the staleness it
    /// set out to fix.
    func periodicRefresh(
        every interval: Duration,
        isActive: Bool,
        action: @escaping () async -> Void,
    ) -> some View {
        modifier(PeriodicRefreshModifier(
            interval: interval, isActive: isActive, action: action,
        ))
    }
}

/// Shared cadence for the slate screens.
enum RefreshCadence {
    /// 90 seconds. The live poll is 15s because scores move pitch to pitch; a
    /// slate only changes at the four transitions a game makes all day, so it
    /// does not need that. The floor is 30s regardless — `getGames` caches at
    /// `ttl: 30`, so anything faster reads its own cache and achieves nothing.
    /// 90s keeps a finished game stale for at most a minute and a half, against
    /// the hours it used to be, for a quarter of the live poll's request rate.
    static let slate: Duration = .seconds(90)
}
