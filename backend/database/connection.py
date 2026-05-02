import os
from contextlib import contextmanager

from sqlalchemy import create_engine
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


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    from .models import Base, Pitcher, Player
    if _engine is not None:
        Base.metadata.create_all(_engine)
        # create_all only runs on missing tables; create indexes explicitly so
        # they are added to already-existing deployments too.
        for tbl in (Player, Pitcher):
            for idx in tbl.__table__.indexes:
                idx.create(bind=_engine, checkfirst=True)
