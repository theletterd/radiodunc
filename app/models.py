from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    file_path: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    artist: Mapped[str | None] = mapped_column(String, nullable=True)
    album: Mapped[str | None] = mapped_column(String, nullable=True)
    year: Mapped[str | None] = mapped_column(String, nullable=True)
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DJClip(Base):
    __tablename__ = "dj_clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_path: Mapped[str] = mapped_column(String, nullable=False)
    voice: Mapped[str | None] = mapped_column(String, nullable=True)
    script_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    is_ad: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PlayerState(Base):
    __tablename__ = "player_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    current_track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_playing: Mapped[bool] = mapped_column(default=False, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    queue_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
