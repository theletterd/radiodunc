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
