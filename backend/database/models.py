from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String
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
