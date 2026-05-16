import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.database import Base
from app.models import Station, Track
from app.scheduler import build_station_queue


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def test_build_station_queue_respects_artist_repeat_window_and_seed():
    db = _make_db_session()
    db.add_all(
        [
            Track(file_path="/music/1.mp3", artist="A"),
            Track(file_path="/music/2.mp3", artist="B"),
            Track(file_path="/music/3.mp3", artist="C"),
            Track(file_path="/music/4.mp3", artist="A"),
        ]
    )
    station = Station(name="Test FM", config_json=json.dumps({"core_artists": ["A", "B", "C"]}))
    db.add(station)
    db.commit()

    cfg = AppConfig(playlist_artist_repeat_window=2)
    out = build_station_queue(db, station.id, cfg, size=4, seed=7)

    assert out["queue_size"] == 4
    artists = [t.artist for t in out["tracks"]]
    for idx in range(1, len(artists)):
        assert artists[idx] != artists[idx - 1]


def test_build_station_queue_errors_for_missing_station_or_tracks():
    db = _make_db_session()
    with pytest.raises(ValueError, match="not found"):
        build_station_queue(db, 999, AppConfig(), size=3)

    station = Station(name="X FM")
    db.add(station)
    db.commit()

    with pytest.raises(ValueError, match="No tracks found"):
        build_station_queue(db, station.id, AppConfig(), size=3)
