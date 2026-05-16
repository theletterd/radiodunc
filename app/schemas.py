from pydantic import BaseModel, Field


class LibraryScanRequest(BaseModel):
    folder_path: str | None = Field(None, description="Absolute or relative folder path to scan")


class TrackOut(BaseModel):
    id: int
    file_path: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: str | None = None
    genre: str | None = None
    duration_seconds: float | None = None
    bitrate: int | None = None

    class Config:
        from_attributes = True


class StationGenerateRequest(BaseModel):
    count: int | None = Field(None, ge=1, le=10)


class StationOut(BaseModel):
    id: int
    name: str
    tagline: str | None = None
    format: str | None = None
    description: str | None = None
    dj_name: str | None = None
    dj_style: str | None = None
    config_json: str | None = None

    class Config:
        from_attributes = True


class QueueGenerateRequest(BaseModel):
    size: int = Field(default=12, ge=1, le=200)
    seed: int | None = None


class QueueResponse(BaseModel):
    station_id: int
    station_name: str
    queue_size: int
    seed: int | None = None
    artist_repeat_window: int
    used_station_alignment: bool
    tracks: list[TrackOut]


class PlayerStateUpdateRequest(BaseModel):
    station_id: int | None = None
    is_playing: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


class PlayerStateResponse(BaseModel):
    station_id: int | None = None
    is_playing: bool
    volume: int
    station: StationOut | None = None
    favorites: list[int]
    recent_station_ids: list[int]
    current_track: TrackOut | None = None
    queue_depth: int = 0
    queue_position: int = 0
    now_playing_type: str | None = None
    now_playing_label: str | None = None
    last_error: str | None = None


class PlayerPlayRequest(BaseModel):
    station_id: int
    queue_size: int = Field(default=12, ge=1, le=200)
    seed: int | None = None


class PlayerActionResponse(BaseModel):
    state: PlayerStateResponse
    action: str


class FavoriteStationRequest(BaseModel):
    favorite: bool


class DJScriptGenerateRequest(BaseModel):
    previous_track_id: int | None = None
    next_track_id: int | None = None
    include_weather: bool = False
    include_news: bool = False
    include_fake_ad: bool = False
    max_sentences: int = Field(default=3, ge=1, le=3)


class DJScriptResponse(BaseModel):
    station_id: int
    station_name: str
    dj_name: str
    sentences: list[str]
    script_text: str


class DJClipSynthesizeRequest(BaseModel):
    script_text: str = Field(min_length=1)
    voice: str | None = None


class DJClipResponse(BaseModel):
    clip_id: int
    audio_path: str
    voice: str | None = None
    cached: bool


class BroadcastStatusResponse(BaseModel):
    running: bool
    station_id: int | None = None
    stream_url: str
    started_at_epoch: float | None = None
    last_error: str | None = None
