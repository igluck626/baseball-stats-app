from sqlalchemy import Column, Float, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (Index("ix_players_name", "name"),)

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
    mlb_debut       = Column(Integer)
    mlb_last_season = Column(Integer)


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


class Pitcher(Base):
    __tablename__ = "pitchers"
    __table_args__ = (Index("ix_pitchers_name", "name"),)

    player_id       = Column(Integer, primary_key=True)
    name            = Column(String, nullable=False)
    bbref_id        = Column(String)
    mlb_debut       = Column(Integer)
    mlb_last_season = Column(Integer)


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
