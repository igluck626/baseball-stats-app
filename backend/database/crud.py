from sqlalchemy.orm import Session

from .models import Pitcher, PitcherSeason, Player, PlayerSeason


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
