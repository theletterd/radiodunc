from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class NewsPreferences(BaseModel):
    enabled: bool = True
    categories: list[str] = Field(default_factory=lambda: ["local", "national", "music"])
    briefing_minutes: int = 30


class AlertConfig(BaseModel):
    weather_location: str = "Seattle, WA"
    local_time_zone: str = "America/Los_Angeles"
    news: NewsPreferences = Field(default_factory=NewsPreferences)


class StationPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = Field(min_length=1)
    tagline: str = Field(min_length=1)
    dj_name_prefix: str = Field(default="DJ", min_length=1)
    dj_style: str = Field(min_length=1)
    voice_hint: str | None = None

    @field_validator("format", "tagline", "dj_name_prefix", "dj_style", mode="before")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("voice_hint", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        return stripped or None


DEFAULT_STATION_PRESETS: list[StationPreset] = [
    StationPreset(format="Indie Discovery", tagline="Fresh cuts and deep tracks.", dj_style="warm storyteller"),
    StationPreset(
        format="Classic Rock Drive", tagline="Legends on repeat, with attitude.", dj_style="high-energy throwback"
    ),
    StationPreset(format="Chill Evenings", tagline="Low-key vibes for late nights.", dj_style="calm minimalist"),
    StationPreset(format="Pop Pulse", tagline="Hooks, hits, and new obsessions.", dj_style="playful and fast-paced"),
    StationPreset(format="Eclectic Mixtape", tagline="No rules, only great songs.", dj_style="quirky curator"),
    StationPreset(format="Retro Rewind", tagline="Back when radio ruled the road.", dj_style="nostalgic host"),
    StationPreset(format="Alternative Edge", tagline="Sharp guitars and bold voices.", dj_style="witty and rebellious"),
    StationPreset(format="Late Night Vinyl", tagline="Analog soul for digital nights.", dj_style="intimate and poetic"),
]


class AppConfig(BaseModel):
    music_folder: str = "~/Music"
    station_generation_count: int = 6
    station_generation_seed: int | None = None
    playlist_artist_repeat_window: int = Field(default=3, ge=0, le=50)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    station_presets: list[StationPreset] = Field(default_factory=lambda: [preset.model_copy() for preset in DEFAULT_STATION_PRESETS])

    @field_validator("station_presets")
    @classmethod
    def validate_station_presets(cls, value: list[StationPreset]) -> list[StationPreset]:
        if not value:
            raise ValueError("must include at least one preset")
        return value


CONFIG_PATH = Path("radio_config.json")
EXAMPLE_CONFIG_PATH = Path("example-radio_config.json")


def _validate_and_format_config(source: str, raw_data: dict) -> AppConfig:
    try:
        return AppConfig.model_validate(raw_data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors())
        raise ValueError(f"Invalid config in {source}: {details}") from exc


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
            config = _validate_and_format_config(str(EXAMPLE_CONFIG_PATH), data)
        else:
            config = AppConfig()
        save_config(config)
        return config

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _validate_and_format_config(str(CONFIG_PATH), data)


def save_config(config: AppConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config.model_dump(), indent=2),
        encoding="utf-8",
    )
