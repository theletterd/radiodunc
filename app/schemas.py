from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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

    model_config = ConfigDict(from_attributes=True)


class StationOut(BaseModel):
    name: str
    tagline: str
    format: str
    description: str | None = None
    era: str | None = None
    genre_focus: list[str] = Field(default_factory=list)
    dj_name: str
    personality: str
    # UUID of the DJ currently on air per the resolver, or None when the
    # Default DJ is hosting (no Show matches now, or the matching Show has
    # dj_id=None). The client uses this to build the on-air avatar URL —
    # active_station() flattens the DJ identity into dj_name/personality
    # overrides but loses the id, so we surface it separately here.
    active_dj_id: str | None = None


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
    ad_break_follows: bool = False
    news_break_follows: bool = False
    max_sentences: int = Field(default=3, ge=1, le=3)
    reason: str | None = None  # "skip" = user skipped; "request" = audience request; "auto"/None = natural end


class TTSPreviewRequest(BaseModel):
    """Audition a voice+instructions combo with arbitrary text. Used by the UI's
    config editor to let users hear voice changes immediately rather than waiting
    for the next on-air transition."""
    text: str = Field(min_length=1, max_length=300)
    voice: str | None = None
    voice_instructions: str | None = None


class TTSPreviewResponse(BaseModel):
    clip_url: str


class DJScriptResponse(BaseModel):
    station_name: str
    dj_name: str
    sentences: list[str]
    script_text: str


class PlayerNextResponse(BaseModel):
    current_track_url: str
    current_track_metadata: TrackOut  # the new current track (what's playing after the transition)
    current_track_label: str          # display label (artist - title or filename fallback)
    dj_clip_url: str
    ad_clip_url: str | None = None    # plays right after the DJ clip when an ad fires
    ad_script: str | None = None
    news_clip_url: str | None = None  # plays right after the DJ clip when news fires (before ads if both)
    news_script: str | None = None
    station_id_clip_url: str | None = None  # short stinger played after an ad break
    next_track_url: str | None = None
    next_track_metadata: TrackOut | None = None  # look-ahead (N+2) for prefetch hints
    dj_script: str


class StingerUrlResponse(BaseModel):
    clip_url: str | None = None


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
    # "next" — insert right after the currently-playing track (caller wants
    #          it heard next). Default; matches the historical behaviour.
    # "end"  — append to the tail of the queue (caller wants it heard
    #          eventually, no rush). Useful for browsing-and-piling-up
    #          tracks without disrupting what's coming next.
    position: Literal["next", "end"] = "next"


class QueueInjectResponse(BaseModel):
    position: int
    label: str
    queue_depth: int


class QueueReorderRequest(BaseModel):
    from_position: int
    to_position: int


class LibraryStatusResponse(BaseModel):
    track_count: int
    last_scan_at: str | None = None


class QueueExtendRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=50)


class QueueExtendResponse(BaseModel):
    added: int
    queue_depth: int
