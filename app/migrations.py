"""Inline DB migrations, run once at app startup (module load of main.py).

No Alembic — deliberate (see TODO.md "deliberately accepted"). Each
migration is a sibling function invoked from run_all(), and each must be
idempotent: filter to only the rows/columns that need touching so
re-running on a fresh or already-migrated DB is a cheap no-op.
"""

import logging

from .database import engine
from sqlalchemy import text

logger = logging.getLogger(__name__)


def migrate_drop_legacy_schema() -> None:
    """Drop legacy multi-station tables and obsolete PlayerState columns."""
    with engine.begin() as conn:
        for table in ("stations", "favorite_stations", "recent_stations"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(player_state)")).fetchall()
        }
        legacy_columns = [
            "current_station_id",
            "timeline_started_at_epoch",
            "current_item_started_at_epoch",
            "current_item_expected_end_at_epoch",
            "current_sequence_id",
            "playout_mode",
        ]
        for col in legacy_columns:
            if col in columns:
                try:
                    conn.execute(text(f"ALTER TABLE player_state DROP COLUMN {col}"))
                except Exception:  # noqa: BLE001
                    pass  # SQLite < 3.35; leave the column in place

        # Add is_ad column to dj_clips if missing.
        dj_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(dj_clips)")).fetchall()}
        if "is_ad" not in dj_cols:
            conn.execute(text("ALTER TABLE dj_clips ADD COLUMN is_ad BOOLEAN NOT NULL DEFAULT 0"))


def migrate_dj_clip_paths_after_generated_rename() -> None:
    """Rewrite ``dj_clips.audio_path`` from the old ``generated_audio/``
    prefix to the new ``generated/`` one introduced in PR #159.

    That rename moved the cache directory on disk but didn't migrate
    existing DB rows, so every clip generated before the rename
    (stingers, ads, news, transitions, previews) 404'd at serve time
    because ``_safe_media_path`` resolved against ``generated_audio/``
    which no longer existed. Symptom: skip-stingers stopped playing.

    Idempotent — the WHERE clause filters to only rows with the legacy
    prefix, so re-running on a clean DB is a no-op.
    """
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE dj_clips "
            "SET audio_path = 'generated/' || substr(audio_path, length('generated_audio/') + 1) "
            "WHERE audio_path LIKE 'generated_audio/%'"
        ))
        rewritten = result.rowcount
    if rewritten:
        logger.info(
            "migrated %d dj_clip audio_path(s) from generated_audio/ → generated/",
            rewritten,
        )


def run_all() -> None:
    migrate_drop_legacy_schema()
    migrate_dj_clip_paths_after_generated_rename()
