from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BroadcastStatus:
    running: bool
    station_id: int | None
    stream_url: str
    started_at_epoch: float | None
    last_error: str | None


class BroadcastEngine:
    """Single backend-owned broadcast encoder.

    Uses ffmpeg to loop a source track into HLS output as a first step toward
    full timeline-driven radio playout.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._station_id: int | None = None
        self._started_at_epoch: float | None = None
        self._last_error: str | None = None

    def stream_url(self) -> str:
        return "/broadcast/live.m3u8"

    def start(self, *, station_id: int, source_track_path: Path) -> None:
        with self._lock:
            self._stop_locked()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            for item in self.output_dir.glob("*.ts"):
                item.unlink(missing_ok=True)
            (self.output_dir / "live.m3u8").unlink(missing_ok=True)

            cmd = [
                "ffmpeg",
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(source_track_path),
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-f",
                "hls",
                "-hls_time",
                "2",
                "-hls_list_size",
                "6",
                "-hls_flags",
                "delete_segments+append_list",
                str(self.output_dir / "live.m3u8"),
            ]
            logger.info("broadcast.start station_id=%s source=%s", station_id, source_track_path)
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self._station_id = station_id
            self._started_at_epoch = time.time()
            self._last_error = None

    def _stop_locked(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None, -15):
            err = (proc.stderr.read() if proc.stderr else "")[-800:]
            logger.error("broadcast.stop nonzero_exit=%s stderr=%s", proc.returncode, err)
            self._last_error = "broadcast process exited unexpectedly"
        self._station_id = None
        self._started_at_epoch = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def status(self) -> BroadcastStatus:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            if self._proc is not None and not running and self._last_error is None:
                self._last_error = "broadcast process is not running"
            return BroadcastStatus(
                running=running,
                station_id=self._station_id,
                stream_url=self.stream_url(),
                started_at_epoch=self._started_at_epoch,
                last_error=self._last_error,
            )
