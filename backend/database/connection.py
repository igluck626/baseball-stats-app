import os
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

_DATABASE_URL = os.getenv("DATABASE_URL", "")

# Railway uses the postgres:// scheme; SQLAlchemy requires postgresql://
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)

_engine = None
_SessionFactory = None

if _DATABASE_URL:
    _engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    _SessionFactory = sessionmaker(bind=_engine)


def db_available() -> bool:
    return _engine is not None


@contextmanager
def get_session():
    if _SessionFactory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Bio columns added to existing players + pitchers tables in older deployments.
# Listed (name, SQL type) so we can ALTER TABLE without depending on the ORM.
# ---------------------------------------------------------------------------
_BIO_COLUMNS: list[tuple[str, str]] = [
    ("position",      "VARCHAR"),
    ("bats",          "VARCHAR"),
    ("throws",        "VARCHAR"),
    ("height",        "INTEGER"),
    ("weight",        "INTEGER"),
    ("birth_year",    "INTEGER"),
    ("birth_month",   "INTEGER"),
    ("birth_day",     "INTEGER"),
    ("birth_city",    "VARCHAR"),
    ("birth_state",   "VARCHAR"),
    ("birth_country", "VARCHAR"),
    ("debut",         "VARCHAR"),
    ("final_game",    "VARCHAR"),
]


_TEAM_SEASONS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("last_updated",         "TIMESTAMP"),
    # Live standings fields from the MLB Stats API. Boolean columns
    # use Postgres BOOLEAN; SQLite tolerates the same DDL string for
    # local tests since SQLAlchemy maps both.
    ("streak_code",          "VARCHAR"),
    ("last_ten_w",           "INTEGER"),
    ("last_ten_l",           "INTEGER"),
    ("home_w",               "INTEGER"),
    ("home_l",               "INTEGER"),
    ("away_w",               "INTEGER"),
    ("away_l",               "INTEGER"),
    ("games_back",           "VARCHAR"),
    ("wild_card_games_back", "VARCHAR"),
    ("clinch_indicator",     "VARCHAR"),
    ("division_leader",      "BOOLEAN"),
    ("clinched",             "BOOLEAN"),
    ("magic_number",         "VARCHAR"),
    ("elimination_number",   "VARCHAR"),
]

# Columns added to batting_gamelogs after the table's initial creation. The
# MLB Stats API exposes both, but the original schema didn't store them; the
# iOS game-logs table needs them for the IBB/CS columns and for cumulative
# OBP (which uses HBP/SF, already on the table).
_BATTING_GAMELOGS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("IBB", "INTEGER"),
    ("CS",  "INTEGER"),
]

# Extended counting stats added to player_seasons / pitcher_seasons in the
# stat-coverage expansion. Listed here so existing prod tables get them via
# ALTER TABLE on the next init_db() (lifespan / /admin/migrate / bulk-load).
_PLAYER_SEASONS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("IBB",  "INTEGER"),
    ("HBP",  "INTEGER"),
    ("SF",   "INTEGER"),
    ("SH",   "INTEGER"),
    ("GIDP", "INTEGER"),
    ("TB",   "INTEGER"),
]
_PITCHER_SEASONS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("CG",    "INTEGER"),
    ("SHO",   "INTEGER"),
    ("SV",    "INTEGER"),
    ("H",     "INTEGER"),
    ("ER",    "INTEGER"),
    ("R",     "INTEGER"),
    ("BAOpp", "FLOAT"),
    ("IBB",   "INTEGER"),
    ("WP",    "INTEGER"),
    ("HBP",   "INTEGER"),
    ("BK",    "INTEGER"),
    ("BFP",   "INTEGER"),
    ("GF",    "INTEGER"),
    ("SH",    "INTEGER"),
    ("SF",    "INTEGER"),
    ("GIDP",  "INTEGER"),
]


