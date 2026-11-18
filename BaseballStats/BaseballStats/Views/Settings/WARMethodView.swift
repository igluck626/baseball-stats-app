//
//  WARMethodView.swift
//  BaseballStats
//
//  "How we calculate WAR": pushed from Settings, and from the ⓘ next to WAR on a player's season card and on the
//  WAR leaderboard. Approved by Isaac 2026-11-17/18: say our figures "will differ slightly from other sources",
//  include a Limits section, and a collapsed "Show the math" with the formulas but none of the tuned weights.
//  The method itself is backend/war (README.md there); keep this text in step with it.
//

import SwiftUI

struct WARMethodView: View {
    @State private var showMath = false

    var body: some View {
        Form {
            Section {
                Text("WAR (wins above replacement) estimates how many wins a player added compared with a replacement-level player: the kind of player a team could sign or call up at little cost.")
                Text("WhosOnFirst calculates its own WAR for every season from 1871 to today. Our figures will differ slightly from other sources.")
                    .foregroundStyle(.secondary)
            }

            Section("Hitters") {
                Text("A hitter's WAR is **Offense** plus **Defense**.")
                Text("**Offense** counts batting, baserunning and avoiding double plays, compared with an average hitter in the same league and ballpark, plus credit for playing time above replacement level.")
                Text("**Defense** counts the difficulty of the positions played and how well the player fielded them.")
            }

            Section("Pitchers") {
                Text("A pitcher's WAR compares the runs a pitcher allowed with what an average pitcher would allow in the same ballparks and league, behind the same defense, then adds credit for innings above replacement level.")
                Text("Relievers' innings count more when they usually enter in high-pressure moments, for better or worse.")
                Text("A pitcher's own hitting is counted separately, as batting WAR.")
            }

            Section("Where the numbers come from") {
                Text("Past seasons are built from Retrosheet's play-by-play and the SABR Lahman Baseball Database. The current season comes from balldontlie.")
                Text("The current season updates every night and can still change. Once Retrosheet publishes a season's full play-by-play, that season is recalculated once and then stays fixed.")
            }

            Section("Limits") {
                limit("Fielding is the hardest part to measure. Since 2003, some other sources use video-scouting data that isn't publicly available, so our defense figures for recent seasons differ the most.")
                limit("Before about 1910 there is little play-by-play, so fielding comes from season totals and is less precise.")
                limit("Negro Leagues seasons aren't rated yet.")
                limit("WAR is an estimate. Differences of a win or so between players aren't meaningful.")
            }

            Section {
                DisclosureGroup(isExpanded: $showMath) {
                    VStack(alignment: .leading, spacing: 12) {
                        formula("Hitter WAR", "Offense + Defense")
                        formula("Offense", "(batting runs + baserunning runs + double-play runs + replacement runs) ÷ runs per win")
                        formula("Defense", "(position runs + fielding runs) ÷ runs per win")
                        formula("Batting runs", "the run value of each plate appearance, minus an average hitter's, adjusted for the ballparks played in")
                        formula("Pitcher WAR", "wins above average + replacement wins")
                        formula("Runs above average", "innings ÷ 9 × expected runs per 9 − runs allowed − runs saved by the defense behind the pitcher")
                        formula("Expected runs per 9", "league runs per 9 × ballpark factor")
                        formula("Relief innings", "wins above average × (1 + entry leverage) ÷ 2")
                        formula("Runs per win", "10 × √(runs per inning, both teams)")
                        Text("Replacement level is set so that all players together are worth about 1,000 wins over a 162-game season.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                } label: {
                    Text("Show the math")
                }
                .accessibilityIdentifier("warShowTheMath")
            }
        }
        .navigationTitle("How We Calculate WAR")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func limit(_ text: String) -> some View {
        Text(text)
    }

    /// One formula: its name, then the formula on its own line (wraps at large text sizes instead of truncating).
    private func formula(_ name: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name).font(.subheadline.weight(.semibold))
            Text(body).font(.subheadline).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }
}

/// The ⓘ next to WAR: opens "How we calculate WAR".
struct WARInfoButton: View {
    var body: some View {
        NavigationLink {
            WARMethodView()
        } label: {
            Image(systemName: "info.circle")
                .imageScale(.medium)
                .foregroundStyle(.secondary)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle().inset(by: -12))      // a ~44pt tap target without growing the line it sits on
        .accessibilityLabel("How we calculate WAR")
        .accessibilityIdentifier("warInfoButton")
    }
}
