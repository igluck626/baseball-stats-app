from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Players (batters) and pitchers
# ---------------------------------------------------------------------------
# Bio columns (position, bats, throws, height, weight, birth_*, debut,
# final_game) are duplicated across Player and Pitcher. Player.position is
# the primary fielding position (computed from Fielding.csv after load);
# Pitcher.position is always "P".

class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        Index("ix_players_name", "name"),
        # Retrosheet person id (e.g. "acunr001"), mapped from the Chadwick
        # register (key_retro -> key_mlbam). Indexed because the Retrosheet
        # historical ingest resolves players by retro_id heavily.
        Index("ix_players_retro_id", "retro_id"),
    )

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
    # Retrosheet person id (Chadwick key_retro). NULL until the historical
    # ingest / register bridge stamps it; distinct from bbref_id.
    retro_id        = Column(String)
    # BallDontLie's internal player id. Populated by the one-shot
    # `/admin/build-bdl-player-mapping` walk and used by the
    # BDL-migration code paths. NULL for historical players BDL
    # doesn't carry (pre-2010 careers) and for anyone the mapping
    # job hasn't reached yet.
    bdl_id          = Column(Integer)
    mlb_debut       = Column(Integer)
    mlb_last_season = Column(Integer)
    position        = Column(String)
    bats            = Column(String)    # "R" / "L" / "B"
    throws          = Column(String)    # "R" / "L"
    height          = Column(Integer)   # inches
    weight          = Column(Integer)   # pounds
    birth_year      = Column(Integer)
    birth_month     = Column(Integer)
    birth_day       = Column(Integer)
    death_year      = Column(Integer)
    death_month     = Column(Integer)
    death_day       = Column(Integer)
    birth_city      = Column(String)
    birth_state     = Column(String)
    birth_country   = Column(String)
    debut           = Column(String)    # ISO date "YYYY-MM-DD"
    final_game      = Column(String)
    # Hot/cold "heat": signed pct of last-N-game form vs season baseline
    # (e.g. +0.22 = 22% above). Stamped by the nightly heat phase.
    heat_score      = Column(Float)
    heat_tier       = Column(String)    # red_hot | hot | neutral | cold | ice_cold | None
    heat_updated    = Column(DateTime)


class PlayerSeason(Base):
    __tablename__ = "player_seasons"

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String)
    league         = Column(String)
    # Row-level provenance for the RAW COUNTING stats: 'retrosheet' | 'bdl' |
    # 'lahman'. WAR/OPS+ are a separate BRef column-overlay, not a row source.
    # Backfilled by year-inference (pre-2008=lahman, 2008+=bdl), then flipped
    # to 'retrosheet' as the historical ingest overwrites the counting stats.
    source         = Column(String)
    WAR            = Column(Float)
    WAR_off        = Column(Float)
    WAR_def        = Column(Float)
    WAA            = Column(Float)
    OPS_plus       = Column(Float)
    runs_above_avg = Column(Float)
    runs_above_rep = Column(Float)
    G              = Column(Integer)
    PA             = Column(Integer)
    AB             = Column(Integer)
    R              = Column(Integer)
    H              = Column(Integer)
    doubles        = Column(Integer)
    triples        = Column(Integer)
    HR             = Column(Integer)
    RBI            = Column(Integer)
    BB             = Column(Integer)
    SO             = Column(Integer)
    SB             = Column(Integer)
    CS             = Column(Integer)
    # Derived from H + 2*doubles + 3*triples + 4*HR, but stored as a
    # column so the leaderboard / leader-detection queries can target
    # it directly (no SQL expression in the aggregate, no missing
    # leader entry on the iOS career table).
    TB             = Column(Integer)
    BA             = Column(Float)
    OBP            = Column(Float)
    SLG            = Column(Float)
    OPS            = Column(Float)
    BABIP          = Column(Float)
    ISO            = Column(Float)
    BB_pct         = Column(Float)
    K_pct          = Column(Float)
    wOBA           = Column(Float)
    # Extended counting stats from Lahman / bref:
    IBB            = Column(Integer)   # intentional walks
    HBP            = Column(Integer)   # hit by pitch
    SF             = Column(Integer)   # sacrifice flies
    SH             = Column(Integer)   # sacrifice hits
    GIDP           = Column(Integer)   # grounded into double plays
    # Stamped on every save_player_seasons() call. iOS uses this on
    # the current-season response to decide which recent box-score
    # lines need to be folded on top of overnight totals — anything
    # whose game started after this timestamp isn't yet in the row.
    last_updated   = Column(DateTime)


