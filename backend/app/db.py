from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings, ROOT


class Base(DeclarativeBase):
    pass


def build_engine(url: str):
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        path = url.removeprefix("sqlite:///")
        from pathlib import Path
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = ROOT / resolved
        resolved.parent.mkdir(parents=True, exist_ok=True)
        url = "sqlite:///" + resolved.as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 20} if url.startswith("sqlite") else {}, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
    return engine


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
