import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .config import AppConfig, load_config
from .database import SessionLocal
from .dj_scripts import generate_dj_script
from .models import PlayerState, Station, Track
from .schemas import DJScriptGenerateRequest
from .tts import build_tts_provider, get_or_create_dj_clip

logger = logging.getLogger(__name__)


def _daypart_greeting(config: AppConfig) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(config.alerts.local_time_zone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    hour = datetime.now(zone).hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Late night vibes"


def _local_time_announcement(config: AppConfig) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(config.alerts.local_time_zone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    stamp = datetime.now(zone).strftime("%-I:%M%p").lower()
    return f"It's {stamp}"


@dataclass(frozen=True)
class PlayoutState:
    IDLE: str = "idle"
    PLAYING_TRACK: str = "playing_track"
    PLAYING_TRANSITION: str = "playing_transition"
    RECOVERING: str = "recovering"


class PlayoutWorker:
    def __init__(self, tick_seconds: float = 0.3) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="playout-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("playout.worker.tick.failed")
            self._stop_event.wait(self._tick_seconds)

    def _tick(self) -> None:
        db = SessionLocal()
        try:
            state = db.query(PlayerState).order_by(PlayerState.id.asc()).first()
            if not state:
                return
            config = load_config()
            now_epoch = time.time()
            state_name = self._resolve_state(state)

            if state_name == PlayoutState.IDLE:
                state.playout_mode = "stopped"
                return
            if state_name == PlayoutState.RECOVERING:
                state.playout_mode = "recovering"
                self._recover_timing(db, state, config, now_epoch)
            elif state_name == PlayoutState.PLAYING_TRACK:
                state.playout_mode = "live"
                self._process_track_tick(db, state, config, now_epoch)
            elif state_name == PlayoutState.PLAYING_TRANSITION:
                state.playout_mode = "live"
                self._process_transition_tick(state, now_epoch)

            db.commit()
        finally:
            db.close()

    def _resolve_state(self, state: PlayerState) -> str:
        if not state.is_playing:
            return PlayoutState.IDLE
        queue = self._queue(state)
        if not queue or not (0 <= state.queue_index < len(queue)):
            return PlayoutState.RECOVERING
        item = queue[state.queue_index]
        return PlayoutState.PLAYING_TRANSITION if item.get("type") == "dj" else PlayoutState.PLAYING_TRACK

    def _recover_timing(self, db: Session, state: PlayerState, config: AppConfig, now_epoch: float) -> None:
        queue = self._queue(state)
        if not queue:
            state.is_playing = False
            state.current_track_id = None
            state.playout_mode = "stopped"
            return
        if state.queue_index < 0:
            state.queue_index = 0
        if state.queue_index >= len(queue):
            state.is_playing = False
            state.current_track_id = None
            state.playout_mode = "stopped"
            return
        self._ensure_planned_timing(db, state, config, now_epoch)

    def _process_track_tick(self, db: Session, state: PlayerState, config: AppConfig, now_epoch: float) -> None:
        queue = self._queue(state)
        if not queue or not (0 <= state.queue_index < len(queue)):
            state.is_playing = False
            state.current_track_id = None
            state.playout_mode = "recovering"
            return
        current = queue[state.queue_index]
        self._ensure_item_timing(db, current, state, config, now_epoch)
        if now_epoch < float(current.get("planned_end_epoch", now_epoch + 1)):
            return

        self._insert_transition_after_current(db, state, config, queue)
        self._advance(state, queue, now_epoch)

    def _process_transition_tick(self, state: PlayerState, now_epoch: float) -> None:
        queue = self._queue(state)
        if not queue or not (0 <= state.queue_index < len(queue)):
            state.is_playing = False
            state.current_track_id = None
            state.playout_mode = "recovering"
            return
        current = queue[state.queue_index]
        if now_epoch >= float(current.get("planned_end_epoch", now_epoch + 1)):
            self._advance(state, queue, now_epoch)

    def _ensure_planned_timing(self, db: Session, state: PlayerState, config: AppConfig, now_epoch: float) -> None:
        queue = self._queue(state)
        if not queue:
            return
        item = queue[state.queue_index]
        self._ensure_item_timing(db, item, state, config, now_epoch)

    def _ensure_item_timing(self, db: Session, item: dict, state: PlayerState, config: AppConfig, now_epoch: float) -> None:
        if item.get("planned_start_epoch") is None:
            item["planned_start_epoch"] = now_epoch
        if item.get("planned_end_epoch") is None:
            duration = self._item_duration_seconds(db, item, state, config)
            item["planned_end_epoch"] = float(item["planned_start_epoch"]) + duration
        state.current_item_started_at_epoch = float(item.get("planned_start_epoch", 0.0))
        state.current_item_expected_end_at_epoch = float(item.get("planned_end_epoch", 0.0))
        state.queue_json = json.dumps(self._queue(state))

    def _item_duration_seconds(self, db: Session, item: dict, state: PlayerState, config: AppConfig) -> float:
        if item.get("type") == "track":
            track_id = item.get("track_id")
            track = db.query(Track).filter(Track.id == track_id).first() if track_id is not None else None
            if track and track.duration_seconds and track.duration_seconds > 0:
                return float(track.duration_seconds)
            return 180.0
        return 10.0

    def _insert_transition_after_current(self, db: Session, state: PlayerState, config: AppConfig, queue: list[dict]) -> None:
        if state.current_station_id is None or state.queue_index + 1 >= len(queue):
            return
        current = queue[state.queue_index]
        next_item = queue[state.queue_index + 1]
        if current.get("type") != "track" or next_item.get("type") != "track":
            return

        station = db.query(Station).filter(Station.id == state.current_station_id).first()
        current_track = db.query(Track).filter(Track.id == current.get("track_id")).first()
        next_track = db.query(Track).filter(Track.id == next_item.get("track_id")).first()
        if not station or not current_track or not next_track:
            return

        payload_script = DJScriptGenerateRequest(
            previous_track_id=current_track.id,
            next_track_id=next_track.id,
            include_weather=False,
            include_news=False,
            include_fake_ad=False,
            max_sentences=3,
        )
        script = generate_dj_script(
            station=station,
            payload=payload_script,
            previous_track=current_track,
            next_track=next_track,
            config=config if config.radio_polish_enabled else None,
        )
        opener = f"{_daypart_greeting(config)} from {station.name}. " if config.daypart_programming_enabled else ""
        time_check = f"{_local_time_announcement(config)} and you're listening to {station.name}. " if config.time_announcement_enabled else ""
        script_text = f"{time_check}{opener}{script.script_text}"

        try:
            provider = build_tts_provider(config)
        except ValueError:
            provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
        _clip, audio_path, _cached = get_or_create_dj_clip(
            db,
            script_text=script_text,
            voice=None,
            provider=provider,
            persist=False,
        )
        queue.insert(
            state.queue_index + 1,
            {
                "type": "dj",
                "label": f"{station.dj_name or 'DJ'} break",
                "script_text": script_text,
                "is_ad_break": False,
                "audio_path": str(audio_path),
            },
        )
        state.queue_json = json.dumps(queue)

    def _advance(self, state: PlayerState, queue: list[dict], now_epoch: float) -> None:
        next_idx = state.queue_index + 1
        if next_idx >= len(queue):
            state.is_playing = False
            state.current_track_id = None
            return
        state.queue_index = next_idx
        next_item = queue[next_idx]
        state.current_track_id = next_item.get("track_id") if next_item.get("type") == "track" else None
        if next_item.get("planned_start_epoch") is None:
            next_item["planned_start_epoch"] = now_epoch
        state.queue_json = json.dumps(queue)

    @staticmethod
    def _queue(state: PlayerState) -> list[dict]:
        return json.loads(state.queue_json) if state.queue_json else []
