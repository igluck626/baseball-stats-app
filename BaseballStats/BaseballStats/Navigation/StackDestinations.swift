//
//  StackDestinations.swift
//  BaseballStats
//
//  One place that says what a navigation stack can push.
//

import SwiftUI

/// Registers the destinations every stack in the app should be able to push:
/// a player profile, and a box score.
///
/// ⚠️ WHY THIS EXISTS. `navigationDestination` registers against the ENCLOSING
/// `NavigationStack`, so a value pushed onto a path whose stack never declared
/// a handler for its type does nothing at all — no crash, no log, no view. The
/// app had 14 stacks that could show a player profile and only 3 that could
/// show a box score, so a `Game` pushed from a profile opened via Search or
/// Leaderboards was silently ignored while the same push from Scores worked.
///
/// That asymmetry has already cost a bug once: `AwardVotingRow` carried a note
/// explaining that two different views each had to declare the player
/// destination, which is the same omission caught one type earlier.
///
/// ⚠️ SO THE POINT IS NOT BREVITY. Collapsing ten-line closures into one line
/// is incidental; the point is that "which types can this stack push" stops
/// being a thing to remember at fourteen call sites and becomes a thing you
/// either applied or did not.
///
/// Twelve stacks declare the player destination; three declared the game one.
/// (Two further matches in a grep are DOC COMMENTS — `BoxScoreView` and
/// `AwardVotingRow` describe the destination they rely on rather than
/// declaring one, and `AwardVotingRow` is right to: a row resolves against
/// whatever stack hosts it.)
/// ⚠️ APPLYING THIS COSTS TYPE-CHECKING BUDGET, AND ON A LARGE BODY IT CAN
/// EXHAUST IT. A SwiftUI view builder is ONE expression, so every modifier
/// added to a chain costs inference time for the whole chain — and this one
/// takes six arguments. Adding it to `HomeView.body`, which already carried
/// five `.sheet` modifiers, produced:
///
///     error: the compiler is unable to type-check this expression in
///            reasonable time; try breaking up the expression into distinct
///            sub-expressions
///
/// THE REMEDY IS EXTRACTION, NOT SIMPLIFYING THIS MODIFIER. Pull one branch of
/// the chain into its own `@ViewBuilder` property — `HomeView.historySheet` is
/// the worked example — which changes nothing at runtime and gives the checker
/// a smaller expression to solve. Reaching instead for fewer arguments here
/// would trade a compile-time cost for a runtime one, since the arguments this
/// modifier drops are the ones that stop it crashing in a sheet.
///
/// So: expect to extract a sheet or two when adopting this on a big view. That
/// is a cost of the modifier, not a defect in the view you are applying it to.
/// Everything a stack must supply for a view pushed onto it to open a box
/// score.
///
/// ⚠️ A STRUCT RATHER THAN FOUR-AND-COUNTING PARAMETERS, and the reason is the
/// cascade rather than tidiness. `PlayerProfileView` needs these to construct
/// `AwardVotingView`, which needs them to construct a box score, so every
/// addition propagates through views that do not use it themselves. Four
/// arguments is tolerable; this is the second time the list has grown, and the
/// next growth would be free with a struct and expensive without one.
///
/// ⚠️ AND ITS OPTIONALITY IS THE CAPABILITY. A non-nil context means "this
/// stack registered the destination AND has somewhere to push", because only
/// `.stackDestinations(...)` builds one. That is the same reasoning that made
/// an optional path binding better than a `canPushBoxScore: Bool` — a flag can
/// disagree with reality; a context that is only ever constructed alongside the
/// destination cannot.
struct BoxScoreContext {
    let path: Binding<NavigationPath>
    /// Gates the pushed box score's live polling via
    /// `navigation.shouldPoll(on:)`.
    let owningTab: AppNavigation.Tab
    let navigation: AppNavigation
    let liveStore: LiveGameStore
    /// Records + division ranks for the box score's team header. Empty is a
    /// SUPPORTED value, not a degraded one — most stacks hold no standings and
    /// `teamHeader` reads both through `if let`, so a miss omits the "(78-54)"
    /// and "3rd AL East" sub-lines rather than rendering an empty parenthetical.
    var teamStandings: [Int: TeamStandingInfo] = [:]
    var teamRecords: [Int: TeamRecord] = [:]
}

// ⚠️ ONE STACK IS DELIBERATELY NOT COVERED: AskView. It is a
// `fullScreenCover` launched from a floating button rather than a tab, so
// `BoxScoreContext.owningTab` — which gates the pushed box score's live
// polling through `navigation.shouldPoll(on:)` — has no correct value for it.
// Giving Ask a `Tab` case means excluding that case everywhere `Tab` is
// iterated (the TabView itself, `CaseIterable`, `TabOrderStore`); making
// `owningTab` optional with always-poll semantics would leave a 15-second BDL
// loop running behind the cover and after dismissal, with no visible symptom.
// Neither is a call to make while wiring navigation, so Ask stays uncovered
// until it is made. A KNOWN GAP, not an oversight.
struct StackDestinations: ViewModifier {
    /// ⚠️ PASSED, NOT READ FROM THE ENVIRONMENT, AND DELIBERATELY SO. Five of
    /// the twelve stacks are SHEETS, and `.sheet` content is hosted separately
    /// from its presenter. `ScheduleSheet` already takes both as
    /// `@ObservedObject` properties rather than reading the environment —
    /// evidence that someone met this and worked around it — and a missing
    /// `@EnvironmentObject` is a RUNTIME CRASH, not a compile error. An
    /// environment-reading modifier would build clean and die the first time
    /// somebody opened a sheet and tapped a game.
    let context: BoxScoreContext

    /// Records + division ranks for the box score's team header.
    ///
    /// ⚠️ EMPTY IS A SUPPORTED VALUE, NOT A DEGRADED ONE TO BE AVOIDED. Ten of
    /// the fourteen stacks hold no standings — Search and Leaderboards have no
    /// reason to — and `teamHeader` reads both through `if let`, so a miss
    /// omits the "(78-54)" and "3rd AL East" lines rather than rendering an
    /// empty parenthetical or a dash where a record belongs. A box score
    /// reached from Search is therefore the same box score, minus two optional
    /// sub-lines. That is a far better outcome than the tap doing nothing,
    /// which is what those stacks do today.
    func body(content: Content) -> some View {
        content
            .navigationDestination(for: PlayerSearchResult.self) { player in
                // The path goes WITH the destination, so a profile can only be
                // told it may push a box score by the same thing that
                // registers where the box score goes.
                PlayerProfileView(player: player, boxScoreContext: context)
            }
            .navigationDestination(for: Game.self) { game in
                BoxScoreView(
                    game:           game,
                    teamStandings:  context.teamStandings,
                    teamRecords:    context.teamRecords,
                    path:           context.path,
                    owningTab:      context.owningTab,
                    navigation:     context.navigation,
                    liveStore:      context.liveStore,
                )
            }
    }
}

extension View {
    /// Apply at every `NavigationStack` root. See `StackDestinations`.
    /// Apply at every `NavigationStack` root. See `StackDestinations`.
    func stackDestinations(_ context: BoxScoreContext) -> some View {
        modifier(StackDestinations(context: context))
    }

    /// For a stack whose context is only sometimes available — a sheet that may
    /// have been presented from a screen that had none. Registers the player
    /// destination either way, so a profile still opens; only the box score
    /// needs the context.
    @ViewBuilder
    func applyStackDestinations(_ context: BoxScoreContext?) -> some View {
        if let context {
            modifier(StackDestinations(context: context))
        } else {
            navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
        }
    }
}
