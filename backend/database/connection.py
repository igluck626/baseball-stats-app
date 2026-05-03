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


def _add_missing_columns(table_name: str) -> list[str]:
    """ALTER TABLE ADD COLUMN for any bio columns not yet present.

    Postgres uses ADD COLUMN IF NOT EXISTS (safe under concurrent migrations).
    Other dialects (SQLite for local tests) check the inspector and skip rather
    than relying on dialect-specific syntax. Returns the list of column names
    added in this call.
    """
    inspector = inspect(_engine)
    if table_name not in inspector.get_table_names():
        return []
    existing = {c["name"] for c in inspector.get_columns(table_name)}
    dialect = _engine.dialect.name

    added: list[str] = []
    with _engine.begin() as conn:
        for col_name, col_type in _BIO_COLUMNS:
            if col_name in existing:
                continue
            if dialect == "postgresql":
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_type}'
                ))
            else:
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                ))
            added.append(col_name)
    return added


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

    from .models import Base, Pitcher, Player, TeamSeason

    # 1. Create missing tables. create_all already does checkfirst=True; we
    #    snapshot table names before/after to report what was actually created.
    inspector = inspect(_engine)
    before = set(inspector.get_table_names())
    Base.metadata.create_all(_engine, checkfirst=True)
    after = set(inspect(_engine).get_table_names())
    summary["tables_created"] = sorted(after - before)

    # 2. ALTER TABLE for any missing bio columns on the (now-confirmed-to-exist)
    #    players + pitchers tables.
    for tbl_name in ("players", "pitchers"):
        added = _add_missing_columns(tbl_name)
        if added:
            summary["columns_added"][tbl_name] = added

    # 3. Indexes on tables that were already present in older deployments.
    #    create_all only adds indexes for newly-created tables, so explicit
    #    create-with-checkfirst is needed for the rest.
    for tbl in (Player, Pitcher, TeamSeason):
        for idx in tbl.__table__.indexes:
            idx.create(bind=_engine, checkfirst=True)
            summary["indexes_created"].append(idx.name)

    return summary
