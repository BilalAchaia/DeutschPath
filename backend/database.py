import os
import tempfile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# All mutable state (SQLite DB, uploaded books, usage stats) lives under DATA_DIR.
# Defaults to the backend directory for local development. In the container it is
# set to /data so it can be backed by a persistent volume/disk.
_DEFAULT_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
if os.getenv("VERCEL"):
    # Vercel's filesystem is read-only except /tmp (which is per-instance).
    # Book bytes live in the database, so /tmp is only used for scratch files.
    _DEFAULT_DATA_DIR = os.path.join(tempfile.gettempdir(), "deutschpath")
DATA_DIR = os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# SQLite fallback path (used only when DATABASE_URL is not provided).
DB_PATH = os.path.join(DATA_DIR, "deutschpath.db")

# Prefer an external database (e.g. Neon PostgreSQL) when DATABASE_URL is set.
# Otherwise fall back to a local SQLite file so local development needs zero setup.
_raw_url = os.getenv("DATABASE_URL", "").strip()
if _raw_url:
    # Heroku/older style "postgres://" is not understood by SQLAlchemy 2.x.
    if _raw_url.startswith("postgres://"):
        _raw_url = "postgresql://" + _raw_url[len("postgres://"):]
    # Pin the sync driver explicitly (psycopg2-binary) so the connection works
    # regardless of the installed SQLAlchemy version's default DBAPI.
    if _raw_url.startswith("postgresql://"):
        _raw_url = "postgresql+psycopg2://" + _raw_url[len("postgresql://"):]
    DATABASE_URL = _raw_url
    IS_SQLITE = DATABASE_URL.startswith("sqlite")
else:
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    IS_SQLITE = True

if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )
else:
    # External DB (Postgres/Neon). pool_pre_ping avoids stale connections that
    # serverless Postgres providers close while idle.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models import Base
    Base.metadata.create_all(bind=engine)
    if IS_SQLITE:
        _migrate_sqlite()
    _seed_if_empty()


def _migrate_sqlite():
    """Add new columns to existing SQLite tables if they're missing.

    SQLite-only: a fresh Postgres database is created with the full schema by
    ``create_all`` so no ad-hoc migration is required.
    """
    with engine.connect() as conn:
        from sqlalchemy import text

        existing_sc = {row[1] for row in conn.execute(text("PRAGMA table_info(scenarios)")).fetchall()}
        for col, typedef in [("subject", "TEXT"), ("scenario_type", "TEXT"), ("opening_message", "TEXT")]:
            if col not in existing_sc:
                conn.execute(text(f"ALTER TABLE scenarios ADD COLUMN {col} {typedef}"))

        existing_bk = {row[1] for row in conn.execute(text("PRAGMA table_info(books)")).fetchall()}
        if "file_type" not in existing_bk:
            conn.execute(text("ALTER TABLE books ADD COLUMN file_type TEXT DEFAULT 'pdf'"))

        existing_gr = {row[1] for row in conn.execute(text("PRAGMA table_info(grammar_rules)")).fetchall()}
        if "translations" not in existing_gr:
            conn.execute(text("ALTER TABLE grammar_rules ADD COLUMN translations TEXT DEFAULT '{}'"))

        existing_ann = {row[1] for row in conn.execute(text("PRAGMA table_info(annotations)")).fetchall()}
        if "mark_type" not in existing_ann:
            conn.execute(text("ALTER TABLE annotations ADD COLUMN mark_type TEXT DEFAULT 'text'"))
        if "font_size" not in existing_ann:
            conn.execute(text("ALTER TABLE annotations ADD COLUMN font_size INTEGER DEFAULT 12"))
        if "ann_width" not in existing_ann:
            conn.execute(text("ALTER TABLE annotations ADD COLUMN ann_width REAL"))
        if "ann_height" not in existing_ann:
            conn.execute(text("ALTER TABLE annotations ADD COLUMN ann_height REAL"))

        conn.commit()


def _seed_if_empty():
    """Insert missing seed records and update existing ones with new fields."""
    from models import GrammarRule, Scenario, WritingTopic
    from seed_data import GRAMMAR_RULES, SCENARIOS, WRITING_TOPICS

    db = SessionLocal()
    try:
        existing_rules = {r.name for r in db.query(GrammarRule.name).all()}
        for r in GRAMMAR_RULES:
            if r["name"] not in existing_rules:
                db.add(GrammarRule(**r))
        db.commit()

        existing_map = {s.name: s for s in db.query(Scenario).all()}
        for s in SCENARIOS:
            if s["name"] not in existing_map:
                db.add(Scenario(**s))
            else:
                # Update new fields on existing rows
                row = existing_map[s["name"]]
                for field in ("subject", "scenario_type", "opening_message"):
                    if field in s and getattr(row, field, None) is None:
                        setattr(row, field, s[field])
        db.commit()

        existing_topics = {t.id for t in db.query(WritingTopic.id).all()}
        for t in WRITING_TOPICS:
            if t["id"] not in existing_topics:
                db.add(WritingTopic(**t))
        db.commit()
    finally:
        db.close()