class Pitcher(Base):
    __tablename__ = "pitchers"
    __table_args__ = (
        Index("ix_pitchers_name", "name"),
        # Retrosheet person id (Chadwick key_retro); indexed for the historical
        # ingest's retro_id lookups. Mirrors Player.retro_id (two-way players
        # carry the same retro_id on both sides).
        Index("ix_pitchers_retro_id", "retro_id"),
    )

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
    # Retrosheet person id (Chadwick key_retro). See Player.retro_id.
    retro_id        = Column(String)
    # See Player.bdl_id — same mapping, populated for two-way
    # players (Ohtani) on both sides with the same BDL id.
    bdl_id          = Column(Integer)
    mlb_debut       = Column(Integer)
    mlb_last_season = Column(Integer)
    position        = Column(String)
    bats            = Column(String)
    throws          = Column(String)
    height          = Column(Integer)
    weight          = Column(Integer)
    birth_year      = Column(Integer)
    birth_month     = Column(Integer)
    birth_day       = Column(Integer)
    death_year      = Column(Integer)
    death_month     = Column(Integer)
    death_day       = Column(Integer)
    birth_city      = Column(String)
    birth_state     = Column(String)
    birth_country   = Column(String)
    debut           = Column(String)
    final_game      = Column(String)
    # Hot/cold "heat" — see Player.heat_score. For pitchers, positive =
    # pitching better than season baseline (lower ERA/WHIP).
    heat_score      = Column(Float)
    heat_tier       = Column(String)    # red_hot | hot | neutral | cold | ice_cold | None
    heat_updated    = Column(DateTime)
    # Role the heat window was scored as — "SP" (started >50% of appearances)
    # or "RP", set during compute. Lets the heat leaderboard split starters
    # from relievers. None for unscored pitchers. Batters never set this.
    heat_role       = Column(String)    # SP | RP | None


class PitcherSeason(Base):
    __tablename__ = "pitcher_seasons"

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String)
    league         = Column(String)
    # Row-level provenance for the RAW COUNTING stats: 'retrosheet' | 'bdl' |
    # 'lahman'. See PlayerSeason.source.
    source         = Column(String)
    W              = Column(Integer)
    L              = Column(Integer)
    G              = Column(Integer)
    GS             = Column(Integer)
    IP             = Column(Float)
    SO             = Column(Integer)
    BB             = Column(Integer)
    HR             = Column(Integer)
    ERA            = Column(Float)
    WHIP           = Column(Float)
    ERA_plus       = Column(Float)
    FIP            = Column(Float)
    WAR            = Column(Float)
    WAR_def        = Column(Float)
    WAA            = Column(Float)
    runs_above_avg = Column(Float)
    runs_above_rep = Column(Float)
    BABIP          = Column(Float)
    K_per9         = Column(Float)
    BB_per9        = Column(Float)
    HR_per9        = Column(Float)
    # Extended counting stats from Lahman / bref:
    CG             = Column(Integer)   # complete games
    SHO            = Column(Integer)   # shutouts
    SV             = Column(Integer)   # saves
    H              = Column(Integer)   # hits allowed
    ER             = Column(Integer)   # earned runs
    R              = Column(Integer)   # total runs (incl. unearned)
    BAOpp          = Column(Float)     # opponent batting average
    IBB            = Column(Integer)   # intentional walks issued
    WP             = Column(Integer)   # wild pitches
    HBP            = Column(Integer)   # hit by pitch
    BK             = Column(Integer)   # balks
    BFP            = Column(Integer)   # batters faced
    GF             = Column(Integer)   # games finished
    SH             = Column(Integer)   # sacrifice hits allowed
    SF             = Column(Integer)   # sacrifice flies allowed
    GIDP           = Column(Integer)   # double plays induced
    # Mirror of PlayerSeason.last_updated for the pitcher path.
    # Stamped on save_pitcher_seasons() so the live-stats overlay on
    # iOS can compare against game start times.
    last_updated   = Column(DateTime)


# ---------------------------------------------------------------------------
# Retrosheet per-team stints — one row per (player, year, team). The
# player_seasons / pitcher_seasons tables keep ONE combined row per
# player-year; these companion tables hold the per-team breakdown for
# traded players. Raw counting only (no rates/WAR) — populated by the
# Retrosheet ingest; the combined tables remain the display source.
# ---------------------------------------------------------------------------

class PlayerSeasonStint(Base):
    __tablename__ = "player_season_stints"
    __table_args__ = (
        Index("ix_player_season_stints_py", "player_id", "year"),
    )

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String,  primary_key=True)
    stint_order    = Column(Integer)
    G              = Column(Integer)
    PA             = Column(Integer)
    AB             = Column(Integer)
    R              = Column(Integer)
    H              = Column(Integer)
    doubles        = Column(Integer)
    triples        = Column(Integer)
    HR             = Column(Integer)
    RBI            = Column(Integer)
    BB             = Column(Integer)
    SO             = Column(Integer)
    SB             = Column(Integer)
    CS             = Column(Integer)
    IBB            = Column(Integer)
    HBP            = Column(Integer)
    SH             = Column(Integer)
    SF             = Column(Integer)
    GIDP           = Column(Integer)
    TB             = Column(Integer)
    source         = Column(String)
    last_updated   = Column(DateTime)


