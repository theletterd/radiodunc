"""Single-worker guard.

Several subsystems keep process-local state: the prefetch cache
(prefetch.py), the news bulletin cache and its in-flight flag
(news_cache.py), and the DJ handoff tuple (dj_scripts.py). Running more
than one server process splits that state — doubled news-generation
spend, split-brain prefetches, repeated on-air handoffs — without any
error to point at the cause.

This guard makes the constraint loud instead of silent: at startup we
record our PID in a pidfile; if another live process already holds it,
we log CRITICAL naming the consequences. We deliberately warn rather
than refuse to boot — PID reuse and intentional second instances (a
scratch copy on another port) are both possible, and dying over a
heuristic would be worse than the disease.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PIDFILE = Path("generated/radiodunc.pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    return True


def ensure_single_worker(pidfile: Path = PIDFILE) -> bool:
    """Record this process in the pidfile; CRITICAL-log if another live
    process already holds it.

    Returns True when this process is (as far as we can tell) the sole
    worker and the pidfile now names it; False when another live process
    holds the file — in that case the file is left untouched so the
    original owner's claim survives (e.g. a pytest run importing app.main
    while the dev server is up must not steal the server's pidfile).
    """
    try:
        if pidfile.exists():
            try:
                other = int(pidfile.read_text().strip())
            except ValueError:
                other = None
            if other and other != os.getpid() and _pid_alive(other):
                logger.critical(
                    "Another RadioDunc process (pid %d) appears to be running. "
                    "The prefetch cache, news cache, and DJ handoff state are "
                    "process-local, so multiple workers (e.g. uvicorn --workers 2) "
                    "split them: doubled news-generation spend, stale prefetched "
                    "clips, repeated on-air handoffs. Run exactly one worker.",
                    other,
                )
                return False
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
        return True
    except OSError:
        # The guard must never take the app down with it.
        logger.exception("Single-worker pidfile check failed; continuing")
        return True
