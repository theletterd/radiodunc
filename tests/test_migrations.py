"""Tests for app/migrations.py — inline startup DB migrations."""

def _seed_clip(session, script_hash: str, audio_path: str) -> None:
    """Insert a DJClip via the ORM so server-side defaults (created_at,
    is_ad) fill in automatically."""
    from app.models import DJClip
    session.add(DJClip(
        script_text="x", audio_path=audio_path,
        voice="default", script_hash=script_hash,
    ))
    session.commit()


def test_migrate_dj_clip_paths_rewrites_generated_audio_to_generated(tmp_path, monkeypatch):
    """PR #159 renamed the cache dir on disk but left dj_clips.audio_path
    rows pointing at the old prefix. The startup migration rewrites them
    so DJClip serve-time path resolution finds the files."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        for hsh, path in [
            ("stalehash1", "generated_audio/station_ids/aaa.mp3"),
            ("stalehash2", "generated_audio/transitions/bbb.mp3"),
            ("freshhash1", "generated/station_ids/ccc.mp3"),       # already migrated
            ("absolutepath", "/Volumes/external/music/track.mp3"),  # untouched
        ]:
            _seed_clip(session, hsh, path)
    finally:
        session.close()

    monkeypatch.setattr("app.migrations.engine", engine)
    from app.migrations import migrate_dj_clip_paths_after_generated_rename
    migrate_dj_clip_paths_after_generated_rename()

    paths_by_hash = {
        row[0]: row[1]
        for row in engine.connect().execute(
            text("SELECT script_hash, audio_path FROM dj_clips")
        )
    }
    # Stale rows rewritten with the new prefix; subdir + filename preserved.
    assert paths_by_hash["stalehash1"] == "generated/station_ids/aaa.mp3"
    assert paths_by_hash["stalehash2"] == "generated/transitions/bbb.mp3"
    # Already-migrated row untouched (no double-prefix).
    assert paths_by_hash["freshhash1"] == "generated/station_ids/ccc.mp3"
    # Untouched: paths that aren't under generated_audio/ at all.
    assert paths_by_hash["absolutepath"] == "/Volumes/external/music/track.mp3"


def test_migrate_dj_clip_paths_is_idempotent(tmp_path, monkeypatch):
    """Re-running the migration on a DB where everything's already at
    generated/ should be a no-op (no double-rewrites, no errors)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    db_path = tmp_path / "idempotent.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        _seed_clip(session, "h", "generated/transitions/already.mp3")
    finally:
        session.close()

    monkeypatch.setattr("app.migrations.engine", engine)
    from app.migrations import migrate_dj_clip_paths_after_generated_rename
    migrate_dj_clip_paths_after_generated_rename()
    migrate_dj_clip_paths_after_generated_rename()  # twice for good measure

    row = engine.connect().execute(text("SELECT audio_path FROM dj_clips")).fetchone()
    assert row[0] == "generated/transitions/already.mp3"