class PitcherSeasonStint(Base):
    __tablename__ = "pitcher_season_stints"
    __table_args__ = (
        Index("ix_pitcher_season_stints_py", "player_id", "year"),
    )

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String,  primary_key=True)
    stint_order    = Column(Integer)
    W              = Column(Integer)
    L              = Column(Integer)
    G              = Column(Integer)
    GS             = Column(Integer)
    IP             = Column(Float)     # decimal innings (IPouts / 3)
    SO             = Column(Integer)
    BB             = Column(Integer)
    HR             = Column(Integer)
    CG             = Column(Integer)
    SHO            = Column(Integer)
    SV             = Column(Integer)
    H              = Column(Integer)
    ER             = Column(Integer)
    R              = Column(Integer)
    IBB            = Column(Integer)
    WP             = Column(Integer)
    HBP            = Column(Integer)
    BK             = Column(Integer)
    BFP            = Column(Integer)
    GF             = Column(Integer)
    SH             = Column(Integer)
    SF             = Column(Integer)
    GIDP           = Column(Integer)
    source         = Column(String)
    last_updated   = Column(DateTime)


# ---------------------------------------------------------------------------
# Fielding — one row per (player, year, position); stints are summed.
# ---------------------------------------------------------------------------

class PlayerFielding(Base):
    __tablename__ = "player_fielding"

    player_id     = Column(Integer, primary_key=True)
    year          = Column(Integer, primary_key=True)
    position      = Column(String,  primary_key=True)
    team          = Column(String)
    G             = Column(Integer)
    GS            = Column(Integer)
    innings_outs  = Column(Integer)   # InnOuts in Lahman = innings * 3
    PO            = Column(Integer)
    A             = Column(Integer)
    E             = Column(Integer)
    DP            = Column(Integer)
    fielding_pct  = Column(Float)
    RF_per9       = Column(Float)


# ---------------------------------------------------------------------------
# Awards & All-Star appearances
# ---------------------------------------------------------------------------

class PlayerAward(Base):
    __tablename__ = "player_awards"

    # AwardsPlayers.csv allows the same player to receive an award in
    # multiple leagues (rare, e.g. minor-league awards) — include league in PK.
    player_id  = Column(Integer, primary_key=True)
    year       = Column(Integer, primary_key=True)
    award_name = Column(String,  primary_key=True)
    league     = Column(String,  primary_key=True)
    tie        = Column(String)
    notes      = Column(String)


class PlayerAllstar(Base):
    __tablename__ = "player_allstar"

    # Some seasons (1959-1962) had two All-Star games per year, so game_num
    # is part of the PK.
    player_id    = Column(Integer, primary_key=True)
    year         = Column(Integer, primary_key=True)
    game_num     = Column(Integer, primary_key=True)
    team         = Column(String)
    league       = Column(String)
    GP           = Column(Integer)
    starting_pos = Column(Integer)


class PlayerAwardShare(Base):
    """Vote-share rows for the three award votes Lahman ships
    (MVP / Cy Young / Rookie of the Year). One row per
    (player, year, award, league) — the same player can appear in
    both AL and NL columns in rare interleague-eligibility cases.
    `rank` is computed at load time from `points_won` descending
    within each (year, award_id, league) group so callers can
    surface "finished 2nd in MVP voting" without re-sorting.
    """
    __tablename__ = "player_award_shares"

    player_id   = Column(Integer, primary_key=True)
    year        = Column(Integer, primary_key=True)
    # Canonical short code: "MVP" / "CY Young" / "ROY". Stored
    # rather than computed so the table can be filtered cheaply
    # by award without parsing the Lahman string column.
    award_id    = Column(String,  primary_key=True)
    league      = Column(String,  primary_key=True)   # "AL" / "NL" / "ML"
    points_won  = Column(Float)
    points_max  = Column(Float)
    votes_first = Column(Integer)
    rank        = Column(Integer)


# ---------------------------------------------------------------------------
# Postseason — keyed by (player_id, year, round). round is "WS", "ALCS",
# "NLDS", "WC" etc.
# ---------------------------------------------------------------------------

class PlayerPostseasonBatting(Base):
    __tablename__ = "player_postseason_batting"

    player_id = Column(Integer, primary_key=True)
    year      = Column(Integer, primary_key=True)
    round     = Column(String,  primary_key=True)
    team      = Column(String)
    league    = Column(String)
    G         = Column(Integer)
    AB        = Column(Integer)
    R         = Column(Integer)
    H         = Column(Integer)
    doubles   = Column(Integer)
    triples   = Column(Integer)
    HR        = Column(Integer)
    RBI       = Column(Integer)
    BB        = Column(Integer)
    SO        = Column(Integer)
    SB        = Column(Integer)
    CS        = Column(Integer)
    BA        = Column(Float)
    OBP       = Column(Float)
    SLG       = Column(Float)
    OPS       = Column(Float)


