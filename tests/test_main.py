from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.database import Base
from app.main import (
    generate_stations_endpoint,
    get_config,
    healthcheck,
    list_stations,
    list_tracks,
    scan_library_endpoint,
    update_config,
)
from app.models import Station, Track
from app.schemas import LibraryScanRequest, QueueGenerateRequest, StationGenerateRequest


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def test_healthcheck_returns_ok():
    assert healthcheck() == {"status": "ok"}


def test_get_and_put_config_round_trip(monkeypatch):
    cfg = AppConfig(music_folder="/tmp/music")
    saved = {}

    monkeypatch.setattr("app.main.load_config", lambda: cfg)

    def fake_save_config(incoming: AppConfig):
        saved["config"] = incoming

    monkeypatch.setattr("app.main.save_config", fake_save_config)

    returned = get_config()
    assert returned.music_folder == "/tmp/music"

    updated = cfg.model_copy(update={"music_folder": "/new/music"})
    result = update_config(updated)

    assert result.music_folder == "/new/music"
    assert saved["config"].music_folder == "/new/music"


def test_scan_library_endpoint_uses_payload_folder(monkeypatch):
    db = _make_db_session()

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))

    def fake_scan_library(folder_path, _db):
        assert folder_path == "/explicit"
        return {"scanned": 1, "imported": 1, "skipped_duplicates": 0, "errors": []}

    monkeypatch.setattr("app.main.scan_library", fake_scan_library)

    response = scan_library_endpoint(LibraryScanRequest(folder_path="/explicit"), db)

    assert response["folder_path"] == "/explicit"


def test_scan_library_endpoint_maps_file_not_found_to_400(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))
    monkeypatch.setattr("app.main.scan_library", lambda _folder_path, _db: (_ for _ in ()).throw(FileNotFoundError("missing folder")))

    with pytest.raises(HTTPException) as exc:
        scan_library_endpoint(LibraryScanRequest(), db)

    assert exc.value.status_code == 400
    assert exc.value.detail == "missing folder"


def test_scan_library_endpoint_maps_unexpected_error_to_500(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))
    monkeypatch.setattr("app.main.scan_library", lambda _folder_path, _db: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(HTTPException) as exc:
        scan_library_endpoint(LibraryScanRequest(), db)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to scan library: boom"


def test_tracks_endpoint_orders_results():
    db = _make_db_session()
    db.add_all(
        [
            Track(file_path="/m/2.mp3", title="Song B", artist="B", album="Z"),
            Track(file_path="/m/1.mp3", title="Song A", artist="A", album="Y"),
        ]
    )
    db.commit()

    results = list_tracks(db)

    assert [row.artist for row in results] == ["A", "B"]


def test_generate_stations_endpoint_success_and_value_error(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())

    monkeypatch.setattr(
        "app.main.generate_stations",
        lambda _db, _config, count: {"generated": count, "library_summary": {"track_count": 10}, "seed": None},
    )
    ok = generate_stations_endpoint(StationGenerateRequest(count=2), db)
    assert ok["generated"] == 2

    def raises_value_error(_db, _config, _count):
        raise ValueError("No tracks found")

    monkeypatch.setattr("app.main.generate_stations", raises_value_error)
    with pytest.raises(HTTPException) as exc:
        generate_stations_endpoint(StationGenerateRequest(count=2), db)
    assert exc.value.status_code == 400


def test_stations_endpoint_orders_by_created_at_desc():
    db = _make_db_session()
    now = datetime.utcnow()
    db.add_all(
        [
            Station(name="Older", created_at=now - timedelta(minutes=5)),
            Station(name="Newer", created_at=now),
        ]
    )
    db.commit()

    results = list_stations(db)

    assert [row.name for row in results] == ["Newer", "Older"]


def test_generate_station_queue_endpoint_maps_value_error(monkeypatch):
    db = _make_db_session()

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())

    def fake_queue(**_kwargs):
        return {
            "station_id": 1,
            "station_name": "X FM",
            "queue_size": 1,
            "seed": None,
            "artist_repeat_window": 2,
            "used_station_alignment": False,
            "tracks": [],
        }

    from app.main import generate_station_queue

    monkeypatch.setattr("app.main.build_station_queue", fake_queue)
    ok = generate_station_queue(1, QueueGenerateRequest(size=1), db)
    assert ok["station_id"] == 1

    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(HTTPException) as exc:
        generate_station_queue(1, QueueGenerateRequest(size=1), db)
    assert exc.value.status_code == 400
