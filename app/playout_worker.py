import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable
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
    """Timeline-based playout.

    Listener metrics are intentionally excluded from progression logic. If
    `PlayerState.is_playing` is true, the worker continues to progress live
    playout regardless of audience size.
    """

    def __init__(
        self,
        tick_seconds: float = 0.3,
        *,
        broadcast_engine=None,
        safe_media_path: Callable[[str, AppConfig], Path] | None = None,
    ) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._broadcast_engine = broadcast_engine
        self._safe_media_path = safe_media_path
        self._last_observed_signature: tuple | None = None

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
            self._consume_admin_commands(db, state)
            state_name = self._resolve_state(state)
            self._log_state_snapshot(state, state_name)

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




    def _log_state_snapshot(self, state: PlayerState, state_name: str) -> None:
        queue = self._queue(state)
        current_type = None
        planned_end = None
        if queue and 0 <= state.queue_index < len(queue):
            item = queue[state.queue_index]
            current_type = item.get("type")
            planned_end = item.get("planned_end_epoch")
        signature = (state_name, bool(state.is_playing), state.queue_index, current_type, state.current_sequence_id)
        if signature == self._last_observed_signature:
            return
        self._last_observed_signature = signature
        logger.info(
            "playout.state state=%s is_playing=%s queue_index=%s current_type=%s planned_end_epoch=%s now_epoch=%s current_track_id=%s",
            state_name,
            state.is_playing,
            state.queue_index,
            current_type,
            planned_end,
            round(time.time(), 3),
            state.current_track_id,
        )

    def _consume_admin_commands(self, db: Session, state: PlayerState) -> None:
        commands = self._admin_commands(state)
        if not commands:
            return
        command = commands.pop(0)
        name = command.get("command")
        logger.info("playout.admin.command command=%s station_id=%s", name, command.get("station_id"))
        if name == "force_station_change":
            station_id = command.get("station_id")
            if station_id is None:
                state.last_error = "admin command force_station_change missing station_id"
            else:
                state.current_station_id = int(station_id)
                state.queue_json = "[]"
                state.queue_index = 0
                state.current_track_id = None
                state.is_playing = True
                state.timeline_started_at_epoch = time.time()
                state.current_item_started_at_epoch = 0.0
                state.current_item_expected_end_at_epoch = 0.0
                state.current_sequence_id = (state.current_sequence_id or 0) + 1
                state.playout_mode = "recovering"
                state.last_error = None
        state.admin_commands_json = json.dumps(commands)

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
        self._orchestrate_transition_window(db, state, config, queue, now_epoch)
        planned_end = float(current.get("planned_end_epoch", now_epoch + 1))
        if now_epoch < planned_end:
            return
        logger.info("playout.track.expired queue_index=%s now_epoch=%s planned_end_epoch=%s label=%s", state.queue_index, round(now_epoch, 3), round(planned_end, 3), current.get("label"))
        self._advance(state, queue, now_epoch)

    def _process_transition_tick(self, state: PlayerState, now_epoch: float) -> None:
        queue = self._queue(state)
        if not queue or not (0 <= state.queue_index < len(queue)):
            state.is_playing = False
            state.current_track_id = None
            state.playout_mode = "recovering"
            return
        current = queue[state.queue_index]
        planned_end = float(current.get("planned_end_epoch", now_epoch + 1))
        if now_epoch >= planned_end:
            logger.info("playout.transition.expired queue_index=%s now_epoch=%s planned_end_epoch=%s", state.queue_index, round(now_epoch, 3), round(planned_end, 3))
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
            logger.info("playout.timing.start_assigned queue_index=%s type=%s planned_start_epoch=%s", state.queue_index, item.get("type"), round(float(item["planned_start_epoch"]), 3))
        if item.get("planned_end_epoch") is None:
            duration = self._item_duration_seconds(db, item, state, config)
            item["planned_end_epoch"] = float(item["planned_start_epoch"]) + duration
            logger.info("playout.timing.end_assigned queue_index=%s type=%s duration_s=%s planned_end_epoch=%s", state.queue_index, item.get("type"), round(duration, 3), round(float(item["planned_end_epoch"]), 3))
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

    def _orchestrate_transition_window(self, db: Session, state: PlayerState, config: AppConfig, queue: list[dict], now_epoch: float) -> None:
        # Temporary stability guard: disable ffmpeg transition handoffs until
        # timing/hls rollover behavior is fully stabilized.
        return
        trigger_seconds = 20.0
        if state.current_station_id is None or state.queue_index + 1 >= len(queue):
            return
        current = queue[state.queue_index]
        next_item = queue[state.queue_index + 1]
        if current.get("type") != "track" or next_item.get("type") != "track":
            return
        planned_end = float(current.get("planned_end_epoch", now_epoch + 1))
        if planned_end - now_epoch > trigger_seconds:
            return
        plan = current.get("transition_plan") or {}
        if plan.get("status") == "committed":
            return
        if plan.get("status") != "ready":
            plan = self._prepare_transition_assets(db, state, config, queue)
            current["transition_plan"] = plan
            state.queue_json = json.dumps(queue)
        if plan.get("status") == "ready" and now_epoch >= float(plan.get("transition_at_epoch", planned_end)):
            self._commit_transition_or_fallback(db, state, config, queue, plan, now_epoch)

    def _prepare_transition_assets(self, db: Session, state: PlayerState, config: AppConfig, queue: list[dict]) -> dict:
        if state.current_station_id is None or state.queue_index + 1 >= len(queue):
            return {"status": "skipped"}
        current = queue[state.queue_index]
        next_item = queue[state.queue_index + 1]
        if current.get("type") != "track" or next_item.get("type") != "track":
            return {"status": "skipped"}

        station = db.query(Station).filter(Station.id == state.current_station_id).first()
        current_track = db.query(Track).filter(Track.id == current.get("track_id")).first()
        next_track = db.query(Track).filter(Track.id == next_item.get("track_id")).first()
        if not station or not current_track or not next_track:
            return {"status": "skipped"}

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
        return {
            "status": "ready",
            "station_id": station.id,
            "current_track_path": current_track.file_path,
            "next_track_path": next_track.file_path,
            "dj_audio_path": str(audio_path),
            "transition_at_epoch": max(
                time.time(),
                float(current.get("planned_end_epoch", time.time())) - 20.0,
            ),
        }

    def _commit_transition_or_fallback(
        self,
        db: Session,
        state: PlayerState,
        config: AppConfig,
        queue: list[dict],
        plan: dict,
        now_epoch: float,
    ) -> None:
        if self._broadcast_engine is None or self._safe_media_path is None:
            return
        station_id = int(plan.get("station_id", state.current_station_id or 0))
        try:
            current_path = self._safe_media_path(str(plan["current_track_path"]), config)
            next_path = self._safe_media_path(str(plan["next_track_path"]), config)
            dj_path = self._safe_media_path(str(plan["dj_audio_path"]), config)
            self._broadcast_engine.start_transition(
                station_id=station_id,
                current_track_path=current_path,
                dj_clip_path=dj_path,
                next_track_path=next_path,
            )
            queue.insert(
                state.queue_index + 1,
                {
                    "type": "dj",
                    "label": "DJ break",
                    "script_text": "Live transition",
                    "is_ad_break": False,
                    "audio_path": str(dj_path),
                    "planned_start_epoch": now_epoch,
                    "planned_end_epoch": now_epoch + 10,
                },
            )
            plan["status"] = "committed"
            current = queue[state.queue_index]
            current["transition_plan"] = plan
            self._advance(state, queue, now_epoch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("playout.transition.commit.failed")
            state.last_error = f"transition failed, fallback to next track: {exc}"
            try:
                self._broadcast_engine.start(station_id=station_id, source_track_path=self._safe_media_path(str(plan["next_track_path"]), config))
            except Exception:
                logger.exception("playout.transition.fallback.start.failed")
            plan["status"] = "failed"
            current = queue[state.queue_index]
            current["transition_plan"] = plan
        state.queue_json = json.dumps(queue)

    def _advance(self, state: PlayerState, queue: list[dict], now_epoch: float) -> None:
        next_idx = state.queue_index + 1
        if next_idx >= len(queue):
            logger.info("playout.advance.stop reason=end_of_queue queue_size=%s", len(queue))
            state.is_playing = False
            state.current_track_id = None
            return
        logger.info("playout.advance queue_index=%s->%s next_type=%s next_track_id=%s", state.queue_index, next_idx, queue[next_idx].get("type"), queue[next_idx].get("track_id"))
        state.queue_index = next_idx
        next_item = queue[next_idx]
        state.current_track_id = next_item.get("track_id") if next_item.get("type") == "track" else None
        if next_item.get("planned_start_epoch") is None:
            next_item["planned_start_epoch"] = now_epoch
        state.queue_json = json.dumps(queue)

    @staticmethod
    def _queue(state: PlayerState) -> list[dict]:
        return json.loads(state.queue_json) if state.queue_json else []

    @staticmethod
    def _admin_commands(state: PlayerState) -> list[dict]:
        return json.loads(state.admin_commands_json) if state.admin_commands_json else []