class PlayerPostseasonPitching(Base):
    __tablename__ = "player_postseason_pitching"

    player_id = Column(Integer, primary_key=True)
    year      = Column(Integer, primary_key=True)
    round     = Column(String,  primary_key=True)
    team      = Column(String)
    league    = Column(String)
    W         = Column(Integer)
    L         = Column(Integer)
    G         = Column(Integer)
    GS        = Column(Integer)
    SV        = Column(Integer)
    IP        = Column(Float)
    H         = Column(Integer)
    ER        = Column(Integer)
    HR        = Column(Integer)
    BB        = Column(Integer)
    SO        = Column(Integer)
    ERA       = Column(Float)
    WHIP      = Column(Float)


# ---------------------------------------------------------------------------
# Team standings — keyed by (year, team_id). franch_id is indexed because
# /teams/{team_id}/history queries by franchise to follow relocations.
# ---------------------------------------------------------------------------

class BattingGameLog(Base):
    __tablename__ = "batting_gamelogs"
    __table_args__ = (
        Index("ix_batting_gamelogs_player_season", "player_id", "season"),
        Index("ix_batting_gamelogs_date",          "game_date"),
    )

    player_id   = Column(Integer, primary_key=True)
    game_id     = Column(String,  primary_key=True)
    game_date   = Column(Date)
    season      = Column(Integer)
    opponent    = Column(String)
    home_away   = Column(String)    # "H" / "A"
    result      = Column(String)    # "W" / "L" / "T"
    team_score  = Column(Integer)
    opp_score   = Column(Integer)
    PA          = Column(Integer, nullable=True)
    AB          = Column(Integer)
    R           = Column(Integer)
    H           = Column(Integer)
    doubles     = Column(Integer)
    triples     = Column(Integer)
    HR          = Column(Integer)
    RBI         = Column(Integer)
    BB          = Column(Integer)
    IBB         = Column(Integer)
    SO          = Column(Integer)
    SB          = Column(Integer)
    CS          = Column(Integer)
    HBP         = Column(Integer)
    SF          = Column(Integer)
    # Sourced from BDL `/stats` (`gidp` / `sac_bunts`) and the MLB Stats
    # API boxscore (`groundIntoDoublePlay` / `sacBunts`). Stored so the
    # season-row aggregation can sum them — season_stats omits both.
    GIDP        = Column(Integer)
    SH          = Column(Integer)   # sacrifice bunts
    LOB         = Column(Integer)
    # Lineup slot 1-9 and the appearance sequence within it, from Retrosheet
    # daybyday's `slot` / `seq`. SLOT 0 MEANS "NOT IN THE BATTING ORDER" — in
    # 2024 that is 20,994 rows, every one a DH-era pitcher — so 0 is not slot
    # one and must never sort as if it were. Pre-DH the pitcher bats and slot
    # is 9. `seq` is 1 for the man who started in that slot, 2+ for whoever
    # replaced him, which is what orders a slot's occupants correctly.
    slot        = Column(Integer, nullable=True)
    seq         = Column(Integer, nullable=True)
    # Fielding position(s) for this game — "SS", "LF-CF", or the derived
    # "DH" / "PH" / "PR" for an appearance with no position at all. NULL only
    # when the player neither fielded, batted nor ran (~0.45% of rows), where
    # anything else would be a guess.
    pos         = Column(String, nullable=True)


class PitchingGameLog(Base):
    __tablename__ = "pitching_gamelogs"
    __table_args__ = (
        Index("ix_pitching_gamelogs_player_season", "player_id", "season"),
        Index("ix_pitching_gamelogs_date",          "game_date"),
    )

    player_id   = Column(Integer, primary_key=True)
    game_id     = Column(String,  primary_key=True)
    game_date   = Column(Date)
    season      = Column(Integer)
    opponent    = Column(String)
    home_away   = Column(String)
    # "W" / "L" / "ND" / "S" / "H" / "BS" — derived from the per-game stat flags
    result      = Column(String)
    IP          = Column(Float)     # decimal innings (6.1 IP → 6.333)
    H           = Column(Integer)
    R           = Column(Integer)
    ER          = Column(Integer)
    BB          = Column(Integer)
    SO          = Column(Integer)
    HR          = Column(Integer)
    HBP         = Column(Integer)
    WP          = Column(Integer)
    pitches     = Column(Integer)
    strikes     = Column(Integer)


# ---------------------------------------------------------------------------
# Staging game-log tables — a COLUMN-IDENTICAL copy of batting_gamelogs /
# pitching_gamelogs, written by the Retrosheet 2000-2025 replacement ingest so
# the LIVE tables are untouched during verification. A later swap is a clean
# column-match copy. Not read by the app.
# ---------------------------------------------------------------------------

