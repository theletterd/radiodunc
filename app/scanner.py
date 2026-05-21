from pathlib import Path
import logging

from mutagen import File as MutagenFile
from sqlalchemy.orm import Session

from .models import Track

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg"}

# How many new tracks to accumulate before flushing + committing. 200 is a
# compromise: small enough to bound peak memory on huge libraries (each Track
# only lives in the session for the chunk it was added in), large enough that
# commit overhead doesn't dominate the wall-clock time (a commit costs roughly
# one fsync; doing it every track would be ~10x slower on a typical SSD).
SCAN_COMMIT_CHUNK_SIZE = 200

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

    # NOTE: chunked commits. Every SCAN_COMMIT_CHUNK_SIZE imported tracks we
    # commit + expire — so peak memory stays bounded regardless of library
    # size, and a crash partway through leaves all previously-committed
    # tracks in the DB (the next scan picks up from there because the
    # duplicate-check query sees committed rows). The trade-off vs the old
    # "one big commit at the end" path is a small wall-clock overhead per
    # chunk, dominated by the fsync.
    uncommitted_in_chunk = 0
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
            uncommitted_in_chunk += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"file_path": file_path, "error": str(exc)})
            continue

        if uncommitted_in_chunk >= SCAN_COMMIT_CHUNK_SIZE:
            # commit() flushes + writes to disk; expire_all() drops the
            # in-memory objects so the next chunk doesn't accumulate them.
            # The duplicate-check query below still works correctly because
            # SQLAlchemy will re-load any row it needs from the DB.
            db.commit()
            db.expire_all()
            uncommitted_in_chunk = 0

    # Final chunk (the remainder that didn't fill a full SCAN_COMMIT_CHUNK_SIZE).
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
