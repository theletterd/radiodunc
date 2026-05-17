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


class StationOut(BaseModel):
    name: str
    tagline: str
    format: str
    description: str | None = None
    era: str | None = None
    genre_focus: list[str] = Field(default_factory=list)
    dj_name: str
    dj_style: str


class QueueResponse(BaseModel):
    station_name: str
    queue_size: int
    seed: int | None = None
    artist_repeat_window: int
    used_station_alignment: bool
    tracks: list[TrackOut]


class PlayerStateUpdateRequest(BaseModel):
    is_playing: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


class PlayerStateResponse(BaseModel):
    is_playing: bool
    volume: int
    station: StationOut
    current_track: TrackOut | None = None
    queue_depth: int = 0
    queue_position: int = 0
    now_playing_type: str | None = None
    now_playing_label: str | None = None
    last_error: str | None = None


class PlayerPlayRequest(BaseModel):
    queue_size: int = Field(default=12, ge=1, le=200)
    seed: int | None = None


class PlayerNextRequest(BaseModel):
    reason: str | None = Field(
        default=None,
        description="Optional hint about why next was triggered: 'skip' (user clicked Next) or 'auto' (track ended).",
    )


class PlayerActionResponse(BaseModel):
    state: PlayerStateResponse
    action: str


class DJScriptGenerateRequest(BaseModel):
    previous_track_id: int | None = None
    next_track_id: int | None = None
    include_weather: bool = False
    include_news: bool = False
    include_fake_ad: bool = False
    max_sentences: int = Field(default=3, ge=1, le=3)
    reason: str | None = None  # "skip" = user skipped previous track; "auto" or None = natural end


class DJScriptResponse(BaseModel):
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


class PlayerNextResponse(BaseModel):
    current_track_url: str
    dj_clip_url: str
    ad_clip_url: str | None = None  # plays right after the DJ clip when an ad fires
    ad_script: str | None = None
    next_track_url: str | None = None
    next_track_metadata: TrackOut | None = None
    dj_script: str


class QueueItemOut(BaseModel):
    position: int
    track_id: int
    label: str


class QueuePreviewResponse(BaseModel):
    items: list[QueueItemOut]
    queue_position: int
    queue_depth: int


class QueueInjectRequest(BaseModel):
    track_id: int


class QueueInjectResponse(BaseModel):
    position: int
    label: str
    queue_depth: int