class StagingBattingGameLog(Base):
    __tablename__ = "staging_batting_gamelogs"
    __table_args__ = (
        Index("ix_staging_batting_gamelogs_player_season", "player_id", "season"),
        Index("ix_staging_batting_gamelogs_date",          "game_date"),
    )

    player_id   = Column(Integer, primary_key=True)
    game_id     = Column(String,  primary_key=True)
    game_date   = Column(Date)
    season      = Column(Integer)
    opponent    = Column(String)
    home_away   = Column(String)
    result      = Column(String)
    team_score  = Column(Integer)
    opp_score   = Column(Integer)
    PA          = Column(Integer, nullable=True)
    AB          = Column(Integer)
    R           = Column(Integer)
    H           = Column(Integer)
    doubles     = Column(Integer)
    triples     = Column(Integer)
    HR          = Column(Integer)
    RBI         = Column(Integer)
    BB          = Column(Integer)
    IBB         = Column(Integer)
    SO          = Column(Integer)
    SB          = Column(Integer)
    CS          = Column(Integer)
    HBP         = Column(Integer)
    SF          = Column(Integer)
    GIDP        = Column(Integer)
    SH          = Column(Integer)
    LOB         = Column(Integer)
    # Mirrors BattingGameLog — the retro ingest can be pointed at this table
    # (`/admin/stage-retrosheet-gamelogs`) and builds one row dict for either
    # target, so a column missing here is a hard failure on that path, not a
    # missing value.
    slot        = Column(Integer, nullable=True)
    seq         = Column(Integer, nullable=True)
    pos         = Column(String, nullable=True)


class RetroGameInfo(Base):
    """One row per historical game, from Retrosheet's GAME LOGS (`gl{year}.zip`).

    A different source from the daybyday files that fill the gamelog tables:
    those are per-player-per-game, these are per-GAME, which is why this is its
    own table rather than more columns on `batting_gamelogs`.

    ⚠️ SCHEMA WIRING. `create_all` will CREATE this table because it does not
    exist yet — but it will NOT alter it afterwards. Any column added here
    later must ALSO be listed in `connection._RETRO_GAME_INFO_NEW_COLUMNS`, or
    the deploy leaves prod without it and every read 500s on the first SELECT.
    That is not hypothetical: the same omission was caught in review on the
    batting_gamelogs slot/seq/pos change one commit ago.

    ⚠️ ATTENDANCE, TIME OF GAME AND THE UMPIRES ARE DELIBERATELY UNREAD.
    Nothing in the app surfaces them today: no payload field, no view, no
    query. They are stored anyway because this is a ONE-TIME 128-year ingest
    and the same source row is already being parsed — capturing them now costs
    nothing, and not capturing them would mean re-reading every year again when
    the UI for them is designed. **Do not delete them as dead weight.** They
    are paid-for data waiting on a surface.
    """
    __tablename__ = "retro_game_info"

    # `game_id` alone is the key — one row per game. It already carries the
    # doubleheader number ("retro-BOS190706241" vs "...0"), so the two halves
    # of a doubleheader are distinct rows and cannot collapse into one.
    game_id      = Column(String, primary_key=True)
    game_date    = Column(Date)
    season       = Column(Integer, index=True)
    park_id      = Column(String)     # Retrosheet park code, e.g. "BAL11"
    park_name    = Column(String)     # resolved from the committed parks file
    # Per-inning runs as Retrosheet writes them: one character per inning, with
    # double-digit innings parenthesised ("102(10)250x") and a trailing "x"
    # where the home side did not need to bat. Stored RAW rather than exploded
    # into columns because the inning count is not fixed — extra innings run to
    # 26 in one 1920 game — and the string is the source of truth a reader can
    # check against Retrosheet directly.
    away_line    = Column(String)
    home_line    = Column(String)
    away_runs    = Column(Integer)
    home_runs    = Column(Integer)
    away_hits    = Column(Integer)
    home_hits    = Column(Integer)
    away_errors  = Column(Integer)
    home_errors  = Column(Integer)
    away_lob     = Column(Integer)
    home_lob     = Column(Integer)

    # --- captured, not surfaced (see the warning above) ---------------------
    # NULL where unrecorded, never 0. Retrosheet writes both "" and "0" for an
    # unknown gate — 720 games across a nine-year sample, concentrated in the
    # deadball era (143 in 1912, 188 in 1950, down to ~12 a year by 2000) — and
    # "nobody recorded it" is a different fact from "nobody came". No real zero
    # is lost to this: the one game famously played to an empty park,
    # Baltimore on 2015-04-29, carries a BLANK attendance rather than a zero.
    attendance       = Column(Integer, nullable=True)
    time_of_game_min = Column(Integer, nullable=True)
    # Umpire NAMES, not ids — a name is what any future surface would print.
    # Absence in the source is the literal string "(none)", not an empty field,
    # so it is mapped to NULL on the way in; stored raw it would render as a
    # man called "(none)" standing at third base. LF and RF are normally empty
    # in the regular season and populated for the postseason's six-man crews.
    ump_hp           = Column(String, nullable=True)
    ump_1b           = Column(String, nullable=True)
    ump_2b           = Column(String, nullable=True)
    ump_3b           = Column(String, nullable=True)
    ump_lf           = Column(String, nullable=True)
    ump_rf           = Column(String, nullable=True)


