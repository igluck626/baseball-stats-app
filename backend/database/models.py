from sqlalchemy import Boolean, Column, Date, DateTime, Float, Index, Integer, String
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
    __table_args__ = (Index("ix_players_name", "name"),)

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
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
    birth_city      = Column(String)
    birth_state     = Column(String)
    birth_country   = Column(String)
    debut           = Column(String)    # ISO date "YYYY-MM-DD"
    final_game      = Column(String)


class PlayerSeason(Base):
    __tablename__ = "player_seasons"

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String)
    league         = Column(String)
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
    __table_args__ = (Index("ix_pitchers_name", "name"),)

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
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
    birth_city      = Column(String)
    birth_state     = Column(String)
    birth_country   = Column(String)
    debut           = Column(String)
    final_game      = Column(String)


class PitcherSeason(Base):
    __tablename__ = "pitcher_seasons"

    player_id      = Column(Integer, primary_key=True)
    year           = Column(Integer, primary_key=True)
    team           = Column(String)
    league         = Column(String)
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
    LOB         = Column(Integer)


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
