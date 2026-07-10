import datetime

from sqlalchemy import or_
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
    PitcherSeasonStint,
    PlayerPostseasonBatting,
    PlayerPostseasonPitching,
    PlayerSeason,
    PlayerSeasonStint,
    SeriesPost,
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
    surfaced "Sosa 1998" three times in the All-Time HR leaderboard.

    Stamps last_updated=utcnow() on every saved row so the iOS
    live-stats overlay can compare game start times against this
    timestamp to decide whether a game is already in the DB."""
    now = datetime.datetime.utcnow()
    for season in seasons:
        _upsert_season(db, PlayerSeason, {
            "player_id":    player_id,
            "last_updated": now,
            **season,
        })


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
    (player_id, year) pairs even under parallel ingest. last_updated
    is stamped on every saved row (parallel to save_player_seasons)
    for the live-stats overlay comparison."""
    now = datetime.datetime.utcnow()
    for season in seasons:
        _upsert_season(db, PitcherSeason, {
            "player_id":    player_id,
            "last_updated": now,
            **season,
        })


def _upsert_stint(db: Session, model, row: dict) -> None:
    """Insert-or-update one per-team stint row keyed on the composite PK
    (player_id, year, team). Same PG-native ON CONFLICT path as
    _upsert_season, but with the three-column conflict target. `row` must
    contain player_id, year, and team; every other key becomes the SET."""
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = pg_insert(model).values(**row)
        update_cols = {
            k: stmt.excluded[k]
            for k in row.keys()
            if k not in ("player_id", "year", "team")
        }
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=["player_id", "year", "team"],
                set_=update_cols,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["player_id", "year", "team"],
            )
        db.execute(stmt)
    else:
        db.merge(model(**row))


def save_player_season_stints(db: Session, stints: list[dict]) -> None:
    """Upsert per-team batting stint rows (one per player-year-team).
    Each dict carries its own player_id/year/team. last_updated stamped
    like the season saves."""
    now = datetime.datetime.utcnow()
    for stint in stints:
        _upsert_stint(db, PlayerSeasonStint, {"last_updated": now, **stint})


def save_pitcher_season_stints(db: Session, stints: list[dict]) -> None:
    """Upsert per-team pitching stint rows (one per player-year-team)."""
    now = datetime.datetime.utcnow()
    for stint in stints:
        _upsert_stint(db, PitcherSeasonStint, {"last_updated": now, **stint})


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


def get_award_share_combos(db: Session, award_ids) -> list[tuple[str, int, str]]:
    """Distinct (award_id, year, league) triples that have voting data,
    restricted to `award_ids`. Lightweight metadata powering the
    `/awards/available` picker — caller groups these into per-award /
    per-year league sets."""
    rows = (
        db.query(PlayerAwardShare.award_id,
                 PlayerAwardShare.year,
                 PlayerAwardShare.league)
        .filter(PlayerAwardShare.award_id.in_(award_ids))
        .distinct()
        .all()
    )
    return [(r.award_id, r.year, r.league) for r in rows]


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


def bulk_insert_gamelogs(db: Session, model, rows: list[dict]) -> int:
    """Fast batched INSERT ... ON CONFLICT (player_id, game_id) DO NOTHING for
    gamelog rows (each dict carries its own player_id + game_id). Used by the
    Retrosheet historical backfill, which writes millions of rows and can't
    afford the per-row SELECT of db.merge(). Existing rows (e.g. a re-run, or
    the 2000+ BDL/MLB rows) are left untouched — DO NOTHING, never overwrite.
    SQLite falls back to per-row merge. Returns the number of rows submitted."""
    if not rows:
        return 0
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        db.execute(
            pg_insert(model).values(rows).on_conflict_do_nothing(
                index_elements=["player_id", "game_id"],
            )
        )
    else:
        for r in rows:
            db.merge(model(**r))
    return len(rows)


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


# ---------------------------------------------------------------------------
# Series post (postseason series outcomes — Lahman SeriesPost.csv)
# ---------------------------------------------------------------------------

def save_series_post(db: Session, rows: list[dict]) -> None:
    """Upsert series-post rows by (year, round, team_id_winner).
    PostgreSQL takes the native `ON CONFLICT ... DO UPDATE` form
    keyed off the `uq_series_post` unique constraint so a re-run of
    the loader (or `/admin/load-series-post`) cleanly overwrites
    wins/losses/ties in place. SQLite falls back to a select-then-
    update-or-insert pattern because `db.merge` needs a primary-key
    match — the autoincrement `id` PK isn't known at the application
    side until the row is already saved, so merge can't help here.
    """
    if not rows:
        return
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        for r in rows:
            stmt = pg_insert(SeriesPost).values(**r)
            update_cols = {
                k: stmt.excluded[k]
                for k in r.keys()
                if k not in ("year", "round", "team_id_winner")
            }
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_series_post",
                    set_=update_cols,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(constraint="uq_series_post")
            db.execute(stmt)
    else:
        for r in rows:
            existing = (
                db.query(SeriesPost)
                  .filter(SeriesPost.year           == r["year"],
                          SeriesPost.round          == r["round"],
                          SeriesPost.team_id_winner == r["team_id_winner"])
                  .first()
            )
            if existing is None:
                db.add(SeriesPost(**r))
            else:
                for k, v in r.items():
                    if k not in ("year", "round", "team_id_winner"):
                        setattr(existing, k, v)


def get_series_post_by_team(
    db: Session, franch_id: str, year_from: int | None = None,
) -> list[SeriesPost]:
    """Return all postseason series involving any team that's
    historically been part of `franch_id`'s franchise — either as
    winner or loser. Resolves the team_id list off `team_seasons`
    so a relocation/rename (e.g. MON → WSN) surfaces both sides of
    the franchise's history in one call. Sorted by year desc, then
    round."""
    team_ids = [
        r.team_id for r in
        db.query(TeamSeason.team_id)
          .filter(TeamSeason.franch_id == franch_id)
          .distinct()
          .all()
    ]
    if not team_ids:
        return []
    q = (
        db.query(SeriesPost)
          .filter(or_(
              SeriesPost.team_id_winner.in_(team_ids),
              SeriesPost.team_id_loser.in_(team_ids),
          ))
    )
    if year_from is not None:
        q = q.filter(SeriesPost.year >= year_from)
    return q.order_by(SeriesPost.year.desc(), SeriesPost.round).all()


def get_series_post_years(db: Session) -> list[int]:
    """Distinct years that have postseason series, newest first. Drives the
    Playoff History year picker — years with no postseason (1904, 1994, …) are
    simply absent."""
    rows = (
        db.query(SeriesPost.year)
          .distinct()
          .order_by(SeriesPost.year.desc())
          .all()
    )
    return [r.year for r in rows]


def get_series_post_by_year(db: Session, year: int) -> list[SeriesPost]:
    """All postseason series for one year, both leagues. Ordered by round so a
    multi-round year reads in a stable order; the caller maps each raw round
    code to a bracket slot."""
    return (
        db.query(SeriesPost)
          .filter(SeriesPost.year == year)
          .order_by(SeriesPost.round)
          .all()
    )


def get_world_series_results(db: Session) -> list[SeriesPost]:
    """Every World Series result (round == 'WS'), newest year first. Powers the
    Playoff History champions list. Pre-1969 the WS row is simply the year's
    champion; older WS-equivalent rows that carry the 'WS' code are included."""
    return (
        db.query(SeriesPost)
          .filter(SeriesPost.round == "WS")
          .order_by(SeriesPost.year.desc())
          .all()
    )
