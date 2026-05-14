import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import (
    BattingGameLog,
    Pitcher,
    PitcherSeason,
    PitchingGameLog,
    Player,
    PlayerAllstar,
    PlayerAward,
    PlayerAwardShare,
    PlayerFielding,
    PlayerHof,
    PlayerPostseasonBatting,
    PlayerPostseasonPitching,
    PlayerSeason,
    TeamSeason,
)


def _upsert_season(db: Session, model, row: dict) -> None:
    """Insert-or-update one season row keyed on the table's composite
    PK (player_id, year). PostgreSQL gets the native atomic
    ``INSERT ... ON CONFLICT (player_id, year) DO UPDATE SET …`` —
    cheap, concurrent-safe, and won't produce duplicates even under
    parallel ingest. SQLite falls back to ``db.merge()`` (SELECT-then-
    UPDATE/INSERT) since the older SQLite versions in CI don't have
    ON CONFLICT in the same SQLAlchemy form.

    `row` must contain `player_id` and `year`; every other key in the
    dict becomes part of the SET clause. The PK columns are excluded
    from the SET so we don't try to overwrite themselves with the same
    values (Postgres accepts it, but the resulting NOOP update is
    wasted work)."""
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = pg_insert(model).values(**row)
        update_cols = {
            k: stmt.excluded[k]
            for k in row.keys()
            if k not in ("player_id", "year")
        }
        # No non-PK columns means there's nothing to update on
        # conflict — just skip writing the duplicate row entirely.
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=["player_id", "year"],
                set_=update_cols,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["player_id", "year"],
            )
        db.execute(stmt)
    else:
        db.merge(model(**row))


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
    """Upsert batting season rows for one player. Uses PG-native
    ON CONFLICT (player_id, year) DO UPDATE so concurrent ingests can't
    produce duplicate (player_id, year) rows — the historical bug that
    surfaced "Sosa 1998" three times in the All-Time HR leaderboard."""
    for season in seasons:
        _upsert_season(db, PlayerSeason, {"player_id": player_id, **season})


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
    """Upsert pitching season rows. Same ON CONFLICT path as
    save_player_seasons — keeps pitcher_seasons free of duplicate
    (player_id, year) pairs even under parallel ingest."""
    for season in seasons:
        _upsert_season(db, PitcherSeason, {"player_id": player_id, **season})


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


def get_player_award_shares(db: Session, player_id: int) -> list[PlayerAwardShare]:
    return (
        db.query(PlayerAwardShare)
        .filter(PlayerAwardShare.player_id == player_id)
        .order_by(PlayerAwardShare.year,
                  PlayerAwardShare.award_id,
                  PlayerAwardShare.rank)
        .all()
    )


def get_award_share_voting(db: Session, award_id: str,
                           year: int, league: str) -> list[PlayerAwardShare]:
    """Ranked voting leaderboard for a specific (award, year, league)
    — caller turns each row into a `{player, points_won, …}` entry."""
    return (
        db.query(PlayerAwardShare)
        .filter(PlayerAwardShare.award_id == award_id,
                PlayerAwardShare.year == year,
                PlayerAwardShare.league == league)
        .order_by(PlayerAwardShare.rank)
        .all()
    )


def save_player_award_shares(db: Session, rows: list[dict]) -> None:
    """Upsert award-share rows. PostgreSQL gets the native
    `INSERT ... ON CONFLICT (player_id, year, award_id, league)
    DO UPDATE SET ...` form so a re-run of the loader (or the
    `/admin/load-award-shares` endpoint) cleanly overwrites
    points / votes / rank in place instead of leaving stale rows
    or relying on db.merge's SELECT-then-UPDATE/INSERT round-trip.
    SQLite falls back to merge for local-dev compatibility."""
    if not rows:
        return
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        for r in rows:
            stmt = pg_insert(PlayerAwardShare).values(**r)
            update_cols = {
                k: stmt.excluded[k]
                for k in r.keys()
                if k not in ("player_id", "year", "award_id", "league")
            }
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["player_id", "year", "award_id", "league"],
                    set_=update_cols,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["player_id", "year", "award_id", "league"],
                )
            db.execute(stmt)
    else:
        for r in rows:
            db.merge(PlayerAwardShare(**r))


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
# Game logs (batting + pitching)
# ---------------------------------------------------------------------------

def get_batting_gamelogs(
    db: Session,
    player_id: int,
    season: int | None = None,
    last_n: int | None = None,
) -> list[BattingGameLog]:
    q = (
        db.query(BattingGameLog)
        .filter(BattingGameLog.player_id == player_id)
        .order_by(BattingGameLog.game_date.desc())
    )
    if season is not None:
        q = q.filter(BattingGameLog.season == season)
    if last_n is not None:
        q = q.limit(last_n)
    return q.all()


def save_batting_gamelogs(db: Session, player_id: int, games: list[dict]) -> None:
    for g in games:
        # Allow callers to pass dicts that already contain player_id; trust the
        # arg over the dict to keep things consistent.
        merged = {**g, "player_id": player_id}
        db.merge(BattingGameLog(**merged))


def get_pitching_gamelogs(
    db: Session,
    player_id: int,
    season: int | None = None,
    last_n: int | None = None,
) -> list[PitchingGameLog]:
    q = (
        db.query(PitchingGameLog)
        .filter(PitchingGameLog.player_id == player_id)
        .order_by(PitchingGameLog.game_date.desc())
    )
    if season is not None:
        q = q.filter(PitchingGameLog.season == season)
    if last_n is not None:
        q = q.limit(last_n)
    return q.all()


def save_pitching_gamelogs(db: Session, player_id: int, games: list[dict]) -> None:
    for g in games:
        merged = {**g, "player_id": player_id}
        db.merge(PitchingGameLog(**merged))


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