def _add_missing_columns(table_name: str, columns: list[tuple[str, str]]) -> list[str]:
    """ALTER TABLE ADD COLUMN for any of the given columns not yet present.

    Identifiers are double-quoted so Postgres preserves their exact case (it
    folds unquoted identifiers to lowercase, which produced the case-mismatch
    bug between buggy lowercase columns and the SQLAlchemy model's quoted
    names). The existence check is case-INSENSITIVE so we don't try to add
    "IBB" when a buggy lowercase "ibb" is already present — the rename
    migration handles those.

    Postgres uses ADD COLUMN IF NOT EXISTS (safe under concurrent migrations).
    Other dialects (SQLite for local tests) just rely on the case-insensitive
    pre-check. Returns the list of column names actually added.
    """
    inspector = inspect(_engine)
    if table_name not in inspector.get_table_names():
        return []
    existing_names = {c["name"] for c in inspector.get_columns(table_name)}
    existing_lower = {n.lower() for n in existing_names}
    dialect = _engine.dialect.name

    added: list[str] = []
    with _engine.begin() as conn:
        for col_name, col_type in columns:
            if col_name.lower() in existing_lower:
                continue
            if dialect == "postgresql":
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}'
                ))
            else:
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD COLUMN "{col_name}" {col_type}'
                ))
            added.append(col_name)
    return added


# ---------------------------------------------------------------------------
# One-time rename migration: fix the case-folding bug
# ---------------------------------------------------------------------------
# Earlier _add_missing_columns versions emitted unquoted ALTER TABLE
# statements, so Postgres folded the new columns to lowercase. The
# SQLAlchemy models expect proper case (e.g. "IBB"). This rename brings
# the physical schema back in line with the ORM.
_LOWERCASE_TO_PROPER_RENAMES: dict[str, list[tuple[str, str]]] = {
    "player_seasons": [
        ("ibb",  "IBB"),
        ("hbp",  "HBP"),
        ("sf",   "SF"),
        ("sh",   "SH"),
        ("gidp", "GIDP"),
    ],
    "pitcher_seasons": [
        ("cg",    "CG"),
        ("sho",   "SHO"),
        ("sv",    "SV"),
        ("h",     "H"),
        ("er",    "ER"),
        ("r",     "R"),
        ("baopp", "BAOpp"),
        ("ibb",   "IBB"),
        ("wp",    "WP"),
        ("hbp",   "HBP"),
        ("bk",    "BK"),
        ("bfp",   "BFP"),
        ("gf",    "GF"),
        ("sh",    "SH"),
        ("sf",    "SF"),
        ("gidp",  "GIDP"),
    ],
}


def rename_lowercase_columns() -> dict:
    """One-time fix: rename lowercase columns (created by the buggy unquoted
    ALTER) back to the proper case that the SQLAlchemy model expects.

    Idempotent and safe to re-run:
      • If the lowercase column is gone, skip (already renamed).
      • If both lowercase and proper-case columns exist, skip (manual review
        — shouldn't happen but worth flagging).
      • If only the lowercase column exists, RENAME COLUMN it.

    Returns {table → list of "old→new"} for what actually changed, plus a
    parallel "skipped" map explaining the no-ops.
    """
    summary: dict = {
        "renamed":           {},
        "skipped":           {},
        "skipped_no_engine": False,
    }
    if _engine is None:
        summary["skipped_no_engine"] = True
        return summary

    inspector = inspect(_engine)
    dialect = _engine.dialect.name

    for table_name, renames in _LOWERCASE_TO_PROPER_RENAMES.items():
        if table_name not in inspector.get_table_names():
            summary["skipped"].setdefault(table_name, []).append(
                f"(table {table_name!r} does not exist)"
            )
            continue
        existing = {c["name"] for c in inspector.get_columns(table_name)}
        renamed: list[str] = []
        skipped: list[str] = []

        with _engine.begin() as conn:
            for old, new in renames:
                old_present = old in existing
                new_present = new in existing
                if not old_present and not new_present:
                    skipped.append(f"{old}→{new} (neither column exists)")
                    continue
                if not old_present and new_present:
                    skipped.append(f"{old}→{new} (already proper case)")
                    continue
                if old_present and new_present:
                    skipped.append(
                        f"{old}→{new} (BOTH exist — manual review needed)"
                    )
                    continue
                # old_present and not new_present → safe to rename
                if dialect == "postgresql":
                    conn.execute(text(
                        f'ALTER TABLE {table_name} RENAME COLUMN {old} TO "{new}"'
                    ))
                else:
                    # SQLite: 3.25+ supports RENAME COLUMN; quoting works the same.
                    conn.execute(text(
                        f'ALTER TABLE {table_name} RENAME COLUMN {old} TO "{new}"'
                    ))
                renamed.append(f"{old}→{new}")

        if renamed:
            summary["renamed"][table_name] = renamed
        if skipped:
            summary["skipped"][table_name] = skipped

    return summary


