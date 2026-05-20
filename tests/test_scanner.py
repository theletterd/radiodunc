from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Track
from app.scanner import scan_library


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return testing_session_local()


def test_scan_library_imports_every_file_no_cap(monkeypatch, tmp_path):
    """The old MAX_TRACKS_PER_SCAN cap was a placeholder safety net from
    development; real libraries routinely exceed it. Scans now run to
    completion regardless of size."""
    db = _make_db_session()

    file_count = 1500  # comfortably past the old 1000 cap
    for idx in range(file_count):
        (tmp_path / f"song-{idx}.mp3").write_bytes(b"mock")

    monkeypatch.setattr(
        "app.scanner._extract_track_metadata",
        lambda _path: {"title": "T", "artist": "A", "album": "B", "year": None, "genre": None, "duration_seconds": 1.0, "bitrate": 128000},
    )

    result = scan_library(str(tmp_path), db)

    assert result["imported"] == file_count
    assert result["scanned"] == file_count
    assert db.query(Track).count() == file_count
    # The old result fields are gone — keep this assertion so a future revert
    # of the cap-removal would loudly fail this test.
    assert "limit_reached" not in result
    assert "max_tracks_per_scan" not in result
