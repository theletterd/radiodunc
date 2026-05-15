from pydantic import BaseModel, Field


class LibraryScanRequest(BaseModel):
    folder_path: str = Field(..., description="Absolute or relative folder path to scan")


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
