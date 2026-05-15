from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class NewsPreferences(BaseModel):
    enabled: bool = True
    categories: list[str] = Field(default_factory=lambda: ["local", "national", "music"])
    briefing_minutes: int = 30


class AlertConfig(BaseModel):
    weather_location: str = "Seattle, WA"
    local_time_zone: str = "America/Los_Angeles"
    news: NewsPreferences = Field(default_factory=NewsPreferences)


class AppConfig(BaseModel):
    music_folder: str = "~/Music"
    station_generation_count: int = 6
    alerts: AlertConfig = Field(default_factory=AlertConfig)


CONFIG_PATH = Path("radio_config.json")


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        default = AppConfig()
        save_config(default)
        return default

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)


def save_config(config: AppConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config.model_dump(), indent=2),
        encoding="utf-8",
    )
