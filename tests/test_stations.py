import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.database import Base
from app.models import Station, Track
from app.stations import generate_stations


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def test_generate_stations_requires_tracks():
    db = _make_db_session()
    config = AppConfig()

    with pytest.raises(ValueError, match="No tracks found"):
        generate_stations(db, config)


def test_generate_stations_uses_seeded_order_and_persists_config_json():
    db = _make_db_session()
    db.add_all(
        [
            Track(file_path="/music/a.mp3", artist="Artist A", genre="Rock"),
            Track(file_path="/music/b.mp3", artist="Artist B", genre="Rock"),
            Track(file_path="/music/c.mp3", artist="Artist C", genre="Pop"),
        ]
    )
    db.commit()

    config = AppConfig(station_generation_seed=42, station_generation_count=3)
    result = generate_stations(db, config)

    assert result["generated"] == 3
    assert result["seed"] == 42

    stations = db.query(Station).all()
    assert len(stations) == 3

    parsed = json.loads(stations[0].config_json)
    assert parsed["weather_location"] == config.alerts.weather_location
    assert parsed["core_artists"]
