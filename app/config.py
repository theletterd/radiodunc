from __future__ import annotations

import json
import os
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


class StationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="RadioDunc", min_length=1)
    tagline: str = Field(default="Your personal AI radio station.", min_length=1)
    format: str = Field(default="Eclectic", min_length=1)
    description: str | None = None
    era: str | None = None
    genre_focus: list[str] = Field(default_factory=list)
    core_artists: list[str] = Field(default_factory=list)
    dj_name: str = Field(default="DJ", min_length=1)
    dj_style: str = Field(default="warm and conversational", min_length=1)
    voice_hint: str | None = None

    @field_validator("name", "tagline", "format", "dj_name", "dj_style", mode="before")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("description", "era", "voice_hint", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        return stripped or None


class AppConfig(BaseModel):
    music_folder: str = "~/Music"
    tts_provider: str = "tone"
    script_provider: str = "template"
    openai_api_key: str | None = None
    openai_text_model: str = "gpt-4o-mini"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "verse"
    playlist_artist_repeat_window: int = Field(default=3, ge=0, le=50)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    station: StationConfig = Field(default_factory=StationConfig)

    @field_validator("tts_provider", mode="before")
    @classmethod
    def normalize_tts_provider(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        normalized = value.strip().lower()
        if normalized not in {"tone", "openai"}:
            raise ValueError("must be one of: tone, openai")
        return normalized

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_optional_secret_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        return stripped or None

    @field_validator("openai_text_model", "openai_tts_model", "openai_tts_voice", mode="before")
    @classmethod
    def normalize_model_id(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("script_provider", mode="before")
    @classmethod
    def normalize_script_provider(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        normalized = value.strip().lower()
        if normalized not in {"template", "openai"}:
            raise ValueError("must be one of: template, openai")
        return normalized


CONFIG_PATH = Path("radio_config.json")
EXAMPLE_CONFIG_PATH = Path("example-radio_config.json")
DOTENV_PATH = Path(".env")


def _load_dotenv_api_key() -> str | None:
    env_value = os.getenv("OPENAI_API_KEY")
    if env_value and env_value.strip():
        return env_value.strip()
    if not DOTENV_PATH.exists():
        return None
    for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "OPENAI_API_KEY":
            continue
        normalized = value.strip().strip("'").strip('"')
        return normalized or None
    return None


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
        env_api_key = _load_dotenv_api_key()
        if env_api_key:
            config = config.model_copy(update={"openai_api_key": env_api_key})
        return config

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config = _validate_and_format_config(str(CONFIG_PATH), data)
    env_api_key = _load_dotenv_api_key()
    if env_api_key:
        config = config.model_copy(update={"openai_api_key": env_api_key})
    return config


def save_config(config: AppConfig) -> None:
    serialized = config.model_dump()
    serialized.pop("openai_api_key", None)
    CONFIG_PATH.write_text(
        json.dumps(serialized, indent=2),
        encoding="utf-8",
    )
