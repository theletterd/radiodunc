from pathlib import Path
import logging

from mutagen import File as MutagenFile
from sqlalchemy.orm import Session

from .models import Track

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg"}

logger = logging.getLogger(__name__)


def _safe_tag(tags: dict, key: str):
    if not tags:
        return None
    value = tags.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _extract_track_metadata(file_path: Path) -> dict:
    audio = MutagenFile(file_path, easy=True)
    if audio is None:
        raise ValueError("Unsupported or unreadable audio file")

    info = getattr(audio, "info", None)
    duration_seconds = float(getattr(info, "length", 0.0) or 0.0)
    bitrate = getattr(info, "bitrate", None)
    bitrate = int(bitrate) if bitrate else None

    tags = audio.tags or {}
    return {
        "title": _safe_tag(tags, "title"),
        "artist": _safe_tag(tags, "artist"),
        "album": _safe_tag(tags, "album"),
        "year": _safe_tag(tags, "date") or _safe_tag(tags, "year"),
        "genre": _safe_tag(tags, "genre"),
        "duration_seconds": duration_seconds,
        "bitrate": bitrate,
    }


def scan_library(folder_path: str, db: Session) -> dict:
    root = Path(folder_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Folder does not exist or is not a directory: {root}")

    scanned = 0
    imported = 0
    skipped_duplicates = 0
    errors: list[dict] = []

    logger.info("library.scan.started", extra={"folder": str(root)})

    # NOTE: every new Track is added to the session and we commit once at the
    # end. On very large libraries (10k+ files) this holds the full set in
    # memory and produces one huge transaction. See TODO.md ("Chunked scan
    # commits") for the follow-up — flush + commit every N tracks so memory
    # stays bounded and partial progress survives a crash.
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        scanned += 1
        file_path = str(path)
        existing = db.query(Track).filter(Track.file_path == file_path).first()
        if existing:
            skipped_duplicates += 1
            continue

        try:
            metadata = _extract_track_metadata(path)
            track = Track(file_path=file_path, **metadata)
            db.add(track)
            imported += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"file_path": file_path, "error": str(exc)})

    db.commit()
    logger.info(
        "library.scan.completed",
        extra={
            "folder": str(root),
            "scanned": scanned,
            "imported": imported,
            "skipped_duplicates": skipped_duplicates,
            "errors": len(errors),
        },
    )
    return {
        "scanned": scanned,
        "imported": imported,
        "skipped_duplicates": skipped_duplicates,
        "errors": errors,
    }