def init_db() -> dict:
    """Create missing tables/indexes and add any new bio columns to existing
    players + pitchers tables. Returns a summary of what changed so callers
    (e.g. POST /admin/migrate) can report it.

    Safe to re-run — uses checkfirst=True for tables/indexes and ADD COLUMN
    IF NOT EXISTS (Postgres) / inspector-based skip (SQLite) for columns.
    """
    summary: dict = {
        "tables_created":    [],
        "columns_added":     {},
        "indexes_created":   [],
        "skipped_no_engine": False,
    }
    if _engine is None:
        summary["skipped_no_engine"] = True
        return summary

    from .models import (
        Base, BattingGameLog, Pitcher, PitchingGameLog, Player, TeamSeason,
    )

    # 1. Create missing tables. create_all already does checkfirst=True; we
    #    snapshot table names before/after to report what was actually created.
    inspector = inspect(_engine)
    before = set(inspector.get_table_names())
    Base.metadata.create_all(_engine, checkfirst=True)
    after = set(inspect(_engine).get_table_names())
    summary["tables_created"] = sorted(after - before)

    # 2. ALTER TABLE for any missing columns added in earlier expansions.
    for tbl_name in ("players", "pitchers"):
        added = _add_missing_columns(tbl_name, _BIO_COLUMNS)
        if added:
            summary["columns_added"][tbl_name] = added
    for tbl_name, cols in (
        ("team_seasons",      _TEAM_SEASONS_NEW_COLUMNS),
        ("player_seasons",    _PLAYER_SEASONS_NEW_COLUMNS),
        ("pitcher_seasons",   _PITCHER_SEASONS_NEW_COLUMNS),
        ("batting_gamelogs",  _BATTING_GAMELOGS_NEW_COLUMNS),
    ):
        added = _add_missing_columns(tbl_name, cols)
        if added:
            summary["columns_added"][tbl_name] = added

    # 3. Indexes on tables that were already present in older deployments.
    #    create_all only adds indexes for newly-created tables, so explicit
    #    create-with-checkfirst is needed for the rest.
    for tbl in (Player, Pitcher, TeamSeason, BattingGameLog, PitchingGameLog):
        for idx in tbl.__table__.indexes:
            idx.create(bind=_engine, checkfirst=True)
            summary["indexes_created"].append(idx.name)

    # 4. One-time backfills for derived columns. Each helper is
    #    idempotent — it only touches rows whose derived column is
    #    NULL but whose inputs are present. Safe to call on every
    #    deploy; once the historical rows are filled it's a no-op.
    tb_filled = _backfill_player_seasons_tb()
    if tb_filled:
        summary.setdefault("backfilled", {})["player_seasons.TB"] = tb_filled

    # 5. Historical (player_id, year) duplicate cleanup + composite PK
    #    enforcement. Pre-PK ingestions could write duplicate season
    #    rows for the same player-year; the leaderboard then surfaced
    #    them as repeat entries. Dedupe first (Postgres can't add a PK
    #    over an indexed-pair that has duplicates), then add the PK so
    #    future ingestion writes via INSERT ... ON CONFLICT can't
    #    reintroduce them.
    for tbl_name, qual_col in (("player_seasons", "PA"), ("pitcher_seasons", "IP")):
        removed = _dedupe_season_duplicates(tbl_name, qual_col)
        if removed:
            summary.setdefault("deduped", {})[tbl_name] = removed
        added_pk = _ensure_seasons_primary_key(tbl_name)
        if added_pk:
            summary.setdefault("pks_added", []).append(tbl_name)

    return summary


