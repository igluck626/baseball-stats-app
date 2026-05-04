import datetime

from sqlalchemy.orm import Session

from .models import (
    Pitcher,
    PitcherSeason,
    Player,
    PlayerAllstar,
    PlayerAward,
    PlayerFielding,
    PlayerHof,
    PlayerPostseasonBatting,
    PlayerPostseasonPitching,
    PlayerSeason,
    TeamSeason,
)


# ---------------------------------------------------------------------------
# Players (batters)
# ---------------------------------------------------------------------------

def get_player(db: Session, player_id: int) -> Player | None:
    return db.get(Player, player_id)


def save_player(db: Session, player_info: dict) -> None:
    """Insert or update a player row, never overwriting existing non-null fields with null."""
    existing = db.get(Player, player_info["player_id"])
    if existing is None:
        db.add(Player(**player_info))
    else:
        for field, value in player_info.items():
            if field != "player_id" and value is not None:
                setattr(existing, field, value)


def search_players_by_name(db: Session, name: str) -> list[Player]:
    """Case-insensitive name search; all words in the query must appear in the name."""
    q = db.query(Player)
    for part in name.strip().split():
        if len(part) > 1:
            q = q.filter(Player.name.ilike(f"%{part}%"))
    return q.all()


def get_player_seasons(db: Session, player_id: int) -> list[PlayerSeason]:
    return db.query(PlayerSeason).filter(PlayerSeason.player_id == player_id).all()


def save_player_seasons(db: Session, player_id: int, seasons: list[dict]) -> None:
    for season in seasons:
        db.merge(PlayerSeason(player_id=player_id, **season))


def get_all_player_ids(db: Session) -> list[int]:
    rows = db.query(PlayerSeason.player_id).distinct().all()
    return [r.player_id for r in rows]


# ---------------------------------------------------------------------------
# Pitchers
# ---------------------------------------------------------------------------

def get_pitcher(db: Session, player_id: int) -> Pitcher | None:
    return db.get(Pitcher, player_id)


def save_pitcher(db: Session, player_info: dict) -> None:
    """Insert or update a pitcher row, never overwriting existing non-null fields with null."""
    existing = db.get(Pitcher, player_info["player_id"])
    if existing is None:
        db.add(Pitcher(**player_info))
    else:
        for field, value in player_info.items():
            if field != "player_id" and value is not None:
                setattr(existing, field, value)


def search_pitchers_by_name(db: Session, name: str) -> list[Pitcher]:
    """Case-insensitive name search; all words in the query must appear in the name."""
    q = db.query(Pitcher)
    for part in name.strip().split():
        if len(part) > 1:
            q = q.filter(Pitcher.name.ilike(f"%{part}%"))
    return q.all()


def get_pitcher_seasons(db: Session, player_id: int) -> list[PitcherSeason]:
    return db.query(PitcherSeason).filter(PitcherSeason.player_id == player_id).all()


def save_pitcher_seasons(db: Session, player_id: int, seasons: list[dict]) -> None:
    for season in seasons:
        db.merge(PitcherSeason(player_id=player_id, **season))


def get_all_pitcher_ids(db: Session) -> list[int]:
    rows = db.query(PitcherSeason.player_id).distinct().all()
    return [r.player_id for r in rows]


# ---------------------------------------------------------------------------
# Fielding
# ---------------------------------------------------------------------------

def get_player_fielding(db: Session, player_id: int) -> list[PlayerFielding]:
    return (
        db.query(PlayerFielding)
        .filter(PlayerFielding.player_id == player_id)
        .order_by(PlayerFielding.year, PlayerFielding.position)
        .all()
    )


def save_player_fielding(db: Session, player_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerFielding(player_id=player_id, **r))


# ---------------------------------------------------------------------------
# Awards & All-Star
# ---------------------------------------------------------------------------

def get_player_awards(db: Session, player_id: int) -> list[PlayerAward]:
    return (
        db.query(PlayerAward)
        .filter(PlayerAward.player_id == player_id)
        .order_by(PlayerAward.year, PlayerAward.award_name)
        .all()
    )


def save_player_awards(db: Session, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerAward(**r))


def get_player_allstar(db: Session, player_id: int) -> list[PlayerAllstar]:
    return (
        db.query(PlayerAllstar)
        .filter(PlayerAllstar.player_id == player_id)
        .order_by(PlayerAllstar.year, PlayerAllstar.game_num)
        .all()
    )


def save_player_allstar(db: Session, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerAllstar(**r))


# ---------------------------------------------------------------------------
# Postseason
# ---------------------------------------------------------------------------

def get_player_postseason_batting(db: Session, player_id: int) -> list[PlayerPostseasonBatting]:
    return (
        db.query(PlayerPostseasonBatting)
        .filter(PlayerPostseasonBatting.player_id == player_id)
        .order_by(PlayerPostseasonBatting.year, PlayerPostseasonBatting.round)
        .all()
    )


def save_player_postseason_batting(db: Session, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerPostseasonBatting(**r))


def get_player_postseason_pitching(db: Session, player_id: int) -> list[PlayerPostseasonPitching]:
    return (
        db.query(PlayerPostseasonPitching)
        .filter(PlayerPostseasonPitching.player_id == player_id)
        .order_by(PlayerPostseasonPitching.year, PlayerPostseasonPitching.round)
        .all()
    )


def save_player_postseason_pitching(db: Session, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerPostseasonPitching(**r))


# ---------------------------------------------------------------------------
# Hall of Fame
# ---------------------------------------------------------------------------

def get_player_hof(db: Session, player_id: int) -> list[PlayerHof]:
    return (
        db.query(PlayerHof)
        .filter(PlayerHof.player_id == player_id)
        .order_by(PlayerHof.year_inducted, PlayerHof.voted_by)
        .all()
    )


def save_player_hof(db: Session, rows: list[dict]) -> None:
    for r in rows:
        db.merge(PlayerHof(**r))


# ---------------------------------------------------------------------------
# Team standings
# ---------------------------------------------------------------------------

def get_team_standings(db: Session, year: int) -> list[TeamSeason]:
    return (
        db.query(TeamSeason)
        .filter(TeamSeason.year == year)
        .order_by(TeamSeason.league, TeamSeason.division, TeamSeason.rank)
        .all()
    )


def get_team_history_by_franchise(db: Session, franch_id: str) -> list[TeamSeason]:
    return (
        db.query(TeamSeason)
        .filter(TeamSeason.franch_id == franch_id)
        .order_by(TeamSeason.year)
        .all()
    )


def get_team_franchise(db: Session, team_id: str) -> str | None:
    """Resolve a teamID (or franchID) to its franchID. Looks at the latest
    matching row to handle teams that changed teamID across history."""
    row = (
        db.query(TeamSeason.franch_id)
        .filter((TeamSeason.team_id == team_id) | (TeamSeason.franch_id == team_id))
        .order_by(TeamSeason.year.desc())
        .first()
    )
    return row.franch_id if row else None


def save_team_seasons(db: Session, rows: list[dict]) -> None:
    """Upsert team-season rows. Stamps last_updated=utcnow() on every row so
    the standings endpoint can show "data as of X"."""
    now = datetime.datetime.utcnow()
    for r in rows:
        db.merge(TeamSeason(last_updated=now, **r))
