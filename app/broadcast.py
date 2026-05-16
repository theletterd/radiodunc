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
    """Single backend-owned broadcast encoder with warm handoff between workers."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self._lock = threading.Lock()
        self._active_slot = "a"
        self._procs: dict[str, subprocess.Popen[str] | None] = {"a": None, "b": None}
        self._station_id: int | None = None
        self._started_at_epoch: float | None = None
        self._last_error: str | None = None

    def stream_url(self) -> str:
        return "/broadcast/live.m3u8"

    def _slot_dir(self, slot: str) -> Path:
        return self.output_dir / slot

    def _inactive_slot(self) -> str:
        return "b" if self._active_slot == "a" else "a"

    def manifest_path(self) -> Path:
        return self._slot_dir(self._active_slot) / "live.m3u8"

    def segment_path(self, segment_name: str) -> Path:
        return self._slot_dir(self._active_slot) / segment_name

    def _clear_slot(self, slot: str) -> None:
        out = self._slot_dir(slot)
        out.mkdir(parents=True, exist_ok=True)
        for item in out.glob("*.ts"):
            item.unlink(missing_ok=True)
        (out / "live.m3u8").unlink(missing_ok=True)

    def _start_proc(self, cmd: list[str], slot: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self._procs[slot] = proc
        return proc

    def _wait_until_hls_ready(self, slot: str, timeout_seconds: float = 6.0) -> bool:
        deadline = time.time() + timeout_seconds
        manifest = self._slot_dir(slot) / "live.m3u8"
        while time.time() < deadline:
            if manifest.exists() and any(self._slot_dir(slot).glob("*.ts")):
                return True
            proc = self._procs.get(slot)
            if proc is None or proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def _stop_slot_locked(self, slot: str) -> None:
        proc = self._procs.get(slot)
        if proc is None:
            return
        self._procs[slot] = None
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in (0, None, -15):
            err = (proc.stderr.read() if proc.stderr else "")[-800:]
            logger.error("broadcast.stop slot=%s nonzero_exit=%s stderr=%s", slot, proc.returncode, err)
            self._last_error = "broadcast process exited unexpectedly"

    def _replace_active_stream(self, new_slot: str, station_id: int) -> None:
        old_slot = self._active_slot
        self._active_slot = new_slot
        self._station_id = station_id
        self._started_at_epoch = time.time()
        self._last_error = None
        self._stop_slot_locked(old_slot)

    def start(self, *, station_id: int, source_track_path: Path) -> None:
        with self._lock:
            target_slot = self._inactive_slot()
            self._clear_slot(target_slot)
            out_manifest = self._slot_dir(target_slot) / "live.m3u8"
            cmd = [
                "ffmpeg", "-y", "-re", "-stream_loop", "-1", "-i", str(source_track_path), "-vn",
                "-c:a", "aac", "-b:a", "192k", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_delete_threshold", "12",
                "-hls_flags", "delete_segments+independent_segments", str(out_manifest),
            ]
            logger.info("broadcast.start station_id=%s source=%s slot=%s", station_id, source_track_path, target_slot)
            self._start_proc(cmd, target_slot)
            if not self._wait_until_hls_ready(target_slot):
                proc = self._procs.get(target_slot)
                stderr_tail = ""
                if proc is not None and proc.stderr is not None:
                    stderr_tail = proc.stderr.read()[-800:]
                logger.error("broadcast.start.not_ready station_id=%s slot=%s stderr=%s", station_id, target_slot, stderr_tail)
                self._last_error = "broadcast start failed to produce hls output"
                self._stop_slot_locked(target_slot)
                return
            self._replace_active_stream(target_slot, station_id)

    def start_transition(self, *, station_id: int, current_track_path: Path, dj_clip_path: Path, next_track_path: Path, tail_seconds: int = 20, fade_seconds: int = 8) -> None:
        with self._lock:
            target_slot = self._inactive_slot()
            self._clear_slot(target_slot)
            out_manifest = self._slot_dir(target_slot) / "live.m3u8"
            filter_complex = (
                f"[0:a]afade=t=out:st=0:d={fade_seconds},volume=0.22[musicduck];"
                "[1:a]adelay=700|700[dj];"
                "[musicduck][dj]amix=inputs=2:duration=longest:dropout_transition=0[transition];"
                "[transition][2:a]concat=n=2:v=0:a=1[out]"
            )
            cmd = [
                "ffmpeg", "-y", "-re", "-sseof", f"-{tail_seconds}", "-i", str(current_track_path), "-i", str(dj_clip_path),
                "-re", "-stream_loop", "-1", "-i", str(next_track_path), "-vn", "-filter_complex", filter_complex,
                "-map", "[out]", "-c:a", "aac", "-b:a", "192k", "-f", "hls", "-hls_time", "2",
                "-hls_list_size", "6", "-hls_delete_threshold", "12", "-hls_flags", "delete_segments+independent_segments", str(out_manifest),
            ]
            logger.info("broadcast.transition.start station_id=%s current=%s dj=%s next=%s slot=%s", station_id, current_track_path, dj_clip_path, next_track_path, target_slot)
            self._start_proc(cmd, target_slot)
            if not self._wait_until_hls_ready(target_slot):
                proc = self._procs.get(target_slot)
                stderr_tail = ""
                if proc is not None and proc.stderr is not None:
                    stderr_tail = proc.stderr.read()[-800:]
                logger.error("broadcast.transition.not_ready station_id=%s slot=%s stderr=%s", station_id, target_slot, stderr_tail)
                self._last_error = "broadcast transition failed to produce hls output"
                self._stop_slot_locked(target_slot)
                return
            self._replace_active_stream(target_slot, station_id)

    def stop(self) -> None:
        with self._lock:
            self._stop_slot_locked("a")
            self._stop_slot_locked("b")
            self._station_id = None
            self._started_at_epoch = None

    def status(self) -> BroadcastStatus:
        with self._lock:
            active_proc = self._procs.get(self._active_slot)
            running = active_proc is not None and active_proc.poll() is None
            if active_proc is not None and not running and self._last_error is None:
                self._last_error = "broadcast process is not running"
            return BroadcastStatus(running=running, station_id=self._station_id, stream_url=self.stream_url(), started_at_epoch=self._started_at_epoch, last_error=self._last_error)
