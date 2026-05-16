from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Track
from app.scanner import MAX_TRACKS_PER_SCAN, scan_library


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return testing_session_local()


def test_scan_library_limits_imports(monkeypatch, tmp_path):
    db = _make_db_session()

    for idx in range(MAX_TRACKS_PER_SCAN + 5):
        (tmp_path / f"song-{idx}.mp3").write_bytes(b"mock")

    monkeypatch.setattr(
        "app.scanner._extract_track_metadata",
        lambda _path: {"title": "T", "artist": "A", "album": "B", "year": None, "genre": None, "duration_seconds": 1.0, "bitrate": 128000},
    )

    result = scan_library(str(tmp_path), db)

    assert result["imported"] == MAX_TRACKS_PER_SCAN
    assert result["limit_reached"] is True
    assert result["max_tracks_per_scan"] == MAX_TRACKS_PER_SCAN
    assert db.query(Track).count() == MAX_TRACKS_PER_SCAN