def _dedupe_season_duplicates(table: str, quality_col: str) -> int:
    """Delete duplicate (player_id, year) rows from `table`, keeping
    the row with the highest `quality_col` (PA for batters, IP for
    pitchers) — that's the most-complete stat row when an old ingest
    wrote both a per-stint and a season-total version. Ties broken
    deterministically by Postgres ctid so the operation is repeatable.

    Postgres-only — relies on ctid + ROW_NUMBER. SQLite skips
    (the local dev DB rarely accumulates duplicates and lacks
    the same physical-row identifier).
    """
    if _engine is None or _engine.dialect.name != "postgresql":
        return 0
    inspector = inspect(_engine)
    if table not in inspector.get_table_names():
        return 0
    columns = {c["name"] for c in inspector.get_columns(table)}
    if quality_col not in columns or "player_id" not in columns or "year" not in columns:
        return 0
    sql = text(f"""
        WITH ranked AS (
            SELECT ctid,
                   ROW_NUMBER() OVER (
                       PARTITION BY player_id, year
                       ORDER BY COALESCE("{quality_col}", -1) DESC, ctid ASC
                   ) AS rn
            FROM {table}
        )
        DELETE FROM {table} t
        USING ranked r
        WHERE t.ctid = r.ctid
          AND r.rn > 1
    """)
    with _engine.begin() as conn:
        result = conn.execute(sql)
        return result.rowcount or 0


def _ensure_seasons_primary_key(table: str) -> bool:
    """Make (player_id, year) the PK on `table` if it isn't already.
    Caller is responsible for deduping first — `ALTER TABLE … ADD
    PRIMARY KEY` fails on duplicate rows.

    Postgres-only. Returns True if a new PK was added, False when the
    correct PK was already present, the table doesn't exist, or the
    dialect doesn't support the migration. If a DIFFERENT PK is already
    in place we leave it alone — re-keying a populated table is too
    risky to do silently.
    """
    if _engine is None or _engine.dialect.name != "postgresql":
        return False
    inspector = inspect(_engine)
    if table not in inspector.get_table_names():
        return False
    pk = inspector.get_pk_constraint(table) or {}
    pk_cols = set(pk.get("constrained_columns") or [])
    if pk_cols == {"player_id", "year"}:
        return False
    if pk_cols:
        # An unexpected PK shape — bail loudly via the no-op return.
        # Operator intervention required; we're not going to drop a
        # PK we didn't put there.
        return False
    with _engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE {table} ADD PRIMARY KEY (player_id, year)'
        ))
    return True


def _backfill_player_seasons_tb() -> int:
    """Fill player_seasons.TB for rows where it's NULL but H is known.
    TB = H + 2·doubles + 3·triples + 4·HR (formula expressed with the
    canonical "singles + 2·2B + 3·3B + 4·HR" identity:
    H - 2B - 3B - HR + 2·2B + 3·3B + 4·HR = H + 2B + 2·3B + 3·HR).
    Returns the number of rows updated. Idempotent — re-runs do
    nothing once TB is populated everywhere.
    """
    if _engine is None:
        return 0
    inspector = inspect(_engine)
    if "player_seasons" not in inspector.get_table_names():
        return 0
    columns = {c["name"] for c in inspector.get_columns("player_seasons")}
    if "TB" not in columns:
        return 0
    # Postgres + SQLite both accept this UPDATE; quoting "TB" /
    # "doubles" / "triples" / "HR" is required so Postgres preserves
    # the mixed case (init_db ALTER TABLE writes them quoted, and the
    # SQLAlchemy model expects them that way).
    sql = text("""
        UPDATE player_seasons
           SET "TB" = COALESCE("H", 0)
                    + COALESCE("doubles", 0)
                    + 2 * COALESCE("triples", 0)
                    + 3 * COALESCE("HR", 0)
         WHERE "TB" IS NULL
           AND "H" IS NOT NULL
    """)
    with _engine.begin() as conn:
        result = conn.execute(sql)
        return result.rowcount or 0
