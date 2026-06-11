"""Structured-logging plumbing shared across app modules.

Lives in its own module (rather than main.py) so the cache subsystems
(prefetch, news_cache) can log through the same helpers without importing
main — which would be a circular import, since main imports them.
"""

import logging
import os

logger = logging.getLogger("app.main")

# Capture reserved fields both before and after a format call so that
# attributes set as side-effects of format() (e.g. `message`) are excluded.
_RESERVED_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys()) | {"message"}


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_LOG_RECORD_FIELDS and not key.startswith("_")
        }
        if not extras:
            return base
        extra_text = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} {extra_text}"


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = ContextFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        return

    for handler in root_logger.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.log(level, "%s %s", event, details)
