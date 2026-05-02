from sqlalchemy.orm import Session

from .models import Player, PlayerSeason


def get_player(db: Session, player_id: int) -> Player | None:
    return db.get(Player, player_id)


def save_player(db: Session, player_info: dict) -> None:
    db.merge(Player(**player_info))


def get_player_seasons(db: Session, player_id: int) -> list[PlayerSeason]:
    return db.query(PlayerSeason).filter(PlayerSeason.player_id == player_id).all()


def save_player_seasons(db: Session, player_id: int, seasons: list[dict]) -> None:
    for season in seasons:
        db.merge(PlayerSeason(player_id=player_id, **season))
