from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Track
from app.scanner import SCAN_COMMIT_CHUNK_SIZE, scan_library


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return testing_session_local()


def _make_shared_db_sessionmaker():
    """A sessionmaker bound to a shared on-disk-ish in-memory DB, so multiple
    sessions opened by the same test see each other's commits. Used by the
    resume-after-crash test, which needs to discard the crashed session
    (mimicking a real process restart) and open a fresh one against the same
    data."""
    # shared cache makes all connections see the same in-memory DB.
    engine = create_engine(
        "sqlite:///file:memdb_scanner_resume?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


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


def test_scan_partial_progress_survives_midstream_crash(monkeypatch, tmp_path):
    """The whole point of the chunked-commits change: if scanning crashes
    halfway through a big library, the tracks committed so far stay in the DB
    instead of being lost in a never-committed transaction. A re-run picks up
    from there because the duplicate-check query sees the committed rows.

    Uses KeyboardInterrupt (a BaseException) to simulate the failure because
    the scanner's per-file `except Exception` swallows ordinary errors as
    per-file `errors[]` entries — it deliberately keeps going on a bad file.
    A real "user killed the scan" / Ctrl-C / parent-process death is
    BaseException-level, which bubbles out the way we want.
    """
    db = _make_db_session()

    file_count = 1000  # 5 full chunks at SCAN_COMMIT_CHUNK_SIZE=200
    for idx in range(file_count):
        (tmp_path / f"song-{idx:04d}.mp3").write_bytes(b"mock")

    # Crash at the 450th extraction → 2 complete chunks of 200 already
    # committed, plus 49 adds in the current chunk that get discarded
    # when the exception bubbles before the next chunk-boundary commit.
    call_count = {"n": 0}
    def flaky_extract(_path):
        call_count["n"] += 1
        if call_count["n"] >= 450:
            raise KeyboardInterrupt("simulated mid-scan kill")
        return {"title": "T", "artist": "A", "album": "B", "year": None,
                "genre": None, "duration_seconds": 1.0, "bitrate": 128000}
    monkeypatch.setattr("app.scanner._extract_track_metadata", flaky_extract)

    with pytest.raises(KeyboardInterrupt):
        scan_library(str(tmp_path), db)

    # Two complete chunks (2 × SCAN_COMMIT_CHUNK_SIZE = 400) made it to disk.
    # The 49 tracks in the in-flight chunk are lost — that's the expected
    # granularity of the chunked-commits approach. A pre-chunked-commits
    # version of this test would have shown 0 here (single final commit,
    # never reached because of the crash).
    committed_count = db.query(Track).count()
    assert committed_count == 2 * SCAN_COMMIT_CHUNK_SIZE


def test_scan_resumes_correctly_after_partial_failure(monkeypatch, tmp_path):
    """End-to-end of the recovery story: after a mid-scan crash leaves N
    tracks committed, a second scan of the same folder picks up where the
    first one left off — the duplicate-check sees the committed rows.

    Uses two distinct sessions over a shared in-memory DB to mimic a real
    process restart (crash → terminate → relaunch → new session). Reusing
    the crashed session would leave its 49 uncommitted-chunk-2 adds floating
    in the identity map, colliding on the resume."""
    SessionMaker = _make_shared_db_sessionmaker()
    file_count = 500
    for idx in range(file_count):
        (tmp_path / f"song-{idx:04d}.mp3").write_bytes(b"mock")

    # First scan: crash at the 250th extraction (one chunk fully committed).
    call_count = {"n": 0}
    def flaky_extract(_path):
        call_count["n"] += 1
        if call_count["n"] >= 250:
            raise KeyboardInterrupt("simulated kill")
        return {"title": "T", "artist": "A", "album": "B", "year": None,
                "genre": None, "duration_seconds": 1.0, "bitrate": 128000}
    monkeypatch.setattr("app.scanner._extract_track_metadata", flaky_extract)

    db1 = SessionMaker()
    try:
        with pytest.raises(KeyboardInterrupt):
            scan_library(str(tmp_path), db1)
    finally:
        db1.close()

    # Fresh session — equivalent to "the user re-ran the app after the crash".
    db2 = SessionMaker()
    assert db2.query(Track).count() == SCAN_COMMIT_CHUNK_SIZE  # 200 survived

    # Second scan: extraction succeeds. The duplicate-check skips the 200
    # already-committed files and imports the remaining 300.
    def good_extract(_path):
        return {"title": "T", "artist": "A", "album": "B", "year": None,
                "genre": None, "duration_seconds": 1.0, "bitrate": 128000}
    monkeypatch.setattr("app.scanner._extract_track_metadata", good_extract)

    result = scan_library(str(tmp_path), db2)
    assert result["imported"] == file_count - SCAN_COMMIT_CHUNK_SIZE  # 300 new
    assert result["skipped_duplicates"] == SCAN_COMMIT_CHUNK_SIZE     # 200 skipped
    assert db2.query(Track).count() == file_count                      # 500 total
    db2.close()


def test_scan_commit_chunk_size_is_reasonable():
    """Sanity check on the tunable. Worth locking in so a future "make it 1"
    or "make it 100,000" change has to also explain itself by updating
    this test."""
    assert 50 <= SCAN_COMMIT_CHUNK_SIZE <= 1000