class StagingPitchingGameLog(Base):
    __tablename__ = "staging_pitching_gamelogs"
    __table_args__ = (
        Index("ix_staging_pitching_gamelogs_player_season", "player_id", "season"),
        Index("ix_staging_pitching_gamelogs_date",          "game_date"),
    )

    player_id   = Column(Integer, primary_key=True)
    game_id     = Column(String,  primary_key=True)
    game_date   = Column(Date)
    season      = Column(Integer)
    opponent    = Column(String)
    home_away   = Column(String)
    result      = Column(String)
    IP          = Column(Float)
    H           = Column(Integer)
    R           = Column(Integer)
    ER          = Column(Integer)
    BB          = Column(Integer)
    SO          = Column(Integer)
    HR          = Column(Integer)
    HBP         = Column(Integer)
    WP          = Column(Integer)
    pitches     = Column(Integer)
    strikes     = Column(Integer)


class PlayerHof(Base):
    __tablename__ = "player_hof"

    # Same player can appear on multiple ballots in different years and from
    # different voting bodies (BBWAA / Veterans / Special Election), so the PK
    # spans all three.
    player_id      = Column(Integer, primary_key=True)
    year_inducted  = Column(Integer, primary_key=True)
    voted_by       = Column(String,  primary_key=True)
    category       = Column(String)
    needed         = Column(Integer)
    votes          = Column(Integer)
    inducted       = Column(Boolean)


class TeamSeason(Base):
    __tablename__ = "team_seasons"
    __table_args__ = (
        Index("ix_team_seasons_year",   "year"),
        Index("ix_team_seasons_franch", "franch_id"),
    )

    year         = Column(Integer, primary_key=True)
    team_id      = Column(String,  primary_key=True)
    franch_id    = Column(String)
    team_name    = Column(String)
    # BallDontLie's internal team id. Identical across seasons for
    # the same franchise — populated once by the BDL teams discovery
    # endpoint and the same id is stamped on every year's row for
    # that team. Used as the join key when reading BDL standings
    # or game payloads.
    bdl_id       = Column(Integer)
    league       = Column(String)
    division     = Column(String)
    rank         = Column(Integer)
    G            = Column(Integer)
    W            = Column(Integer)
    L            = Column(Integer)
    win_pct      = Column(Float)
    runs_scored  = Column(Integer)
    runs_allowed = Column(Integer)
    HR           = Column(Integer)
    ERA          = Column(Float)
    attendance   = Column(Integer)
    park_name    = Column(String)
    # Set to utcnow() on every save_team_seasons() call so the standings
    # endpoint can surface "data last updated at X" without depending on
    # in-memory state surviving restarts.
    last_updated = Column(DateTime)
    # Live standings fields, populated by the nightly update from
    # the MLB Stats API. Historical (Lahman-only) seasons leave them
    # NULL — they're dynamic concepts (streak, L10, clinch state) that
    # don't make sense post-season.
    streak_code          = Column(String)   # "W4", "L2"
    last_ten_w           = Column(Integer)
    last_ten_l           = Column(Integer)
    home_w               = Column(Integer)
    home_l               = Column(Integer)
    away_w               = Column(Integer)
    away_l               = Column(Integer)
    games_back           = Column(String)   # MLB API returns "-" or "2.5" — keep as-is
    wild_card_games_back = Column(String)
    clinch_indicator     = Column(String)   # "y" / "x" / "w" / "z" / "e"
    division_leader      = Column(Boolean)
    clinched             = Column(Boolean)
    magic_number         = Column(String)
    elimination_number   = Column(String)


# One row per postseason series outcome (from Lahman's SeriesPost.csv).
# Team-keyed (Lahman team_id, not MLBAM), so there's no `bridge` step
# in the loader — series are matched directly by `teamIDwinner` /
# `teamIDloser` against `team_seasons.team_id`. The autoincrement
# `id` PK + unique constraint on (year, round, team_id_winner) gives
# the upsert path a stable conflict target while keeping the bulk
# loader idempotent across re-runs.
class SeriesPost(Base):
    __tablename__ = "series_post"
    __table_args__ = (
        UniqueConstraint(
            "year", "round", "team_id_winner",
            name="uq_series_post",
        ),
        Index("ix_series_post_year",    "year"),
        Index("ix_series_post_winner",  "team_id_winner"),
        Index("ix_series_post_loser",   "team_id_loser"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    year             = Column(Integer, nullable=False)
    # Lahman round codes: WS, ALCS, NLCS, ALDS1/ALDS2, NLDS1/NLDS2,
    # ALWC/ALWC1/ALWC2, NLWC/NLWC1/NLWC2, etc. Stored raw so future
    # round-code additions don't need a migration.
    round            = Column(String,  nullable=False)
    team_id_winner   = Column(String,  nullable=False)
    league_id_winner = Column(String)
    team_id_loser    = Column(String,  nullable=False)
    league_id_loser  = Column(String)
    wins             = Column(Integer)
    losses           = Column(Integer)
    ties             = Column(Integer)


# ---------------------------------------------------------------------------
# /ask cost controls: one append-only row per question. Doubles as (a) the
# translation cache (look up the most recent successful translation of a
# normalized question) and (b) the question log for analysis / an eventual
# deterministic parser fast-path. No raw IP — identity is hashed.
# ---------------------------------------------------------------------------

class AskLog(Base):
    __tablename__ = "ask_log"
    __table_args__ = (
        Index("ix_ask_log_normalized", "normalized"),
        Index("ix_ask_log_created", "created_at"),
    )

    id             = Column(Integer, primary_key=True)
    created_at     = Column(DateTime)
    question       = Column(Text)          # raw question as asked
    normalized     = Column(String)        # cache key (see _normalize_question)
    prompt_version = Column(String)        # hash of the system prompt + tool schemas
                                           # that produced this translation; the cache
                                           # only reuses rows matching the CURRENT
                                           # version, so any prompt change re-translates
                                           # instead of serving a stale routing.
    tool_name      = Column(String)        # which tool the model chose (NULL = no translation)
    understood_as  = Column(Text)          # extracted params, JSON string
    source         = Column(String)        # plays / season_stats / *_leaderboard / ...
    status         = Column(String)        # ok / declined / out_of_scope / ambiguous / error
    answer         = Column(Text)
    cached         = Column(Boolean)       # translation served from the cache?
    identity_hash  = Column(String)        # sha256(device-id or IP + salt) — never the raw value
    input_tokens   = Column(Integer)
    output_tokens  = Column(Integer)
    timing_ms      = Column(Text)          # timings, JSON string


class GameUnitLeaderboard(Base):
    """Precomputed game-unit leaderboard: each player's best streak (per season)
    and best span (per window+event), computed by REUSING the verified
    single-player _run_streak / _run_span logic (never a second SQL
    implementation), so a leaderboard value always equals the single-player card.
    Refreshed nightly for active players; historical rows are immutable.

    Streaks: one row per (player, season, metric), window=0, event="".
    Spans:   one row per (player, window, event), season=0, metric="span".

    `role` ('bat'/'pit') tags whether the row is a batting or pitching metric — a
    two-way player (Ohtani) has both. It is NOT part of the PK: the metric/event
    vocabularies don't overlap (batting streaks hitting_streak/…, pitching
    win_streak/…; batting spans HR/H/RBI/TB, pitching K/W/SV), so (player_id,
    metric, window, season, event) stays unique across roles; `role` is a filter.
    """
    __tablename__ = "game_unit_leaderboard"

    player_id  = Column(Integer, primary_key=True)
    metric     = Column(String,  primary_key=True)   # hitting_streak / … / win_streak / span
    window     = Column(Integer, primary_key=True, default=0)   # 0 for streaks; N for spans
    season     = Column(Integer, primary_key=True, default=0)   # season for streaks; 0 for spans
    event      = Column(String,  primary_key=True, default="")  # "" for streaks; HR/H/RBI/TB/K/W/SV for spans
    role       = Column(String,  default="bat")   # 'bat' | 'pit' (not in PK — see class doc)
    value      = Column(Integer)
    start_date = Column(Date)
    end_date   = Column(Date)


class LeaderboardJob(Base):
    """Durable state for a game_unit_leaderboard backfill run — one row per run.

    Written synchronously at enqueue (status='running') BEFORE the worker thread
    starts, so /admin/leaderboard-preview shows real state from the very first
    poll instead of a phase:None startup-window gap while the in-memory global was
    still unpopulated. The in-process worker updates the row per batch (single
    worker — the row is the one shared source of truth). It also survives a
    process restart: a 'running' row whose updated_at has gone stale reveals a
    crashed run rather than silently vanishing. The compute/math is unchanged;
    only the state STORAGE moved from an in-memory dict to this row.
    """
    __tablename__ = "leaderboard_job"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    status        = Column(String, nullable=False, default="running")  # running/done/error
    phase         = Column(String)          # starting/computing/done/error
    confirm       = Column(Boolean, default=False)  # False = dry run (no table writes)
    players_done  = Column(Integer, default=0)
    players_total = Column(Integer, default=0)
    total_rows    = Column(Integer, default=0)
    rows_written  = Column(Integer, default=0)  # >0 only for confirm=true
    summary_json  = Column(Text)            # JSON: top-15 hitting streaks + top-5 HR/162 spans
    error         = Column(Text)
    started_at    = Column(DateTime)
    updated_at    = Column(DateTime)


# ---------------------------------------------------------------------------
# Game-log reconciliation — the nightly assertion that the per-game rows and
# the season row still agree. Written by `gamelog_recon.run_reconciliation`.
#
# Why this exists: `player_seasons.H` comes from BDL's season_stats and
# `batting_gamelogs` comes from BDL's per-game /stats. Nothing forced them to
# agree once H left `_BATTING_COUNTING_FIELDS`, and a silent disagreement is
# exactly the Brice Turang 8/6 defect — a hit BDL revised after we ingested it,
# invisible until someone divided a batting average back out by hand.
#
# Persisted rather than held on `_nightly_state` for three reasons: that dict
# is in-memory and lies after a restart, the two-run rule needs the previous
# run to compare against, and sizing the re-pull window wants a HISTORY of
# revision ages, not one night's snapshot.
# ---------------------------------------------------------------------------

class GamelogReconRun(Base):
    __tablename__ = "gamelog_recon_runs"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    run_at            = Column(DateTime)
    season            = Column(Integer)
    players_checked   = Column(Integer)
    # Distinct (player, side, stat) disagreements across BOTH sides. Batting
    # reconciles one field (H); pitching reconciles five (H/ER/BB/SO/HR), and
    # they do NOT move together — a scorer ruling a hit an error moves H and
    # ER while leaving BB/SO/HR alone. Per-field counts live in `by_stat_json`.
    disagreeing       = Column(Integer)
    bat_disagreeing   = Column(Integer)
    pit_disagreeing   = Column(Integer)
    # JSON {"bat:H": {"n":…, "reachable":…, "value":…, "coverage":…}, …}
    by_stat_json      = Column(Text)
    # Split of the above by whether a BDL re-pull could ever fix it.
    disagreeing_reachable   = Column(Integer)
    disagreeing_unreachable = Column(Integer)
    # Disagreements that ALSO appeared in the previous run — the two-run rule.
    # A live game at run time makes the season row (which BDL updates during
    # play) briefly outrun the logs (finals only); such a gap closes by the
    # next run, so only a persisting one is real.
    confirmed         = Column(Integer)
    # JSON {gap: player_count}, e.g. {"-1": 5, "1": 9, "3": 1}. Signed, so
    # "our logs have too many hits" and "too few" stay distinguishable.
    magnitude_json    = Column(Text)
    max_abs_gap       = Column(Integer)
    # League-wide, INDEPENDENT of whether anyone disagrees: how many distinct
    # games sit under ids BDL has never heard of. Its own number because a
    # RISE means the MLB-gamePk drift path is getting worse.
    # Counted per log table — the two are filled by different backfills and
    # their coverage genuinely differs (2026: batting 379 games/586 rows,
    # pitching 714/5,915), so a single combined number would hide a move in
    # either one.
    unreachable_games = Column(Integer)   # batting + pitching, distinct games
    unreachable_rows  = Column(Integer)
    unreachable_games_bat = Column(Integer)
    unreachable_rows_bat  = Column(Integer)
    unreachable_games_pit = Column(Integer)
    unreachable_rows_pit  = Column(Integer)
    bdl_game_ids_seen = Column(Integer)
    # Tier 2 — per-game attribution.
    tier2_players     = Column(Integer)
    tier2_attributed  = Column(Integer)
    # A gap has two possible causes and they need DIFFERENT fixes, so they are
    # never summed. VALUE gaps are games we both hold where the hit totals
    # differ — a scorer revision, repaired by re-pulling that date, and the
    # only kind whose age should size the re-pull window. COVERAGE gaps are
    # games one side has and the other doesn't; a re-pull will not touch them.
    value_gap_players    = Column(Integer)
    coverage_gap_players = Column(Integer)
    tier2_requests    = Column(Integer)
    oldest_attributed = Column(Date)
    median_age_days   = Column(Float)
    duration_seconds  = Column(Float)
    # Non-fatal: a failure is recorded here, never raised into the nightly.
    error             = Column(Text)


class GamelogReconFinding(Base):
    __tablename__ = "gamelog_recon_findings"
    __table_args__ = (
        Index("ix_gamelog_recon_findings_run",    "run_id"),
        Index("ix_gamelog_recon_findings_player", "player_id", "season"),
        Index("ix_gamelog_recon_findings_stat",   "run_id", "side", "stat"),
    )

    run_id            = Column(Integer, primary_key=True)
    player_id         = Column(Integer, primary_key=True)
    # 'bat' | 'pit'. A two-way player reconciles on both sides independently.
    side              = Column(String,  primary_key=True)
    # 'H' for batting; 'H' | 'ER' | 'BB' | 'SO' | 'HR' for pitching. Part of
    # the key because one pitcher can disagree on several at once.
    stat              = Column(String,  primary_key=True)
    season            = Column(Integer)
    log_sum           = Column(Integer)
    season_value      = Column(Integer)
    gap               = Column(Integer)   # log_sum - season_value, signed
    reachable         = Column(Boolean)
    non_bdl_rows      = Column(Integer)
    confirmed         = Column(Boolean)
    # How the gap decomposes. `value_gap` is the signed sum of (ours - BDL)
    # over games we BOTH hold; `coverage_gap` is the remainder, which means
    # the two sides disagree about which games exist. `gap_explained` is true
    # only when the value side accounts for the whole gap — when it is false,
    # the attributed game below is a PARTIAL story and re-pulling that date
    # will not close the row.
    diff_games        = Column(Integer)
    value_gap         = Column(Integer)
    coverage_gap      = Column(Integer)
    gap_explained     = Column(Boolean)
    # Tier 2 attribution — the single game the gap traces to, when it traces
    # to exactly one. Null when tier 2 found none or more than one.
    game_id           = Column(String)
    game_date         = Column(Date)
    revision_age_days = Column(Integer)
    ours_value        = Column(Integer)
    bdl_value         = Column(Integer)
